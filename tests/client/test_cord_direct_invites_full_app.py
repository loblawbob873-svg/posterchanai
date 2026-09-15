"""Direct invite review uses the real DOM; unopened/declined invites never join."""
import asyncio
import json
from pathlib import Path
import pytest
from tests.client import test_desktop_offline_full_app as desktop


@pytest.fixture(scope='module', autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


async def click(browser, selector):
    await browser.until(f"!!document.querySelector({json.dumps(selector)})")
    rect = await browser.js(f"(()=>{{const r=document.querySelector({json.dumps(selector)}).getBoundingClientRect();return{{x:r.x+r.width/2,y:r.y+r.height/2}}}})()")
    hit = await browser.js(f"document.elementFromPoint({rect['x']},{rect['y']})?.closest({json.dumps(selector)})!==null")
    assert hit
    await browser.call('Input.dispatchMouseEvent', dict(type='mousePressed', button='left', clickCount=1, **rect))
    await browser.call('Input.dispatchMouseEvent', dict(type='mouseReleased', button='left', clickCount=1, **rect))


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
@pytest.mark.parametrize('width', [1280, 390])
def test_private_invite_review_and_decline_never_connect_or_store_membership(width):
    async def check(browser):
        await browser.call('Emulation.setDeviceMetricsOverride',dict(width=width,height=850,deviceScaleFactor=1,mobile=width<600))
        await browser.until("document.body.classList.contains('guest')")
        await browser.js("(()=>{const secret=new Uint8Array(32).fill(7),pubkey=NostrTools.getPublicKey(secret);window.__recipient=NostrTools.nip19.npubEncode(pubkey);window.__events=[0,1].map(kind=>NostrTools.finalizeEvent({kind,created_at:Math.floor(Date.now()/1000)-2,tags:[],content:kind===0?JSON.stringify({name:'Direct recipient',nip05:'recipient@fixture.invalid'}):'fixture'},secret));})()")
        await desktop.login(browser)
        await browser.js("__PC.switchView('nostrverse')")
        await browser.until("__PC.profOf(NostrTools.nip19.decode(__recipient).data).name==='Direct recipient'")
        await browser.js('''(async()=>{
const p=__PC,api=await p.cordDirectModule(),ctx=p.cordDirectContext();
const salt='22'.repeat(32),owner=ctx.pubkey;
const input=new Uint8Array([...new TextEncoder().encode('concord/community'),...owner.match(/../g).map(h=>parseInt(h,16)),...salt.match(/../g).map(h=>parseInt(h,16))]);
const id=[...new Uint8Array(await crypto.subtle.digest('SHA-256',input))].map(b=>b.toString(16).padStart(2,'0')).join('');
window.__directBundle={community_id:id,owner,owner_salt:salt,community_root:'33'.repeat(32),root_epoch:1,channels:[],relays:['wss://never-connect.invalid'],name:'Private <fixture>',icon:'https://never-fetch.invalid/icon.png'};
const made=await api.create(__directBundle,ctx.pubkey,ctx);await api.park(made.wrap,ctx);
window.__directQueries=[];const original=p.relayQueryFrom;
p.relayQueryFrom=(relays,...args)=>{if(relays.includes('wss://never-connect.invalid')){__directQueries.push(relays);return Promise.resolve([]);}return original(relays,...args);};
window.__directBefore=localStorage.getItem('pc.concord.invites');
__PC.switchView('concord');
})()''')
        await browser.until("!!window.PCConcord&&!!document.querySelector('#cc-direct-inbox')")
        await click(browser,'#cc-direct-inbox')
        await browser.until("document.querySelectorAll('#cc-direct-list button').length===1")
        await click(browser,'#cc-direct-list button')
        await browser.until("!!document.querySelector('#cc-invite-preview')")
        assert 'Private <fixture>' in await browser.js("document.querySelector('#cc-invite-preview').textContent")
        assert not await browser.js("!!document.querySelector('#cc-invite-preview img')")
        assert await browser.js("__directQueries.length===0")
        assert await browser.js("localStorage.getItem('pc.concord.invites')===__directBefore")
        await click(browser,'#cc-invite-decline')
        assert await browser.js("PCCordDirectInvites.pending(__PC.cordDirectContext()).length===0")
        await browser.js("window.__sentDirect=[];__PC.sendCordDirectInvite=async(bundle,recipient)=>{__sentDirect.push({bundle,recipient});return 'accepted';};PCConcord.showDirectInviteSender({name:'Fixture',cord:{bundle:{...__directBundle,control_root:'SECRET',held_roots:[{seed:'SECRET'}]}}});")
        await browser.js("(()=>{const input=document.querySelector('#cc-direct-recipient');input.focus();input.value='Direct rec';input.setSelectionRange(input.value.length,input.value.length);input.dispatchEvent(new Event('input',{bubbles:true}));})()")
        await browser.until("!!document.querySelector('[role=listbox]:not(.hidden) [role=option]')")
        for key, code in [('ArrowDown',40),('Enter',13)]:
            await browser.call('Input.dispatchKeyEvent',dict(type='keyDown',key=key,code=key,windowsVirtualKeyCode=code))
            await browser.call('Input.dispatchKeyEvent',dict(type='keyUp',key=key,code=key))
        assert await browser.js("document.querySelector('#cc-direct-recipient').value===__recipient&&__sentDirect.length===0")
        await click(browser,'#cc-direct-send-confirm')
        await browser.until("__sentDirect.length===1")
        assert await browser.js("__sentDirect[0].recipient===__recipient&&!('control_root' in __sentDirect[0].bundle)&&!('held_roots' in __sentDirect[0].bundle)")
        assert await browser.js("__directQueries.length===0")
        assert await browser.js("localStorage.getItem('pc.concord.invites')===__directBefore")
    init="localStorage.setItem('pc_nostr_settings',JSON.stringify({...JSON.parse(localStorage.getItem('pc_nostr_settings')||'{}'),osMode:false}));"
    asyncio.run(desktop.with_browser('online','',check,extra_init=init))
