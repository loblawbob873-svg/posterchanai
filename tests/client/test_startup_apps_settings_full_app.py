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


PC_FIXTURE = FIXTURE + r'''
window.__startupSaves=[];window.__opened=[];
'''


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
def test_posterchan_apps_can_be_chosen_to_open_at_login_and_follow_the_account():
    """'System Settings -> Startup Apps, no posterchan apps listed? wtf': PosterChan's own apps are offered,
    saved in the synced preferences, and the desktop opens them -- once -- at login."""
    got = {}

    async def check(b):
        await desktop.login(b)
        await b.until("!!document.body && document.body.classList.contains('os-on') && !!document.querySelector('#os-bar')")
        await b.js("document.getElementById('osfr')?.remove();document.documentElement.classList.remove('osfr-on')")
        await b.js("(()=>{const pc=window.__PC,s=pc.saveStartupApps;pc.saveStartupApps=v=>{__startupSaves.push(v.slice());return s&&s(v)}})()")
        await b.js("PCOS.openSystemSettings()")
        await b.until("!!document.querySelector('.os-set-nav [data-page=\"startup\"]')")
        await b.js("document.querySelector('.os-set-nav [data-page=\"startup\"]').click()")
        page = "document.querySelector('[data-settings-page=\"startup\"]:not([hidden])')"
        await b.until(f"!!{page} && {page}.querySelectorAll('[data-startup-view]').length>3")
        got['offered'] = await b.js(f"[...{page}.querySelectorAll('[data-startup-view]')].map(x=>x.dataset.startupView)")
        for v in ('messages', 'notes'):
            await b.js(f"(()=>{{const c={page}.querySelector('[data-startup-view=\"{v}\"]');c.checked=true;c.dispatchEvent(new Event('change'))}})()")
        got['saved'] = await b.js("__startupSaves.slice(-1)[0]")
        got['local'] = await b.js("ClientSettings.get('startupApps',[])")
        # At login on PosterChanOS (real app windows): the desktop opens them, once.
        await b.js("PCOSWin.enabled=()=>true;PCOSWin.open=(v)=>{__opened.push(v);return {}};true")
        got['count'] = await b.js("PCOS.runStartupApps()")
        await asyncio.sleep(2.4)
        got['opened'] = await b.js("__opened.slice()")
        got['again'] = await b.js("PCOS.runStartupApps()")

    asyncio.run(desktop.with_browser('online', '', check, PC_FIXTURE))
    assert {'messages', 'notes', 'global'} <= set(got['offered']), ("PosterChan apps not offered", got['offered'])
    assert got['saved'] == ['messages', 'notes'] and got['local'] == ['messages', 'notes'], got
    assert got['count'] == 2 and sorted(got['opened']) == ['messages', 'notes'], ("not opened at login", got)
    assert got['again'] == 0, "a second call opened them again"


