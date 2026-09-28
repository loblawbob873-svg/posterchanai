// What a Vector user and ours see of EACH OTHER in a Concord room — the shipped concord.js functions,
// lifted out by name and run under node against events shaped exactly as Vector writes them
// (crates/vector-core/src/community/{v2/chat.rs, attachments.rs}, src-tauri subscription_handler.rs).
import fs from 'node:fs';import vm from 'node:vm';import assert from 'node:assert/strict';import {webcrypto} from 'node:crypto';
globalThis.window=globalThis;if(!globalThis.crypto)globalThis.crypto=webcrypto;
vm.runInThisContext(fs.readFileSync('static/vendor/nostr/nostr.bundle.js','utf8'));
const NT=NostrTools;
function lift(file,...names){
  const code=fs.readFileSync(file,'utf8');
  for(const name of names){
    let i=code.indexOf('  function '+name+'(');if(i<0)i=code.indexOf('  async function '+name+'(');
    assert(i>=0,name+' is missing from '+file);
    const j=code.indexOf('\n  }\n',i)+4;vm.runInThisContext(code.slice(i,j));
  }
}
const C='static/js/client/concord.js';
lift(C,'wireReplyParentId','wireMentionText','npubOf','readableMentions','mentionNames','wireAttachmentText','attachmentImeta','sealAttachment','bytesHex','imetaFields','encryptedAttachments','hexBytes','canEditMessage');

// ── REPLIES: Vector's everyday reply is kind 9 + ["q", parent, relay, author] (NIP-C7) ─────────────
const parent='a'.repeat(64);
assert.equal(wireReplyParentId({kind:9,tags:[['q',parent,'','b'.repeat(64)]]}),parent,'a Vector reply is read as a message out of nowhere');
assert.equal(wireReplyParentId({kind:1111,tags:[['E','r'],['e',parent]]}),parent,'our own thread replies stopped resolving');
assert.equal(wireReplyParentId({kind:9,tags:[['q','nevent1notahexid']]}),'','a q that is not an event id is not a parent');
assert.equal(wireReplyParentId({kind:7,tags:[['q',parent]]}),'','only messages are replies');
console.log('replies passed');

// ── MENTIONS: Vector notifies on `@npub1…` in the TEXT, and reads no p tag for it ──────────────────
const pk=NT.getPublicKey(NT.generateSecretKey()),np=NT.nip19.npubEncode(pk);
assert.equal(wireMentionText('hi @alice and @alice_b!',[['alice',pk],['alice_b','c'.repeat(64)]],x=>x===pk?np:'npubC'),`hi @${np} and @npubC!`,'longest handle first, both rewritten');
assert.equal(wireMentionText('mail me at bob@alice.com',[['alice',pk]],()=>np),'mail me at bob@alice.com','an email address is not a mention');
assert.equal(wireMentionText('@Alice Smith said',[['alice smith',pk]],()=>np),`@${np} said`,'a picked handle with a space');
assert.equal(readableMentions(`ping @${np} now`),`ping nostr:${np} now`,'@npub must reach linkify as an entity, or it renders @@Name');
assert.equal(mentionNames(`ping @${np}`,k=>k===pk?{name:'Alice'}:{}),'ping @Alice','a notification body showed the raw npub');
console.log('mentions passed');

// ── ATTACHMENTS: end-to-end encrypted, Vector's imeta field set, caption without the URL ───────────
const plain=new TextEncoder().encode('the secret picture bytes '.repeat(40));
const file={type:'image/png',arrayBuffer:async()=>plain.buffer.slice(0)};
globalThis.Blob=globalThis.Blob||class{};
const sealed=await sealAttachment(file);
assert.equal(sealed.nonce.length,32,'Vector uses a 16-byte AES-GCM nonce');
assert.equal(sealed.key.length,64);assert.equal(sealed.size,plain.byteLength);
const cipher=new Uint8Array(await sealed.blob.arrayBuffer());
assert.notDeepEqual(cipher.slice(0,20),plain.slice(0,20),'the uploaded bytes are the plaintext');
const url='https://blossom.example/'+'e'.repeat(64);
const tag=attachmentImeta(url,'image/png','cat.png',sealed);
for(const field of ['url '+url,'m image/png','encryption-algorithm aes-gcm','decryption-key '+sealed.key,'decryption-nonce '+sealed.nonce,'ox '+sealed.ox,'name cat.png','size '+plain.byteLength])
  assert(tag.includes(field),'imeta is missing Vector field: '+field);
// The reader every client folds with opens it, and the bytes decrypt back with the key it names.
const got=encryptedAttachments({tags:[tag]});
assert.equal(got.length,1,'our own attachment is not recognised as an encrypted one');
const k=await crypto.subtle.importKey('raw',hexBytes(got[0].key),'AES-GCM',false,['decrypt']);
const back=new Uint8Array(await crypto.subtle.decrypt({name:'AES-GCM',iv:hexBytes(got[0].nonce)},k,cipher));
assert.deepEqual(back,plain,'round trip failed');
assert.equal(bytesHex(await crypto.subtle.digest('SHA-256',back)),got[0].hash,'ox is not the plaintext sha256');
assert.equal(wireAttachmentText(`look ${url} nice`,[url]),'look nice','Vector prints a raw Blossom link above the file');
assert.equal(wireAttachmentText(url,[url]),'','an attachment-only message keeps a stray space');
console.log('attachments passed');

// ── EDIT is offered on your own confirmed text message in a relay community, and nowhere else ──────
const me={pubkey:'m'.repeat(64)},room={cord:{bundle:{}},local:false};
assert(canEditMessage({pubkey:me.pubkey,kind:9,id:'x'},me,room));
assert(!canEditMessage({pubkey:'o'.repeat(64),kind:9,id:'x'},me,room),'edit offered on someone else\'s message');
assert(!canEditMessage({pubkey:me.pubkey,kind:9,id:'pending-1',pending:true},me,room),'edit offered before the message was sent');
assert(!canEditMessage({pubkey:me.pubkey,kind:9,id:'x'},me,{...room,protocol:'nip29'}),'NIP-29 has no 3302');
console.log('edit gating passed');

// ── NIP-17: a seal that is not a valid signed kind-13 is refused (NIP-59) ─────────────────────────
lift('static/js/client/app.js','_nip17unwrapVia');
const sender=NT.generateSecretKey(),senderPk=NT.getPublicKey(sender),rumor={pubkey:senderPk,kind:14,content:'hi',tags:[],created_at:1};
const goodSeal=NT.finalizeEvent({kind:13,content:'RUMOR',tags:[],created_at:1},sender);
const dec=seal=>async(pk2,ct)=>ct==='WRAP'?JSON.stringify(seal):JSON.stringify(rumor);
assert.equal((await _nip17unwrapVia(dec(goodSeal),{pubkey:'w',content:'WRAP'})).content,'hi','a valid seal was refused');
await assert.rejects(()=>_nip17unwrapVia(dec({...goodSeal,sig:'0'.repeat(128)}),{pubkey:'w',content:'WRAP'}),/seal signature invalid/,'a forged seal signature was accepted');
await assert.rejects(()=>_nip17unwrapVia(dec({...goodSeal,kind:1}),{pubkey:'w',content:'WRAP'}),/seal signature invalid/,'a non-seal was accepted');
console.log('nip17 seal passed');
