"""Bundled client live delivery using real login, Relay, DOM and signed outer events.

Only HTTP/WebSocket and CORD decryption boundaries are fixtures; no public sends.
"""
import asyncio
from pathlib import Path
import pytest
from tests.client import test_desktop_offline_full_app as desktop

@pytest.fixture(scope='module',autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()

@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(),reason='Chrome required')
def test_overlapping_live_batches_survive_view_changes_without_history_reload():
    async def check(b):
        await desktop.login(b)
        await b.js(r'''(()=>{
          window.__openedBatches=[];window.__releaseBatches={};
          window.__streamKey=new Uint8Array(32).fill(3);
          const pk=NostrTools.getPublicKey(__streamKey);
          const channels=[{id:'fixture-general',name:'general',streamPubkeys:[pk]},
            {id:'fixture-other',name:'other',streamPubkeys:['d'.repeat(64)]}];
          const room={name:'Live fixture',communityId:'c'.repeat(64),naddr:'fixture-community',
            channels,cord:{bundle:{relays:['wss://fixture.invalid']},hydrated:true}};
          localStorage.setItem('pc.concord.invites',JSON.stringify([room]));localStorage.setItem('pc.concord.active','0');
          window.PosterCordReader={inspectControl:()=>({controlPubkeys:[],channels}),
            inspectChat:async(_bundle,_control,_channel,wraps)=>{
              const messages=(wraps||[]).filter(w=>['slow','fast'].includes(w.content)).map(w=>({id:w.id,pubkey:w.pubkey,text:'Live '+w.content,at:w.created_at*1000,kind:9,tags:[]}));
              if(messages.length){const name=messages.at(-1).text;if(!__openedBatches.includes(name)){__openedBatches.push(name);await new Promise(resolve=>{__releaseBatches[name]=resolve})}}
              return{messages,reactions:[],reactionUrls:[]};
            }};
          window.__pushLive=name=>{
            const ev=NostrTools.finalizeEvent({kind:1059,created_at:Math.floor(Date.now()/1000),content:name,tags:[]},__streamKey);
            for(const socket of __sockets)if(socket.readyState===1)for(const [id,filters]of socket.__reqs||[])
              if(filters.some(f=>f.kinds?.includes(1059)&&f.authors?.includes(pk)))socket.fire('message',['EVENT',id,ev]);
          };
          __PC.switchMessagesTab('concord');
        })()''')
        await b.until("!!document.querySelector('#cc-input') && __sockets.some(s=>[...(s.__reqs||[])].some(([id,fs])=>fs.some(f=>f.kinds?.includes(1059)&&!f.limit)))")
        await b.js("__pushLive('slow')")
        await b.until("!!__releaseBatches['Live slow']")
        await b.js("__pushLive('fast')")
        # Accept either concurrent or serialized decryption; both transport arrivals must survive.
        await asyncio.sleep(.5)
        if await b.js("!!__releaseBatches['Live fast']"):
            await b.js("__releaseBatches['Live fast']()")
            await b.until("document.querySelector('.cc-messages')?.innerText.includes('Live fast')")
            await b.js("__releaseBatches['Live slow']()")
        else:
            await b.js("__releaseBatches['Live slow']()")
            await b.until("!!__releaseBatches['Live fast']")
            await b.js("__releaseBatches['Live fast']()")
        await b.until("document.querySelector('.cc-messages')?.innerText.includes('Live fast')")
        await b.until("document.querySelector('.cc-messages')?.innerText.includes('Live slow')")
        assert await b.js("document.querySelector('.cc-messages').innerText.includes('Live fast')"),'late decryption erased newer live arrival'
        token=await b.js('__documentIdentity')
        await b.js("document.querySelector('[data-cc-channel=other]').click()")
        await b.until("document.querySelector('.cc-channel.active')?.dataset.ccChannel==='other'")
        assert not await b.js("document.querySelector('.cc-messages')?.innerText.includes('Live fast')")
        await b.js("document.querySelector('[data-cc-channel=general]').click()")
        await b.until("document.querySelector('.cc-messages')?.innerText.includes('Live fast')")
        assert await b.js("document.querySelector('.cc-messages').innerText.includes('Live slow')")
        assert await b.js('__documentIdentity')==token
    extra=r'''
localStorage.setItem('pc_nostr_settings',JSON.stringify({...JSON.parse(localStorage.getItem('pc_nostr_settings')||'{}'),osMode:false}));
const sendSocket=WebSocket.prototype.send;
WebSocket.prototype.send=function(raw){const m=JSON.parse(raw);this.__reqs??=new Map();
 if(m[0]==='REQ')this.__reqs.set(m[1],m.slice(2));if(m[0]==='CLOSE')this.__reqs.delete(m[1]);return sendSocket.call(this,raw)};
'''
    asyncio.run(desktop.with_browser('online','',check,extra))
