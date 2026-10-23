"""Android: the "Hide system bars" switch ('#set-hide-bars') ON must not freeze User Settings.

The sibling bug was the desktop Tor switch (test_settings_remaining_buttons_full_app.py): a checkbox painted by
code (checked set, defaultChecked not) is taken by renderUserSettings for somebody's unsaved edit, and every later
redraw is refused -- "Unlink Telegram" unlinked and the card went on saying "Linked". The hide-bars switch is
painted the same way from the phone's answer (HomeScreen.systemBars), so it is pressed here with the bars hidden,
then Telegram is unlinked and the card must say so. The switch itself is pressed too: it asks the phone and shows
what the phone answered.
"""
import asyncio
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop
from tests.client.test_settings_account_full_app import open_tab_holding
from tests.client.test_settings_remaining_buttons_full_app import FIXTURE, SERVER, TOASTS, calls_to, confirm

CHROME = pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')


@pytest.fixture(scope='module', autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


# The Android app (the bars choice lives on the phone; `__bars.hidden` is what it starts with), and a linked Telegram.
ANDROID_BARS = r"""
window.__bars={hidden:BARS,calls:[]};
window.__srv={settings:{telegram_chat_id:4242}};
window.Capacitor={isNativePlatform:()=>true,getPlatform:()=>'android',Plugins:{
  HomeScreen:{systemBars:async()=>({hidden:__bars.hidden}),
    setSystemBarsHidden:async o=>{__bars.calls.push(o.hidden);__bars.hidden=!!o.hidden;return {hidden:__bars.hidden};}}}};
"""


def android(hidden):
    return FIXTURE + SERVER + ANDROID_BARS.replace('BARS', 'true' if hidden else 'false')


async def open_settings(b):
    await b.call("Emulation.setDeviceMetricsOverride", {"width": 412, "height": 900, "deviceScaleFactor": 2, "mobile": True})
    await desktop.login(b)
    await b.js("try{ if(window.PCOS && PCOS.isOn()) PCOS.exit(); }catch(_){} __PC.switchView('settings'); 1")
    await b.until("!!document.querySelector('#set-hide-bars')")


async def unlink_telegram_and_see_it(b):
    """Unlink Telegram: the server is told, and the card must REDRAW to say it is no longer linked."""
    await b.until("!!document.querySelector('#us-tg-unlink')")
    await open_tab_holding(b, '#us-tg-unlink')
    assert await b.js("/Linked \\(chat 4242\\)/.test(document.querySelector('#us-tg-status').textContent)")
    await b.js("document.querySelector('#us-tg-unlink').click(); 1")
    await confirm(b, 'Unlink Telegram?')
    await b.until(f"{calls_to('/api/telegram/unlink')}.length===1 && /unlinked/.test({TOASTS})")
    await b.until("!!document.querySelector('#us-tg-status') && /Not linked/.test(document.querySelector('#us-tg-status').textContent) && !document.querySelector('#us-tg-unlink')")


@CHROME
def test_hide_bars_painted_on_by_the_phone_does_not_freeze_user_settings():
    async def check(b):
        await open_settings(b)
        # The switch shows what the PHONE says: bars hidden.
        await b.until("document.querySelector('#set-hide-bars').checked")
        await unlink_telegram_and_see_it(b)
        # Pressing it asks the phone to SHOW the bars; the switch shows the phone's answer.
        await open_tab_holding(b, '#set-hide-bars')
        await b.js("document.querySelector('#set-hide-bars').click(); 1")
        await b.until(f"__bars.calls.length===1 && /system bars shown/.test({TOASTS}) && !document.querySelector('#set-hide-bars').disabled")
        assert await b.js("__bars.calls") == [False]
        assert await b.js("document.querySelector('#set-hide-bars').checked") is False
        assert not await b.js("__errors"), await b.js("__errors")
    asyncio.run(desktop.with_browser('online', '', check, android(True)))


@CHROME
def test_hide_bars_pressed_on_does_not_freeze_user_settings():
    """The press takes effect on the phone at once, so the switch's new state is not an unsaved edit either."""
    async def check(b):
        await open_settings(b)
        await open_tab_holding(b, '#set-hide-bars')
        assert await b.js("document.querySelector('#set-hide-bars').checked") is False
        await b.js("document.querySelector('#set-hide-bars').click(); 1")
        await b.until(f"__bars.calls.length===1 && /system bars hidden/.test({TOASTS}) && !document.querySelector('#set-hide-bars').disabled")
        assert await b.js("__bars.calls") == [True] and await b.js("__bars.hidden") is True
        assert await b.js("document.querySelector('#set-hide-bars').checked") is True
        await unlink_telegram_and_see_it(b)
        assert not await b.js("__errors"), await b.js("__errors")
    asyncio.run(desktop.with_browser('online', '', check, android(False)))
