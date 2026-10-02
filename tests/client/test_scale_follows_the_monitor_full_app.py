"""The desktop's text size follows the MONITOR, not the window.

"poster.place changes font size based on window width ... I run this site on the left 50% of my
desktop screen and the font becomes very small so I CTRL+ to 125%. But if I maximize the window, the
font and all elements are too big again." The tiers were keyed on the viewport: on one 1920px monitor a
half-width window drew at .67 and a maximized one at .77. The real shell (templates/client.html) in
headless Chrome with an emulated SCREEN: one monitor, two window widths, one size -- and the layouts
that must not change (a touch tablet, a phone) keep theirs. The page always fills its window exactly.
"""
import asyncio
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

# name: (window w, h, screen w, h, dpr, touch)
CASES = {
    "1080p half": (960, 1000, 1920, 1080, 1, False),
    "1080p max": (1920, 1000, 1920, 1080, 1, False),
    "4K half": (1920, 2400, 3840, 2560, 1, False),
    "4K max": (3840, 2400, 3840, 2560, 1, False),
    "1366 laptop half": (900, 700, 1366, 768, 1, False),
    "1366 laptop max": (1366, 700, 1366, 768, 1, False),
    "Galaxy Tab": (1340, 800, 1340, 800, 2, True),
    "phone": (390, 800, 390, 800, 3, True),
}


async def measure():
    full.ROOT = Path(__file__).resolve().parents[2]
    server = ThreadingHTTPServer(("127.0.0.1", 0), full.Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    out = {}
    with tempfile.TemporaryDirectory(prefix="pc-scale-", ignore_cleanup_errors=True) as prof:
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
                for name, (w, hh, sw, sh, dpr, touch) in CASES.items():
                    await b.call("Emulation.setDeviceMetricsOverride", dict(width=w, height=hh, deviceScaleFactor=dpr,
                                                                            mobile=touch, screenWidth=sw, screenHeight=sh))
                    await b.call("Emulation.setTouchEmulationEnabled", dict(enabled=touch, maxTouchPoints=5 if touch else 1))
                    await b.call("Page.navigate", {"url": f"http://127.0.0.1:{server.server_port}/client"})
                    await b.until("document.readyState==='complete' && !!document.querySelector('.app')")
                    out[name] = await b.js("({zoom:+getComputedStyle(document.body).zoom,"
                                           "app:Math.round(document.querySelector('.app').getBoundingClientRect().height),vh:innerHeight})")
        finally:
            p.terminate()
            p.wait(timeout=10)
            server.shutdown()
    return out


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
def test_one_monitor_one_size_whatever_the_window():
    m = asyncio.run(measure())
    for mon in ("1080p", "4K", "1366 laptop"):
        assert m[f"{mon} half"]["zoom"] == m[f"{mon} max"]["zoom"], (
            f"{mon}: a half-width window is drawn at {m[mon + ' half']['zoom']} and a maximized one at "
            f"{m[mon + ' max']['zoom']}", m)
    assert m["1080p max"]["zoom"] == 0.77 and m["4K max"]["zoom"] == 1.25 and m["1366 laptop max"]["zoom"] == 0.67, m
    assert m["Galaxy Tab"]["zoom"] == 0.67, ("the tablet's size changed", m)
    assert m["phone"]["zoom"] == 1, ("the phone layout changed", m)
    for name, r in m.items():
        assert abs(r["app"] - r["vh"]) <= 1, (f"{name}: the page is {r['app']}px in a {r['vh']}px window", m)
