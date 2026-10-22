// A COMMUNITY'S RELAY LIST IS WRITABLE (the SHIPPED cord-reader.js createMetadataWrap, CORD-02 metadata `relays`).
//
// 2026-10-21 "add the backups to the invite": a community made on a PosterChan instance listed its node alone. The
// metadata edition can now set the authoritative relay list -- every client folds it (inspectControl relays /
// relaysAuthoritative) -- validated: wss:// only, de-duplicated, at most 8, never empty. Without `relays` an edit keeps
// the list it had (a name change must never drop the backups). And only someone who may edit metadata can write it.
import fs from 'node:fs';import vm from 'node:vm';import assert from 'node:assert/strict';
globalThis.window=globalThis;
for(const file of ['static/vendor/nostr/nostr.bundle.js','static/js/client/cord-reader.js','static/js/client/cord-protocol.js'])vm.runInThisContext(fs.readFileSync(file,'utf8'));
const NT=NostrTools,R=PosterCordReader;
const key=()=>{const sk=NT.generateSecretKey();return {sk,pk:NT.getPublicKey(sk),sign:e=>NT.finalizeEvent(e,sk)};};
const O=key(),stranger=key();
const HOME='wss://relay.poster.place',BACKUPS=['wss://jskitty.com/nostr','wss://nostr.computingcache.com','wss://relay.dreamith.to'];
const made=await PosterCord.createCommunity({owner:O.pk,name:'Hosted',relays:[HOME],base:'https://fixture.example',signEvent:O.sign});
const bundle={...PosterCord.openInvite(made.url,made.events).bundle,control_root:made.secrets.controlRoot};
let controls=made.events.filter(e=>e.kind===1059);
const info=()=>R.inspectControl(bundle,controls);
const tick=()=>new Promise(r=>setTimeout(r,3));
const edit=async(meta,who=O)=>{await tick();const r=await R.createMetadataWrap(bundle,controls,meta,who.pk,who.sign);controls=[...controls,r.wrap];return r;};
assert.deepEqual(info().relays,[HOME]);
// 1. Listing the backups: authoritative, in order, trailing slashes and duplicates cleaned.
await edit({relays:[HOME,BACKUPS[0]+'/',...BACKUPS,BACKUPS[1]]});
assert.equal(info().relaysAuthoritative,true);
assert.deepEqual(info().relays,[HOME,...BACKUPS]);
// 2. An ordinary edit (a rename) keeps the list.
await edit({name:'Hosted renamed'});
assert.equal(info().name,'Hosted renamed');
assert.deepEqual(info().relays,[HOME,...BACKUPS],'a rename dropped the backups');
// 3. Junk is dropped; nothing usable is refused; more than the protocol's cap is cut at the cap.
await edit({relays:['http://plain.example','javascript:alert(1)','wss://ok.example',' ']});
assert.deepEqual(info().relays,['wss://ok.example']);
await assert.rejects(()=>edit({relays:['http://nope.example']}),/at least one relay/);
await edit({relays:Array.from({length:12},(_,i)=>'wss://r'+i+'.example')});
assert.equal(info().relays.length,5,'past the protocol cap (MAX_COMMUNITY_RELAYS) a reader would drop them');
// 4. Someone who may not edit the community cannot write its relays.
await assert.rejects(()=>edit({relays:['wss://evil.example']},stranger));
assert.ok(!info().relays.includes('wss://evil.example'));
console.log('OK');
