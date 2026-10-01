"""Print Screen on PosterChanOS asks what to keep, in a HUD that fits the screen.

Asked for: "when screenshot keys are pressed, prompt user with nice cyberpunk-style UI to save to
Pictures or select region." Runs the real bundled desktop with a stubbed screenshot bridge and
asserts the ORDER (the picture is taken before the prompt exists, so the prompt is never in it),
what each choice does, and that the prompt fits a big and a small screen.
"""
import asyncio
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop

BRIDGE = r"""
window.__shot=[];
window.pcShot={
  available: async()=>({ok:true, region:true}),
  stage: async()=>{ __shot.push(['stage', !!document.querySelector('.shot-prompt')]);
    return {ok:true, staged:true, path:'/tmp/posterchan-shots-1000/PosterChan-2026-09-28-120000.png',
            preview:'data:image/svg+xml,'+encodeURIComponent('<svg xmlns="http://www.w3.org/2000/svg" width="640" height="360"><rect width="640" height="360" fill="#123"/></svg>')}; },
  discard: async(p)=>{ __shot.push(['discard', p]); return {ok:true}; },
  take: async(o)=>{ __shot.push(['take', o]); return {ok:true, path:'/home/u/Pictures/Screenshots/PosterChan-x.png', copied:!!o.copy}; },
};
"""


@pytest.fixture(scope="module", autouse=True)
def bundle():
    yield from desktop.bundle.__wrapped__()


async def _press(b):
    await b.js("__shot.length=0")
    await b.js("document.dispatchEvent(new KeyboardEvent('keydown',{key:'PrintScreen',keyCode:44,bubbles:true,cancelable:true}))")
    await b.until("!!document.querySelector('.shot-prompt')")


KEYS = {"Enter": (13, "Enter", "\r"), "Escape": (27, "Escape", ""), "r": (82, "KeyR", "r"), "f": (70, "KeyF", "f")}


async def _key(b, k):
    """A REAL key press (CDP), so a focused button activates on Enter exactly as it does for a person."""
    code, name, text = KEYS[k]
    base = {"key": k, "code": name, "windowsVirtualKeyCode": code, "nativeVirtualKeyCode": code}
    await b.call("Input.dispatchKeyEvent", dict(base, type="keyDown", **({"text": text} if text else {})))
    await b.call("Input.dispatchKeyEvent", dict(base, type="keyUp"))


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
def test_print_screen_prompts_and_each_choice_does_what_it_says():
    async def check(b):
        await desktop.login(b)
        await b.until("!!window.PCOS && PCOS.isOn() && !!window.PCOSShell")
        await b.js("document.getElementById('osfr')?.remove();document.documentElement.classList.remove('osfr-on')")
        for width, height in ((1440, 900), (400, 740)):
            await b.call("Emulation.setDeviceMetricsOverride", {"width": width, "height": height, "deviceScaleFactor": 1, "mobile": False})
            await _press(b)
            got = await b.js("""(()=>{const m=document.querySelector('.shot-modal').getBoundingClientRect();
              const btns=[...document.querySelectorAll('.shot-prompt [data-shot-act]')];
              return {first:__shot[0], acts:btns.map(x=>x.dataset.shotAct),
                fits:m.left>=0&&m.right<=innerWidth+1&&m.top>=0&&m.bottom<=innerHeight+1,
                tappable:btns.every(x=>{const r=x.getBoundingClientRect();return r.width>=44&&r.height>=44&&r.right<=innerWidth}),
                preview:!!document.querySelector('.shot-prev'), focus:document.activeElement&&document.activeElement.dataset.shotAct}})()""")
            assert got["first"] == ["stage", False], "the picture must be taken BEFORE the prompt is drawn"
            # "Full screen" is the delayed-capture partner of "Select region" (the delay runs BEFORE either).
            assert got["acts"] == ["save", "copy", "region", "screen", "cancel"], got
            assert got["fits"] and got["tappable"] and got["preview"], (width, got)
            assert got["focus"] == "save", "Enter must save"
            await _key(b, "Escape")
            await asyncio.sleep(.2)
            assert await b.js("__shot.slice(1)") == [["discard", "/tmp/posterchan-shots-1000/PosterChan-2026-09-28-120000.png"]]
            assert await b.js("!document.querySelector('.shot-prompt')")
        await b.call("Emulation.clearDeviceMetricsOverride", {})

        # Save to Pictures keeps THE STAGED picture -- no second capture of the prompt closing.
        await _press(b)
        await _key(b, "Enter")
        await asyncio.sleep(.3)
        assert await b.js("__shot.slice(1)") == [["take", {"staged": "/tmp/posterchan-shots-1000/PosterChan-2026-09-28-120000.png", "copy": False}]]

        # Select region throws the whole-screen shot away and runs the area picker.
        await _press(b)
        await _key(b, "r")
        await asyncio.sleep(.6)
        calls = await b.js("__shot.slice(1)")
        assert calls[0][0] == "discard" and calls[1] == ["take", {"mode": "region"}], calls

        # Full screen (F) likewise throws the staged shot away and captures the whole screen afresh --
        # with no delay set, straight away.
        await _press(b)
        await _key(b, "f")
        await asyncio.sleep(.6)
        calls = await b.js("__shot.slice(1)")
        assert calls[0][0] == "discard" and calls[1] == ["take", {"mode": "screen"}], calls

        # Save & copy.
        await _press(b)
        await b.js("document.querySelector('[data-shot-act=\"copy\"]').click()")
        await asyncio.sleep(.3)
        assert (await b.js("__shot.slice(1)"))[0][1]["copy"] is True

    asyncio.run(desktop.with_browser("online", "", check, extra_init=BRIDGE))
