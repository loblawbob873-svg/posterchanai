"""The scroll report records what moves the timeline under a finger, with its cause.

"scrolling down the timeline it keeps fighting and moving up" (Android phone and tablet). No desktop
reproduction showed it, so the device reports it: Settings → Phone → "Copy scroll report". This drives
the shipped client on a touch phone and makes each cause happen once, while a finger is down:
  - code writing #feed.scrollTop (the caller's name must be in the report),
  - a card ABOVE the reading position growing,
  - the page jumping back up while the finger scrolls down.
It also checks the report carries no post text.
"""
import asyncio
import json
import os
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


async def run():
    full.ROOT = Path(__file__).resolve().parents[2]
    server = ThreadingHTTPServer(("127.0.0.1", 0), full.Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    with tempfile.TemporaryDirectory(prefix="pc-scrollrep-", ignore_cleanup_errors=True) as profile:
        proc = subprocess.Popen(["/opt/google/chrome/chrome", "--headless=new", "--no-sandbox", "--disable-gpu",
                                 "--window-size=390,800", "--remote-debugging-port=0", "--user-data-dir=" + profile,
                                 "about:blank"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            for _ in range(100):
                if Path(profile, "DevToolsActivePort").exists():
                    break
                await asyncio.sleep(.1)
            port = Path(profile, "DevToolsActivePort").read_text().splitlines()[0]
            async with httpx.AsyncClient() as h:
                pages = (await h.get(f"http://127.0.0.1:{port}/json")).json()
            wsurl = next(p for p in pages if p.get("type") == "page")["webSocketDebuggerUrl"]
            async with websockets.connect(wsurl, max_size=20_000_000) as ws:
                b = full.Browser(ws)
                await b.call("Page.enable")
                await b.call("Network.setBlockedURLs", {"urls": ["https://*", "wss://*"]})
                await b.call("Emulation.setDeviceMetricsOverride", {"width": 390, "height": 800, "deviceScaleFactor": 2, "mobile": True})
                await b.call("Emulation.setTouchEmulationEnabled", {"enabled": True, "maxTouchPoints": 5})
                await b.call("Page.addScriptToEvaluateOnNewDocument", {"source": soc.INIT})
                await b.call("Page.navigate", {"url": f"http://127.0.0.1:{server.server_port}/client"})
                await b.until('!!window.__PC && !!window.NostrTools && document.readyState==="complete"')
                await b.until("document.body.classList.contains('guest')")
                await b.js("(()=>{const key=new Uint8Array(32).fill(1);document.querySelector('#nsec-input').value="
                           "NostrTools.nip19.nsecEncode(key);document.querySelector('#btn-nsec-login').click()})()")
                await b.until('!!__PC.me() && Relay.status==="ok"')
                await b.js("__PC.switchView('global')")
                await b.until("window.__feedCards() >= 8")
                assert await b.js("typeof window.PCScrollReport") == "function", "the recorder is not loaded"
                f = "document.getElementById('feed')"
                await b.js(f"{f}.scrollTop=900")
                await asyncio.sleep(.3)
                # A finger goes down and starts scrolling DOWN (moving up the screen)...
                await b.call("Input.dispatchTouchEvent", dict(type="touchStart", touchPoints=[dict(x=200, y=600)]))
                for step in range(1, 6):
                    await b.call("Input.dispatchTouchEvent", dict(type="touchMove", touchPoints=[dict(x=200, y=600 - step * 10)]))
                    await asyncio.sleep(.02)
                # ...while (1) code drags the page back up, and (2) a card above the reading position grows.
                await b.js(f"(function pullsThePageBackUp(){{ {f}.scrollTop = {f}.scrollTop - 300; }})()")
                await b.js(f"""(()=>{{const top={f}.getBoundingClientRect().top;
                    const c=[...document.querySelectorAll('#tl-notes>.note,#tl-notes>.reply-pair')].find(n=>n.getBoundingClientRect().bottom<top);
                    c.style.paddingBottom='250px';}})()""")
                await asyncio.sleep(.4)
                await b.call("Input.dispatchTouchEvent", dict(type="touchEnd", touchPoints=[]))
                rep = await b.js("PCScrollReport()")
                text = await b.js("JSON.stringify(PCScrollReport())")
                return rep, text
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=10)          # Chrome must stop writing its profile before the folder goes
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
            server.shutdown()


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
def test_the_scroll_report_names_what_moved_the_timeline():
    rep, text = asyncio.run(run())
    assert any("pullsThePageBackUp" in w.get("by", "") for w in rep["writes"]), rep["writes"]
    assert any(r["delta"] >= 200 for r in rep["resizes"]), ("a card above the reader grew and was not recorded", rep["resizes"])
    assert any(j["delta"] <= -200 for j in rep["jumps"]), ("the jump back up was not recorded", rep["jumps"])
    assert "fixture timeline post" not in text, "the report carries post text"


def test_settings_offers_the_copy_button():
    src = (Path(__file__).resolve().parents[2] / "static/js/client/settings.js").read_text()
    assert 'id="us-scroll-copy"' in src and "copyValue(JSON.stringify(window.PCScrollReport())" in src
