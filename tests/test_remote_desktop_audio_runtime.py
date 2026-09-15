"""Shared sound is transported, replaced, and recovered after phone autoplay rejection."""
from pathlib import Path
import json
import subprocess
import pytest
from tests.test_remote_desktop_alignment_runtime import browser_page

ROOT=Path(__file__).resolve().parents[1]
APP=(ROOT/'static/js/client/app.js').read_text()


def test_capture_requests_system_sound_and_never_viewer_microphone(tmp_path):
    code=APP[APP.index('  function _getMedia('):APP.index('  function _hasLiveVideo(')]
    driver=tmp_path/'capture.cjs'
    driver.write_text('''const assert=require('node:assert/strict');
let display=[],microphones=0;const MediaStream=class{};
const navigator={mediaDevices:{getDisplayMedia:async o=>{display.push(o);return 'screen'},getUserMedia:()=>{microphones++;throw Error('mic')}}};
'''+code+'''
(async()=>{assert.equal(await _getMedia(true,true,false),'screen');assert.deepEqual(display[0].audio,{echoCancellation:false,noiseSuppression:false,autoGainControl:false});assert.equal(display[0].systemAudio,'include');
assert(await _getMedia(true,false,true) instanceof MediaStream);assert.equal(microphones,0);console.log('COMPLETE')})().catch(e=>{console.error(e);process.exitCode=1});''')
    r=subprocess.run(['node',str(driver)],capture_output=True,text=True,timeout=10)
    assert r.returncode==0,r.stderr
    assert 'COMPLETE' in r.stdout


@pytest.mark.parametrize('mode',['replace','remove','add','failure','stale','ended'])
def test_screen_switch_replaces_audio_or_stops_on_failure(tmp_path,mode):
    code=APP[APP.index('  async function _rdSwitchScreen('):APP.index('  function _rdWireControl(')]
    driver=tmp_path/'switch.cjs'
    driver.write_text('''const assert=require('node:assert/strict');const mode=process.argv[2];
const track=kind=>({kind,readyState:'live',stop(){this.readyState='ended'}});
const oldVideo=track('video'),oldAudio=track('audio'),newVideo=track('video'),newAudio=track('audio');
const stream=tracks=>({getTracks:()=>tracks,getVideoTracks:()=>tracks.filter(t=>t.kind==='video')});
const old=stream([oldVideo,oldAudio]),next=stream(mode==='remove'?[newVideo]:[newVideo,newAudio]);
let replaced=[],added=[],negotiations=0,hungup=0,resolveAudio;
const videoSender={track:oldVideo,replaceTrack:async()=>{}};
const audioSender={track:oldAudio,replaceTrack:async t=>{replaced.push(t);if(mode==='failure')throw Error('audio');if(mode==='stale'||mode==='ended')await new Promise(r=>resolveAudio=r)}};
let _call={remoteDesktop:true,caller:true,local:old,pc:{getSenders:()=>mode==='add'?[videoSender]:[videoSender,audioSender],addTrack:(t,s)=>{assert.equal(s,old);added.push(t);return {track:t}}}};
const session=_call,navigator={mediaDevices:{getDisplayMedia:async opts=>{assert.deepEqual(opts.audio,{echoCancellation:false,noiseSuppression:false,autoGainControl:false});return next}}};
const _rdGrant=()=>{},toast=()=>{},_mediaErrMsg=()=>'',_rdConfigureNative=async()=>true,_rdTuneSender=async()=>{},_rdWatchScreen=()=>{},_callUI=()=>{},_hangup=()=>{hungup++;old.getTracks().forEach(t=>t.stop());_call=null};
const _renegotiate=async active=>{assert.equal(active,session);negotiations++;return true};
'''+code+'''
(async()=>{const pending=_rdSwitchScreen();
if(mode==='stale'||mode==='ended'){while(!resolveAudio)await new Promise(r=>setImmediate(r));if(mode==='stale')_call={replacement:true};else newVideo.stop();resolveAudio();}
await pending;
if(mode==='failure'||mode==='ended'){assert.equal(hungup,1);assert.equal(newAudio.readyState,'ended');assert.equal(oldAudio.readyState,'ended');}
else if(mode==='stale'){assert(_call.replacement);assert.equal(newAudio.readyState,'ended');}
else{assert.equal(session.local,next);assert.equal(oldAudio.readyState,'ended');assert.equal(newVideo.readyState,'live');
if(mode==='add'){assert.deepEqual(added,[newAudio]);assert.equal(negotiations,1)}
else assert.deepEqual(replaced,[mode==='remove'?null:newAudio]);}
console.log('COMPLETE')})().catch(e=>{console.error(e);process.exitCode=1});''')
    r=subprocess.run(['node',str(driver),mode],capture_output=True,text=True,timeout=10)
    assert r.returncode==0,r.stderr
    assert 'COMPLETE' in r.stdout


