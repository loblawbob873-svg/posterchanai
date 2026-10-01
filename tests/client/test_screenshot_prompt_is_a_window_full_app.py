"""PosterChanOS screenshots: the prompt is a window above every application, and choosing an area works.

Reported: "new screen capture menu is going behind active windows so it becomes useless" — the prompt
was a sheet on the desktop surface, which is below every real toplevel — and "selecting region does
nothing": the tray's "Choose an area…" runs in the tray POPUP, whose closePop() closes that window, so
the capture awaited after it was never started. The real bundled client, with stubbed bridges.
"""
import asyncio
import json
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop
from tests.client.test_a_focused_desktop_window_comes_in_front_full_app import COMPOSITOR

PREVIEW = "data:image/svg+xml," + "%3Csvg xmlns='http://www.w3.org/2000/svg' width='640' height='360'%3E%3Crect width='640' height='360' fill='%23123'/%3E%3C/svg%3E"
BRIDGES = r"""
window.__shot=[]; window.__acts=[]; window.__opened=[]; window.__closed=0;
window.pcShot={
  available: async()=>({ok:true, region:true}),
  stage: async()=>{ __shot.push(['stage']); return {ok:true, staged:true, path:'/tmp/posterchan-shots-1000/s.png', preview:%PREVIEW%}; },
  discard: async(p)=>{ __shot.push(['discard', p]); return {ok:true}; },
  take: async(o)=>{ __shot.push(['take', o]); return {ok:true, path:'/home/u/Pictures/Screenshots/PosterChan-x.png', copied:!!o.copy}; },
};
window.pcPopup={ open: async(k,r)=>{ __opened.push([k,r]); return true; }, toggle: async()=>true, close: async()=>true,
                 pick: async()=>true, act: async(a)=>{ __acts.push(a); return true; } };
window.close=function(){ window.__closed++; };
""".replace("%PREVIEW%", json.dumps(PREVIEW))


