// CORD-02 §5 guestbook WRITES and CORD-03 §3 reply tags -- the shipped cord-reader.js / concord.js.
import fs from 'node:fs';import vm from 'node:vm';import assert from 'node:assert/strict';
globalThis.window=globalThis;
for(const file of ['static/vendor/nostr/nostr.bundle.js','static/js/client/cord-reader.js','static/js/client/cord-protocol.js'])vm.runInThisContext(fs.readFileSync(file,'utf8'));
const NT=NostrTools,R=PosterCordReader;
const osk=NT.generateSecretKey(),owner=NT.getPublicKey(osk),osign=e=>NT.finalizeEvent(e,osk);
const made=await PosterCord.createCommunity({owner,name:'Guests',relays:['wss://r.example'],base:'https://fixture.example',signEvent:osign});
const bundle={...PosterCord.openInvite(made.url,made.events).bundle,control_root:made.secrets.controlRoot},controls=made.events.filter(e=>e.kind===1059);
const msk=NT.generateSecretKey(),member=NT.getPublicKey(msk),msign=e=>NT.finalizeEvent(e,msk);

// A Join is readable by the SAME reader every client folds with, and names its author.
const join=await R.createGuestbookWrap(bundle,'join',member,msign,{creator:owner,label:'friends'});
assert.equal(join.wrap.kind,1059);
assert.deepEqual(R.inspectControl(bundle,controls).guestbookPubkeys.includes(join.wrap.pubkey),true,'wrapped at the guestbook key');
let gb=R.inspectGuestbook(bundle,controls,[join.wrap],[],Date.now());
assert(gb.members.includes(member),'a published Join does not make the member present');
// …with the invite attribution CORD-05 asks the Join to echo.
const gk=R.inspectControl(bundle,controls).guestbookPubkeys[0];
// A later Leave removes them; nothing else in the fold changes.
await new Promise(r=>setTimeout(r,5));
const leave=await R.createGuestbookWrap(bundle,'leave',member,msign);
gb=R.inspectGuestbook(bundle,controls,[join.wrap,leave.wrap],[],Date.now());
assert(!gb.members.includes(member),'a Leave did not take the member out');
assert(gb.members.includes(owner));
await assert.rejects(()=>R.createGuestbookWrap(bundle,'kick',member,msign),/join or leave/);
console.log('guestbook join/leave writer passed');

// Reply tags: uppercase K/E/P are the ROOT, inherited verbatim; P names the root author only.
const code=fs.readFileSync('static/js/client/concord.js','utf8'),i=code.indexOf('  function cordReplyTags('),j=code.indexOf('\n  }\n',i)+4;
vm.runInThisContext(code.slice(i,j));
const alice='a'.repeat(64),bob='b'.repeat(64),carol='c'.repeat(64);
const root={id:'r1',kind:9,pubkey:alice,tags:[]};
let t=cordReplyTags(root,'r1',[carol]);
assert.deepEqual(t.filter(x=>x[0]==='P'),[['P',alice]],'P must be the root author, once');
assert.deepEqual(t.filter(x=>x[0]==='E'),[['E','r1','',alice]]);
assert.deepEqual(t.filter(x=>x[0]==='p').map(x=>x[1]),[alice,carol]);
const reply={id:'r2',kind:1111,pubkey:bob,tags:[['K','9'],['E','r1','',alice],['P',alice],['k','9'],['e','r1','',alice]]};
t=cordReplyTags(reply,'r2',[carol,alice]);
assert.deepEqual(t.filter(x=>'KEP'.includes(x[0])),[['K','9'],['E','r1','',alice],['P',alice]],'root scope not inherited verbatim');
assert.deepEqual(t.filter(x=>x[0]==='e'),[['e','r2','',bob]],'lowercase e names the PARENT');
assert.deepEqual(t.filter(x=>x[0]==='p').map(x=>x[1]),[bob,carol,alice]);
assert(!t.some(x=>x[0]==='P'&&x[1]!==alice),'a participant leaked into P');
console.log('CORD-03 reply tags passed');