MONITOR_FIXTURE = PC_FIXTURE + r'''
window.__layoutSaves=[];window.__arranged=[];
window.pcDisplays={status:async()=>[
  {name:'DP-1',make:'Dell',model:'U2720Q',active:true,primary:true,rect:{x:0,y:0,width:3840,height:2160}},
  {name:'HDMI-A-1',make:'LG',model:'27GL850',active:true,primary:false,rect:{x:3840,y:0,width:2560,height:1440}}]};
window.pcWM.arrange=async(l)=>{__arranged.push(l);return {ok:true}};
window.pcWM.windows=async()=>__opened.map((v,i)=>({id:i+1,title:'PosterChan Window — '+v}));
'''


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
def test_a_startup_app_can_be_pinned_to_a_monitor_and_that_monitor_tiles_it():
    """'add to OS Settings Startup Apps a way to pin startup apps to a specific monitor and apply a tiling style'.
    The page offers each switched-on app a monitor and each monitor a tiling style, keeps both on this device,
    and at login the SECOND monitor's desktop opens only the app pinned to it, then tiles its screen. Both choices
    belong to THIS computer: a laptop on the same account must not inherit them."""
    got = {}

    async def check(b):
        await desktop.login(b)
        await b.until("!!document.body && document.body.classList.contains('os-on') && !!document.querySelector('#os-bar')")
        await b.js("document.getElementById('osfr')?.remove();document.documentElement.classList.remove('osfr-on')")
        await b.js("PCOS.openSystemSettings()")
        await b.until("!!document.querySelector('.os-set-nav [data-page=\"startup\"]')")
        await b.js("document.querySelector('.os-set-nav [data-page=\"startup\"]').click()")
        page = "document.querySelector('[data-settings-page=\"startup\"]:not([hidden])')"
        await b.until(f"!!{page} && {page}.querySelectorAll('[data-startup-view]').length>3")
        got['monitor_before_on'] = await b.js(f"!!{page}.querySelector('[data-startup-monitor=\"notes\"]')")
        for v in ('messages', 'notes'):
            await b.js(f"(()=>{{const c={page}.querySelector('[data-startup-view=\"{v}\"]');c.checked=true;c.dispatchEvent(new Event('change'))}})()")
        await b.until(f"!!{page}.querySelector('[data-startup-monitor=\"notes\"]')")
        # The switch's own account save settles first. Polled, not slept: 15s after load the desktop runs the
        # startup apps itself (os.js fallback), and a slow test would let that run before the login below.
        published_before = await b.js("new Promise(r=>{let n=-1,t0=Date.now();const k=()=>{const m=(window.__published||[]).length;if(m===n||Date.now()-t0>3000)return r(m);n=m;setTimeout(k,300)};k()})")
        got['choices'] = await b.js(f"[...{page}.querySelectorAll('[data-startup-monitor=\"notes\"] option')].map(o=>o.textContent)")
        await b.js(f"(()=>{{const s={page}.querySelector('[data-startup-monitor=\"notes\"]');s.value='HDMI-A-1';s.dispatchEvent(new Event('change'))}})()")
        await b.until(f"!!{page}.querySelector('[data-startup-tile=\"HDMI-A-1\"]') && !{page}.querySelector('[data-startup-tiling]').hidden")
        await b.js(f"(()=>{{const s={page}.querySelector('[data-startup-tile=\"HDMI-A-1\"]');s.value='side-by-side';s.dispatchEvent(new Event('change'))}})()")
        got['placement'] = await b.js("ClientSettings.get('startupPlacement',{})")
        got['tiling'] = await b.js("ClientSettings.get('startupTiling',{})")
        got['published'] = await b.js("new Promise(r=>{let n=-1,t0=Date.now();const k=()=>{const m=(window.__published||[]).length;if(m===n||Date.now()-t0>3000)return r(m);n=m;setTimeout(k,300)};k()})") - published_before
        got['account_api'] = await b.js("typeof __PC.saveStartupLayout")
        got['switch_still'] = await b.js(f"{page}.querySelector('[data-startup-view=\"notes\"]').checked")
        await b.call("Emulation.setDeviceMetricsOverride", {"width": 400, "height": 900, "deviceScaleFactor": 1, "mobile": True})
        await asyncio.sleep(.3)
        got['overflow'] = await b.js(f"(()=>{{const c={page}.querySelector('[data-startup-pc]');return c.scrollWidth-c.clientWidth}})()")
        # Login on the SECOND monitor's desktop renderer.
        await b.js("pcShell.backgroundOwner=false;pcShell.outputName=async()=>'HDMI-A-1';"
                   "PCOSWin.enabled=()=>true;PCOSWin.open=(v)=>{__opened.push(v);return {}};true")
        got['count'] = await b.js("PCOS.runStartupApps()")
        await b.until("__arranged.length>0")
        got['opened'] = await b.js("__opened.slice()")
        got['arranged'] = await b.js("__arranged.slice()")
        got['errors'] = await b.js("__errors")

    asyncio.run(desktop.with_browser('online', '', check, MONITOR_FIXTURE))
    assert got['monitor_before_on'] is False, "an app that does not start at login offered a monitor"
    assert got['choices'] == ['Main monitor', 'Dell U2720Q (DP-1) · main', 'LG 27GL850 (HDMI-A-1)'], got['choices']
    assert got['placement'] == {'notes': 'HDMI-A-1'} and got['tiling'] == {'HDMI-A-1': 'side-by-side'}, got
    # PER DEVICE ("if I use laptop with same account I don't want issues"): kept on this machine, never published.
    assert got['published'] == 0 and got['account_api'] == 'undefined', ("monitor/tiling went to the account", got)
    assert got['switch_still'] is True, "choosing a monitor toggled the app's switch"
    assert got['overflow'] <= 1, f"the card overflows a phone-width window by {got['overflow']}px"
    assert got['count'] == 1 and got['opened'] == ['notes'], ("the second monitor opened the wrong apps", got)
    assert got['arranged'] == ['side-by-side'], got
    assert not got['errors'], got['errors']


