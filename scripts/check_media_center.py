#!/usr/bin/env python3
"""Isolated Media Center browser check: real API, FFmpeg and HLS; no live data.

Run with .venv/bin/python scripts/check_media_center.py. Requires local Chrome,
FFmpeg and the normal server dependencies. Artifacts go to /tmp/pc-media-check.
Authentication and relay documents are fixtures; all media requests/transcodes
and browser rendering use the real implementation.
"""
import asyncio
import base64
import copy
from contextvars import ContextVar
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urljoin

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import httpx
import uvicorn
import websockets
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from PIL import Image
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.models import User

from app.routers import media_center as routes, jellyfin
from app.services import media_center as media

OWNER, VIEWER = "11" * 32, "22" * 32
ARTIFACTS = Path("/tmp/pc-media-check")
CDP_PORT = int(os.environ.get("PC_CHECK_PORT", "19439"))


class Browser:
    def __init__(self, ws):
        self.ws, self.sequence = ws, 0

    async def call(self, method, params=None):
        self.sequence += 1
        await self.ws.send(json.dumps({"id": self.sequence, "method": method, "params": params or {}}))
        while True:
            message = json.loads(await self.ws.recv())
            if message.get("id") == self.sequence:
                assert "error" not in message, message
                return message.get("result", {})

    async def js(self, expression, gesture=False):
        result = await self.call("Runtime.evaluate", {"expression": expression, "returnByValue": True,
                                                     "awaitPromise": True, "userGesture": gesture})
        assert "exceptionDetails" not in result, result
        return result["result"].get("value")

    async def until(self, expression):
        for _ in range(200):
            if await self.js(expression):
                return
            await asyncio.sleep(.1)
        raise AssertionError({"waiting_for": expression, "player": await self.js(
            "({status:document.querySelector('#mc-status')?.textContent,time:document.querySelector('video')?.currentTime,error:document.querySelector('video')?.error?.message,dialog:document.querySelector('dialog')?.outerHTML})")})

    async def screenshot(self, name):
        result = await self.call("Page.captureScreenshot", {"format": "png"})
        (ARTIFACTS / name).write_bytes(base64.b64decode(result["data"]))


# THE PLAYER USES THE SPACE IT IS SHOWN IN. Measured after playback starts, in VISUAL px throughout
# (body{zoom} sits between layout and visual px on desktop tiers — never mix the two):
#   * the whole player — title, picture, controls — fits inside the scroller it lives in (the page,
#     or a desktop window's feed), so pressing Play shows the picture and its controls together;
#   * the picture is as big as that allows: either it spans the full width of the player, or it is
#     as tall as the scroller leaves room for. `max-height:65vh` under a toolbar of three full-width
#     selects did neither on a phone, and in a desktop window `vh` is the SCREEN, so the picture ran
#     off the bottom of any window shorter than the monitor.
PLAYER_LAYOUT = """JSON.stringify((()=>{
  const box=document.querySelector('#mc-playback'), t=document.querySelector('#mc-playing');
  const st=box.querySelector('.mc-stage'), v=document.querySelector('#mc-player'), bar=box.querySelector('.mc-controls');
  if(!st||!bar) return {error:'the player has no .mc-stage / .mc-controls'};
  box.scrollIntoView({block:'start'});
  let sc=box.parentElement;
  while(sc&&sc!==document.body&&sc!==document.documentElement){
    const o=getComputedStyle(sc).overflowY; if(o==='auto'||o==='scroll')break; sc=sc.parentElement; }
  const inPage=!sc||sc===document.body||sc===document.documentElement;
  const R=inPage?{top:0,bottom:innerHeight}:sc.getBoundingClientRect();
  const top=Math.max(0,R.top), bottom=Math.min(innerHeight,R.bottom);
  const B=box.getBoundingClientRect(), T=t.getBoundingClientRect(), S=st.getBoundingClientRect(),
        V=v.getBoundingClientRect(), C=bar.getBoundingClientRect();
  const z=box.offsetWidth?B.width/box.offsetWidth:1, cs=getComputedStyle(box);
  const contentW=B.width-(parseFloat(cs.paddingLeft)+parseFloat(cs.paddingRight))*z;
  const ar=(v.videoWidth||16)/(v.videoHeight||9);
  const picW=Math.min(V.width,V.height*ar);
  return {scroller: inPage?'page':(sc.id||sc.className), avail: Math.round(bottom-top),
          boxTop: Math.round(B.top-top), boxBottom: Math.round(B.bottom-bottom),
          titleAbove: T.bottom<=S.top+0.5, controlsBelow: C.top>=S.bottom-0.5,
          stageW: Math.round(S.width), contentW: Math.round(contentW), videoW: Math.round(V.width),
          videoH: Math.round(V.height), picW: Math.round(picW),
          roomH: Math.round((bottom-top)-(B.height-S.height))};
})())"""


