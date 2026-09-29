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
