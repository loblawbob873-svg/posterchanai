// Concord parity with Vector (the reference client, same CORD specs): the WRITERS we lacked, run
// through the shipped cord-reader.js against a real community, and read back by the same fold every
// client uses. Each block names the Vector function whose wire shape it matches.
import fs from 'node:fs';import vm from 'node:vm';import assert from 'node:assert/strict';
globalThis.window=globalThis;
for(const file of ['static/vendor/nostr/nostr.bundle.js','static/js/client/cord-reader.js','static/js/client/cord-protocol.js'])vm.runInThisContext(fs.readFileSync(file,'utf8'));
const NT=NostrTools,R=PosterCordReader,hex=b=>[...b].map(x=>x.toString(16).padStart(2,'0')).join('');
const key=()=>{const sk=NT.generateSecretKey();return {sk,pk:NT.getPublicKey(sk),sign:e=>NT.finalizeEvent(e,sk)};};
const O=key(),mod=key(),alice=key(),bob=key();
const made=await PosterCord.createCommunity({owner:O.pk,name:'Parity',relays:['wss://r.example'],base:'https://fixture.example',signEvent:O.sign});
const bundle={...PosterCord.openInvite(made.url,made.events).bundle,control_root:made.secrets.controlRoot};
let controls=made.events.filter(e=>e.kind===1059);
const info=()=>R.inspectControl(bundle,controls);
const general=info().channels[0].id;
const encryptBytesAs=sk=>async(peer,bytes)=>R.encryptRekeyBytes(bytes,NT.nip44.getConversationKey(sk,peer));
const tick=()=>new Promise(r=>setTimeout(r,3));

// ── roles: Vector's server Moderator, then a grant (build_role / set_member_grant) ──────────────────
const role=await R.createRoleWrap(bundle,controls,'moderator',O.pk,O.sign);controls=[...controls,role.wrap];
const listed=info().roles.find(r=>r.id===role.roleId);
assert(listed&&listed.name==='Moderator'&&listed.scope==='server','the Moderator role did not fold');
assert.match(listed.permissions,/^[0-9]+$/,'permissions must be a decimal string on the wire (CORD-04 §3)');
await assert.rejects(()=>R.createRoleWrap(bundle,controls,'moderator',alice.pk,alice.sign),/owner or authorized staff/,'a plain member minted a role');
await tick();
const grant=await R.createRoleGrantWrap(bundle,controls,mod.pk,[role.roleId],O.pk,O.sign,encryptBytesAs(O.sk));controls=[...controls,grant.wrap];
assert.deepEqual(info().grants[mod.pk],[role.roleId]);
console.log('roles passed');

// ── the vac a moderator's removal cites (build_delete_rumor citation) ─────────────────────────────
assert.deepEqual(R.authorityCitation(bundle,controls,O.pk,'MANAGE_MESSAGES'),[],'the owner cites nothing');
const vac=R.authorityCitation(bundle,controls,mod.pk,'MANAGE_MESSAGES');
assert.equal(vac.length,1);assert.equal(vac[0][0],'vac');assert.equal(vac[0].length,4);
assert.throws(()=>R.authorityCitation(bundle,controls,alice.pk,'MANAGE_MESSAGES'),/owner or authorized staff/);
console.log('authority citation passed');

// ── delegated ban (Vector lets BAN holders write the banlist) ─────────────────────────────────────
await tick();
const ban=await R.createBanWrap(bundle,controls,bob.pk,mod.pk,mod.sign);controls=[...controls,ban.wrap];
assert(info().banned.includes(bob.pk),'a moderator ban did not fold');
await assert.rejects(()=>R.createBanWrap(bundle,controls,O.pk,mod.pk,mod.sign),/owner cannot be banned/);
await assert.rejects(()=>R.createBanWrap(bundle,controls,mod.pk,alice.pk,alice.sign),/owner or authorized staff/,'a plain member banned someone');
console.log('delegated ban passed');

// ── kick (guestbook 3309, build_kick_rumor) ───────────────────────────────────────────────────────
const join=await R.createGuestbookWrap(bundle,'join',alice.pk,alice.sign);await tick();
let gb=R.inspectGuestbook(bundle,controls,[join.wrap],[],Date.now());assert(gb.members.includes(alice.pk));
const kick=await R.createKickWrap(bundle,controls,alice.pk,mod.pk,mod.sign);
gb=R.inspectGuestbook(bundle,controls,[join.wrap,kick.wrap],[],Date.now());
assert(!gb.members.includes(alice.pk),'a moderator kick did not remove the member');
await assert.rejects(()=>R.createKickWrap(bundle,controls,O.pk,mod.pk,mod.sign),/owner cannot be kicked/);
await assert.rejects(()=>R.createKickWrap(bundle,controls,mod.pk,alice.pk,alice.sign),/owner or authorized staff/);
console.log('kick passed');

