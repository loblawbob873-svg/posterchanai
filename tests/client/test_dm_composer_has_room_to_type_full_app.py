"""On a phone, the DM message box leaves room to type.

Reported on the APK: "so now message field in dms is so small. no space to type message". The composer
was ONE row: 📎, 📁, GIF, ✨, the box, the send-status label (up to 120px) and Send. Every one of those
is a fixed-width control, so each addition (✨ was the last) came straight out of the box, and on a
360-412px phone it ended up ~100px wide -- a dozen characters. The row never OVERFLOWED, which is all
test_chat_assist_full_app checked, so it passed while the box shrank.

Now on a phone the tools sit on their own line above, and the box shares its line only with Send.
"""
import asyncio
import json
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop
from tests.client.test_chat_assist_full_app import PLAIN, ASSIST
from tests.client.test_dm_newest_first_full_app import RELAY
from tests.client.test_dm_send_scrolls_to_bottom_full_app import SEED as DM_SEED


@pytest.fixture(scope="module", autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


GEOM = r"""(()=>{const q=s=>{const e=document.querySelector(s);if(!e||e.hidden)return null;const r=e.getBoundingClientRect();
  return r.width?{l:r.left,r:r.right,t:r.top,b:r.bottom,w:r.width}:null};
  return {vw:innerWidth, inp:q('#dm-in'), send:q('#dm-send'), ai:q('#dm-ai'), gif:q('#dm-gif'), attach:q('#dm-attach'),
          state:q('#dm-sendstate'), compose:q('.dm-compose'),
          replybar:Math.round(document.getElementById('dm-replybar').getBoundingClientRect().height)}})()"""


async def _open_thread(b, w, h):
    await b.call('Emulation.setDeviceMetricsOverride', {'width': w, 'height': h, 'deviceScaleFactor': 1, 'mobile': True})
    await desktop.login(b)
    await b.js("__PC.CFG && (__PC.CFG.gif_enabled=true)")
    pk = await b.js(DM_SEED + '(4)')
    await b.js("__PC.switchView('messages')")
    sel = "#dm-rows .dm-peer[data-peer=" + json.dumps(pk) + "]"
    await b.until(f"!!document.querySelector('{sel}')")
    await b.js(f"document.querySelector('{sel}').click()")
    await b.until("!!document.querySelector('#dm-in')")
    await asyncio.sleep(.5)


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
@pytest.mark.parametrize('w,h', [(360, 740), (412, 915)])
def test_the_message_box_is_most_of_the_width_on_a_phone(w, h):
    got = {}

    async def check(b):
        await _open_thread(b, w, h)
        got['idle'] = await b.js(GEOM)
        # The send-status label is what takes the most when it speaks.
        await b.js("(()=>{const s=document.getElementById('dm-sendstate');s.textContent='Sending to 3 relays…';})()")
        await asyncio.sleep(.2)
        got['busy'] = await b.js(GEOM)

    asyncio.run(desktop.with_browser('online', '', check, PLAIN + RELAY + ASSIST))
    for k in ('idle', 'busy'):
        g = got[k]
        assert g['inp'] and g['send'], g
        assert g['gif'] and g['ai'] and g['attach'], ('a tool went missing', k, g)
        assert g['replybar'] == 0, ('an empty reply banner takes room above the composer', k, g['replybar'])
        assert g['inp']['w'] >= 0.6 * g['vw'], ('the message box is too narrow to type in', k, g['inp']['w'], g['vw'])
        assert g['send']['t'] < g['inp']['b'] and g['send']['b'] > g['inp']['t'], ('Send is not beside the box', k, g)
        for name in ('inp', 'send', 'ai', 'gif', 'attach'):
            assert g[name]['l'] >= -0.5 and g[name]['r'] <= g['vw'] + 0.5, ('overflows the screen', k, name, g[name])


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
def test_the_desktop_composer_stays_one_row():
    got = {}

    async def check(b):
        await b.call('Emulation.setDeviceMetricsOverride', {'width': 1280, 'height': 900, 'deviceScaleFactor': 1, 'mobile': False})
        await desktop.login(b)
        await b.js("__PC.CFG && (__PC.CFG.gif_enabled=true)")
        pk = await b.js(DM_SEED + '(4)')
        await b.js("__PC.switchView('messages')")
        sel = "#dm-rows .dm-peer[data-peer=" + json.dumps(pk) + "]"
        await b.until(f"!!document.querySelector('{sel}')")
        await b.js(f"document.querySelector('{sel}').click()")
        await b.until("!!document.querySelector('#dm-in')")
        await asyncio.sleep(.5)
        got.update(await b.js(GEOM))

    asyncio.run(desktop.with_browser('online', '', check, PLAIN + RELAY + ASSIST))
    assert abs(got['attach']['t'] - got['send']['t']) < 12 or abs(got['attach']['b'] - got['send']['b']) < 12, got


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
def test_the_reply_banner_still_shows_when_replying():
    """The other half of `.dm-replybar[hidden]{display:none}`: Reply must still open it, above the box."""
    got = {}

    async def check(b):
        await _open_thread(b, 390, 844)
        await b.js("(()=>{const m=document.querySelector('#dm-msgs .bubble');m.dispatchEvent(new MouseEvent('contextmenu',{bubbles:true,cancelable:true,clientX:60,clientY:200}));})()")
        await b.until("[...document.querySelectorAll('button,[role=menuitem],.menu-item,li')].some(x=>x.textContent.includes('Reply')&&x.getBoundingClientRect().width>0)")
        await b.js("[...document.querySelectorAll('button,[role=menuitem],.menu-item,li')].filter(x=>x.textContent.includes('Reply')&&x.getBoundingClientRect().width>0).pop().click()")
        await asyncio.sleep(.3)
        got.update(await b.js(r"""(()=>{const r=document.getElementById('dm-replybar'),i=document.getElementById('dm-in');
          const a=r.getBoundingClientRect(),c=i.getBoundingClientRect();
          return {h:a.height,hidden:r.hidden,text:r.textContent.trim(),above:a.bottom<=c.top+1}})()"""))
        await b.js("document.getElementById('dm-rb-x').click()")
        await asyncio.sleep(.2)
        got['after_x'] = await b.js("document.getElementById('dm-replybar').getBoundingClientRect().height")

    asyncio.run(desktop.with_browser('online', '', check, PLAIN + RELAY + ASSIST))
    assert not got['hidden'] and got['h'] > 20 and 'Replying' in got['text'] and got['above'], got
    assert got['after_x'] == 0, got
