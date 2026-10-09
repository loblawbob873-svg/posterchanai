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
def test_in_the_big_window_the_preview_fills_it_and_there_is_no_retake_timer():
    """The delay belongs BEFORE the capture, in the chooser -- not as a retake after it ("the timer was
    supposed to be a timer after you choose what you want to do. you fucked it all up")."""
    async def check(b):
        await b.call("Emulation.setDeviceMetricsOverride", {"width": 1180, "height": 760, "deviceScaleFactor": 1, "mobile": False})
        await b.until("!!document.querySelector('.os-shot-popup .shot-prompt')")
        lay = await b.js("""(()=>{const p=document.querySelector('.shot-prev').getBoundingClientRect(),
            acts=document.querySelector('.shot-acts').getBoundingClientRect();
            return {prevH:p.height, retake:document.querySelectorAll('[data-shot-delay], .shot-delay').length,
                    actsInside:acts.bottom<=innerHeight}})()""")
        assert lay["retake"] == 0, ("the prompt still offers a timed retake", lay)
        assert lay["prevH"] >= 0.5 * 760 and lay["actsInside"], ("the preview does not fill the window", lay)

    asyncio.run(desktop.with_browser("online", "?pcpopup=shot", check, BRIDGES + STAGED))


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
def test_in_the_capture_window_the_delay_comes_before_the_action_you_choose():
    """ "the timer was supposed to be a timer after you choose what you want to do ... like a 5s delay
    before the screenshot action happens (full, rectangle, etc)": pick the delay, then Select region or
    Full screen -- the window hands BOTH to the desktop, which counts down and then does that action."""
    got = {}

    async def check(b):
        await b.until("!!document.querySelector('.os-shot-popup .shot-prompt')")
        got["opts"] = await b.js("[...document.querySelectorAll('[data-shot-wait]')].map(x=>x.textContent.trim())")
        got["acts_on_screen"] = await b.js("[...document.querySelectorAll('[data-shot-act]')].map(x=>x.dataset.shotAct)")
        await b.js("document.querySelector('[data-shot-wait=\"5\"]').click()")
        got["on"] = await b.js("[...document.querySelectorAll('[data-shot-wait].on')].map(x=>x.dataset.shotWait)")
        got["kept"] = await b.js("localStorage.getItem('pc_shot_delay')")
        await b.js("document.querySelector('[data-shot-act=\"screen\"]').click()")
        await asyncio.sleep(.5)
        got["acts"] = await b.js("__acts"); got["shot"] = await b.js("__shot")

    asyncio.run(desktop.with_browser("online", "?pcpopup=shot", check, BRIDGES + STAGED))
    assert got["opts"] == ["None", "3 s", "5 s", "10 s"], got
    assert got["acts_on_screen"] == ["save", "copy", "markup", "region", "screen", "cancel"], got
    assert got["on"] == ["5"] and got["kept"] == "5", got
    assert got["acts"] == ["shot:delay-5-screen"], ("the window must hand the delay AND the action to the desktop", got)
    assert ["discard", "/tmp/posterchan-shots-1000/s.png"] in got["shot"], got
    assert not any(x[0] == "take" for x in got["shot"]), "it saved the old capture instead of taking a new one"


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
def test_with_no_delay_select_region_happens_at_once():
    got = {}

    async def check(b):
        await b.js("localStorage.setItem('pc_shot_delay','0')")
        await b.until("!!document.querySelector('.os-shot-popup .shot-prompt')")
        assert await b.js("document.querySelector('[data-shot-wait].on').dataset.shotWait") == "0"
        await b.js("document.querySelector('[data-shot-act=\"region\"]').click()")
        await asyncio.sleep(.5)
        got["acts"] = await b.js("__acts")

    asyncio.run(desktop.with_browser("online", "?pcpopup=shot", check, BRIDGES + STAGED))
    assert got["acts"] == ["shot:region"], got


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
def test_after_the_countdown_the_desktop_runs_the_action_that_was_chosen():
    """An AREA chosen with a delay: the countdown runs on the taskbar, nothing is captured until it ends,
    and THEN the area picker runs (pcShot.take with region) -- not a whole-screen grab."""
    async def check(b):
        await desktop.login(b)
        await b.until("!!window.PCOS && PCOS.isOn() && !!window.__wm && !!__wm.emit && !!document.getElementById('os-bar')")
        await b.js("window.__chips=[];new MutationObserver(()=>{const c=document.getElementById('os-shot-count');"
                   "const t=c?c.textContent:'';if(t&&__chips[__chips.length-1]!==t)__chips.push(t);})"
                   ".observe(document.body,{subtree:true,childList:true,characterData:true})")
        await b.js("__wm.emit({name:'tick',change:'run',payload:'pc:act:shot:delay-3-region'})")
        await b.until("__chips.includes('◉ 2')")
        assert not await b.js("__shot.some(x=>x[0]==='take'||x[0]==='stage')"), "it captured before the countdown ended"
        await b.until("__shot.some(x=>x[0]==='take'||x[0]==='stage')", )
        first = await b.js("__shot.find(x=>x[0]==='take'||x[0]==='stage')")
        assert first[0] == "take" and first[1].get("mode") == "region", ("the area picker should run, not a full grab", first)
        assert await b.js("__chips") == ["◉ 3", "◉ 2", "◉ 1"], await b.js("__chips")

    asyncio.run(desktop.with_browser("online", "", check, COMPOSITOR + BRIDGES))


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


