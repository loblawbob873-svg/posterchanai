import assert from 'node:assert/strict';
import vm from 'node:vm';
import {hkdfSync,webcrypto} from 'node:crypto';
import {makeRealm,loadInto,into} from '../../botframework/cord_realm.mjs';
const root=new URL('../../',import.meta.url),ctx=makeRealm(),copy=into(ctx);ctx.crypto.subtle=webcrypto.subtle;
for(const f of ['static/vendor/nostr/nostr.bundle.js','static/js/client/cord-reader.js','static/js/client/cord-protocol.js','static/js/client/cord-invite-links.js'])loadInto(ctx,new URL(f,root));
const NT=ctx.NostrTools,P=ctx.PosterCord,R=ctx.PosterCordReader,A=ctx.PCCordInviteLinks,sk=NT.generateSecretKey(),pk=NT.getPublicKey(sk);
const bytes=b=>vm.runInContext(`Uint8Array.from(${JSON.stringify([...b])})`,ctx);
const sign=t=>NT.finalizeEvent(copy(t),sk),key=NT.nip44.getConversationKey(sk,pk),storage=new Map();ctx.localStorage={getItem:k=>storage.get(k),setItem:(k,v)=>storage.set(k,v)};
const made=await P.createCommunity({...copy({owner:pk,name:'Links',relays:['wss://relay.example'],base:'https://example.test'}),signEvent:sign});
const bundle=P.openInvite(made.url,copy(made.events)).bundle,creator=copy({...bundle,control_root:made.secrets.controlRoot});
let current=true,events=[],published=[],complete=true;
const context={pubkey:pk,isCurrent:()=>current,verify:async es=>es.filter(e=>NT.verifyEvent(copy(e))),decrypt:async(_,text)=>NT.nip44.decrypt(text,key),encrypt:async(_,text)=>NT.nip44.encrypt(text,key),sign:async t=>sign(t),query:async()=>({events,complete}),publish:async e=>{published.push(e);return true;}};
const minted=await A.create(creator,context,{base:'https://example.test'}),entry=minted.entry;
assert(NT.verifyEvent(copy(minted.event)));
const derived=bytes(Buffer.from(hkdfSync('sha256',Buffer.from(entry.token,'hex'),Buffer.alloc(0),Buffer.concat([Buffer.from('concord/invite-key'),Buffer.from([0]),Buffer.alloc(32)]),32)));
const plaintext=NT.nip44.decrypt(minted.event.content,derived);assert.equal(JSON.parse(plaintext).community_id,bundle.community_id);assert(!plaintext.includes('control_root'));
assert.equal(P.openInvite(entry.url,copy([minted.event])).bundle.community_id,bundle.community_id);
assert.equal(A.details(entry).pubkey,minted.event.pubkey);
assert.throws(()=>A.details({...entry,token:'00'.repeat(16)}));
await A.remember(entry,context);assert.equal(published.length,1);assert.equal(published[0].kind,13303);assert(!JSON.stringify([...storage]).includes(entry.signer_sk));
assert.equal(JSON.parse(NT.nip44.decrypt(published[0].content,key)).entries[0].url,entry.url);
// A relay that returns stale list data must not erase a locally acknowledged tombstone.
await A.forget(entry,context);await assert.rejects(()=>A.remember(entry,context),/retired/);
const unknown=copy({entries:[entry],tombstones:[],future:{z:2},__proto__:{}});
const merged=A.mergeLists(copy([unknown,{entries:[],tombstones:[{token:entry.token,community_id:entry.community_id}],future:{a:1}}]));
assert.equal(merged.entries.length,0);assert.equal(merged.future.a,1);assert.equal(merged.future.z,2);
const proto=A.mergeLists(vm.runInContext(`JSON.parse('[{"entries":[],"tombstones":[],"__proto__":{"polluted":true},"constructor":{"opaque":1}}]')`,ctx));assert.equal(proto.__proto__.polluted,true);assert.equal({}.polluted,undefined);assert.equal(proto.constructor.opaque,1);
complete=false;const before=published.length;await assert.rejects(()=>A.forget(entry,context),/incomplete/);assert.equal(published.length,before);complete=true;
await assert.rejects(()=>A.remember(entry,{...context,query:async()=>{current=false;return {events:[],complete:true};}}),/account/);current=true;
const refreshed=await A.refreshEvent(entry,copy({...bundle,name:'New name'}),copy([minted.event]),context);assert(refreshed.created_at>minted.event.created_at);assert.equal(P.openInvite(entry.url,copy([refreshed])).bundle.name,'New name');
const retired=A.revokeEvent(entry,copy([refreshed]),context);assert(NT.verifyEvent(copy(retired)));await assert.rejects(()=>A.refreshEvent(entry,bundle,copy([retired]),context),/retired/);
events=[retired];assert((await A.linkState(entry,context)).retired);
const controls=made.events.filter(e=>e.kind===1059),registry=await R.createInviteRegistryWrap(creator,copy(controls),copy([minted.event.pubkey]),pk,sign);
const folded=R.inspectControl(bundle,copy([...controls,registry.wrap]));assert.equal(folded.liveInviteLinks[0],minted.event.pubkey);assert.equal(folded.registriesByCreator[pk][0],minted.event.pubkey);assert(folded.inviteCreators.includes(pk));
const empty=await R.createInviteRegistryWrap(creator,copy([...controls,registry.wrap]),copy([]),pk,sign);assert.equal(R.inspectControl(bundle,copy([...controls,registry.wrap,empty.wrap])).liveInviteLinks.length,0);
await assert.rejects(()=>R.createInviteRegistryWrap(creator,copy(controls),copy([entry.url]),pk,sign),/invalid/);
const other=NT.getPublicKey(NT.generateSecretKey());await assert.rejects(()=>R.createInviteRegistryWrap(creator,copy(controls),copy([]),other,sign),/authorized/);
console.log('creator invite links passed');
// Exercise the shipped adoption hook with real signed lists, registry folds and links.
const fs=await import('node:fs');const source=fs.readFileSync(new URL('static/js/client/concord.js',root),'utf8');
vm.runInContext(source.slice(source.indexOf('  async function ownedInviteContext('),source.indexOf('  async function saveCommunitySettings(')),ctx);
const second=await A.create(creator,context,{base:'https://example.test'}),secondPk=A.details(second.entry).pubkey;
const liveRegistry=await R.createInviteRegistryWrap(creator,copy(controls),copy([secondPk]),pk,sign);
const room=copy({communityId:bundle.community_id,cord:{bundle:{...creator,channels:[{id:'aa'.repeat(32),key:'bb'.repeat(32),epoch:0}]}}});let rooms=[room],wire=[],incomplete=false,revoked=false;
ctx.saved=()=>rooms;ctx.roomIdentity=r=>r.communityId;ctx.roomRelays=b=>b.relays;ctx.CORD_RELAYS=copy([]);ctx.roomControls=new Map([[room.communityId,controls]]);ctx.cordPlaneContext=()=>({current:()=>current});ctx.mergeEnvelopes=(...sets)=>sets.flat();
ctx.cordQuery=async(_p,relays,_filters,options)=>{options.report.ok=relays;return copy([liveRegistry.wrap]);};
const listEvent=sign({kind:13303,created_at:Math.floor(Date.now()/1000)+10,tags:[],content:NT.nip44.encrypt(JSON.stringify({entries:[second.entry],tombstones:[]}),key)});
const host={cordDirectContext:()=>context,cordInviteLinksModule:async()=>A,relayQueryFrom:async(relays,filters,options)=>{options.report.ok=incomplete?[]:relays;return filters[0].kinds[0]===13303?[listEvent]:[revoked?A.revokeEvent(second.entry,copy([second.event]),context):second.event];},relayPublishRoom:async(_relays,e)=>{wire.push(e);return {ok:true};}};
assert.equal(await ctx.refreshOwnedInviteLinks(host,room),1);assert.equal(P.openInvite(second.entry.url,copy(wire)).bundle.channels.length,0,'public link must not acquire privately granted channel keys');
wire=[];revoked=true;const retiredRegistry=await R.createInviteRegistryWrap(creator,copy([...controls,liveRegistry.wrap]),copy([]),pk,sign);ctx.cordQuery=async(_p,relays,_f,options)=>{options.report.ok=relays;return copy([liveRegistry.wrap,retiredRegistry.wrap]);};assert.equal(await ctx.refreshOwnedInviteLinks(host,room),0);assert.equal(wire.length,1);assert.equal(wire[0].kind,13303,'signed tombstone reconciles bookkeeping without reviving the link');wire=[];
revoked=false;incomplete=true;await assert.rejects(()=>ctx.refreshOwnedInviteLinks(host,room),/incomplete/);assert.equal(wire.length,0);
incomplete=false;ctx.cordQuery=async(_p,relays,_f,options)=>{options.report.failed=relays;return [];};await assert.rejects(()=>ctx.refreshOwnedInviteLinks(host,room),/registry sync is incomplete/);assert.equal(wire.length,0);
console.log('creator refresh hook passed');
ctx.cordPlaneAuth=()=>({});ctx.PCConcord={};host.viewer=()=>({pubkey:pk});
let controlEvents=copy([...controls,liveRegistry.wrap]);ctx.roomControls.set(room.communityId,controlEvents);
ctx.cordQuery=async(_p,relays,_f,options)=>{options.report.ok=relays;return controlEvents;};
ctx.cordPlaneContext=()=>({current:()=>current});
let calls=[];host.relayPublishRoom=async(_relays,e)=>{calls.push(e);return {ok:true};};
const createdLink=await ctx.changeOwnedInviteLink(host,room);assert.equal(createdLink.community_id,bundle.community_id);assert.deepEqual(calls.map(e=>e.kind),[13303,33301,1059]);
const newRegistry=calls[2];controlEvents=copy([...controlEvents,newRegistry]);assert(R.inspectControl(bundle,controlEvents).liveInviteLinks.includes(A.details(createdLink).pubkey));
assert.equal(P.openInvite(createdLink.url,copy([calls[1]])).bundle.channels.length,0);
// Ordinary retirement writes the tombstone before removing the registry, then records terminal bookkeeping.
calls=[];await ctx.changeOwnedInviteLink(host,room,createdLink);assert.deepEqual(calls.map(e=>e.kind),[33301,1059,13303]);assert(calls[0].tags.some(t=>t[0]==='vsk'&&t[1]==='9'));
controlEvents=copy([...controlEvents,calls[1]]);
// Final-link retirement must review recipients and pass the registry update into authenticated refounding.
ctx.PCConcord.refoundingBeforeEvents=true;ctx.PCConcord.reviewRefoundingRecipients=async()=>({recipients:[]});ctx.PCConcord.refoundRoom=async(_p,_room,options)=>{assert.equal(calls.length,0);assert.equal(options.controlUpdates.length,1);assert.equal(options.beforeEvents.length,1);await host.relayPublishRoom([],options.beforeEvents[0]);assert.equal(calls.length,1);calls.push({kind:'refound'});};
calls=[];await ctx.changeOwnedInviteLink(host,room,second.entry);assert.deepEqual(calls.map(e=>e.kind),[33301,'refound',13303]);
console.log('creator link lifecycle passed');
// Canceling recipient review never publishes a tombstone or claims retirement.
ctx.PCConcord.reviewRefoundingRecipients=async()=>null;calls=[];assert.equal(await ctx.changeOwnedInviteLink(host,room,second.entry),null);assert.equal(calls.length,0);
// Backup refusal prevents publication of any usable link or registry change.
host.relayPublishRoom=async(_relays,e)=>{calls.push(e);return {ok:false};};calls=[];await assert.rejects(()=>ctx.changeOwnedInviteLink(host,room),/No relay accepted/);assert.deepEqual(calls.map(e=>e.kind),[13303]);
// Rejecting the bare link must not advance the public registry.
host.relayPublishRoom=async(_relays,e)=>{calls.push(e);return {ok:e.kind!==33301};};calls=[];await assert.rejects(()=>ctx.changeOwnedInviteLink(host,room),/No relay accepted/);assert.deepEqual(calls.map(e=>e.kind),[13303,33301]);
// Explicit recovery keeps the existing URL and remembers its signing key encrypted.
host.relayPublishRoom=async(_relays,e)=>{calls.push(e);return {ok:true};};calls=[];
const original=copy({token:made.secrets.token,signer_sk:made.secrets.linkSignerSk,community_id:bundle.community_id,url:made.url});
assert.equal((await ctx.changeOwnedInviteLink(host,room,original,{adopt:true})).url,made.url);assert.deepEqual(calls.map(e=>e.kind),[13303,33301,1059]);
assert.equal(P.openInvite(made.url,copy([calls[1]])).bundle.community_id,bundle.community_id);
// Signing-account changes during list synchronization stop before all writes.
host.relayQueryFrom=async()=>{current=false;return [];};calls=[];await assert.rejects(()=>ctx.changeOwnedInviteLink(host,room),/changed/);assert.equal(calls.length,0);current=true;
console.log('creator cancellation and rejection passed');
// A second device edit during the signer prompt is observed before a replaceable overwrite.
events=[];const candidate=await A.create(bundle,context);published=[];
await assert.rejects(()=>A.remember(candidate.entry,{...context,sign:async template=>{events=[sign({kind:13303,created_at:template.created_at+10,tags:[],content:NT.nip44.encrypt(JSON.stringify({entries:[],tombstones:[],otherDevice:'preserve'}),key)})];return sign(template);}}),/changed while signing/);assert.equal(published.length,0);events=[];
await assert.rejects(()=>A.remember(candidate.entry,{...context,publish:async()=>undefined}),/not accepted/);
await assert.rejects(()=>A.remember(candidate.entry,{...context,publish:async()=>({ok:false})}),/not accepted/);
// A control-generation change while reading a link blocks release of fresh keys.
let generation=0;ctx.cordPlaneContext=()=>{const own=generation;return {current:()=>current&&generation===own};};
ctx.cordQuery=async(_p,relays,_f,options)=>{options.report.ok=relays;return copy([liveRegistry.wrap]);};
const freshForRace=await A.create(bundle,context);const raceRegistry=await R.createInviteRegistryWrap(creator,copy(controls),copy([A.details(freshForRace.entry).pubkey]),pk,sign);ctx.cordQuery=async(_p,relays,_f,options)=>{options.report.ok=relays;return copy([raceRegistry.wrap]);};
const raceList=sign({kind:13303,created_at:Math.floor(Date.now()/1000)+100,tags:[],content:NT.nip44.encrypt(JSON.stringify({entries:[freshForRace.entry],tombstones:[]}),key)});
const racingHost={...host,relayQueryFrom:async(relays,filters,options)=>{options.report.ok=relays;if(filters[0].kinds[0]===13303)return [raceList];generation++;return [freshForRace.event];}};
calls=[];await assert.rejects(()=>ctx.refreshOwnedInviteLinks(racingHost,room),/changed/);assert.equal(calls.length,0);
console.log('creator concurrent edit and permission guards passed');
// The whole community lifecycle is serialized, so concurrent clicks cannot fork one registry head.
ctx.cordPlaneContext=()=>({current:()=>current});ctx.cordQuery=async(_p,relays,_f,options)=>{options.report.ok=relays;return copy(controls);};ctx.roomControls.set(room.communityId,copy(controls));
const concurrentHost={...host,relayQueryFrom:async(relays,filters,options)=>{options.report.ok=relays;return filters[0].kinds[0]===13303?[listEvent]:[];}};
calls=[];const twins=await Promise.all([ctx.changeOwnedInviteLink(concurrentHost,room),ctx.changeOwnedInviteLink(concurrentHost,room)]);
const paired=R.inspectControl(bundle,copy([...controls,...calls.filter(e=>e.kind===1059)]));for(const twin of twins)assert(paired.liveInviteLinks.includes(A.details(twin).pubkey));assert.equal(paired.liveInviteLinks.length,2);
await ctx.changeOwnedInviteLink(concurrentHost,room,twins[0]);
console.log('concurrent creator mint serialization passed');
// Use the real encrypted membership writer for owner-key durability.
ctx.membershipEvents=async()=>copy([]);ctx.forgetLeftCommunity=()=>{};ctx.rememberLeftCommunity=()=>{};
vm.runInContext(source.slice(source.indexOf('  function cordListHex('),source.indexOf('  function cordListMaterial('))+source.slice(source.indexOf('  // CORD-02 §8: canonical'),source.indexOf('  // Only encrypted, signed envelopes cross')),ctx);
// Ordinary new-community creation must register its original URL immediately.
vm.runInContext(source.slice(source.indexOf('  async function mintPublicRoom('),source.indexOf('  async function activateJoinedRoom(')),ctx);
ctx.inviteParts=url=>({naddr:url.split('/invite/')[1].split('#')[0]});ctx.DISCOVER_RELAYS=copy(['wss://discover.fixture']);
const mintHost={...concurrentHost,relayUrls:()=>['wss://mint.fixture'],publish:async()=>({ev:{kind:1}}),relayPublishTo:async(_relays,e)=>{calls.push(e);return true;}};
calls=[];const newRoom=await ctx.mintPublicRoom(mintHost,'Ordinary creation','');assert.deepEqual(calls.map(e=>e.kind),[33302,13303,1059,1059,1059,33301,1]);
assert.equal(R.inspectControl(newRoom.cord.bundle,newRoom.cord.events).liveInviteLinks[0],P.inviteDetails(newRoom.url).linkSigner);
const ownerSnapshot=JSON.parse(NT.nip44.decrypt(calls[0].content,key)).entries.find(e=>e.community_id===Buffer.from(newRoom.communityId,'hex').toString('base64url')).current;assert.equal(ownerSnapshot.control_root,Buffer.from(newRoom.cord.bundle.control_root,'hex').toString('base64url'));
assert(JSON.parse(NT.nip44.decrypt(calls[1].content,key)).entries.some(e=>e.url===newRoom.url));
calls=[];await assert.rejects(()=>ctx.mintPublicRoom({...mintHost,relayPublishRoom:async(_relays,e)=>{calls.push(e);return {ok:false};}},'Rejected backup',''),/No relay accepted/);assert.deepEqual(calls.map(e=>e.kind),[33302,13303]);
console.log('ordinary community creation tracks original link passed');

