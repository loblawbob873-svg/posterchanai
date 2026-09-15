"""Call migration/rotation races with real DOM and isolated SFU/signer boundaries."""
import asyncio
from pathlib import Path
import pytest
from tests.client import test_desktop_offline_full_app as desktop
ROOT=Path(__file__).resolve().parents[2]

@pytest.fixture(scope='module',autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()

@pytest.mark.parametrize('scenario',['occupied','rotation','departure','camera_close','terminal','unverified','replacement'])
def test_call_lifecycle_races(scenario):
    async def check(b):
        await desktop.login(b)
        await b.js((ROOT/'static/js/client/cord-voice.js').read_text())
        await b.js((ROOT/'static/js/client/cord-call.js').read_text())
        await b.js(r'''(()=>{
          window.__logs=[];window.__ticks=new Map();window.__now=Date.now();Date.now=()=>__now;
          window.setInterval=(fn,ms)=>{__ticks.set(ms,fn);return ms;};window.clearInterval=ms=>__ticks.delete(ms);
          window.__epoch=0;window.__holdToken=false;window.__holdLeft=false;
          window.Worker=class{terminate(){__logs.push('terminate')}};
          class Provider{onSetEncryptionKey(){}}
          class Room{constructor(){window.__room=this;this.handlers=new Map();this.remoteParticipants=new Map();this.localParticipant={identity:'fixture',setMicrophoneEnabled:async()=>__logs.push('mic'),setCameraEnabled:async()=>{if(window.__holdCamera)await new Promise(r=>window.__releaseCamera=r);__logs.push('camera');}};}on(name,handler){this.handlers.set(name,handler);return this;}async setE2EEEnabled(){}async connect(){__logs.push('connect');}async disconnect(){__logs.push('disconnect');if(window.__holdDisconnect){__holdDisconnect=false;await new Promise(r=>window.__releaseDisconnect=r);}}}
          window.LivekitClient={BaseKeyProvider:Provider,Room,RoomEvent:{ParticipantConnected:'participant',ParticipantDisconnected:'gone',TrackSubscribed:'track',TrackUnsubscribed:'untrack',EncryptionError:'error',Disconnected:'disconnected'},isE2EESupported:()=>true};
          window.PCCordVoice={...PCCordVoice,brokers:async(_room,values)=>[...new Set(values)].sort(),
            senderKey:async(root)=>{__logs.push('key:'+root[0]);return new Uint8Array(32).fill(root[0]);},
            token:async(material,broker)=>{__logs.push('token:'+material.room[0]+':'+broker);if(__holdToken){__holdToken=false;await new Promise(r=>window.__releaseToken=r);}return {identity:'fixture',token:'fixture',url:'wss://fixture.invalid',broker};}};
          window.__context={current:()=>true,preferred:'https://a.invalid',
            material:()=>({room:(__epoch?'b':'a').repeat(64),mediaRoot:new Uint8Array(32).fill(__epoch?4:3)}),
            subscribe:receive=>{window.__receive=receive;return ()=>__logs.push('unsubscribe');},decode:async wrap=>wrap,
            presence:async verb=>{__logs.push(verb);if(verb==='left'&&__holdLeft)await new Promise(()=>{});}};
          window.__presence=(broker)=>[{pubkey:'c'.repeat(64),id:'d'.repeat(64),at:__now,content:'joined',tags:[['identity','remote'],['broker',broker]]}];
          return PCCordCall.open(__context);
        })()''')
        if scenario=='occupied':
            await b.js("__receive(__presence('https://z.invalid'))")
        elif scenario=='rotation':
            await b.js('__holdToken=true')
        elif scenario=='departure':
            await b.js("document.querySelector('[data-call=broker]').value='https://z.invalid'")
        await b.js("__now+=31000;__ticks.get(1000)();document.querySelector('[data-call=join]').click()")
        if scenario=='rotation':
            await b.until('!!window.__releaseToken')
            await b.js('__epoch=1;__ticks.get(2000)();__releaseToken()')
        await b.until("document.querySelector('.pc-cord-call [role=status]').textContent.includes('Connected')")
        if scenario=='occupied':
            assert await b.js("__logs.find(s=>s.startsWith('token:'))")=='token:a:https://z.invalid'
        elif scenario=='rotation':
            log=await b.js('__logs')
            assert 'key:3' not in log, 'stale token must never install old epoch media keys'
            assert 'key:4' in log and log.count('mic')==1
            assert any(s.startswith('token:b:') for s in log), 'rotation must queue a fresh join'
        elif scenario=='departure':
            await b.js("__holdLeft=true;__receive(__presence('https://a.invalid'))")
            await b.until("__logs.filter(x=>x==='connect').length===2")
            log=await b.js('__logs')
            assert log.index('left')<log.index('disconnect')<len(log)-1
            assert 'terminate' in log, 'stalled departure cannot retain old capture resources'
        elif scenario=='camera_close':
            await b.js("__holdCamera=true;document.querySelector('[data-call=video]').click()")
            await b.until('!!window.__releaseCamera')
            await b.js('PCCordCall.close()')
            await b.js('__releaseCamera()')
            await b.until("__logs.filter(x=>x==='disconnect').length===2")
        elif scenario=='terminal':
            await b.js("__room.handlers.get('disconnected')()")
            await b.until("__logs.includes('terminate')")
            assert not await b.js('__ticks.has(30000)'), 'terminal disconnect must stop heartbeat'
            assert await b.js("document.querySelector('[data-call=mute]').disabled")
        elif scenario=='replacement':
            await b.js("__holdDisconnect=true;window.__openA=PCCordCall.open({...__context,name:'Replacement A'});void 0")
            await b.until('!!window.__releaseDisconnect')
            await b.js("window.__openB=PCCordCall.open({...__context,name:'Replacement B'});__releaseDisconnect()")
            await b.js('Promise.all([__openA,__openB])')
            assert await b.js("document.querySelectorAll('.pc-cord-call').length")==1
            assert await b.js("document.querySelector('.pc-cord-call strong').textContent")=='Replacement B'
        elif scenario=='unverified':
            await b.js(r'''(async()=>{
              __room.remoteParticipants.set('remote',{identity:'remote'});
              const track={attach:()=>{__logs.push('attach');return document.createElement('audio');},detach:()=>{__logs.push('detach');return [...document.querySelectorAll('[data-call=media] audio')];}};
              await __room.handlers.get('track')(track,{}, {identity:'remote'});
            })()''')
            assert not await b.js("__logs.includes('attach')")
            await b.js("__receive(__presence('https://a.invalid'))")
            await b.until("__logs.includes('attach')")
            await b.js("__receive(__presence('https://a.invalid').map(e=>({...e,pubkey:'e'.repeat(64)})))")
            await b.until("document.querySelector('[data-call=media]').children.length===0")
            assert await b.js("document.querySelector('[data-call=people]').textContent")=='Unverified participant'
        await b.js('PCCordCall.close()')
        await b.until("!document.querySelector('.pc-cord-call')")
    asyncio.run(desktop.with_browser('online','',check))
