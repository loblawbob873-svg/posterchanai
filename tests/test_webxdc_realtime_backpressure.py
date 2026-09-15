"""Game movement stays current even when an account signer is waiting for its user.

Run the complete shipped module. Deferred promises model a slow signer without timing thresholds,
real relays, account keys or browser windows.
"""
import json
from pathlib import Path
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]
WEBXDC = ROOT / "static/js/client/webxdc.js"
pytestmark = pytest.mark.skipif(not shutil.which("node"), reason="node not installed")

BOOT = r"""
const assert = require('node:assert/strict');
global.crypto = require('node:crypto').webcrypto;
const accountCalls = [], signed = [], sent = [], warnings = [];
console.warn = (...args) => warnings.push(args.join(' '));
let keys = 0;
const NT = {
  generateSecretKey(){ return Uint8Array.of(++keys); },
  getPublicKey(sk){ return 'ephemeral-' + sk[0]; },
  finalizeEvent(event, sk){
    signed.push({event, key:sk});
    return {...event, pubkey:NT.getPublicKey(sk), id:'event-' + signed.length};
  },
};
const relay = {
  publishFastAll(event){ sent.push(event); return 2; },
  publishFast(){ throw new Error('single relay publication splits the game lobby'); },
  close(){},
};
global.window = {
  addEventListener(){}, removeEventListener(){}, NostrTools:NT, Relay:relay,
  __PC:{ $(){}, enc:s=>s, toast(){},
    publish(...args){ accountCalls.push(args); return new Promise(()=>{}); },
    me:()=>({pubkey:'account-key'}), profOf:()=>({}), apiBase:()=> 'https://fixture.invalid' },
};
global.document = {addEventListener(){},removeEventListener(){},querySelectorAll:()=>[]};
global.location = {hostname:'fixture.invalid',href:'https://fixture.invalid/'};
const deferred = () => {
  let resolve,reject;
  const promise = new Promise((yes,no)=>{resolve=yes;reject=no;});
  return {promise,resolve,reject};
};
const drain = () => new Promise(resolve=>setImmediate(resolve));
"""


def _run(body, source=WEBXDC):
    script = BOOT + f"\nrequire({json.dumps(str(source))});\n" + """
const create = transport => new window.PCWebxdc.Session(
  {uuid:'shared-game',url:'https://fixture.invalid/doom.xdc',transport},
  {index:new Map(),bytes:new Uint8Array()});
(async()=>{
""" + body + "\n})().catch(error=>{console.error(error);process.exitCode=1;});"
    return subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=10)


BOUNDED_SIGNER = r"""
const pending = [], published = [];
let active = 0, peak = 0;
global.PCConcord = window.PCConcord = {
  webxdcPublish(transport,uuid,payload,description,realtime,subscription){
    active++; peak=Math.max(peak,active);
    published.push({transport,uuid,payload,realtime,subscription});
    const gate=deferred();pending.push(gate);
    return gate.promise.finally(()=>{active--;});
  },
};
const transport={kind:'nip29',id:'room'}, session=create(transport);
const subscription=()=>{};
session.rtSub=subscription;
session.rtSend('first');
for(let i=1;i<=100;i++)session.rtSend('position-'+i);
assert.equal(published.length,1,'a waiting signer received overlapping movement requests');
pending[0].resolve();await drain();
assert.deepEqual(published.map(x=>x.payload),['first','position-100'],
  'the signer replayed stale movement instead of the newest position');
assert.equal(peak,1,'more than one signer request was in flight');
for(const packet of published){
  assert.equal(packet.transport,transport);assert.equal(packet.uuid,'shared-game');
  assert.equal(packet.realtime,true);assert.equal(packet.subscription,subscription);
}
// A rejected signer must release the pump, so the next movement can try again.
pending[1].reject(new Error('signer unavailable'));await drain();
session.rtSend('recovered');
assert.equal(published.length,3,'one signer rejection permanently wedged realtime sending');
assert.equal(published[2].payload,'recovered');
// Closing the game while signing must discard pending movement rather than send it afterward.
session.rtSend('after-close');session.destroy();
pending[2].resolve();await drain();
assert.equal(published.length,3,'closing a game still emitted its pending movement');
assert.equal(active,0);
assert.equal(sent.length,0,'room packets leaked onto the generic realtime plane');
"""


def test_slow_signer_keeps_one_request_and_the_newest_movement():
    result = _run(BOUNDED_SIGNER)
    assert result.returncode == 0, result.stdout + result.stderr


LOCAL_BURST = r"""
const session=create(null);
for(let i=0;i<100;i++)session.rtSend(Buffer.from([i,0,255,128]).toString('base64'));
// Check before yielding: an account signer or relay acknowledgement cannot gate these packets.
assert.equal(sent.length,100,'native realtime waited for account signing or relay acknowledgements');
assert.equal(accountCalls.length,0,'movement asked the account signer to approve a packet');
assert.equal(keys,1,'the session generated a new signing key for every movement packet');
assert.equal(signed.length,100);
for(let i=0;i<sent.length;i++){
  const event=sent[i];
  assert.equal(event.kind,20932);assert.deepEqual(event.tags,[['i','shared-game']]);
  assert.equal(event.pubkey,'ephemeral-1');
  assert.equal(signed[i].key,signed[0].key);
  assert.deepEqual([...Buffer.from(event.content,'base64')],[i,0,255,128]);
}
const peer=create(null);peer.rtSend('cGVlcg==');
assert.equal(keys,2,'two game sessions shared a self-echo identity');
assert.equal(sent[100].pubkey,'ephemeral-2');
"""


def test_native_movement_burst_uses_one_local_key_and_every_open_relay():
    result = _run(LOCAL_BURST)
    assert result.returncode == 0, result.stdout + result.stderr
