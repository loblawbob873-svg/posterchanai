"""A startup app that is ON can be switched OFF with a real click -- on a machine with two monitors.

"I still can't uncheck enabled startup apps" (2026-10-10, after a fix that stamped the saved list). Every earlier
test set `checked` and dispatched `change` itself, so none ever clicked. The row was a <label> holding BOTH the
monitor <select> and the switch, and a click inside a <label> activates the label's FIRST control: with two
monitors, an app that is on draws its monitor picker before its switch, so clicking the switch opened the picker
and the checkbox never changed. An app that was off has no picker, so switching ON always worked.
"""
import asyncio
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop
from tests.client.test_startup_apps_settings_full_app import PC_FIXTURE

FIXTURE = PC_FIXTURE + r'''
window.pcDisplays={status:async()=>[
  {name:'DP-1',make:'Dell',model:'U2720Q',active:true,primary:true,rect:{x:0,y:0,width:2560,height:1440}},
  {name:'HDMI-A-1',make:'LG',model:'27GL850',active:true,primary:false,rect:{x:2560,y:0,width:2560,height:1440}}]};
'''


@pytest.fixture(scope='module', autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


async def _real_click(b, expr):
    rect = await b.js(f"(()=>{{const e={expr};e.scrollIntoView({{block:'center'}});const r=e.getBoundingClientRect();"
                      "return {x:r.x+r.width/2,y:r.y+r.height/2}})()")
    for kind in ('mousePressed', 'mouseReleased'):
        await b.call('Input.dispatchMouseEvent', dict(type=kind, button='left', clickCount=1, **rect))
    await asyncio.sleep(.3)


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
@pytest.mark.parametrize('width', [1280, 390])
def test_an_enabled_startup_app_unchecks_with_a_real_click_on_two_monitors(width):
    got = {}

    async def check(b):
        await b.call('Emulation.setDeviceMetricsOverride', dict(width=width, height=900, deviceScaleFactor=1, mobile=width < 600))
        await desktop.login(b)
        await b.until("!!document.body && document.body.classList.contains('os-on') && !!document.querySelector('#os-bar')")
        await b.js("document.getElementById('osfr')?.remove();document.documentElement.classList.remove('osfr-on')")
        await b.js("ClientSettings.set('startupApps',['notes']);ClientSettings.set('startupAppsAt',Date.now());true")
        await b.js("PCOS.openSystemSettings()")
        await b.until("!!document.querySelector('.os-set-nav [data-page=\"startup\"]')")
        await b.js("document.querySelector('.os-set-nav [data-page=\"startup\"]').click()")
        page = "document.querySelector('[data-settings-page=\"startup\"]:not([hidden])')"
        sw = page + ".querySelector('[data-startup-view=\"notes\"]')"
        await b.until(f"!!{page} && !!{sw} && !!{page}.querySelector('[data-startup-monitor=\"notes\"]')")
        got['before'] = await b.js(f"{sw}.checked")
        await _real_click(b, sw + ".nextElementSibling")            # the visible slider, as a person clicks it
        got['after'] = await b.js(f"{page}.querySelector('[data-startup-view=\"notes\"]').checked")
        got['saved'] = await b.js("ClientSettings.get('startupApps',[])")
        await _real_click(b, page + ".querySelector('[data-startup-view=\"notes\"]').nextElementSibling")
        got['again'] = await b.js(f"{page}.querySelector('[data-startup-view=\"notes\"]').checked")
        got['errors'] = await b.js('__errors')

    asyncio.run(desktop.with_browser('online', '', check, FIXTURE))
    assert got['before'] is True, got
    assert got['after'] is False and 'notes' not in got['saved'], ("an ON startup app could not be switched off", got)
    assert got['again'] is True, ("switching it back on failed", got)
    assert not got['errors'], got['errors']
