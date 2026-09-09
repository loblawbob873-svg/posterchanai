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

@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(),reason='Chrome required')
def test_active_plane_rearms_before_held_history_and_receives_after_reconnect():
    async def check(b):
        await desktop.login(b)
        await b.js(r'''(()=>{
          window.__planeKeys=[new Uint8Array(32).fill(4),new Uint8Array(32).fill(5)];
          window.__planeIndex=0;window.__planeChannels=[{id:'active-plane',name:'general',streamPubkeys:[NostrTools.getPublicKey(__planeKeys[0])]}];
          const room={name:'Active plane fixture',communityId:'a'.repeat(64),channels:__planeChannels,cord:{bundle:{relays:['wss://plane.fixture.invalid']},hydrated:true}};
          localStorage.setItem('pc.concord.invites',JSON.stringify([room]));localStorage.setItem('pc.concord.active','0');
          window.PosterCordReader={inspectControl:()=>({controlPubkeys:[],channels:__planeChannels}),
            createPlaneAuth:(_b,_c,author)=>({pubkey:author,sign:tpl=>NostrTools.finalizeEvent(tpl,__planeKeys.find(k=>NostrTools.getPublicKey(k)===author))}),
            inspectChat:async(_b,_c,_id,wraps)=>({messages:wraps.map(w=>({id:w.id,pubkey:w.pubkey,text:w.content,at:w.created_at*1000,kind:9,tags:[]})),reactions:[],reactionUrls:[]})};
          const query=__PC.relayQueryFrom;
          __PC.relayQueryFrom=(urls,filters,opts)=>__holdPlaneHistory&&filters.some(f=>f.kinds?.includes(1059))?new Promise(resolve=>{__releasePlaneHistory=resolve;__planeHistoryHeld=true}):query(urls,filters,opts);
          window.__pushPlane=text=>{
            const key=__planeKeys[__planeIndex],pk=NostrTools.getPublicKey(key),ev=NostrTools.finalizeEvent({kind:1059,created_at:Math.floor(Date.now()/1000),content:text,tags:[]},key);
            for(const s of __sockets)if(s.readyState===1)for(const[id,filters]of s.__reqs||[])if(filters.some(f=>!f.limit&&f.authors?.includes(pk)))s.fire('message',['EVENT',id,ev]);
          };
          window.__holdPlaneHistory=false;
          __PC.switchMessagesTab('concord');
        })()''')
        await b.until("!!document.querySelector('#cc-input') && __sockets.some(s=>s.readyState===1&&[...(s.__reqs||[])].some(([id,fs])=>fs.some(f=>!f.limit&&f.authors?.includes(NostrTools.getPublicKey(__planeKeys[0])))))")
        await b.js("__pushPlane('Before refresh')")
        await b.until("document.querySelector('.cc-messages')?.innerText.includes('Before refresh')")
        token=await b.js('__documentIdentity')
        await b.js(r'''(()=>{
          __planeIndex=1;__planeChannels[0].streamPubkeys=[NostrTools.getPublicKey(__planeKeys[1])];
          const rooms=JSON.parse(localStorage.getItem('pc.concord.invites'));rooms[0].channels=__planeChannels;rooms[0].cord.bundle.fixtureEpoch=1;
          localStorage.setItem('pc.concord.invites',JSON.stringify(rooms));
          PCConcord.__testState({controls:[rooms[0].communityId,[{id:'new-verified-control-generation'}]]});
          __holdPlaneHistory=true;void PCConcord.refreshActiveChannel(__PC);
        })()''')
        await b.until("window.__planeHistoryHeld===true && __sockets.some(s=>s.readyState===1&&[...(s.__reqs||[])].some(([id,fs])=>fs.some(f=>!f.limit&&f.authors?.includes(NostrTools.getPublicKey(__planeKeys[1])))))")
        await b.js("__pushPlane('After rekey while history waits')")
        await b.until("document.querySelector('.cc-messages')?.innerText.includes('After rekey while history waits')")
        assert await b.js("document.querySelector('.cc-channel.active')?.dataset.ccChannel")== 'general'
        await b.js("for(const s of __sockets)if(s.url==='wss://plane.fixture.invalid'&&s.readyState===1)s.close()")
        await b.until("__sockets.some(s=>s.url==='wss://plane.fixture.invalid'&&s.readyState===1&&[...(s.__reqs||[])].some(([id,fs])=>fs.some(f=>!f.limit&&f.authors?.includes(NostrTools.getPublicKey(__planeKeys[1])))))")
        await b.js("__pushPlane('After reconnect without switching');__holdPlaneHistory=false;__releasePlaneHistory([])")
        await b.until("document.querySelector('.cc-messages')?.innerText.includes('After reconnect without switching')")
        assert await b.js('__documentIdentity')==token
    extra=r'''
localStorage.setItem('pc_nostr_settings',JSON.stringify({...JSON.parse(localStorage.getItem('pc_nostr_settings')||'{}'),osMode:false}));
const planeSend=WebSocket.prototype.send;
WebSocket.prototype.send=function(raw){const m=JSON.parse(raw);this.__reqs??=new Map();if(m[0]==='REQ')this.__reqs.set(m[1],m.slice(2));if(m[0]==='CLOSE')this.__reqs.delete(m[1]);return planeSend.call(this,raw)};
'''
    asyncio.run(desktop.with_browser('online','',check,extra))
