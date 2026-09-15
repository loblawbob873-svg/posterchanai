"""Call controls use the real DOM; SFU and microphone boundaries are isolated fixtures."""
import asyncio
from pathlib import Path
import pytest
from tests.client import test_desktop_offline_full_app as desktop
from tests.client.test_concord_channel_controls_full_app import click
ROOT=Path(__file__).resolve().parents[2]

@pytest.fixture(scope='module',autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()

@pytest.mark.parametrize('width',[1280,390])
@pytest.mark.parametrize('supported',[True,False])
def test_call_controls_encrypt_before_microphone_and_clean_up(width,supported):
    async def check(b):
        await b.call('Emulation.setDeviceMetricsOverride',dict(width=width,height=850,deviceScaleFactor=1,mobile=width<600))
        await desktop.login(b)
        await b.js((ROOT/'static/js/client/cord-voice.js').read_text())
        await b.js((ROOT/'static/js/client/cord-call.js').read_text())
        await b.js(r'''(()=>{
          window.__callLog=[];window.__callNow=Date.now();Date.now=()=>__callNow;
          window.__callOwner=true;
          window.Worker=class{constructor(){__callLog.push('worker')}terminate(){__callLog.push('terminate')}};
          const provider=class{constructor(options){window.__keyOptions=options}onSetEncryptionKey(key,identity){__callLog.push('key:'+identity);window.__keyAlgorithm=key.algorithm.name}};
          const room=class{constructor(){__callLog.push('room');this.remoteParticipants=new Map();this.localParticipant={identity:'fixture-identity',setMicrophoneEnabled:async value=>__callLog.push('mic:'+value),setCameraEnabled:async value=>__callLog.push('camera:'+value),setScreenShareEnabled:async value=>{this.localParticipant.isScreenShareEnabled=value;__callLog.push('screen:'+value)}}}on(){return this}async setE2EEEnabled(value){__callLog.push('encrypted:'+value)}async connect(){__callLog.push('connect')}async disconnect(){__callLog.push('disconnect')}};
          window.LivekitClient={BaseKeyProvider:provider,Room:room,RoomEvent:{},isE2EESupported:()=>SUPPORTED};
          window.PCCordVoice={...PCCordVoice,token:async()=>({identity:'fixture-identity',token:'fixture',url:'wss://fixture.invalid',broker:'https://fixture.invalid'})};
          window.__callContext={name:'Fixture voice',current:()=>__callOwner,
            material:()=>({room:'a'.repeat(64),mediaRoot:new Uint8Array(32).fill(3)}),
            subscribe:()=>()=>__callLog.push('unsubscribe'),decode:async()=>[],
            presence:async verb=>__callLog.push(verb)};
          return PCCordCall.open(__callContext);
        })()'''.replace('SUPPORTED','true' if supported else 'false'))
        await b.js('__callNow+=31000')
        await b.until("!document.querySelector('[data-call=join]').disabled")
        await click(b,'[data-call=join]')
        if supported:
            await b.until("document.querySelector('.pc-cord-call [role=status]').textContent.includes('Connected')")
            log=await b.js('__callLog')
            assert log.index('key:fixture-identity')<log.index('encrypted:true')<log.index('connect')<log.index('mic:true')
            assert await b.js('__keyAlgorithm')=='HKDF'
            assert await b.js('__keyOptions')==dict(sharedKey=False,ratchetWindowSize=0,failureTolerance=-1,keySize=256)
            await click(b,'[data-call=mute]');await b.until("__callLog.includes('mic:false')")
            await click(b,'[data-call=video]');await b.until("__callLog.includes('camera:true')")
            await click(b,'[data-call=screen]');await b.until("__callLog.includes('screen:true')")
            assert await b.js("__callLog.indexOf('encrypted:true')<__callLog.indexOf('screen:true')")
            assert await b.js("document.querySelector('[data-call=screen]').textContent")=='Stop sharing'
            await click(b,'[data-call=screen]');await b.until("__callLog.includes('screen:false')")
            assert await b.js("document.querySelector('[data-call=screen]').textContent")=='Share screen'
        else:
            await b.until("document.querySelector('.pc-cord-call [role=status]').textContent.includes('cannot encrypt')")
            assert await b.js('__callLog')==[]
        await click(b,'[data-call=close]')
        await b.until("!document.querySelector('.pc-cord-call')")
        log=await b.js('__callLog')
        assert log.count('unsubscribe')==1
        if supported:
            assert log.count('disconnect')==1 and log.count('terminate')==1
            assert log.count('joined')==1 and log.count('left')==1
    asyncio.run(desktop.with_browser('online','',check))
