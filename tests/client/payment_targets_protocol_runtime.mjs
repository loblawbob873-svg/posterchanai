import fs from 'node:fs';
import vm from 'node:vm';
import assert from 'node:assert/strict';
import {createRequire} from 'node:module';
const require=createRequire(import.meta.url);
const P=require('../../static/js/client/payment-targets.js');
globalThis.window=globalThis;
vm.runInThisContext(fs.readFileSync(new URL('../../static/vendor/nostr/nostr.bundle.js',import.meta.url),'utf8')+'\nglobalThis.NostrTools=NostrTools;');
const N=globalThis.NostrTools,sk=Uint8Array.from({length:32},(_,i)=>i===31?1:0),other=Uint8Array.from({length:32},(_,i)=>i===31?2:0),owner=N.getPublicKey(sk);
const sign=(tags,created_at=100,key=sk)=>N.finalizeEvent({kind:10133,content:'',tags,created_at},key);
const copy=e=>JSON.parse(JSON.stringify(e));
const old=sign([['payto','lightning','alice@old.test']]),fresh=sign([['payto','monero','8'+'a'.repeat(94)],['payto','bitcoin','bc1qexample']],101);
const state=({events=[old],complete=true,local=[]}={})=>{
  let clock=1000,calls=0,current=events,available=complete,held=local.slice();
  const resolver=P.createResolver({now:()=>clock,verify:e=>N.verifyEvent(e),
    read:async()=>{calls++;return {events:current,complete:available};},local:()=>held,
    remember:e=>{held=[e];}});
  return {resolver,get calls(){return calls;},setEvents(e,ok=true){current=e;available=ok;},setLocal(e){held=e;},advance(ms=61000){clock+=ms;}};
};
const cases={
  format(){
    // The same 3-element wire format published by Amethyst PaymentTargetTag and Dark Wisp NipA3.
    assert.deepEqual(P.parse(fresh),[{type:'monero',address:'8'+'a'.repeat(94)},{type:'bitcoin',address:'bc1qexample'}]);
    assert.deepEqual(P.parse(sign([['payto','paypal','alice'],['payto','newnetwork','username']])),[{type:'paypal',address:'alice'},{type:'newnetwork',address:'username'}]);
  },
  malformed(){
    assert.deepEqual(P.parse({kind:10133,tags:[null,{},['payto'],['payto',{},'address'],['payto','monero',123],['payto','monero','a\nb'],['payto','../bad','address'],['payto','lightning',''],['payto','LIGHTNING',' alice@test.test '],['payto','lightning','alice@test.test']]}),[{type:'lightning',address:'alice@test.test'}]);
    assert.deepEqual(P.parse({...fresh,kind:0}),[]);
  },
  safe_uri(){
    assert.equal(P.uri({type:'javascript',address:'alert(1)'}),'payto://javascript/alert(1)');
    assert.equal(P.uri({type:'bitcoin',address:'bc1qaddress?amount=999&label=x'}),'bitcoin:bc1qaddress%3Famount%3D999%26label%3Dx');
    assert.equal(P.uri({type:'newnetwork',address:'alice/bob#thing'}),'payto://newnetwork/alice%2Fbob%23thing');
    assert.equal(P.uri({type:'../bad',address:'x'}),'');
    assert.equal(P.names.constructor,undefined);
  },
  preserve_extensions(){
    const ev=sign([['alt','Other client description'],['custom','preserve-me'],['payto','newnetwork','alice','future-extension']]);
    const tags=P.buildTags([...P.parse(ev),{type:'monero',address:'8'+'a'.repeat(94)}],ev);
    assert.deepEqual(tags.slice(0,3),ev.tags);assert.equal(tags.length,4);
    assert.deepEqual(P.buildTags([],ev),ev.tags.slice(0,2));
    assert.throws(()=>P.buildTags([{type:'monero',address:''}],ev));
  },
  async newest(){
    const s=state({events:[fresh,old,sign([['payto','lightning','evil@test.test']],200,other)]});
    assert.equal((await s.resolver.load(owner)).event.id,fresh.id);
    await s.resolver.load(owner);assert.equal(s.calls,1);
  },
  async forgery(){
    const forged=copy(fresh);forged.tags[0][2]='4'+'b'.repeat(94);
    const s=state({events:[forged,old]});assert.equal((await s.resolver.load(owner)).event.id,old.id);
  },
  async same_second(){
    const a=sign([['payto','lightning','a@test.test']]),b=sign([['payto','lightning','b@test.test']]);
    for(const events of [[a,b],[b,a]])assert.equal((await state({events}).resolver.load(owner)).event.id,[a.id,b.id].sort()[0]);
  },
  async outage_keeps_targets(){
    const s=state();await s.resolver.load(owner);s.advance();s.setEvents([],false);
    const got=await s.resolver.load(owner);assert.equal(got.event.id,old.id);assert.equal(got.available,false);
  },
  async negative_cache_recovers(){
    const s=state({events:[],complete:false});assert.equal((await s.resolver.load(owner)).event,null);
    s.setEvents([fresh]);await s.resolver.load(owner);assert.equal(s.calls,1);
    s.advance(10001);assert.equal((await s.resolver.load(owner)).event.id,fresh.id);
  },
  async live_update(){
    const s=state();await s.resolver.load(owner);s.setLocal([fresh]);s.setEvents([]);
    assert.equal((await s.resolver.load(owner)).event.id,fresh.id);
  },
  async empty_cache_live_update(){
    const s=state({events:[]});await s.resolver.load(owner);s.setLocal([fresh]);
    assert.equal((await s.resolver.load(owner)).event.id,fresh.id);
  },
  async clear(){
    const s=state();await s.resolver.load(owner);const clear=sign([['alt','Payment targets']],102);s.setEvents([clear]);
    const got=await s.resolver.load(owner,{force:true});assert.deepEqual(got.targets,[]);assert.equal(got.event.id,clear.id);
    s.advance();s.setEvents([old]);assert.equal((await s.resolver.load(owner)).event.id,clear.id);
  },
  async deletion_invalidates_cache(){
    let removed=false;
    const resolver=P.createResolver({verify:e=>N.verifyEvent(e),deleted:()=>removed,read:async()=>({events:[old],complete:true})});
    assert.equal((await resolver.load(owner)).event.id,old.id);removed=true;
    assert.equal((await resolver.load(owner)).event,null);
  },
  async coalesce(){
    let done,calls=0;const resolver=P.createResolver({verify:e=>N.verifyEvent(e),read:()=>{calls++;return new Promise(r=>done=r);}});
    const jobs=Array.from({length:20},()=>resolver.load(owner));assert.equal(calls,1);
    done({events:[fresh],complete:true});assert((await Promise.all(jobs)).every(s=>s.event.id===fresh.id));
  },
  async account_isolation(){
    const s=state();await s.resolver.load(owner);
    const got=await s.resolver.load(N.getPublicKey(other));assert.equal(got.event,null);assert.deepEqual(got.targets,[]);
  },
};
const name=process.argv[2];assert(cases[name],name);await cases[name]();console.log(name+' passed');
