import assert from 'node:assert/strict';
import vm from 'node:vm';
import {createHash,webcrypto} from 'node:crypto';
import {makeRealm,loadInto,into} from '../../botframework/cord_realm.mjs';
const root=new URL('../../',import.meta.url),ctx=makeRealm(),copy=into(ctx);
ctx.crypto.subtle=webcrypto.subtle;
for(const f of ['static/vendor/nostr/nostr.bundle.js','static/js/client/cord-reader.js','static/js/client/cord-direct-invites.js'])loadInto(ctx,new URL(f,root));
const NT=ctx.NostrTools,api=ctx.PCCordDirectInvites;
const secret=h=>vm.runInContext(`Uint8Array.from(${JSON.stringify([...Buffer.from(h,'hex')])})`,ctx);
const sender=secret('11'.repeat(32)),recipient=secret('22'.repeat(32)),ephemeral=secret('33'.repeat(32));
const senderPk=NT.getPublicKey(sender),recipientPk=NT.getPublicKey(recipient),salt='44'.repeat(32);
const bundle={community_id:createHash('sha256').update(Buffer.concat([Buffer.from('concord/community'),Buffer.from(senderPk,'hex'),Buffer.from(salt,'hex')])).digest('hex'),owner:senderPk,owner_salt:salt,community_root:'55'.repeat(32),root_epoch:1,channels:[],relays:['wss://example.invalid'],name:'Direct fixture',expires_at:Date.now()+60000};
const sign=(e,sk)=>NT.finalizeEvent(copy(e),sk);
const encrypt=(text,sk,pk)=>NT.nip44.encrypt(text,NT.nip44.getConversationKey(sk,pk));
const decrypt=(text,sk,pk)=>NT.nip44.decrypt(text,NT.nip44.getConversationKey(sk,pk));
const hash=e=>createHash('sha256').update(JSON.stringify([0,e.pubkey,e.created_at,e.kind,e.tags,e.content])).digest('hex');
// Independent producer: ordinary NIP-59, never the CORD stream builder or module.create.
function wire({hint=true,kind=3313,body=bundle,badId=false,badSeal=false,mismatch=false}={}){
 const rumor={kind,pubkey:mismatch?recipientPk:senderPk,created_at:Math.floor(Date.now()/1000),tags:[],content:JSON.stringify(body)};
 rumor.id=badId?'0'.repeat(64):hash(rumor);
 const seal=sign({kind:13,created_at:rumor.created_at,tags:[],content:encrypt(JSON.stringify(rumor),sender,recipientPk)},sender);
 if(badSeal)seal.sig='0'.repeat(128);
 return sign({kind:1059,created_at:rumor.created_at,tags:[['p',recipientPk],...(hint?[['k','3313']]:[])],content:encrypt(JSON.stringify(seal),ephemeral,recipientPk)},ephemeral);
}
let active=true;
const context={pubkey:recipientPk,isCurrent:()=>active,verify:async events=>events.filter(e=>NT.verifyEvent(copy(e))),decrypt:async(peer,text)=>decrypt(text,recipient,peer)};
for(const hint of [true,false]){
 const opened=await api.open(wire({hint}),context);
 assert.equal(opened.inviter,senderPk);assert.equal(opened.bundle.community_id,bundle.community_id);
}
for(const options of [{kind:14},{badId:true},{badSeal:true},{mismatch:true},{body:{...bundle,owner_salt:'66'.repeat(32)}},{body:{...bundle,channels:Array(257).fill({})}}])await assert.rejects(()=>api.open(wire(options),context));
const invalid=JSON.parse(JSON.stringify(wire()));invalid.sig='0'.repeat(128);await assert.rejects(()=>api.open(invalid,context));
await assert.rejects(()=>api.open(wire(),{...context,pubkey:senderPk}));
const expired=wire({body:{...bundle,expires_at:Date.now()-1}});assert((await api.open(expired,context)).bundle);await assert.rejects(()=>api.open(expired,context,{forJoin:true}));
await assert.rejects(()=>api.open(wire(),{...context,decrypt:async(peer,text)=>{active=false;return decrypt(text,recipient,peer)}}));active=true;
// Run the shipped worker; invitation metadata must be inside the signed envelope.
ctx.importScripts=()=>{};let reply;
ctx.postMessage=value=>{reply=value;};loadInto(ctx,new URL('static/js/client/signer-worker.js',root));
let id=0;async function wrapSeal(seal,pk,metadata={}){await ctx.onmessage({data:{id:++id,op:'giftwrapSeal',args:copy({seal,recipient:pk,...metadata})}});assert(reply.ok,reply.error);return reply.data.wrap;}
const outbound={pubkey:senderPk,isCurrent:()=>active,verify:context.verify,encrypt:async(pk,text)=>encrypt(text,sender,pk),sign:async template=>sign(template,sender),wrapSeal};
const created=await api.create(copy(bundle),recipientPk,outbound);
assert(NT.verifyEvent(copy(created.wrap)));assert.equal((await api.open(created.wrap,context)).bundle.name,bundle.name);
assert(created.wrap.tags.some(t=>t[0]==='k'&&t[1]==='3313'));
assert(created.wrap.tags.some(t=>t[0]==='expiration'&&t[1]===String(Math.ceil(bundle.expires_at/1000))));
const fullEpoch='18446744073709551615',fullChannel='18446744073709551614';
const incomingMax=wire({body:{...bundle,root_epoch:JSON.rawJSON(fullEpoch),channels:[{id:'aa'.repeat(32),key:'bb'.repeat(32),epoch:JSON.rawJSON(fullChannel)}]}});
const maxOpened=await api.open(incomingMax,context);assert.equal(maxOpened.bundle.root_epoch,fullEpoch);assert.equal(maxOpened.bundle.channels[0].epoch,fullChannel);
const outgoingMax=await api.create(maxOpened.bundle,recipientPk,outbound);
const maxSeal=JSON.parse(decrypt(outgoingMax.wrap.content,recipient,outgoingMax.wrap.pubkey));
const maxRumor=JSON.parse(decrypt(maxSeal.content,recipient,maxSeal.pubkey));
assert(maxRumor.content.includes('"root_epoch":'+fullEpoch));assert(maxRumor.content.includes('"epoch":'+fullChannel));
assert.equal((await api.open(outgoingMax.wrap,context)).bundle.root_epoch,fullEpoch);
const seal=JSON.parse(decrypt(created.wrap.content,recipient,created.wrap.pubkey));
const legacy=await wrapSeal(seal,recipientPk);assert.deepEqual(JSON.parse(JSON.stringify(legacy.tags)),[['p',recipientPk]]);assert(NT.verifyEvent(copy(legacy)));
await assert.rejects(()=>api.create(copy({...bundle,control_root:'77'.repeat(32)}),recipientPk,outbound));
await assert.rejects(()=>api.create(copy(bundle),recipientPk,{...outbound,wrapSeal:async s=>wrapSeal(s,recipientPk)}));
const historical={...bundle,channels:[{id:'77'.repeat(32),key:'88'.repeat(32),epoch:2,held_keys:[{epoch:1,key:'99'.repeat(32)}]}]};
const limited=await api.create(copy(historical),recipientPk,outbound);
const limitedSeal=JSON.parse(decrypt(limited.wrap.content,recipient,limited.wrap.pubkey));
const limitedRumor=JSON.parse(decrypt(limitedSeal.content,recipient,limitedSeal.pubkey));
assert(!limitedRumor.content.includes('held_keys')&&!limitedRumor.content.includes('99'.repeat(32)),'archived channel keys leaked');
const provenance=await api.open(wire({body:{...bundle,root_refounder:'aa'.repeat(32),removed:true}}),context);
assert(!('root_refounder'in provenance.bundle)&&!('removed'in provenance.bundle));
const storage=new Map();ctx.localStorage={getItem:key=>storage.get(key)||null,setItem:(key,value)=>storage.set(key,value)};
const parked=wire();assert(await api.park(parked,context));assert.equal(api.pending(context).length,1);
assert.equal(await api.park(parked,context),false);
assert(![...storage.values()].join('').includes(bundle.community_root),'pending storage exposed plaintext keys');
assert.equal(api.pending({...context,pubkey:senderPk}).length,0);
api.dismiss(parked.id,context);assert.equal(api.pending(context).length,0);assert.equal(await api.park(parked,context),false);
for(let i=0;i<34;i++)await api.park(wire({body:{...bundle,name:'bounded '+i}}),context);
assert.equal(api.pending(context).length,32);
const persisted=JSON.stringify([...storage]);
await assert.rejects(()=>api.park(invalid,context));assert.equal(JSON.stringify([...storage]),persisted);
console.log('direct invitation wire passed');
