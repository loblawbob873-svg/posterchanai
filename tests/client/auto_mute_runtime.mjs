import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
const context=vm.createContext({console,TextEncoder,TextDecoder,Uint8Array,crypto:globalThis.crypto});
vm.runInContext(fs.readFileSync(process.argv[3],'utf8'),context);
vm.runInContext(fs.readFileSync(process.argv[2],'utf8'),context);
const NT=context.NostrTools, owner=NT.getPublicKey(Uint8Array.from({length:32},(_,i)=>i===31?1:0));
const key=n=>Uint8Array.from({length:32},(_,i)=>i===31?n:0),pk=n=>NT.getPublicKey(key(n));
const clock=2000000000000;
function event(n,muted,time=100,tags=[]){
 context.fixture=JSON.stringify({kind:10000,created_at:time,tags:[...(muted?[['p',owner]]:[]),...tags],content:''});
 context.sk=Array.from(key(n));
 return vm.runInContext('NostrTools.finalizeEvent(JSON.parse(fixture),Uint8Array.from(sk))',context);
}
const rows=(events,complete=true)=>Object.defineProperty([...events],'complete',{value:complete});
let passed=0;
async function test(name,fn){await fn();passed++;console.log('PASS '+name);}
function setup({cached=null,latest=[],discovery=latest,query,writeFail=false}={}){
 let active=owner,saved=cached,writes=0,changes=0;const calls=[];
 const engine=context.PCAutoMute.create({owner,now:()=>clock,read:()=>cached,
  write:s=>{if(writeFail)throw Error('disk full');saved=JSON.parse(JSON.stringify(s));writes++;},
  isCurrent:who=>active===who,verify:e=>NT.verifyEvent(e),onChange:()=>changes++,
  query:async f=>{calls.push(f);return query?query(f):rows(f[0]['#p']?discovery:latest);}});
 return {engine,calls,get saved(){return saved;},get writes(){return writes;},get changes(){return changes;},switchAccount:()=>{active=pk(9);}};
}
const enabled=s=>s.engine.setEnabled(true);
await test('off by default performs no relay reads',async()=>{const s=setup();assert.equal((await s.engine.update()).ok,false);assert.equal(s.calls.length,0);});
await test('verified public mute is applied',async()=>{const s=setup({latest:[event(2,true)]});enabled(s);assert.equal((await s.engine.update()).added,1);assert(s.engine.has(pk(2)));});
await test('reconciliation never queries the owner manual list',async()=>{const s=setup({latest:[event(2,true)]});enabled(s);assert.equal((await s.engine.update()).ok,true);assert.equal(s.calls.length,2);assert(s.calls.every(f=>!f[0].authors?.includes(owner)));});
await test('duplicate refresh is idempotent',async()=>{const s=setup({latest:[event(2,true)]});enabled(s);await s.engine.update();assert.equal((await s.engine.update()).added,0);assert.equal(s.engine.list().length,1);});
await test('discovery candidate must still mute in newest list',async()=>{const s=setup({discovery:[event(2,true,100)],latest:[event(2,false,101)]});enabled(s);await s.engine.update();assert(!s.engine.has(pk(2)));});
await test('known authors queried without p to discover unmute',async()=>{const a=setup({latest:[event(2,true)]});enabled(a);await a.engine.update();const s=setup({cached:a.saved,discovery:[],latest:[event(2,false,101)]});assert.equal((await s.engine.update()).removed,1);assert(!s.engine.has(pk(2)));assert(s.calls[1][0].authors.includes(pk(2)));assert(!s.calls[1][0]['#p']);});
await test('missing author response does not imply unmute',async()=>{const a=setup({latest:[event(2,true)]});enabled(a);await a.engine.update();const s=setup({cached:a.saved,latest:[]});await s.engine.update();assert(s.engine.has(pk(2)));});
await test('stale list cannot undo a newer unmute',async()=>{const a=setup({discovery:[event(2,true)],latest:[event(2,false,101)]});enabled(a);await a.engine.update();const s=setup({cached:a.saved,latest:[event(2,true,100)]});await s.engine.update();assert(!s.engine.has(pk(2)));});
await test('stale unmute cannot remove a newer mute',async()=>{const a=setup({latest:[event(2,true,101)]});enabled(a);await a.engine.update();const s=setup({cached:a.saved,latest:[event(2,false,100)]});await s.engine.update();assert(s.engine.has(pk(2)));});
await test('equal timestamp uses Nostr lowest event ID',async()=>{const yes=event(2,true),no=event(2,false);const s=setup({discovery:[yes],latest:[yes,no]});enabled(s);await s.engine.update();assert.equal(s.engine.has(pk(2)),yes.id<no.id);});
await test('signature forgery is ignored',async()=>{const good=event(2,true);const bad=vm.runInContext('JSON.parse(JSON.stringify(fixtureBad))',Object.assign(context,{fixtureBad:good}));bad.pubkey=pk(3);const s=setup({latest:[bad]});enabled(s);await s.engine.update();assert.equal(s.engine.list().length,0);});
await test('future timestamps are ignored',async()=>{const s=setup({latest:[event(2,true,Math.floor(clock/1000)+301)]});enabled(s);await s.engine.update();assert.equal(s.engine.list().length,0);});
await test('incomplete discovery leaves prior state intact',async()=>{const a=setup({latest:[event(2,true)]});enabled(a);await a.engine.update();const s=setup({cached:a.saved,query:async()=>rows([],false)});assert.equal((await s.engine.update()).ok,false);assert(s.engine.has(pk(2)));assert.equal(s.writes,0);});
await test('incomplete latest query commits no partial result',async()=>{let n=0;const s=setup({query:async()=>++n===1?rows([event(2,true)]):rows([event(2,true)],false)});enabled(s);assert.equal((await s.engine.update()).ok,false);assert(!s.engine.has(pk(2)));});
await test('relay exception does not clear filtering',async()=>{const s=setup({query:async()=>{throw Error('offline');}});enabled(s);assert.equal((await s.engine.update()).ok,false);assert.equal(s.engine.snapshot().lastChecked,0);});
await test('manual unmute exception survives updates',async()=>{const s=setup({latest:[event(2,true)]});enabled(s);await s.engine.update();s.engine.exclude(pk(2));await s.engine.update();assert(!s.engine.has(pk(2)));});
await test('fresh unmute resets exception for a later remute',async()=>{const a=setup({latest:[event(2,true)]});enabled(a);await a.engine.update();a.engine.exclude(pk(2));const b=setup({cached:a.saved,latest:[event(2,false,101)]});await b.engine.update();const s=setup({cached:b.saved,latest:[event(2,true,102)]});await s.engine.update();assert(s.engine.has(pk(2)));});
await test('disabling removes only automatic filtering',async()=>{const manual=new Set([pk(3)]),s=setup({latest:[event(2,true)]});enabled(s);await s.engine.update();s.engine.setEnabled(false);assert(!s.engine.has(pk(2)));assert.equal(s.engine.list().length,0);assert(manual.has(pk(3)));});
await test('account switch during read prevents commit',async()=>{let release;const s=setup({query:()=>new Promise(r=>release=r)});enabled(s);const job=s.engine.update();s.switchAccount();release(rows([event(2,true)]));assert.equal((await job).ok,false);assert.equal(s.writes,1);assert(!s.engine.has(pk(2)));});
await test('disable during read prevents commit',async()=>{let release;const s=setup({query:()=>new Promise(r=>release=r)});enabled(s);const job=s.engine.update();s.engine.setEnabled(false);release(rows([event(2,true)]));assert.equal((await job).ok,false);assert.equal(s.writes,2);});
await test('concurrent Update now clicks share one check',async()=>{let release;const s=setup({query:()=>new Promise(r=>release=r)});enabled(s);const a=s.engine.update(),b=s.engine.update();assert.equal(a,b);release(rows([]));await a;assert.equal(s.calls.length,1);});
await test('wrong-account cache is discarded',async()=>{const s=setup({cached:{owner:pk(3),enabled:true,records:{[pk(2)]:{id:'0'.repeat(64),created_at:100,muted:true}}}});assert(!s.engine.has(pk(2)));assert.equal(s.engine.snapshot().enabled,false);});
await test('cached automatic mutes survive reload',async()=>{const a=setup({latest:[event(2,true)]});enabled(a);await a.engine.update();assert(setup({cached:a.saved}).engine.has(pk(2)));});
await test('private content alone cannot reveal mute',async()=>{const s=setup({latest:[event(2,false)]});enabled(s);await s.engine.update();assert.equal(s.engine.list().length,0);});
await test('failed settings write leaves enabled state unchanged',async()=>{const s=setup({writeFail:true});assert.throws(()=>s.engine.setEnabled(true),/disk full/);assert.equal(s.engine.snapshot().enabled,false);assert.equal(s.writes,0);});
await test('failed reconciliation write retains last durable mute',async()=>{const a=setup({latest:[event(2,true)]});enabled(a);await a.engine.update();const s=setup({cached:a.saved,latest:[event(2,false,101)],writeFail:true});assert.equal((await s.engine.update()).ok,false);assert(s.engine.has(pk(2)));assert.deepEqual(JSON.parse(JSON.stringify(s.engine.snapshot())),a.saved);assert.equal(s.changes,0);});
await test('failed exception write does not silently unmute',async()=>{const a=setup({latest:[event(2,true)]});enabled(a);await a.engine.update();const s=setup({cached:a.saved,writeFail:true});assert.throws(()=>s.engine.exclude(pk(2)),/disk full/);assert(s.engine.has(pk(2)));assert.equal(s.changes,0);});
console.log('TOTAL '+passed);
