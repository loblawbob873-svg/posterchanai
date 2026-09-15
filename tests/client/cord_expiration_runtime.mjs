// CORD-08 independent wire producer: node HKDF/SHA256 + Nostr primitives only.
// No production CORD wrap/binding/hash builder creates the ingestion fixtures.
import fs from 'node:fs';import vm from 'node:vm';import assert from 'node:assert/strict';
import {hkdfSync,createHash} from 'node:crypto';
globalThis.window=globalThis;
vm.runInThisContext(fs.readFileSync(new URL('../../static/vendor/nostr/nostr.bundle.js',import.meta.url),'utf8')+'\nglobalThis.NT=NostrTools;');
vm.runInThisContext(fs.readFileSync(new URL('../../static/js/client/cord-reader.js',import.meta.url),'utf8')+'\nglobalThis.R=PosterCordReader;');
const ownerKey=new Uint8Array(32).fill(3),memberKey=new Uint8Array(32).fill(4),owner=NT.getPublicKey(ownerKey),member=NT.getPublicKey(memberKey);
const salt=Buffer.alloc(32,5),root=Buffer.alloc(32,6),channel='07'.repeat(32),privateId='08'.repeat(32),privateKey=Buffer.alloc(32,9);
const cid=createHash('sha256').update(Buffer.concat([Buffer.from('concord/community'),Buffer.from(owner,'hex'),salt])).digest('hex');
const bundle={community_id:cid,owner,owner_salt:salt.toString('hex'),community_root:root.toString('hex'),root_epoch:0,channels:[{id:privateId,key:privateKey.toString('hex'),epoch:0,name:'private'}],relays:['wss://example.invalid']};
function info(label,id,epoch){const parts=[Buffer.from(label),Buffer.from([0]),Buffer.from(id,'hex')];if(epoch!==undefined){const n=Buffer.alloc(8);n.writeBigUInt64BE(BigInt(epoch));parts.push(n);}return Buffer.concat(parts);}
function group(label,secret,id){const sk=new Uint8Array(hkdfSync('sha256',secret,Buffer.alloc(0),info(label,id,0),32)),pk=NT.getPublicKey(sk);return{sk,pk,conv:NT.nip44.v2.utils.getConversationKey(sk,pk)};}
const control=group('concord/control',root,cid),chat=group('concord/channel',root,channel),priv=group('concord/channel',privateKey,privateId);
let clock=1700000000;const realNow=Date.now;Date.now=()=>clock*1000;
function wire({group=chat,kind=9,tags=[],content='independent message',at=clock,key=ownerKey,outerTags=[],plain=false,ephemeral=false}={}){
 const rumor={kind,pubkey:NT.getPublicKey(key),created_at:at,tags,content};rumor.id=NT.getEventHash(rumor);
 const seal=NT.finalizeEvent({kind:plain?20014:20013,created_at:at,tags:[],content:plain?JSON.stringify(rumor):NT.nip44.v2.encrypt(JSON.stringify(rumor),group.conv)},key);
 return NT.finalizeEvent({kind:ephemeral?21059:1059,created_at:at,tags:[['p',NT.getPublicKey(new Uint8Array(32).fill(11))],...outerTags],content:NT.nip44.v2.encrypt(JSON.stringify(seal),group.conv)},group.sk);
}
const binding=[['channel',channel],['epoch','0']],expired=[['expiration',String(clock-1)]],future=[['expiration',String(clock+20)]];
function edition(vsk,id,body){return wire({group:control,kind:3308,tags:[['vsk',vsk],['eid',id],['ev','1']],content:JSON.stringify(body),plain:true});}
const channelWrap=edition('2',channel,{name:'general',private:false});
const controls=timer=>[edition('0',cid,{name:'Expiry',relays:bundle.relays,message_expiration:timer}),channelWrap];
const on=controls(86400),off=controls(0),signEvent=async e=>NT.finalizeEvent(e,ownerKey);
function unwrap(w,group=chat){assert(NT.verifyEvent(w));const seal=JSON.parse(NT.nip44.v2.decrypt(w.content,group.conv));assert(NT.verifyEvent(seal));const rumor=JSON.parse(NT.nip44.v2.decrypt(seal.content,group.conv));assert.equal(rumor.id,NT.getEventHash(rumor));assert.equal(rumor.pubkey,seal.pubkey);return rumor;}
const expiry=e=>e.tags.filter(t=>t[0]==='expiration');
for(const kind of [9,1111,7,1068,1018,9735,3310]){
 const made=await R.createChatWrap(bundle,on,channel,'new',owner,signEvent,[['expiration','1']],kind),rumor=unwrap(made.wrap);
 assert.deepEqual(expiry(rumor),[['expiration',String(rumor.created_at+86400)]]);assert.deepEqual(expiry(made.wrap),expiry(rumor));assert.deepEqual(made.tags,rumor.tags);
}
for(const kind of [5,1740,20001]){const made=await R.createChatWrap(bundle,on,channel,'exempt',owner,signEvent,[['expiration','1']],kind);assert.deepEqual(expiry(unwrap(made.wrap)),[]);assert.deepEqual(expiry(made.wrap),[]);}
for(const timer of [undefined,null,0,-1,'86400',true,1.5,{},Number.MAX_SAFE_INTEGER+1]){const policy=controls(timer),made=await R.createChatWrap(bundle,policy,channel,'off',owner,signEvent);assert.equal(R.inspectControl(bundle,policy).message_expiration,0);assert.deepEqual(expiry(unwrap(made.wrap)),[]);}
assert.equal(R.inspectControl(bundle,on).message_expiration,86400);
const privateMade=await R.createChatWrap(bundle,on,privateId,'private',owner,signEvent);assert.equal(expiry(unwrap(privateMade.wrap,priv))[0][1],String(clock+86400));
for(const ephemeral of [false,true]){const made=await R.createWebxdcWrap(bundle,on,channel,'{}',owner,signEvent,[['i','app']],ephemeral);assert.equal(expiry(unwrap(made.wrap)).length,ephemeral?0:1);assert.deepEqual(expiry(made.wrap),expiry(unwrap(made.wrap)));}
const read=async(w,policy=on)=>R.inspectChat(bundle,policy,channel,w);
const old=wire({tags:binding,at:clock-1000}),gone=wire({tags:[...binding,...expired]}),soon=wire({tags:[...binding,...future]}),outerOnly=wire({tags:binding,outerTags:expired,content:'outer tag is not authority'});
assert.equal((await read([gone])).messages.length,0,'expired at ingest');
assert((await read([gone])).expirations.some(([id,at])=>id===gone.id&&at===clock-1),'expired authenticated ciphertext deadline exposed for purge');
assert.equal((await read([old,soon,outerOnly])).messages.length,3,'new policy must not expire untagged history or trust outer tag');
assert.equal((await read([soon],off)).messages.length,1,'turning timer off does not alter signed expiry');
clock+=20;
assert.equal((await read([soon],off)).messages.length,0,'memoized rumor expires exactly at signed deadline');
const unsignedExpiry=wire({tags:[...binding,...expired],outerTags:[['expiration',String(clock+90000)]]});assert.equal((await read([unsignedExpiry])).messages.length,0,'doctored later outer expiry cannot override signed rumor');
const target=wire({tags:binding}),targetId=unwrap(target).id,del=wire({kind:5,tags:[...binding,['e',targetId]]});assert.equal((await read([target,del])).messages.length,0,'nonexpiring delete tombstone preserved');
const ownerNotice=wire({kind:1740,tags:[...binding,['timer','86400']],content:''}),memberNotice=wire({kind:1740,key:memberKey,tags:[...binding,['timer','0']],content:''});
assert.equal((await read([ownerNotice,memberNotice])).messages.length,1,'only metadata authority may announce timer changes');
const role='12'.repeat(32),grant=Buffer.from(hkdfSync('sha256',Buffer.from(cid,'hex'),Buffer.alloc(0),info('concord/grant',member),32)).toString('hex');
const staff=[...on,edition('1',role,{role_id:role,name:'Timer staff',permissions:'4',position:50,color:0}),edition('3',grant,{member,role_ids:[role]})];
assert.equal((await read([memberNotice],staff)).messages.length,1,'MANAGE_METADATA staff notice admitted');
const changed=await R.createMetadataWrap(bundle,on,{name:'Expiry',message_expiration:60},owner,signEvent);const later=[...on,changed.wrap];assert.equal(R.inspectControl(bundle,later).message_expiration,60);
const newMessage=await R.createChatWrap(bundle,later,channel,'new timer',owner,signEvent);assert.equal(expiry(unwrap(newMessage.wrap))[0][1],String(clock+60));assert.equal((await read([old],later)).messages.length,1,'policy change is not retroactive');
// Every durable effect expires too; a dead edit cannot preserve its replacement text.
const effectTarget=wire({tags:binding,content:'original text'}),effectId=unwrap(effectTarget).id,deadline=clock+10;
const effectTags=[...binding,['e',effectId],['expiration',String(deadline)]];
const edit=wire({kind:3302,tags:effectTags,content:'temporary edit'}),reaction=wire({kind:7,tags:effectTags,content:'+'}),vote=wire({kind:1018,tags:[...effectTags,['response','a']],content:''});
const beforeEffects=await read([effectTarget,edit,reaction,vote]);
assert.equal(beforeEffects.messages[0].text,'temporary edit');assert.equal(Number(beforeEffects.messages[0].editedExpires),deadline);
assert.equal(beforeEffects.reactions.length,1);assert.equal(Number(beforeEffects.pollVotes[0][1][0].expires),deadline);
clock=deadline;
const afterEffects=await read([effectTarget,edit,reaction,vote]);assert.equal(afterEffects.messages[0].text,'original text');assert.equal(afterEffects.reactions.length,0);assert.equal(afterEffects.pollVotes.length,0);
// Exercise actual adapter storage normalization on cached rows without a network refold.
const uiSource=fs.readFileSync(new URL('../../static/js/client/concord.js',import.meta.url),'utf8');
const normalize=vm.runInNewContext(uiSource.slice(uiSource.indexOf('  function messageExpired('),uiSource.indexOf('  const remoteMessages='))+';uniqueMessages', {Date,messageId:m=>m.id});
const cached={id:effectId,kind:9,text:'temporary edit',originalText:'original text',editedExpires:String(deadline),tags:[['edited',String(clock-1)]],reactions:{'+':[owner]},reactionIds:{'+':{[owner]:'rx'}},reactionExpirations:{rx:deadline},votes:[{pubkey:owner,expires:String(deadline)}],zaps:[{id:'zap',expires:String(deadline)}]};
const clean=normalize([cached])[0];assert.equal(clean.text,'original text');assert.deepEqual(Object.keys(clean.reactions),[]);assert.equal(clean.votes.length,0);assert.equal(clean.zaps.length,0);assert.equal(cached.text,'temporary edit','normalization leaves input immutable');
Date.now=realNow;
console.log('CORD08_OK independent wire, signed/outer expiration, malformed policy, exemptions, private/Webxdc, ingest/memo deadline, notice authority, nonretroactive policy change');
