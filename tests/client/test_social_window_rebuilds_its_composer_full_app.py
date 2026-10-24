"""Desktop mode: a Social window that lost its new-post box and ＋ gets them back on the next redraw.

Reported 2026-10-24 (Windows app, windowed desktop): "on Social, the new post at the top and the + button disappeared";
closing and reopening the window brought them back. Both are built once, with the timeline header, only when
#tl-notes is absent -- so a window whose notes survived without their header stayed without a composer for as long as
it was open. The trigger was not reproduced (80 random desktop rounds kept it), so this pins the repair: the shipped
client, desktop mode, real posts; both removed the way the bug leaves them; the next redraw rebuilds them, they work,
and the repair is counted (PC.tlHeals) for the next report.

The harness below is test_desktop_back_then_scroll_full_app's.

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
                await b.until("document.querySelectorAll('#feed #tl-notes .note').length>=3")
                assert await b.js("!!document.querySelector('#feed .tl-cmp') && !!document.querySelector('#feed #tl-fab')")
                # The state the report describes: the notes are there, their header and the plus are not.
                await b.js("(()=>{const f=document.querySelector('#feed'),n=f.querySelector('#tl-notes');"
                           "while(n.previousSibling)n.previousSibling.remove();f.querySelector('#tl-fab').remove();})()")
                assert await b.js("!document.querySelector('#feed .tl-cmp') && !document.querySelector('#feed #tl-fab')")
                # The in-place redraw every live arrival does (it keeps the cards; returning to the view would rebuild
                # the whole feed and so hide the bug, which never re-entered that path).
                await b.js("__PC.redrawTimeline()")
                await b.until("!!document.querySelector('#feed .tl-cmp') && !!document.querySelector('#feed #tl-fab')")
                got = await b.js("({cmpBeforeNotes:!!document.querySelector('#feed .tl-cmp')&&(document.querySelector('#feed .tl-cmp').compareDocumentPosition(document.querySelector('#feed #tl-notes'))&4)>0,"
                                 "headers:document.querySelectorAll('#feed .tl-cmp').length,fabs:document.querySelectorAll('#feed #tl-fab').length,"
                                 "notes:document.querySelectorAll('#feed #tl-notes .note').length,heals:__PC.tlHeals()})")
                assert got["cmpBeforeNotes"] and got["headers"] == 1 and got["fabs"] == 1, got
                assert got["notes"] >= 3, ("the repair threw the posts away", got)
                assert got["heals"] and "header" in got["heals"][-1]["what"] and "plus" in got["heals"][-1]["what"], got
                # The rebuilt + still opens the composer.
                await b.js("document.querySelector('#feed #tl-fab').click()")
                await b.until("!!document.querySelector('#modal-root .compose, #modal-root textarea, .modal textarea')")
        finally:
            p.kill()
            server.shutdown()


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
def test_a_social_window_that_lost_its_composer_gets_it_back():
    asyncio.run(run())
