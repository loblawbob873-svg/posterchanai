import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
globalThis.window=globalThis;
const noop=()=>{};
globalThis.document={querySelector:()=>({}),createElement:()=>({dataset:{}}),head:{appendChild:noop},documentElement:{appendChild:noop},addEventListener:noop};
globalThis.addEventListener=noop;
globalThis.localStorage={getItem:()=>null,setItem:noop,removeItem:noop};
globalThis.sessionStorage=localStorage;
for(const [path,name] of [['../../static/vendor/nostr/nostr.bundle.js','NostrTools'],['../../static/js/client/cord-protocol.js','PosterCord']])
  vm.runInThisContext(fs.readFileSync(new URL(path,import.meta.url),'utf8')+`\nglobalThis.${name}=${name};`);
vm.runInThisContext(fs.readFileSync(new URL('../../static/js/client/concord.js',import.meta.url),'utf8'));
const signer='a'.repeat(64),token=Buffer.alloc(16,7);
const address=relays=>NostrTools.nip19.naddrEncode({kind:33301,pubkey:signer,identifier:'',relays});
const fragment=(relay,tok=token)=>{const text=Buffer.from(relay);return Buffer.concat([Buffer.from([4,0,1,255,text.length]),text,tok]).toString('base64url');};
const room=(host,hints,relay,tok)=>({url:`https://${host}/invite/${address(hints)}#${fragment(relay,tok)}`,name:'Same name'});
const first=room('armada.buzz',['wss://first.example'],'wss://first.example');
const repost=room('poster.place',['wss://second.example'],'wss://second.example');
assert.equal(PosterCord.inviteDetails(first.url).linkSigner,signer);
assert.equal(PosterCord.inviteDetails(repost.url).linkSigner,signer);
const sameInvite=PCConcord.uniqueRooms([first,repost,first,repost]);
assert.equal(sameInvite.length,1,'real v4 routing hints produced duplicate room');
assert.equal((sameInvite[0].inviteAliases||[]).length,0,'routing-only variants accumulated redundant aliases');
const distinct=room('poster.place',[],'wss://second.example',Buffer.alloc(16,8));
assert.equal(PCConcord.uniqueRooms([first,repost,distinct]).length,2,'different token was merged at same signer');
const unknown={...first,url:first.url.split('#')[0]+'#'+Buffer.concat([Buffer.from([5,0]),token]).toString('base64url')};
assert.throws(()=>PosterCord.inviteDetails(unknown.url));
assert.equal(PCConcord.sameRoom(first,unknown),false,'unknown format was decoded as v4');
const hydrated={...repost,communityId:'b'.repeat(64),cord:{bundle:{community_id:'b'.repeat(64)},hydrated:true},channels:[{id:'general-channel',name:'general'}]};
const repaired=PCConcord.uniqueRooms([{...first,communityId:address([])},hydrated]);
assert.equal(repaired.length,1);
assert.equal(repaired[0].communityId,hydrated.communityId);
assert.equal(repaired[0].channels[0].id,'general-channel');
assert.equal(PCConcord.uniqueRooms([...repaired,{...distinct,communityId:hydrated.communityId}]).length,1,'rotated token recreated hydrated room');

// ONE INVITER, TWO COMMUNITIES (2026-10-22, "Sorsmenedék and Rin's anime room keep changing into each other"). Both
// invites live at the inviter's one address (kind 33301, d=''), so before hydration both rows carry the SAME naddr as
// their communityId; only the token differs. They are two communities and must stay two rows.
const sors={...room('armada.buzz',[],'wss://first.example',Buffer.alloc(16,1)),name:'Sorsmenedék',communityId:address([])};
const rin={...room('armada.buzz',[],'wss://first.example',Buffer.alloc(16,2)),name:"Rin's anime room",communityId:address([])};
assert.equal(PCConcord.sameRoom(sors,rin),false,'two invites from one inviter were taken as one community');
assert.equal(PCConcord.uniqueRooms([sors,rin]).length,2,'two communities from one inviter were merged');
// Hydrated: different bundles are never the same room, whatever their invites say.
const A='c'.repeat(64),B='d'.repeat(64);
const sorsH={...sors,communityId:A,cord:{bundle:{community_id:A},hydrated:true}},rinH={...rin,communityId:B,cord:{bundle:{community_id:B},hydrated:true}};
assert.equal(PCConcord.uniqueRooms([sorsH,rinH,sors,rin]).length,2,'hydrated communities from one inviter collapsed');
// A row the OLD rule merged: one community's id, the other's bundle, both invite URLs. It is repaired to the bundle it
// holds and SPLIT, never dropped (2026-10-22, "still shows rins room but community says Sorsmenedek": dropping the
// alias lost the other room on a device whose membership list never brought it back, and the kept row went on
// drawing the other community's channels). Every other invite becomes its own row; the kept row's channels are
// cleared so its own control read draws them.
const merged={...sorsH,channels:[{name:'Anime 🍿',id:'rin-anime'}],cord:{bundle:{community_id:B},hydrated:true},inviteAliases:[sors.url,rin.url]};
const healed=PCConcord.uniqueRooms([merged,sorsH]);
const kept=healed.find(r=>r.cord&&r.cord.bundle&&r.cord.bundle.community_id===B);
assert.ok(kept,'the merged row was lost');
assert.equal(kept.communityId,B,'the kept row does not follow its bundle');
assert.ok(!kept.inviteAliases,'a foreign alias survived on the kept row');
assert.ok(!(kept.channels||[]).some(c=>c.id==='rin-anime'),'the kept row still draws the other community\'s channels');
assert.ok(healed.some(r=>r.communityId===A),'the other hydrated community disappeared');
const rebuilt=healed.find(r=>r.url===rin.url&&!(r.cord&&r.cord.bundle));
assert.ok(rebuilt,'the other invite was DROPPED instead of becoming its own room');
// Once that invite hydrates to the kept row's community, it merges back: nothing is duplicated for good.
const hydratedAgain=PCConcord.uniqueRooms(healed.map(r=>r===rebuilt?{...r,communityId:B,cord:{bundle:{community_id:B},hydrated:true}}:r));
assert.equal(hydratedAgain.length,2,'a rebuilt invite that hydrated to the same community stayed a duplicate');
assert.deepEqual(hydratedAgain.map(r=>r.communityId).sort(),[A,B].sort());
// Clicking Rin's invite after the split ("it reloads both channels at the same time"): the invite must find ONE
// room, Rin's own, and never the kept Sorsmenedek row -- the merged row matched both invites and reloaded both.
const clicked={...PCConcord.inviteParts(rin.url),url:rin.url};
const hits=healed.filter(r=>PCConcord.sameRoom(r,clicked));
assert.equal(hits.length,1,'an invite click matched '+hits.length+' rooms');
assert.ok(hits[0]!==kept&&hits[0].url===rin.url,'Rin\'s invite opened the Sorsmenedek row');
// And the leftover alias itself no longer captures it: a decrypted row is matched by an undecrypted invite through
// its OWN invite only, so even the pre-split row cannot swallow Rin's (2026-10-22, "it loads the other community").
assert.equal(PCConcord.sameRoom(merged,clicked),false,'a leftover alias on a decrypted row still swallows an undecrypted invite');
assert.equal(PCConcord.sameRoom(merged,{...PCConcord.inviteParts(sors.url),url:sors.url}),true,'a row no longer matches its own invite');
console.log('Concord real invite identity runtime ok');
