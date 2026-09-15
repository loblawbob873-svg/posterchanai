// Independent CORD-01/02 wire producer: Node HKDF (not the reader's derivation), NostrTools.
// Frozen spec: concord-protocol/concord b84554ea5dd47510057a580fa2f8587b4399ad17.
import assert from 'node:assert/strict';
import { hkdfSync, createHash, webcrypto } from 'node:crypto';
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

ctx.crypto.subtle=webcrypto.subtle;ctx.AbortController=AbortController;ctx.URL=URL;
loadInto(ctx,new URL('static/js/client/cord-voice.js',root));const V=ctx.PCCordVoice;
const material=R.voiceMaterial(member,copy(editions),channel);
for (const denied of [{removed:true},{dissolved:true},{refounding_pending:true},{removed_channels:[channel]}]) {
  const revoked=copy({...member,...denied});
  assert.throws(()=>R.voiceMaterial(revoked,copy(editions),channel),/read-only/);
  await assert.rejects(()=>R.createVoicePresence(revoked,copy(editions),channel,'joined',owner,
    ()=>assert.fail('revoked membership reached signer'),'identity','https://broker.example'),/read-only/);
}
assert.equal(material.room,NT.getPublicKey(secret(derive('concord/voice-signer',communityRoot,channel,epoch))));
assert.equal(Buffer.from(material.mediaRoot).toString('hex'),derive('concord/voice-media',communityRoot,channel,epoch));
const privateBundle=copy({...member,channels:[{id:channel,key:'77'.repeat(32),epoch:3}]});
const privateEditions=[editions[0],wrapRumor({kind:3308,tags:[['vsk','2'],['eid',channel],['ev','1']],content:JSON.stringify({name:'private',private:true})})];
const privateVoice=R.voiceMaterial(privateBundle,copy(privateEditions),channel);
assert.equal(privateVoice.room,NT.getPublicKey(secret(derive('concord/voice-signer','77'.repeat(32),channel,3))));
assert.notEqual(privateVoice.room,material.room);
assert.throws(()=>R.voiceMaterial(member,copy(privateEditions),channel),/cannot call/);
// Authenticated cached public history keeps this converted channel readable, without call keys.
const historyOnly=copy({...member,public_channel_history:[channel]});
assert.equal(R.inspectControl(historyOnly,copy(privateEditions)).channels[0].readOnly,true);
assert.throws(()=>R.voiceMaterial(historyOnly,copy(privateEditions),channel),/cannot call/);
await assert.rejects(()=>R.createVoicePresence(historyOnly,copy(privateEditions),channel,'joined',owner,
  ()=>assert.fail('history-only channel reached signer'),'identity','https://broker.example'),/cannot call/);
