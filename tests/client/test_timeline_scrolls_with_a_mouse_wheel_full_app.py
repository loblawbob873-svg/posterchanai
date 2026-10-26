"""The Social timeline scrolls with a MOUSE WHEEL -- in a browser tab, in the desktop app's windowed desktop, and in
the Social window there.

2026-10-26: "social timeline scrolling is now broken on desktop" -- the Windows app: the wheel did nothing over the
Social window while the arrow keys still scrolled it. Nothing tested the most basic thing a timeline does with the
most common input device, so this drives REAL wheel input (CDP Input.dispatchMouseEvent mouseWheel, delivered by the
browser exactly as a mouse's) over the visible timeline and requires it to move -- and to keep moving on a second
burst, since a scroll that is snapped back a frame later is the same bug.
"""
import asyncio
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop


@pytest.fixture(scope='module', autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


# 120 notes on the relay so the timeline is long enough to scroll.
SEED = r'''
window.__events = window.__events || [];
(() => { const now = Math.floor(Date.now() / 1000);
  for (let i = 0; i < 120; i++) window.__events.push({ id: (i.toString(16).padStart(4, '0')).repeat(16), kind: 1,
    pubkey: '7'.repeat(64), created_at: now - i * 60, sig: 'a'.repeat(128), tags: [],
    content: 'Scroll test note ' + i + '\n\nA second paragraph so each card has some height to it.' }); })();
'''

SCROLLER = r"""(sel => { const root = sel ? document.querySelector(sel) : document;
  const feed = root && root.querySelector('#feed') || document.getElementById('feed');
  if (!feed) return null; const r = feed.getBoundingClientRect();
  return { x: r.left + r.width / 2, y: r.top + Math.min(r.height / 2, 300), top: feed.scrollTop,
           max: feed.scrollHeight - feed.clientHeight, cards: feed.querySelectorAll('.note, article').length }; })"""


async def wheel(b, x, y, notches):
    for _ in range(notches):
        await b.call('Input.dispatchMouseEvent', {'type': 'mouseWheel', 'x': x, 'y': y, 'deltaX': 0, 'deltaY': 120})
        await asyncio.sleep(.08)
    await asyncio.sleep(1.2)                        # let the smooth-scroll animation finish -- and any snap-back


async def scrolls(b, sel):
    st = await b.js(SCROLLER + '(%s)' % ('null' if sel is None else repr(sel)))
    assert st and st['cards'] >= 20 and st['max'] > 2000, ("the timeline is not long enough to prove anything", st)
    await wheel(b, st['x'], st['y'], 5)
    one = await b.js(SCROLLER + '(%s)' % ('null' if sel is None else repr(sel)))
    await wheel(b, st['x'], st['y'], 5)
    two = await b.js(SCROLLER + '(%s)' % ('null' if sel is None else repr(sel)))
    return st['top'], one['top'], two['top']


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
@pytest.mark.parametrize('desk', [False, True])
def test_the_social_timeline_scrolls_with_a_mouse_wheel(desk):
    async def check(b):
        await desktop.login(b)
        if desk:
            await b.until("!!document.querySelector('#os-start')")
            await b.js("document.querySelector('#os-start').click()")
            await b.until("!!document.querySelector('#os-q')")
            await b.js("(()=>{const q=document.querySelector('#os-q');q.value='Social';q.dispatchEvent(new Event('input',{bubbles:true}))})()")
            await b.until("!!document.querySelector('#os-startmenu [data-view=global]')")
            await b.js("document.querySelector('#os-startmenu [data-view=global]').click()")
            await b.until("!!document.querySelector('.osw #feed') && document.querySelectorAll('.osw #feed .note').length>=20")
            sel = '.osw'
        else:
            await b.js("__PC.switchView('global')")
            await b.until("document.querySelectorAll('#feed .note').length>=20")
            sel = None
        start, one, two = await scrolls(b, sel)
        assert one > start + 200, ("five notches of the mouse wheel did not move the timeline", start, one)
        assert two > one + 200, ("the timeline moved once and then stuck (or was snapped back)", start, one, two)
        assert not await b.js("__errors.slice()")
    extra = SEED + ("" if desk else "localStorage.setItem('pc_nostr_settings',JSON.stringify({...JSON.parse(localStorage.getItem('pc_nostr_settings')||'{}'),osMode:false}));")
    asyncio.run(desktop.with_browser('online', '', check, extra))
