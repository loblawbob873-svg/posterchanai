import fs from 'node:fs';import vm from 'node:vm';import assert from 'node:assert/strict';
globalThis.window=globalThis;
const load=(path,name)=>vm.runInThisContext(fs.readFileSync(new URL(path,import.meta.url),'utf8')+`\nglobalThis.${name}=${name};`);
load('../../static/vendor/nostr/nostr.bundle.js','NostrTools');load('../../static/js/client/cord-protocol.js','PosterCord');load('../../static/js/client/cord-reader.js','PosterCordReader');
const secret=Uint8Array.from({length:32},(_,i)=>i+1),owner=NostrTools.getPublicKey(secret),signEvent=async e=>NostrTools.finalizeEvent(e,secret);
const made=await PosterCord.createCommunity({name:'Synthetic membership fixture',owner,relays:['wss://relay.example'],base:'https://example.test',signEvent});
const bundle={community_id:made.communityId,owner,owner_salt:made.secrets.ownerSalt,community_root:made.secrets.root,root_epoch:0,channels:[],relays:['wss://relay.example'],name:'Fixture'};
const controls=made.events.slice(0,2);const control=PosterCordReader.inspectControl(bundle,controls),groups=[...control.controlPubkeys,...control.channels.flatMap(c=>c.streamPubkeys)];
assert.equal(groups.length,2,'real seed exposes a control group and a public chat group');
const code=fs.readFileSync(process.env.PC_CONCORD_SOURCE||new URL('../../static/js/client/concord.js',import.meta.url),'utf8');
const start=code.indexOf('  var _partsCache='),end=code.indexOf('  function mentionAliases(',start);
const viewer='a'.repeat(64),talker='b'.repeat(64);let messages=[];
const context={window:{PosterCordReader},roomControls:new Map([[made.communityId,controls]]),channelsOf:()=>[{name:'general'}],channelStoreId:()=> 'general',testMessages:()=>messages};vm.createContext(context);vm.runInContext(code.slice(start,end),context);
const room={communityId:made.communityId,cord:{bundle},members:[...groups]};
const people=()=>Array.from(context.roomParticipants(room,viewer));
assert.deepEqual(people().sort(),[viewer,owner].sort(),'real group transport keys are not members even if copied into explicit members');
messages=[{pubkey:talker},...groups.map(pubkey=>({pubkey}))];
assert.deepEqual(people().sort(),[viewer,owner,talker].sort(),'only owner/viewer/human authors remain call and mention candidates');
// The existing full-module suite exercises actual mention dispatch; this fixture proves its shared candidate boundary.
for(const group of groups)assert(!people().includes(group));
assert(code.includes('<b>Known members</b>'));
assert(code.includes('This is not a complete roster.'));
console.log('real CORD transport keys excluded; owner and human authors retained');

// Repeated paints reuse the same verified metadata; an equal-length replacement must revalidate.
let reads=0;context.window.PosterCordReader={inspectControl(...args){reads++;return PosterCordReader.inspectControl(...args);}};
context._partsCache.clear();people();people();assert.equal(reads,1);
context.roomControls.set(made.communityId,[...controls]);people();assert.equal(reads,2);
// Broken metadata cannot erase visible human authors or trust an unvalidated invite owner.
context._partsCache.clear();context.window.PosterCordReader={inspectControl(){throw Error('unreadable');}};
messages=[{pubkey:talker}];assert.deepEqual(people().sort(),[viewer,talker].sort());
