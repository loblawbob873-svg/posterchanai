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
        # OVER WINDOWS ("posterchan should be going over windows right? should never be hidden unless
        # you kill it"): open an app and put its window right on top of her.
        await b.js("PCOS.routeView('notes');true")
        await b.until("!!document.querySelector('.osw')")
        res["over"] = await b.js(f"""(()=>{{const R={RECT};const me=R('.os-buddy'),w=document.querySelector('.osw');
            w.style.left=(me.l-40)+'px';w.style.top=(me.t-40)+'px';w.style.width=(me.w+80)+'px';w.style.height=(me.h+80)+'px';
            const hit=document.elementFromPoint(me.l+me.w/2,me.t+me.h/2);
            return {{onTop:!!(hit&&hit.closest('.os-buddy')), covered:!!(hit&&hit.closest('.osw'))}}}})()""")
        await b.js("(()=>{const w=document.querySelector('.osw .osw-close, .osw [data-act=close]');if(w)w.click();})();true")
        await asyncio.sleep(.3)
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
        # Switch dancer from her own menu: the axolotl takes her place and the choice is saved.
        await b.js("window.__buddySaves=[];(()=>{const pc=window.__PC,s=pc.saveDesktopBuddy;pc.saveDesktopBuddy=v=>{__buddySaves.push(JSON.parse(JSON.stringify(v)));return s&&s(v)}})()")
        res["switch"] = await _menu_pick(b, "document.querySelector('.os-buddy')", "Switch to Axolotl")
        await b.until("(()=>{const i=document.querySelector('#os-desk .os-buddy img');return !!i&&/\\/mascot\\/axolotl\\//.test(i.src)&&i.complete&&i.naturalWidth>0})()")
        res["axo_saved"] = await b.js("(__buddySaves.slice(-1)[0]||{}).who")
        await b.call("Page.reload", {})
        await b.until("!!window.__PC && document.readyState==='complete'")
        await b.until("document.body.classList.contains('os-on') && !!document.querySelector('#os-desk .os-buddy img')")
        res["axo_after_reload"] = await b.js("/\\/mascot\\/axolotl\\//.test(document.querySelector('#os-desk .os-buddy img').src)")
        res["axo_menu"] = await b.js("(()=>{const t=document.querySelector('.os-buddy');const r=t.getBoundingClientRect();t.dispatchEvent(new MouseEvent('contextmenu',{bubbles:true,cancelable:true,clientX:r.left+5,clientY:r.top+5}));const rows=[...document.querySelectorAll('.os-ctx .os-ctx-b')].map(x=>x.textContent.trim());document.body.click();return rows})()")
        # Back to PosterChan the other way, from Settings' choice.
        await b.js("PCBuddy.choose('posterchan');true")
        await b.until("/\\/mascot\\/dance\\//.test((document.querySelector('#os-desk .os-buddy img')||{}).src||'')")
        await b.js("window.__buddySaves=[];(()=>{const pc=window.__PC,s=pc.saveDesktopBuddy;pc.saveDesktopBuddy=v=>{__buddySaves.push(JSON.parse(JSON.stringify(v)));return s&&s(v)}})()")
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
    assert p["inDesk"] and 200 < p["z"] < 320, ("not over the windows (10-200) and under the menus (320+)", p)
    assert res["over"] == {"onTop": True, "covered": False}, ("a window covered her", res["over"])
    assert not p["onIcon"] and not p["onBar"], ("she covers an icon or the taskbar", p)
    assert res["reacts"], "clicking her does nothing"
    dx, dy = res["moved"]
    assert dx > 150 and dy > 60, ("dragging did not move her", res["moved"])
    assert res["saved"] and res["saved"]["on"] is True and 0 <= res["saved"]["x"] < 0.86, ("the new spot was not saved", res["saved"])
    assert abs(res["kept"][0]) <= 3 and abs(res["kept"][1]) <= 3, ("after a reload she was not where she was left", res["kept"])
    assert res["switch"] is True, ("her menu has no Switch to Axolotl", res["switch"])
    assert res["axo_saved"] == "axolotl" and res["axo_after_reload"], ("the axolotl choice did not stick", res)
    assert "Hide Axolotl" in res["axo_menu"] and "Switch to PosterChan" in res["axo_menu"], res["axo_menu"]
    assert res["hide"] is True, ("her menu has no Hide PosterChan", res["hide"])
    assert res["hidden"], "Hide did not remove her (or did not save it)"
    assert res["hidden_after_reload"], "hidden, then a reload brought her back"
    assert res["frames_fetched_while_hidden"] == 0, "hidden, and her frames were still downloaded"
    assert res["show"] is True and res["back"], ("the desktop menu does not offer her back", res["show"])
    assert res["switch_off"]


