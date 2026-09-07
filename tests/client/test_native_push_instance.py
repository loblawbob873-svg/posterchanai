"""Execute the shipped native registration using a bundled asset origin and real instance routing."""
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[2]


def test_bundled_registration_uses_the_instance_and_rejects_cross_origin_socket():
    source = (ROOT / 'static/js/client/app.js').read_text()
    code = source[source.index('  function _directPushSocketUrl('):source.index('  async function _registerPushSub(')]
    harness = r'''
const assert=require('node:assert/strict');
const location={href:'https://localhost/index.html'};
let base='https://poster.example',path='/api/push/direct/ws';
const _instanceBase=()=>base,ME={pubkey:'owner'},toast=()=>{};
const _directPushAuth=async()=>({sig:'proof'});
const requests=[], registrations=[];
const fetch=async(url,opts)=>{requests.push([url,JSON.parse(opts.body)]);return {json:async()=>({ok:true,token:'server-token',websocket_url:path})};};
const P={getEndpoint:async()=>({deviceId:'phone-1234567890123456'}),register:async r=>{registrations.push(r);return {ok:true};},batteryStatus:async()=>({healthy:true})};
CODE
(async()=>{
 await _enablePushNative(P);
 assert.equal(registrations[0].socketUrl,'wss://poster.example/api/push/direct/ws');
 assert.equal(registrations[0].token,'server-token');
 base='https://second.example';await _enablePushNative(P);
 assert.equal(registrations[1].socketUrl,'wss://second.example/api/push/direct/ws');
 path='https://attacker.example/api/push/direct/ws';
 await assert.rejects(()=>_enablePushNative(P),/does not match/);
 assert.equal(registrations.length,2,'token must never reach a different origin');
 base='';assert.throws(()=>_directPushSocketUrl(),/Choose a server/);
 base='http://127.0.0.1:3051';assert.equal(_directPushSocketUrl(),'ws://127.0.0.1:3051/api/push/direct/ws');
})().catch(e=>{console.error(e);process.exitCode=1;});
'''.replace('CODE', code)
    result = subprocess.run(['node', '-e', harness], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
