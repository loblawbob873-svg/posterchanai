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


@pytest.mark.parametrize('share', [False, True])
@pytest.mark.parametrize('size', [(360, 640), (390, 850)])
def test_phone_panel_scrolls_by_touch_and_fits_the_width(size, share):
    """Reported as "can't scroll, basically useless". `#feed.feed-remote` is overflow:hidden, so the
    panel has to be the scroller; with `height:auto` it grew to its content and nothing could move.
    MEASURED at 360x640 before the fix: #feed 574px, .pcrd 894px, last control at y=948, a scroll
    gesture moved nothing. Driven with a TOUCH gesture, which is what an Android WebView gets."""
    width, height = size
    async def check(browser):
        await browser.call('Emulation.setDeviceMetricsOverride', dict(width=width,height=height,deviceScaleFactor=1,mobile=True))
        await browser.call('Emulation.setTouchEmulationEnabled', dict(enabled=True, maxTouchPoints=5))
        await browser.until("document.body.classList.contains('guest')")
        await desktop.login(browser)
        # A phone WebView has no capture API (viewer only); a tablet browser may have one (share form shown).
        value = 'async()=>{throw Error("fixture")}' if share else 'undefined'
        await browser.js("Object.defineProperty(navigator.mediaDevices,'getDisplayMedia',{value:"+value+",configurable:true})")
        await click(browser, '#btn-more-m')
        await click(browser, '.more-item[data-v="__remote"]')
        await browser.until("!!document.querySelector('[data-rd-viewer-ready]')")
        await asyncio.sleep(.3)
        state = await browser.js('''(()=>{const p=document.querySelector('.pcrd'),f=document.querySelector('#feed').getBoundingClientRect();
          const wide=[...document.querySelectorAll('#feed *')].filter(e=>e.getClientRects().length&&e.getBoundingClientRect().right>innerWidth+.5).map(e=>String(e.className));
          return {overflow:p.scrollHeight-p.clientHeight,feedBottom:f.bottom,wide,pageWidth:document.scrollingElement.scrollWidth}})()''')
        assert not state['wide'], state
        assert state['pageWidth'] <= width, state
        last = "[...document.querySelectorAll('.pcrd > *')].filter(e=>e.getClientRects().length).at(-1)"
        if state['overflow'] <= 0:
            # Everything already fits; the last control must be on screen, not under the fold.
            assert await browser.js(last+".getBoundingClientRect().bottom") <= state['feedBottom'] + .5, state
            return
        # Raw touch events: headless Chrome's synthesizeScrollGesture ignores `touch` and moves nothing.
        for _ in range(3):
            x, y = width//2, int(height*.8)
            await browser.call('Input.dispatchTouchEvent', dict(type='touchStart', touchPoints=[dict(x=x, y=y)]))
            for step in range(1, 16):
                await browser.call('Input.dispatchTouchEvent', dict(type='touchMove', touchPoints=[dict(x=x, y=y-step*25)]))
            await browser.call('Input.dispatchTouchEvent', dict(type='touchEnd', touchPoints=[]))
            await asyncio.sleep(.2)
        top = await browser.js("document.querySelector('.pcrd').scrollTop")
        assert top >= state['overflow'] - 1, (top, state)
        assert await browser.js(last+".getBoundingClientRect().bottom") <= state['feedBottom'] + .5, state
    init="localStorage.setItem('pc_nostr_settings',JSON.stringify({...JSON.parse(localStorage.getItem('pc_nostr_settings')||'{}'),osMode:false}));"
    asyncio.run(desktop.with_browser('online','',check,extra_init=init))
