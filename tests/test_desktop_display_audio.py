"""Execute the shipped display grant handler: audio opt-in and origin cancellation."""
from pathlib import Path
import shutil
import subprocess

ROOT = Path(__file__).resolve().parents[1]


def test_display_audio_grants_and_picker_cancellation():
    source = (ROOT / 'desktop/main.js').read_text()
    handler = source[source.index('function wirePermissions()'):source.index('// ---- screen-source picker')]
    script = r'''
const vm=require('node:vm'),assert=require('node:assert/strict');
async function run(platform,audioRequested,{url='https://ours/client',cancel=false,navigate=false,destroy=false}={}) {
 let display,options,picks=0;
 const frame={url},source={id:'screen:1',display_id:'1'};
 const context={process:{platform},console,session:{defaultSession:{
  setPermissionRequestHandler(){},setPermissionCheckHandler(){},
  setDisplayMediaRequestHandler(fn,opts){display=fn;options=opts;}}},
  isOurs:url=>url.startsWith('https://ours/'),isWebxdcSandbox:()=>false,
  remoteCaptureGeneration:0,remoteControlDisplayId:'',remoteControlDisplayExplicit:false,
  screenLog(){},pickScreenSource:async()=>{picks++;if(navigate)frame.url='https://evil/';
    if(destroy)Object.defineProperty(frame,'url',{get(){throw Error('destroyed frame')}});
    return cancel?null:source;}};
 vm.runInNewContext(HANDLER+';wirePermissions()',context);
 let result,count=0;await display({audioRequested,frame},grant=>{result=grant;count++});
 assert.equal(count,1);assert.equal(options.useSystemPicker,platform==='darwin');
 return {result:JSON.parse(JSON.stringify(result)),picks};
}
(async()=>{
 for(const platform of ['linux','win32']) {
  assert.deepEqual((await run(platform,true)).result,{video:{id:'screen:1',display_id:'1'},audio:'loopback'});
  for(const requested of [false,undefined])assert.deepEqual((await run(platform,requested)).result,{video:{id:'screen:1',display_id:'1'}});
 }
 assert.equal((await run('darwin',true)).result.audio,undefined);
 for(const opts of [{cancel:true},{navigate:true},{destroy:true},{url:'https://evil/'}]) {
  const value=await run('linux',true,opts);assert.deepEqual(value.result,{});
  if(opts.url)assert.equal(value.picks,0);
 }
})().catch(e=>{console.error(e);process.exit(1)});
'''
    import json
    result = subprocess.run([shutil.which('node') or 'node', '-e', script.replace('HANDLER', json.dumps(handler))], capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stdout + result.stderr
