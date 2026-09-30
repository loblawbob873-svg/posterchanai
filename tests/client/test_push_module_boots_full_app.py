"""The real bundled client boots with push.js split out of app.js, and Settings wires the push switch.

Drives the SHIPPED desktop bundle in headless Chrome, through the same harness as the offline desktop
test. push.js holds Web Push + "stay connected" (pushState, _wirePushToggle, _wireStayConnected,
_pushPlugin …). What a broken split looks like, none of it logged:

  * push.js missing or loaded AFTER app.js -> `_pushPlugin()` (blossom.js calls it inline, expecting an
    answer, not a promise) gets a pending load back, and the lazy loader fetches the file a second time;
  * a dependency not passed or a live binding captured by value (`S.ME` read once at build) -> the
    Settings switch keeps the label it was DRAWN with ("Enable push notifications") whatever the real
    state is, because _wirePushToggle threw before render() ran.
"""
import asyncio
from pathlib import Path

import pytest
from tests.client import test_desktop_offline_full_app as desktop


@pytest.fixture(scope='module', autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


BUILD_PROBE = r'''
localStorage.setItem('pc_nostr_settings',JSON.stringify({...JSON.parse(localStorage.getItem('pc_nostr_settings')||'{}'),osMode:false}));
window.__consoleErrors=[];
{const ce=console.error.bind(console);console.error=(...a)=>{__consoleErrors.push(a.map(x=>String(x&&x.stack||x)).join(' ').slice(0,400));ce(...a);};}
addEventListener('unhandledrejection',e=>__consoleErrors.push('unhandled: '+String(e.reason&&e.reason.stack||e.reason).slice(0,400)));
{let real;Object.defineProperty(window,'PCPushFactory',{configurable:true,get(){return real;},set(f){
  real=function(dep){ window.__puBuilds=(window.__puBuilds||0)+1; window.__puStack=String(new Error().stack);
    return f.apply(this,arguments); };}});}
'''


async def _boot_and_open_settings(b):
    order = await b.js("[...document.scripts].map(s=>(s.getAttribute('src')||'').split('?')[0].split('/').pop())"
                       ".filter(n=>n==='push.js'||n==='app.js')")
    assert order == ['push.js', 'app.js'], order
    await desktop.login(b)
    await b.js("__PC.switchView('settings')")
    await b.until("!!document.querySelector('#set-push-toggle')")
    # render() rewrites the drawn label with the MEASURED state; the drawn one has no bell glyph.
    await b.until("/^(🔔|🔕) /.test(document.querySelector('#set-push-toggle').textContent.trim())")
    label = await b.js("document.querySelector('#set-push-toggle').textContent.trim()")
    assert label in ('🔔 Enable push notifications', '🔕 Turn off push notifications',
                     '🔔 Not supported on this browser', '🔔 Blocked in browser settings'), label
    assert await b.js("window.__puBuilds") == 1
    assert '_lzRun' in await b.js("window.__puStack") or '_pushMod' in await b.js("window.__puStack")
    fetched = await b.js("performance.getEntriesByType('resource').filter(e=>/\\/push\\.js(\\?|$)/.test(e.name)).length")
    assert fetched == 1, ('push.js was fetched again by the lazy loader', fetched)
    assert not await b.js('__errors'), await b.js('__errors')
    assert not await b.js('__consoleErrors'), await b.js('__consoleErrors')


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
def test_the_bundled_client_boots_with_push_js_and_settings_wires_the_switch():
    asyncio.run(desktop.with_browser('online', '', _boot_and_open_settings, extra_init=BUILD_PROBE))
