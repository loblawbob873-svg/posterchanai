import fs from 'node:fs';
import vm from 'node:vm';
import assert from 'node:assert/strict';
import {makeRealm,loadInto,into} from '../../botframework/cord_realm.mjs';
const root=new URL('../../',import.meta.url),source=fs.readFileSync(new URL('static/js/client/concord.js',root),'utf8');
const ctx=makeRealm(),copy=into(ctx);loadInto(ctx,new URL('static/vendor/nostr/nostr.bundle.js',root));
const NT=ctx.NostrTools,sk=NT.generateSecretKey(),owner=NT.getPublicKey(sk),key=NT.nip44.v2.utils.getConversationKey(sk,owner);
const hex=n=>n.toString(16).padStart(64,'0'),b64=h=>Buffer.from(h,'hex').toString('base64url');
let viewer=owner,relay=[],published=[],stale=false,signHook=null;
const docOf=ev=>JSON.parse(NT.nip44.v2.decrypt(ev.content,key));
const sign=template=>NT.finalizeEvent(copy(template),sk);
const make=(index,doc,at=100)=>sign({kind:33302,created_at:at,tags:[['d',String(index)]],content:NT.nip44.v2.encrypt(JSON.stringify(doc),key)});
const query=async()=>copy(relay);
ctx.membershipEvents=query;ctx.CORD_RELAYS=copy(['wss://test.example']);
ctx.roomIdentity=room=>room.communityId;ctx.forgetLeftCommunity=()=>{};ctx.rememberLeftCommunity=()=>{};
const api={viewer:()=>({pubkey:viewer}),nip44enc:async(_p,text)=>NT.nip44.v2.encrypt(text,key),nip44dec:async(_p,text)=>NT.nip44.v2.decrypt(text,key),
 signTemplate:async template=>{if(signHook)await signHook();return sign(template);},relayPublishTo:async(_urls,ev)=>{assert(NT.verifyEvent(copy(ev)));published.push(ev);if(!stale){const d=ev.tags[0][1];relay=relay.filter(e=>e.tags[0][1]!==d);relay.push(ev);}return true;}};
vm.runInContext(source.slice(source.indexOf('  function cordListHex('),source.indexOf('  function cordListMaterial('))+
 source.slice(source.indexOf('  // CORD-02 §8: canonical'),source.indexOf('  // Only encrypted, signed envelopes cross')),ctx);
