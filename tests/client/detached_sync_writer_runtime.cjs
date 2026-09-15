const assert=require('node:assert/strict');
const {boot}=require('./sync_owner_fixture.cjs');
const scenario=process.argv[2];
const buses=new Map();
class BroadcastChannel {
  constructor(name){this.name=name;this.listeners=[];if(!buses.has(name))buses.set(name,new Set());buses.get(name).add(this);}
  postMessage(message){
    for(const peer of buses.get(this.name)||[]){
      if(peer===this)continue;
      const data=JSON.parse(JSON.stringify(message));
      queueMicrotask(()=>{if(!buses.get(this.name)?.has(peer))return;
        peer.onmessage?.({data});for(const fn of peer.listeners)fn({data});});
    }
  }
  addEventListener(name,fn){if(name==='message')this.listeners.push(fn);}
  removeEventListener(name,fn){if(name==='message')this.listeners=this.listeners.filter(f=>f!==fn);}
  close(){buses.get(this.name)?.delete(this);}
}
const rows=new Map(),storage={getItem:k=>rows.get(k)||null,setItem:(k,v)=>rows.set(k,String(v)),removeItem:k=>rows.delete(k)};
const databases=new Map();
const indexedDB={open(name){
  const request={};setImmediate(()=>{
    if(!databases.has(name))databases.set(name,new Map());
    const stores=databases.get(name);
    request.result={objectStoreNames:{contains:key=>stores.has(key)},
      createObjectStore:key=>stores.set(key,new Map()),
      transaction(key){
        key=Array.isArray(key)?key[0]:key;if(!stores.has(key))stores.set(key,new Map());
        const data=stores.get(key),tx={};
        const operation=fn=>{const req={};setImmediate(()=>{req.result=fn();req.onsuccess?.({target:req});
          setImmediate(()=>tx.oncomplete?.({target:tx}));});return req;};
        tx.objectStore=()=>({get:k=>operation(()=>data.get(k)),put:(v,k)=>operation(()=>data.set(k,v)),
          delete:k=>operation(()=>data.delete(k)),getAll:()=>operation(()=>[...data.values()]),
          clear:()=>operation(()=>data.clear())});return tx;
      }};
    request.onupgradeneeded?.({target:request});request.onsuccess?.({target:request});
  });return request;
}};
let primaryPrompts=0,popupPrompts=0,destructiveWrites=0;
const deferred=()=>{let resolve;const promise=new Promise(yes=>resolve=yes);return {promise,resolve};};
const started=deferred(),release=deferred(),prompted=deferred(),answer=deferred();
const drain=()=>new Promise(resolve=>setImmediate(resolve));
let active=0,peak=0,executions=0,lastStopped=false;
const report=stopped=>({ok:true,scanned:60,uploaded:[],downloaded:[],failed:[],trashed:[],conflicted:[],skipped:[],stopped});
const options={hidden:false,BroadcastChannel,storage,indexedDB,source:process.env.PC_SYNC_TEST_SOURCE,
  location:{protocol:'app:',href:'app://posterchan/index.html?pcwin=sync',search:'?pcwin=sync'}};
const primary=boot({...options,shell:{backgroundOwner:true},uiConfirm:async()=>{primaryPrompts++;return true;},
  ...(scenario==='verify'?{executor:{verify:async(fs,docs,opts)=>{
    await fs.scan(opts.id);assert.equal(opts.key,'Pictures','owner used popup-supplied pair mapping');
    return {checked:1,corrupt:[],missingHere:[],extra:[],unverified:[],missingBytes:[],unaddressed:[]};
  }}}:{}),
  ...(['cancel','changed_account','stop','concurrent','background','completed_then_automatic'].includes(scenario)?{executor:{sweep:async(fs,docs,opts)=>{
    await fs.scan(opts.id);
    assert.equal(opts.key,'Pictures','owner used popup-supplied pair mapping');
    if(['stop','concurrent','background','completed_then_automatic'].includes(scenario)){
      opts.onProgress({phase:'hashing',i:2,n:5,path:'photo.jpg'});
      active++;peak=Math.max(peak,active);executions++;started.resolve();
      if(executions===1)await release.promise;
      lastStopped=opts.shouldStop();active--;return report(lastStopped);
    }
    assert.equal(typeof opts.confirm,'function','owner lost the manual confirmation callback');
    const accepted=await opts.confirm({kind:'massTrash',n:60,keep:1});
    if(accepted)destructiveWrites++;
    return report(!accepted);
  }}}:{})});
const makePopup=()=>boot({...options,shell:{backgroundOwner:false},
  pubkey:scenario==='wrong_account'?'b'.repeat(64):undefined,
  uiConfirm:async(message)=>{popupPrompts++;assert.match(String(message),/60/);prompted.resolve();
    return scenario==='changed_account'?answer.promise:false;}});
