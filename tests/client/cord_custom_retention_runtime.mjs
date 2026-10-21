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
// CONCORD KEEP-HISTORY (posterchan/history_retention) against the CORD spec:
//  * CORD-02 §6 -- top-level metadata is RESERVED for the protocol: the setting lives in `custom`, under a
//    prefixed key, and an editor MUST round-trip every key it does not understand;
//  * CORD-08 -- retention is a local-cache policy: it never puts an `expiration` tag on a message.
const signEvent=async e=>NT.finalizeEvent(e,ownerKey);
const channelWrap=edition('2',channel,{name:'general',private:false});
const theirs={rules:'Be excellent to each other.','vector/theme':'dark'};
const base=[edition('0',cid,{name:'Keep',relays:bundle.relays,message_expiration:0,custom:theirs}),channelWrap];
assert.deepEqual(R.inspectControl(bundle,base).custom,theirs,'inspectControl exposes the folded custom object');
const KEY='posterchan/history_retention';
// a PosterChan save merges ITS key and leaves the others as they were
const set=await R.createMetadataWrap(bundle,base,{name:'Keep',customPatch:{[KEY]:30}},owner,signEvent);
const withKeep=[...base,set.wrap],folded=R.inspectControl(bundle,withKeep);
assert.deepEqual(folded.custom,{...theirs,[KEY]:30},'another client\'s custom keys were not round-tripped');
const body=JSON.parse(JSON.parse(NT.nip44.v2.decrypt(set.wrap.content,control.conv)).content);
assert.equal(body.history_retention,undefined,'a top-level field outside custom is reserved for the protocol (CORD-02 §6)');
assert.equal(Object.keys(body).filter(k=>/retention/i.test(k)).length,0,Object.keys(body));
// Forever = the key removed; the others stay
const forever=await R.createMetadataWrap(bundle,withKeep,{name:'Keep',customPatch:{[KEY]:null}},owner,signEvent);
assert.deepEqual(R.inspectControl(bundle,[...withKeep,forever.wrap]).custom,theirs,'Forever must remove only our key');
// an unrelated edit (a rename) carries custom through untouched
const renamed=await R.createMetadataWrap(bundle,withKeep,{name:'Renamed'},owner,signEvent);
assert.deepEqual(R.inspectControl(bundle,[...withKeep,renamed.wrap]).custom,{...theirs,[KEY]:30},'a rename dropped custom');
// retention never becomes an expiration tag on a message (CORD-08 is the only expiry)
const msg=await R.createChatWrap(bundle,withKeep,channel,'hello',owner,signEvent);
assert.deepEqual(msg.wrap.tags.filter(t=>t[0]==='expiration'),[],'retention must not tag the wrap for relay deletion');
assert.deepEqual((msg.tags||[]).filter(t=>t[0]==='expiration'),[],'retention must not tag the rumor');
Date.now=realNow;
console.log('cord custom retention ok');
