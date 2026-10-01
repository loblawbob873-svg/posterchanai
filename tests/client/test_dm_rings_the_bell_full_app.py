"""An incoming DM lights the notification bell ("i see no notification bell when DM's come in").

The bell counted only the Notifications list, and DMs are not in it -- an arriving message showed a toast
and an OS notification and left the bell dark. Now the bell counts unread DMs, and Notifications shows
them as one row ("2 unread messages") that opens Messages, so the bell never counts something the screen
behind it cannot show. Real NIP-17 gift wraps through the in-page relay (test_dm_newest_first_full_app).
"""
import asyncio
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop
from tests.client.test_dm_newest_first_full_app import RELAY, SEED


@pytest.fixture(scope='module', autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


BELL = ("[...document.querySelectorAll('#notif-badge,#notif-badge-m,#rb-notif-badge')]"
        ".filter(b=>!b.classList.contains('hidden')&&b.getClientRects().length).map(b=>b.textContent)")


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
@pytest.mark.parametrize('width', [1280, 390])
def test_unread_dms_light_the_bell_and_notifications_leads_to_them(width):
    async def check(b):
        await b.call('Emulation.setDeviceMetricsOverride', dict(width=width, height=900, deviceScaleFactor=1, mobile=width < 600))
        await b.until("document.body.classList.contains('guest')")
        await b.js(SEED + "('new',2)")                       # two DMs from somebody else, just now
        await desktop.login(b)
        await b.js("try{ if(window.PCOS && PCOS.isOn()) PCOS.exit(); }catch(_){}")
        await b.until("__PC.dmStats && __PC.dmStats().done>=2")
        await b.until(BELL + ".some(t=>Number(t)>=2)")
        await b.js("__PC.switchView('notifications')")
        await b.until("!!document.querySelector('#feed .dm-notif')")
        row = await b.js("document.querySelector('#feed .dm-notif').innerText")
        assert '2 unread messages' in row, row
        await b.js("document.querySelector('#feed .dm-notif').click()")
        await b.until("document.querySelectorAll('#feed .dm-row, #feed [data-peer], #feed .dm-list, #feed .dm-convo').length>0 || /Messages/.test((document.querySelector('.topbar')||{}).textContent||'')")
        # Reading them clears the row and takes them off the bell.
        await asyncio.sleep(.5)
        await b.js("__PC.switchView('notifications')")
        await asyncio.sleep(.5)
        assert not await b.js("!!document.querySelector('#feed .dm-notif')"), 'the DM row outlived reading the messages'
        assert not any(int(t or 0) >= 2 for t in await b.js(BELL)), await b.js(BELL)
        assert not await b.js('__errors'), await b.js('__errors')
    asyncio.run(desktop.with_browser('online', '', check, RELAY))
