"""Notification recipients reach the real Texts renderer, including cold native windows."""
import asyncio
from pathlib import Path
import pytest
from tests.client import test_desktop_offline_full_app as desktop


@pytest.fixture(scope='module', autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
@pytest.mark.parametrize('native_window', [False, True])
def test_notification_opens_recipient_in_actual_texts_renderer(native_window):
    async def check(browser):
        await desktop.login(browser)
        if native_window:
            # Native children inherit an already signed-in desktop profile.
            url=await browser.js("location.origin+'/index.html?pcwin=texts&pcsms=%2B15550100'")
            await browser.call('Page.navigate', {'url':url})
            await browser.until("!!window.PCSms && PCSms._state().open===PCSms._key('+15550100')")
            assert await browser.js("new URL(location.href).searchParams.has('pcsms')") is False
        await browser.js("window.__notificationDocument=document;PCOpenNotificationRoute('texts:%2B15550200')")
        await asyncio.sleep(1)
        await browser.until("!!window.PCSms && PCSms._state().open===PCSms._key('+15550200') && !!document.querySelector('.sms-wrap')")
        assert await browser.js("__PC.VIEW") == 'texts'
        assert await browser.js("document===__notificationDocument")
        assert await browser.js("document.querySelector('.sms-wrap').textContent.includes('+15550200')")
    extra = "localStorage.setItem('pc_nostr_settings',JSON.stringify({...JSON.parse(localStorage.getItem('pc_nostr_settings')||'{}'),osMode:false}));"
    if native_window:
        extra += "window.pcShell.windowContext={role:'app',view:'texts'};window.pcShell.backgroundOwner=false;"
    asyncio.run(desktop.with_browser('online', '?pcwin=texts&pcsms=%2B15550100' if native_window else '', check, extra))
