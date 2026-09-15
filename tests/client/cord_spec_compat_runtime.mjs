// Independent CORD-01/02 wire producer: Node HKDF (not the reader's derivation), NostrTools.
// Frozen spec: concord-protocol/concord b84554ea5dd47510057a580fa2f8587b4399ad17.
import assert from 'node:assert/strict';
import { hkdfSync, createHash } from 'node:crypto';
import { makeRealm, loadInto, into } from '../../botframework/cord_realm.mjs';
const root = new URL('../../', import.meta.url), ctx = makeRealm(), copy = into(ctx);
for (const file of ['static/vendor/nostr/nostr.bundle.js','static/js/client/cord-reader.js']) loadInto(ctx, new URL(file,root));
const NT=ctx.NostrTools,R=ctx.PosterCordReader;
// The realm’s own Uint8Array is required by the crypto library.
import vm from 'node:vm';
const secret=hex=>vm.runInContext(`Uint8Array.from(${JSON.stringify([...Buffer.from(hex,'hex')])})`,ctx);
const ownerSk=secret('11'.repeat(32)),owner=NT.getPublicKey(ownerSk), salt='22'.repeat(32);
const communityId=createHash('sha256').update(Buffer.concat([Buffer.from('concord/community'),Buffer.from(owner,'hex'),Buffer.from(salt,'hex')])).digest('hex');
const communityRoot='33'.repeat(32),controlRoot='44'.repeat(32),epoch=7;
function derive(label,key,id,epoch) {
  const tail=Buffer.alloc(8);if(epoch!==undefined)tail.writeBigUInt64BE(BigInt(epoch));
  return Buffer.from(hkdfSync('sha256',Buffer.from(key,'hex'),Buffer.alloc(0),Buffer.concat([Buffer.from(label),Buffer.from([0]),Buffer.from(id,'hex'),...(epoch===undefined?[]:[tail])]),32)).toString('hex');
}
function plane(label,key,id,epoch) { const sk=secret(derive(label,key,id,epoch)),pk=NT.getPublicKey(sk);return{sk,pk,conv:NT.nip44.v2.utils.getConversationKey(sk,pk)}; }
const read=plane('concord/control',communityRoot,communityId,epoch),write=plane('concord/control-signer',controlRoot,communityId,epoch);
const member=copy({community_id:communityId,owner,owner_salt:salt,community_root:communityRoot,root_epoch:epoch,control_pk:write.pk,channels:[],relays:['wss://relay.example'],name:'fixture'});
const staff=copy({...member,control_root:controlRoot});
const now=Math.floor(Date.now()/1000),channel='55'.repeat(32);
function sign(template,sk=ownerSk){return NT.finalizeEvent(copy(template),sk);}
function wrapRumor(rumor,sealKind=20014,signer=write,reader=read){
  const doc=copy({...rumor,pubkey:owner,created_at:now});doc.id=NT.getEventHash(doc);
  const seal=sign({kind:sealKind,created_at:now,tags:[],content:sealKind===20014?JSON.stringify(doc):NT.nip44.v2.encrypt(JSON.stringify(doc),reader.conv)});
  return sign({kind:1059,created_at:now,tags:[['p',owner]],content:NT.nip44.v2.encrypt(JSON.stringify(seal),reader.conv)},signer.sk);
}
const metadata={name:'external split room',description:'fixture',relays:['wss://relay.example'],message_expiration:86400,future:{opaque:['keep','EXACT']},icon:{url:'https://example.test/icon.png',type:'image/png'}};
const editions=[wrapRumor({kind:3308,tags:[['vsk','0'],['eid',communityId],['ev','1']],content:JSON.stringify(metadata)}),wrapRumor({kind:3308,tags:[['vsk','2'],['eid',channel],['ev','1']],content:JSON.stringify({name:'general',private:false})})];
const info=R.inspectControl(member,copy(editions));
assert.deepEqual([...info.controlPubkeys],[write.pk],'split address must derive from independent control signer vector');
assert.equal(info.name,metadata.name,'external owner metadata must decrypt using read key');
assert.equal(info.channels.length,1,'external split channel must fold');
assert.equal(R.createPlaneAuth(member,copy(editions),write.pk,copy(member.relays)),null,'read capability cannot sign staff AUTH');
const auth=R.createPlaneAuth(staff,copy(editions),write.pk,copy(member.relays));
const authEvent=auth.sign(copy({kind:22242,created_at:now,content:'',tags:[['relay','wss://relay.example'],['challenge','fixture']]}));
assert.equal(authEvent.pubkey,write.pk);assert(NT.verifyEvent(authEvent));
assert.throws(()=>R.inspectControl(copy({...staff,control_root:'99'.repeat(32)}),[]),/does not match/);
assert.throws(()=>R.inspectControl(copy({...member,control_pk:''}),[]),/invalid control/);
// A valid event memo must not authenticate a later tampered envelope with the same claimed ID.
for(const bad of [{...editions[0],sig:'0'.repeat(128)},{...editions[0],content:editions[1].content}]) assert.equal(R.inspectControl(member,copy([bad])).name,'fixture');
// A different read secret at the same claimed address must not inherit decrypted memo entries.
assert.equal(R.inspectControl(copy({...member,community_root:'66'.repeat(32)}),copy(editions)).name,'fixture');
const archivedRoot='77'.repeat(32),legacy=plane('concord/control',archivedRoot,communityId,0);
const legacyBundle=copy({...member,community_root:archivedRoot,root_epoch:0});delete legacyBundle.control_pk;
const legacyEditions=[wrapRumor({kind:3308,tags:[['vsk','0'],['eid',communityId],['ev','1']],content:JSON.stringify(metadata)},20014,legacy,legacy)];
assert.equal(R.inspectControl(legacyBundle,copy(legacyEditions)).name,metadata.name,'pre-split epochs stay readable');
const withHistory=copy({...staff,held_roots:[{epoch:0,key:archivedRoot}]});
const signEvent=async template=>sign(template);
const changed=await R.createMetadataWrap(withHistory,copy(editions),copy({name:'renamed',description:'new'}),owner,signEvent);
assert.equal(changed.wrap.pubkey,write.pk,'metadata writes current epoch, not archived one');
const openedSeal=JSON.parse(NT.nip44.v2.decrypt(changed.wrap.content,read.conv)),openedRumor=JSON.parse(openedSeal.content),body=JSON.parse(openedRumor.content);
assert.deepEqual(body.future,metadata.future);assert.equal(body.message_expiration,86400);assert.deepEqual(body.icon,metadata.icon);
const added=await R.createChannelWrap(withHistory,copy(editions),copy({name:'new room'}),owner,signEvent);
assert.equal(added.wrap.pubkey,write.pk);
await assert.rejects(R.createChannelWrap(member,copy(editions),copy({name:'cannot sign'}),owner,signEvent),/staff signing key/);
const chat=plane('concord/channel',communityRoot,channel,epoch),message=wrapRumor({kind:9,tags:[['channel',channel],['epoch',String(epoch)]],content:'independent message'},20013,chat,chat);
assert.equal((await R.inspectChat(member,copy(editions),channel,copy([message]))).messages.length,1);
assert.equal((await R.inspectChat(member,copy(editions),channel,copy([{...message,sig:'0'.repeat(128)}]))).messages.length,0,'chat cannot reuse authenticated memo for tampered envelope');
console.log('CORD spec compatibility: independent split/legacy vectors, staff AUTH, invalid envelopes, current epoch writes, metadata preservation passed');
