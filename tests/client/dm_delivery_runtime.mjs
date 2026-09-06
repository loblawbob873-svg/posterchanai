import fs from 'node:fs';
import vm from 'node:vm';
import assert from 'node:assert/strict';
const src=fs.readFileSync(process.argv[2] || new URL('../../static/js/client/app.js',import.meta.url),'utf8');
const shipped=src.slice(src.indexOf('  let _dmWatching='),src.indexOf('  // Unwrap a NIP-17 gift wrap'));
function setup({pull=async()=>0,query=async()=>[],ingest,mode="local",clock=Date}={}){
  const subs=[],received=[],tried=new Set(),timers=[];
  const ctx=vm.createContext({console:{warn(){},info(){}},Map,Set,Date:clock,Promise,
    setInterval(fn){timers.push(fn);}, ME:{pubkey:'self'},signer:{nip17unwrap:true,mode},_dmLoaded:false,_dmUnread:0,
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
// Replayed history must wait for the shared cache, while an actually new DM still arrives.
{
  let release;const x=setup({mode:'nip46',pull:()=>new Promise(r=>release=r)});
  const pending=x.ensureDMs();const live=x.subs[1];
  await live.onEvent({id:'already-in-shared-cache'});
  assert.equal(x.received.length,0,'history started before shared cache loaded');
  x.ctx.Store.byKind=()=>[{id:'already-in-shared-cache'}];x.timers[0]();
  assert.equal(x.received.length,0,'retry timer bypassed shared-cache barrier');
  live.onEose();await live.onEvent({id:'new-arrival'});
  assert.equal(x.received[0].ev.id,'new-arrival');
  release(1);await pending;
  assert.equal(x.received.filter(r=>r.ev.id==='already-in-shared-cache').length,1);
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
    if(done.has(ev.id))return;done.add(ev.id);x.ctx._wrapTried.add(ev.id);seen.push([ev.id,live]);
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
// Browser-extension and Windows clients share a slow phone without each filling its work queue.
{
  const releases=[],started=[];let active=0,peak=0;
  const clients=['nip07','nip46'].map((mode,index)=>{
    let x;x=setup({mode,ingest:async(ev,live)=>{
      if(x.ctx._wrapTried.has(ev.id))return;
      x.ctx._wrapTried.add(ev.id);started.push([index,ev.id,live]);
      if(live)return;
      active++;peak=Math.max(peak,active);
      await new Promise(r=>releases.push({index,id:ev.id,finish:()=>{active--;r();}}));
    }});return x;
  });
  for(const x of clients){await x.ensureDMs();x.subs[1].onEose();}
  const drains=clients.map(x=>x._queueDmHistory(Array.from({length:10},(_,i)=>({id:'old-'+i}))));
  assert.equal(active,4,'two clients must use only two background decrypts each');
  await clients[1].subs[1].onEvent({id:'fresh-server-notice'});
  assert.equal(started.at(-1)[1],'fresh-server-notice');assert.equal(started.at(-1)[2],true);
  const free=releases.findIndex(r=>r.index===0&&r.id==='old-1');releases.splice(free,1)[0].finish();
  await new Promise(r=>setImmediate(r));
  assert(started.some(([i,id])=>i===0&&id==='old-2'),'a stalled first request blocked the other worker');
  for(let i=0;i<30&&(active||started.length<21);i++){
    releases.splice(0).forEach(r=>r.finish());await new Promise(r=>setImmediate(r));
  }
  await Promise.all(drains);assert.equal(peak,4);assert.equal(started.length,21);
}
// An unavailable signer is retried with backoff, not hammered by every minute timer tick.
{
  let now=1000000,attempts=0,healthy=false;
  class Clock extends Date{static now(){return now;}}
  let x;x=setup({mode:'nip46',clock:Clock,ingest:async ev=>{
    attempts++;if(healthy)x.ctx._wrapTried.add(ev.id);
  }});
  await x.ensureDMs();x.ctx.Store.byKind=()=>[{id:'phone-asleep'}];
  const tick=async()=>{x.timers[0]();await new Promise(r=>setImmediate(r));};
  await tick();assert.equal(attempts,1);
  await tick();assert.equal(attempts,1);
  now+=60000;await tick();assert.equal(attempts,2);
  now+=60000;await tick();assert.equal(attempts,2,'second failure must back off longer');
  now+=60000;healthy=true;await tick();assert.equal(attempts,3);
  now+=600000;await tick();assert.equal(attempts,3,'recovered message must not decrypt again');
}
// A failed phone pauses the remaining history rather than trying the whole inbox during an outage.
{
  let now=2000000,attempts=0;class Clock extends Date{static now(){return now;}}
  const x=setup({mode:'nip46',clock:Clock,ingest:async()=>{attempts++;}});
  await x.ensureDMs();const history=Array.from({length:40},(_,i)=>({id:'offline-'+i}));
  await x._queueDmHistory(history);assert.equal(attempts,2);
  await x._queueDmHistory([]);assert.equal(attempts,2,'failed signer spun through the inbox');
  now+=30000;await x._queueDmHistory([]);assert.equal(attempts,4);
}
console.log('ok: live delivery, history failure recovery, bounded history and self-note arrival');
