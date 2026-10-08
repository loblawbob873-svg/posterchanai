"""Tablet desktop mode keeps only the most recent windows' content resident ("you need to memory-optimize
tablet because the OOM is crazy", 2026-10-08).

Every unfocused window PARKED its whole DOM -- timeline cards, images, video, chat -- and nothing capped how
many did. In the Android app the desk is one WebView, so six parked windows were six full views resident at
once and Android killed the renderer (the "reload"). A constrained device now keeps the content of its 2 most
recently used windows; older ones hibernate (nodes dropped, media unloaded) and repaint when focused, and
Android's onTrimMemory (`pc:trim-memory`) hibernates them all. The real client in desktop mode on an emulated
2 GB touch tablet; parked content is counted straight from the `.osw-slot` elements, not from the code under test.
"""
import asyncio
import subprocess
import tempfile
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest
import websockets

from tests.client import test_effects_full_app as full
from tests.client import test_social_offline_refresh_full_app as soc

VIEWS = ["global", "notifications", "messages", "bookmarks", "articles"]
SLOTS = "[...document.querySelectorAll('.osw-slot')].map(s=>s.getElementsByTagName('*').length)"


async def run(device_memory):
    full.ROOT = Path(__file__).resolve().parents[2]
    server = ThreadingHTTPServer(("127.0.0.1", 0), full.Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    got = {}
    with tempfile.TemporaryDirectory(prefix="pc-deskmem-", ignore_cleanup_errors=True) as prof:
        p = subprocess.Popen(["/opt/google/chrome/chrome", "--headless=new", "--no-sandbox", "--disable-gpu",
                              "--remote-debugging-port=0", "--user-data-dir=" + prof, "about:blank"],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            for _ in range(100):
                if Path(prof, "DevToolsActivePort").exists():
                    break
                await asyncio.sleep(.1)
            port = Path(prof, "DevToolsActivePort").read_text().splitlines()[0]
            async with httpx.AsyncClient() as h:
                pages = (await h.get(f"http://127.0.0.1:{port}/json")).json()
            async with websockets.connect(next(x for x in pages if x["type"] == "page")["webSocketDebuggerUrl"],
                                          max_size=20_000_000) as ws:
                b = full.Browser(ws)
                await b.call("Page.enable")
                await b.call("Network.setBlockedURLs", {"urls": ["https://*", "wss://*"]})
                await b.call("Emulation.setDeviceMetricsOverride", dict(width=1340, height=800, deviceScaleFactor=2,
                                                                        mobile=True, screenWidth=1340, screenHeight=800))
                await b.call("Emulation.setTouchEmulationEnabled", dict(enabled=True, maxTouchPoints=5))
                mem = f"Object.defineProperty(Navigator.prototype,'deviceMemory',{{get:()=>{device_memory},configurable:true}});"
                await b.call("Page.addScriptToEvaluateOnNewDocument", {"source": mem + soc.INIT})
                await b.call("Page.navigate", {"url": f"http://127.0.0.1:{server.server_port}/client"})
                await b.until('!!window.__PC && !!window.NostrTools && document.readyState==="complete"')
                await b.until("document.body.classList.contains('guest')")
                await b.js("(()=>{const key=new Uint8Array(32).fill(1);document.querySelector('#nsec-input').value="
                           "NostrTools.nip19.nsecEncode(key);document.querySelector('#btn-nsec-login').click()})()")
                await b.until('!!__PC.me() && Relay.status==="ok"')
                await b.js("try{ if(window.PCOS && !PCOS.isOn()) PCOS.enter(); }catch(e){}")
                await asyncio.sleep(1)
                got["on"] = await b.js("!!(window.PCOS && PCOS.isOn())")
                for v in VIEWS:
                    await b.js(f"__PC.switchView('{v}'); true")
                    await asyncio.sleep(1.6)
                got["slots"] = await b.js(SLOTS)
                got["windows"] = await b.js("document.querySelectorAll('.osw').length")
                # Back to the first window, which a 2 GB tablet has hibernated: it must repaint real content.
                await b.js(f"__PC.switchView('{VIEWS[0]}'); true")
                await b.until("window.__feedCards && window.__feedCards() >= 3")
                got["back_cards"] = await b.js("window.__feedCards()")
                window_event = "window.dispatchEvent(new Event('pc:trim-memory')); true"
                await b.js(window_event)
                await asyncio.sleep(.3)
                got["after_trim"] = await b.js(SLOTS)
        finally:
            p.terminate()
            server.shutdown()
    return got


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
def test_a_two_gigabyte_tablet_keeps_two_windows_resident_and_trims_on_pressure():
    got = asyncio.run(run(2))
    resident = [n for n in got["slots"] if n > 0]
    assert got["windows"] >= len(VIEWS), got
    assert len(resident) <= 2, f"{len(resident)} parked windows still hold their whole DOM: {got['slots']}"
    assert got["back_cards"] >= 3, "a hibernated window came back empty"
    assert sum(got["after_trim"]) == 0, f"onTrimMemory left parked content resident: {got['after_trim']}"


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
def test_a_desktop_with_memory_keeps_every_window_as_it_was():
    got = asyncio.run(run(8))
    assert len([n for n in got["slots"] if n > 0]) >= len(VIEWS) - 1, got["slots"]
