import fs from 'node:fs';
const app=fs.readFileSync(new URL('../../static/js/client/app.js',import.meta.url),'utf8');
const start=app.indexOf('  function _guardSignerNip44(s){');
const end=app.indexOf('  // build + sign an event',start);
const shipped=app.slice(start,end);
globalThis.encCalls=0;globalThis.decCalls=0;
globalThis.window={nostr:{
  signEvent:async x=>x,nip04:{encrypt:async()=>'',decrypt:async()=>''},
  nip44:{encrypt:async(_p,t)=>{encCalls++;return 'ct:'+t},decrypt:async(_p,c)=>{decCalls++;return 'pt:'+c}},
}};
globalThis._extGate={call:(_key,fn)=>fn()};
globalThis._nip17wrapVia=()=>{};globalThis._nip17unwrapVia=()=>{};
globalThis.Nip55={};globalThis.Nip46={};globalThis.Relay={worker:{call(){throw Error('raw local signer reached')}}};
const run=new Function(`return (async()=>{${shipped}
  const direct=makeSigner('nip07','a'.repeat(64));
  const submodule={nip44enc:direct.nip44enc,nip44dec:direct.nip44dec};
  const errors=[];
  for(const job of [direct.nip44enc('b',''),direct.nip44dec('b',''),submodule.nip44enc('b','x'.repeat(65536))])
    try{await job}catch(e){errors.push({message:e.message,meta:e.nip44})}
  direct.nip44enc('b',''); submodule.nip44dec('b',''); // deliberately unawaited
  const allModes=[];
  for(const mode of ['nip55','nip46','local']){
    const s=makeSigner(mode,'a'.repeat(64));
    try{await s.nip44enc('b','')}catch(e){allModes.push(e.nip44&&e.nip44.mode)}
  }
  await new Promise(r=>setTimeout(r,20));
  const valid=await submodule.nip44enc('b','ok');
  return {encCalls,decCalls,errors,valid,allModes};
})()`);
process.stdout.write(JSON.stringify(await run()));
