"""Settings → Phone → "Open Android's Default apps" (#ps-defaults), in the SHIPPED bundle against a stubbed
HomeScreen plugin (2026-10-22: every button gets a test).

Android refuses a role it will not give by finishing the request with no dialog at all, so a switch that asked and
got nothing would look unwired. The card then SAYS the role was not handed over and shows this button -- the route
that always works. Pressed, it opens Android's own Default apps screen (HomeScreen.openDefaultApps); on a phone
without that screen it says so instead of doing nothing.
"""
import asyncio
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop
from tests.client.test_hide_system_bars_full_app import _phone_pane

NATIVE = r'''
window.__homeCalls=[];window.__noDefaultsScreen=false;
window.Capacitor={isNativePlatform:()=>true,getPlatform:()=> 'android',Plugins:{HomeScreen:{
 consumeLaunchView:async()=>({view:''}), addListener:()=>({remove(){}}), formFactor:async()=>({form:'phone'}),
 systemBars:async()=>({hidden:false}),
 status:async()=>({isDefaultHome:false,isDefaultSms:false,isDefaultDialer:false,smsCapable:true,dialerCapable:true,batteryExempt:false}),
 requestSms:async()=>{__homeCalls.push('requestSms');return {};},   // Android's silent refusal: nothing changes
 openDefaultApps:async()=>{__homeCalls.push('openDefaultApps');if(__noDefaultsScreen)throw new Error('no activity');return {};},
}}};'''


@pytest.fixture(scope="module", autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
def test_a_refused_role_offers_default_apps_and_the_button_opens_it():
    async def check(b):
        await _phone_pane(b)
        await b.until("!!document.querySelector('#ps-sms')")
        assert await b.js("document.querySelector('#ps-defaults').hidden") is True, "the way out shown before it is needed"
        await b.js("(()=>{const s=document.querySelector('#ps-sms');s.checked=true;s.dispatchEvent(new Event('change'))})(); 1")
        await b.until("__homeCalls.includes('requestSms') && document.querySelector('#ps-defaults').hidden===false")
        assert 'did not hand over the messages app role' in await b.js("document.querySelector('#ps-msg').textContent")
        assert await b.js("document.querySelector('#ps-sms').checked") is False, "the switch claims a role the phone refused"
        await b.js("document.querySelector('#ps-defaults').click(); 1")
        await b.until("__homeCalls.filter(c=>c==='openDefaultApps').length===1")
        # A phone with no such screen: said, not swallowed.
        await b.js("window.__noDefaultsScreen=true; document.querySelector('#ps-defaults').click(); 1")
        await b.until("/this phone has no Default apps screen/.test(document.querySelector('#ps-msg').textContent)")
        assert not await b.js("__errors"), await b.js("__errors")
    asyncio.run(desktop.with_browser("online", "", check, NATIVE))
