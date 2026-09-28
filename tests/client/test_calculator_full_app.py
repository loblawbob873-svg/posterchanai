"""The Calculator app in the real bundled client: phone (classic) and PosterChanOS desktop.

Asserts what a person sees: it is in the sidebar and the More sheet, it opens, the keypad fits the
screen with keys a thumb can hit, real key presses compute, the tape records, and on the desktop it
opens as its own window whose keys only act while that window is focused.
"""
import asyncio
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop

KEYS = {"Enter": (13, "Enter", "\r"), "Backspace": (8, "Backspace", ""), "Escape": (27, "Escape", "")}


async def _type(b, text):
    for ch in text:
        if ch in KEYS:
            code, name, t = KEYS[ch]
        else:
            code, name, t = ord(ch.upper()) if ch.isalnum() else 0, "", ch
        base = {"key": ch, "windowsVirtualKeyCode": code}
        await b.call("Input.dispatchKeyEvent", dict(base, type="keyDown", **({"text": t} if t else {})))
        await b.call("Input.dispatchKeyEvent", dict(base, type="keyUp"))


LAYOUT = r"""(()=>{const r=e=>e.getBoundingClientRect(), keys=[...document.querySelectorAll('.calc-pad .calc-key')];
  return {keys:keys.length, fit:keys.every(k=>r(k).left>=0&&r(k).right<=innerWidth+1&&r(k).width>=44&&r(k).height>=44),
          overflow:document.documentElement.scrollWidth>innerWidth+1,
          padVisible:keys.every(k=>r(k).bottom<=innerHeight+1)}})()"""


@pytest.fixture(scope="module", autouse=True)
def bundle():
    yield from desktop.bundle.__wrapped__()


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
def test_on_a_phone_it_fits_and_computes():
    async def check(b):
        await b.call("Emulation.setDeviceMetricsOverride", {"width": 390, "height": 844, "deviceScaleFactor": 3, "mobile": True})
        await desktop.login(b)
        await b.js("try{ if(window.PCOS && PCOS.isOn()) PCOS.exit(); }catch(_){}")
        assert await b.js("!!document.querySelector('.nav-item[data-view=\"calculator\"]')"), "not in the sidebar"
        await b.js("document.getElementById('btn-more-m').click()")
        await b.until("!!document.querySelector('#modal-root [data-view=\"calculator\"], #modal-root [data-v=\"calculator\"], #modal-root .more-item')")
        assert await b.js("[...document.querySelectorAll('#modal-root .more-item')].some(x=>/Calculator/.test(x.textContent))"), \
            "not in the More sheet"
        await b.js("__PC.closeModal && __PC.closeModal(); __PC.switchView('calculator')")
        await b.until("!!document.querySelector('#feed .calc-app')")
        lay = await b.js(LAYOUT)
        assert lay == {"keys": 20, "fit": True, "overflow": False, "padVisible": True}, lay
        await _type(b, "12+3*4")
        assert await b.js("document.querySelector('.calc-res').textContent") == "= 24"
        await b.call("Input.dispatchKeyEvent", {"type": "keyDown", "key": "Enter", "windowsVirtualKeyCode": 13, "text": "\r"})
        await b.call("Input.dispatchKeyEvent", {"type": "keyUp", "key": "Enter", "windowsVirtualKeyCode": 13})
        assert await b.js("document.querySelector('.calc-expr').textContent") == "24"
        assert await b.js("document.querySelector('.calc-tape-row b').textContent") == "= 24"
        # Tapping works as well as typing.
        await b.js("document.querySelector('.calc-key[data-k=\"clear\"]').click()")
        for k in ("9", "/", "3", "eq"):
            await b.js("document.querySelector('.calc-key[data-k=\"%s\"]').click()" % k)
        assert await b.js("document.querySelector('.calc-expr').textContent") == "3"

    asyncio.run(desktop.with_browser("online", "", check))


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
def test_on_the_desktop_it_is_a_window_and_keys_follow_focus():
    async def check(b):
        await desktop.login(b)
        await b.until("!!window.PCOS && PCOS.isOn() && !!document.querySelector('#os-desk')")
        await b.js("document.getElementById('osfr')?.remove();document.documentElement.classList.remove('osfr-on')")
        await b.js("__PC.switchView('calculator')")
        await b.until("PCOS.windows().some(w=>w.appView==='calculator') && !!document.querySelector('.osw .calc-app')")
        inside = await b.js("""(()=>{const w=document.querySelector('.calc-app').closest('.osw').getBoundingClientRect();
          return [...document.querySelectorAll('.calc-pad .calc-key')].every(k=>{const r=k.getBoundingClientRect();
            return r.left>=w.left&&r.right<=w.right&&r.top>=w.top&&r.bottom<=w.bottom&&r.width>=30&&r.height>=30})})()""")
        assert inside, "the keypad does not fit its window"
        await _type(b, "6*7")
        assert await b.js("document.querySelector('.calc-res').textContent") == "= 42"
        # Another window focused: keys are not the calculator's.
        await b.js("__PC.switchView('notes')")
        await b.until("PCOS.windows().some(w=>w.appView==='notes')")
        await asyncio.sleep(.3)
        await _type(b, "9")
        assert await b.js("(document.querySelector('.calc-expr')||{}).textContent||''") in ("6*7", ""), \
            "a key typed at another window reached the calculator"

    asyncio.run(desktop.with_browser("online", "", check))
