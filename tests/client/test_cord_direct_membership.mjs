import fs from 'node:fs';
import vm from 'node:vm';
import assert from 'node:assert/strict';
import {makeRealm,loadInto,into} from '../../botframework/cord_realm.mjs';
const root=new URL('../../',import.meta.url),source=fs.readFileSync(new URL('static/js/client/concord.js',root),'utf8');
const ctx=makeRealm(),copy=into(ctx);loadInto(ctx,new URL('static/vendor/nostr/nostr.bundle.js',root));
const NT=ctx.NostrTools,sk=NT.generateSecretKey(),owner=NT.getPublicKey(sk),key=NT.nip44.v2.utils.getConversationKey(sk,owner);
const hex=n=>n.toString(16).padStart(64,'0'),b64=h=>Buffer.from(h,'hex').toString('base64url');
let published=[];
ctx.membershipEvents=async()=>copy([]);ctx.CORD_RELAYS=copy(['wss://test.example']);
ctx.roomIdentity=room=>room.communityId;ctx.forgetLeftCommunity=()=>{};ctx.rememberLeftCommunity=()=>{};
ctx.mergeRoom=(a,b)=>({...a,...b});
const api={viewer:()=>({pubkey:owner}),nip44enc:async(_p,text)=>NT.nip44.v2.encrypt(text,key),nip44dec:async(_p,text)=>NT.nip44.v2.decrypt(text,key),
 signTemplate:async template=>NT.finalizeEvent(copy(template),sk),relayPublishTo:async(_urls,event)=>{assert(NT.verifyEvent(copy(event)));published.push(event);return true;}};
vm.runInContext(source.slice(source.indexOf('  function cordListHex('),source.indexOf('  function cordListMaterial('))+
 source.slice(source.indexOf('  // CORD-02 §8: canonical'),source.indexOf('  // Only encrypted, signed envelopes cross'))+
 source.slice(source.indexOf('  function mergeDirectInviteRoom('),source.indexOf('  async function showDirectInvitations(')),ctx);
const bundle={community_id:hex(1),owner,owner_salt:hex(2),community_root:hex(3),root_epoch:0,channels:[{id:hex(4),key:hex(5),epoch:0}],relays:['wss://test.example'],name:'existing'};
const room=copy({communityId:hex(1),name:'existing',cord:{bundle,hydrated:true}});
// A link is absent by design; membership still gets a signed, encrypted33302 fragment.
await ctx.persistArmadaMembership(api,room);assert.equal(published.length,1);
let doc=JSON.parse(NT.nip44.v2.decrypt(published[0].content,key));
assert.equal(doc.entries[0].community_id,b64(hex(1)));assert(!doc.entries[0].invite_ref);
const incoming=copy({communityId:hex(1),cord:{bundle:{...bundle,channels:[{id:hex(6),key:hex(7),epoch:0}]}}});
const merged=ctx.mergeDirectInviteRoom(room,incoming);
assert.equal(merged.cord.bundle.channels.length,2);assert.equal(merged.cord.bundle.channels[0].key,hex(5));
await ctx.persistArmadaMembership(api,merged);doc=JSON.parse(NT.nip44.v2.decrypt(published.at(-1).content,key));
assert.equal(doc.entries[0].current.channels.length,2,'same-community grant lost during membership persistence');
const upgraded=ctx.mergeDirectInviteRoom(room,copy({...incoming,cord:{bundle:{...bundle,channels:[{id:hex(4),key:hex(8),epoch:1}]}}}));
assert.equal(upgraded.cord.bundle.channels[0].key,hex(8));assert.equal(upgraded.cord.bundle.channels[0].held_keys[0].key,hex(5));
for(const changed of [{community_root:hex(9)},{root_epoch:1},{control_pk:hex(10)},{channels:[{id:hex(4),key:hex(8),epoch:0}]}])
 assert.throws(()=>ctx.mergeDirectInviteRoom(room,copy({...incoming,cord:{bundle:{...bundle,...changed}}})));
assert.throws(()=>ctx.mergeDirectInviteRoom(upgraded,room),/older channel key/);
assert.equal(room.cord.bundle.channels[0].key,hex(5),'merge mutated prior membership');
console.log('direct membership passed');
