"""Drive the shipped notification Test click handler, including hung native/signer/network legs."""
import json
import os
from pathlib import Path
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[2]
APP = Path(os.environ.get('PC_PUSH_TEST_APP', ROOT / 'static/js/client/app.js')).read_text()


def run(case):
    start = APP.find('  async function _pushTestWait(')
    code = APP[start if start >= 0 else APP.index('  async function testPush(){'):APP.index('  async function disablePush(){')]
    click = APP[APP.index('    if(tb) tb.onclick='):APP.index('    let cur = await pushState();')]
    harness = r'''
const assert=require('node:assert/strict');
const scenario=CASE, messages=[], requests=[],timers=new Map();let next=0,aborted=false;
const never=()=>new Promise(()=>{}),setTimeout=(fn,ms)=>{timers.set(++next,fn);return next;},clearTimeout=id=>timers.delete(id);
let GUEST=scenario==='guest',ME={pubkey:'a'.repeat(64)};
const toast=s=>messages.push(s),Notification={permission:scenario==='denied'?'denied':'granted'};
const P={getEndpoint:()=>scenario==='native_hang'?never():Promise.resolve({deviceId:'phone-device-12345678'})};
const _pushPlugin=()=>scenario==='browser'||scenario==='denied'?null:P;
const pushState=()=>scenario==='state_hang'?never():Promise.resolve(scenario==='off'?'off':'on');
const sign=()=>{if(scenario==='signer_hang')return never();if(scenario==='switch')ME.pubkey='b'.repeat(64);return Promise.resolve({sig:'signed'});};
const fetch=(_url,options)=>{requests.push(JSON.parse(options.body));options.signal.addEventListener('abort',()=>aborted=true);
 if(scenario==='network_hang')return never();
 return Promise.resolve({ok:scenario!=='http_error',status:503,json:()=>scenario==='body_hang'?never():Promise.resolve(
 scenario==='server_error'?{ok:false,error:'Device not registered'}:scenario==='browser'?{ok:true,accepted:1}:{ok:true,queued:1})});};
const tb={disabled:false};
CODE
CLICK
(async()=>{
 const task=tb.onclick();assert.equal(tb.disabled,true);
 for(let i=0;i<30;i++)await Promise.resolve();
 const hung=['state_hang','native_hang','signer_hang','network_hang','body_hang'].includes(scenario);
 if(hung){assert(messages.length>0,'click must immediately report progress');assert.equal(timers.size,1);[...timers.values()][0]();}
 await task;
 assert.equal(tb.disabled,false);assert.equal(timers.size,0);
 if(scenario==='signer_hang'||scenario==='switch'||scenario==='state_hang'||scenario==='native_hang'||scenario==='off'||scenario==='denied'||scenario==='guest')assert.equal(requests.length,0);
 if(hung)assert.match(messages.at(-1),/did not answer/);
 if(scenario==='success'){assert.equal(requests[0].device_id,'phone-device-12345678');assert.match(messages.at(-1),/queued/);assert(!messages.at(-1).includes('sent to'));}
 if(scenario==='browser'){assert(!('device_id' in requests[0]));assert.match(messages.at(-1),/accepted/);}
 if(scenario==='http_error')assert.match(messages.at(-1),/503/);
 if(scenario==='server_error')assert.equal(messages.at(-1),'Device not registered');
 if(scenario==='switch')assert.match(messages.at(-1),/Account changed/);
 if(requests.length)assert(aborted);
 console.log(JSON.stringify({messages,requests}));
})().catch(e=>{console.error(e);process.exitCode=1;});
'''.replace('CASE', json.dumps(case)).replace('CODE', code).replace('CLICK', click)
    result = subprocess.run(['node', '-e', harness], capture_output=True, text=True, timeout=5)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip(), 'handler never finished'


@pytest.mark.parametrize('case', ['success', 'browser', 'state_hang', 'native_hang', 'signer_hang',
                                    'network_hang', 'body_hang', 'switch', 'off', 'denied', 'guest',
                                    'http_error', 'server_error'])
def test_actual_test_click_feedback_and_recovery(case):
    run(case)
