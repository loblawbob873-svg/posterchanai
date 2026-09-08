import fs from 'node:fs';import vm from 'node:vm';import assert from 'node:assert/strict';
globalThis.window=globalThis;
for(const [path,name] of [['../../static/vendor/nostr/nostr.bundle.js','NostrTools'],['../../static/js/client/cord-protocol.js','PosterCord']])vm.runInThisContext(fs.readFileSync(new URL(path,import.meta.url),'utf8')+`\nglobalThis.${name}=${name};`);
let source=fs.readFileSync(new URL('../../static/js/client/cord-reader.js',import.meta.url),'utf8');
source=source.replace('return __toCommonJS(pc_cord_reader_exports);','globalThis.fixture={runtime,guestbookGroups,groupKeyCached,buildRumor,sealRumor,wrapSeal,foldGuestbookMembers,openGuestbook,guestbookCanKick,controlGroups,grantLocator,hex32,bytesToHex2,editionHash};return __toCommonJS(pc_cord_reader_exports);');
vm.runInThisContext(source+'\nglobalThis.reader=PosterCordReader;');
assert.equal(fixture.groupKeyCached('concord/guestbook',Uint8Array.from({length:32},(_,i)=>i),Uint8Array.from({length:32},(_,i)=>255-i),0n).pk,'ad09de582026fa7a052db18bb5827fa24c15e929d59aadcc91efb8508f5368ad','official Vector cross-implementation guestbook key');
const keys=[3,4,5].map(n=>new Uint8Array(32).fill(n)),[owner,member,other]=keys.map(k=>NostrTools.getPublicKey(k)),signEvent=async e=>NostrTools.finalizeEvent(e,keys[0]);
const made=await PosterCord.createCommunity({name:'Guestbook',owner,relays:['wss://example.test'],base:'https://example.test',signEvent});
const base={community_id:made.communityId,owner,owner_salt:made.secrets.ownerSalt,community_root:made.secrets.root,root_epoch:0,channels:[],relays:['wss://example.test'],name:'Guestbook'},now=1700001000000,t=now-10000;
async function wrap(bundle,{kind=3306,content='join',key=keys[1],ms=t,tags=[],sealKind=20013}={}){const group=fixture.guestbookGroups(bundle)[0],rumor=fixture.buildRumor({kind,content,pubkey:NostrTools.getPublicKey(key),ms,tags}),seal=await fixture.sealRumor(rumor,sealKind,group,{signEvent:async e=>NostrTools.finalizeEvent(e,key)});return fixture.wrapSeal(seal,group);}
const project=(bundle,wraps,observed=[])=>reader.inspectGuestbook(bundle,[],wraps,observed,now).members;
const join=await wrap(base),leave=await wrap(base,{content:'leave',ms:t+1});
assert.deepEqual(project(base,[join]),[owner,member].sort());
assert.deepEqual(project(base,[leave,join]),[owner]);
assert.deepEqual(project(base,[join,leave],[{pubkey:member,at:t}]),[owner],'old message cannot resurrect departed member');
assert(project(base,[join,leave],[{pubkey:member,at:t+2}]).includes(member),'new activity may rejoin');
assert.deepEqual(project(base,[await wrap(base,{sealKind:20014})]),[owner],'plaintext seal rejected');
assert.deepEqual(project(base,[{...join,sig:'0'.repeat(128)}]),[owner],'forged outer signature rejected');
assert.deepEqual(project(base,[await wrap(base,{ms:now+3600001})]),[owner],'future presence rejected');
const kick=await wrap(base,{kind:3309,content:'',key:keys[0],ms:t+1,tags:[['p',member]]});
assert.deepEqual(project(base,[join,kick]),[owner],'owner kick removes member');
assert(project(base,[join,await wrap(base,{kind:3309,content:'',key:keys[2],ms:t+1,tags:[['p',member]]})]).includes(member),'uncited stranger cannot kick');
assert(project(base,[join,await wrap(base,{kind:3309,content:'',key:keys[0],ms:t+1,tags:[['p',member],['p',other]]})]).includes(member),'duplicate kick target rejected');
const snapArgs={kind:3312,content:JSON.stringify([member]),key:keys[0],tags:[['snap','a'.repeat(64),'1','1']]};
assert.deepEqual(project(base,[await wrap(base,snapArgs)]),[owner],'epoch zero owner snapshot rejected');
const epoch1={...base,root_epoch:1,refounder:other};
assert(project(epoch1,[await wrap(epoch1,snapArgs)]).includes(member),'community-bound owner fallback for nonzero epoch');
assert.deepEqual(project(epoch1,[await wrap(epoch1,{...snapArgs,key:keys[2]})]),[owner],'raw invite refounder cannot authorize roster');
assert.deepEqual(project(epoch1,[await wrap(epoch1,{...snapArgs,tags:[...snapArgs.tags,...snapArgs.tags]})]),[owner],'duplicate snapshot tag rejected');
const fold=(events,extra={})=>fixture.foldGuestbookMembers(events,{owner,banned:new Set(),bannedAt:new Map(),grantees:[],observed:[],now,...extra});
const event={id:'b'.repeat(64),author:member,member,type:'join',at:t,epoch:0n};
assert.deepEqual(fold([event],{bannedAt:new Map([[member,t/1000]])}),[owner],'unban does not resurrect pre-ban join');
assert(fold([{...event,at:t+1}],{bannedAt:new Map([[member,t/1000]])}).includes(member));
const tie={...event,id:'a'.repeat(64),type:'leave'};assert.deepEqual(fold([event,tie]),fold([tie,event]));assert.deepEqual(fold([event,tie]),[owner],'same-time lower inner rumor ID wins');
console.log('actual encrypted guestbook, official key vector, signature/seal/authority/epoch/departure/ban/tie checks passed');

