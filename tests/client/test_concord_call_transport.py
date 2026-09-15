"""Call wiring scopes subscriptions and signed presence to the held room/account."""
from pathlib import Path
import subprocess

ROOT=Path(__file__).resolve().parents[2]


def test_call_adapter_uses_room_transport_and_rejects_late_signatures():
    source=(ROOT/'static/js/client/concord.js').read_text()
    planes=source[source.index('  function cordControlStamp('):source.index('  async function cordQuery(')]
    calls=source[source.index('  async function callModule('):source.index('  async function webxdcQuery(')]
    script=r'''
const assert=require('node:assert/strict');let owner='a'.repeat(64),opened,signGate=null,publishes=0;
const room={communityId:'room',cord:{bundle:{relays:['wss://room.example'],epoch:1}}};
const roomIdentity=r=>r.communityId,saved=()=>[room],roomControls=new Map([['room',[]]]),roomRelays=b=>b.relays;
const subscriptions=[];const p={viewer:()=>({pubkey:owner}),profOf:()=>({}),signTemplate:async t=>{if(signGate)await signGate;return t;},relayPublishRoom:async()=>{publishes++;return{ok:true};}};
const window={PCCordVoice:{},PCCordCall:{open:async ctx=>{opened=ctx;return ctx;}},PosterCordReader:{
 inspectControl:()=>({channels:[{id:'channel',name:'general'}]}),voiceMaterial:b=>({room:'voice'+b.epoch,stream:'stream'+b.epoch}),
 createPlaneAuth:(_b,_c,author,relays)=>({author,relays}),
 inspectVoicePresence:async(_b,_c,_ch,w)=>w,
 createVoicePresence:async(_b,_c,_ch,verb,pk,sign)=>{await sign({pubkey:pk});return{pubkey:'stream'+room.cord.bundle.epoch,kind:21059,content:verb};}},
 Relay:{subscribeFrom:(relays,filters,opts)=>{subscriptions.push({relays,filters,opts});const stop=()=>{};stop.publish=()=>0;stop.hasTargets=true;stop.ready=Promise.resolve(true);return stop;}}};
const localStorage={getItem:()=>null,setItem:()=>{}},callModules=new Map(),concordScriptUrl=null;
'''+planes+calls+r'''
(async()=>{
 await startCordCall(p,room,'general');assert(opened);
 assert.equal(opened.material().room,'voice1');opened.subscribe(()=>{});
 assert.deepEqual(subscriptions[0].filters[0].kinds,[21059]);assert.deepEqual(subscriptions[0].filters[0].authors,['stream1']);
 assert.deepEqual(subscriptions[0].relays,['wss://room.example']);assert.equal(subscriptions[0].opts.authScope.author,'stream1');
 await opened.presence('joined','identity','https://broker.example');assert.equal(publishes,1);
 room.cord.bundle={relays:['wss://room.example'],epoch:2};assert.equal(opened.material().room,'voice2');
 let release;signGate=new Promise(r=>release=r);const sending=opened.presence('joined','identity','https://broker.example');
 await new Promise(r=>setImmediate(r));owner='b'.repeat(64);release();
 await assert.rejects(sending,/account changed/);assert.equal(publishes,1);assert.equal(opened.current(),false);
 console.log('account-owned call transport passed');
})().catch(e=>{console.error(e);process.exitCode=1;});
'''
    result=subprocess.run(['node','-e',script],cwd=ROOT,capture_output=True,text=True,timeout=15)
    assert result.returncode==0,result.stdout+result.stderr
    assert 'transport passed' in result.stdout