NATIVE_INIT = r'''
window.__publishOK = true;
window.__buddySaves = [];
window.__native = { shows: [], hides: 0, cb: null };
window.pcBuddy = { show: w => { __native.shows.push(w); return Promise.resolve(true); },
                   hide: () => { __native.hides++; return Promise.resolve(true); },
                   onEvent: cb => { __native.cb = cb; } };
'''


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
def test_on_posterchanos_her_own_window_draws_her_and_the_desktop_keeps_her_state():
    """PosterChanOS app windows are compositor toplevels over the desktop surface: she must be handed
    to her own always-on-top window (desktop/buddy-host.js), never drawn under them."""
    res = {}

    async def check(b):
        await desktop.login(b)
        await b.until("document.body.classList.contains('os-on') && !!document.querySelector('#os-desk')")
        await b.js("(()=>{const pc=window.__PC,s=pc.saveDesktopBuddy;pc.saveDesktopBuddy=v=>{__buddySaves.push(JSON.parse(JSON.stringify(v)));return s&&s(v)}})()")
        # Real app windows on this machine (what PosterChanOS reports), then the desktop redraws her.
        await b.js("PCOSWin.enabled=()=>true;PCBuddy.refresh();true")
        await b.until("__native.shows.length>0")
        res["drawn_in_page"] = await b.js("!!document.querySelector('.os-buddy')")
        res["show"] = await b.js("__native.shows.slice(-1)[0]")
        res["desk"] = await b.js("(()=>{const r=document.querySelector('#os-desk').getBoundingClientRect();return {l:r.left,t:r.top,w:r.width,h:r.height}})()")
        # Her window was dragged to the desk's top-left area: the desktop saves where she landed.
        await b.js("__native.cb({type:'moved', vx:" + "document.querySelector('#os-desk').getBoundingClientRect().left+20" + ", vy:document.querySelector('#os-desk').getBoundingClientRect().top+10});true")
        res["saved"] = await b.js("__buddySaves.slice(-1)[0]||null")
        # Her own "Hide PosterChan".
        await b.js("__native.cb({type:'hide'});true")
        await asyncio.sleep(.2)
        res["hidden"] = await b.js("({on:PCBuddy.isOn(), saved:(__buddySaves.slice(-1)[0]||{}).on, hides:__native.hides})")
        # Back from the desktop's menu, then off from the desktop side (Settings): her window closes.
        await b.js("__native.shows.length=0;PCBuddy.show();true")
        await b.until("__native.shows.length>0")
        await b.js("PCBuddy.hide();true")
        res["desk_hide"] = await b.js("({on:PCBuddy.isOn(), hides:__native.hides})")

    asyncio.run(desktop.with_browser("online", "", check, NATIVE_INIT))
    assert res["drawn_in_page"] is False, "drawn on the desktop surface, which every app window covers"
    s, d = res["show"], res["desk"]
    assert s["bw"] > 100 and s["bh"] > s["bw"], ("her window has no size", s)
    assert d["l"] <= s["vx"] <= d["l"] + d["w"] and s["vy"] <= d["t"] + d["h"], ("her window is off the desk", s, d)
    assert res["saved"] and res["saved"]["on"] is True and res["saved"]["x"] < 0.1 and res["saved"]["y"] < 0.1, \
        ("a drag of her window was not saved", res["saved"])
    # Her own Hide already closed her window in the host; the desktop only records it.
    assert res["hidden"] == {"on": False, "saved": False, "hides": 0}, ("her Hide did not stick", res["hidden"])
    assert res["desk_hide"] == {"on": False, "hides": 1}, ("hiding from the desktop left her window up", res["desk_hide"])