STALE = PC_FIXTURE + r'''
/* The ACCOUNT still holds the old list: a save that never landed, or another window's older read. */
(function(){ let Rl; Object.defineProperty(window,'Relay',{configurable:true,get(){return Rl;},set(v){Rl=v;const real=Rl.query.bind(Rl);
  Rl.query=async(filters,...rest)=>{const f=filters&&filters[0]||{};
    if((f['#d']||[]).includes('pcai:client-prefs')){const out=[{id:'f'.repeat(64),pubkey:(f.authors||[''])[0],kind:30078,created_at:1,
      tags:[['d','pcai:client-prefs']],content:JSON.stringify({startupApps:['messages','notes']}),sig:''}];out.complete=true;return out;}
    return real(filters,...rest);};}}); })();
'''


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
def test_a_startup_app_switched_off_stays_off_when_an_older_account_copy_arrives():
    """'System Settings -> Startup, not letting me turn off an app to autostart now' (2026-10-09). Every
    PosterChan window re-applies the account's list when it loads; only the window the switch was flipped in
    knew it changed, so an older copy put the app straight back on. A reload is exactly 'another window'."""
    got = {}

    async def check(b):
        await desktop.login(b)
        await b.js("ClientSettings.set('startupApps',['messages','notes'])")
        await b.until("!!document.body && document.body.classList.contains('os-on') && !!document.querySelector('#os-bar')")
        await b.js("document.getElementById('osfr')?.remove();document.documentElement.classList.remove('osfr-on')")
        await b.js("PCOS.openSystemSettings()")
        await b.until("!!document.querySelector('.os-set-nav [data-page=\"startup\"]')")
        await b.js("document.querySelector('.os-set-nav [data-page=\"startup\"]').click()")
        page = "document.querySelector('[data-settings-page=\"startup\"]:not([hidden])')"
        await b.until(f"!!{page} && !!{page}.querySelector('[data-startup-view=\"messages\"]')")
        await b.js(f"(()=>{{const c={page}.querySelector('[data-startup-view=\"messages\"]');c.checked=false;c.dispatchEvent(new Event('change'))}})()")
        got['after_switch'] = await b.js("ClientSettings.get('startupApps',[])")
        await asyncio.sleep(1.0)
        # Another window loading = this page loading again, with the stale account copy still there.
        await b.js("window.__published=[];location.reload()")
        # Still signed in after the reload (the session persists), so the page restores the account's
        # prefs on its own boot -- exactly what a second window does. No second login.
        await asyncio.sleep(6.0)
        got['after_reload'] = await b.js("ClientSettings.get('startupApps',[])")
        got['healed'] = await b.js("(window.__published||[]).filter(e=>e.kind===30078&&(e.tags||[]).some(t=>t[1]==='pcai:client-prefs'))"
                                   ".map(e=>JSON.parse(e.content).startupApps)")

    asyncio.run(desktop.with_browser('online', '', check, STALE))
    assert got['after_switch'] == ['notes'], got
    assert got['after_reload'] == ['notes'], ("an older account copy turned the app back on", got)
    assert ['notes'] in got['healed'], ("the newer list was not put back up to the account", got)
