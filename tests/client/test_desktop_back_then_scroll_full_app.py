"""Desktop mode: after a post window's Back, the Social timeline scrolls -- it is not dragged back.

"tablet desktop mode: social scrolling issue: I am trying to scroll down timeline and it keeps
fighting and moving up" / "open a post, click back, try to scroll. it keeps bringing you back to the
same timeline position". Every touch on a window runs focusWin (capture-phase pointerdown), and
focusWin ended by replaying the scroll offset the window saved when it was last PARKED -- even when
it already held the live feed, so each new touch wrote the old position back. The real client, in
desktop mode on a touch tablet, with the fixture relay; the toast layer is hidden because the
fixture answers every subscription with posts and its toasts would cover the touch point.
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


async def run():
    full.ROOT = Path(__file__).resolve().parents[2]
    server = ThreadingHTTPServer(("127.0.0.1", 0), full.Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    with tempfile.TemporaryDirectory(prefix="pc-deskback-", ignore_cleanup_errors=True) as prof:
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
                await b.call("Page.addScriptToEvaluateOnNewDocument", {"source": soc.INIT})
                await b.call("Page.navigate", {"url": f"http://127.0.0.1:{server.server_port}/client"})
                await b.until('!!window.__PC && !!window.NostrTools && document.readyState==="complete"')
                await b.until("document.body.classList.contains('guest')")
                await b.js("(()=>{const key=new Uint8Array(32).fill(1);document.querySelector('#nsec-input').value="
                           "NostrTools.nip19.nsecEncode(key);document.querySelector('#btn-nsec-login').click()})()")
                await b.until('!!__PC.me() && Relay.status==="ok"')
                await b.js("(()=>{const st=document.createElement('style');st.textContent='#toast-root{display:none!important}';document.head.appendChild(st);})()")
                await b.js("try{ if(window.PCOS && !PCOS.isOn()) PCOS.enter(); }catch(e){}")
                await asyncio.sleep(1)
                await b.js("__PC.switchView('global')")
                await b.until("window.__feedCards() >= 8")
                await asyncio.sleep(2)                       # let the Social window finish drawing
                feed = "document.getElementById('feed')"
                await b.js(f"{feed}.scrollTop=1200")
                await asyncio.sleep(.5)
                assert await b.js(f"Math.round({feed}.scrollTop)") == 1200, "could not scroll the timeline at all"
                # Open a post: on the desktop it opens in its OWN window, which borrows the shared feed.
                await b.js(f"""(()=>{{const top={feed}.getBoundingClientRect().top;
                    const c=[...document.querySelectorAll('#tl-notes>.note')].find(n=>n.getBoundingClientRect().top>top+20);
                    (c.querySelector('.txt')||c).click();}})()""")
                await b.until("[...document.querySelectorAll('.osw')].length >= 2")
                await asyncio.sleep(1)
                await b.js("(()=>{const bk=[...document.querySelectorAll('.pc-nav-back')].find(x=>x.getClientRects().length);bk.click();})()")
                await asyncio.sleep(1.2)
                back_at = await b.js(f"Math.round({feed}.scrollTop)")
                # The reader scrolls on, then touches the Social window again (every touch focuses it).
                # A real swipe on the Social window (the touch is what focuses the window).
                r = await b.js(f"(()=>{{const r={feed}.getBoundingClientRect();return [r.left+r.width/2, r.top+r.height*0.45];}})()")
                x, y0 = int(r[0]), int(r[1])
                await b.call("Input.dispatchTouchEvent", dict(type="touchStart", touchPoints=[dict(x=x, y=y0)]))
                for step in range(1, 16):
                    await b.call("Input.dispatchTouchEvent", dict(type="touchMove", touchPoints=[dict(x=x, y=y0 - step * 14)]))
                    await asyncio.sleep(.016)
                await b.call("Input.dispatchTouchEvent", dict(type="touchEnd", touchPoints=[]))
                await asyncio.sleep(.6)
                scrolled = await b.js(f"Math.round({feed}.scrollTop)")
                # ...and a second swipe: each new touch runs focusWin again.
                await b.call("Input.dispatchTouchEvent", dict(type="touchStart", touchPoints=[dict(x=x, y=y0)]))
                for step in range(1, 16):
                    await b.call("Input.dispatchTouchEvent", dict(type="touchMove", touchPoints=[dict(x=x, y=y0 - step * 14)]))
                    await asyncio.sleep(.016)
                await b.call("Input.dispatchTouchEvent", dict(type="touchEnd", touchPoints=[]))
                await asyncio.sleep(.8)
                after = await b.js(f"Math.round({feed}.scrollTop)")
                return back_at, scrolled, after
        finally:
            p.terminate()
            p.wait(timeout=10)
            server.shutdown()


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
def test_back_returns_to_the_place_and_then_the_timeline_scrolls_freely():
    back_at, scrolled, after = asyncio.run(run())
    assert abs(back_at - 1200) <= 2, f"Back did not return to the reading place: {back_at}"
    assert scrolled > back_at + 100, ("the first swipe did not scroll", back_at, scrolled)
    assert after > scrolled + 100, (
        f"the second swipe was dragged back: {back_at} -> {scrolled} -> {after}")
