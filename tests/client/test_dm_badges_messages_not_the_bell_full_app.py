"""A DM LIGHTS MESSAGES, NEVER THE NOTIFICATION BELL (operator decision, 2026-10-11).

Unread DMs used to be summed into the bell ("i see no notification bell when DM's come in"), so one
message lit two badges, and the desktop's taskbar bell rang for something Notifications only pointed
away from. Now: an incoming DM raises the Messages badge and leaves the bell where it was; a mention
or reply still raises the bell. DM toasts and OS/push notifications are untouched -- only the bell's
COUNT changed. Real NIP-17 gift wraps through the in-page relay (test_dm_newest_first_full_app).
"""
import asyncio
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop
from tests.client.test_dm_newest_first_full_app import RELAY, SEED as DM_SEED
from tests.client.test_read_state_follows_the_account_full_app import SEED, until


@pytest.fixture(scope='module', autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


BELL = ("[...document.querySelectorAll('#notif-badge,#notif-badge-m,#rb-notif-badge')]"
        ".filter(b=>!b.classList.contains('hidden')&&b.getClientRects().length).map(b=>b.textContent)")
MSGS = ("[...document.querySelectorAll('#dm-badge,#dm-badge-m')]"
        ".filter(b=>!b.classList.contains('hidden')&&b.getClientRects().length).map(b=>b.textContent)")


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
@pytest.mark.parametrize('width', [1280, 390])
def test_unread_dms_badge_messages_and_the_bell_counts_only_the_mention(width):
    async def check(b):
        await b.call('Emulation.setDeviceMetricsOverride', dict(width=width, height=900, deviceScaleFactor=1, mobile=width < 600))
        await b.until("document.body.classList.contains('guest')")
        await b.js(SEED + "(120,2,1)")                       # two DMs and one mention, just now
        await desktop.login(b)
        await b.js("try{ if(window.PCOS && PCOS.isOn()) PCOS.exit(); }catch(_){}")
        await until(b, "__PC.dmStats && __PC.dmStats().done>=2 && __PC.notifItems(30).length>=1")
        await until(b, MSGS + ".some(t=>t==='2')")
        await until(b, BELL + ".length>0")
        await asyncio.sleep(1)                                # let every count settle
        assert set(await b.js(BELL)) == {'1'}, ('the bell counts the mention only', await b.js(BELL))
        assert await b.js('__PC.notifUnread()') == 1, await b.js('__PC.notifUnread()')
        assert '2' in await b.js(MSGS), await b.js(MSGS)
        # Opening Notifications clears the bell and leaves Messages counting.
        await b.js("__PC.switchView('notifications')")
        await until(b, BELL + ".length===0")
        assert '2' in await b.js(MSGS), ('Notifications must not read the DMs', await b.js(MSGS))
        assert not await b.js('__errors'), await b.js('__errors')
    asyncio.run(desktop.with_browser('online', '', check, RELAY))


OS_BELL = "(()=>{const d=document.querySelector('#os-bell .os-dot');return d?d.textContent:''})()"
DM_BADGE = "(()=>{const b=document.querySelector('#dm-badge');return b&&!b.classList.contains('hidden')?b.textContent:''})()"


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
def test_the_desktop_taskbar_bell_stays_dark_for_dms():
    """The PosterChanOS taskbar draws its own bell from the same count (PC.notifUnread). DMs arrive
    AFTER sign-in, in desktop mode; Messages counts them, the taskbar bell does not."""
    async def check(b):
        await b.call('Emulation.setDeviceMetricsOverride', dict(width=1440, height=900, deviceScaleFactor=1, mobile=False))
        await b.until("document.body.classList.contains('guest')")
        await desktop.login(b)
        await b.js('PCOS.enter()')
        await b.until("!!document.querySelector('#os-bell')")
        assert await b.js(OS_BELL) == '', 'the taskbar bell was lit before anything arrived'
        await b.js(DM_SEED + "('new',2)")                    # two DMs arrive while the desktop is up
        await until(b, "__PC.dmStats && __PC.dmStats().done>=2")
        await until(b, DM_BADGE + "==='2'")
        await asyncio.sleep(1.5)
        assert await b.js(OS_BELL) == '', ('a DM lit the taskbar bell', await b.js(OS_BELL))
        assert await b.js('__PC.notifUnread()') == 0, await b.js('__PC.notifUnread()')
        assert not await b.js('__errors'), await b.js('__errors')
    asyncio.run(desktop.with_browser('online', '', check, RELAY))


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
def test_reading_dms_on_one_monitor_clears_messages_on_the_other():
    """Each monitor is its own page; reading messages on one moves the shared `dmSeen`, and the OTHER
    page hears it as a `storage` event -- replayed here exactly as the browser delivers it."""
    async def check(b):
        await b.call('Emulation.setDeviceMetricsOverride', dict(width=1440, height=900, deviceScaleFactor=1, mobile=False))
        await b.until("document.body.classList.contains('guest')")
        await b.js(DM_SEED + "('new',2)")
        await desktop.login(b)
        await b.js('PCOS.enter()')
        await until(b, "__PC.dmStats().done>=2")
        await until(b, DM_BADGE + "==='2'")
        await b.js("""(()=>{const k='pc_nostr_settings',old=localStorage.getItem(k)||'{}',o=JSON.parse(old);
          o.dmSeen=Math.floor(Date.now()/1000)+60;const nv=JSON.stringify(o);localStorage.setItem(k,nv);
          window.dispatchEvent(new StorageEvent('storage',{key:k,oldValue:old,newValue:nv,storageArea:localStorage}));})()""")
        await until(b, DM_BADGE + "===''")
        assert await b.js(OS_BELL) == '', await b.js(OS_BELL)
    asyncio.run(desktop.with_browser('online', '', check, RELAY))
