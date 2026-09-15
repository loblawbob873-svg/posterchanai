import assert from 'node:assert/strict';
import {boot, OK} from './monero_paint_runtime.mjs';
const tick=()=>new Promise(r=>setImmediate(r));
const deferred=()=>{let resolve,reject;const promise=new Promise((a,b)=>{resolve=a;reject=b;});return {promise,resolve,reject};};

async function scenario(kind){
  const w=boot({fetcher:()=>OK({enabled:false})});
  await tick();
  let owner='a'.repeat(64),balance='1',gate=null,arrived=null;
  const stage=kind.includes('status')?'status':kind.includes('history')?'history':'balance';
  const calls=[];
  const probe=kind.startsWith('node_')?w.api.probe:w.api.meProbe;
  w.PC.viewer=()=>({pubkey:owner});
  w.PC.authFetch=async path=>{
    calls.push(path);
    if(kind.startsWith('render_')&&!path.includes('/me/'))return {...OK({detail:'operator only'}),ok:false,status:403};
    if(path.endsWith('/status')){
      if(gate&&stage==='status'){const wait=gate;gate=null;arrived.resolve();await wait.promise;}
      return OK({enabled:true,network:'mainnet'});
    }
    if(path.endsWith('/address'))return OK({address:owner});
    if(path.endsWith('/balance')){
      const body={address:owner,balance,unlocked_balance:balance};
      if(gate&&stage==='balance'){const wait=gate;gate=null;arrived.resolve();await wait.promise;}
      return OK(body);
    }
    if(gate&&stage==='history'){const wait=gate;gate=null;arrived.resolve();await wait.promise;}
    return OK({in:[],out:[]});
  };
  if(kind==='tip_account_pending'){
    w.advance(60000);
    let modals=0;w.PC.modal=()=>{modals++;};
    gate=deferred();const held=gate;arrived=deferred();
    const tip=w.api.meTip({address:'4'+'A'.repeat(94),amount:'0.1'});
    await arrived.promise;owner='b'.repeat(64);held.resolve();
    assert.equal(await tip,false);assert.equal(modals,0,'old-account probe opened a payment dialog');
  }else if(kind==='logout_cache'){
    await probe(true);const before=calls.length;owner='';
    assert.equal(await probe(false),null);
    assert.equal(calls.length,before,'signed-out probe made authenticated requests');
    owner='a'.repeat(64);balance='2';
    assert.equal((await probe(false)).balance,'2','login reused data from the previous session');
  }else if(kind.startsWith('render_')){
    w.advance(60000);await w.api.probe(true);
    gate=deferred();const held=gate;arrived=deferred();
    const old=w.api.render();await arrived.promise;
    if(kind==='render_account_pending')owner='b'.repeat(64);
    balance='2';await w.api.render();
    assert.match(w.feed.innerHTML,/2 <small>XMR/);
    if(kind==='render_failure')held.reject(Error('old request failed'));else held.resolve();
    await old;
    assert.match(w.feed.innerHTML,/2 <small>XMR/,'old render replaced the newer wallet screen');
    assert.doesNotMatch(w.feed.innerHTML,/Local wallet unavailable/);
  }else if(kind.endsWith('account_cache')){
    assert.equal((await probe(true)).balance,'1');
    owner='b'.repeat(64);balance='2';
    const next=await probe(false);
    assert.equal(next.balance,'2','account switch reused the previous wallet balance');
    assert.equal(next.address,owner);
  }else{
    gate=deferred();const held=gate;arrived=deferred();
    const old=probe(true);await arrived.promise;
    if(kind.endsWith('account_pending'))owner='b'.repeat(64);
    balance='2';
    assert.equal((await probe(true)).balance,'2');
    const before=calls.length;
    if(kind.includes('failure'))held.reject(Error('old balance request failed'));
    else held.resolve();
    await old;
    assert.equal((await probe(false)).balance,'2','late result replaced the newer balance');
    assert.equal(calls.length,before,'superseded request continued or cleared the newer cache');
  }
}
for(const kind of [process.argv[2]]){
  await scenario(kind);console.log(kind+' passed');
}
