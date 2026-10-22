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
window.__directBefore=localStorage.getItem('pc.concord.rooms.v1.'+__PC.me().pubkey);
__PC.switchView('concord');
})()''')
        await browser.until("!!window.PCConcord&&!!document.querySelector('#cc-direct-inbox')")
        await click(browser,'#cc-direct-inbox')
        # Each invitation is a NAMED card (not a column of identical "Review invitation" buttons).
        await browser.until("document.querySelectorAll('#cc-direct-list .cc-direct-card').length===1")
        assert 'Private <fixture>' in await browser.js("document.querySelector('#cc-direct-list .cc-direct-card').textContent")
        await click(browser,'#cc-direct-list [data-cc-direct-view]')
        await browser.until("!!document.querySelector('#cc-invite-sheet:not(.hidden) #cc-invite-preview')")
        assert 'Private <fixture>' in await browser.js("document.querySelector('#cc-invite-preview').textContent")
        # Its own sheet: no paste-a-link box, and the join dialog stays shut.
        assert await browser.js("!document.querySelector('#cc-invite-sheet #cc-invite-url') && document.querySelector('#cc-join').classList.contains('hidden')")
        # Nothing is fetched from the community before you accept (its icon would tell its host who was invited).
        assert not await browser.js("!!document.querySelector('#cc-invite-preview img[src*=\"never-fetch\"]')")
        # It fits the screen it is on.
        assert await browser.js("(()=>{const r=document.querySelector('#cc-invite-preview').getBoundingClientRect();return r.left>=-1&&r.right<=innerWidth+1})()")
        assert await browser.js("__directQueries.length===0")
        assert await browser.js("localStorage.getItem('pc.concord.rooms.v1.'+__PC.me().pubkey)===__directBefore")
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
        assert await browser.js("localStorage.getItem('pc.concord.rooms.v1.'+__PC.me().pubkey)===__directBefore")
    init="localStorage.setItem('pc_nostr_settings',JSON.stringify({...JSON.parse(localStorage.getItem('pc_nostr_settings')||'{}'),osMode:false}));"
    asyncio.run(desktop.with_browser('online','',check,extra_init=init))


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
@pytest.mark.parametrize('width', [1280, 390])
def test_the_invitation_list_answers_in_place_and_not_now_keeps_it(width):
    """Decline from the list removes it; "Not now" on the sheet leaves it pending; Accept from the list joins."""
    async def check(browser):
        await browser.call('Emulation.setDeviceMetricsOverride',dict(width=width,height=850,deviceScaleFactor=1,mobile=width<600))
        await desktop.login(browser)
        await browser.js('''(async()=>{
const p=__PC,api=await p.cordDirectModule(),ctx=p.cordDirectContext(),owner=ctx.pubkey;
window.__ids=[];
for(const [i,name] of [[1,'First community'],[2,'Second community']]){
  const salt=String(i).repeat(64);
  const input=new Uint8Array([...new TextEncoder().encode('concord/community'),...owner.match(/../g).map(h=>parseInt(h,16)),...salt.match(/../g).map(h=>parseInt(h,16))]);
  const id=[...new Uint8Array(await crypto.subtle.digest('SHA-256',input))].map(b=>b.toString(16).padStart(2,'0')).join('');
  __ids.push(id);
  const made=await api.create({community_id:id,owner,owner_salt:salt,community_root:'33'.repeat(32),root_epoch:1,channels:[],relays:['wss://never-connect.invalid'],name},ctx.pubkey,ctx);
  await api.park(made.wrap,ctx);
}
p.relayQueryFrom=()=>Promise.resolve([]);
__PC.switchView('concord');})()''')
        await browser.until("!!window.PCConcord&&!!document.querySelector('#cc-direct-inbox')")
        await click(browser,'#cc-direct-inbox')
        await browser.until("document.querySelectorAll('#cc-direct-list .cc-direct-card').length===2")
        # Decline the first one, right there in the list.
        await browser.js("[...document.querySelectorAll('.cc-direct-card')].find(c=>c.textContent.includes('First community')).querySelector('[data-cc-direct-decline]').click()")
        await browser.until("document.querySelectorAll('#cc-direct-list .cc-direct-card').length===1")
        assert await browser.js("PCCordDirectInvites.pending(__PC.cordDirectContext()).length===1")
        # Open the second, then "Not now": the sheet closes and the invitation is still waiting.
        await browser.js("document.querySelector('[data-cc-direct-view]').click()")
        await browser.until("!!document.querySelector('#cc-invite-sheet:not(.hidden) #cc-invite-close')")
        await browser.js("document.querySelector('#cc-invite-close').click()")
        await browser.until("document.querySelector('#cc-invite-sheet').classList.contains('hidden')")
        assert await browser.js("PCCordDirectInvites.pending(__PC.cordDirectContext()).length===1")
        # Accept it from the list: it joins, and the sheet does not stay up.
        await click(browser,'#cc-direct-inbox')
        await browser.until("!!document.querySelector('[data-cc-direct-accept]')")
        await browser.js("document.querySelector('[data-cc-direct-accept]').click()")
        await browser.until("(JSON.parse(localStorage.getItem('pc.concord.rooms.v1.'+__PC.me().pubkey)||'[]')).some(r=>r&&r.communityId===__ids[1])")
        await browser.until("document.querySelector('#cc-invite-sheet').classList.contains('hidden')")
        assert await browser.js("PCCordDirectInvites.pending(__PC.cordDirectContext()).length===0")
    asyncio.run(desktop.with_browser('online','',check))
