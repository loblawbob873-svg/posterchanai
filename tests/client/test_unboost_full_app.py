"""Real bundled Social UI, signer and relay handling; only network boundaries are fixtures."""
import asyncio
from pathlib import Path
import pytest
from tests.client import test_desktop_offline_full_app as desktop

@pytest.fixture(scope='module', autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()

@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
@pytest.mark.parametrize(('width','reject_first'),[(1280,False),(390,True)])
def test_repost_button_waits_for_ack_and_preserves_original_after_undo(width,reject_first):
    async def check(b):
        await b.call('Emulation.setDeviceMetricsOverride',{'width':width,'height':900,'deviceScaleFactor':1,'mobile':width<640})
        await desktop.login(b)
        await b.js(r'''(()=>{
          const key=new Uint8Array(32).fill(1),other=new Uint8Array(32).fill(2),now=Math.floor(Date.now()/1000);
          window.__original=NostrTools.finalizeEvent({kind:1,created_at:now,content:'Original survives undo',tags:[]},other);
          window.__repost=NostrTools.finalizeEvent({kind:6,created_at:now+1,content:JSON.stringify(__original),tags:[['e',__original.id,'wss://fixture.invalid'],['p',__original.pubkey]]},key);
          window.__events=[__original,__repost];for(const event of __events)Store.saveEvent(event);
          __PC.timelineTop('home');
        })()''')
        await b.until("!!document.querySelector('.note[data-id=\"'+__original.id+'\"] .act[data-a=repost]')")
        await b.until("document.querySelector('.note[data-id=\"'+__original.id+'\"] .act[data-a=repost]').classList.contains('on')")
        await b.js("document.querySelector('.note[data-id=\"'+__original.id+'\"] .act[data-a=repost]').click()")
        await b.until('__undoAcks.length>0')
        assert await b.js('Store.has(__repost.id)')
        assert await b.js("document.querySelector('.note[data-id=\"'+__original.id+'\"] .act[data-a=repost]').disabled")
        if reject_first:
            await b.js("for(const a of __undoAcks.splice(0))a.socket.fire('message',['OK',a.event.id,false,'blocked: fixture rejection'])")
            await b.until("!document.querySelector('.note[data-id=\"'+__original.id+'\"] .act[data-a=repost]').disabled")
            assert await b.js('Store.has(__repost.id)')
            assert await b.js("document.querySelector('.note[data-id=\"'+__original.id+'\"] .act[data-a=repost]').title==='retry undo repost'")
            await b.js("localStorage.setItem('__unboost_fixture',JSON.stringify({original:__original,repost:__repost,firstDeletion:__published.find(e=>e.kind===5).id}))")
            await b.call('Page.reload')
            await asyncio.sleep(.3)
            await b.until('!!window.__PC && !!__PC.me() && document.readyState==="complete"')
            await b.js("for(const event of __events)Store.saveEvent(event);__PC.timelineTop('home')")
            await b.until("!!document.querySelector('.note[data-id=\"'+__original.id+'\"] .act[data-a=repost]')")
            assert await b.js("document.querySelector('.note[data-id=\"'+__original.id+'\"] .act[data-a=repost]').title==='retry undo repost'")
            await b.js("document.querySelector('.note[data-id=\"'+__original.id+'\"] .act[data-a=repost]').click()")
            await b.until('__undoAcks.length>0')
            assert await b.js("__published.filter(e=>e.kind===5).every(e=>e.id===JSON.parse(localStorage.getItem('__unboost_fixture')).firstDeletion)"),'failed retry created another signed deletion'
        await b.js("for(const a of __undoAcks.splice(0))a.socket.fire('message',['OK',a.event.id,true,'accepted'])")
        await b.until('!Store.has(__repost.id)')
        assert await b.js('Store.has(__original.id)')
        assert await b.js("document.querySelector('.note[data-id=\"'+__original.id+'\"]').innerText.includes('Original survives undo')")
        assert not await b.js("!!document.querySelector('[data-repost-id=\"'+__repost.id+'\"] .repost-tag')")
        assert await b.js("__published.filter(e=>e.kind===5).every(e=>e.tags.filter(t=>t[0]==='e').every(t=>t[1]===__repost.id))")
    extra=r'''
localStorage.setItem('pc_nostr_settings',JSON.stringify({...JSON.parse(localStorage.getItem('pc_nostr_settings')||'{}'),osMode:false}));
window.__undoAcks=[];
const restored=JSON.parse(localStorage.getItem('__unboost_fixture')||'null');if(restored){window.__original=restored.original;window.__repost=restored.repost;window.__events=[__original,__repost];}
const socketSend=WebSocket.prototype.send;
WebSocket.prototype.send=function(raw){const message=JSON.parse(raw);if(message[0]==='EVENT'&&message[1].kind===5){__published.push(message[1]);__undoAcks.push({socket:this,event:message[1]});return;}return socketSend.call(this,raw)};
'''
    asyncio.run(desktop.with_browser('online','',check,extra))
