"""System Settings → Startup apps: add, switch off, run and remove, through the bridge.

'Users need a way to be able to define startup programs for posterchanOS'. The shipped bundle with
only the autostart/app bridges stood in (their real half is tests/test_desktop_autostart.py). The
page must list what is on disk, add an installed app and a typed command, pass a switch to the
bridge, ask before removing, and fit its window.
"""
import asyncio
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop


@pytest.fixture(scope='module', autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


FIXTURE = r'''
window.__calls=[];window.__rows=[{id:'discord.desktop',name:'Discord',exec:'discord --start-minimized',enabled:false}];
window.pcWM={windows:async()=>[],onEvent:()=>()=>{},focus:async()=>true,launch:async()=>({pid:1}),shellFront:async()=>true};
window.pcApps={list:async()=>({apps:[{id:'firefox',name:'Firefox',argv:['firefox']}]})};
window.pcAutostart={
  list:async()=>__rows.map(r=>({...r})),
  add:async(s)=>{__calls.push(['add',s]);__rows.push({id:'sync.desktop',name:s.name||'syncthing',exec:s.exec,enabled:true});return 'sync.desktop'},
  addApp:async(id)=>{__calls.push(['addApp',id]);__rows.push({id:'firefox.desktop',name:'Firefox',exec:'firefox %u',enabled:true});return 'firefox.desktop'},
  set:async(id,on)=>{__calls.push(['set',id,on]);__rows.find(r=>r.id===id).enabled=on;return {id,enabled:on}},
  remove:async(id)=>{__calls.push(['remove',id]);__rows=__rows.filter(r=>r.id!==id);return {id}},
  run:async(id)=>{__calls.push(['run',id]);return {ok:true}},
};
'''


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
def test_startup_apps_can_be_added_switched_run_and_removed():
    async def check(b):
        await desktop.login(b)
        await b.until("!!document.body && document.body.classList.contains('os-on') && !!document.querySelector('#os-bar')")
        await b.js("document.getElementById('osfr')?.remove();document.documentElement.classList.remove('osfr-on')")
        await b.js("PCOS.openSystemSettings()")
        await b.until("!!document.querySelector('.os-set-nav [data-page=\"startup\"]')")
        await b.js("document.querySelector('.os-set-nav [data-page=\"startup\"]').click()")
        page = "document.querySelector('[data-settings-page=\"startup\"]:not([hidden])')"
        await b.until(f"!!{page} && !!{page}.querySelector('.os-startup-row')")
        assert 'Discord' in await b.js(f"{page}.querySelector('[data-startup-list]').textContent")
        assert await b.js(f"{page}.querySelector('[data-startup-on]').checked") is False, "an off entry drew as on"

        await b.js(f"(()=>{{const c={page}.querySelector('[data-startup-on]');c.checked=true;c.dispatchEvent(new Event('change'))}})()")
        await b.until("__calls.some(c=>c[0]==='set')")
        assert await b.js("__calls.find(c=>c[0]==='set')") == ['set', 'discord.desktop', True]

        await b.until(f"{page}.querySelectorAll('[data-startup-app] option').length>1")
        await b.js(f"(()=>{{const s={page}.querySelector('[data-startup-app]');s.value='firefox';{page}.querySelector('[data-startup-add-app]').click()}})()")
        await b.until(f"{page}.querySelectorAll('.os-startup-row').length===2")

        await b.js(f"(()=>{{{page}.querySelector('[data-startup-exec]').value='syncthing --no-browser';{page}.querySelector('[data-startup-add-cmd]').click()}})()")
        await b.until(f"{page}.querySelectorAll('.os-startup-row').length===3")
        assert await b.js("__calls.find(c=>c[0]==='add')[1].exec") == 'syncthing --no-browser'

        await b.js(f"{page}.querySelector('[data-startup-run]').click()")
        await b.until("__calls.some(c=>c[0]==='run')")

        # Remove asks first; a "no" removes nothing.
        await b.js("__PC.uiConfirm=async()=>false")
        await b.js(f"{page}.querySelector('[data-startup-remove]').click()")
        await asyncio.sleep(.3)
        assert not await b.js("__calls.some(c=>c[0]==='remove')")
        await b.js("__PC.uiConfirm=async()=>true")
        await b.js(f"{page}.querySelector('[data-startup-remove]').click()")
        await b.until(f"{page}.querySelectorAll('.os-startup-row').length===2")

        over = await b.js(f"(()=>{{const c={page}.querySelector('[data-startup]');return c.scrollWidth-c.clientWidth}})()")
        assert over <= 1, f"the startup card overflows its window by {over}px"
        assert not await b.js('__errors'), await b.js('__errors')

    asyncio.run(desktop.with_browser('online', '', check, FIXTURE))
