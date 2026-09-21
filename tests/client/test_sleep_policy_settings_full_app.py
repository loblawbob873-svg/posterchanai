"""System Settings → Power → Hibernation offers "when the computer sleeps", and it reaches the machine.

The shipped bundle, with the power bridge as the only fixture. Its status is the LAPTOP's real one
(hibernation ready, and the old installer's `HibernateDelaySec=500` still in sleep.conf) so the row
must show a delay that is not among its own choices as itself, rather than silently selecting a
wrong one; choosing an hour must hand exactly 3600 to the bridge; a refusal must put the select
back; and before hibernation is ready the row must not be offered at all.
"""
import asyncio
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop


@pytest.fixture(scope='module', autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


FIXTURE = r'''
window.__sleepCalls=[];window.__hibReady=localStorage.getItem('__hibNotReady')?false:true;
window.pcWM={windows:async()=>[],onEvent:()=>()=>{},focus:async()=>true,launch:async()=>({pid:1}),
  shellFront:async()=>true};
window.pcPower={
  status:async()=>({brightness:{available:false},battery:{present:true,percent:80,status:'Discharging'},
    profiles:{available:false,list:[]},keepAwake:false,idleSeconds:300,canHibernate:__hibReady,
    hibernateConfigured:__hibReady,
    sleepPolicy:__hibReady?{hibernateReady:true,mode:'suspend-then-hibernate',delaySec:500,
      choices:[0,1800,3600,7200,10800],canChange:true}:{hibernateReady:false,mode:'suspend',delaySec:0}}),
  setSleepPolicy:async(n)=>{__sleepCalls.push(n);if(n===7200)throw new Error('refused by fixture');
    return {mode:n?'suspend-then-hibernate':'suspend',delaySec:n}},
  setIdleTimeout:async()=>({}),setKeepAwake:async()=>({}),suspend:async()=>({ok:true}),
};
'''


async def _power_page(b):
    await desktop.login(b)
    await b.until("!!document.body && document.body.classList.contains('os-on') && !!document.querySelector('#os-bar')")
    await b.js("document.getElementById('osfr')?.remove();document.documentElement.classList.remove('osfr-on')")
    await b.js("PCOS.openSystemSettings()")
    await b.until("!!document.querySelector('.os-set-nav [data-page=\"power\"]')")
    await b.js("document.querySelector('.os-set-nav [data-page=\"power\"]').click()")
    await b.until("!!document.querySelector('[data-settings-page=\"power\"]:not([hidden]) .os-hibernate')")


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
def test_the_sleep_delay_is_shown_as_it_is_and_changed_through_the_bridge():
    async def check(b):
        await _power_page(b)
        await b.until("!!document.querySelector('[data-sleep-delay]')")
        sel = "document.querySelector('[data-sleep-delay]')"
        opts = await b.js("[...%s.options].map(o=>[o.value,o.textContent.trim(),o.selected])" % sel)
        values = [o[0] for o in opts]
        assert values[-1] == '0' and 'never' in opts[-1][1].lower(), opts
        assert {'1800', '3600', '7200', '10800'} <= set(values), opts
        assert [o[0] for o in opts if o[2]] == ['500'], 'the machine\'s real delay must be what is selected: %r' % opts

        await b.js("(()=>{const s=%s;s.value='3600';s.dispatchEvent(new Event('change'))})()" % sel)
        await b.until('__sleepCalls.length===1')
        assert await b.js('__sleepCalls[0]') == 3600
        # A refusal puts the select back where the machine still is.
        await b.until("!%s.disabled" % sel)
        await b.js("(()=>{const s=%s;s.value='7200';s.dispatchEvent(new Event('change'))})()" % sel)
        await b.until('__sleepCalls.length===2')
        await b.until("!%s.disabled" % sel)
        assert await b.js("%s.value" % sel) == '3600'
        await b.js("(()=>{const s=%s;s.value='0';s.dispatchEvent(new Event('change'))})()" % sel)
        await b.until('__sleepCalls.length===3')
        assert await b.js('__sleepCalls[2]') == 0
        assert not await b.js('__errors'), await b.js('__errors')

    asyncio.run(desktop.with_browser('online', '', check, FIXTURE))


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
def test_before_hibernation_is_ready_there_is_nothing_to_choose():
    async def check(b):
        await b.js("localStorage.setItem('__hibNotReady','1')")
        await b.call('Page.reload')
        await b.until('!!window.__PC && !!window.PCOS')
        await _power_page(b)
        assert await b.js("!!document.querySelector('[data-enable-hibernate]')")
        assert not await b.js("!!document.querySelector('[data-sleep-delay]')")

    asyncio.run(desktop.with_browser('online', '', check, FIXTURE))
