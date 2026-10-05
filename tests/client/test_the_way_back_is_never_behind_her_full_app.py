"""With PosterChan set to let clicks through, right-clicking HER opens a menu you can see and use.

"posterchan, right click on her to get menu, hides behind her, totally useless" / "can't move posterchan by
dragging". She was in "clicks pass through" mode, so the right-click went through her to the desktop, and
the desktop's menu opened at the pointer -- under her: she is an always-on-top window, and the desktop is
the bottom surface. The only way back ("Make PosterChan clickable again") was in that menu, behind her.
Now a desktop right-click that lands on her opens BESIDE her, with the way back first.
"""
import asyncio
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop
from tests.client.test_desktop_buddy_full_app import NATIVE_INIT


@pytest.fixture(scope="module", autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
def test_right_clicking_a_click_through_posterchan_shows_the_way_back_beside_her():
    res = {}

    async def check(b):
        await desktop.login(b)
        await b.until("document.body.classList.contains('os-on') && !!document.querySelector('#os-desk')")
        await b.call("Emulation.setDeviceMetricsOverride", {"width": 1700, "height": 950, "deviceScaleFactor": 1, "mobile": False})
        await b.js("PCOSWin.enabled=()=>true;PCBuddy.refresh();true")
        await b.until("__native.shows.length>0")
        await b.js("PCBuddy.setClickThrough(true);true")
        await b.until("(__native.shows.slice(-1)[0]||{}).clickThrough===true")
        her = await b.js("(()=>{const s=__native.shows.slice(-1)[0];return {l:s.vx,t:s.vy,r:s.vx+s.bw,b:s.vy+s.bh}})()")
        x, y = (her["l"] + her["r"]) / 2, (her["t"] + her["b"]) / 2
        # The click went through her: the desktop receives it at her centre.
        await b.js(f"""(()=>{{const d=document.querySelector('#os-desk');
            d.dispatchEvent(new MouseEvent('contextmenu',{{bubbles:true,cancelable:true,clientX:{x},clientY:{y}}}));}})()""")
        await b.until("!!document.querySelector('.os-ctx')")
        res["menu"] = await b.js("(()=>{const m=document.querySelector('.os-ctx').getBoundingClientRect();"
                                 "return {l:m.left,t:m.top,r:m.right,b:m.bottom,first:document.querySelector('.os-ctx .os-ctx-b').textContent.trim()}})()")
        res["her"] = her

    asyncio.run(desktop.with_browser("online", "", check, NATIVE_INIT))
    m, h = res["menu"], res["her"]
    overlap = not (m["r"] <= h["l"] or m["l"] >= h["r"] or m["b"] <= h["t"] or m["t"] >= h["b"])
    assert not overlap, ("the menu opened under her always-on-top window, where it cannot be used", res)
    assert m["first"].startswith("Make") and "clickable" in m["first"], ("the way back is not the first row", res)
