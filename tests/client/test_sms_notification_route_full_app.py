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
            await browser.until("!!document.querySelector('.sms-wrap') && document.querySelector('.sms-wrap').textContent.includes('+15550100')")
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


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
def test_a_route_delivered_to_the_open_texts_window_is_handled_there_once():
    """The laptop, 2026-10-26: "texts is stealing focus from any other window, keeps loading, can't type anywhere else".
    A Texts notification's route reached the open Texts window, which handed it BACK to the desktop -- and the desktop,
    seeing that window open, broadcast it to the window again. ~190 broadcasts a second, each ending in window.focus().
    Here the window's desktop is one that does exactly what the real one does with a texts route; one route is
    delivered, and it must be performed here, once."""
    async def check(browser):
        await desktop.login(browser)
        url = await browser.js("location.origin+'/index.html?pcwin=texts&pcsms=%2B15550100'")
        await browser.call('Page.navigate', {'url': url})
        await browser.until("!!window.PCSms && !!window.PCOSWin && PCOSWin.isWindow()")
        got = await browser.js(r"""(async () => {
          let focuses = 0, routed = 0; const wf = window.focus; window.focus = function(){ focuses++; return wf.apply(this, arguments); };
          // The desktop: a texts route for a window that is already open is broadcast to that window (os.js -> routeExisting).
          PCOSWin.desktop = () => ({ __PC: { openNotificationRoute(r){ routed++;
            const m = /^texts:(.*)$/.exec(String(r)); if(!m) return false;
            const ch = new BroadcastChannel('pc-os-window-route-v1'); ch.postMessage({ view: 'texts', arg: decodeURIComponent(m[1]) }); ch.close();
            return true; } } });
          const ch = new BroadcastChannel('pc-os-window-route-v1'); ch.postMessage({ view: 'texts', arg: '+15550300' }); ch.close();
          await new Promise(r => setTimeout(r, 1500));
          window.focus = wf;
          return { focuses, routed, open: PCSms._state().open === PCSms._key('+15550300') };
        })()""")
        assert got['routed'] == 0, ("the Texts window handed a route it was given back to the desktop -- a loop", got)
        assert got['focuses'] <= 2, ("the Texts window kept taking focus", got)
        assert got['open'], ("the delivered route did not open its conversation here", got)
    extra = ("localStorage.setItem('pc_nostr_settings',JSON.stringify({...JSON.parse(localStorage.getItem('pc_nostr_settings')||'{}'),osMode:false}));"
             "window.pcShell.windowContext={role:'app',view:'texts'};window.pcShell.backgroundOwner=false;")
    asyncio.run(desktop.with_browser('online', '?pcwin=texts&pcsms=%2B15550100', check, extra))
