"""Real draft retry/delivery handlers and Outbox: identity, edits, and duplicate safety."""
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[2]


def test_draft_retry_and_delivery_account_safety(tmp_path):
    app = (ROOT / 'static/js/client/app.js').read_text()
    helpers = app[app.index("  const _QD_KEY ="):app.index('  function _flushOutbox(){')]
    send = app[app.index('  const _draftSending='):app.index('  // NIP-22 comment on a NIP-23 article.')]
    setup = r'''
const assert=require('node:assert/strict');
global.window=global;global.self=global;
const target=new EventTarget();global.addEventListener=target.addEventListener.bind(target);global.dispatchEvent=target.dispatchEvent.bind(target);
global.CustomEvent=class extends Event{constructor(name,opts){super(name);this.detail=opts?.detail}};
global.document={hidden:false,addEventListener(){}};
const data=new Map();global.localStorage={getItem:k=>data.get(k)||null,setItem:(k,v)=>data.set(k,String(v))};
let ME={pubkey:'alice'},VIEW='drafts',renders=0,signs=0,behavior=async()=>({ok:false,msg:'timeout'});
const saved=new Map(),key=id=>ME.pubkey+':'+id;
const Drafts={get:id=>saved.get(key(id)),remove:id=>saved.delete(key(id))};
const Store={get:()=>null},CFG={},toast=()=>{},renderDrafts=()=>{renders++};
const mentionTags=()=>[],replyKindFor=()=>1;
const _appendQuoteNevent=s=>s;
global.__PC={me:()=>ME};
global.Relay={status:'ok',reviveStale(){},publish:ev=>behavior(ev)};
const ev=id=>({id,pubkey:'alice',kind:1,sig:'signed-'+id,content:'original',tags:[]});
async function publish(){signs++;return initialPublish();}
let initialPublish;
function draft(id,text='original'){saved.set(key(id),{id,text});}
'''
    exercise = r'''
(async()=>{try{
 draft('d');Outbox.add(ev('one'));_qDraftSet('one','d');
 await _sendDraft('d');assert.equal(signs,0);assert(Drafts.get('d'));assert(Outbox.has('one'));
 behavior=async()=>({ok:true});await Outbox.flush();
 assert(!Drafts.get('d'),'automatic delivery must clear draft');assert.equal(signs,0);
 assert.equal(Outbox.delivered()[0].ev.id,'one');
 // Edits are independent work, not the recovery copy of the acknowledged event.
 draft('edited');Outbox.add(ev('two'));_qDraftSet('two','edited');draft('edited','new text');
 await Outbox.flush();assert.equal(Drafts.get('edited').text,'new text');
 // Legacy string mappings must preserve same-text edits to semantic metadata.
 for(const edit of [{cw:true,cwReason:'changed'},{reply:'different-parent'},{quote:'different-quote'}]){
  draft('legacy');Object.assign(Drafts.get('legacy'),edit);
  localStorage.setItem('pc_draft_queued',JSON.stringify({'legacy-event':'legacy'}));
  assert.equal(_reconcileDraftDelivery(ev('legacy-event')),false);assert(Drafts.get('legacy'));
 }
 // An ACK for Alice while Bob is active cannot delete Bob's same-id draft.
 draft('shared');Outbox.add(ev('three'));_qDraftSet('three','shared');
 let ack;behavior=()=>new Promise(r=>ack=r);const pending=Outbox.flush();
 ME={pubkey:'bob'};draft('shared','Bobs draft');ack({ok:true});await pending;
 assert.equal(Drafts.get('shared').text,'Bobs draft');
 ME={pubkey:'alice'};assert(Drafts.get('shared'));_reconcileDeliveredDrafts();assert(!Drafts.get('shared'));
 // A sibling document/account cannot flush the other owner's cached queue.
 Outbox.add({...ev('foreign'),pubkey:'bob'});let attempts=0;behavior=async()=>{attempts++;return {ok:true}};
 await Outbox.flush();assert.equal(attempts,0);assert(Outbox.has('foreign'));Outbox.remove('foreign');
 // Re-rendered Send buttons still join one in-flight sign/publish operation.
 draft('busy');let finish;initialPublish=()=>new Promise(r=>finish=r);
 const a=sendDraft('busy'),b=sendDraft('busy');assert.equal(signs,1);
 draft('busy','edited during signing');finish({ok:true});await Promise.all([a,b]);
 assert.equal(Drafts.get('busy').text,'edited during signing');
 // Late queue result records the original owner and original draft snapshot.
 draft('late');initialPublish=()=>new Promise(r=>finish=r);const late=sendDraft('late');
 ME={pubkey:'bob'};draft('late','other account');finish({ok:false,queued:true,ev:ev('late-event')});await late;
 const mapped=_qDrafts()['late-event'];assert.equal(mapped.owner,'alice');
 assert.equal(mapped.snapshot,JSON.stringify(['original','','','',false,'']));
 assert.equal(Drafts.get('late').text,'other account');
 console.log('draft behavior passed');process.exit(0);
}catch(e){console.error(e);process.exit(1)}})();
'''
    script = tmp_path / 'drafts.cjs'
    script.write_text(setup + (ROOT / 'static/js/client/outbox.js').read_text() + helpers + send + exercise)
    run = subprocess.run(['node', str(script)], capture_output=True, text=True, timeout=10)
    assert run.returncode == 0, run.stderr
    assert 'draft behavior passed' in run.stdout


