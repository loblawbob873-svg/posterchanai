const assert=require('node:assert/strict'),fs=require('fs'),vm=require('vm');
const source=fs.readFileSync(process.argv[2],'utf8');
const start=source.indexOf('  const Nip46 = {'),end=source.indexOf('\n  };',start)+5;
function fixture(){
 const sockets=[],timers=new Set();let failConstructor=false;
 class Socket{
  constructor(url){if(failConstructor)throw Error('offline');this.url=url;this.readyState=0;sockets.push(this)}
  send(){} close(){this.readyState=3} open(){this.readyState=1;this.onopen?.()}
 }
 const ctx={WebSocket:Socket,console,Date,Math,Set,Map,Promise,NIP46_SINCE_SKEW:900,_ncRelays:()=>[],Relay:{worker:{call:async(op,args)=>({pubkey:'app-'+args.sk})}},
  setTimeout(fn,ms){const t=setTimeout(fn,ms===20000?50:ms);timers.add(t);return t;},
  clearTimeout(t){clearTimeout(t);timers.delete(t)}};
 vm.createContext(ctx);vm.runInContext(source.slice(start,end).replace('const Nip46 =','globalThis.Nip46 ='),ctx);
 const n=ctx.Nip46;n.appPk='fixture';
 return {n,ctx,sockets,fail(v){failConstructor=v},close(){n.reset();for(const t of timers)clearTimeout(t)}};
}
(async()=>{
 let f=fixture();
 try{
  const initial=f.n._openAll(['wss://one']);f.n.revive();
  assert.equal(f.sockets.length,1,'initial open + revive must share one pending socket');
  f.sockets[0].open();await initial;await Promise.resolve();
  assert.equal(f.n._live().length,1);assert.equal(f.n._urls.length,1);
  await f.n._openRelay('wss://one');assert.equal(f.sockets.length,1,'healthy open is reused');
 }finally{f.close()}
 f=fixture();try{
  const old=f.n._openAll(['wss://old']);const rejected=assert.rejects(old);
  const lateOpen=f.sockets[0].onopen;f.n.reset();
  const fresh=f.n._openAll(['wss://new']);lateOpen();f.sockets[1].open();
  await rejected;await fresh;
  assert.equal(f.sockets[0].readyState,3,'late old-account callback cannot adopt');
  assert.equal(f.n._live().length,1);assert.equal(f.n._live()[0].url,'wss://new');
  assert.deepEqual(Array.from(f.n._urls),['wss://new']);
  assert.equal(Object.keys(f.n._rtimer).length,0,'old-account failure cannot schedule retries');
 }finally{f.close()}
 f=fixture();try{
  const p=f.n._openAll(['wss://one','wss://two','wss://one']);
  assert.equal(f.sockets.length,2);f.sockets[0].open();await p;
  f.sockets[1].open();await Promise.resolve();
  assert.equal(f.n._live().length,2);assert.equal(f.n._urls.length,2);
 }finally{f.close()}
 f=fixture();try{
  f.fail(true);await assert.rejects(f.n._openRelay('wss://one'));
  assert.equal(Object.keys(f.n._opening).length,0,'constructor failure cannot poison attempt cache');
  f.fail(false);const p=f.n._openRelay('wss://one');f.sockets[0].open();await p;
 }finally{f.close()}
 for(const failure of ['onclose','onerror']){f=fixture();try{
  const p=f.n._openRelay('wss://one');const lateOpen=f.sockets[0].onopen;
  f.sockets[0][failure]();await assert.rejects(p);lateOpen();assert.equal(f.n._live().length,0);
  const retry=f.n._openRelay('wss://one');f.sockets[1].open();await retry;
 }finally{f.close()}}
 f=fixture();try{
  await assert.rejects(f.n._openRelay('wss://one'),/timed out/);
  assert.equal(f.sockets[0].readyState,3);assert.equal(Object.keys(f.n._opening).length,0);
 }finally{f.close()}
 f=fixture();try{
  const p=f.n._openAll(['wss://one']),ensure=f.n._ensure(500);
  assert.equal(f.sockets.length,1,'ensure shares initial attempt');f.sockets[0].open();
  await p;assert.equal(await ensure,true);
 }finally{f.close()}

 for(const savedIdentity of [true,false]){f=fixture();try{
  const old=f.n.resume({sk:'old',relay:'wss://old',remotePk:'old-remote',userPk:savedIdentity?'old-user':null});
  const outcome=old.catch(e=>e);
  await new Promise(setImmediate);
  if(savedIdentity)assert.equal(await old,'old-user');
  assert.equal(f.sockets.length,1);
  f.n.reset();
  const fresh=f.n.resume({sk:'new',relay:'wss://new',remotePk:'new-remote',userPk:'new-user'});
  assert.equal(await fresh,'new-user');await new Promise(setImmediate);
  const result=await outcome;if(!savedIdentity)assert.equal(result.cancelledSession,true);
  assert.deepEqual(f.sockets.map(w=>w.url),['wss://old','wss://new'],'cancelled resume cannot redial old relay');
  f.sockets[1].open();await Promise.resolve();
  assert.equal(f.n.remotePk,'new-remote');assert.equal(f.n.userPk,'new-user');
  assert.equal(f.n.appPk,'app-new');assert.equal(f.n.appSk,'new');
 }finally{f.close()}}
 f=fixture();try{
  let releaseOld;
  f.ctx.Relay.worker.call=async(op,args)=>args.sk==='old'?new Promise(r=>releaseOld=r):{pubkey:'app-new'};
  const old=f.n.resume({sk:'old',relay:'wss://old',remotePk:'old-remote',userPk:'old-user'});
  const cancelled=assert.rejects(old,e=>e.cancelledSession===true);
  f.n.reset();
  await f.n.resume({sk:'new',relay:'wss://new',remotePk:'new-remote',userPk:'new-user'});
  releaseOld({pubkey:'app-old'});await cancelled;
  assert.equal(f.n.appPk,'app-new');assert.equal(f.n.appSk,'new');assert.equal(f.n.remotePk,'new-remote');
  assert.deepEqual(f.sockets.map(w=>w.url),['wss://new']);
 }finally{f.close()}
 f=fixture();try{
  let releaseKey;const calls=[];
  f.ctx.Relay.worker.call=(op,args)=>{calls.push(op);return new Promise(r=>releaseKey=r)};
  const old=f.n._ensureAppKey(),cancelled=assert.rejects(old,e=>e.cancelledSession===true);
  f.n.reset();releaseKey({sk:'old'});await cancelled;
  assert.deepEqual(calls,['genKey'],'cancelled generated key cannot be installed');
 }finally{f.close()}
 console.log('PASS initial/revive, reset/late-open, multi-relay, constructor/close/timeout retries and ensure');
})().catch(e=>{console.error(e);process.exit(1)});