def test_phone_can_enable_sound_after_autoplay_blocks_it(tmp_path):
    code=APP[APP.index('  function _rdPlayRemote('):APP.index('  // Shrink a call/room overlay')]
    with browser_page(tmp_path) as page:
        page.resize(390,844)
        page.evaluate('''document.body.innerHTML='';
window._call={remoteDesktop:true,caller:false,remote:new MediaStream(),state:'connected',peer:'test'};
const audio=new AudioContext(),dest=audio.createMediaStreamDestination();_call.remote.addTrack(dest.stream.getAudioTracks()[0]);
window.LOGO='';window.profOf=()=>({name:'Laptop'});window._callStatus=()=> 'connected';window._hasLiveVideo=()=>true;
for(const n of ['_callWake','_callService','_ringtone','_dragSelfView','_placeSelfView','_rdBindViewer'])window[n]=()=>{};
window._rdEnsureHost=()=>document.body;window._callSvcName=()=> 'Laptop';
window._rdSwitchScreen=()=>{};window.playCalls=[];window.blockAudio=true;
HTMLMediaElement.prototype.play=function(){playCalls.push(this.muted);return blockAudio&&!this.muted?Promise.reject(new DOMException('autoplay','NotAllowedError')):Promise.resolve()};''')
        page.evaluate(code+'\n_callUI();')
        page.evaluate('new Promise(r=>setTimeout(r,20))')
        assert page.evaluate("document.querySelector('#call-sound').parentElement.textContent.includes('Enable sound')")
        assert page.evaluate("document.querySelector('#call-remote').muted")
        page.evaluate("blockAudio=false;document.querySelector('#call-sound').click()")
        page.evaluate('new Promise(r=>setTimeout(r,20))')
        assert not page.evaluate("document.querySelector('#call-remote').muted")
        assert page.evaluate("document.querySelector('#call-sound').parentElement.textContent.includes('Mute sound')")
        page.evaluate("document.querySelector('#call-sound').click()")
        assert page.evaluate("document.querySelector('#call-remote').muted")