const identity='independent-broker-identity';
const expectedSender=derive('concord/voice-sender',Buffer.from(material.mediaRoot).toString('hex'),createHash('sha256').update(identity).digest('hex'));
assert.equal(Buffer.from(await V.senderKey(material.mediaRoot,identity)).toString('hex'),expectedSender);
assert.notEqual(Buffer.from(await V.senderKey(material.mediaRoot,'other-sender')).toString('hex'),expectedSender);
const chat=plane('concord/channel',communityRoot,channel,epoch);
function externalPresence(ms,verb='joined',key=ownerSk,extra=[]){
  const author=NT.getPublicKey(key),rumor=copy({kind:23313,pubkey:author,created_at:Math.floor(ms/1000),content:verb,
    tags:[['channel',channel],['epoch',String(epoch)],['ms',String(ms%1000)],...(verb==='joined'?[['identity',identity],['broker','https://broker.example']]:[]),...extra]});
  rumor.id=NT.getEventHash(rumor);
  const seal=sign({kind:20013,created_at:now,tags:[],content:NT.nip44.v2.encrypt(JSON.stringify(rumor),chat.conv)},key);
  return sign({kind:21059,created_at:now,tags:[['p',author]],content:NT.nip44.v2.encrypt(JSON.stringify(seal),chat.conv)},chat.sk);
}
const ms=now*1000;
const external=externalPresence(ms),opened=await R.inspectVoicePresence(member,copy(editions),channel,copy([external]));
assert.equal(opened.length,1);assert.equal(opened[0].pubkey,owner);
assert.equal(V.fold(opened,ms+89999)[0].verified,true);assert.equal(V.fold(opened,ms+90000).length,0);
const second=externalPresence(ms,'joined',secret('66'.repeat(32)));
const conflicts=await R.inspectVoicePresence(member,copy(editions),channel,copy([external,second]));
assert(V.fold(conflicts,ms).every(p=>p.verified===false),'two identity claimants cannot be attributed');
const left=await R.inspectVoicePresence(member,copy(editions),channel,copy([externalPresence(ms+1,'left')]));
assert.equal(V.fold([...opened,...left],ms+1).length,0);
const tampered=copy(external);tampered.content=tampered.content.slice(0,-1)+'A';
assert.equal((await R.inspectVoicePresence(member,copy(editions),channel,[tampered])).length,0);
assert.equal((await R.inspectVoicePresence(member,copy(editions),channel,copy([{...external,kind:1059}]))).length,0);
const produced=await R.createVoicePresence(member,copy(editions),channel,'joined',owner,t=>Promise.resolve(sign(t)),identity,'https://broker.example');
assert.equal(produced.kind,21059);assert(NT.verifyEvent(copy(produced)));
const seal=JSON.parse(NT.nip44.v2.decrypt(produced.content,chat.conv));assert(NT.verifyEvent(copy(seal)));assert.equal(seal.kind,20013);
const rumor=JSON.parse(NT.nip44.v2.decrypt(seal.content,chat.conv));assert.equal(rumor.kind,23313);assert.equal(rumor.content,'joined');
assert(rumor.tags.some(t=>t[0]==='channel'&&t[1]===channel));
assert.equal(V.origin('HTTPS://Example.COM:443/path'),'https://example.com');
assert.equal(V.origin('http://example.com'),null);assert.equal(V.origin('https://u:p@example.com'),null);
const origin='https://broker.example',expectedRank=createHash('sha256').update(Buffer.concat([Buffer.from(material.room,'hex'),Buffer.from(origin)])).digest('hex');
assert.equal(await V.rank(material.room,origin),expectedRank);
const calls=[];let grant;
const fetcher=async(url,opts)=>{calls.push({url,opts});if(calls.length===1)return{status:204};
  grant=JSON.parse(Buffer.from(opts.headers.Authorization.slice(8),'base64').toString());
  return{ok:true,json:async()=>({token:'fixture-token',url:'wss://sfu.example',identity})};};
const result=await V.token(material,origin,{fetcher,now:ms});
assert.equal(result.identity,identity);assert(NT.verifyEvent(copy(grant)));assert.equal(grant.pubkey,material.room);assert.equal(grant.kind,27235);
assert.deepEqual(grant.tags.slice(0,2),[['u',origin+'/.well-known/concord/av/'+material.room],['method','GET']]);
assert(calls.every(c=>c.opts.redirect==='error'&&c.opts.credentials==='omit'));
await assert.rejects(()=>V.token(material,'http://broker.example',{fetcher}),/HTTPS/);
const cancel=new AbortController();let requests=0;
await assert.rejects(()=>V.token(material,origin,{signal:cancel.signal,fetcher:async()=>{requests++;cancel.abort();return{status:204};}}),/canceled/);
assert.equal(requests,1,'canceled capability probe must not send a bearer grant');
await assert.rejects(()=>V.token(material,origin,{fetcher:async(url)=>url.endsWith('/av')?{status:204}:{ok:true,json:async()=>({token:'x',identity,url:'ws://sfu.example'})}}),/invalid response/);

console.log('independent voice keys, presence, sender separation and broker grants passed');
