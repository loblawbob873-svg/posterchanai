"""PosterChan dances on the desktop -- and is easy to move, hide and bring back.

"i want a live dancing posterchan character on the desktop mode that you can interact with" /
"make sure users can disable the dancing posterchan somehow sometimes they may hate it" / "she is moveable?"
/ "make sure works on webui and OS". Runs the BUNDLED desktop app (the same build PosterChanOS and the
desktop apps ship), with real mouse input, so it also proves the frames are in the bundle.
"""
import asyncio
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop


@pytest.fixture(scope="module", autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


INIT = r'''
window.__publishOK = true;
window.__buddySaves = [];
'''

RECT = "(s=>{const n=typeof s==='string'?document.querySelector(s):s;if(!n)return null;const r=n.getBoundingClientRect();return {l:r.left,t:r.top,r:r.right,b:r.bottom,w:r.width,h:r.height}})"


async def _mouse(b, x, y, kind, buttons=1):
    await b.call("Input.dispatchMouseEvent", {"type": kind, "x": x, "y": y, "button": "left",
                                              "buttons": buttons, "clickCount": 1})


async def _menu_pick(b, target_js, label):
    await b.js(f"""(()=>{{const t={target_js};const r=t.getBoundingClientRect();
        t.dispatchEvent(new MouseEvent('contextmenu',{{bubbles:true,cancelable:true,clientX:r.left+r.width/2,clientY:r.top+r.height/2}}));}})()""")
    await asyncio.sleep(.2)
    return await b.js(f"""(()=>{{const row=[...document.querySelectorAll('.os-ctx .os-ctx-b')].find(x=>x.textContent.trim()==={label!r});
        if(!row) return [...document.querySelectorAll('.os-ctx .os-ctx-b')].map(x=>x.textContent.trim());row.click();return true;}})()""")


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
def test_the_desktop_posterchan_dances_moves_hides_and_comes_back():
    res = {}

    async def check(b):
        await desktop.login(b)
        await b.until("document.body.classList.contains('os-on') && !!document.querySelector('#os-desk')")
        await b.js("(()=>{const pc=window.__PC,s=pc.saveDesktopBuddy;pc.saveDesktopBuddy=v=>{__buddySaves.push(JSON.parse(JSON.stringify(v)));return s&&s(v)}})()")
        await b.until("(()=>{const i=document.querySelector('#os-desk .os-buddy img');return !!i&&i.complete&&i.naturalWidth>0})()")
        # Dances: the frame changes on its own.
        f0 = await b.js("PCBuddy._frame()")
        await asyncio.sleep(1.6)
        res["animates"] = (await b.js("PCBuddy._frame()")) != f0
        # Her pace ("posterchan is moving too fast"): count frame changes over 4 seconds, idle.
        res["pace"] = await b.js("new Promise(ok=>{let n=0,last=PCBuddy._frame();const t=setInterval(()=>{const f=PCBuddy._frame();if(f!==last){n++;last=f}},40);setTimeout(()=>{clearInterval(t);ok(n)},4000)})")
        # Where she is: on the desk, below windows, off the icons and the taskbar.
        res["place"] = await b.js(f"""(()=>{{const R={RECT};const bd=document.querySelector('.os-buddy');const me=R(bd);
            const over=(a,c)=>a&&c&&a.l<c.r-4&&a.r>c.l+4&&a.t<c.b-4&&a.b>c.t+4;
            return {{inDesk: bd.parentNode.id==='os-desk', z: +getComputedStyle(bd).zIndex,
                     onIcon: [...document.querySelectorAll('.os-icon')].some(i=>over(me,R(i))),
                     onBar: over(me,R('#os-bar'))}}}})()""")
        # Click: she reacts.
        r = await b.js(f"{RECT}('.os-buddy')")
        cx, cy = r["l"] + r["w"] / 2, r["t"] + r["h"] / 2
        await _mouse(b, cx, cy, "mousePressed"); await _mouse(b, cx, cy, "mouseReleased", 0)
        await asyncio.sleep(.15)
        res["reacts"] = await b.js("!!document.querySelector('.os-buddy-say.on')")
        # Drag her 260px left and 120px up.
        await _mouse(b, cx, cy, "mousePressed")
        for k in range(1, 11):
            await b.call("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": cx - 26 * k, "y": cy - 12 * k, "button": "left", "buttons": 1})
            await asyncio.sleep(.02)
        await _mouse(b, cx - 260, cy - 120, "mouseReleased", 0)
        await asyncio.sleep(.3)
        r2 = await b.js(f"{RECT}('.os-buddy')")
        res["moved"] = (r["l"] - r2["l"], r["t"] - r2["t"])
        res["saved"] = await b.js("__buddySaves.slice(-1)[0]||null")
        # Reload: she is where she was left.
        await b.call("Page.reload", {})
        await b.until("!!window.__PC && document.readyState==='complete'")
        await b.until("document.body.classList.contains('os-on') && !!document.querySelector('#os-desk .os-buddy img')")
        await asyncio.sleep(.6)
        r3 = await b.js(f"{RECT}('.os-buddy')")
        res["kept"] = (round(r3["l"] - r2["l"]), round(r3["t"] - r2["t"]))
        # Hide from her own menu.
        await b.js("window.__buddySaves=[];(()=>{const pc=window.__PC,s=pc.saveDesktopBuddy;pc.saveDesktopBuddy=v=>{__buddySaves.push(v);return s&&s(v)}})()")
        res["hide"] = await _menu_pick(b, "document.querySelector('.os-buddy')", "Hide PosterChan")
        await asyncio.sleep(.2)
        res["hidden"] = await b.js("!document.querySelector('.os-buddy') && __buddySaves.slice(-1)[0] && __buddySaves.slice(-1)[0].on===false")
        # Hidden + reload: nothing of her loads.
        await b.call("Page.reload", {})
        await b.until("!!window.__PC && document.readyState==='complete'")
        await b.until("document.body.classList.contains('os-on') && !!document.querySelector('#os-desk')")
        await asyncio.sleep(1.2)
        res["hidden_after_reload"] = await b.js("!document.querySelector('.os-buddy')")
        res["frames_fetched_while_hidden"] = await b.js("performance.getEntriesByType('resource').filter(e=>/mascot\\/dance\\//.test(e.name)).length")
        # The desktop's own menu brings her back.
        res["show"] = await _menu_pick(b, "document.querySelector('#os-desk')", "Show PosterChan")
        await b.until("!!document.querySelector('#os-desk .os-buddy img')")
        res["back"] = True
        # And the Settings switch turns her off again.
        await b.js("PCBuddy.hide()")
        res["switch_off"] = await b.js("!document.querySelector('.os-buddy') && !PCBuddy.isOn()")

    asyncio.run(desktop.with_browser("online", "", check, INIT))
    assert res["animates"], "she does not dance: the frame never changes"
    assert 3 <= res["pace"] <= 7, ("4s should be ~5-6 moves -- fewer is frozen, more is frantic", res["pace"])
    p = res["place"]
    assert p["inDesk"] and 0 < p["z"] < 10, ("not on the desktop layer, below windows", p)
    assert not p["onIcon"] and not p["onBar"], ("she covers an icon or the taskbar", p)
    assert res["reacts"], "clicking her does nothing"
    dx, dy = res["moved"]
    assert dx > 150 and dy > 60, ("dragging did not move her", res["moved"])
    assert res["saved"] and res["saved"]["on"] is True and 0 <= res["saved"]["x"] < 0.86, ("the new spot was not saved", res["saved"])
    assert abs(res["kept"][0]) <= 3 and abs(res["kept"][1]) <= 3, ("after a reload she was not where she was left", res["kept"])
    assert res["hide"] is True, ("her menu has no Hide PosterChan", res["hide"])
    assert res["hidden"], "Hide did not remove her (or did not save it)"
    assert res["hidden_after_reload"], "hidden, then a reload brought her back"
    assert res["frames_fetched_while_hidden"] == 0, "hidden, and her frames were still downloaded"
    assert res["show"] is True and res["back"], ("the desktop menu does not offer her back", res["show"])
    assert res["switch_off"]