// Same inner event rewrapped on two held epochs must converge regardless of relay order.
const historical={...epoch1,held_roots:[{epoch:0,key:base.community_root}]},groups=fixture.guestbookGroups(historical),rumor=fixture.buildRumor({kind:3306,content:'join',pubkey:member,ms:t,tags:[]}),rewraps=[];
for(const group of groups){const seal=await fixture.sealRumor(rumor,20013,group,{signEvent:async e=>NostrTools.finalizeEvent(e,keys[1])});rewraps.push(await fixture.wrapSeal(seal,group));}
assert.equal(fixture.openGuestbook(historical,rewraps).events[0].epoch,1n);assert.equal(fixture.openGuestbook(historical,[...rewraps].reverse()).events[0].epoch,1n);
const mismatchedSeal=await fixture.sealRumor(rumor,20013,groups[0],{signEvent:async e=>NostrTools.finalizeEvent(e,keys[2])});assert.deepEqual(project(historical,[await fixture.wrapSeal(mismatchedSeal,groups[0])]),[owner],'seal signer must equal inner author');
assert.equal(fixture.guestbookCanKick({owner},{banned:new Set([member])},{author:member,member:other}),false,'banned admin rejected before surviving role grant authority');
const refoundEvents=[{...event,epoch:0n},{id:'f'.repeat(64),author:owner,type:'snapshot',members:[other],at:t+10,epoch:1n}];assert.deepEqual(fold(refoundEvents,{snapshotAuthority:owner,observed:[{pubkey:member,at:t}]}),[owner,other].sort(),'refounding excludes old presence omitted from new snapshot');assert(fold(refoundEvents,{snapshotAuthority:owner,observed:[{pubkey:member,at:t+11}]}).includes(member),'later actual activity re-enters');
console.log('cross-epoch rewrap convergence, sealed author binding, banned actor and refound activity cut passed');

