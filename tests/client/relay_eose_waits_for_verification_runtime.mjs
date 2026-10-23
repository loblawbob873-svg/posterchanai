// An EOSE must not overtake the events of its own subscription that are still being VERIFIED.
// Runs the SHIPPED relay.js under node with a verification worker whose answer the test releases by hand:
// an untrusted relay sends EVENT, the verify batch is taken off the queue (the 40 ms timer, or another
// subscription's events arming it), and EOSE lands before the worker answers. The one-shot query used to
// resolve [] marked complete and drop the event (a fresh device re-reading a profile another device had
// just saved got nothing, about half the time with "use my own relays" on).
// RELAY_JS=<path> runs another copy (the fail-first run).
import fs from 'node:fs';
import vm from 'node:vm';
import assert from 'node:assert/strict';
const timers=new Set();
const later=(fn,ms)=>{const t=setTimeout(()=>{timers.delete(t);fn();},ms);timers.add(t);return t;};
let releases=[];
const ctx={console,Map,Set,Promise,Date,Math,JSON,URL,Error,
 setTimeout:later,clearTimeout:t=>{clearTimeout(t);timers.delete(t);},setInterval,clearInterval,
 document:{addEventListener(){}},navigator:{onLine:true},location:{protocol:'https:',host:'app.test'},
 Worker:class{postMessage(m){releases.push(()=>this.onmessage({data:{id:m.id,ok:true,
   data:m.args.events.map(e=>({id:e.id,valid:e.sig==='good'}))}}));}},
};ctx.window=ctx;ctx.self=ctx;vm.createContext(ctx);
const src=process.env.RELAY_JS||new URL('../../static/js/client/relay.js',import.meta.url);
vm.runInContext(fs.readFileSync(src,'utf8'),ctx);
const R=ctx.Relay;
const ev=(id,kind=0)=>({id,pubkey:'me',kind,created_at:100,tags:[],content:'{}',sig:'good'});
const tick=()=>new Promise(r=>setImmediate(r));
function start(filters,timeout=2000){
  R._conns.clear();
  const conns=['wss://home.test','wss://mine.test'].map(url=>({url,trusted:false,ws:{readyState:1},_send:()=>true}));
  conns.forEach(c=>R._conns.set(c.url,c));
  const result=R.query(filters,timeout);
  const id=[...R._subs.keys()].at(-1);
  return {result,emit:(i,type,e)=>R._onMessage(conns[i],JSON.stringify(e?[type,id,e]:[type,id]))};
}
try{
  // 1. The batch is in flight (taken off the queue) when the last EOSE arrives.
  let t=start([{authors:['me'],kinds:[0],limit:1}]);
  t.emit(0,'EVENT',ev('profile'));
  R._flush();                                   // what the 40 ms timer does
  await tick();
  t.emit(0,'EOSE');t.emit(1,'EOSE');
  let done=false;t.result.then(()=>done=true);
  await tick();
  assert.equal(done,false,'the query resolved while its event was still being verified');
  while(releases.length)releases.shift()();
  let out=await t.result;
  assert.equal(out.complete,true);
  assert.equal(JSON.stringify(out.map(e=>e.id)),'["profile"]','the event verified after the EOSE was dropped');

  // 2. A wedged worker (it has no deadline of its own) costs the answer its unverified event, never the answer.
  t=start([{authors:['me'],kinds:[0],limit:1}],5000);
  t.emit(0,'EVENT',ev('stuck'));
  R._flush();
  await tick();
  t.emit(0,'EOSE');t.emit(1,'EOSE');
  const t0=Date.now();out=await t.result;
  assert.ok(Date.now()-t0<2500,'a wedged verification held the answer');
  assert.equal(out.complete,true);
  releases.length=0;

  // 3. Nothing in flight: an EOSE still resolves at once, and an empty complete answer is still empty.
  t=start([{authors:['me'],kinds:[0],limit:1}]);
  t.emit(0,'EOSE');t.emit(1,'EOSE');
  out=await t.result;assert.equal(out.complete,true);assert.equal(out.length,0);
  console.log('EOSE waits for in-flight verification: PASS');
}finally{for(const timer of timers)clearTimeout(timer);}