// ── channel rename / delete (a new channel edition, prior fields carried) ──────────────────────────
await tick();
const extra=await R.createChannelWrap(bundle,controls,{name:'offtopic'},O.pk,O.sign);controls=[...controls,extra.wrap];await tick();
const ren=await R.createChannelEditWrap(bundle,controls,extra.channelId,{name:'random'},O.pk,O.sign);controls=[...controls,ren.wrap];
assert.equal(ren.channel.private,false,'a rename dropped a field of the prior edition');
assert(info().channels.some(c=>c.id===extra.channelId&&c.name==='random'),'the rename did not fold');
await tick();
const del=await R.createChannelEditWrap(bundle,controls,extra.channelId,{deleted:true},O.pk,O.sign);controls=[...controls,del.wrap];
assert(info().deletedChannels.includes(extra.channelId),'the delete did not fold');
await assert.rejects(()=>R.createChannelEditWrap(bundle,controls,general,{deleted:true},O.pk,O.sign),/at least one channel/);
await assert.rejects(()=>R.createChannelEditWrap(bundle,controls,general,{name:'x'},alice.pk,alice.sign),/owner or authorized staff/);
console.log('channel edit passed');

// ── encrypted icon: CORD-02 `icon` ImageRef, exclusive with the old plain `picture` ───────────────
await tick();
const ref={url:'https://blossom.example/'+'a'.repeat(64),key:'b'.repeat(64),nonce:'c'.repeat(32),hash:'d'.repeat(64),ext:'png'};
const m1=await R.createMetadataWrap(bundle,controls,{iconRef:ref},O.pk,O.sign);controls=[...controls,m1.wrap];
assert.deepEqual(m1.metadata.icon,ref);assert(!('picture' in m1.metadata),'a stale plain picture would outrank the encrypted icon');
assert.deepEqual(info().icon,ref,'the encrypted icon did not fold');
await assert.rejects(()=>R.createMetadataWrap(bundle,controls,{iconRef:{...ref,nonce:'c'.repeat(24)}},O.pk,O.sign),/invalid encrypted icon/,'a 12-byte nonce is not Vector\'s ImageRef');
await tick();
const m2=await R.createMetadataWrap(bundle,controls,{icon:'🚀'},O.pk,O.sign);controls=[...controls,m2.wrap];
assert.equal(m2.metadata.picture,'🚀');assert(!('icon' in m2.metadata),'Vector would keep showing the old encrypted icon');
console.log('encrypted icon passed');

// ── typing (23311 in an EPHEMERAL 21059, build_typing_rumor) ───────────────────────────────────────
const typing=await R.createTypingWrap(bundle,controls,general,mod.pk,mod.sign);
assert.equal(typing.wrap.kind,21059,'typing must ride an ephemeral wrap');
assert.deepEqual(await R.inspectTyping(bundle,controls,general,[typing.wrap]),[mod.pk]);
assert.deepEqual(await R.inspectTyping(bundle,controls,general,[typing.wrap],0),[],'a stale typing signal still showed');
console.log('typing passed');

// ── the FIRST pin of a channel (no pin-list entity yet — Vector publishes version 1) ──────────────
const msg=await R.createChatWrap(bundle,controls,general,'pin me',O.pk,O.sign);
const proof=R.makePinProof(bundle,controls,general,msg.wrap);
await assert.rejects(()=>R.createPinListWrap(bundle,controls,general,[proof],O.pk,O.sign),/history is unavailable/,'an unread pin list must not be overwritten by default');
const pins=await R.createPinListWrap(bundle,controls,general,[proof],O.pk,O.sign,{firstPin:true});controls=[...controls,pins.wrap];
assert.deepEqual(R.inspectPinList(bundle,controls,general).entries.map(e=>e.id),[msg.rumorId],'the first pin did not fold');
await tick();
const unpin=await R.createPinListWrap(bundle,controls,general,[],O.pk,O.sign);controls=[...controls,unpin.wrap];
assert.deepEqual(R.inspectPinList(bundle,controls,general).entries,[],'unpin did not fold');
console.log('pins passed');

// ── rekey chunks fit strfry's 64 KB maxEventSize (Vector splits for the same reason) ───────────────
const recipients=[{pubkey:O.pk},...Array.from({length:100},()=>({pubkey:key().pk}))];
const rk=await R.createRekeyWraps(bundle,controls,{scope:'0'.repeat(64),recipients},O.pk,O.sign,encryptBytesAs(O.sk));
const rkWraps=(rk.wraps||rk).filter(w=>w&&w.kind===1059);
assert.equal(rkWraps.length,2,'101 recipients must split into two chunks');
for(const w of rkWraps)assert(JSON.stringify(w).length<64*1024,'a rekey chunk is over strfry\'s 64 KB maxEventSize: '+JSON.stringify(w).length);
console.log('rekey chunking passed');

// ── dissolution (3308 vsk 10, eid = community id — Vector's erratum, which we match) ─────────────
const diss=await R.createDissolutionWrap(bundle,O.pk,O.sign);
assert.equal(diss.wrap.pubkey,R.dissolutionPubkey(bundle),'dissolutionPubkey is not where the tombstone is wrapped');
assert(R.inspectDissolution(bundle,[diss.wrap]),'our own dissolution is not recognised');
await assert.rejects(()=>R.createDissolutionWrap(bundle,mod.pk,mod.sign),/only the owner/);
console.log('dissolution passed');