const community=fixture.runtime(base),controlGroup=fixture.controlGroups(community)[0],roleId='e'.repeat(64),grantId=fixture.bytesToHex2(fixture.grantLocator(community.id,fixture.hex32(member)));
async function controlEdition(vsk,id,content){const rumor=fixture.buildRumor({kind:3308,content:JSON.stringify(content),pubkey:owner,ms:t,tags:[['vsk',vsk],['eid',id],['ev','1']]});const seal=await fixture.sealRumor(rumor,20014,controlGroup,{signEvent});return fixture.wrapSeal(seal,controlGroup);}
const grant={member,role_ids:[roleId]},roleWrap=await controlEdition('1',roleId,{role_id:roleId,name:'Moderator',permissions:'8',position:50,color:0}),grantWrap=await controlEdition('3',grantId,grant),controls=[...made.events.slice(0,2),roleWrap,grantWrap],grantHash=fixture.bytesToHex2(fixture.editionHash(fixture.hex32(grantId),1n,undefined,new TextEncoder().encode(JSON.stringify(grant)))),citation=['vac',grantId,'1',grantHash],otherJoin=await wrap(base,{key:keys[2]}),adminKick=await wrap(base,{kind:3309,content:'',key:keys[1],ms:t+1,tags:[['p',other],citation]});
assert(!reader.inspectGuestbook(base,controls,[otherJoin,adminKick],[],now).members.includes(other),'actual valid grant and citation permit lower-rank kick');
const banned=await reader.createBanWrap(base,controls,member,owner,signEvent);
assert(reader.inspectGuestbook(base,[...controls,banned.wrap],[otherJoin,adminKick],[],now).members.includes(other),'actual signed ban defeats admin kick despite surviving cited grant');
console.log('actual signed moderator grant/citation kick and signed ban authority rejection passed');
const uiSource=fs.readFileSync(new URL('../../static/js/client/concord.js',import.meta.url),'utf8'),uiStart=uiSource.indexOf('  var _partsCache='),uiEnd=uiSource.indexOf('  function mentionAliases(',uiStart),uiRoom={communityId:made.communityId,cord:{bundle:base}},uiCtx={window:{PosterCordReader:reader},roomControls:new Map([[made.communityId,[]]]),cordControlStamp:w=>[...new Set((w||[]).map(e=>e.id))].sort(),roomIdentity:r=>r.communityId,channelsOf:()=>[{name:'general'}],channelStoreId:()=>'',testMessages:()=>[{pubkey:member,at:t}],bundle:base,room:uiRoom,owner,join,leave};vm.createContext(uiCtx);vm.runInContext(uiSource.slice(uiStart,uiEnd),uiCtx);vm.runInContext("ownGuestbookViewer(owner);roomGuestbooks.set(owner+'\\n'+room.communityId,{owner,material:JSON.stringify(bundle),controlIds:'[]',wraps:[join,leave]})",uiCtx);assert(!Array.from(uiCtx.roomParticipants(uiRoom,owner)).includes(member),'actual member UI suppresses departed author despite old channel history');uiCtx.testMessages=()=>[{pubkey:member,at:t+2}];assert(Array.from(uiCtx.roomParticipants(uiRoom,owner)).includes(member),'actual member UI observes newer presence without reloading room');
const chunk1=await wrap(epoch1,{...snapArgs,tags:[['snap','c'.repeat(64),'1','2']]}),chunk2=await wrap(epoch1,{...snapArgs,content:JSON.stringify([other]),tags:[['snap','c'.repeat(64),'2','2']]});assert(project(epoch1,[chunk1]).includes(member),'upstream independent partial chunk seeds known member');assert.deepEqual(project(epoch1,[chunk1,chunk2]),project(epoch1,[chunk2,chunk1]),'out-of-order chunks converge');assert.equal(reader.inspectGuestbook(epoch1,[],[chunk1],[],now).complete,false,'partial snapshot never claims complete roster');
