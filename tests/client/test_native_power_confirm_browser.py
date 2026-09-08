"""Real bundled popup/confirmation DOM; power IPC is a recorder, never systemctl."""
import asyncio
import pytest
from tests.client.test_desktop_offline_full_app import bundle, with_browser

POWER = r'''
window.__powerCalls=[]; window.__windowCloses=0; window.__powerReject=false;
window.close=()=>{__windowCloses++;};
window.pcPower={status:async()=>({profiles:[],canHibernate:false}),
 reboot:async()=>{__powerCalls.push('reboot');if(__powerReject)throw Error('Policy denied reboot');return{ok:true};},
 poweroff:async()=>{__powerCalls.push('poweroff');if(__powerReject)throw Error('Policy denied shutdown');return{ok:true};},
 suspend:async()=>{__powerCalls.push('suspend');return{ok:true};}};
'''

@pytest.mark.parametrize('action',['reboot','poweroff'])
def test_native_power_confirmation_keeps_owner_alive_and_dispatches_once(action):
    async def check(b):
        await b.js("PCOSShell.openControl('power',document.querySelector('#os-popup-host'))")
        await b.until("!!document.querySelector('[data-act=\""+action+"\"]')")
        await b.js("document.querySelector('[data-act=\""+action+"\"]').click()")
        await b.until("!!document.querySelector('.uiconfirm')")
        assert await b.js('__windowCloses') == 0, 'native popup closed before confirmation'
        assert await b.js("(()=>{const e=document.querySelector('.uiconfirm');return e.getBoundingClientRect().width>0&&!!e.closest('#os-popup-host')})()"), 'confirmation must be visible inside popup'
        assert await b.js('__powerCalls.length') == 0
        await b.js("document.querySelector('[data-uc=\"0\"]').click()")
        await b.until("!document.querySelector('.uiconfirm')")
        assert await b.js('__windowCloses') == 0
        await b.js("document.querySelector('[data-act=\""+action+"\"]').click();document.querySelector('[data-act=\""+action+"\"]').click()")
        await b.until("!!document.querySelector('.uiconfirm')")
        assert await b.js("document.querySelectorAll('.uiconfirm').length") == 1
        await b.js("document.querySelector('[data-uc=\"1\"]').click()")
        await b.until('__powerCalls.length===1')
        assert await b.js('__powerCalls') == [action]
        await b.until('__windowCloses===1')
    asyncio.run(with_browser('hang','?pcpopup=tray',check,POWER))


def test_native_power_dispatch_failure_stays_visible_and_retryable():
    async def check(b):
        await b.js("PCOSShell.openControl('power',document.querySelector('#os-popup-host'));__powerReject=true")
        await b.until("!!document.querySelector('[data-act=reboot]')")
        await b.js("document.querySelector('[data-act=reboot]').click()")
        await b.until("!!document.querySelector('.uiconfirm')")
        await b.js("document.querySelector('[data-uc=\"1\"]').click()")
        await b.until("!!document.querySelector('.os-power-error')")
        assert await b.js("document.querySelector('.os-power-error').textContent") == 'Policy denied reboot'
        assert await b.js("document.querySelector('.os-power-error').getBoundingClientRect().height>0")
        assert await b.js('__windowCloses') == 0
        assert await b.js("!document.querySelector('[data-act=reboot]').disabled")
    asyncio.run(with_browser('hang','?pcpopup=tray',check,POWER))


@pytest.mark.parametrize('action',['reboot','poweroff'])
def test_actual_tray_power_tile_reaches_owned_confirmation(action):
    async def check(b):
        await b.until("!!document.querySelector('[data-os=power]')")
        pos=await b.js("(()=>{const r=document.querySelector('[data-os=power]').getBoundingClientRect();return{x:r.x+r.width/2,y:r.y+r.height/2}})()")
        await b.call('Input.dispatchMouseEvent',dict(type='mousePressed',button='left',clickCount=1,**pos))
        await b.call('Input.dispatchMouseEvent',dict(type='mouseReleased',button='left',clickCount=1,**pos))
        await b.until("!!document.querySelector('[data-act='+"+repr(action)+"+']')")
        assert await b.js('__windowCloses')==0
        await b.js("document.querySelector('[data-act='+"+repr(action)+"+']').click()")
        await b.until("!!document.querySelector('.uiconfirm')")
        assert await b.js("document.querySelector('.uiconfirm').getBoundingClientRect().height>0")
        assert await b.js('__powerCalls.length')==0
        await b.js("document.querySelector('[data-uc=\"0\"]').click()")
        assert await b.js('__windowCloses')==0
    asyncio.run(with_browser('hang','?pcpopup=tray',check,POWER))
