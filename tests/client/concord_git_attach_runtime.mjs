// ARMADA GIT ATTACHMENTS, WRITTEN AND READ BY THE SHIPPED cord-reader.js.
//
// Armada attaches a NIP-34 repository to a CHANNEL as `custom["armada.git"].repositories`, each a
// half-open interval [attachedAt, detachedAt) (src/concord/lib/types.ts, src/lib/gitActivity.ts).
// This mints a real community and drives the shipped builders through attach → detach → re-attach,
// a channel opened with a repo, a rename that must keep the attachments, an entry another client
// wrote badly, and the permission rule: Moderator (no MANAGE_CHANNELS) is refused, Admin is not.
import fs from 'node:fs';import vm from 'node:vm';import assert from 'node:assert/strict';
globalThis.window=globalThis;
for(const file of ['static/vendor/nostr/nostr.bundle.js','static/js/client/cord-reader.js','static/js/client/cord-protocol.js'])vm.runInThisContext(fs.readFileSync(file,'utf8'));
const NT=NostrTools,R=PosterCordReader;
const key=()=>{const sk=NT.generateSecretKey();return {sk,pk:NT.getPublicKey(sk),sign:e=>NT.finalizeEvent(e,sk)};};
const O=key(),mod=key(),adm=key();
const made=await PosterCord.createCommunity({owner:O.pk,name:'Git',relays:['wss://r.example'],base:'https://fixture.example',signEvent:O.sign});
const bundle={...PosterCord.openInvite(made.url,made.events).bundle,control_root:made.secrets.controlRoot};
let controls=made.events.filter(e=>e.kind===1059);
const info=()=>R.inspectControl(bundle,controls);
const general=info().channels[0].id;
const tick=()=>new Promise(r=>setTimeout(r,3));
const reposOf=id=>info().channels.find(c=>c.id===id).repositories;
const edit=async(changes,who=O,id=general)=>{await tick();const r=await R.createChannelEditWrap(bundle,controls,id,changes,who.pk,who.sign);controls=[...controls,r.wrap];return r;};
const encryptBytesAs=sk=>async(peer,bytes)=>R.encryptRekeyBytes(bytes,NT.nip44.getConversationKey(sk,peer));

const repo='30617:'+O.pk+':posterchanai', other='30617:'+mod.pk+':ngit';

assert.deepEqual(reposOf(general),[],'a fresh channel carries no repositories');

// ── attach: Armada's wire shape exactly ──────────────────────────────────────────────────────────
const a1=await edit({attachRepository:{address:repo,relayHints:['wss://relay.ngit.dev/','not a relay','wss://relay.ngit.dev']}});
let list=reposOf(general);
assert.equal(list.length,1);
assert.equal(list[0].address,repo);
assert.deepEqual(list[0].relayHints,['wss://relay.ngit.dev'],'hints are normalized, deduped, non-relays dropped');
assert.ok(Number.isSafeInteger(list[0].attachedAt)&&!('detachedAt' in list[0]));
assert.deepEqual(a1.repositories,list,'the builder reports exactly what the fold reads back');
assert.deepEqual(Object.keys(a1.channel.custom),['armada.git']);
assert.deepEqual(Object.keys(a1.channel.custom['armada.git'].repositories[0]),['address','relayHints','attachedAt'],
  'an open interval is written with no detachedAt key at all (Armada omits it, never null)');
console.log('attach passed');

await assert.rejects(()=>edit({attachRepository:{address:repo,relayHints:[]}}),/already attached/,'a second open interval for one repo');
await assert.rejects(()=>edit({attachRepository:{address:'https://github.com/x/y',relayHints:[]}}),/30617/);
await assert.rejects(()=>edit({attachRepository:{address:'30617:'+O.pk.toUpperCase()+':x',relayHints:[]}}),/30617/,'Armada requires a lowercase owner');
await assert.rejects(()=>edit({attachRepository:{address:'30023:'+O.pk+':x',relayHints:[]}}),/30617/,'only repository announcements attach');

// ── a rename keeps the attachment (the edit carries `custom`) ─────────────────────────────────────
await edit({name:'dev'});
assert.equal(info().channels.find(c=>c.id===general).name,'dev');
assert.deepEqual(reposOf(general),list,'rename kept the repository untouched');

// ── several repositories on one channel ─────────────────────────────────────────────────────────
await edit({attachRepository:{address:other,relayHints:[]}});
assert.deepEqual(reposOf(general).map(r=>r.address).sort(),[repo,other].sort());

// ── detach CLOSES the interval rather than deleting it ───────────────────────────────────────────
await edit({detachRepository:repo});
list=reposOf(general).filter(r=>r.address===repo);
assert.equal(list.length,1,'detach kept the interval (history stays attached to it)');
assert.ok(Number.isSafeInteger(list[0].detachedAt)&&list[0].detachedAt>=list[0].attachedAt);
assert.equal(reposOf(general).find(r=>r.address===other).detachedAt,undefined,'detaching one repo left the other open');
await assert.rejects(()=>edit({detachRepository:repo}),/not attached/);

// ── re-attach appends a NEW interval; the closed one stays closed ─────────────────────────────────
await edit({attachRepository:{address:repo,relayHints:[]}});
list=reposOf(general).filter(r=>r.address===repo);
assert.equal(list.length,2,'re-attach is a second interval');
assert.equal(list.filter(r=>r.detachedAt===undefined).length,1);
console.log('detach / re-attach passed');

// ── a channel may OPEN with a repository, in its first edition (Armada's New Channel dialog) ───────
await tick();
const ch=await R.createChannelWrap(bundle,controls,{name:'ngit',repository:{address:other,relayHints:['wss://relay.ngit.dev']}},O.pk,O.sign);
controls=[...controls,ch.wrap];
assert.deepEqual(reposOf(ch.channelId).map(r=>[r.address,r.relayHints]),[[other,['wss://relay.ngit.dev']]]);
assert.deepEqual(ch.repositories,reposOf(ch.channelId));
await tick();
await assert.rejects(()=>R.createChannelWrap(bundle,controls,{name:'bad',repository:{address:'nope',relayHints:[]}},O.pk,O.sign),/30617/,
  'a bad address must refuse the channel, not create it without the repo');
console.log('channel with repository passed');

// ── permissions: MANAGE_CHANNELS, which Moderator does NOT hold and Admin does ────────────────────
await tick();
const modRole=await R.createRoleWrap(bundle,controls,'moderator',O.pk,O.sign);controls=[...controls,modRole.wrap];await tick();
const g1=await R.createRoleGrantWrap(bundle,controls,mod.pk,[modRole.roleId],O.pk,O.sign,encryptBytesAs(O.sk));controls=[...controls,g1.wrap];
await assert.rejects(()=>edit({attachRepository:{address:'30617:'+mod.pk+':x',relayHints:[]}},mod),/owner or authorized staff|edit channels/,
  'a Moderator attached a repository; Armada gates this on MANAGE_CHANNELS');
await tick();
const admRole=await R.createRoleWrap(bundle,controls,'admin',O.pk,O.sign);controls=[...controls,admRole.wrap];await tick();
const g2=await R.createRoleGrantWrap(bundle,controls,adm.pk,[admRole.roleId],O.pk,O.sign,encryptBytesAs(O.sk));controls=[...controls,g2.wrap];
await edit({attachRepository:{address:'30617:'+adm.pk+':y',relayHints:[]}},adm);
assert.ok(reposOf(general).some(r=>r.address==='30617:'+adm.pk+':y'),'an Admin attach folded');
console.log('permissions passed');

console.log('OK');
