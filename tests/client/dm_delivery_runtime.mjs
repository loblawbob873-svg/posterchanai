import fs from 'node:fs';
import vm from 'node:vm';
import assert from 'node:assert/strict';
const src=fs.readFileSync(new URL('../../static/js/client/app.js',import.meta.url),'utf8');
const shipped=src.slice(src.indexOf('  let _dmWatching='),src.indexOf('  // Unwrap a NIP-17 gift wrap'));
function setup({pull=async()=>0,query=async()=>[],ingest}={}){
  const subs=[],received=[],tried=new Set(),timers=[];
  const ctx=vm.createContext({console:{warn(){},info(){}},Map,Set,Date,Promise,
    setInterval(fn){timers.push(fn);}, ME:{pubkey:'self'},signer:{nip17unwrap:true},_dmLoaded:false,_dmUnread:0,
    _wrapTried:tried,MUTED:new Set(),VIEW:'home',
    Relay:{subscribe(filters,handlers){subs.push({filters,...handlers});},query},
    DmCache:{pullShared:pull,pushShared:async()=>{}},Store:{byKind:()=>[],saveEvent:()=>false},
    ingestDM:()=>false,ingestWrap:ingest|| (async(ev,live)=>{received.push({ev,live});tried.add(ev.id);}),
    bumpDm(){},_dmNotify(){},_scheduleDmRefresh(){},renderMessages(){},recountDmUnread(){}
  });
  vm.runInContext(shipped+';globalThis.api={ensureDMs,_queueDmHistory};',ctx);
  return {ctx,subs,received,timers,...ctx.api};
}
// A slow shared-cache download must not prevent live, self-addressed server notifications.
{
  let release;const x=setup({pull:()=>new Promise(r=>release=r)});
  const pending=x.ensureDMs();assert.equal(x.subs.length,2);
  const live=x.subs.find(s=>s.filters[0].kinds[0]===1059);
  assert.equal(live.filters[0].since,undefined,'randomized gift-wrap dates must not be filtered');
  live.onEose();const ev={id:'application-from-last-night',kind:1059,created_at:1};
  await live.onEvent(ev);assert.equal(x.received[0].ev,ev);assert.equal(x.received[0].live,true);
  release(0);await pending;
}
// Failed history reads can be retried without losing or duplicating live subscriptions.
{
  let fail=true;const x=setup({query:async()=>{if(fail)throw Error('offline');return [];}});
  await x.ensureDMs();assert.equal(x.ctx._dmLoaded,false);assert.equal(x.subs.length,2);
  fail=false;await x.ensureDMs();assert.equal(x.ctx._dmLoaded,true);assert.equal(x.subs.length,2);
  x.subs[1].onEose();await x.subs[1].onEvent({id:'after-retry'});assert.equal(x.received.length,1);
}
// The historical queue bounds signer work; new DMs bypass that queue and old duplicates disappear.
{
  const releases=[],seen=[],done=new Set();let active=0,peak=0;
  const x=setup({ingest:async(ev,live)=>{
    if(done.has(ev.id))return;done.add(ev.id);seen.push([ev.id,live]);
    if(live)return;active++;peak=Math.max(peak,active);
    await new Promise(r=>releases.push(()=>{active--;r();}));
  }});
  await x.ensureDMs();x.subs[1].onEose();
  const history=Array.from({length:20},(_,i)=>({id:'history-'+i}));
  const pending=x._queueDmHistory(history);assert.equal(active,6);
  await x.subs[1].onEvent({id:'new-system-self-note'});
  assert.equal(seen.at(-1)[0],'new-system-self-note');assert.equal(seen.at(-1)[1],true);
  while(done.size<21||active){releases.splice(0).forEach(r=>r());await new Promise(r=>setImmediate(r));}
  await pending;assert.equal(peak,6);assert.equal(done.size,21);
}
// Cached redelivery survives relay-level dedup after a transient signer failure.
{
  let attempts=0;const x=setup({ingest:async(ev)=>{attempts++;if(attempts>1)x.ctx._wrapTried.add(ev.id);}});
  await x.ensureDMs();x.subs[1].onEose();
  const ev={id:'temporarily-undecryptable'};await x.subs[1].onEvent(ev);
  x.ctx.Store.byKind=()=>[ev];x.timers[0]();await new Promise(r=>setImmediate(r));
  assert.equal(attempts,2);x.timers[0]();await new Promise(r=>setImmediate(r));assert.equal(attempts,2);
}
console.log('ok: live delivery, history failure recovery, bounded history and self-note arrival');
