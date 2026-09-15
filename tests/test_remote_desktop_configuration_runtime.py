"""Run host coordination through delayed native selection and live screen switches."""
from pathlib import Path
import subprocess
import pytest

ROOT = Path(__file__).resolve().parents[1]
APP = (ROOT / 'static/js/client/app.js').read_text()
HELPERS = APP[APP.index('  async function _rdConfigureNative('):APP.index('  function _rdVideoPoint(')]

@pytest.mark.parametrize('scenario', ['pending', 'cancel', 'replaced', 'ended', 'switch', 'switch_cancel', 'replace_failure', 'switch_ended', 'switch_picker_cancel', 'view_only'])
def test_native_mapping_must_finish_before_control_or_video_switch(tmp_path, scenario):
    driver = tmp_path / 'host.cjs'
    driver.write_text('''const assert=require('node:assert/strict');
let _call, resolveConfig, resolveReplace, sent=[], notices=[], releases=0, hungup=0;
const window={pcRemoteControl:{configure:()=>new Promise(r=>resolveConfig=r)}};
const pcRemoteControl=window.pcRemoteControl;
const _rdSend=m=>sent.push(m), _rdReleaseNative=()=>releases++;
const toast=m=>notices.push(m), _callUI=()=>{}, _hangup=()=>{hungup++;_call=null}, _mediaErrMsg=()=>'';
const ME={pubkey:'self'};
function stream(){const t={kind:'video',readyState:'live',getSettings:()=>({width:3840,height:2560}),addEventListener(){},stop(){this.readyState='ended'}};return {getVideoTracks:()=>[t],getTracks:()=>[t]};}
let old=stream(), next=stream(), replaced=0;
const sender={track:old.getTracks()[0],replaceTrack:()=>{replaced++;return new Promise(r=>resolveReplace=r)},getParameters:()=>({}),setParameters:async()=>{}};
const navigator={mediaDevices:{getDisplayMedia:async()=>next}};
_call={remoteDesktop:true,caller:true,local:old,controlGranted:true,pc:{getSenders:()=>[sender]}};
''' + HELPERS + '\n' + '''
(async()=>{
const scenario=process.argv[2];
if(!scenario.startsWith('switch')&&scenario!=='replace_failure'){
  const session=_call, pending=_rdConfigureNative(old);
  assert.equal(session.nativeReady,false);_rdGrant(true);assert(!sent.some(m=>m.t==='grant'&&m.on));
  if(scenario==='replaced')_call={remoteDesktop:true,caller:true};
  if(scenario==='ended')old.getTracks()[0].stop();
  resolveConfig({ok:scenario!=='cancel',control:scenario==='view_only'?false:true});
  const ok=await pending;
  assert.equal(ok,scenario==='pending'||scenario==='view_only');
  if(scenario==='view_only'){assert.equal(session.nativeReady,false);assert(sent.some(m=>m.t==='geometry'));return;}
  if(ok){_rdGrant(true);assert(sent.some(m=>m.t==='grant'&&m.on));}
  else {assert(!sent.some(m=>m.t==='geometry'));assert.equal(session.nativeReady,false);}
}else{
  if(scenario==='switch_picker_cancel')navigator.mediaDevices.getDisplayMedia=async()=>{throw {name:'NotAllowedError'}};
  if(scenario==='replace_failure')sender.replaceTrack=async()=>{throw Error('replace failed')};
  const session=_call,pending=_rdSwitchScreen();
  await new Promise(r=>setImmediate(r));
  assert.equal(releases,1);assert.equal(session.controlGranted,false);assert.equal(replaced,0);
  _rdGrant(true);assert.equal(session.controlGranted,false);
  if(scenario==='switch_picker_cancel'){await pending;assert.equal(session.local,old);assert.equal(session.nativeReady,false);assert.equal(session.nativeSwitching,false);return;}
  resolveConfig({ok:scenario!=='switch_cancel'});
  await new Promise(r=>setImmediate(r));
  if(scenario==='switch'||scenario==='switch_ended'){
    assert.equal(replaced,1);assert.equal(session.nativeReady,false);
    _rdGrant(true);assert.equal(session.controlGranted,false);assert.equal(old.getTracks()[0].readyState,'live');
    if(scenario==='switch_ended')next.getTracks()[0].stop();
    resolveReplace();await pending;
    if(scenario==='switch_ended'){assert.equal(hungup,1);assert.equal(session.nativeReady,false);assert.equal(session.controlGranted,false);return;}
    assert.equal(session.local,next);assert.equal(old.getTracks()[0].readyState,'ended');
    assert.equal(session.nativeReady,true);_rdGrant(true);assert.equal(session.controlGranted,true);
  }else{
    await pending;assert.equal(session.local,old);assert.equal(session.nativeReady,false);
    assert.equal(old.getTracks()[0].readyState,'live');assert.equal(next.getTracks()[0].readyState,'ended');
    _rdGrant(true);assert.equal(session.controlGranted,false);
  }
  assert.equal(session.nativeSwitching,false);
}
})().then(()=>console.log('ASSERTIONS_COMPLETED')).catch(e=>{console.error(e);process.exitCode=1});
''')
    result = subprocess.run(['node', str(driver), scenario], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'ASSERTIONS_COMPLETED' in result.stdout, 'Node exited before awaited checks completed'
