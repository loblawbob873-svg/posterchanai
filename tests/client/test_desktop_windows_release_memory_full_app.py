"""Closing a desktop-mode window gives its memory back -- open/close it all day and the page does not grow.

"android tablet keeps restarting in the middle of using it, desktop mode" (2026-10-15). In the APK every desktop
window is drawn inside ONE WebView, so whatever a closed window leaves behind accumulates in the one renderer
Android kills when memory runs out (MainActivity.surviveRenderProcessDeath then reloads the app -- the restart).
Measured in the shipped bundle at tablet size, with all windows closed, after a forced GC, per open->close:
  * the timeline kept +382 DOM nodes and +103 listeners: menus.js's emoji autocomplete -- attached to the inline
    composer on EVERY render -- added a capture keydown on the document and a resize on the window that held the
    textarea (and the card tree around it) for ever;
  * the Terminal kept +64 nodes: the WebGL renderer's `document.fonts` listener held the addon and the terminal.
Both now leave nothing. This drives the real desk: double-click the icon, close with the window's own X.
"""
import asyncio
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop


@pytest.fixture(scope='module', autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


OPEN = """(v=>{const e=document.querySelector('#os-desk [data-view="'+v+'"]');if(!e)return false;
  e.dispatchEvent(new MouseEvent('dblclick',{bubbles:true}));e.click();return true;})(%r)"""
CLOSE_ALL = "(()=>{const xs=[...document.querySelectorAll('.osw .osw-x')];xs.forEach(x=>x.click());return xs.length})()"
ROUNDS = 5


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
@pytest.mark.parametrize('view', ['global', 'terminal'])
def test_a_closed_window_leaves_nothing_behind(view):
    got = {'nodes': []}

    async def check(b):
        await b.call('Emulation.setDeviceMetricsOverride', {'width': 1280, 'height': 800, 'deviceScaleFactor': 1, 'mobile': False})
        await desktop.login(b)
        await b.until("document.body.classList.contains('os-on') && !!document.querySelector('#os-desk')")
        await b.js("document.getElementById('osfr')?.remove();document.documentElement.classList.remove('osfr-on')")
        await b.call('Performance.enable')
        assert await b.js(OPEN % view), 'no %s icon on the desk' % view
        await asyncio.sleep(1.0)
        await b.js(CLOSE_ALL)
        for _ in range(ROUNDS):
            await b.js(OPEN % view)
            await asyncio.sleep(0.8)
            got['opened'] = await b.js("document.querySelectorAll('.osw').length")
            closed = await b.js(CLOSE_ALL)
            await asyncio.sleep(0.8)
            await b.call('HeapProfiler.collectGarbage')
            m = {x['name']: x['value'] for x in (await b.call('Performance.getMetrics'))['metrics']}
            got['nodes'].append(int(m['Nodes']))
            assert closed >= 1, 'the window never opened'
    asyncio.run(desktop.with_browser('online', '', check, ''))

    n = got['nodes']
    # The first round may still load a module; from then on a closed window must cost nothing. The leak was
    # +382 (timeline) / +64 (terminal) per round, i.e. +1528 / +256 across the four measured rounds.
    assert n[-1] - n[1] <= 24, ('every open/close of %s keeps DOM alive: %r' % (view, n))


SEED_EMOJI = ("localStorage.setItem('pc_emoji_index',JSON.stringify({at:Math.floor(Date.now()/1000),base:'https://fixture.invalid/emoji',"
              "emojis:[{s:'party',p:'a',f:'party.png'},{s:'parrot',p:'a',f:'parrot.png'}]}));")

TA = "[...document.querySelectorAll('textarea')].find(t=>t.closest('[id^=\"tl-cmp\"]')||/^tl-cmp/.test(t.id))"


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
def test_the_emoji_autocomplete_still_takes_the_keys_while_open_and_lets_go_when_closed():
    """The fix moved the autocomplete's document/window listeners to live only while its list is open. The
    behaviour must be unchanged: with the list open, ArrowDown and Enter pick a shortcode (and Enter does not
    reach the composer); with it closed, no listener of it is left on the document."""
    got = {}

    async def check(b):
        await desktop.login(b)
        await b.until("document.body.classList.contains('os-on') && !!document.querySelector('#os-desk')")
        await b.js("document.getElementById('osfr')?.remove();document.documentElement.classList.remove('osfr-on')")
        await b.js(OPEN % 'global')
        await b.until("!!(" + TA + ")")
        o = (await b.call('Runtime.evaluate', {'expression': 'document'}))['result']['objectId']

        async def doc_keydowns():
            ls = (await b.call('DOMDebugger.getEventListeners', {'objectId': o}))['listeners']
            return sum(1 for l in ls if l['type'] == 'keydown' and l.get('useCapture'))
        got['before'] = await doc_keydowns()
        await b.js("(()=>{const ta=" + TA + ";ta.focus();ta.value=':par';ta.selectionStart=ta.selectionEnd=4;"
                   "ta.dispatchEvent(new Event('input',{bubbles:true}));})()")
        await b.until("!!document.querySelector('.em-ac')")
        got['open'] = await doc_keydowns()
        got['rows'] = await b.js("document.querySelectorAll('.em-ac [data-i], .em-ac .em-ac-row, .em-ac > *').length")
        got['enter_seen_by_composer'] = await b.js(
            "(()=>{const ta=" + TA + ";window.__enter=0;ta.addEventListener('keydown',e=>{if(e.key==='Enter')__enter++;});"
            "ta.dispatchEvent(new KeyboardEvent('keydown',{key:'ArrowDown',bubbles:true,cancelable:true}));"
            "ta.dispatchEvent(new KeyboardEvent('keydown',{key:'Enter',bubbles:true,cancelable:true}));return __enter;})()")
        got['value'] = await b.js(TA + ".value")
        got['list_gone'] = await b.js("!document.querySelector('.em-ac')")
        got['after'] = await doc_keydowns()
    asyncio.run(desktop.with_browser('online', '', check, SEED_EMOJI))

    assert got['open'] == got['before'] + 1, ('the open list did not take the keyboard', got)
    assert got['value'].startswith(':parrot: ') or got['value'].startswith(':party: '), ('no shortcode was picked', got)
    assert got['enter_seen_by_composer'] == 0, ('Enter reached the composer while the list was open', got)
    assert got['list_gone'] and got['after'] == got['before'], ('the closed list left its listener on the document', got)
