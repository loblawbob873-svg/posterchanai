import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import path from 'node:path';
import {webcrypto} from 'node:crypto';

const dir=path.resolve(process.argv[2]),scenario=process.argv[3];
let now=1700000000000,seq=0;
const timers=new Map(),listeners=[],sockets=[],published=[],events=[],problems=[];
const noop=()=>{},evt=()=>({addListener:noop,removeListener:noop});
const setTimer=(fn,ms=0)=>{const id=++seq;timers.set(id,{fn,at:now+ms});return id;};
const store={};
const B={runtime:{onMessage:{addListener:f=>listeners.push(f)},onInstalled:evt(),onStartup:evt(),onConnect:evt(),onSuspend:evt(),
  getURL:p=>'moz-extension://test/'+p,id:'test',getManifest:()=>({version:'test'}),sendMessage:async()=>({})},
  storage:{local:{get:async key=>typeof key==='string'?{[key]:store[key]}:{...store},set:async o=>Object.assign(store,o),remove:async()=>{},clear:async()=>{}},session:{get:async()=>({}),set:async()=>{}},onChanged:evt()},
  alarms:{create:noop,onAlarm:evt()},tabs:{query:async()=>[],sendMessage:async()=>{},onUpdated:evt(),onRemoved:evt()},
  action:{setBadgeText:noop,setBadgeBackgroundColor:noop,setTitle:noop,onClicked:evt()},
  webRequest:{onBeforeSendHeaders:evt(),onHeadersReceived:evt(),onBeforeRequest:evt()},
  contextMenus:{create:noop,onClicked:evt()},windows:{onFocusChanged:evt(),create:async()=>({})},idle:{onStateChanged:evt()},
  scripting:{executeScript:async()=>[]},permissions:{contains:async()=>true},commands:{onCommand:evt()}};
const clockDate=class extends Date {static now(){return now;}};
let T,appSk,appPk,remoteSk,remotePk,foreignSk;
let closing=false,dropReply=false,zombie=false,reject=false,forged=false,failSubscription=false,approveAt=0;
const awaitingApproval=new Set();
class Socket {
  constructor(url){this.url=url;this.readyState=0;this.subscribed=false;sockets.push(this);
    if(url.includes('stalled'))return;
    setTimer(()=>{
      if(this.readyState!==0)return;
      if(url.includes('closed')){this.readyState=3;this.onclose?.({code:1001});return;}
      this.readyState=1;this.onopen?.();
    },0);
  }
  addEventListener(){}
  close(){if(this.readyState===3)return;this.readyState=3;setTimer(()=>this.onclose?.({code:1001}),0);}
  send(wire){
    assert.equal(this.readyState,1);const m=JSON.parse(wire);
    if(m[0]==='REQ'&&m[1]==='pcn46'){
      if(failSubscription){failSubscription=false;throw new Error('subscription send failed');}
      this.subscribed=true;events.push('REQ');return;
    }
    if(m[0]!=='EVENT'||m[1].kind!==24133)return;
    assert(this.subscribed,'request sent before subscription');
    if(!verify(m[1])){const error=new Error('Request signature invalid');problems.push(error);throw error;}
    published.push({wire,socket:this});events.push('EVENT');
    if(now<approveAt){
      if(!awaitingApproval.has(m[1].id)){
        awaitingApproval.add(m[1].id);
        setTimer(()=>{const current=sockets.findLast(w=>w.readyState===1&&w.subscribed);if(current)respond(current,m[1]).catch(e=>problems.push(e));},approveAt-now);
      }
      return;
    }
    if(closing){closing=false;this.close();return;}
    if(zombie){this.zombie=true;zombie=false;return;}
    if(this.zombie)return;
    if(dropReply){dropReply=false;return;}
    respond(this,m[1]).catch(e=>problems.push(e));
  }
}
async function respond(socket,event){
  const nip44=!event.content.includes('?iv=');
  const request=JSON.parse(nip44?T.nip44.v2.decrypt(event.content,T.nip44.v2.utils.getConversationKey(remoteSk,appPk))
    :await T.nip04.decrypt(remoteSk,appPk,event.content));
  let result=request.method==='sign_event'?JSON.stringify(sign(JSON.parse(request.params[0]),remoteSk)):'answer:'+request.id;
  if(request.method==='nip44_decrypt')result=T.nip44.v2.decrypt(request.params[1],T.nip44.v2.utils.getConversationKey(remoteSk,request.params[0]));
  const secret=forged?foreignSk:remoteSk;
  const responseBody=JSON.stringify(reject?{id:request.id,error:'not permitted'}:{id:request.id,result});
  const content=nip44?T.nip44.v2.encrypt(responseBody,T.nip44.v2.utils.getConversationKey(secret,appPk))
    :await T.nip04.encrypt(secret,appPk,responseBody);
  let response=sign({kind:24133,created_at:Math.floor(now/1000),tags:[['p',appPk]],content},secret);
  if(scenario==='bad-signature')response={...response,sig:'0'.repeat(128)};
  if(scenario==='wrong-recipient')response=sign({kind:24133,created_at:Math.floor(now/1000),tags:[['p',remotePk]],content},remoteSk);
  if(socket.readyState===1)socket.onmessage?.({data:JSON.stringify(['EVENT','pcn46',response])});
}
const ctx={browser:B,chrome:B,console,crypto:webcrypto,WebSocket:Socket,Date:clockDate,TextEncoder,TextDecoder,URL,URLSearchParams,
  Uint8Array,Uint32Array,ArrayBuffer,DataView,Promise,Map,Set,WeakMap,WeakSet,Math,Number,Error,AbortController,
  setTimeout:setTimer,clearTimeout:id=>timers.delete(id),setInterval:noop,clearInterval:noop,queueMicrotask,performance,
  atob:s=>Buffer.from(s,'base64').toString('binary'),btoa:s=>Buffer.from(s,'binary').toString('base64'),
  navigator:{onLine:true,userAgent:'Firefox'},fetch:async()=>({ok:false,json:async()=>({})})};