const reset=()=>{relay=[];published=[];stale=false;signHook=null;viewer=owner;vm.runInContext('membershipPublished.clear();membershipWrites.clear();',ctx);};
const material=(n,extra={})=>({owner:b64(owner),owner_salt:b64(hex(2)),community_root:b64(hex(1000+n)),root_epoch:0,channels:[],name:'room '+n,relays:['wss://test.example'],...extra});
const entry=(n,extra={})=>({community_id:b64(hex(n)),current:material(n),added_at:1000,...extra});
const room=(n,extra={})=>copy({communityId:hex(n),url:'https://example.test/invite/'+n+'#token',name:'room '+n,cord:{bundle:{community_id:hex(n),...material(n,extra)}}});
const maxEpoch='18446744073709551615',priorEpoch='18446744073709551614';
const bigMerge=ctx.cordMergeEntry(copy(entry(1,{current:material(1,{root_epoch:priorEpoch})})),copy(entry(1,{current:material(2,{root_epoch:maxEpoch})})));
assert.equal(ctx.cordInteger(bigMerge.current.root_epoch),maxEpoch,'adjacent u64 epochs must not collapse through Number');
const byteTie=ctx.cordMergeEntry(copy(entry(1,{current:material(1,{name:'\uE000'})})),copy(entry(1,{current:material(1,{name:'\u{10000}'})})));
assert.equal(byteTie.current.name,'\uE000','canonical-byte tie is UTF-8, not JavaScript UTF-16 ordering');
// Independent encrypted fragmented input, including opaque fields at every level.
reset();
const retained={name:'old',root_epoch:0,...material(1),channels:[{id:b64(hex(70)),key:b64(hex(71)),epoch:0,name:'old name',future:{bytes:'AbCdEF'}}],future:{token:'DoNotCaseFold'}};
const current={...retained,root_epoch:2,community_root:b64(hex(2001)),name:'new',channels:[{...retained.channels[0],name:'new name'}]};
relay=[make(0,{frags:2,futureTop:{secret:'Opaque'},entries:[entry(1,{current,seed:retained,unknownEntry:['x']})],tombstones:[]}),make(1,{frags:2,entries:[entry(2)],tombstones:[{community_id:b64(hex(3)),removed_at:900,opaqueTomb:'keep'}]})];
await ctx.persistArmadaMembership(api,room(4));
assert.equal(published.length,1,'small join only rewrites its own fragment');assert.equal(published[0].kind,33302);assert.equal(published[0].tags[0][1],'0');assert(published[0].created_at>100);
let doc=docOf(published[0]);assert.deepEqual(doc.futureTop,{secret:'Opaque'});
let old=doc.entries.find(e=>e.community_id===b64(hex(1)));assert.deepEqual(old.unknownEntry,['x']);assert.deepEqual(old.current.future,retained.future);assert.equal(old.seed.name,old.current.name);assert.equal(old.seed.channels[0].name,old.current.channels[0].name);assert.equal(old.current.channels[0].future.bytes,'AbCdEF');assert(!('community_id'in old.current));
const fresh=doc.entries.find(e=>e.community_id===b64(hex(4)));assert(!('seed'in fresh));assert.equal(fresh.current.owner,b64(owner));
assert.equal(docOf(relay.find(e=>e.tags[0][1]==='1')).tombstones[0].opaqueTomb,'keep');
// Independently encrypted numeric wire tokens stay exact across read/merge/write.
reset();
const rawDoc=JSON.stringify({frags:1,entries:[entry(1,{added_at:'RAW_PRIOR',current:material(1,{root_epoch:'RAW_MAX',channels:[{id:b64(hex(70)),key:b64(hex(71)),epoch:'RAW_PRIOR'}],opaque:{numeric:'RAW_MAX',text:maxEpoch,root_epoch:maxEpoch}})})],tombstones:[{community_id:b64(hex(1)),removed_at:'RAW_OLDER'}]}).replaceAll('"RAW_MAX"',maxEpoch).replaceAll('"RAW_PRIOR"',priorEpoch).replaceAll('"RAW_OLDER"','18446744073709551613');
relay=[sign({kind:33302,created_at:100,tags:[['d','0']],content:NT.nip44.v2.encrypt(rawDoc,key)})];
await ctx.persistArmadaMembership(api,room(4));
const exactWire=NT.nip44.v2.decrypt(published[0].content,key);
assert(exactWire.includes('"root_epoch":'+maxEpoch));assert(exactWire.includes('"epoch":'+priorEpoch));
assert(exactWire.includes('"added_at":'+priorEpoch));assert(exactWire.includes('"numeric":'+maxEpoch));
assert(exactWire.includes('"text":"'+maxEpoch+'"'),'opaque digit strings retain string type');
assert(exactWire.includes('"root_epoch":"'+maxEpoch+'"'),'unknown nested names must not trigger numeric conversion');
assert(ctx.cordMergeLists([ctx.cordJsonParse(exactWire)]).entries.some(e=>e.community_id===b64(hex(1))),'adjacent timestamps must not round a live entry into its tombstone');
await ctx.leaveArmadaMembership(api,room(1));
const leftWire=NT.nip44.v2.decrypt(published.at(-1).content,key);assert(leftWire.includes('"removed_at":'+priorEpoch));
assert(!ctx.cordMergeLists([ctx.cordJsonParse(leftWire)]).entries.some(e=>e.community_id===b64(hex(1))));
assert.throws(()=>ctx.cordU64(Number(maxEpoch)),/unsafe/);assert.throws(()=>ctx.cordU64('18446744073709551616'),/invalid/);
// Storage preserves raw numbers; JSON itself validates tokens rather than interpolating strings.
const parsed=ctx.cordJsonParse('{"opaque":18446744073709551615,"text":"18446744073709551615"}');
assert.equal(ctx.cordCanonical(parsed),'{"opaque":18446744073709551615,"text":"18446744073709551615"}');
assert.throws(()=>ctx.cordJsonParse('{"opaque":18446744073709551615x}'));
reset();relay=[make(0,{frags:1,entries:[entry(1)],tombstones:[]})];await ctx.persistArmadaMembership(api,room(4));
// Consecutive same-second writes must increase each coordinate's timestamp.
const firstAt=published[0].created_at;await ctx.persistArmadaMembership(api,room(5));assert(published.at(-1).created_at>firstAt);
// List-level unknowns union by lowest fragment index and move intact to fragment zero.
reset();relay=[make(0,{frags:2,shared:'zero wins',entries:[entry(1)],tombstones:[]}),make(1,{frags:2,shared:'one loses',onlyOne:{opaque:'keep'},entries:[entry(2)],tombstones:[]})];
await ctx.persistArmadaMembership(api,room(4));
const zero=docOf(relay.find(e=>e.tags[0][1]==='0')),one=docOf(relay.find(e=>e.tags[0][1]==='1'));
assert.equal(zero.shared,'zero wins');assert.deepEqual(zero.onlyOne,{opaque:'keep'});assert(!('shared'in one));assert(!('onlyOne'in one));
// Opaque JSON names that coincide with Object.prototype remain ordinary data.
reset();const special=JSON.parse('{"__proto__":{"secret":"keep"},"constructor":"also keep"}');
relay=[make(0,{...special,frags:1,entries:[entry(1,{...special,current:{...material(1),...special}})],tombstones:[{community_id:b64(hex(3)),removed_at:900,...special}]})];
await ctx.persistArmadaMembership(api,room(4));
const specialDoc=docOf(published[0]);assert.deepEqual(specialDoc.__proto__,{secret:'keep'});assert.equal(specialDoc.constructor,'also keep');assert.deepEqual(specialDoc.entries[0].__proto__,{secret:'keep'});assert.equal(specialDoc.tombstones[0].constructor,'also keep');
// Missing fragment zero does not prevent leaving an entry actually held in fragment one.
reset();relay=[make(1,{frags:2,entries:[entry(2)],tombstones:[]})];
await ctx.leaveArmadaMembership(api,room(2));assert.equal(published.length,1);assert.equal(published[0].tags[0][1],'1');doc=docOf(published[0]);assert.equal(doc.frags,2);assert.equal(doc.entries.length,0);assert.equal(doc.tombstones[0].community_id,b64(hex(2)));
// Actual NIP-44 ciphertext + Schnorr signatures, enough opaque entries to force several fragments.
reset();relay=[make(0,{frags:1,entries:[],tombstones:[],opaqueRoot:'stay'})];
await ctx.persistArmadaMemberships(api,copy(Array.from({length:90},(_,i)=>room(i+10,{opaque:'Ω'.repeat(600)}))));
assert(published.length>2,'large list must fragment');const docs=published.map(docOf),count=docs[0].frags;
assert.equal(docs.reduce((n,d)=>n+d.entries.length,0),90);assert(docs.every(d=>d.frags===count));assert(published.every(e=>Buffer.byteLength(JSON.stringify(e))<=65536));assert.equal(published[0].tags[0][1],String(count-1),'append fragments publish before old coordinates');
assert.equal(docs.flatMap(d=>d.entries).filter(e=>e.current.opaque==='Ω'.repeat(600)).length,90,'unknown bytes cannot be shed to fit');
// A partial list may repair an oversized coordinate without seeing/repacking its peers.
reset();relay=[make(1,{frags:2,entries:[entry(1,{current:material(1,{opaque:'x'.repeat(46000)})}),entry(2,{current:material(2,{opaque:'y'.repeat(14000)})})],tombstones:[]})];
const oversizedBytes=Buffer.byteLength(JSON.stringify(relay[0]));assert(oversizedBytes>65536);
await ctx.leaveArmadaMembership(api,room(2));
assert.equal(published.length,1);assert.equal(published[0].tags[0][1],'1');
const shrunkBytes=Buffer.byteLength(JSON.stringify(published[0]));assert(shrunkBytes>65536);assert(shrunkBytes<oversizedBytes);
assert.equal(docOf(published[0]).frags,2);assert.equal(docOf(published[0]).entries[0].current.opaque,'x'.repeat(46000));
// Less plaintext is insufficient when NIP-44 padding leaves the encoded size unchanged.
reset();relay=[make(1,{frags:2,entries:[entry(1,{current:material(1,{opaque:'x'.repeat(46000)})}),entry(2)],tombstones:[]})];
await assert.rejects(ctx.leaveArmadaMembership(api,room(2)),/size limit|all membership fragments/);assert.equal(published.length,0);
// A stale relay cannot make two queued local updates replace each other.
reset();stale=true;await Promise.all([ctx.persistArmadaMembership(api,room(6)),ctx.persistArmadaMembership(api,room(7))]);assert.equal(docOf(published.at(-1)).entries.length,2);
// An incomplete list cannot repack; the attempted write must publish nothing.
reset();relay=[make(0,{frags:2,entries:[],tombstones:[]})];await assert.rejects(ctx.persistArmadaMemberships(api,copy(Array.from({length:40},(_,i)=>room(i+50,{opaque:'x'.repeat(2000)})))),/all membership fragments/);assert.equal(published.length,0);
// A huge individual unknown field has no safe split; preserve it and refuse before publication.
reset();await assert.rejects(ctx.persistArmadaMembership(api,room(9,{opaque:'x'.repeat(60000)})),/size limit/);assert.equal(published.length,0);
// Newest fragment is unreadable: never overwrite it from the older/local view.
reset();relay=[{...make(0,{frags:1,entries:[entry(1)],tombstones:[]}),content:'unreadable'}];await assert.rejects(ctx.persistArmadaMembership(api,room(8)));assert.equal(published.length,0);
// A changed account while signing must never publish the former account's keys.
reset();signHook=async()=>{viewer=hex(900);};await assert.rejects(ctx.persistArmadaMembership(api,room(8)),/account changed/);assert.equal(published.length,0);
// A newer coordinate observed while signing cannot be clobbered by a stale pending template.
reset();relay=[make(0,{frags:1,entries:[entry(1)],tombstones:[]})];signHook=async()=>{relay=[make(0,{frags:1,entries:[entry(1),entry(2)],tombstones:[]},200)];signHook=null;};await assert.rejects(ctx.persistArmadaMembership(api,room(8)),/changed while signing/);assert.equal(published.length,0);
console.log('CORD membership fragments: actual ciphertext sizes, canonical fields, partial writes, tombstones, concurrent writes and account races passed');