def test_draft_sync_and_pull_do_not_cross_accounts(tmp_path):
    app = (ROOT / 'static/js/client/app.js').read_text()
    drafts = app[app.index('  const Drafts = {'):app.index('  function bumpDraft()')]
    setup = r'''
const assert=require('node:assert/strict');
let ME={pubkey:'alice'},VIEW='drafts',timer,calls=[],proof;
const data=new Map();const localStorage={getItem:k=>data.get(k)||null,setItem:(k,v)=>data.set(k,v)};
const bumpDraft=()=>{},renderDrafts=()=>{};
const setTimeout=f=>(timer=f,1),clearTimeout=()=>{};
let selfProof=async()=> 'proof';
let fetch=async(url,opts)=>{calls.push(JSON.parse(opts.body));return {json:async()=>({ok:true,drafts:[]})}};
'''
    exercise = r'''
(async()=>{try{
 const original=[{id:'a',text:'Alice private draft',ts:1}];
 localStorage.setItem('pc_drafts_alice',JSON.stringify(original));
 localStorage.setItem('pc_drafts_bob',JSON.stringify([{id:'b',text:'Bob private draft',ts:1}]));
 Drafts._sync(original);ME={pubkey:'bob'};await timer();assert.equal(calls.length,0);
 ME={pubkey:'alice'};selfProof=()=>new Promise(r=>proof=r);Drafts._sync(original);
 const saving=timer();ME={pubkey:'bob'};proof('alice-proof');await saving;assert.equal(calls.length,0);
 ME={pubkey:'alice'};const pulling=Drafts.pull();ME={pubkey:'bob'};proof('alice-proof');await pulling;assert.equal(calls.length,0);
 ME={pubkey:'alice'};selfProof=async()=> 'alice-proof';let response;
 fetch=async(url,opts)=>{calls.push(JSON.parse(opts.body));return {json:()=>new Promise(r=>response=r)}};
 const late=Drafts.pull();await Promise.resolve();await Promise.resolve();
 ME={pubkey:'bob'};response({ok:true,drafts:[{id:'foreign',text:'Alice remote',ts:5}]});await late;
 assert.deepEqual(JSON.parse(localStorage.getItem('pc_drafts_alice')),original);
 assert.deepEqual(JSON.parse(localStorage.getItem('pc_drafts_bob')),[{id:'b',text:'Bob private draft',ts:1}]);
 assert.equal(calls[0].pubkey,'alice');console.log('sync owner preserved');
}catch(e){console.error(e);process.exit(1)}})();
'''
    script = tmp_path / 'draft-sync.cjs'
    script.write_text(setup + drafts + exercise)
    result = subprocess.run(['node', str(script)], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert 'sync owner preserved' in result.stdout
