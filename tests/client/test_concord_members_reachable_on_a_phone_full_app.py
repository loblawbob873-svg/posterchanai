"""CONCORD ON A PHONE: THE ROOM'S MEMBERS ARE ONE TAP AWAY.

"no way to see concord room members on android". Members was the last of a dozen header buttons, and
at phone width the header ran out of screen before it. The real client and the real Concord screen,
inside a room, at phone widths: every header control that is shown must be fully on screen, Members
must be one of them, and tapping it must open the member list. The room actions that made room for it
are behind ⋯ and still work from there. At desktop width ⋯ does not appear.
"""
import asyncio
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop
from tests.client.test_concord_link_sparkle_full_app import EXTRA, ROOM


@pytest.fixture(scope='module', autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


HEADER = r"""(()=>{const h=document.querySelector('.cc-conversation>header');const out=[];
  for(const b of h.querySelectorAll('button')){const r=b.getBoundingClientRect();
    if(!r.width||getComputedStyle(b).display==='none')continue;
    out.push([b.id||b.className,Math.round(r.left),Math.round(r.right),r.right<=innerWidth+1&&r.left>=-1]);}
  return out;})()"""


async def _room(b, width):
    await b.call("Emulation.setDeviceMetricsOverride", {"width": width, "height": 800, "deviceScaleFactor": 2, "mobile": width < 600})
    await desktop.login(b)
    await b.js("window.__publishOK=true")
    await b.js(ROOM + "(true)")
    await b.until("!!document.querySelector('#cc-input')")
    await b.js("""(()=>{const app=document.querySelector('.cc-app');if(app&&!app.classList.contains('show-chat')){const ch=document.querySelector('.cc-channel');if(ch)ch.click();}})()""")
    await b.until("[...document.querySelectorAll('.cc-message')].some(m=>m.textContent.includes('just words'))")


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
@pytest.mark.parametrize('width', [360, 412])
def test_members_is_on_screen_and_opens_the_list_on_a_phone(width):
    got = {}

    async def check(b):
        await _room(b, width)
        got['header'] = await b.js(HEADER)
        await b.js("document.querySelector('#cc-members').click()")
        await asyncio.sleep(.3)
        got['dialog'] = await b.js("(()=>{const d=document.querySelector('#cc-members-dialog');return !!d&&!d.classList.contains('hidden')&&d.getBoundingClientRect().height>0})()")
        await b.js("document.querySelector('#cc-members-close').click()")
        # The folded actions are still reachable through ⋯.
        await b.js("document.querySelector('#cc-head-more').click()")
        await asyncio.sleep(.3)
        got['menu'] = await b.js("[...document.querySelectorAll('.menu-pop button')].map(x=>x.textContent.trim())")

    asyncio.run(desktop.with_browser("online", "", check, EXTRA))
    shown = {h[0]: h for h in got['header']}
    assert 'cc-members' in shown, ("Members is not in the header at all", got['header'])
    off = [h for h in got['header'] if not h[3]]
    assert not off, ("header controls off screen", off)
    assert got['dialog'], "tapping Members did not open the member list"
    assert any('Invite' in t for t in got['menu']) and any('Leave' in t for t in got['menu']), got['menu']


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
def test_desktop_width_keeps_every_action_and_no_more_button():
    got = {}

    async def check(b):
        await _room(b, 1280)
        got['header'] = await b.js(HEADER)

    asyncio.run(desktop.with_browser("online", "", check, EXTRA))
    ids = [h[0] for h in got['header']]
    assert 'cc-head-more' not in ids and 'cc-members' in ids and 'cc-direct-send' in ids, ids
