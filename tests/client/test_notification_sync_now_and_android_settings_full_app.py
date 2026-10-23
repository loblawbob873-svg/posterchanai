"""Settings → Notifications: the two buttons no test pressed (2026-10-22: every button gets a test).

  * '#us-notification-sync' "Sync now": a change made while the relay REFUSED it stays on this device ("Saved on
    this device. Waiting to sync."); pressing Sync now once the relay accepts publishes the account's prefs document
    (kind 30078, d=pcai:client-prefs) carrying that change, and the status line then says "Saved.". Pressed while
    the relay still refuses, it says nothing false: the change stays waiting.
  * '#us-notification-android' "Open Android app settings": only in the APK (window.Capacitor); it calls the
    PosterChanPush plugin's openBatterySettings, and on a build without that plugin it says where to go instead.
Real login, real settings pane and blossom.js; the relay socket and the Capacitor bridge are fixtures.
"""
import asyncio
import json
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop


@pytest.fixture(scope='module', autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


PLAIN = r'''
localStorage.setItem('pc_nostr_settings',JSON.stringify({...JSON.parse(localStorage.getItem('pc_nostr_settings')||'{}'),osMode:false}));
'''


async def open_notifications(b):
    await b.js("__PC.switchView('settings')")
    await b.until("!!document.querySelector('.us-tab[data-tab=notifications]')")
    await b.js("document.querySelector('.us-tab[data-tab=notifications]').click()")
    await b.until("document.querySelector('[data-pane=notifications]')?.classList.contains('active')")


def toast_has(text):
    return "[...document.querySelectorAll('#toast-root .toast')].some(t=>t.textContent===" + json.dumps(text) + ")"


PREFS_EMAIL_OFF = ("__published.filter(e=>e.kind===30078&&e.tags.some(t=>t[0]==='d'&&t[1]==='pcai:client-prefs')"
                   "&&JSON.parse(e.content).notificationPrefs?.email===false)")
STATUS = "document.querySelector('#us-notification-sync-state').textContent"


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
def test_sync_now_publishes_a_change_the_relay_refused_earlier():
    async def check(b):
        await desktop.login(b)
        await open_notifications(b)
        # The relay refuses (the fixture's default): the change is kept here and said to be waiting.
        await b.js("window.__publishOK=false; document.querySelector('[data-notification-type=email]').click(); 1")
        await b.until(PREFS_EMAIL_OFF + ".length>=1")
        await b.until(STATUS + "==='Saved on this device. Waiting to sync.'")
        # Sync now while it still refuses: tried again, and still honestly waiting.
        tried = await b.js(PREFS_EMAIL_OFF + ".length")
        await b.js("document.querySelector('#us-notification-sync').click(); 1")
        await b.until(PREFS_EMAIL_OFF + ".length>" + str(tried))
        await asyncio.sleep(.5)
        assert await b.js(STATUS) == 'Saved on this device. Waiting to sync.'
        # Sync now once the relay accepts: the change is published and the line says so.
        await b.js("window.__publishOK=true; window.__mark=__published.length; document.querySelector('#us-notification-sync').click(); 1")
        await b.until(STATUS + "==='Saved.'")
        sent = await b.js(PREFS_EMAIL_OFF + ".filter(e=>__published.indexOf(e)>=__mark)")
        assert len(sent) == 1, sent
        assert sent[0]["pubkey"] == await b.js("__PC.me().pubkey")
        assert await b.js("!__PC.notificationAllowed('email')")
        assert await b.js("Object.keys(JSON.parse(localStorage.getItem('pc_notification_prefs:'+__PC.me().pubkey)).dirty||{}).length") == 0
        assert not await b.js("__errors.length"), await b.js("__errors")
    asyncio.run(desktop.with_browser('online', '', check, PLAIN))


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
def test_open_android_app_settings_calls_the_native_plugin_or_says_where_to_go():
    async def check(b):
        await desktop.login(b)
        # Not the APK: no such button.
        await open_notifications(b)
        assert not await b.js("!!document.querySelector('#us-notification-android')")
        # The APK, with the plugin.
        await b.js("window.__opened=0; window.Capacitor={Plugins:{PosterChanPush:{openBatterySettings:async()=>{__opened++;}}}}; 1")
        await b.js("__PC.switchView('global')")
        await open_notifications(b)
        await b.until("!!document.querySelector('#us-notification-android')")
        await b.js("document.querySelector('#us-notification-android').click(); 1")
        await b.until("__opened===1")
        # An APK build without the plugin: told where to go, nothing thrown.
        await b.js("window.Capacitor={Plugins:{Other:{}}}; 1")
        await b.js("document.querySelector('#us-notification-android').click(); 1")
        await b.until(toast_has("Open Android Settings → Apps → PosterChan → Notifications."))
        assert await b.js("__opened") == 1
        # A plugin that throws: same sentence.
        await b.js("window.Capacitor={Plugins:{PosterChanPush:{openBatterySettings:async()=>{throw new Error('no activity')}}}}; "
                   "document.querySelectorAll('#toast-root .toast').forEach(t=>t.remove()); 1")
        await b.js("document.querySelector('#us-notification-android').click(); 1")
        await b.until(toast_has("Open Android Settings → Apps → PosterChan → Notifications."))
    asyncio.run(desktop.with_browser('online', '', check, PLAIN))
