import fs from 'node:fs';
import vm from 'node:vm';
import assert from 'node:assert/strict';
import {makeRealm,loadInto,into} from '../../botframework/cord_realm.mjs';
const ctx=makeRealm(),copy=into(ctx),root=new URL('../../',import.meta.url);
loadInto(ctx,new URL('static/vendor/nostr/nostr.bundle.js',root));
const NT=ctx.NostrTools,sk=NT.generateSecretKey(),owner=NT.getPublicKey(sk),key=NT.nip44.v2.utils.getConversationKey(sk,owner);
const make=(index,count=100,signer=sk)=>NT.finalizeEvent(copy({kind:33302,created_at:100,tags:[['d',String(index)]],content:NT.nip44.v2.encrypt(JSON.stringify({frags:count,entries:[],tombstones:[]}),key)}),signer);
let events=Array.from({length:100},(_,i)=>make(i)),calls=[];
ctx.externalAllowed=()=>false;ctx.externalDone=()=>{};ctx.CORD_RELAYS=copy([]);ctx.LEGACY_RECOVERY_RELAYS=copy([]);
const api={relayQuery:async filters=>{const f=filters[0];calls.push(f);return copy(events.filter(e=>f.kinds.includes(e.kind)&&(!f['#d']||f['#d'].includes(e.tags[0][1]))).slice(0,f.limit));},
 nip44dec:async(_who,payload)=>NT.nip44.v2.decrypt(payload,key),verifyRelayEvents:async list=>list.filter(e=>NT.verifyEvent(copy(e)))};
const source=fs.readFileSync(new URL('static/js/client/concord.js',root),'utf8');
vm.runInContext(source.slice(source.indexOf('  const membershipFragmentReads='),source.indexOf('  function cordListHex(')),ctx);
let rows=await ctx.membershipEvents(api,owner);assert.equal(rows.length,80);assert.equal(calls.length,4,'startup spends one bounded missing-coordinate query');
rows=await ctx.membershipEvents(api,owner);assert.equal(rows.length,96);
rows=await ctx.membershipEvents(api,owner);assert.equal(rows.length,100,'recovery must continue beyond the relay first page');
assert(rows.some(e=>e.tags[0][1]==='99'));
// A missing fragment does not stop discovery of later coordinates, and can heal later.
vm.runInContext('membershipFragmentReads.clear()',ctx);events=events.filter(e=>e.tags[0][1]!=='70');calls=[];
rows=await ctx.membershipEvents(api,owner,{fullFragments:true});assert.equal(rows.length,99);assert(rows.some(e=>e.tags[0][1]==='99'));
events.push(make(70));rows=await ctx.membershipEvents(api,owner,{fullFragments:true});assert.equal(rows.length,100);
// Copied self-encrypted content signed by another identity and forged own signatures do not count.
vm.runInContext('membershipFragmentReads.clear()',ctx);const other=NT.generateSecretKey();events=[make(0,2),make(1,2,other),{...make(1,2),sig:'0'.repeat(128)}];
rows=await ctx.membershipEvents(api,owner,{fullFragments:true});assert.equal(rows.length,1);assert.equal(rows[0].pubkey,owner);
// A hostile enormous declared count cannot allocate an array or trigger an unbounded query loop.
vm.runInContext('membershipFragmentReads.clear()',ctx);events=[make(0,Number.MAX_SAFE_INTEGER)];calls=[];
rows=await ctx.membershipEvents(api,owner);assert.equal(rows.length,1);assert.equal(calls.length,4);assert.equal(calls.at(-1)['#d'].length,16);
// Ciphertext remembered for one account cannot appear when another account starts reading.
const otherOwner=NT.getPublicKey(other);rows=await ctx.membershipEvents({...api,nip44dec:async()=>{throw Error('wrong account');}},otherOwner);assert.equal(rows.length,0);
console.log('CORD membership recovery: beyond 64 fragments, bounded batches, holes, late arrival, signatures and account isolation passed');
