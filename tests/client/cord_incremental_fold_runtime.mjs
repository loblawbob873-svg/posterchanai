import fs from 'node:fs';import vm from 'node:vm';import assert from 'node:assert/strict';
globalThis.window=globalThis;
globalThis.chatDecrypts=0;
const load=(path,name)=>vm.runInThisContext(fs.readFileSync(name==='PosterCordReader'&&process.env.PC_CORD_READER_SOURCE?process.env.PC_CORD_READER_SOURCE:new URL(path,import.meta.url),'utf8').replace('const ev = openWrap(wrap2, stream.group);','globalThis.chatDecrypts++;const ev = openWrap(wrap2, stream.group);')+`\nglobalThis.${name}=${name};`);
load('../../static/vendor/nostr/nostr.bundle.js','NostrTools');load('../../static/js/client/cord-protocol.js','PosterCord');load('../../static/js/client/cord-reader.js','PosterCordReader');
const secret=Uint8Array.from({length:32},(_,i)=>i+1),owner=NostrTools.getPublicKey(secret),signEvent=async e=>NostrTools.finalizeEvent(e,secret);
const otherSecret=Uint8Array.from({length:32},(_,i)=>i+2),other=NostrTools.getPublicKey(otherSecret),otherSign=async e=>NostrTools.finalizeEvent(e,otherSecret);
const made=await PosterCord.createCommunity({name:'Incremental fixture',owner,relays:['wss://relay.example'],base:'https://example.test',signEvent});
const bundle={community_id:made.communityId,owner,owner_salt:made.secrets.ownerSalt,community_root:made.secrets.root,root_epoch:0,channels:[],relays:['wss://relay.example']},controls=made.events.slice(0,2),channel=PosterCordReader.inspectControl(bundle,controls).channels[0],room={communityId:made.communityId};
const create=(text,tags=[],kind=9,pk=owner,sign=signEvent)=>PosterCordReader.createChatWrap(bundle,controls,channel.id,text,pk,sign,tags,kind);
const message=await create('same body'),sameBody=await create('same body',[],9,other,otherSign),reaction=await create('+',[['e',message.rumorId]],7),secondReaction=await create('❤️',[['e',message.rumorId]],7,other,otherSign);
let rows=[],envelopes=[];
const code=fs.readFileSync(process.env.PC_CONCORD_SOURCE||new URL('../../static/js/client/concord.js',import.meta.url),'utf8'),start=code.indexOf('  function mergeCordTimeline('),end=code.indexOf('  async function refreshActiveChannel(',start);
const context={console,Map,Set,Promise,deliveryOwner:()=>owner,roomIdentity:r=>r.communityId,saved:()=>[room],envelopeCacheKey:()=> 'fixture',cachedEnvelopes:async()=>envelopes,
 readChat:(_p,reader,b,c,_r,ch,w)=>reader.inspectChat(b,c,ch.id,w),testMessages:()=>structuredClone(rows),messageId:m=>m.id,
 mergeRelayMessages:(prior,incoming)=>[...new Map([...prior,...incoming].map(m=>[m.id,m])).values()],
 saveTestMessages:(_id,next)=>{rows=structuredClone(next);},notifyMentions:()=>{},document:{body:{classList:{contains:()=>false}}}};
vm.createContext(context);vm.runInContext(code.slice(code.indexOf('  function uniqueMessages('),code.indexOf('  const remoteMessages='))+code.slice(code.indexOf('  function pendingEchoMatch('),code.indexOf('  function channelStoreId('))+code.slice(start,end),context);
const p={viewer:()=>({pubkey:owner})};
const absorb=async wraps=>{envelopes=[...new Map([...envelopes,...wraps].map(w=>[w.id,w])).values()];await context.absorbChatWraps(p,PosterCordReader,bundle,controls,room,channel,wraps,'fixture');};
await absorb([message.wrap,sameBody.wrap,reaction.wrap]);assert.equal(rows.length,2,'same text with distinct authenticated IDs remains two messages');
const decryptsBeforeEcho=chatDecrypts;await absorb([message.wrap]);assert.equal(chatDecrypts,decryptsBeforeEcho,'cached echo does not repeat cryptographic opening');assert.deepEqual(rows.find(m=>m.id===message.rumorId).reactions,{'👍':[owner]},'message echo must not erase existing reaction');
await absorb([secondReaction.wrap]);assert.deepEqual(rows.find(m=>m.id===message.rumorId).reactions,{'👍':[owner],'❤️':[other]},'new reaction retains prior reaction group');
const forged=await create('',[['e',reaction.rumorId],['k','7']],5,other,otherSign);await absorb([forged.wrap]);assert.deepEqual(rows.find(m=>m.id===message.rumorId).reactions['👍'],[owner],'another author cannot delete a reaction');
await absorb([message.wrap]);assert.deepEqual(rows.find(m=>m.id===message.rumorId).reactions['👍'],[owner],'wrong-author tombstone cannot poison later folds');
const unpoisoned=await PosterCordReader.inspectChat(bundle,controls,channel.id,[message.wrap,reaction.wrap,forged.wrap]);assert.deepEqual(new Map(new Map(unpoisoned.reactions).get(message.rumorId)).get('👍'),[owner],'reader itself must reject wrong-author reaction tombstone');
const remove=await create('',[['e',reaction.rumorId],['k','7']],5);await absorb([remove.wrap]);assert.deepEqual(rows.find(m=>m.id===message.rumorId).reactions,{'❤️':[other]},'author deletion removes only its reaction');
const wrongDelete=await create('',[['e',message.rumorId]],5,other,otherSign);await absorb([wrongDelete.wrap]);assert(rows.some(m=>m.id===message.rumorId),'non-author cannot delete message');
const removeMessage=await create('',[['e',message.rumorId]],5);await absorb([removeMessage.wrap]);assert.deepEqual(rows.map(m=>m.id),[sameBody.rumorId],'delete-only live batch removes authenticated prior target only');
await absorb([message.wrap]);assert.deepEqual(rows.map(m=>m.id),[sameBody.rumorId],'replayed message cannot resurrect cached authenticated deletion');
console.log('actual encrypted incremental reaction/deletion fold passed');

