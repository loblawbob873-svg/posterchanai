"""A REPLY FROM SOMEONE WHOSE CLOCK IS BEHIND STILL LANDS BELOW WHAT IT ANSWERS -- AND STAYS THERE AFTER A RELOAD.

"its happening again to me" / "you can see the conversation replies are out of order" (2026-10-30, Windows app,
screenshot): we sent "thats armada" at 10:29; the peer's "I know." and "The thing is, that border around Armada…" --
typed after reading it -- were stamped 10:28-10:29 by the PEER's clock, a minute behind ours, so a thread sorted by
timestamp put the answers above the question. The 10-29 fix (_dmSendAt) only floors OUR sends. Shipped bundle, the
Windows app's desktop mode, Messages opened beside other windows, a real Enter, and a live NIP-17 reply whose sender's
clock is 90 s BEHIND ours.
"""
import asyncio
import json
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop
from tests.client.test_dm_newest_first_full_app import RELAY
from tests.client.test_dm_send_scrolls_to_bottom_full_app import SEED
from tests.client.test_dm_enter_after_send_keeps_the_conversation_full_app import _enter
from tests.client.test_dm_send_beside_other_windows_full_app import LIVE, INCOMING


@pytest.fixture(scope='module', autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


ORDER = ("[...document.querySelectorAll('#dm-msgs .bubble')].map(x=>x.textContent.includes('thats armada')?'mine'"
         ":x.textContent.includes('I know')?'reply':x.textContent.includes('The thing is')?'reply2':'').filter(Boolean)")


async def open_thread(b, pk):
    await b.js("PCOS.routeView('messages');true")
    row = "#dm-rows .dm-peer[data-peer=" + json.dumps(pk) + "]"
    await b.until("!!document.querySelector(" + json.dumps(row) + ")")
    await b.js("document.querySelector(" + json.dumps(row) + ").click()")
    await b.until("!!document.querySelector('#dm-in') && document.querySelectorAll('#dm-msgs .bubble').length>=3")


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
def test_a_reply_stamped_by_a_slow_clock_lands_below_the_message_it_answers():
    got = {}

    async def check(b):
        await b.call('Emulation.setDeviceMetricsOverride', {'width': 1600, 'height': 900, 'deviceScaleFactor': 1, 'mobile': False})
        await desktop.login(b)
        pk = await b.js(SEED + '(5)')
        await b.js("PCOS.enter();true")
        await b.until("document.body.classList.contains('os-on')")
        await b.js("(()=>{const st=document.createElement('style');st.textContent='.os-buddy{display:none!important}';document.head.appendChild(st);return true})()")
        await open_thread(b, pk)
        await asyncio.sleep(1)
        await b.js("document.querySelector('#dm-in').focus()")
        await b.call('Input.insertText', {'text': 'thats armada'})
        await _enter(b)
        await b.until("[...document.querySelectorAll('#dm-msgs .bubble')].some(x=>x.textContent.includes('thats armada'))")
        await asyncio.sleep(3)
        # the peer reads it and answers -- twice -- from a computer whose clock is 90 s behind ours
        got['delivered'] = await b.js(INCOMING + "('I know.', -90)")
        await asyncio.sleep(1.5)
        await b.js(INCOMING + "('The thing is, that border around Armada comes from the AppImage.', -80)")
        await b.until("[...document.querySelectorAll('#dm-msgs .bubble')].some(x=>x.textContent.includes('The thing is'))")
        await asyncio.sleep(1)
        got['live'] = await b.js(ORDER)
        await asyncio.sleep(2)                                   # the placement is written down
        await b.call('Page.reload', {})
        await b.until("!!window.PCOS && !!window.__PC && !!__PC.me && !!__PC.me()")
        await b.js("PCOS.enter();true")
        await b.until("document.body.classList.contains('os-on')")
        await open_thread(b, pk)
        await b.until("[...document.querySelectorAll('#dm-msgs .bubble')].some(x=>x.textContent.includes('The thing is'))")
        await asyncio.sleep(1)
        got['reloaded'] = await b.js(ORDER)

    asyncio.run(desktop.with_browser('online', '', check, RELAY + LIVE))
    assert got['delivered'] >= 1, ('the stub relay delivered nothing -- the test is not testing', got)
    assert got['live'] == ['mine', 'reply', 'reply2'], ('the answers went above the question', got)
    assert got['reloaded'] == ['mine', 'reply', 'reply2'], ('the order changed after a reload', got)
