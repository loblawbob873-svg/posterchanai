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
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import httpx
import uvicorn
import websockets
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from PIL import Image

from app.auth import get_admin_user, get_current_user
from app.routers import media_center as routes
from app.services import media_center as media

OWNER, VIEWER = "11" * 32, "22" * 32
ARTIFACTS = Path("/tmp/pc-media-check")


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
            "({status:document.querySelector('#mc-status')?.textContent,time:document.querySelector('video')?.currentTime,error:document.querySelector('video')?.error?.message})")})

    async def screenshot(self, name):
        result = await self.call("Page.captureScreenshot", {"format": "png"})
        (ARTIFACTS / name).write_bytes(base64.b64decode(result["data"]))


async def main():
    chrome = shutil.which("google-chrome") or "/opt/google/chrome/chrome"
    assert Path(chrome).exists(), "Chrome is required for this check"
    ARTIFACTS.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="pc-media-check-") as directory:
        temp = Path(directory)
        source = temp / "Adventure" / "Northern Lights.mp4"
        source.parent.mkdir()
        os.environ["POSTERCHANAI_MEDIA_ROOTS"] = str(temp)
        os.environ["POSTERCHANAI_MEDIA_CACHE"] = str(temp / "cache")
        subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=320x240:rate=24",
                        "-f", "lavfi", "-i", "sine=frequency=440", "-t", "18", "-c:v", "libx264",
                        "-threads", "1", "-c:a", "aac", str(source)], check=True, timeout=30)
        Image.new("RGB", (600, 400), "#195d78").save(source.parent / "poster.jpg")
        other = source.parent / "The Long Way Home.mp4"
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
        app = FastAPI()
        app.include_router(routes.router)
        app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")
        def user(request: Request):
            key = request.headers.get("X-Test-Viewer", OWNER)
            return SimpleNamespace(nostr_npub=key, is_admin=key == OWNER)
        app.dependency_overrides[get_current_user] = user
        app.dependency_overrides[get_admin_user] = user
        javascript = (ROOT / "static/js/client/app.js").read_text()
        functions = javascript[javascript.index("  let _mediaCenterSession="):javascript.index("  // ---------- torrents (NIP-35")]
        bootstrap = """
          const $=s=>document.querySelector(s);let VIEW='media-center';const _instanceBase=()=>location.origin;
          const enc=s=>String(s).replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('"','&quot;');
          const _streamFetch=(url,opts={})=>fetch(url,{...opts,headers:{...opts.headers,
            'X-Test-Viewer':new URLSearchParams(location.search).get('viewer')||'OWNER'}});
          const loadHls=async()=>{};
        """.replace("'OWNER'", json.dumps(OWNER))
        @app.get("/", response_class=HTMLResponse)
        async def page():
            return ("<!doctype html><meta name='viewport' content='width=device-width,initial-scale=1'>"
                    "<link rel='stylesheet' href='/static/css/client.css'>"
                    "<style>body{display:block!important;margin:0!important;padding:12px}#feed{width:100%;max-width:1500px;margin:auto}</style>"
                    "<main id='feed'></main><script src='/static/vendor/hls/hls.min.js'></script><script>" + bootstrap + functions +
                    "renderMediaCenter().then(async()=>{await document.querySelector('.mc-library-open').onclick();document.title='READY';});</script>")
        server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=19438, log_level="error"))
        server_task = asyncio.create_task(server.serve())
        process = subprocess.Popen([chrome, "--headless=new", "--no-sandbox", "--disable-gpu",
                                    "--disable-dev-shm-usage", "--autoplay-policy=no-user-gesture-required",
                                    "--remote-debugging-port=19439", "--remote-debugging-address=127.0.0.1",
                                    "--user-data-dir=" + str(temp / "chrome"), "about:blank"],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            async with httpx.AsyncClient() as client:
                for _ in range(100):
                    try:
                        pages = (await client.get("http://127.0.0.1:19439/json/list")).json()
                        browser_page = next(p for p in pages if p["type"] == "page")
                        if server.started:
                            break
                    except Exception:
                        pass
                    await asyncio.sleep(.1)
                async with websockets.connect(browser_page["webSocketDebuggerUrl"], max_size=32 * 1024 * 1024) as ws:
                    browser = Browser(ws)
                    await browser.call("Page.enable")
                    for name, width, height in (("phone", 390, 844), ("tv", 1920, 1080)):
                        await browser.call("Emulation.setDeviceMetricsOverride", {"width": width, "height": height,
                                                                                  "deviceScaleFactor": 1, "mobile": False})
                        await browser.call("Page.navigate", {"url": "http://127.0.0.1:19438/"})
                        await browser.until("document.title==='READY'")
                        await browser.until("document.querySelector('.mc-tile img')?.complete")
                        assert not await browser.js("document.documentElement.scrollWidth>innerWidth")
                        if name == "phone":
                            assert await browser.js("getComputedStyle(document.querySelector('.xdc-grid')).gridTemplateColumns.split(' ').length") == 2
                        await browser.js("document.querySelector('#mc-search').value='Northern';document.querySelector('#mc-search').oninput()")
                        assert await browser.js("document.querySelectorAll('.mc-tile:not([hidden])').length") == 1
                        await browser.js("document.querySelector('#mc-search').value='';document.querySelector('#mc-search').oninput()")
                        await browser.screenshot(name + ".png")
                        await browser.js("document.querySelector('.mc-tile button').onclick()", True)
                        await browser.until("document.querySelector('video')?.currentTime>1")
                        assert await browser.js("document.querySelector('video').videoWidth") == 320
                        await browser.js("document.querySelector('#mc-fullscreen').onclick()", True)
                        assert await browser.js("document.fullscreenElement?.id") == "mc-playback"
                        await browser.screenshot(name + "-fullscreen.png")
                        await browser.js("document.querySelector('#mc-fullscreen').onclick()", True)
                        await browser.js("document.querySelector('video').currentTime=12")
                        await browser.until("document.querySelector('video').currentTime>12.3")
                        await browser.js("document.querySelector('#mc-close-player').onclick()", True)
                        await browser.until("document.querySelector('#mc-playback').hidden")
                        print(name, "PASS: artwork, grid, search, actual HLS playback, full screen, seek, close", flush=True)
                    # Two real browsers play the same cached media under separate Nostr identities.
                    target = await browser.call("Target.createTarget", {"url": "http://127.0.0.1:19438/?viewer=" + VIEWER})
                    pages = (await client.get("http://127.0.0.1:19439/json/list")).json()
                    second_page = next(p for p in pages if p["id"] == target["targetId"])
                    async with websockets.connect(second_page["webSocketDebuggerUrl"], max_size=32 * 1024 * 1024) as second_ws:
                        second = Browser(second_ws)
                        await second.until("document.title==='READY'")
                        await asyncio.gather(browser.js("document.querySelector('.mc-tile button').onclick()", True),
                                             second.js("document.querySelector('.mc-tile button').onclick()", True))
                        await asyncio.gather(browser.until("document.querySelector('video').currentTime>2"),
                                             second.until("document.querySelector('video').currentTime>2"))
                        assert len(media._sessions) == 2, media._sessions
                        url = await second.js("_mediaCenterSession")
                        documents["library:test"]["shared_with"] = []
                        assert (await client.get("http://127.0.0.1:19438" + url)).status_code == 404
                        print("two viewers PASS: concurrent HLS playback and existing-ticket revocation", flush=True)
                        await asyncio.gather(browser.js("document.querySelector('#mc-close-player').onclick()", True),
                                             second.js("document.querySelector('#mc-close-player').onclick()", True))
                    await asyncio.sleep(.2)
                    assert not media._sessions, media._sessions
                    print("stream slots released PASS; screenshots:", ARTIFACTS, flush=True)
        finally:
            process.terminate()
            process.wait(timeout=10)
            server.should_exit = True
            await server_task


if __name__ == "__main__":
    asyncio.run(main())
