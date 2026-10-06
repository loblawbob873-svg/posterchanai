"""A start menu opened by a FINGER must not pull up the on-screen keyboard.

Reported on an Android tablet in desktop mode: "keyboard should not appear every time i click on the
start menu". The menu focused its search box on every open — right after Super, where typing is the
point, and wrong after a tap, where a focused text field IS the on-screen keyboard. Driven with real
CDP touch and mouse input against the shipped bundle: after a tap nothing typeable has focus, after a
mouse click the box does, and a hardware key pressed into a tapped-open menu still lands in the box.
"""
import asyncio
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop

FOCUS = "(()=>{const a=document.activeElement;return {id:a&&a.id||'',tag:a&&a.tagName||'',typeable:!!(a&&a.matches('input,textarea,[contenteditable=true]'))}})()"
CENTER = "(()=>{const r=document.querySelector('#os-start').getBoundingClientRect();return [r.left+r.width/2,r.top+r.height/2]})()"


async def _tap(b, xy):
    await b.call('Emulation.setTouchEmulationEnabled', {'enabled': True, 'maxTouchPoints': 5})
    pt = [{'x': xy[0], 'y': xy[1]}]
    await b.call('Input.dispatchTouchEvent', {'type': 'touchStart', 'touchPoints': pt})
    await b.call('Input.dispatchTouchEvent', {'type': 'touchEnd', 'touchPoints': []})


async def _click(b, xy):
    await b.call('Emulation.setTouchEmulationEnabled', {'enabled': False})
    for t in ('mouseMoved', 'mousePressed', 'mouseReleased'):
        await b.call('Input.dispatchMouseEvent', {'type': t, 'x': xy[0], 'y': xy[1], 'button': 'left',
                                                  'buttons': 1 if t == 'mousePressed' else 0, 'clickCount': 1})


async def _close(b):
    await b.js("document.querySelector('#os-startmenu') && document.querySelector('#os-start').click()")
    await b.until("!document.querySelector('#os-startmenu')")


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
def test_a_tapped_start_menu_raises_no_keyboard_and_a_clicked_one_is_ready_to_type():
    async def check(b):
        await desktop.login(b)
        await b.until("!!document.querySelector('#os-start')")
        xy = await b.js(CENTER)

        await _tap(b, xy)
        await b.until("!!document.querySelector('#os-q')")
        await asyncio.sleep(.2)
        got = await b.js(FOCUS)
        assert not got["typeable"], f"a tap focused a text field — that is the on-screen keyboard: {got}"
        # A tablet with a keyboard attached: typing still searches.
        await b.call('Input.dispatchKeyEvent', {'type': 'keyDown', 'key': 'n', 'code': 'KeyN', 'text': 'n',
                                                'windowsVirtualKeyCode': 78})
        await b.call('Input.dispatchKeyEvent', {'type': 'keyUp', 'key': 'n', 'code': 'KeyN', 'windowsVirtualKeyCode': 78})
        await asyncio.sleep(.1)
        assert await b.js("document.querySelector('#os-q').value") == "n", "a key typed into a tapped-open menu went nowhere"
        await _close(b)

        await _click(b, xy)
        await b.until("!!document.querySelector('#os-q')")
        await asyncio.sleep(.2)
        assert (await b.js(FOCUS))["id"] == "os-q", "a mouse-opened menu is no longer ready to type into"

    asyncio.run(desktop.with_browser("online", "", check))
