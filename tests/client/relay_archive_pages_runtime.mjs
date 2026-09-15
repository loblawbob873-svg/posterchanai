import fs from 'node:fs';
import vm from 'node:vm';
import assert from 'node:assert/strict';
const timers=new Set();
const later=(fn,ms)=>{const t=setTimeout(()=>{timers.delete(t);fn();},ms);timers.add(t);return t;};
let hold=false,releases=[];
const ctx={console,Map,Set,Promise,Date,Math,JSON,URL,Error,
 setTimeout:later,clearTimeout:t=>{clearTimeout(t);timers.delete(t);},setInterval,clearInterval,
 document:{addEventListener(){}},navigator:{onLine:true},location:{protocol:'https:',host:'app.test'},
 Worker:class{postMessage(m){const answer=()=>this.onmessage({data:{id:m.id,ok:true,
   data:m.args.events.map(e=>({id:e.id,valid:e.sig==='good'}))}});if(hold)releases.push(answer);else later(answer,0);}},
};ctx.window=ctx;ctx.self=ctx;vm.createContext(ctx);
vm.runInContext(fs.readFileSync(new URL('../../static/js/client/relay.js',import.meta.url),'utf8'),ctx);
const R=ctx.Relay;
function start(timeout=100){
 R._conns.clear();const conns=['a','b'].map((url,i)=>({url,trusted:i===0,ws:{readyState:1},_send:()=>true}));
 conns.forEach(c=>R._conns.set(c.url,c));
 const result=R.query([{kinds:[30078],authors:['me']}],timeout,{pages:true});
 const id=[...R._subs.keys()].at(-1);
 return {result,id,conns,emit:(i,type,ev)=>R._onMessage(conns[i],JSON.stringify([type,id,ev]))};
}
const ev=id=>({id,pubkey:'me',kind:30078,created_at:100,tags:[],sig:'good'});
try{
 let t=start();t.emit(0,'EVENT',ev('x'));t.emit(0,'EVENT',ev('y'));
 t.emit(1,'EVENT',ev('x'));t.emit(1,'EVENT',ev('z'));t.emit(1,'EVENT',ev('z'));
 t.emit(0,'EOSE');t.emit(1,'EOSE');let out=await t.result;
 assert.equal(out.complete,true);assert.equal(out.length,3);assert.deepEqual([...out.pageCounts],[2,2]);
 assert(!Object.keys(out).includes('pageCounts'));assert(Object.isFrozen(out.pageCounts));
 assert.deepEqual(JSON.parse(JSON.stringify(out.pageBounds)),[[100,'x'],[100,'x']]);
 assert(Object.isFrozen(out.pageBounds)&&Object.isFrozen(out.pageBounds[0]));
 assert(!Object.keys(out).includes('pageBounds'));
 assert(!JSON.stringify(out).includes('pageCounts'));
 // Same-ID invalid content must not borrow the other relay's valid verification verdict.
 t=start();t.emit(0,'EVENT',ev('x'));t.emit(1,'EVENT',{...ev('x'),sig:'bad'});
 t.emit(0,'EOSE');t.emit(1,'EOSE');out=await t.result;
 assert.equal(out.complete,false);assert.deepEqual([...out.pageCounts],[1,0]);assert.equal(out.pageBounds[1],null);
 for(const mode of ['CLOSED','drop','closedAfterEose']){
   t=start();t.emit(0,'EVENT',ev('x'));
   if(mode==='closedAfterEose'){t.emit(1,'EOSE');t.emit(1,'CLOSED','restricted');}
   else if(mode==='CLOSED')t.emit(1,'CLOSED','restricted');
   else R._connGone(t.conns[1]);
   t.emit(0,'EOSE');out=await t.result;assert.equal(out.complete,false,mode);
 }
 // Verification already in flight when EOSE arrives must finish before successful completion.
 hold=true;t=start();t.emit(1,'EVENT',ev('later'));const flush=R._flush();
 t.emit(0,'EOSE');t.emit(1,'EOSE');let done=false;t.result.then(()=>done=true);
 await new Promise(r=>setImmediate(r));assert.equal(done,false);
 releases.shift()();await flush;out=await t.result;assert.equal(out.complete,true);assert.equal(out.length,1);
 // A timed-out verification cannot mutate a result already handed to its caller.
 t=start(10);t.emit(1,'EVENT',ev('too-late'));const slow=R._flush();
 t.emit(0,'EOSE');t.emit(1,'EOSE');out=await t.result;assert.equal(out.complete,false);assert.equal(out.length,0);
 releases.shift()();await slow;assert.equal(out.length,0);assert.deepEqual([...out.pageCounts],[0,0]);hold=false;
 // Empty EOSE from every asked relay is the only complete empty answer.
 t=start();t.emit(0,'EOSE');t.emit(1,'EOSE');out=await t.result;
 assert.equal(out.complete,true);assert.deepEqual([...out.pageCounts],[0,0]);
 // A socket connecting after subscribe must join the metadata denominator as well.
 R._conns.clear();const sockets=[];
 ctx.WebSocket=class{constructor(url){this.url=url;this.readyState=0;sockets.push(this);}send(){}close(){this.readyState=3;}};
 R.configure({urls:['wss://first.test','wss://late.test'],verify:false});
 sockets[0].readyState=1;sockets[0].onopen();
 const pending=R.query([{kinds:[30078],authors:['me']}],100,{pages:true});
 const id=[...R._subs.keys()].at(-1);
 sockets[1].readyState=1;sockets[1].onopen();
 sockets[0].onmessage({data:JSON.stringify(['EVENT',id,ev('first')])});
 sockets[1].onmessage({data:JSON.stringify(['EVENT',id,ev('late')])});
 sockets[0].onmessage({data:JSON.stringify(['EOSE',id])});
 sockets[1].onmessage({data:JSON.stringify(['CLOSED',id,'restricted'])});
 out=await pending;assert.equal(out.complete,false);assert.deepEqual([...out.pageCounts],[1,1]);
 console.log('verified archive page metadata: PASS');
}finally{for(const conn of R._conns.values())if(conn.destroy)conn.destroy();for(const timer of timers)clearTimeout(timer);}