let popup=scenario==='background'?null:makePopup();
(async()=>{
  if(scenario==='background'){
    const pending=primary.ctx.PCSync.sweep(primary.ctx.PCSync.folders()[0],{});await started.promise;
    popup=makePopup();await drain();
    const state=popup.ctx.PCSync.status.get(popup.ctx.PCSync.folders()[0].id);
    assert.equal(state?.busy,true,'new popup did not see the already-running primary job');
    assert.match(state.text,/hashing/,'new popup lost current primary progress');
    popup.fire('pagehide');await drain();release.resolve();await pending;
    assert(!lastStopped,'closing an observer cancelled an unrelated primary job');
    assert.equal(primary.seen.scans,1);assert.equal(popup.seen.scans,0);assert.equal(popup.seen.writes,0);
    console.log('PASS background');process.exit(0);
  }
  assert.equal(popup.ctx.opener,null);
  const original=popup.ctx.PCSync.folders()[0];
  const folder={...original,key:'untrusted-popup-key',dir:'/untrusted-popup-directory'};
  if(scenario==='unknown_folder')folder.id='not-paired-on-owner';
  if(['unknown_folder','wrong_account'].includes(scenario)){
    await assert.rejects(popup.ctx.PCSync.sweep(folder,{manual:true}),error=>{
      assert.doesNotMatch(error.message,/filesystem access/,'request never reached primary validation');return true;
    });
    assert.equal(primary.seen.scans,0);
  }else if(scenario==='automatic'){
    const result=await popup.ctx.PCSync.sweep(folder,{});
    assert.equal(result.skipped,true);assert.equal(primary.seen.scans,0);
    const timerCount=popup.timers.length;popup.ctx.PCSync.startAll();
    assert.equal(popup.timers.length,timerCount,'secondary started background sync timers');
  }else if(scenario==='verify'){
    await popup.ctx.PCSync.verifyFolder(folder);
    assert.equal(primary.seen.scans,1,'Verify never reached the primary filesystem');
    assert.match(popup.ctx.PCSync.status.get(original.id)?.text,/verified/);
  }else if(scenario==='cleanup'){
    const result=await popup.ctx.PCSync.conflictCleanup(folder,{dryRun:true});
    assert.deepEqual(result.list,[]);assert.equal(result.bytes,0);
    assert(primary.seen.requests.length>0,'cleanup never inspected the primary manifest');
    assert.equal(popup.seen.requests.length,0,'popup performed cleanup itself');
    assert.equal(primary.seen.writes,0,'cleanup preview changed local files');
  }else if(scenario==='completed_then_automatic'){
    release.resolve();await popup.ctx.PCSync.sweep(folder,{manual:true});
    const next={...primary.ctx.PCSync.folders()[0],lastSyncAt:0,lastFullScanAt:0,_dirty:true};
    await primary.ctx.PCSync.sweep(next,{});
    assert.equal(executions,2,'primary automatic follow-up did not execute');
    assert(!lastStopped,'completed popup request left a stale stop flag on the primary');
  }else if(scenario==='stop'){
    const pending=popup.ctx.PCSync.sweep(folder,{manual:true}).catch(error=>({error:error.message}));await started.promise;
    await popup.ctx.PCSync.stop(original.id);await drain();release.resolve();
    await pending;assert.equal(lastStopped,true,'popup Stop did not reach the primary sweep');
    assert.equal(executions,1);
  }else if(scenario==='concurrent'){
    const first=popup.ctx.PCSync.sweep(folder,{manual:true});await started.promise;
    const second=popup.ctx.PCSync.sweep(folder,{manual:true});await drain();
    assert.equal(active,1,'two manual popup requests ran simultaneous writers');
    release.resolve();await Promise.all([first,second]);
    assert.equal(peak,1);assert.equal(active,0);assert(executions>=1);
  }else if(scenario==='changed_account'){
    const pending=popup.ctx.PCSync.sweep(folder,{manual:true}).catch(error=>({error:error.message}));
    await prompted.promise;popup.ctx.__PC.me=()=>({pubkey:'c'.repeat(64)});answer.resolve(true);
    await pending;assert.equal(destructiveWrites,0,'a late Yes from a changed account authorized deletion');
    assert.equal(primaryPrompts,0);assert.equal(popupPrompts,1);
  }else{
    const result=await popup.ctx.PCSync.sweep(folder,{manual:true});
    assert.equal(primary.seen.scans,1,'manual sync never reached the primary filesystem');
    assert(result&&!result.error&&result.skipped!==true,JSON.stringify(result));
    assert(!JSON.stringify(primary.seen.requests).includes('untrusted-popup-key'));
    assert(popup.ctx.PCSync.status.get(original.id)?.text,'popup did not receive owner progress/result status');
    if(scenario==='cancel'){
      assert.equal(primaryPrompts,0,'confirmation appeared in the hidden owner');
      assert.equal(popupPrompts,1);assert.equal(destructiveWrites,0,'popup cancellation was ignored');
    }
  }
  assert.equal(popup.seen.scans,0,'detached popup became a second filesystem writer');
  assert.equal(popup.seen.writes,0,'detached popup changed files locally');
  console.log('PASS '+scenario);process.exit(0);
})().catch(error=>{console.error(error);process.exit(1);});
