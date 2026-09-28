"""On the windowed desktop, My Profile opens IN FRONT -- as its own window, like a post does.

Reported: "My profile is still appearing behind other windows like Social when opening." On
PosterChanOS every app is a real compositor toplevel; anything drawn INSIDE the desktop surface sits
below all of them by construction. os.js `popOutView` let exactly one document family out as its own
window -- `doc:post:<id>` -- and refused `doc:prof:<pubkey>`, so a profile became an in-page frame on
the desktop surface, behind the Social window it was opened from. (Taskbar Search was the same bug,
fixed the same way.) A profile is a pubkey and a fresh window rebuilds it exactly as an npub link.

  pops-out   with Social open, My Profile asks the shell for a `doc:prof:<me>` WINDOW and draws no
             in-page frame (the frame is what lands behind);
  lands      a window opened as `?pcwin=doc:prof:<pubkey>` renders that profile -- not "Nothing here
             can show doc:prof:…", which is what a name nothing routes prints.

The shell is stubbed the way test_a_reply_opens_on_the_first_click.py stubs it: PCOSWin.enabled()
is true and open() records what it was asked for -- enough to drive the branch a headless browser
otherwise cannot reach.
"""
import asyncio
import json
import subprocess
import tempfile
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest
import websockets

from tests.client.test_effects_full_app import Browser, Handler, INIT
from tests.client.test_a_reply_opens_on_the_first_click import POPOUT_STUB

LOGIN = """(()=>{const key=new Uint8Array(32).fill(3);
  window.__events=Array.from({length:12},(_,i)=>NostrTools.finalizeEvent(
    {kind:1,created_at:Math.floor(Date.now()/1000)-i,content:'profile test '+i,tags:[]},key));
  document.querySelector('#nsec-input').value=NostrTools.nip19.nsecEncode(key);
  document.querySelector('#btn-nsec-login').click()})()"""


async def _browser(fn):
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    with tempfile.TemporaryDirectory(prefix="pc-prof-front-") as profile:
        proc = subprocess.Popen(["/opt/google/chrome/chrome", "--headless=new", "--no-sandbox", "--disable-gpu",
                                 "--window-size=1440,1000", "--remote-debugging-port=0", "--user-data-dir=" + profile,
                                 "about:blank"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            for _ in range(100):
                if Path(profile, "DevToolsActivePort").exists():
                    break
                await asyncio.sleep(.1)
            port = Path(profile, "DevToolsActivePort").read_text().splitlines()[0]
            async with httpx.AsyncClient() as h:
                pages = (await h.get("http://127.0.0.1:" + port + "/json")).json()
            page = next(p for p in pages if p.get("type") == "page")
            async with websockets.connect(page["webSocketDebuggerUrl"], max_size=20_000_000) as ws:
                b = Browser(ws)
                await b.call("Page.enable")
                await b.call("Network.setBlockedURLs", {"urls": ["https://*", "wss://*"]})
                await b.call("Emulation.setDeviceMetricsOverride",
                             {"width": 1440, "height": 1000, "deviceScaleFactor": 1, "mobile": False})
                await b.call("Page.addScriptToEvaluateOnNewDocument", {"source": "window.__hasChats=false;" + INIT})
                base = f"http://127.0.0.1:{server.server_port}/client"
                await b.call("Page.navigate", {"url": base})
                await b.until('!!window.__PC && !!window.NostrTools && document.readyState==="complete"')
                await b.until("document.body.classList.contains('guest')")
                await b.js(LOGIN)
                await b.until("!!__PC.me() && document.querySelectorAll('.note').length>=12")
                return await fn(b, base)
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
            server.shutdown()


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
def test_my_profile_opens_as_its_own_window_over_social():
    async def fn(b, base):
        await b.js(POPOUT_STUB)
        await b.js("PCOS.enter()")
        await asyncio.sleep(0.8)
        await b.js("(()=>{const i=document.querySelector('.os-icon[data-view=global]'); if(i) i.click();})()")
        await asyncio.sleep(1.2)
        before = await b.js("window.__popped.slice()")
        await b.js("__PC.openProfile()")
        await asyncio.sleep(1.2)
        return {
            "me": await b.js("__PC.me().pubkey"),
            "asked": [v for v in await b.js("window.__popped") if v not in before],
            "frames": await b.js("[...document.querySelectorAll('.osw .osw-title')].map(t=>t.textContent.trim())"),
        }
    out = asyncio.run(_browser(fn))
    assert out["asked"] == ["doc:prof:" + out["me"]], \
        f"My Profile did not open as its own window -- it can only land behind Social: {out}"
    assert not any("profile" in t.lower() for t in out["frames"]), \
        f"an in-page profile frame was drawn on the desktop surface, behind every real window: {out['frames']}"


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
def test_a_profile_window_renders_the_profile():
    async def fn(b, base):
        me = await b.js("__PC.me().pubkey")
        await b.call("Page.navigate", {"url": base + "?pcwin=doc:prof:" + me})
        await b.until('!!window.__PC && document.readyState==="complete"')
        await b.until("!!(window.__PC.me && __PC.me())")
        await b.until("document.querySelectorAll('.prof-tab').length>0 || /Nothing here can show/.test(document.body.innerText)")
        return await b.js("""({tabs:document.querySelectorAll('.prof-tab').length,
            nothing:/Nothing here can show/.test(document.body.innerText),
            title:(document.getElementById('view-title')||{}).textContent||''})""")
    out = asyncio.run(_browser(fn))
    assert not out["nothing"] and out["tabs"] > 0, f"the profile window did not render the profile: {out}"