@pytest.fixture(scope="module", autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
def test_the_tray_popup_hands_the_capture_to_the_desktop():
    async def check(b):
        await b.until("!!window.PCOSShell && !!PCOSShell.takeShot")
        await b.js("PCOSShell.takeShot('region')")
        await asyncio.sleep(.5)
        assert "shot:region" in await b.js("__acts"), "the tray asked nobody to take the screenshot"
        assert not await b.js("__shot.some(x=>x[0]==='take')"), "the closing popup tried to take it itself"

    asyncio.run(desktop.with_browser("online", "?pcpopup=tray", check, BRIDGES))


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
def test_the_desktop_takes_a_capture_a_popup_asked_for():
    async def check(b):
        await desktop.login(b)
        await b.until("!!window.PCOS && PCOS.isOn() && !!window.__wm && !!__wm.emit")
        await b.js("__wm.emit({name:'tick',change:'run',payload:'pc:act:shot:region'})")
        await b.until("__shot.some(x=>x[0]==='take')")
        assert await b.js("__shot.find(x=>x[0]==='take')[1]") == {"mode": "region"}

    asyncio.run(desktop.with_browser("online", "", check, COMPOSITOR + BRIDGES))


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
def test_print_screen_opens_the_prompt_as_a_window_above_applications():
    async def check(b):
        await desktop.login(b)
        await b.until("!!window.PCOS && PCOS.isOn() && !!window.PCOSShell")
        await b.js("document.getElementById('osfr')?.remove();document.documentElement.classList.remove('osfr-on')")
        await b.js("document.dispatchEvent(new KeyboardEvent('keydown',{key:'PrintScreen',keyCode:44,bubbles:true,cancelable:true}))")
        await b.until("__opened.length>0")
        kind, rect = await b.js("__opened[0]")
        assert kind == "shot" and rect["width"] > 300 and rect["height"] > 200, (kind, rect)
        assert not await b.js("!!document.querySelector('.shot-prompt')"), "the prompt was ALSO drawn on the desktop surface"
        staged = await b.js("JSON.parse(localStorage.getItem('pc_shot_stage')||'null')")
        assert staged and staged["path"].endswith("s.png") and staged["preview"].startswith("data:"), staged

    asyncio.run(desktop.with_browser("online", "", check, COMPOSITOR + BRIDGES))


STAGED = r"""
localStorage.setItem('pc_shot_stage', JSON.stringify({path:'/tmp/posterchan-shots-1000/s.png', preview:%PREVIEW%, region:true, at:Date.now()}));
""".replace("%PREVIEW%", json.dumps(PREVIEW))


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
@pytest.mark.parametrize("choice", ["save", "region", "cancel"])
def test_the_prompt_window_does_what_each_choice_says(choice):
    async def check(b):
        await b.until("!!document.querySelector('.os-shot-popup .shot-prompt')")
        assert await b.js("!!document.querySelector('.shot-prompt .shot-prev')"), "no preview of what was captured"
        await b.js(f"document.querySelector('[data-shot-act=\"{choice}\"]').click()")
        await asyncio.sleep(1.2)
        shot, acts, closed = await b.js("[__shot, __acts, __closed]")
        if choice == "save":
            assert ["take", {"staged": "/tmp/posterchan-shots-1000/s.png", "copy": False}] in shot, shot
            assert "Saved" in await b.js("document.querySelector('.shot-foot').textContent")
            assert closed >= 1, "the prompt window stayed open after saving"
        elif choice == "region":
            assert ["discard", "/tmp/posterchan-shots-1000/s.png"] in shot and "shot:region" in acts, (shot, acts)
        else:
            assert ["discard", "/tmp/posterchan-shots-1000/s.png"] in shot and closed >= 1, (shot, closed)
        assert not await b.js("localStorage.getItem('pc_shot_stage')"), "the staged capture was left in storage"

    asyncio.run(desktop.with_browser("online", "?pcpopup=shot", check, BRIDGES + STAGED))


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
def test_the_prompt_survives_the_page_finishing_its_boot():
    """Reported: "screenshot still broken, pops up and disappears". The prompt window is the whole
    client, signed in like every other window, and app.js runs PCOS.restore() AGAIN once the identity
    is known — which re-ran renderShotPopup. The first run had already taken the staged capture out of
    storage, so the second found nothing and closed the window: a flash, then gone."""
    async def check(b):
        await b.until("!!document.querySelector('.os-shot-popup .shot-prompt')")
        await b.js("PCOS.restore()")                 # what boot does after the identity arrives
        await desktop.login(b)                       # …and signing in does it once more
        await b.js("PCOS.restore()")
        await asyncio.sleep(.6)
        state = await b.js("({closed:__closed, prompt:!!document.querySelector('.os-shot-popup .shot-prompt'),"
                           " prev:!!document.querySelector('.shot-prompt .shot-prev')})")
        assert state == {"closed": 0, "prompt": True, "prev": True}, ("the prompt window closed itself", state)
        # And it still does its job afterwards.
        await b.js("document.querySelector('[data-shot-act=\"save\"]').click()")
        await asyncio.sleep(1.2)
        assert ["take", {"staged": "/tmp/posterchan-shots-1000/s.png", "copy": False}] in await b.js("__shot")

    asyncio.run(desktop.with_browser("online", "?pcpopup=shot", check, BRIDGES + STAGED))


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
def test_the_prompt_window_has_a_visible_neon_frame():
    """"The screen capture window should have a border and look cyberpunk and cool." As its own window
    the prompt filled it edge to edge with no frame at all. Measured, not asserted from the source:
    a visible accent border on all four sides, the corner brackets drawn, and the controls inside it."""
    got = {}

    async def check(b):
        await b.until("!!document.querySelector('.os-shot-popup .shot-prompt')")
        got.update(await b.js("""(()=>{const p=document.querySelector('.shot-prompt'),cs=getComputedStyle(p),
          after=getComputedStyle(p,'::after'),r=p.getBoundingClientRect(),acts=p.querySelector('.shot-acts').getBoundingClientRect();
          const sides=['Top','Right','Bottom','Left'].map(s=>({w:parseFloat(cs['border'+s+'Width']),c:cs['border'+s+'Color']}));
          return {sides,accent:getComputedStyle(document.documentElement).getPropertyValue('--accent-rgb').trim(),
            brackets:after.content!=='none'&&(after.backgroundImage.match(/linear-gradient/g)||[]).length,
            inside:acts.left>=r.left&&acts.right<=r.right&&acts.bottom<=r.bottom,glow:cs.boxShadow}})()"""))

    asyncio.run(desktop.with_browser("online", "?pcpopup=shot", check, BRIDGES + STAGED))
    for s in got["sides"]:
        assert s["w"] >= 1 and s["c"] not in ("rgba(0, 0, 0, 0)", "transparent"), got
    rgb = ", ".join(x.strip() for x in got["accent"].split(","))
    assert rgb and rgb in got["sides"][0]["c"], ("the frame is not the theme's accent", got)
    assert got["brackets"] >= 8, got
    assert got["inside"], got
    assert got["glow"] and got["glow"] != "none", got


# ---- "add option for a timed delay before the capture so user has time to prep … Make entire screen
#      capture window much larger in size" ------------------------------------------------------------

@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
def test_the_prompt_window_takes_most_of_the_screen():
    async def check(b):
        await desktop.login(b)
        await b.until("!!window.PCOS && PCOS.isOn() && !!window.PCOSShell")
        await b.js("document.getElementById('osfr')?.remove();document.documentElement.classList.remove('osfr-on')")
        await b.js("document.dispatchEvent(new KeyboardEvent('keydown',{key:'PrintScreen',keyCode:44,bubbles:true,cancelable:true}))")
        await b.until("__opened.length>0")
        kind, rect = await b.js("__opened[0]")
        vw, vh = await b.js("[innerWidth, innerHeight]")
        assert kind == "shot" and rect["width"] >= 0.8 * vw and rect["height"] >= 0.8 * vh, (rect, vw, vh)
        assert rect["x"] >= 0 and rect["y"] >= 0 and rect["x"] + rect["width"] <= vw + 1, rect

    asyncio.run(desktop.with_browser("online", "", check, COMPOSITOR + BRIDGES))


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
def test_in_the_big_window_the_preview_fills_it_and_a_delay_hands_off_to_the_desktop():
    async def check(b):
        await b.call("Emulation.setDeviceMetricsOverride", {"width": 1180, "height": 760, "deviceScaleFactor": 1, "mobile": False})
        await b.until("!!document.querySelector('.os-shot-popup .shot-prompt')")
        lay = await b.js("""(()=>{const p=document.querySelector('.shot-prev').getBoundingClientRect(),
            d=[...document.querySelectorAll('[data-shot-delay]')].map(b=>b.dataset.shotDelay),
            acts=document.querySelector('.shot-acts').getBoundingClientRect();
            return {prevH:p.height, d, actsInside:acts.bottom<=innerHeight}})()""")
        assert lay["d"] == ["3", "5", "10"], lay
        assert lay["prevH"] >= 0.5 * 760 and lay["actsInside"], ("the preview does not fill the window", lay)
        await b.js("document.querySelector('[data-shot-delay=\"5\"]').click()")
        await asyncio.sleep(.6)
        shot, acts = await b.js("[__shot, __acts]")
        assert ["discard", "/tmp/posterchan-shots-1000/s.png"] in shot and "shot:delay-5" in acts, (shot, acts)
        assert not any(x[0] == "take" for x in shot), "a timed retake saved the old capture"

    asyncio.run(desktop.with_browser("online", "?pcpopup=shot", check, BRIDGES + STAGED))


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
def test_the_desktop_counts_down_on_the_taskbar_and_the_countdown_is_not_in_the_picture():
    """The countdown is a taskbar chip (a window would take the keyboard from what is being set up);
    it is gone BEFORE the capture is staged, and the new capture opens in the prompt."""
    async def check(b):
        await desktop.login(b)
        await b.until("!!window.PCOS && PCOS.isOn() && !!window.__wm && !!__wm.emit && !!document.getElementById('os-bar')")
        await b.js("{const st=pcShot.stage;pcShot.stage=async()=>{__shot.push(['chip-at-capture', !!document.getElementById('os-shot-count')]);return st();};}")
        await b.js("window.__chips=[];new MutationObserver(()=>{const c=document.getElementById('os-shot-count');"
                   "const t=c?c.textContent:'';if(t&&__chips[__chips.length-1]!==t)__chips.push(t);})"
                   ".observe(document.body,{subtree:true,childList:true,characterData:true})")
        await b.js("__wm.emit({name:'tick',change:'run',payload:'pc:act:shot:delay-3'})")
        await b.until("__chips.includes('◉ 2')")
        assert not await b.js("__shot.some(x=>x[0]==='stage')"), "it captured before the countdown ended"
        await b.until("__opened.some(o=>o[0]==='shot')")
        assert await b.js("__chips") == ["◉ 3", "◉ 2", "◉ 1"], await b.js("__chips")
        assert await b.js("__shot.find(x=>x[0]==='chip-at-capture')[1]") is False, "the countdown chip was in the picture"
        assert not await b.js("!!document.getElementById('os-shot-count')")

    asyncio.run(desktop.with_browser("online", "", check, COMPOSITOR + BRIDGES))


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
def test_clicking_the_countdown_cancels_it():
    async def check(b):
        await desktop.login(b)
        await b.until("!!window.PCOS && PCOS.isOn() && !!window.PCOSShell && !!document.getElementById('os-bar')")
        await b.js("PCOSShell.delayedShot(3)")
        await b.until("!!document.getElementById('os-shot-count')")
        await b.js("document.getElementById('os-shot-count').click()")
        await asyncio.sleep(3.6)
        assert not await b.js("__shot.some(x=>x[0]==='stage')"), "a cancelled countdown still took the screenshot"
        assert not await b.js("!!document.getElementById('os-shot-count')")

    asyncio.run(desktop.with_browser("online", "", check, COMPOSITOR + BRIDGES))
