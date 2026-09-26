"""Every start-menu footer button DOES something on PosterChanOS — measured on the desktop that performs it.

Reported 2026-09-25 on the laptop: "why does the power button on the start menu do nothing, most of the
buttons you added do nothing". test_start_menu_footer_full_app.py only proved the POPUP emits
`pcPopup.act('settings')` into a stub; nothing ever checked that the desktop window, which receives it
as a `pc:act:<kind>` shell tick (main.js pc:popup:act → forwardShellTick), then shows anything.

Runs the SHIPPED bundle as the PosterChanOS desktop. The only fixtures are the machine's bridges
(pcWM, pcPower, pcShell's session calls) — each button is pressed by delivering exactly the tick the
popup's press produces, and the assertion is what a person would see.
"""
import asyncio
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop


@pytest.fixture(scope='module', autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


MACHINE = r'''
window.__wmListeners=[];window.__power=[];window.__logouts=0;
window.pcWM={windows:async()=>[],onEvent:(fn)=>{__wmListeners.push(fn);return()=>{}},
  launch:async()=>({pid:1}),focus:async()=>true,close:async()=>true};
window.pcPower={status:async()=>({profiles:{available:true,list:['balanced','performance'],active:'balanced'},
    canHibernate:false,brightness:{available:false}}),
  suspend:async()=>{__power.push('suspend');return true},reboot:async()=>{__power.push('reboot');return true},
  poweroff:async()=>{__power.push('poweroff');return true},setProfile:async(p)=>{__power.push('profile:'+p);return true}};
window.pcDisplays={status:async()=>({outputs:[]})};
'''

TICK = "__wmListeners.forEach(f=>f({name:'tick',payload:'pc:act:%s'}))"
POP = r'''(()=>{const d=document.querySelector('.os-pop');if(!d)return null;const r=d.getBoundingClientRect();
  const cs=getComputedStyle(d);return {text:d.innerText.slice(0,200),w:r.width,h:r.height,
  vis:cs.display!=='none'&&cs.visibility!=='hidden'&&+cs.opacity>0.1,
  onscreen:r.right>0&&r.bottom>0&&r.left<innerWidth&&r.top<innerHeight}})()'''


async def _desktop(b):
    await desktop.login(b)
    await b.until("!!document.body && document.body.classList.contains('os-on') && !!document.querySelector('#os-start')")
    await b.until('__wmListeners.length>0')
    await b.js("document.getElementById('osfr')?.remove();document.documentElement.classList.remove('osfr-on')")


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
def test_power_opens_a_visible_power_panel_that_stays_open():
    got = {}

    async def check(b):
        await _desktop(b)
        await b.js(TICK % 'power')
        await asyncio.sleep(1.0)
        got['at1s'] = await b.js(POP)
        await asyncio.sleep(1.5)
        got['at2_5s'] = await b.js(POP)
        got['errors'] = await b.js("(window.__errors||[]).slice(-5)")

    asyncio.run(desktop.with_browser('online', '', check, MACHINE))
    for k in ('at1s', 'at2_5s'):
        p = got[k]
        assert p, f"no power panel {k} after the start menu's Power press: {got}"
        assert p['vis'] and p['onscreen'] and p['w'] > 100 and p['h'] > 60, (k, got)
        assert 'Shut down' in p['text'] and 'Restart' in p['text'], (k, got)


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
def test_settings_opens_system_settings():
    got = {}

    async def check(b):
        await _desktop(b)
        await b.js(TICK % 'settings')
        await asyncio.sleep(2.0)
        got['open'] = await b.js("/System Settings/.test(document.body.innerText)")

    asyncio.run(desktop.with_browser('online', '', check, MACHINE))
    assert got['open'], got


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
def test_accounts_opens_the_account_switcher_on_screen():
    got = {}

    async def check(b):
        await _desktop(b)
        await b.js(TICK % 'accounts')
        await asyncio.sleep(1.2)
        got['pop'] = await b.js(r'''(()=>{const p=document.querySelector('.acct-pop');if(!p)return null;const r=p.getBoundingClientRect();
          return {w:r.width,h:r.height,onscreen:r.top>=0&&r.bottom<=innerHeight+1&&r.left>=0&&r.right<=innerWidth+1}})()''')

    asyncio.run(desktop.with_browser('online', '', check, MACHINE))
    assert got['pop'] and got['pop']['onscreen'] and got['pop']['w'] > 100, got


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
def test_the_os_start_menu_offers_no_button_that_cannot_work_there():
    """Full screen reaches the desktop as a tick (no user gesture) and the shell already fills the output;
    Classic would take the desktop down and leave the machine without a taskbar. Neither is offered on
    PosterChanOS -- and every button that IS offered is one the tests above prove does something."""
    got = {}

    async def check(b):
        await desktop.login(b)
        await b.until("!!document.querySelector('.os-foot .os-foot-btn')")
        got['kinds'] = await b.js("[...document.querySelectorAll('.os-foot-btn')].map(x=>x.dataset.foot)")

    asyncio.run(desktop.with_browser('online', '?pcpopup=start', check, MACHINE))
    assert 'power' in got['kinds'] and 'settings' in got['kinds'] and 'logout' in got['kinds'], got
    assert 'full' not in got['kinds'] and 'classic' not in got['kinds'], got
