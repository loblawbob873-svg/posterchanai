"""A superseded capture/ICE request cannot alter the replacement call or send an invite."""
from pathlib import Path
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]
APP = (ROOT / 'static/js/client/app.js').read_text()
START = APP[APP.index('  async function startCall('):APP.index('  const _remoteDesktopResolved=')]


@pytest.mark.parametrize('scenario', [
    'capture_resolved_after_replacement',
    'capture_rejected_after_replacement',
    'ice_resolved_after_replacement',
    'configuration_cancelled',
    'configuration_pending_then_success',
])
def test_remote_desktop_start_preserves_call_identity_and_configuration_gate(tmp_path, scenario):
    driver = tmp_path / 'start-call.cjs'
    driver.write_text('''
const assert=require('node:assert/strict');
const scenario=process.argv[2];
function deferred(){let resolve,reject;const promise=new Promise((a,b)=>{resolve=a;reject=b});return {promise,resolve,reject};}
const capture=deferred(),ice=deferred(),configuration=deferred();
const tick=()=>new Promise(r=>setImmediate(r));
let _call=null,iceRequests=0,configRequests=0,teardowns=0,createdPeers=0,stops=0;
const invites=[],notices=[];
const GUEST=false,ME={pubkey:'host'},Relay={};
const _rid=()=> 'call-original',normalizeRelay=x=>x,_callUI=()=>{};
const toast=message=>notices.push(message),_mediaErrMsg=e=>e.message;
const _getMedia=()=>capture.promise;
const _rdWatchScreen=()=>{};
const _rdConfigureNative=()=>{configRequests++;return configuration.promise};
const _fetchIceServers=()=>{iceRequests++;return ice.promise};
const track={kind:'video',readyState:'live',stop(){stops++;this.readyState='ended'}};
const stream={getTracks:()=>[track]};
const _callTeardown=()=>{teardowns++;_call=null};
const _hangup=()=>_callTeardown();
const _rdWireControl=()=>{},_rdTuneSender=()=>{},_preferScreenCodec=()=>{},_preferH264=()=>{};
const _newPc=()=>{createdPeers++;return {
  addTrack:()=>({}),createDataChannel:()=>({}),
  createOffer:async()=>({type:'offer',sdp:'screen-offer'}),
  async setLocalDescription(offer){this.localDescription=offer},
}};
const _callSend=async(peer,message)=>invites.push({peer,message});
const setTimeout=()=>1;
function replaceCall(){
  const replacement={id:'call-replacement',peer:'different-peer',local:{marker:'new-media'},pc:{marker:'new-pc'},state:'calling'};
  _call=replacement;return replacement;
}
''' + START + '''
(async()=>{
  const pending=startCall('viewer',{remoteDesktop:true});
  const original=_call;
  assert.equal(original.id,'call-original');
  let replacement;
  if(scenario==='capture_resolved_after_replacement'||scenario==='capture_rejected_after_replacement'){
    replacement=replaceCall();
    // Resolve downstream stubs too, so old unguarded implementations fail assertions rather
    // than leaving an unresolved Promise that allows Node to exit without checking anything.
    configuration.resolve(true);ice.resolve({iceServers:[]});
    if(scenario==='capture_rejected_after_replacement')capture.reject(Error('picker cancelled'));
    else capture.resolve(stream);
    await pending;
    assert.equal(configRequests,0);assert.equal(iceRequests,0);
    assert.equal(stops,scenario==='capture_resolved_after_replacement'?1:0);
    assert.equal(notices.length,0);
  }else{
    capture.resolve(stream);await tick();
    assert.equal(configRequests,1);
    assert.equal(iceRequests,0,'native monitor choice must finish before ICE/signaling');
    assert.equal(createdPeers,0);assert.equal(invites.length,0);
    if(scenario==='configuration_cancelled'){
      configuration.resolve(false);await pending;
      assert.equal(stops,1);assert.equal(teardowns,1);assert.equal(_call,null);
      assert.equal(iceRequests,0);assert.equal(createdPeers,0);
    }else{
      configuration.resolve(true);await tick();assert.equal(iceRequests,1);
      if(scenario==='ice_resolved_after_replacement')replacement=replaceCall();
      ice.resolve({iceServers:[]});await pending;
      if(replacement)assert.equal(stops,1);
      else{
        assert.equal(_call,original);assert.equal(createdPeers,1);assert.equal(stops,0);
        assert.equal(invites.length,1);
        assert.equal(invites[0].peer,'viewer');
        assert.equal(invites[0].message.callId,'call-original');
        assert.equal(invites[0].message.sdp,'screen-offer');
      }
    }
  }
  if(replacement){
    assert.equal(_call,replacement,'old async work must not replace or teardown the new call');
    assert.deepEqual(replacement.local,{marker:'new-media'});
    assert.deepEqual(replacement.pc,{marker:'new-pc'});
    assert.equal(teardowns,0);assert.equal(createdPeers,0);
  }
  if(scenario!=='configuration_pending_then_success')assert.equal(invites.length,0);
  console.log('assertions-complete');
})().catch(e=>{console.error(e);process.exitCode=1});
''')
    result = subprocess.run(['node', str(driver), scenario], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip() == 'assertions-complete', 'driver exited before finishing async assertions'