@pytest.mark.parametrize('initial_audio',[True,False])
def test_shipped_host_sends_generated_system_sound_over_real_webrtc(tmp_path,initial_audio):
    capture=APP[APP.index('  function _getMedia('):APP.index('  function _hasLiveVideo(')]
    start=APP[APP.index('  async function startCall('):APP.index('  const _remoteDesktopResolved=')]
    switch=APP[APP.index('  async function _rdSwitchScreen('):APP.index('  function _rdWireControl(')]
    renegotiate=APP[APP.index('  async function _renegotiate('):APP.index('  // Turn the camera')]
    with browser_page(tmp_path) as page:
        page.evaluate('''window._call=null;window.GUEST=false;window.ME={pubkey:'host'};window.Relay={};
window._rid=()=> 'sound-test';window.normalizeRelay=x=>x;window._callUI=()=>{};
window.toast=m=>{throw Error(m)};window._mediaErrMsg=e=>e.message;
for(const n of ['_rdWatchScreen','_rdWireControl','_rdTuneSender','_preferScreenCodec','_preferH264'])window[n]=()=>{};
window._rdConfigureNative=async()=>true;window._fetchIceServers=async()=>({iceServers:[]});
window.ctx=new AudioContext();window.tone=ctx.createOscillator();window.destination=ctx.createMediaStreamDestination();
tone.frequency.value=440;tone.connect(destination);tone.start();
window.canvas=document.createElement('canvas');canvas.width=320;canvas.height=180;
canvas.getContext('2d').fillRect(0,0,320,180);
window.source=canvas.captureStream(5);source.addTrack(destination.stream.getAudioTracks()[0]);
Object.defineProperty(navigator,'mediaDevices',{value:{getDisplayMedia:async opts=>{if(!opts.audio)throw Error('audio not requested');return source},getUserMedia:()=>{throw Error('unexpected microphone')}}});
window.receiver=new RTCPeerConnection();window.received=new MediaStream();window.signals=[];
receiver.ontrack=e=>{received.addTrack(e.track);window.lastRemote=e.streams[0]};
window._rdGrant=()=>{};window.paintTimer=setInterval(()=>canvas.getContext('2d').fillRect(0,0,320,180),50);
window._newPc=()=>{const pc=new RTCPeerConnection();window.sender=pc;window.hostIce=[];window.viewerIce=[];
pc.onicecandidate=e=>{if(e.candidate){if(receiver.remoteDescription)receiver.addIceCandidate(e.candidate);else hostIce.push(e.candidate)}};
receiver.onicecandidate=e=>{if(e.candidate){if(pc.remoteDescription)pc.addIceCandidate(e.candidate);else viewerIce.push(e.candidate)}};return pc;};
window._callSend=async(peer,msg)=>{signals.push(msg.t);await receiver.setRemoteDescription({type:'offer',sdp:msg.sdp});for(const c of hostIce)await receiver.addIceCandidate(c);
await receiver.setLocalDescription(await receiver.createAnswer());await sender.setRemoteDescription(receiver.localDescription);for(const c of viewerIce)await sender.addIceCandidate(c);};''')
        if not initial_audio:
            page.evaluate('source.removeTrack(source.getAudioTracks()[0])')
        page.evaluate(capture+start+switch+renegotiate+'\nctx.resume().then(()=>startCall("viewer",{remoteDesktop:true}));')
        if not initial_audio:
            page.evaluate('''window.firstRemote=lastRemote;window.nextCapture=new MediaStream([source.getVideoTracks()[0].clone(),destination.stream.getAudioTracks()[0]]);
navigator.mediaDevices.getDisplayMedia=async()=>nextCapture;_rdSwitchScreen();''')
            assert page.evaluate("signals.join(',')==='invite,reoffer'")
            assert page.evaluate('lastRemote===firstRemote && lastRemote.getVideoTracks().length===1 && lastRemote.getAudioTracks().length===1')
        result=page.evaluate('''new Promise((resolve,reject)=>{
const deadline=Date.now()+7000;let analyser;
const poll=()=>{if(received.getAudioTracks().length&&!analyser){window.playout=new Audio();playout.srcObject=received;playout.play();analyser=ctx.createAnalyser();ctx.createMediaStreamSource(received).connect(analyser);analyser.connect(ctx.destination);}
if(analyser){const values=new Float32Array(analyser.fftSize);analyser.getFloatTimeDomainData(values);const rms=Math.sqrt(values.reduce((a,x)=>a+x*x,0)/values.length);if(rms>.05){resolve({rms,audio:received.getAudioTracks().length,video:received.getVideoTracks().length});return;}}
if(Date.now()>deadline){reject(Error('no decoded remote sound '+JSON.stringify({ctx:ctx.state,pc:sender.connectionState,tracks:received.getTracks().map(t=>({kind:t.kind,muted:t.muted}))})));return;}setTimeout(poll,25);};poll();})''')
        assert result['audio']==1 and result['video']==1
        assert result['rms']>.05
        page.evaluate("clearInterval(paintTimer);clearTimeout(_call.timeout);_call.local.getTracks().forEach(t=>t.stop());source.getTracks().forEach(t=>t.stop());sender.close();receiver.close();tone.stop();ctx.close()")


@pytest.mark.parametrize('phase',['offer','local'])
def test_audio_renegotiation_cannot_signal_a_replacement_call(tmp_path,phase):
    code=APP[APP.index('  async function _renegotiate('):APP.index('  // Turn the camera')]
    driver=tmp_path/'negotiate.cjs'
    driver.write_text('''const assert=require('node:assert/strict');const phase=process.argv[2];let unblock,sends=0;
const hold=()=>new Promise(r=>unblock=r);
const original={id:'old',peer:'old-peer',pc:{signalingState:'stable',createOffer:async()=>{if(phase==='offer')await hold();return {}},setLocalDescription:async()=>{if(phase==='local')await hold()},localDescription:{sdp:'old-sdp'}}};
let _call=original;const _callSend=async()=>{sends++};
'''+code+'''
(async()=>{const pending=_renegotiate(original);while(!unblock)await new Promise(r=>setImmediate(r));
_call={id:'new',makingOffer:true};unblock();assert.equal(await pending,false);assert.equal(sends,0);assert.equal(_call.makingOffer,true);assert.equal(original.makingOffer,false);console.log('COMPLETE')})().catch(e=>{console.error(e);process.exitCode=1});''')
    r=subprocess.run(['node',str(driver),phase],capture_output=True,text=True,timeout=10)
    assert r.returncode==0,r.stderr
    assert 'COMPLETE' in r.stdout
