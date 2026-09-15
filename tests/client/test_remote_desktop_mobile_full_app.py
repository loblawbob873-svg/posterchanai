"""Phone users can explicitly arm the shipped receive-only desktop viewer."""
import asyncio
import pytest
from tests.client import test_desktop_offline_full_app as desktop
from tests.client.test_cord_direct_invites_full_app import click

@pytest.fixture(scope='module', autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()

@pytest.mark.parametrize('width', [390, 768])
def test_phone_more_opens_viewer_without_capture_and_leaving_disarms(width):
    async def check(browser):
        await browser.call('Emulation.setDeviceMetricsOverride', dict(width=width,height=850,deviceScaleFactor=1,mobile=True))
        await browser.until("document.body.classList.contains('guest')")
        await desktop.login(browser)
        await browser.js('''window.__rdArms=[];window.__rdHost=null;
const arm=__PC.setRemoteDesktopArmed,host=__PC.setRemoteDesktopHost;
__PC.setRemoteDesktopArmed=value=>{__rdArms.push(value);return arm(value);};
__PC.setRemoteDesktopHost=value=>{__rdHost=value;return host(value);};
Object.defineProperty(navigator.mediaDevices,'getDisplayMedia',{value:undefined,configurable:true});
navigator.mediaDevices.getUserMedia=()=>{throw Error('viewer requested local media');};''')
        await click(browser, '#btn-more-m')
        await click(browser, '.more-item[data-v="__remote"]')
        await browser.until("!!document.querySelector('[data-rd-viewer-ready]')")
        assert await browser.js("__rdArms.join(',')==='true' && __rdHost.isConnected")
        assert await browser.js("document.querySelector('.pcrd-hero b').textContent==='View a desktop'")
        assert await browser.js("document.querySelector('[data-rd-share]').getClientRects().length===0")
        assert await browser.js("document.querySelector('.pcrd').scrollWidth<=document.querySelector('.pcrd').clientWidth")
        assert 'other signed-in device' in await browser.js("document.querySelector('[data-rd-viewer-ready]').textContent")
        await browser.js("document.dispatchEvent(new CustomEvent('pc:remote-desktop-window'))")
        assert not await browser.js("document.body.classList.contains('os-mode')")
        await browser.js("__PC.switchView('global')")
        assert await browser.js("__rdArms.at(-1)===false && __rdHost===null")
    init="localStorage.setItem('pc_nostr_settings',JSON.stringify({...JSON.parse(localStorage.getItem('pc_nostr_settings')||'{}'),osMode:false}));"
    asyncio.run(desktop.with_browser('online','',check,extra_init=init))
