// A relay that REFUSES a read after seeing who asks is `refused` (and still `failed`); one that never answers is only
// `failed`. Concord's invite registry may then go ahead past relay.damus.io (2026-10-26: "error when creating an invite
// link to my own room, complains about damus"). Runs the SHIPPED relay.js, and the SHIPPED concord.js helper lifted out.
import fs from 'fs';
import vm from 'vm';
import assert from 'node:assert/strict';
import { clientSourceAt } from './client_source.mjs';
class Socket {
  static all=[];
  constructor(url){this.url=url;this.readyState=0;this.sent=[];Socket.all.push(this);}
  open(){this.readyState=1;this.onopen?.();}
  send(wire){this.sent.push(JSON.parse(wire));}
  receive(packet){return this.onmessage?.({data:JSON.stringify(packet)});}
  close(){if(this.readyState===3)return;this.readyState=3;this.onclose?.();}
}
Object.assign(globalThis,{window:globalThis,self:globalThis,WebSocket:Socket,document:{addEventListener(){}},location:{origin:'https://fixture.test',protocol:'https:'}});
Object.defineProperty(globalThis,'navigator',{value:{onLine:true}});
globalThis.Worker=class {postMessage(m){queueMicrotask(()=>this.onmessage({data:{id:m.id,ok:true,data:m.args.events.map(e=>({id:e.id,valid:true}))}}));}};
vm.runInThisContext(clientSourceAt(process.argv[2]));
const tick=()=>new Promise(r=>setImmediate(r));
const owner='a'.repeat(64);
Relay.setAuthSigner(async tpl=>({...tpl,pubkey:owner,id:'auth-'+Math.random()}),()=>owner);
const filter={kinds:[1059],authors:['b'.repeat(64)]};
const REFUSE='ERROR: auth-required: requested filter requires authentication';

// 1) damus: challenge + refuse, AUTH accepted, the replayed read refused AGAIN -> refused (+ failed)
let report={};
let pending=Relay.queryFrom(['wss://damus.fixture','wss://open.fixture'],[filter],{timeout:3000,exact:true,report,urgent:true});
const [damus, open] = Socket.all.slice(-2);
damus.open(); open.open();
const idD=damus.sent[0][1], idO=open.sent[0][1];
damus.receive(['AUTH','chal']); damus.receive(['CLOSED',idD,REFUSE]); await tick(); await tick();
const auth=damus.sent.find(x=>x[0]==='AUTH'); assert.ok(auth,'the client never signed in to the refusing relay');
damus.receive(['OK',auth[1].id,true]); await tick();
const reqs=damus.sent.filter(x=>x[0]==='REQ'); assert.equal(reqs.length,2,'the read was not replayed after AUTH');
damus.receive(['CLOSED',reqs[1][1],REFUSE]);
open.receive(['EOSE',idO]);
await pending;
assert.deepEqual(report.ok,['wss://open.fixture'],JSON.stringify(report));
assert.deepEqual(report.refused,['wss://damus.fixture'],'a relay that refused after AUTH is not "refused": '+JSON.stringify(report));
assert.ok(report.failed.includes('wss://damus.fixture'),'every other caller must still see it as failed');

// 2) a relay that never answers is a failure, never a refusal
report={};
pending=Relay.queryFrom(['wss://silent.fixture'],[filter],{timeout:400,exact:true,report,urgent:true});
Socket.all.at(-1).open();
await pending;
assert.ok(!(report.refused||[]).length,'silence counted as a refusal: '+JSON.stringify(report));

// 3) concord.js: a refusal is not a missing answer -- if, and only if, some relay answered
const src=fs.readFileSync(process.argv[3],'utf8');
const grab=name=>{const i=src.indexOf('function '+name+'(');assert.ok(i>=0,name+' is gone');let d=0,j=src.indexOf('{',i);for(;j<src.length;j++){if(src[j]==='{')d++;else if(src[j]==='}'&&--d===0)break;}return src.slice(i,j+1);};
const helpers=new Function(grab('unanswered')+grab('unansweredBesidesRefusals')+';return {unanswered,unansweredBesidesRefusals};')();
const R=['wss://a','wss://damus','wss://c'];
assert.deepEqual(helpers.unansweredBesidesRefusals(R,{ok:['wss://a','wss://c'],refused:['wss://damus'],failed:['wss://damus']}),[]);
assert.deepEqual(helpers.unansweredBesidesRefusals(R,{ok:['wss://a'],refused:['wss://damus'],failed:['wss://damus','wss://c']}),['wss://c'],'a relay that did not answer was excused');
assert.deepEqual(helpers.unansweredBesidesRefusals(R,{ok:[],refused:['wss://damus'],failed:R}),R,'refusals alone cannot complete a read');
console.log('refused reads PASS'); process.exit(0);
