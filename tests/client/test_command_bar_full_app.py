"""THE COMMAND BAR: Ctrl+Space (web desktop) / Super+Space (PosterChanOS) finds apps, files, notes and actions and runs them.

Asked 2026-10-28 ("Do spotlight-style command bar"). Real key events (CDP Input.dispatchKeyEvent -- they go where focus
actually is), the shipped bundle in desktop mode, and what a person sees: the bar opens with the caret in it, typing
finds things, Enter runs the highlighted one, Esc closes, Ctrl+K typed INTO a text field is left to that field, "Ask AI"
puts the question in AI chat without sending it, the compositor's Super+Space opens it, and on a phone it is a sheet
that fits the screen.
"""
import asyncio
import base64
import os
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop
from tests.client.test_super_closes_an_open_start_menu_full_app import COMPOSITOR, _tick

CHROME = pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
OPEN = "!!document.querySelector('.pc-cmdbar')"
FOCUSED = "document.activeElement && document.activeElement.classList.contains('cmd-q')"


@pytest.fixture(scope='module', autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


async def _key(b, key, code, vk, ctrl=False):
    for kind in ('rawKeyDown', 'keyUp'):
        await b.call('Input.dispatchKeyEvent', {'type': kind, 'key': key, 'code': code, 'windowsVirtualKeyCode': vk,
                                                'nativeVirtualKeyCode': vk, 'modifiers': 2 if ctrl else 0})


async def _shot(b, name):
    out = os.environ.get('PC_SHOTS')
    if out:
        Path(out).mkdir(parents=True, exist_ok=True)
        Path(out, name).write_bytes(base64.b64decode((await b.call('Page.captureScreenshot', {'format': 'png'}))['data']))


async def _desk(b, width=1440):
    await b.call('Emulation.setDeviceMetricsOverride', {'width': width, 'height': 900, 'deviceScaleFactor': 1, 'mobile': False})
    await desktop.login(b)
    await b.until("document.body.classList.contains('os-on')")
    await asyncio.sleep(.5)


@CHROME
def test_ctrl_space_opens_it_typing_finds_settings_and_enter_opens_them():
    got = {}

    async def check(b):
        await _desk(b)
        await _key(b, ' ', 'Space', 32, ctrl=True)
        await b.until(OPEN)
        await b.until(FOCUSED)
        got['actions_on_open'] = await b.js("[...document.querySelectorAll('.cmd-sec[data-source=actions] .cmd-row b')].map(x=>x.textContent)")
        await b.call('Input.insertText', {'text': 'settings'})
        await b.until("[...document.querySelectorAll('.cmd-row b')].some(x=>x.textContent==='Settings') && "
                      "![...document.querySelectorAll('.cmd-row b')].some(x=>x.textContent==='New post')")   # the filter applied
        await _shot(b, 'cmdbar-1440.png')
        got['font'] = await b.js("getComputedStyle(document.querySelector('.cmd-row b')).fontFamily")
        got['first'] = await b.js("document.querySelector('.cmd-row.on b').textContent")
        await _key(b, 'Enter', 'Enter', 13)
        await b.until("!" + OPEN)
        await b.until("[...document.querySelectorAll('#os-root .osw .osw-title')].some(t=>/Settings/.test(t.textContent))")
        # Esc closes without running anything.
        await _key(b, ' ', 'Space', 32, ctrl=True)
        await b.until(OPEN)
        await _key(b, 'Escape', 'Escape', 27)
        got['after_esc'] = await b.js(OPEN)

    asyncio.run(desktop.with_browser('online', '', check))
    assert 'New post' in got['actions_on_open'] and 'Take a screenshot' in got['actions_on_open'], got
    assert got['font'].startswith('Inter'), ("the bar fell back to the browser's serif font", got['font'])
    assert got['first'] == 'Settings', got
    assert got['after_esc'] is False, "Esc left the bar open"


@CHROME
def test_ctrl_k_in_a_text_field_belongs_to_the_field():
    got = {}

    async def check(b):
        await _desk(b)
        await b.js("(()=>{const i=document.createElement('input');i.id='__field';document.body.appendChild(i);i.focus();return true})()")
        await _key(b, 'k', 'KeyK', 75, ctrl=True)
        await asyncio.sleep(.6)
        got['in_field'] = await b.js(OPEN)
        await b.js("document.getElementById('__field').blur();document.getElementById('__field').remove();true")
        await _key(b, 'k', 'KeyK', 75, ctrl=True)
        await b.until(OPEN)
        got['outside'] = True

    asyncio.run(desktop.with_browser('online', '', check))
    assert got['in_field'] is False, "Ctrl+K typed into a text box opened the command bar"
    assert got['outside']


@CHROME
def test_ask_ai_puts_the_question_in_ai_chat_and_does_not_send_it():
    got = {}

    async def check(b):
        await _desk(b)
        await b.js("PCOS.cmdbar()")
        await b.until(OPEN)
        await b.until(FOCUSED)
        await b.call('Input.insertText', {'text': 'what is a microkernel'})
        await b.until("[...document.querySelectorAll('.cmd-sec[data-source=more] .cmd-row b')].length===3")
        got['more'] = await b.js("[...document.querySelectorAll('.cmd-sec[data-source=more] .cmd-row b')].map(x=>x.textContent)")
        # Move the highlight to the Ask AI row with the arrow keys, the way a person would.
        for _ in range(12):
            if await b.js("/^Ask AI/.test((document.querySelector('.cmd-row.on b')||{}).textContent||'')"):
                break
            await _key(b, 'ArrowDown', 'ArrowDown', 40)
        await _key(b, 'Enter', 'Enter', 13)
        await b.until("!!document.getElementById('ai-input') && document.getElementById('ai-input').value==='what is a microkernel'")
        got['sent'] = await b.js("(window.__chatRequests||[]).length")

    asyncio.run(desktop.with_browser('online', '', check))
    assert got['more'] == ['Search Nostr for “what is a microkernel”', 'Do it: “what is a microkernel”',
                           'Ask AI: “what is a microkernel”'], got['more']
    assert got['sent'] == 0, "the question was sent without the person pressing send"


@CHROME
def test_super_space_from_the_compositor_opens_it():
    async def check(b):
        await desktop.login(b)
        await b.until("document.body.classList.contains('os-on') && __wm.listeners.length > 0")
        await _tick(b, 'pc:cmdbar')
        await b.until(OPEN)
        await b.until(FOCUSED)
    asyncio.run(desktop.with_browser('online', '', check, extra_init=COMPOSITOR))


@CHROME
def test_on_a_phone_it_is_a_sheet_that_fits_the_screen():
    got = {}

    async def check(b):
        await b.call('Emulation.setDeviceMetricsOverride', {'width': 390, 'height': 844, 'deviceScaleFactor': 2, 'mobile': True})
        await desktop.login(b)
        await b.until("!!window.PCOS")
        await b.js("PCOS.cmdbar()")
        await b.until(OPEN)
        await b.call('Input.insertText', {'text': 'set'})
        await b.until("document.querySelectorAll('.cmd-row').length>0")
        await asyncio.sleep(.3)
        await _shot(b, 'cmdbar-390.png')
        got['titles'] = await b.js("[...document.querySelectorAll('.cmd-row b')].map(x=>x.textContent)")
        got['fit'] = await b.js("""(()=>{const p=document.querySelector('.cmd-panel').getBoundingClientRect();
          const rows=[...document.querySelectorAll('.cmd-row')].map(r=>r.getBoundingClientRect());
          return {left:p.left,right:p.right,vw:innerWidth,rowsInside:rows.every(r=>r.right<=innerWidth+1&&r.left>=-1),
                  tap:Math.min(...rows.map(r=>r.height))}})()""")

    asyncio.run(desktop.with_browser('online', '', check))
    f = got['fit']
    assert f['left'] <= 1 and f['right'] >= f['vw'] - 1, ("not a full-width sheet on a phone", f)
    assert f['rowsInside'], ("a row runs off the screen", f)
    assert f['tap'] >= 40, ("rows too small to tap", f)
    assert got['titles'].count('Settings') == 1, ("the same thing is listed twice", got['titles'])