def assert_player_uses_space(name, m):
    assert "error" not in m, f"{name}: {m['error']}"
    assert m["titleAbove"] and m["controlsBelow"], (
        f"{name}: expected title / picture / controls stacked in that order: {m}")
    assert m["stageW"] >= m["contentW"] - 2 and m["videoW"] >= m["stageW"] - 1, (
        f"{name}: the picture area is narrower than the player ({m['videoW']} of {m['contentW']}px): {m}")
    assert m["boxTop"] >= -1 and m["boxBottom"] <= 1, (
        f"{name}: the player does not fit the {m['avail']}px it is shown in "
        f"(top {m['boxTop']}, overshoots the bottom by {m['boxBottom']}px) in {m['scroller']}: {m}")
    assert m["picW"] >= m["stageW"] - 2 or m["videoH"] >= m["roomH"] - 16, (
        f"{name}: the picture is neither full width ({m['picW']} of {m['stageW']}px) nor as tall as "
        f"the space allows ({m['videoH']} of ~{m['roomH']}px): {m}")


async def main():
    chrome = shutil.which("google-chrome") or "/opt/google/chrome/chrome"
    assert Path(chrome).exists(), "Chrome is required for this check"
    ARTIFACTS.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="pc-media-check-") as directory:
        temp = Path(directory)
        source = temp / "Adventure" / "Northern Lights.mkv"
        source.parent.mkdir()
        captions = temp / 'captions.srt'
        captions.write_text('1\n00:00:00,000 --> 00:00:17,500\nMedia Center subtitle test\n')
        os.environ["POSTERCHANAI_MEDIA_ROOTS"] = str(temp)
        os.environ["POSTERCHANAI_MEDIA_CACHE"] = str(temp / ".cache")
        subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=320x240:rate=24",
                        "-f", "lavfi", "-i", "sine=frequency=440", "-f", "lavfi", "-i", "sine=frequency=880", '-i', str(captions),
                        '-map','0:v:0','-map','1:a:0','-map','2:a:0','-map','3:s:0',
                        '-metadata:s:a:0','language=jpn','-metadata:s:a:1','language=eng','-metadata:s:s:0','language=eng','-c:s','srt',
                        "-t", "18", "-c:v", "libx264",
                        "-threads", "1", "-c:a", "aac", str(source)], check=True, timeout=30)
        Image.new("RGB", (600, 400), "#195d78").save(source.parent / "poster.jpg")
        # A PATHOLOGICAL TITLE, because that is the bug. "Media center UI gets messed up with long
        # video title": a grid item defaults to `min-width:auto` and so refuses to shrink below its
        # content, and an unbroken name is the case with no space to wrap at. Two tiles with ordinary
        # names can never show it — the grid only goes uneven once one item refuses to fit its track.
        other = source.parent / ("A" + "-title-that-simply-keeps-going" * 6 + "-with-no-spaces.mkv")
        shutil.copyfile(source, other)
        scanned, _ = media.scan(str(temp))
        library = {"id": "test", "name": "Movies & Shows", "owner": OWNER, "shared_with": [VIEWER],
                   "folder": str(temp), "encoder": "cpu", "pages": ["page:test:fixture"], "count": len(scanned)}
        documents = {"index": {"ids": ["test"]}, "library:test": library, "page:test:fixture": scanned}
        async def read(key):
            return copy.deepcopy(documents.get(key))
        async def write(key, value):
            documents[key] = copy.deepcopy(value)
        media.read, media.write = read, write
        # Hold the first scan after one real item: browsing/playback must work
        # while the remaining catalog is still being discovered.
        scan_release = threading.Event()
        def staged_scan(folder, previous=None, on_item=None):
            if on_item:on_item(scanned[0])
            if not scan_release.wait(45):raise RuntimeError('Browser did not release staged scan')
            for item in scanned[1:]:
                if on_item:on_item(item)
            return scanned, 0
        real_scan = media.scan
        media.scan = staged_scan
        documents['page:test:fixture'] = []
        library['count'] = 0
        # Two isolated nodes with different local proxy topology. ContextVar keeps
        # their settings separate while both test servers run in this process.
        backend = ContextVar("media_backend", default="")
        routes.settings_store.get = lambda key, default=None: backend.get() if key == "media_center_server_url" else default
        routes.lb_auth.shared_secret = lambda: "isolated-media-test-peer-secret"
        # Identity and membership are fixtures in this check; never query a live instance registry.
        # Membership authorization itself is covered by test_instance_membership_routes.py.
        async def fixture_membership(user):
            if media.identity(user) not in {OWNER, VIEWER}:
                from fastapi import HTTPException
                raise HTTPException(403, 'Unknown fixture member')
            return user
        routes.instance_membership.require_user = fixture_membership
        nas = FastAPI()
        nas.include_router(routes.router)
        @nas.middleware("http")
        async def nas_config(request, next_handler):
            token = backend.set("")
            try:
                return await next_handler(request)
            finally:
                backend.reset(token)
        app = FastAPI()
        app.include_router(routes.router)
        app.include_router(jellyfin.account_router)
        app.include_router(jellyfin.router)
        @app.middleware("http")
        async def edge_config(request, next_handler):
            token = backend.set(nas_url)
            try:
                return await next_handler(request)
            finally:
                backend.reset(token)
        app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")
        def user(request: Request):
            key = request.headers.get("X-Test-Viewer") or request.query_params.get("viewer") or OWNER
            return SimpleNamespace(nostr_npub=key, username='fixture-'+key[:8], is_admin=key == OWNER, can_media=True)
        app.dependency_overrides[routes.media_user_optional] = user
        # Redeeming a TV secret rechecks a registered account independently of browser auth.
        # Keep that lookup isolated too; no request in this harness should touch real users.
        engine = create_engine('sqlite:///' + str(temp / 'users.db'))
        User.__table__.create(engine)
        with Session(engine) as db:
            for name, key in [('owner', OWNER), ('viewer', VIEWER)]:
                db.add(User(username=name, password_hash='fixture-not-a-password', nostr_npub=key,
                            is_admin=key == OWNER, can_media=True))
            db.commit()
        def database():
            with Session(engine) as db:
                yield db
        app.dependency_overrides[routes.get_db] = database
        nas.dependency_overrides[routes.get_db] = database

        javascript = (ROOT / "static/js/client/app.js").read_text()
        functions = javascript[javascript.index("  let _mediaCenterLibraryTab="):javascript.index("  // ---------- torrents (NIP-35")]
        bootstrap = """
          const $=s=>document.querySelector(s);let VIEW='media-center';const _instanceBase=()=>location.origin;
          const enc=s=>String(s).replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('"','&quot;');
          const nativeFetch=window.fetch.bind(window);const fetch=(url,opts={})=>nativeFetch(url,{...opts,headers:{...Object.fromEntries(new Headers(opts.headers||{})),
            'X-Test-Viewer':new URLSearchParams(location.search).get('viewer')||'OWNER'}});
          let _aiToken='fixture',_aiAuth={};const _setAiToken=t=>{_aiToken=t;};const ensureAiSession=async()=>({});
          const loadHls=async()=>{};const attachUserAutocomplete=()=>()=>{};const toast=s=>{window.lastToast=s;};
          const copyValue=(v,msg)=>{window.lastCopied=v;window.lastToast=msg||'copied';return Promise.resolve(true);};
        """.replace("'OWNER'", json.dumps(OWNER))
        @app.get("/", response_class=HTMLResponse)
        async def page():
            return ("<!doctype html><meta name='viewport' content='width=device-width,initial-scale=1'>"
                    "<link rel='stylesheet' href='/static/css/client.css'>"
                    "<link rel='stylesheet' href='/static/css/media-center-ui.css'>"
                    "<style>body{display:block!important;margin:0!important;padding:12px}#feed{width:100%;max-width:1500px;margin:auto}</style>"
                    "<main id='feed'></main><script src='/static/js/client/sprite.js'></script><script src='/static/vendor/hls/hls.min.js'></script><script>" + bootstrap + functions +
                    "renderMediaCenter().then(async()=>{await document.querySelector('.mc-library-open')?.onclick?.();document.title='READY';});</script><script src='/static/js/client/media-center-ui.js'></script>")
        server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0, log_level="error"))
        nas_server = uvicorn.Server(uvicorn.Config(nas, host="127.0.0.1", port=0, log_level="error"))
        nas_task = asyncio.create_task(nas_server.serve())
        server_task = asyncio.create_task(server.serve())
        process = subprocess.Popen([chrome, "--headless=new", "--no-sandbox", "--disable-gpu",
                                    "--disable-dev-shm-usage", "--autoplay-policy=no-user-gesture-required",
                                    f"--remote-debugging-port={CDP_PORT}", "--remote-debugging-address=127.0.0.1",
                                    "--user-data-dir=" + str(temp / "chrome"), "about:blank"],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            async with httpx.AsyncClient() as client:
                for _ in range(100):
                    try:
                        pages = (await client.get(f"http://127.0.0.1:{CDP_PORT}/json/list")).json()
                        browser_page = next(p for p in pages if p["type"] == "page")
                        if server.started and nas_server.started:
                            break
                    except Exception:
                        pass
                    await asyncio.sleep(.1)
                app_url = "http://127.0.0.1:" + str(server.servers[0].sockets[0].getsockname()[1])
                nas_url = "http://127.0.0.1:" + str(nas_server.servers[0].sockets[0].getsockname()[1])
                scan_response = await client.post(f'{app_url}/api/media-center/test/scan')
                assert scan_response.status_code == 200, (scan_response.status_code, scan_response.text)
                async with websockets.connect(browser_page["webSocketDebuggerUrl"], max_size=32 * 1024 * 1024) as ws:
                    browser = Browser(ws)
                    await browser.call("Page.enable")
                    for name, width, height in (("phone", 390, 844), ("tv", 1920, 1080)):
                        await browser.call("Emulation.setDeviceMetricsOverride", {"width": width, "height": height,
                                                                                  "deviceScaleFactor": 1, "mobile": False})
                        await browser.call("Page.navigate", {"url": f"{app_url}/"})
                        await browser.until("document.title==='READY'")
                        # WAIT FOR THE TAB, DO NOT SAMPLE FOR IT ONCE. `document.title==='READY'`
                        # says the fixture is ready, not that renderMediaCenter has painted — this
                        # queried the element in the same turn and read `null` off a page that was
                        # about to have it. The same single-sample mistake as the Android launch
                        # gate: a miss here is "not yet", never "not there", and a tab that never
                        # appears still fails when the wait runs out.
                        # WAIT FOR THE TAB, DO NOT SAMPLE FOR IT ONCE. `document.title==='READY'` says the
                        # fixture is ready, not that renderMediaCenter has painted. Same rule as the
                        # Android launch gate: a miss is "not yet", never "not there", and a tab that
                        # genuinely never appears still fails when the wait runs out.
                        await browser.until("!!document.querySelector('#mc-tab-mine')")
                        assert await browser.js("document.querySelector('#mc-tab-mine').getAttribute('aria-selected')==='true'")
                        await browser.js("document.querySelector('#mc-tab-shared').click()", True)
                        await browser.until("document.querySelector('#mc-libraries')?.textContent.includes('No libraries have been shared')")
                        await browser.js("document.querySelector('#mc-tab-mine').click()", True)
                        await browser.until("document.querySelector('.mc-directory')?.textContent.includes('Adventure')")
                        await browser.until("document.querySelector('.mc-directory img')?.naturalWidth>0")
                        await browser.screenshot(name + '-folders.png')
                        await browser.js("document.querySelector('.mc-directory').click()", gesture=True)
                        await browser.until("document.querySelectorAll('.mc-folder-trail button').length===2")
                        await browser.until("!!document.querySelector('.mc-actions-menu')")
                        assert await browser.js("!document.querySelector('.mc-actions-menu').open")
                        await browser.js("document.querySelector('.mc-actions-menu>summary').click()", gesture=True)
                        for index in range(3):
                            await browser.js("document.querySelectorAll('.mc-tool-card')["+str(index)+"].open=true")
                            await asyncio.sleep(.1)
                            assert await browser.js('document.documentElement.scrollWidth<=innerWidth')
                            await browser.screenshot(name + '-control-' + str(index) + '.png')
                            await browser.js("document.querySelectorAll('.mc-tool-card')["+str(index)+"].open=false")
                        await browser.js("document.querySelector('.mc-actions-menu').open=false")
                        pending = (await client.post(f'{app_url}/jellyfin/QuickConnect/Initiate')).json()
                        await browser.js("document.querySelector('#mc-jellyfin').open=true")
                        await browser.js("document.querySelector('#mc-jellyfin-approve input').value=" + json.dumps(pending['Code']) +
                                         ";document.querySelector('#mc-jellyfin-approve').requestSubmit()", gesture=True)
                        await browser.until("window.lastToast?.startsWith('Jellyfin app approved')")
                        approved = await client.get(f'{app_url}/jellyfin/QuickConnect/Connect', params={'Secret': pending['Secret']})
                        assert approved.json()['Authenticated'] is True
                        import time
                        device_key = jellyfin.account_key(jellyfin.account_id(SimpleNamespace(nostr_npub=OWNER)))
                        documents[device_key] = {'pubkey':OWNER, 'sessions':[{'id':'d'*32, 'hash':'fixture-only',
                            'expires':int(time.time())+3600, 'created':int(time.time()),
                            'device':'Living room TV', 'client':'Jellyfin Android TV', 'version':'test'}]}
                        await browser.js("document.querySelector('#mc-jellyfin-refresh').click()", gesture=True)
                        await browser.until("document.querySelector('.mc-device-row')?.textContent.includes('Living room TV')")
                        await browser.js("document.querySelector('.mc-device-row button').click()", gesture=True)
                        await browser.until("document.querySelector('#mc-jellyfin-devices')?.textContent==='No connected devices.'")
                        assert documents[device_key]['sessions'] == []

                        assert await browser.js('document.documentElement.scrollWidth<=innerWidth')
                        await browser.js("document.querySelector('#mc-jellyfin-revoke').click()", gesture=True)
                        await browser.until("window.lastToast==='All Jellyfin apps disconnected.'")
                        assert (await client.get(f'{app_url}/jellyfin/QuickConnect/Connect', params={'Secret': pending['Secret']})).status_code == 404
                        await browser.js("document.querySelector('#mc-jellyfin').open=false")
                        print(name, 'PASS: Jellyfin Quick Connect approval and disconnect UI', flush=True)

                        await browser.until("document.querySelector('.mc-tile img')?.complete")
                        # MEASURE THE CROWD, NOT A CARD. Every tile shares one `1fr` track, so the
                        # symptom is tiles of DIFFERENT widths and a grid wider than its own box —
                        # neither of which is visible from any single tile.
                        layout = json.loads(await browser.js("""JSON.stringify((()=>{
                          const tiles=[...document.querySelectorAll('.mc-tile')];
                          const grid=document.querySelector('.xdc-grid');
                          return {widths:tiles.map(t=>Math.round(t.getBoundingClientRect().width)),
                                  overflow: grid ? grid.scrollWidth-grid.clientWidth : 0,
                                  page: document.documentElement.scrollWidth-innerWidth};
                        })())"""))
                        assert len(set(layout['widths'])) <= 1, (
                            f"{name}: a long title made the tiles different widths {layout['widths']} — "
                            "the grid track grew to fit it instead of the title being clipped")
                        assert layout['overflow'] <= 1, (
                            f"{name}: the media grid scrolls sideways by {layout['overflow']}px")
                        assert layout['page'] <= 0, (
                            f"{name}: a long title pushed the whole page wider by {layout['page']}px")
                        # THE NOW-PLAYING TITLE IS ITS OWN LINE, AND IT IS READ IN FULL. It used to be
                        # an ellipsised <h3> inside the control toolbar ("The Northern …" on a phone),
                        # because letting it wrap there pushed Full screen / Close player / Quality
                        # onto another row. Reported: "the video title should be on its own line so
                        # it always fits". So: the title sits ABOVE the picture on a line of its own,
                        # wraps to show every character, and the CONTROLS still do not depend on it —
                        # render a short name and a 190-character unbroken one, require the same
                        # control-row shape and a title that is never clipped.
                        async def _toolbar(text):
                            await browser.js("document.querySelector('#mc-playback').hidden=false;"
                                             "document.querySelector('#mc-playing').textContent="
                                             + json.dumps(text))
                            return json.loads(await browser.js("""JSON.stringify((()=>{
                              const box=document.querySelector('#mc-playback');
                              const h=document.querySelector('#mc-playing');
                              const bar=box.querySelector('.mc-controls'), stage=box.querySelector('.mc-stage');
                              const r=h.getBoundingClientRect(), B=box.getBoundingClientRect();
                              const cs=getComputedStyle(h);
                              return {barH: bar ? Math.round(bar.getBoundingClientRect().height) : -1,
                                      ownLine: h.parentElement===box && !!stage &&
                                               r.bottom <= stage.getBoundingClientRect().top + 0.5,
                                      clipped: h.scrollWidth > h.clientWidth + 1 || h.scrollHeight > h.clientHeight + 1 ||
                                               cs.textOverflow==='ellipsis' || cs.whiteSpace==='nowrap',
                                      spill: Math.round(r.right - B.right)};
                            })())"""))
                        short = await _toolbar("Short name")
                        long_name = await browser.js("document.querySelector('.mc-tile b').textContent")
                        assert len(long_name) > 120, f"the long-title fixture is only {len(long_name)} chars"
                        wide = await _toolbar(long_name)
                        assert wide['ownLine'] and short['ownLine'], (
                            f"{name}: the now-playing title is not on a line of its own above the picture")
                        assert not wide['clipped'], (
                            f"{name}: a long now-playing title is clipped/ellipsised instead of wrapping")
                        assert wide['spill'] <= 1, (
                            f"{name}: the now-playing title escapes the player by {wide['spill']}px")
                        assert wide['barH'] == short['barH'] and short['barH'] > 0, (
                            f"{name}: a long title changed the player controls from {short['barH']}px to "
                            f"{wide['barH']}px, moving the controls")
                        await browser.js("document.querySelector('#mc-playback').hidden=true")
                        print(name, 'PASS: the now-playing title is its own line, wraps in full, and the controls stay put', flush=True)
                        if name=='phone':
                            assert (await client.get(f'{app_url}/api/media-center/test/scan')).json()['state']=='running'
                            await browser.js("window.scanFirstCard=document.querySelector('.mc-tile');window.scanFirstImage=scanFirstCard.querySelector('img');document.querySelector('.mc-tile button').click();setTimeout(()=>document.querySelector('.mc-resume-dialog[open] button[value=start]')?.click(),100)",gesture=True)
                            await browser.until("document.querySelector('video').currentTime>1")
                            scan_release.set()
                            await browser.until("document.querySelectorAll('.mc-tile').length===2")
                            assert await browser.js("window.scanFirstCard===document.querySelector('.mc-tile')&&window.scanFirstImage===document.querySelector('.mc-tile img')")
                            assert await browser.js("document.querySelector('video').currentTime>1&&!document.querySelector('video').paused")
                            await browser.js("document.querySelector('#mc-close-player').click()",gesture=True)
                            print('PASS: playable preview during scan; new titles append without replacing covers or stopping playback',flush=True)

                        assert not await browser.js("document.documentElement.scrollWidth>innerWidth")
                        if name == "phone":
                            assert await browser.js("getComputedStyle(document.querySelector('.xdc-grid')).gridTemplateColumns.split(' ').length") == 2
                        await browser.js("document.querySelector('#mc-search').value='Northern';document.querySelector('#mc-search').oninput()")
                        assert await browser.js("document.querySelectorAll('.mc-tile:not([hidden])').length") == 1
                        await browser.js("document.querySelector('#mc-search').value='';document.querySelector('#mc-search').oninput()")
                        await browser.screenshot(name + ".png")
                        await browser.js("document.querySelector('.mc-tile button').click();setTimeout(()=>document.querySelector('.mc-resume-dialog[open] button[value=start]')?.click(),100)", True)
                        await browser.until("document.querySelector('video')?.currentTime>1")
                        assert await browser.js("document.querySelector('video').videoWidth") == 320
                        await asyncio.sleep(.3)
                        layout = json.loads(await browser.js(PLAYER_LAYOUT))
                        await browser.screenshot(name + "-playing.png")
                        assert_player_uses_space(name, layout)
                        print(name, 'PASS: the player fits the screen and the picture uses the space', layout, flush=True)
                        await browser.js("document.querySelector('#mc-subtitles option:last-child').textContent='English (SDH) (Dub) · Full dialogue and translated signs · Subtitle track'")
                        assert await browser.js('document.documentElement.scrollWidth<=innerWidth')
                        assert await browser.js("document.querySelectorAll('#mc-audio option').length") == 3
                        await browser.js("document.querySelector('#mc-subtitles').value='3';document.querySelector('#mc-subtitles').onchange()", True)
                        await browser.until("document.querySelector('video').textTracks[0]?.activeCues?.length>0")
                        await browser.js("document.querySelector('#mc-subtitles').value='-1';document.querySelector('#mc-subtitles').onchange()", True)
                        assert await browser.js("document.querySelectorAll('video track').length") == 0
                        await browser.js("window.audioSwitchTime=document.querySelector('video').currentTime;document.querySelector('#mc-audio').value='2';document.querySelector('#mc-audio').onchange()", True)
                        await browser.until("document.querySelector('video').currentTime>window.audioSwitchTime&&!document.querySelector('video').paused")
                        print(name, 'PASS: subtitles on/off and audio language switching preserve playback', flush=True)
                        await browser.js("document.querySelector('#mc-fullscreen').onclick()", True)
                        assert await browser.js("document.fullscreenElement?.id") == "mc-playback"
                        await browser.screenshot(name + "-fullscreen.png")
                        await browser.js("document.querySelector('#mc-fullscreen').onclick()", True)
                        await browser.js("document.querySelector('video').currentTime=12")
                        await browser.until("document.querySelector('video').currentTime>12.3")
                        await browser.js("document.querySelector('#mc-close-player').onclick()", True)
                        await browser.until("document.querySelector('#mc-playback').hidden")
                        print(name, "PASS: artwork, grid, search, actual HLS playback, full screen, seek, close", flush=True)
                        if name == "tv":
                            # INSIDE A DESKTOP WINDOW. os.js windows ADOPT the #feed (no iframe), so
                            # the scroller is a 900x520 box on a 1920x1080 screen and `vh` means the
                            # screen. The player must fit the window, not the monitor.
                            await browser.js("""(()=>{const st=document.createElement('style');st.id='as-window';
                              st.textContent='#feed{position:fixed!important;left:160px;top:120px;width:900px!important;'+
                                'height:520px;overflow-y:auto;margin:0!important;max-width:none!important}';
                              document.head.append(st);})()""")
                            await browser.js("document.querySelector('.mc-tile button').click();setTimeout(()=>document.querySelector('.mc-resume-dialog[open] button[value=start]')?.click(),100)", True)
                            await browser.until("document.querySelector('video')?.currentTime>1")
                            await asyncio.sleep(.3)
                            layout = json.loads(await browser.js(PLAYER_LAYOUT))
                            await browser.screenshot("window-playing.png")
                            assert layout.get("scroller") == "feed", layout
                            assert_player_uses_space("window", layout)
                            await browser.js("document.querySelector('#mc-close-player').onclick()", True)
                            await browser.until("document.querySelector('#mc-playback').hidden")
                            await browser.js("document.getElementById('as-window').remove()")
                            print("window PASS: the player fits a desktop window, not the monitor", layout, flush=True)
                    # Start the recipient chain with an actual administrator share mutation.
                    documents['library:test']['shared_with'] = []
                    private = {**copy.deepcopy(documents['library:test']), 'id':'private',
                               'name':'Unrelated private movies', 'shared_with':[]}
                    documents['library:private'] = private
                    documents['index']['ids'].append('private')
                    before_share = await client.get(f'{app_url}/api/media-center', headers={'X-Test-Viewer': VIEWER})
                    assert before_share.json()['libraries'] == [], before_share.text
                    assert before_share.json()['unshared'] == 2, before_share.text

                    # THE SCREEN A RECIPIENT ACTUALLY GETS BEFORE ANYONE SHARES ANYTHING. Being given
                    # the Media Center permission and being given a library are two separate acts, and
                    # only the first has happened here — the state a real account was left in, and
                    # reported as "I cannot see the media library". This viewer can never create a
                    # library, so "My libraries" is not their tab and "No libraries of your own yet."
                    # is not an answer to anything they can do: it reads as "this server has no media"
                    # and names no next step, which is why the share that was never made looked
                    # identical to one that was. Driven in a browser at phone width because the fault
                    # is what is on screen; the API assertion above cannot see any of it.
                    await browser.call("Emulation.setDeviceMetricsOverride", {"width": 390, "height": 844,
                                                                              "deviceScaleFactor": 1, "mobile": False})
                    stranded = await browser.call("Target.createTarget", {"url": f"{app_url}/?viewer=" + VIEWER})
                    pages = (await client.get(f"http://127.0.0.1:{CDP_PORT}/json/list")).json()
                    stranded_page = next(p for p in pages if p["id"] == stranded["targetId"])
                    async with websockets.connect(stranded_page["webSocketDebuggerUrl"], max_size=32 * 1024 * 1024) as stranded_ws:
                        lonely = Browser(stranded_ws)
                        await lonely.call("Page.enable")
                        await lonely.call("Emulation.setDeviceMetricsOverride", {"width": 390, "height": 844,
                                                                                 "deviceScaleFactor": 1, "mobile": False})
                        await lonely.until("document.title==='READY'")
                        assert await lonely.js("document.querySelector('#mc-tab-shared').getAttribute('aria-selected')==='true'"), (
                            'a viewer who cannot create a library was landed on "My libraries"')
                        empty = await lonely.js("document.querySelector('#mc-libraries').textContent")
                        assert 'No libraries of your own' not in empty, empty
                        assert 'you cannot open' in empty, empty
                        assert VIEWER in await lonely.js("document.querySelector('#mc-libraries code')?.textContent||''"), empty
                        await lonely.js("document.querySelector('.mc-share-key button').click()", gesture=True)
                        await lonely.until("window.lastCopied===" + json.dumps(VIEWER))
                        assert await lonely.js('document.documentElement.scrollWidth<=innerWidth'), (
                            'the shared key widened the page')
                        await lonely.screenshot('stranded-viewer.png')
                    await browser.call("Target.closeTarget", {"targetId": stranded["targetId"]})
                    print('PASS: a viewer with nothing shared is told what is missing and given the key to hand over', flush=True)

                    shared = await client.put(f'{app_url}/api/media-center/test/sharing',
                        headers={'X-Test-Viewer': OWNER}, json={'shared_with':[VIEWER]})
                    assert shared.status_code == 200 and shared.json()['shared_with'] == [VIEWER], shared.text
                    # Two real browsers play the same cached media under separate Nostr identities.
                    target = await browser.call("Target.createTarget", {"url": f"{app_url}/?viewer=" + VIEWER})
                    pages = (await client.get(f"http://127.0.0.1:{CDP_PORT}/json/list")).json()
                    second_page = next(p for p in pages if p["id"] == target["targetId"])
                    async with websockets.connect(second_page["webSocketDebuggerUrl"], max_size=32 * 1024 * 1024) as second_ws:
                        second = Browser(second_ws)
                        await second.until("document.title==='READY'")
                        assert await second.js("document.querySelector('#mc-tab-shared').getAttribute('aria-selected')==='true'")
                        assert await second.js("!document.querySelector('#mc-add')&&!document.querySelector('#mc-limits')&&!document.querySelector('.mc-share')")
                        await second.js("document.querySelector('.mc-directory').click()", True)
                        await second.until("document.querySelectorAll('.mc-folder-trail button').length===2")
                        print('PASS: shared viewer opens Shared with me, browses folders, and has no admin controls', flush=True)
                        # Pair from the recipient's rendered controls, not the owner's session.
                        # This identity owns no library; an owner-only Quick Connect card can pass
                        # every owner UI test while stranding exactly this shared-library TV user.
                        recipient_libraries = (await client.get(f'{app_url}/api/media-center',
                            headers={'X-Test-Viewer': VIEWER})).json()['libraries']
                        assert recipient_libraries and all(item['shared_with_me'] and not item['can_manage']
                                                           for item in recipient_libraries), recipient_libraries
                        recipient_pending = (await client.post(f'{app_url}/jellyfin/QuickConnect/Initiate')).json()
                        await second.js("document.querySelector('#mc-jellyfin').open=true")
                        await second.js("document.querySelector('#mc-jellyfin-approve input').value=" + json.dumps(recipient_pending['Code']) +
                                        ";document.querySelector('#mc-jellyfin-approve').requestSubmit()", gesture=True)
                        await second.until("window.lastToast?.startsWith('Jellyfin app approved')")
                        recipient_polled = await client.get(f'{app_url}/jellyfin/QuickConnect/Connect',
                                                           params={'Secret': recipient_pending['Secret']})
                        assert recipient_polled.json()['Authenticated'] is True, recipient_polled.text
                        recipient_login_response = await client.post(f'{app_url}/jellyfin/Users/AuthenticateWithQuickConnect',
                            json={'Secret': recipient_pending['Secret']})
                        assert recipient_login_response.status_code == 200, recipient_login_response.text
                        recipient_login = recipient_login_response.json()
                        assert recipient_login['User']['Id'] == jellyfin.account_id(SimpleNamespace(nostr_npub=VIEWER))
                        recipient_tv_headers = {'X-Emby-Token': recipient_login['AccessToken']}
                        recipient_views = await client.get(f'{app_url}/jellyfin/UserViews', headers=recipient_tv_headers)
                        assert recipient_views.status_code == 200 and len(recipient_views.json()['Items']) == 1, recipient_views.text
                        await second.js("document.querySelector('#mc-jellyfin').open=false")
                        print('PASS: zero-owned-library recipient pairs TV from Quick Connect UI and sees the shared library', flush=True)
                        # Follow the TV API's browse/playback/HLS path all the way to decoded video.
                        tv_library = recipient_views.json()['Items'][0]
                        assert tv_library['Id'] == jellyfin.library_id(documents['library:test'])
                        listing = await client.get(f'{app_url}/jellyfin/Items', headers=recipient_tv_headers,
                                                   params={'ParentId':tv_library['Id']})
                        tv_item = listing.json()['Items'][0]
                        while tv_item['IsFolder']:
                            listing = await client.get(f'{app_url}/jellyfin/Items', headers=recipient_tv_headers,
                                                       params={'ParentId':tv_item['Id']})
                            assert listing.status_code == 200, listing.text
                            tv_item = listing.json()['Items'][0]
                        playback = await client.post(f"{app_url}/jellyfin/Items/{tv_item['Id']}/PlaybackInfo",
                            headers=recipient_tv_headers, json={'MaxStreamingBitrate':650000})
                        assert playback.status_code == 200, playback.text
                        playback_info = playback.json()
                        master_url = urljoin(app_url+'/jellyfin/', playback_info['MediaSources'][0]['TranscodingUrl'])
                        master = await client.get(master_url)
                        assert master.status_code == 200, master.text
                        variant_url = urljoin(master_url, next(line for line in master.text.splitlines() if line and not line.startswith('#')))
                        variant = await client.get(variant_url)
                        assert variant.status_code == 200, variant.text
                        segment_url = urljoin(variant_url, next(line for line in variant.text.splitlines() if line and not line.startswith('#')))
                        segment = await client.get(segment_url, timeout=30)
                        assert segment.status_code == 200, segment.text[:100] if segment.status_code != 200 else ''
                        decoded = subprocess.run(['ffmpeg','-v','error','-i','pipe:0','-map','0:v:0','-frames:v','1','-f','null','-'],
                                                 input=segment.content, capture_output=True, timeout=20)
                        assert decoded.returncode == 0, decoded.stderr.decode(errors='replace')
                        # Valid IDs from another library cannot bypass its private ACL, even if cached.
                        private_item = jellyfin.remember(private, scanned[0])
                        for forbidden_id in [jellyfin.library_id(private), private_item]:
                            denied = await client.get(f'{app_url}/jellyfin/Items/{forbidden_id}', headers=recipient_tv_headers)
                            assert denied.status_code == 404, denied.text
                            denied = await client.post(f'{app_url}/jellyfin/Items/{forbidden_id}/PlaybackInfo',
                                                       headers=recipient_tv_headers, json={})
                            assert denied.status_code == 404, denied.text
                        stopped = await client.post(f'{app_url}/jellyfin/Sessions/Playing/Stopped',
                            headers=recipient_tv_headers, json={'PlaySessionId':playback_info['PlaySessionId']})
                        assert stopped.status_code == 204, stopped.text
                        print('PASS: admin share -> recipient browser pairing -> TV browse and decoded HLS; unrelated private library denied', flush=True)


                        await asyncio.gather(browser.js("document.querySelector('.mc-tile button').click();setTimeout(()=>document.querySelector('.mc-resume-dialog[open] button[value=start]')?.click(),100)", True),
                                             second.js("document.querySelector('.mc-tile button').click();setTimeout(()=>document.querySelector('.mc-resume-dialog[open] button[value=start]')?.click(),100)", True))
                        await asyncio.gather(browser.until("document.querySelector('video').currentTime>2"),
                                             second.until("document.querySelector('video').currentTime>2"))
                        assert len(media._sessions) == 2, media._sessions
                        url = await second.js("_mediaCenterSession")
                        documents["library:test"]["shared_with"] = []
                        assert (await client.get(f"{app_url}" + url)).status_code == 404
                        revoked_views = await client.get(f'{app_url}/jellyfin/UserViews', headers=recipient_tv_headers)
                        assert revoked_views.status_code == 200 and revoked_views.json()['Items'] == [], revoked_views.text

                        print("two viewers PASS: concurrent HLS playback and existing-ticket revocation", flush=True)
                        await asyncio.gather(browser.js("document.querySelector('#mc-close-player').onclick()", True),
                                             second.js("document.querySelector('#mc-close-player').onclick()", True))
                    await asyncio.sleep(.2)
                    assert not media._sessions, media._sessions
                    await browser.call('Page.bringToFront')
                    media.scan = real_scan
                    source.parent.rename(temp / 'Renamed Adventure')
                    await browser.js("[...document.querySelectorAll('#mc-libraries button')].find(b=>b.textContent==='Rescan').click()", True)
                    await browser.until("document.querySelector('.mc-directory[data-path=\"Renamed Adventure\"]')&&!document.querySelector('.mc-directory[data-path=\"Adventure\"]')")
                    await browser.until("[...document.querySelectorAll('.mc-folder')].every(s=>s.dataset.folder==='Renamed Adventure')")
                    assert (await client.get(f'{app_url}/api/media-center/test/scan')).json()['state'] == 'complete'
                    print('PASS: a completed rescan removes the old folder and moved titles without reloading the page', flush=True)
                    print("stream slots released PASS; screenshots:", ARTIFACTS, flush=True)
        finally:
            process.terminate()
            process.wait(timeout=10)
            scan_release.set()
            server.should_exit = True
            nas_server.should_exit = True
            await server_task
            await nas_task
            await routes.close_proxy()
            engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