ctx.self=ctx;ctx.globalThis=ctx;vm.createContext(ctx);
for(const file of ['vendor/nostr.bundle.js','vaultcore.js','background.js'])vm.runInContext(fs.readFileSync(path.join(dir,file),'utf8'),ctx,{filename:file});
await vm.runInContext('ready',ctx);
T=ctx.NostrTools;appSk=new Uint8Array(32);appSk[31]=7;remoteSk=new Uint8Array(32);remoteSk[31]=8;foreignSk=new Uint8Array(32);foreignSk[31]=9;
// Nostr's validator checks array identity; represent relay JSON in the background's realm.
function realm(value){ctx.jsonValue=JSON.stringify(value);return vm.runInContext('JSON.parse(jsonValue)',ctx);}
function verify(event){return T.verifyEvent(realm(event));}
function sign(event,secret){return T.finalizeEvent(realm(event),secret);}
appPk=T.getPublicKey(appSk);remotePk=T.getPublicKey(remoteSk);
const relays=scenario==='first-stalled'?['wss://stalled.test','wss://healthy.test']:scenario==='close-before-open'?['wss://closed.test']:scenario==='dial-timeout'?['wss://stalled.test']:['wss://healthy.test'];
ctx.fixture={pubkey:remotePk,key:Buffer.alloc(32,1).toString('base64'),mode:'full',nip46:{sk:Buffer.from(appSk).toString('hex'),remotePk,enc:scenario.includes('nip44')?'nip44':'nip04',relays}};
vm.runInContext('cfg=fixture;key=V.fromB64(cfg.key)',ctx);
store.nostrPerms={'https://poster.place|signEvent|27235':'allow'};
store.nostrPerms['https://poster.place|nip44.decrypt']='allow';
const started=now;
const realStarted=performance.now(),latency=[];
async function flush(){for(let i=0;i<12;i++)await new Promise(r=>setImmediate(r));if(problems.length)throw problems[0];}
async function advance(ms){const end=now+ms;for(let i=0;i<1000;i++){
  await flush();let next=[...timers].filter(([,t])=>t.at<=end).sort((a,b)=>a[1].at-b[1].at)[0];
  if(!next){now=end;await flush();return;}timers.delete(next[0]);now=next[1].at;next[1].fn();
}throw Error('timer storm');}
function track(p){const state={done:false};Promise.resolve(p).then(v=>{state.done=true;state.value=v;},e=>{state.done=true;state.error=e;state.errorText=String(e.stack||e);});return state;}
async function until(state,ms=10000){for(let i=0;i<ms/100&&!state.done;i++)await advance(100);return state;}
const call=()=>vm.runInContext("N46.rpc('get_public_key',[])",ctx);
const handler=(method,params)=>new Promise(resolve=>listeners[0]({type:'nostr',method,params},{url:'https://poster.place/os'},resolve));
let result;
if(scenario==='healthy'||scenario==='first-stalled'||scenario==='healthy-nip44'){
  result=await until(track(call()));assert(result.done&&!result.error,JSON.stringify({result,published:published.length,events,state:vm.runInContext('({generation:N46.generation,pending:N46.pending.size,sockets:N46.sockets.map(w=>w.readyState)})',ctx)}));assert(now-started<2000);assert.equal(published.length,1);
}else if(scenario==='close-before-open'||scenario==='dial-timeout'){
  result=await until(track(vm.runInContext('N46.open()',ctx)),5000);assert(result.done&&result.error);assert(now-started<=4500);
}else if(scenario==='repeated-auth-dm-latency'){
  const peerPk=T.getPublicKey(foreignSk),plain='DM plaintext after recovery';
  const ciphertext=T.nip44.v2.encrypt(plain,T.nip44.v2.utils.getConversationKey(foreignSk,remotePk));
  for(let i=0;i<7;i++){
    if(i===3)closing=true;
    for(const method of ['signEvent','nip44.decrypt']){
      const start=now,realStart=performance.now();
      const params=method==='signEvent'?{event:{kind:27235,tags:[['u','https://poster.place/api/auth'],['method','POST']],content:''}}
        :{pubkey:peerPk,ciphertext};
      const answer=await until(track(handler(method,params)));assert(answer.done&&!answer.error);assert.equal(answer.value?.ok,true);
      if(method==='signEvent')assert(verify(answer.value.result));else assert.equal(answer.value.result,plain);
      const elapsed=now-start;assert(elapsed<(i===3&&method==='signEvent'?10000:2000));
      latency.push({method,phase:i<3?'before':i===3?'recovery':'after',virtualMs:elapsed,realMs:Math.round(performance.now()-realStart)});
    }
  }
}else if(scenario==='concurrent-auth-dm'){
  closing=true;dropReply=true;
  const peerPk=T.getPublicKey(foreignSk),plain='concurrent DM restore';
  const ciphertext=T.nip44.v2.encrypt(plain,T.nip44.v2.utils.getConversationKey(foreignSk,remotePk));
  const requests=Array.from({length:24},(_,i)=>track(i%2?handler('nip44.decrypt',{pubkey:peerPk,ciphertext})
    :handler('signEvent',{event:{kind:27235,tags:[['u','https://poster.place/api/auth']],content:''}})));
  for(let i=0;i<requests.length;i++){
    const answer=requests[i];await until(answer);assert(answer.done&&!answer.error);assert.equal(answer.value?.ok,true);
    if(i%2)assert.equal(answer.value.result,plain);else assert(verify(answer.value.result));
  }
  assert(now-started<10000);
  const counts=new Map();for(const p of published){const id=JSON.parse(p.wire)[1].id;counts.set(id,(counts.get(id)||0)+1);}
  assert.equal(counts.size,24);assert([...counts.values()].every(n=>n<=3));assert(published.length<=48);
}else if(scenario==='stale-callback'){
  closing=true;result=await until(track(call()));assert(result.done&&!result.error);
  const old=sockets[0],replacement=sockets.at(-1);assert.notEqual(old,replacement);
  old.onclose?.({code:1001});old.onopen?.();await flush();
  assert(vm.runInContext('N46.sockets.includes',ctx).call(vm.runInContext('N46.sockets',ctx),replacement));
  result=await until(track(call()));assert(result.done&&!result.error);
}else if(scenario.startsWith('dm-')){
  closing=scenario.includes('close');dropReply=scenario.includes('drop');
  const plain='a real DM after the relay restarted',peerPk=T.getPublicKey(foreignSk);
  const ciphertext=T.nip44.v2.encrypt(plain,T.nip44.v2.utils.getConversationKey(foreignSk,remotePk));
  result=track(new Promise(resolve=>listeners[0]({type:'nostr',method:'nip44.decrypt',params:{pubkey:peerPk,ciphertext}},{url:'https://poster.place/os'},resolve)));
  await until(result);assert(result.done&&!result.error);assert.equal(result.value?.ok,true);assert.equal(result.value?.result,plain);
  assert(published.length>=2);assert.equal(new Set(published.map(p=>p.wire)).size,1);
}else if(scenario==='approval-delay'){
  approveAt=now+45000;result=track(call());await advance(35000);assert.equal(result.done,false);
  await until(result,25000);assert(result.done&&!result.error);assert(now-started>=45000&&now-started<120000);
  assert.equal(new Set(published.map(p=>p.wire)).size,1);
  assert.equal(published.length,5,'initial publication plus only four approval-safe retries');
}else if(scenario==='subscription-failure'){
  failSubscription=true;result=await until(track(call()));assert(result.done&&!result.error);assert.equal(published.length,1);assert(sockets.length>=2);
}else if(['close-after-publish','dropped-reply','zombie','auth-message'].includes(scenario)){
  closing=scenario==='close-after-publish'||scenario==='auth-message';dropReply=scenario==='dropped-reply';zombie=scenario==='zombie';
  if(scenario==='auth-message'){
    result=track(new Promise(resolve=>listeners[0]({type:'nostr',method:'signEvent',params:{event:{kind:27235,created_at:Math.floor(now/1000),tags:[['u','https://poster.place/api/monero/status'],['method','GET']],content:''}}},{url:'https://poster.place/os'},resolve)));
  }else result=track(call());
  await until(result);assert(result.done&&!result.error,JSON.stringify(result));
  if(scenario==='auth-message'){assert(result.value.ok);assert(verify(result.value.result));assert.equal(result.value.result.kind,27235);}
  assert(published.length>=2);assert.equal(new Set(published.map(p=>p.wire)).size,1,'retry must preserve the signed event byte for byte');
  assert(now-started<10000);
}else if(scenario==='concurrent'){
  closing=true;const requests=Array.from({length:8},()=>track(call()));
  for(const q of requests){await until(q);assert(q.done&&!q.error);}
  const ids=published.map(p=>JSON.parse(p.wire)[1].id);assert.equal(new Set(ids).size,8);
}else if(scenario==='rejection'){
  reject=true;result=await until(track(call()));assert.match(result.error?.message||'',/not permitted/);const count=published.length;await advance(6000);assert.equal(published.length,count);
}else if(['wrong-peer','bad-signature','wrong-recipient'].includes(scenario)){
  forged=scenario==='wrong-peer';result=track(call());await advance(2500);assert.equal(result.done,false,'unauthenticated response accepted');
  vm.runInContext('N46.reset()',ctx);await flush();assert(result.error);
}else if(scenario==='cancel'||scenario==='session-change'){
  zombie=true;ctx.controller=new AbortController();result=track(vm.runInContext("N46.rpc('get_public_key',[],controller.signal)",ctx));await advance(100);
  if(scenario==='cancel')ctx.controller.abort();else await vm.runInContext('unpair()',ctx);
  await flush();assert(result.done&&result.error);const count=published.length;await advance(8000);assert.equal(published.length,count);
}else if(scenario==='permission-denied'){
  store.nostrPerms['https://poster.place|signEvent|27235']='deny';
  result=await until(track(new Promise(resolve=>listeners[0]({type:'nostr',method:'signEvent',params:{event:{kind:27235,tags:[],content:''}}},{url:'https://poster.place/os'},resolve))));
  assert.equal(result.value?.ok,false);assert.equal(result.value?.error,'refused');assert.equal(published.length,0);
}else throw Error('unknown scenario');
vm.runInContext('if(N46.reset)N46.reset();else for(const w of N46.sockets)w.close()',ctx);await flush();
console.log(JSON.stringify({scenario,elapsed:now-started,realMs:Math.round(performance.now()-realStarted),published:published.length,latency,ok:true}));
