import fs from 'node:fs';
import vm from 'node:vm';
import {createRequire} from 'node:module';
const require=createRequire(import.meta.url);
const PCPaymentTargets=require('../../static/js/client/payment-targets.js');
globalThis.window=globalThis;
vm.runInThisContext(fs.readFileSync(new URL('../../static/vendor/nostr/nostr.bundle.js',import.meta.url),'utf8')+'\nglobalThis.NostrTools=NostrTools;');
const sk=Uint8Array.from({length:32},(_,i)=>i===31?1:0);
const owner=NostrTools.getPublicKey(sk);
const signed=(tags,created_at)=>NostrTools.finalizeEvent({kind:10133,content:'',tags,created_at},sk);

const app=fs.readFileSync(new URL('../../static/js/client/app.js',import.meta.url),'utf8');
const start=app.indexOf('  const _paymentTargetCache=');
const end=app.indexOf('  // base58',start);
if(start<0||end<0)throw new Error('payment-target helpers not found');
const helper=app.slice(start,end);

async function scenario(primary,discovery,profile={}){
  const calls=[];let held=[];
  const context={
    Map, String, Array,PCPaymentTargets,NostrTools,
    Store:{query:filters=>filters[0].kinds[0]===10133?held:[],saveEvent:ev=>{held=[ev];}},
    Relay:{
      query:async filters=>{calls.push(['primary',filters]);return primary;},
      queryFrom:async(relays,filters,opts)=>{calls.push(['discovery',relays,filters,opts]);return discovery;},
    },
    DISCOVERY_RELAYS:['wss://discovery.example'],
  };
  const api=vm.runInNewContext(`(function(){${helper};return {_lightningTarget,_loadPaymentTargets,_lightningAddress};})()`,context);
  const first=await api._lightningAddress(owner,profile);
  const second=await api._lightningAddress(owner,profile);
  return {first,second,calls,api};
}

const old=signed([['payto','lightning','old@example.com']],10);
const newest=signed([['payto','bitcoin','bc1qnope'],['payto','lightning','new@example.com']],20);
let got=await scenario([old,newest],[],{lud16:'legacy@example.com'});
if(got.first!=='new@example.com')throw new Error('newest NIP-A3 Lightning target did not win');
if(got.calls.length!==2)throw new Error('payment target was not cached between zap actions');

got=await scenario([], [newest], {});
if(got.first!=='new@example.com'||got.calls.map(x=>x[0]).join(',')!=='primary,discovery')
  throw new Error('discovery-relay fallback did not find the payment target');

got=await scenario([{...newest,pubkey:'b'.repeat(64)}],[],{lud06:'lnurl1legacy'});
if(got.first!=='lnurl1legacy')throw new Error('foreign event bypassed the legacy profile fallback');

got=await scenario([signed([['payto','bitcoin','bc1qonly']],30)],[],{lud16:'legacy@example.com'});
if(got.first!=='legacy@example.com')throw new Error('non-Lightning targets suppressed lud16 fallback');

if(got.api._lightningTarget({kind:10133,tags:[['payto','LIGHTNING','  case@example.com  ']]})!=='case@example.com')
  throw new Error('payment type normalization or address trimming regressed');