// Captured signer cancellation applies to the final discovery announcement too.
calls=[];await assert.rejects(()=>ctx.mintPublicRoom({...mintHost,cordDirectContext:()=>({...context,sign:async t=>{const event=sign(t);if(t.kind===1)current=false;return event;}})},'Signer switch',''),/account changed/);assert(!calls.some(e=>e.kind===1));current=true;
calls=[];await assert.rejects(()=>ctx.mintPublicRoom({...mintHost,relayPublishRoom:async(_relays,e)=>{calls.push(e);if(e.kind===1)current=false;return {ok:true};}},'Discovery switch',''),/account changed/);assert.equal(calls.filter(e=>e.kind===1).length,1);current=true;
console.log('creation announcement account guards passed');

const vault=JSON.parse(NT.nip44.decrypt((await (async()=>{calls=[];await ctx.mintPublicRoom(mintHost,'Owner backup','');return calls.find(e=>e.kind===33302);})()).content,key));
assert(vault.entries.some(e=>e.current.control_root),'owner control root missing from encrypted membership');
calls=[];await assert.rejects(()=>ctx.mintPublicRoom({...mintHost,relayPublishTo:async(_r,e)=>{calls.push(e);return false;}},'Denied owner backup',''),/membership relays rejected/);assert.deepEqual(calls.map(e=>e.kind),[33302]);
calls=[];await assert.rejects(()=>ctx.mintPublicRoom({...mintHost,relayPublishRoom:async(_r,e)=>{calls.push(e);if(e.kind===1059)throw Error('genesis disconnected');return {ok:true};}},'Late disconnect',''),/genesis disconnected/);assert.equal(calls[0].kind,33302);assert(JSON.parse(NT.nip44.decrypt(calls[0].content,key)).entries.some(e=>e.current.control_root));
console.log('owner control root durable before public genesis passed');