// Overlapping crypto completion cannot publish an older reaction snapshot after a newer batch.
const later=await create('later');await absorb([later.wrap]);
const lateReact=await create('🔥',[['e',later.rumorId]],7);
const realRead=context.readChat;let release,enteredResolve;const entered=new Promise(r=>enteredResolve=r);let held=true;
context.readChat=async(...args)=>{const result=await realRead(...args);if(held){held=false;enteredResolve();await new Promise(r=>release=r);}return result;};
const first=absorb([later.wrap]);await entered;const second=absorb([lateReact.wrap]);release();await Promise.all([first,second]);
assert.deepEqual(rows.find(m=>m.id===later.rumorId).reactions,{'🔥':[owner]});
assert.equal(context.absorbChatWraps.pending.size,0,'finished fold queues release their store references');

// An unavailable encrypted disk cache still retains context for this running owner's stream.
context.cachedEnvelopes=async()=>[];rows=[];context.absorbChatWraps.history=new Map();context.mergeCordTimeline.deleted=new Map();
await absorb([message.wrap,reaction.wrap]);await absorb([message.wrap]);
assert.deepEqual(rows.find(m=>m.id===message.rumorId).reactions,{'👍':[owner]});
await absorb([remove.wrap]);assert.deepEqual(rows.find(m=>m.id===message.rumorId).reactions,{});
await absorb([removeMessage.wrap]);assert(!rows.some(m=>m.id===message.rumorId));
await absorb([message.wrap]);assert(!rows.some(m=>m.id===message.rumorId),'memory envelope context prevents stale resurrection without disk');
assert([...context.absorbChatWraps.history.values()].every(item=>item.ev.kind===1059&&!item.ev.content.includes('same body')),'memory ledger retains encrypted envelopes only');

// Simulate eviction of all envelope context while retained UI state remains authoritative.
context.absorbChatWraps.history=new Map();rows=[];await absorb([later.wrap,lateReact.wrap]);
context.absorbChatWraps.history=new Map();await absorb([later.wrap]);
assert.deepEqual(rows.find(m=>m.id===later.rumorId).reactions,{'🔥':[owner]},'context loss does not clear prior reaction');
const lateDelete=await create('',[['e',later.rumorId]],5);await absorb([lateDelete.wrap]);
context.absorbChatWraps.history=new Map();await absorb([later.wrap]);assert(!rows.some(m=>m.id===later.rumorId),'validated tombstone survives envelope eviction');
// Both hydration and live delivery call this same production merge. Older hydration cannot
// clear a reaction or replace a newer edit, even when its crypto completed last.
const oldSnapshot=await PosterCordReader.inspectChat(bundle,controls,channel.id,[message.wrap]);
const actualEdit=await create('new edited text',[['e',message.rumorId]],3302);
const liveSnapshot=await PosterCordReader.inspectChat(bundle,controls,channel.id,[message.wrap,secondReaction.wrap,actualEdit.wrap]);
context.mergeCordTimeline.deleted=new Map();
const liveRows=context.mergeCordTimeline([],liveSnapshot,p,'hydrate');
const afterHydration=context.mergeCordTimeline(liveRows,oldSnapshot,p,'hydrate');
assert.equal(afterHydration[0].text,'new edited text');assert.deepEqual(JSON.parse(JSON.stringify(afterHydration[0].reactions)),{'❤️':[other]});
assert(code.includes('mergeCordTimeline(testMessages(storeId),opened,p,'),'hydration uses shared production fold');

// Separate encrypted receipts citing the same validated proof remain one payment after merging.
const txid='e'.repeat(64),zapTags=[['e',sameBody.rumorId],['i','bitcoin:tx:'+txid],['amount','25']];
const zapA=await create('first receipt',zapTags,8333),zapB=await create('second receipt',zapTags,8333,other,otherSign);
const pageA=await PosterCordReader.inspectChat(bundle,controls,channel.id,[sameBody.wrap,zapA.wrap]);
const pageB=await PosterCordReader.inspectChat(bundle,controls,channel.id,[sameBody.wrap,zapB.wrap]);
assert.equal(pageA.zaps[0][1][0].paymentId,txid);
const zapRows=context.mergeCordTimeline(context.mergeCordTimeline([],pageA,p,'zaps'),pageB,p,'zaps');
assert.equal(zapRows[0].zaps.length,1,'one payment proof cannot count twice across pages');
