"""Recipient selection uses real profile events, browser input, and shipped painters."""
import asyncio
import json
import pytest
from tests.client import test_desktop_offline_full_app as desktop

@pytest.fixture(scope='module',autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()

EXTRA=r'''
window.__shares=[];window.__connections=[];
const userFetch=window.fetch;
window.fetch=(url,opts={})=>{
 if(String(url).includes('/api/media-center')){
  if(opts.method==='PUT'){__shares.push(JSON.parse(opts.body));return Promise.resolve(new Response(opts.body,{status:200}));}
  return Promise.resolve(new Response(JSON.stringify({libraries:[{id:'fixture',name:'Movies',count:0,can_manage:true,shared_with:['existing@poster.place']}],roots:[],profiles:[],enabled:true}),{status:200}));
 }
 return userFetch(url,opts);
};
'''

async def seed(b):
    await b.until("document.body.classList.contains('guest')")
    await b.js('''(()=>{
      __events=[];window.__keys=[];
      for(const n of [2,3]){const secret=new Uint8Array(32).fill(n),pubkey=NostrTools.getPublicKey(secret);__keys.push(NostrTools.nip19.npubEncode(pubkey));
        for(const kind of [0,1])__events.push(NostrTools.finalizeEvent({kind,created_at:Math.floor(Date.now()/1000)-n,tags:[],content:kind===0?JSON.stringify({name:'Same User',nip05:'same'+n+'@fixture.invalid'}):'User fixture'},secret));}
    })()''')
    await desktop.login(b)
    await b.until("!!window.PCOS && !!document.querySelector('#os-start')")
    await b.js("__PC.switchView('nostrverse')")
    await b.until("__PC.profOf(NostrTools.nip19.decode(__keys[1]).data).name==='Same User'")

async def type_query(b, selector, value):
    await b.js(f"(()=>{{const e=document.querySelector({json.dumps(selector)});e.focus();e.value={json.dumps(value)};e.setSelectionRange(e.value.length,e.value.length);e.dispatchEvent(new Event('input',{{bubbles:true}}));}})()")
    await b.until("!!document.querySelector('[role=listbox]:not(.hidden) [role=option]')")

async def key(b, value):
    await b.call('Input.dispatchKeyEvent',{'type':'keyDown','key':value,'code':value,'windowsVirtualKeyCode':{'Enter':13,'ArrowDown':40,'ArrowUp':38,'Escape':27}[value]})
    await b.call('Input.dispatchKeyEvent',{'type':'keyUp','key':value,'code':value})

@pytest.mark.parametrize('surface',['remote','media'])
def test_user_selection_is_canonical_and_does_not_submit(surface):
    async def check(b):
        await seed(b)
        if surface=='remote':
            await b.js("__PC.uiConfirm=async()=>true;__PC.startRemoteDesktop=async peer=>{__connections.push(peer);return true;};document.dispatchEvent(new Event('pc:remote-desktop-window'))")
            selector='[data-rd-peer]'
        else:
            await b.js("__PC.switchView('media-center')")
            await b.until("!!document.querySelector('.mc-share textarea')")
            await b.js("document.querySelector('.mc-share').open=true")
            selector='.mc-share textarea'
        await type_query(b,selector,('existing@poster.place\n' if surface=='media' else '')+'same')
        assert await b.js("document.querySelectorAll('[role=listbox]:not(.hidden) [role=option]').length")==2
        await key(b,'ArrowDown');await key(b,'ArrowDown');await key(b,'ArrowUp');await key(b,'ArrowDown')
        selected=await b.js("document.querySelector('[role=option][aria-selected=true]').textContent")
        expected=await b.js('__keys['+('1' if 'same3@' in selected else '0')+']')
        await key(b,'Enter')
        chosen=await b.js(f"document.querySelector({json.dumps(selector)}).value")
        assert chosen.split('\n')[-1]==expected
        assert await b.js('__connections.length+__shares.length')==0
        if surface=='remote':
            await b.js("document.querySelector('[data-rd-share]').click()")
            await b.until('__connections.length===1')
            assert await b.js('__connections')==[chosen]
        else:
            assert chosen.split('\n')[0]=='existing@poster.place'
            await b.js("document.querySelector('.mc-share button').click()")
            await b.until('__shares.length===1')
            assert await b.js('__shares')==[{'shared_with':chosen.split('\n')}]
        await type_query(b,selector,'same')
        point=await b.js("(()=>{const e=document.querySelector('[role=listbox]:not(.hidden) [role=option]');e.scrollIntoView({block:'nearest'});const r=e.getBoundingClientRect(),x=r.left+r.width/2,y=r.top+r.height/2;return{x,y,hit:document.elementFromPoint(x,y)?.closest('[role=option]')===e,text:e.textContent};})()")
        assert point['hit'],point
        for event in ['mousePressed','mouseReleased']:
            await b.call('Input.dispatchMouseEvent',{'type':event,'x':point['x'],'y':point['y'],'button':'middle','clickCount':1})
        assert await b.js(f"document.querySelector({json.dumps(selector)}).value")=='same'
        await type_query(b,selector,'same')
        point=await b.js("(()=>{const e=document.querySelector('[role=listbox]:not(.hidden) [role=option]');e.scrollIntoView({block:'nearest'});const r=e.getBoundingClientRect();return{x:r.left+r.width/2,y:r.top+r.height/2,text:e.textContent};})()")
        for event in ['mousePressed','mouseReleased']:
            await b.call('Input.dispatchMouseEvent',{'type':event,'x':point['x'],'y':point['y'],'button':'left','clickCount':1})
        expected=await b.js('__keys['+('1' if 'same3@' in point['text'] else '0')+']')
        assert await b.js(f"document.querySelector({json.dumps(selector)}).value")==expected
        assert await b.js('__connections.length+__shares.length')==1
        await type_query(b,selector,'same')
        await key(b,'ArrowDown')
        await b.js(f"document.querySelector({json.dumps(selector)}).dispatchEvent(new KeyboardEvent('keydown',{{key:'Enter',isComposing:true,bubbles:true}}))")
        assert await b.js(f"document.querySelector({json.dumps(selector)}).value")=='same'
        assert await b.js('__connections.length+__shares.length')==1
        # A drag is cancellation, even if release returns over its starting row.
        point=await b.js("(()=>{const e=document.querySelector('[role=listbox]:not(.hidden) [role=option]');e.scrollIntoView({block:'nearest'});const r=e.getBoundingClientRect();return{x:r.left+20,y:r.top+r.height/2};})()")
        await b.call('Input.dispatchMouseEvent',{'type':'mousePressed',**point,'button':'left','clickCount':1})
        await b.call('Input.dispatchMouseEvent',{'type':'mouseMoved','x':point['x']+20,'y':point['y'],'button':'left','buttons':1})
        await b.call('Input.dispatchMouseEvent',{'type':'mouseReleased',**point,'button':'left','clickCount':1})
        assert await b.js(f"document.querySelector({json.dumps(selector)}).value")=='same'
        await type_query(b,selector,'same');await key(b,'Escape')
        assert await b.js("document.querySelectorAll('[role=listbox]:not(.hidden)').length")==0
        if surface=='remote':
            await type_query(b,selector,'fixture.invalid')
            await key(b,'Enter')
            await b.until('__connections.length===2')
            assert await b.js('__connections[1]')=='fixture.invalid'
        if surface=='media':
            await b.js(f"(()=>{{const e=document.querySelector({json.dumps(selector)});e.value='same\\nkeep@poster.place';e.focus();e.setSelectionRange(2,2);e.dispatchEvent(new Event('input'));}})()")
            await key(b,'ArrowDown');await key(b,'Enter')
            assert await b.js(f"document.querySelector({json.dumps(selector)}).value.split('\\n')[1]")=='keep@poster.place'
            await b.js(f"(()=>{{const e=document.querySelector({json.dumps(selector)});e.value='\\nkeep@poster.place';e.setSelectionRange(0,0);e.dispatchEvent(new Event('input'));}})()")
            assert await b.js("document.querySelectorAll('[role=listbox]:not(.hidden)').length")==0
        await b.js(f"(()=>{{const e=document.querySelector({json.dumps(selector)});e.value='192.168.0.5';e.dispatchEvent(new Event('input'));}})()")
        assert await b.js(f"document.querySelector({json.dumps(selector)}).value")=='192.168.0.5'
        await type_query(b,selector,'same')
        await b.js(f"window.__retiredPickerInput=document.querySelector({json.dumps(selector)})")
        if surface=='remote':
            await b.js("document.querySelector('.osw-remote [data-w=close]').click()")
        else:
            await b.js("__PC.switchView('messages')")
        assert await b.js("document.querySelectorAll('.pc-user-options').length")==0
        assert await b.js("!__retiredPickerInput.hasAttribute('aria-controls')")

    asyncio.run(desktop.with_browser('online','',check,EXTRA))
