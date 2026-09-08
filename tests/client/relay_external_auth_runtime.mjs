import fs from 'fs';
import vm from 'vm';
import assert from 'node:assert/strict';
class Socket {
  static all=[];
  constructor(url){this.url=url;this.readyState=0;this.sent=[];Socket.all.push(this);}
  open(){this.readyState=1;this.onopen?.();}
  send(wire){assert.equal(this.readyState,1);this.sent.push(JSON.parse(wire));}
  receive(packet){return this.onmessage?.({data:JSON.stringify(packet)});}
  close(){if(this.readyState===3)return;this.readyState=3;this.onclose?.();}
}
Object.assign(globalThis,{window:globalThis,self:globalThis,WebSocket:Socket,document:{addEventListener(){}},location:{origin:'https://fixture.test',protocol:'https:'}});
Object.defineProperty(globalThis,'navigator',{value:{onLine:true}});
globalThis.Worker=class {postMessage(m){queueMicrotask(()=>this.onmessage({data:{id:m.id,ok:true,data:m.args.events.map(e=>({id:e.id,valid:true}))}}));}};
vm.runInThisContext(fs.readFileSync(process.argv[2],'utf8'));
const tick=()=>new Promise(resolve=>setImmediate(resolve));
let owner='a'.repeat(64),signs=[],release=null,deferred=false;
Relay.setAuthSigner(async tpl=>{signs.push(tpl);if(deferred)await new Promise(r=>release=r);return{...tpl,pubkey:owner,id:'auth-'+signs.length};},()=>owner);
const filter={kinds:[1059],authors:['b'.repeat(64)]};
const authOf=s=>s.sent.find(x=>x[0]==='AUTH');
function challenge(s,id,value='challenge'){s.receive(['AUTH',value]);s.receive(['CLOSED',id,'ERROR: auth-required: requested filter requires authentication']);}
// Actual external query must not turn an AUTH-required CLOSED into a successful empty read.
let pending=Relay.queryFrom(['wss://query.fixture'],[filter],{timeout:1000,exact:true});let s=Socket.all.at(-1);s.open();let id=s.sent[0][1];
challenge(s,id);s.receive(['CLOSED',id,'auth-required: repeated']);await tick();
assert.equal(signs.length,1);assert.equal(authOf(s)[1].pubkey,owner);assert.deepEqual(authOf(s)[1].tags,[['relay',s.url],['challenge','challenge']]);
s.receive(['OK','unrelated',true]);assert.equal(s.sent.filter(x=>x[0]==='REQ').length,1);
s.receive(['OK',authOf(s)[1].id,true]);assert.equal(s.sent.filter(x=>x[0]==='REQ').length,2);
s.receive(['EVENT',id,{id:'history',kind:1059,tags:[]}]);s.receive(['EOSE',id]);assert.deepEqual((await pending).map(e=>e.id),['history']);assert.equal(s.readyState,3);
// An account change while signing cannot leak old proof or return old-account events.
deferred=true;pending=Relay.queryFrom(['wss://owner.fixture'],[filter],{timeout:1000,exact:true});s=Socket.all.at(-1);s.open();challenge(s,s.sent[0][1]);await tick();owner='c'.repeat(64);release();assert.deepEqual(await pending,[]);assert.equal(authOf(s),undefined);deferred=false;
// Live reconnect keeps filter/subscription identity and rejects retired signer/verification callbacks.
const got=[];let stop=Relay.subscribeFrom(['wss://live.fixture'],[filter],{live:true,timeout:0,onEvent:e=>got.push(e)});s=Socket.all.at(-1);s.open();id=s.sent[0][1];
deferred=true;challenge(s,id,'old');await tick();const old=s,oldRelease=release;old.close();await new Promise(r=>setTimeout(r,1050));s=Socket.all.at(-1);assert.notEqual(s,old);s.open();deferred=false;challenge(s,id,'new');await tick();oldRelease();await tick();assert.equal(authOf(old),undefined);assert.deepEqual(authOf(s)[1].tags,[['relay',s.url],['challenge','new']]);
s.receive(['OK',authOf(s)[1].id,true]);s.receive(['EVENT',id,{id:'live-event',kind:1059,tags:[]}]);await tick();assert.deepEqual(got.map(e=>e.id),['live-event']);stop();
// Explicit AUTH denial is terminal for this subscription, not an automatic approval/reconnect storm.
stop=Relay.subscribeFrom(['wss://denied.fixture'],[filter],{live:true,timeout:0});s=Socket.all.at(-1);s.open();challenge(s,s.sent[0][1]);await tick();let before=signs.length;s.receive(['OK',authOf(s)[1].id,false,'denied']);s.receive(['AUTH','another']);await tick();assert.equal(signs.length,before);assert.equal(s.readyState,3);stop();
// A live handle cannot reconnect its old filters under a different signed-in account.
stop=Relay.subscribeFrom(['wss://account-live.fixture'],[filter],{live:true,timeout:0});s=Socket.all.at(-1);s.open();
const socketCount=Socket.all.length;owner='e'.repeat(64);s.close();await new Promise(r=>setTimeout(r,1050));assert.equal(Socket.all.length,socketCount);stop();
// Room publish AUTH is the user, never the random wrap author; replay exact signed EVENT bytes.
const wrap={id:'wrap',kind:1059,pubkey:'d'.repeat(64),tags:[],content:'cipher'};
pending=Relay.publishTo(['wss://publish.fixture'],wrap,{includeManaged:true,detailed:true,timeout:1000});s=Socket.all.at(-1);s.open();s.receive(['AUTH','publish-challenge']);s.receive(['OK',wrap.id,false,'ERROR: auth-required: user must authenticate']);await tick();assert.equal(authOf(s)[1].pubkey,owner);s.receive(['OK',authOf(s)[1].id,true]);assert.deepEqual(s.sent.filter(x=>x[0]==='EVENT'),[['EVENT',wrap],['EVENT',wrap]]);s.receive(['OK',wrap.id,true]);assert.equal((await pending).accepted,1);
// Denied signer closes a query; no AUTH or replay is synthesized.
Relay.setAuthSigner(()=>Promise.reject(new Error('declined')),()=>owner);
pending=Relay.queryFrom(['wss://declined.fixture'],[filter],{timeout:1000,exact:true});s=Socket.all.at(-1);s.open();challenge(s,s.sent[0][1]);assert.deepEqual(await pending,[]);assert.equal(authOf(s),undefined);
// Held opening sockets cannot publish or query after an account switch.
Relay.setAuthSigner(async tpl=>({...tpl,pubkey:owner,id:'managed-auth'}),()=>owner);
pending=Relay.publishTo(['wss://held-publish.fixture'],wrap,{includeManaged:true,detailed:true,timeout:1000});s=Socket.all.at(-1);owner='f'.repeat(64);s.open();assert.equal(s.sent.length,0);let refused=await pending;assert.equal(refused.ok,false);assert.equal(refused.uncertain,false);
pending=Relay.queryFrom(['wss://held-query.fixture'],[filter],{exact:true,timeout:1000});s=Socket.all.at(-1);owner='a'.repeat(64);s.open();assert.deepEqual(await pending,[]);assert.equal(s.sent.length,0);
// The same Vector relay may already be managed: authenticate as the user, not stream author.
Relay.configure({urls:['wss://managed.fixture'],verify:false});s=Socket.all.at(-1);s.open();s.receive(['AUTH','managed-challenge']);
id=Relay.subscribe([filter],{live:true,onEvent(){}});s.receive(['CLOSED',id,'ERROR: auth-required: requested filter requires authentication']);await tick();assert.equal(authOf(s)[1].pubkey,owner);s.receive(['OK',authOf(s)[1].id,true]);await tick();assert.equal(s.sent.filter(x=>x[0]==='REQ'&&x[1]===id).length,2);Relay.close(id);
console.log('external query/live/reconnect/owner/denial/room publication AUTH PASS');process.exit(0);
