"""The right panel: hidden by a button INSIDE it, brought back from Settings -- no floating tab.

"the sidebar button is getting in the way and users are saying it does not work right" / "no more
annoying tab button" / a user: "clicking it just makes it move but nothing happens. So seemingly has no
purpose being on the DMs screen and some others." The fold control was a 20px arrow tab floating at
mid-height on the panel's edge: 13-15px wide after the desktop zoom, over the timeline's right edge on a
1920 screen, and jumping ~250px sideways when used. Measured on the real client at 1440px wide.
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
                await b.call("Emulation.setDeviceMetricsOverride", dict(width=1440, height=900, deviceScaleFactor=1, mobile=False, screenWidth=1440, screenHeight=900))
                pass
                await b.call("Page.addScriptToEvaluateOnNewDocument", {"source": soc.INIT})
                await b.call("Page.navigate", {"url": f"http://127.0.0.1:{server.server_port}/client"})
                await b.until('!!window.__PC && !!window.NostrTools && document.readyState==="complete"')
                await b.until("document.body.classList.contains('guest')")
                await b.js("(()=>{const key=new Uint8Array(32).fill(1);document.querySelector('#nsec-input').value="
                           "NostrTools.nip19.nsecEncode(key);document.querySelector('#btn-nsec-login').click()})()")
                await b.until('!!__PC.me() && Relay.status==="ok"')
                await b.js("(()=>{const st=document.createElement('style');st.textContent='#toast-root{display:none!important}';document.head.appendChild(st);})()")
                await b.js("__PC.switchView('global')")
                await b.until("window.__feedCards() >= 8")
                await asyncio.sleep(1)
                R = "(el=>{const r=el.getBoundingClientRect();return {l:r.left,t:r.top,r:r.right,b:r.bottom,w:r.width,h:r.height}})"
                st = await b.js(f"""(()=>{{const t=document.getElementById('rb-toggle'),rb=document.querySelector('.rightbar'),f=document.getElementById('feed');
                    const vis=e=>!!e&&e.getClientRects().length>0&&getComputedStyle(e).display!=='none';
                    return {{toggle:vis(t)?{R}(t):null, panel:vis(rb)?{R}(rb):null, feed:{R}(f), pos:t?getComputedStyle(t).position:null}};}})()""")
                res = {"open": st}
                # Hide, from the button in the panel.
                tb = st["toggle"]
                for t in ("mousePressed", "mouseReleased"):
                    await b.call("Input.dispatchMouseEvent", dict(type=t, x=tb["l"] + tb["w"] / 2, y=tb["t"] + tb["h"] / 2, button="left", clickCount=1))
                await asyncio.sleep(.6)
                res["hidden"] = await b.js(f"""(()=>{{const rb=document.querySelector('.rightbar'),t=document.getElementById('rb-toggle');
                    const vis=e=>!!e&&e.getClientRects().length>0;
                    return {{panel:vis(rb), toggle:vis(t), stored:localStorage.getItem('rbCollapsed'),
                      floating:[...document.querySelectorAll('body *')].filter(e=>/rb-|right/.test(String(e.className))&&vis(e)&&['fixed','absolute'].includes(getComputedStyle(e).position)).length,
                      feedW:document.getElementById('feed').getBoundingClientRect().width}};}})()""")
                async def reload_and_read():
                    await b.call("Page.reload", {"ignoreCache": False})
                    await b.until('!!window.__PC && !!window.NostrTools && document.readyState==="complete"')
                    await b.until('!!__PC.me()')
                    await b.js("__PC.switchView('global')")
                    await b.until("window.__feedCards() >= 4")
                    await asyncio.sleep(.8)
                    panel = await b.js("document.querySelector('.rightbar').getClientRects().length>0")
                    await b.js("__PC.switchView('settings')")
                    await b.until("!!document.querySelector('.us-tab[data-tab=\"timeline\"]')")
                    await b.js("document.querySelector('.us-tab[data-tab=\"timeline\"]').click()")
                    await b.until("!!document.getElementById('set-right-panel')")
                    sw = await b.js("document.getElementById('set-right-panel').checked")
                    await b.js("__PC.switchView('global')")
                    await asyncio.sleep(.5)
                    return {"panel": panel, "switch": sw}
                res["after_hide_reload"] = await reload_and_read()
                # EVERY app, with the panel ON: where an app hides the panel to get the width ("so they have
                # more space"), its control must go too. The old tab stayed on 42 of them, doing nothing.
                await b.js("PCRightPanel.set(false)")
                views = await b.js("[...new Set([...document.querySelectorAll('.sidebar .nav-item[data-view]')].map(x=>x.dataset.view))]")
                stray = []
                for v in views:
                    await b.js(f"__PC.switchView({v!r})")
                    await asyncio.sleep(.35)
                    if await b.js("(()=>{const t=document.getElementById('rb-toggle'),rb=document.querySelector('.rightbar');"
                                  "const vis=e=>!!e&&e.getClientRects().length>0;return vis(t)&&!vis(rb);})()"):
                        stray.append(v)
                res["stray"] = (stray, len(views))
                await b.js("PCRightPanel.set(true)")
                # Messages, with the panel hidden: nothing of it anywhere ("no purpose being on the DMs screen").
                await b.js("__PC.switchView('messages')")
                await asyncio.sleep(.8)
                res["dms"] = await b.js("(()=>{const t=document.getElementById('rb-toggle');return !!t&&t.getClientRects().length>0;})()")
                # Bring it back from Settings -> Timeline.
                await b.js("__PC.switchView('settings')")
                await b.until("!!document.querySelector('.us-tab[data-tab=\"timeline\"]')")
                await b.js("document.querySelector('.us-tab[data-tab=\"timeline\"]').click()")
                await b.until("!!document.getElementById('set-right-panel')")
                res["switch_was"] = await b.js("document.getElementById('set-right-panel').checked")
                await b.js("(()=>{const x=document.getElementById('set-right-panel');x.checked=true;x.dispatchEvent(new Event('change'));})()")
                await b.js("__PC.switchView('global')")
                await asyncio.sleep(.8)
                res["back"] = await b.js("(()=>{const rb=document.querySelector('.rightbar');return rb.getClientRects().length>0 && localStorage.getItem('rbCollapsed')==='0';})()")
                res["after_show_reload"] = await reload_and_read()
                return res
        finally:
            p.terminate()
            p.wait(timeout=10)
            server.shutdown()


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
def test_the_right_panel_hides_from_inside_and_comes_back_from_settings():
    res = asyncio.run(run())
    o = res["open"]
    assert o["panel"] and o["toggle"], res
    p, t, f = o["panel"], o["toggle"], o["feed"]
    assert p["l"] <= t["l"] and t["r"] <= p["r"] + 1 and p["t"] <= t["t"] and t["b"] <= p["b"], (
        "the hide control sits outside the panel -- a floating tab again", o)
    assert t["l"] >= f["r"], ("the hide control overlaps the timeline", o)
    assert o["pos"] not in ("fixed", "absolute"), ("the hide control floats", o)
    assert t["w"] >= 24 and t["h"] >= 24, ("the hide control is too small to hit", t)
    stray, n = res["stray"]
    assert n > 30 and not stray, f"the panel control shows with no panel on {len(stray)} of {n} apps: {stray}"
    h = res["hidden"]
    assert not h["panel"] and not h["toggle"] and h["stored"] == "1", ("Hide did not hide the panel", h)
    assert h["floating"] == 0, ("something of the panel still floats over the page", h)
    assert h["feedW"] > f["w"], ("the timeline did not take the panel's room", h, f)
    assert res["dms"] is False, "a panel control shows on Messages with the panel hidden"
    assert res["switch_was"] is False, "Settings does not know the panel is hidden"
    assert res["back"], "Settings -> Timeline -> Right panel did not bring it back"
    assert res["after_hide_reload"] == {"panel": False, "switch": False}, (
        "after a reload the hidden panel came back, or Settings does not show it as off", res["after_hide_reload"])
    assert res["after_show_reload"] == {"panel": True, "switch": True}, (
        "after a reload the panel turned on in Settings is gone again", res["after_show_reload"])