# "we need a way to markup screenshots after taking them ... a button that opens up meme builder"
# (2026-10-09). Two halves, like Select region: the prompt saves and hands the path over, the desktop
# opens it -- the popup is closing and cannot host the Meme Builder.
@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
def test_mark_up_saves_the_shot_and_hands_it_to_the_desktop():
    async def check(b):
        await b.until("!!document.querySelector('.os-shot-popup .shot-prompt [data-shot-act=\"markup\"]')")
        await b.js("document.querySelector('[data-shot-act=\"markup\"]').click()")
        await asyncio.sleep(1.0)
        shot, acts = await b.js("[__shot, __acts]")
        assert ["take", {"staged": "/tmp/posterchan-shots-1000/s.png", "copy": False}] in shot, ("the original was not saved first", shot)
        assert "markup:" + "%2Fhome%2Fu%2FPictures%2FScreenshots%2FPosterChan-x.png" in acts, acts

    asyncio.run(desktop.with_browser("online", "?pcpopup=shot", check, BRIDGES + STAGED))


HOST = r"""
window.pcHost = Object.assign(window.pcHost||{}, { read: async (p, max)=>{ window.__read=[p,max];
  return new Uint8Array([137,80,78,71,13,10,26,10,0,0,0,0]); } });
"""


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
def test_the_desktop_opens_a_marked_up_shot_in_the_meme_builder():
    async def check(b):
        await desktop.login(b)
        await b.until("!!window.PCOS && PCOS.isOn() && !!window.__wm && !!__wm.emit")
        await b.js("window.__up=null; __PC.uploadBlob=async(f,o)=>{ window.__up=[f.name,f.type,f.size,o]; return 'https://media.example/shot.png'; }")
        await b.js("__wm.emit({name:'tick',change:'run',payload:'pc:act:markup:%2Fhome%2Fu%2FPictures%2FScreenshots%2FPosterChan-x.png'})")
        await b.until("!!window.__up")
        assert await b.js("__read[0]") == "/home/u/Pictures/Screenshots/PosterChan-x.png"
        assert await b.js("__up") == ["PosterChan-x.png", "image/png", 12, {"noCompress": True}], await b.js("__up")
        await b.until("[...document.querySelectorAll('img,video')].some(e=>(e.getAttribute('src')||'')==='https://media.example/shot.png')"
                      "||JSON.stringify(localStorage).indexOf('https://media.example/shot.png')>=0")

    asyncio.run(desktop.with_browser("online", "", check, COMPOSITOR + BRIDGES + HOST))
