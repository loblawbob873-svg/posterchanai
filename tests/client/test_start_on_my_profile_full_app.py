"""'SCREEN THE APP OPENS ON' OFFERS YOUR OWN PROFILE, AND THE APP THEN OPENS THERE.

"Can you add ability to show your profile on Android under 'Screen the app opens on'" (2026-10-30). The choices came
from the sidebar, and your profile is not a sidebar row, so it was never offered. The shipped client, phone and desktop
width: choose "My profile" in Settings (the real select), reload, and the app lands on the signed-in account's profile.
"""
import asyncio
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop


@pytest.fixture(scope='module', autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


CLASSIC = ("localStorage.setItem('pc_nostr_settings',JSON.stringify({...JSON.parse("
           "localStorage.getItem('pc_nostr_settings')||'{}'),osMode:false}));")


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
@pytest.mark.parametrize('width', [390, 1280])
def test_my_profile_can_be_the_screen_the_app_opens_on(width):
    got = {}

    async def check(b):
        await b.call('Emulation.setDeviceMetricsOverride', {'width': width, 'height': 860, 'deviceScaleFactor': 1, 'mobile': width < 600})
        await desktop.login(b)
        await b.js("__PC.switchView('settings'); true")
        await b.until("!!document.querySelector('#set-start-view')")
        got['offered'] = await b.js("[...document.querySelector('#set-start-view').options].map(o=>[o.value,o.text])")
        await b.js("(()=>{const s=document.querySelector('#set-start-view');s.value='profile';s.dispatchEvent(new Event('change',{bubbles:true}));return true})()")
        await asyncio.sleep(.5)
        me = await b.js("__PC.me().pubkey")
        await b.call('Page.reload', {})
        await b.until("!!window.__PC && !!__PC.me && !!__PC.me()")
        await b.until("!!document.querySelector('.prof-tabs')")
        await asyncio.sleep(1)
        got['landed'] = await b.js("({view:(window.__PC.view&&__PC.view())||'', copy:!!document.querySelector('#copy-npub'), mine:!!document.querySelector('#edit-prof')})")
        got['me'] = me

    asyncio.run(desktop.with_browser('online', '', check, CLASSIC))
    assert ['profile', 'My profile'] in got['offered'], ('your profile is not offered', got['offered'])
    assert got['landed']['mine'] and got['landed']['copy'], ('the app did not open on your own profile', got)
