"""Shipped app invite transport respects recipient routing and account changes."""
import json
from pathlib import Path
import subprocess
import pytest
from tests.client.test_tip_tell_on_dismiss import _fn

ROOT = Path(__file__).resolve().parents[2]
APP = (ROOT / 'static/js/client/app.js').read_text()
SEND = _fn(APP, 'sendCordDirectInvite', 'async function sendCordDirectInvite(')
INGEST = _fn(APP, '_ingestCordDirectWrap', 'async function _ingestCordDirectWrap(')
INBOX = _fn(APP, '_startCordDirectInbox', 'async function _startCordDirectInbox(')


def run(script):
    result = subprocess.run(['node', '-e', script], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize('mode', ['accepted', 'rejected', 'uncertain', 'account_during_discovery', 'account_during_sign', 'no_relays'])
def test_send_targets_recipient_inbox_and_requires_acceptance(mode):
    run('''const assert=require('node:assert/strict');
let active=true, published=[],created=0;const context={pubkey:'a'.repeat(64),isCurrent:()=>active};
const cordDirectContext=()=>context,refToPk=value=>value==='chosen-npub'?'b'.repeat(64):null;
const mode=''' + json.dumps(mode) + ''';
const dmInboxRelays=async pk=>{assert.equal(pk,'b'.repeat(64));if(mode==='account_during_discovery')active=false;return{relays:mode==='no_relays'?[]:['wss://recipient.invalid']};};
const cordDirectModule=async()=>({create:async(bundle,recipient,ctx)=>{created++;assert.equal(recipient,'b'.repeat(64));if(mode==='account_during_sign')active=false;return{wrap:{id:'c'.repeat(64)}};}});
const Relay={publishTo:async(...args)=>{published.push(args);return{ok:mode==='accepted',uncertain:mode==='uncertain'};}};
''' + SEND + '''
(async()=>{
if(mode==='accepted')assert.equal(await sendCordDirectInvite({},'chosen-npub'),'c'.repeat(64));
else await assert.rejects(()=>sendCordDirectInvite({},'chosen-npub'));
if(mode.startsWith('account_')||mode==='no_relays')assert.equal(published.length,0);
else{assert.equal(published.length,1);assert.deepEqual(published[0][0],['wss://recipient.invalid']);assert.equal(published[0][2].includeManaged,true);assert.equal(published[0][2].detailed,true);}
if(mode==='account_during_discovery'||mode==='no_relays')assert.equal(created,0);
})().catch(e=>{console.error(e);process.exitCode=1});''')


def test_indexed_inbox_is_bounded_and_independent_of_general_dm_sync():
    run('''const assert=require('node:assert/strict');
let active=true,subscribed=[],ingested=[],closes=0;
const context={pubkey:'a'.repeat(64),isCurrent:()=>active},cordDirectContext=()=>context;
let _cordDirectClose=null,_cordDirectOwner='';
const _ingestCordDirectWrap=async(event,live)=>ingested.push({event,live});
const dmInboxRelays=async()=>({relays:['wss://recipient.invalid']});
const Relay={subscribe:(filters,handlers)=>{subscribed.push({filters,handlers});return 'pool-sub';},close:id=>{assert.equal(id,'pool-sub');closes++;},subscribeFrom:(relays,filters,handlers)=>{subscribed.push({relays,filters,handlers});return()=>closes++;}};
''' + INBOX + '''
(async()=>{await _startCordDirectInbox();assert.equal(subscribed.length,2);
for(const sub of subscribed){assert.deepEqual(sub.filters,[{kinds:[1059],'#p':['a'.repeat(64)],'#k':['3313'],limit:32}]);}
assert.equal(subscribed[1].handlers.live,true);
await _startCordDirectInbox();assert.equal(subscribed.length,2,'duplicate subscription');
subscribed[0].handlers.onEvent({id:'first'});assert.equal(ingested.length,1);
active=false;subscribed[0].handlers.onEvent({id:'other-account'});assert.equal(ingested.length,1);
_cordDirectClose();assert.equal(closes,2);
})().catch(e=>{console.error(e);process.exitCode=1});''')


@pytest.mark.parametrize('indexed', [False, True])
@pytest.mark.parametrize('valid', [False, True])
def test_indexed_and_untagged_rumors_must_take_strict_invitation_path(indexed, valid):
    ingest_wrap = _fn(APP, 'ingestWrap', 'async function ingestWrap(')
    run('''const assert=require('node:assert/strict');
const indexed=''' + json.dumps(indexed) + ',valid=' + json.dumps(valid) + ''';
let strict=0,unwrapped=0,_dmTotal=0,_dmDone=0;const _wrapTried=new Set(),_dmTick=()=>{};
const signer={nip17unwrap:async()=>{unwrapped++;return{kind:3313,content:'untrusted'};}};
const DmCache={get:async()=>null,put:()=>{throw Error('invite entered plaintext DM cache');}};
const _ingestCordDirectWrap=async()=>{strict++;if(!valid)throw Error('invalid signature');return true;};
''' + ingest_wrap + '''
(async()=>{const ev={id:'a'.repeat(64),tags:indexed?[['k','3313']]:[]};
if(indexed&&!valid)await assert.rejects(()=>ingestWrap(ev,false));
else assert.equal(await ingestWrap(ev,false),valid);
assert.equal(strict,1);assert.equal(unwrapped,indexed?0:1);
if(!valid)assert(!_wrapTried.has(ev.id));
})().catch(e=>{console.error(e);process.exitCode=1});''')


@pytest.mark.parametrize('stage', ['module', 'discovery', 'sign'])
def test_private_grant_permission_revocation_stops_key_delivery(stage):
    run('''const assert=require('node:assert/strict');
let allowed=true,published=0;const stage=''' + json.dumps(stage) + ''';
const cordDirectContext=()=>({pubkey:'a'.repeat(64),isCurrent:()=>true}),refToPk=()=> 'b'.repeat(64);
const cordDirectModule=async()=>{if(stage==='module')allowed=false;return{create:async()=>{if(stage==='sign')allowed=false;return{wrap:{id:'c'.repeat(64)}};}};};
const dmInboxRelays=async()=>{if(stage==='discovery')allowed=false;return{relays:['wss://recipient.invalid']};};
const Relay={publishTo:async()=>{published++;return{ok:true};}};
''' + SEND + '''
(async()=>{await assert.rejects(()=>sendCordDirectInvite({},'recipient',{isCurrent:()=>allowed}));assert.equal(published,0);})().catch(e=>{console.error(e);process.exitCode=1});''')
