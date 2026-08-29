import fs from 'fs';
import vm from 'vm';
const src=fs.readFileSync(new URL('../../static/js/client/concord.js',import.meta.url),'utf8');
const noop=()=>{};
const document={querySelector:()=>({}),createElement:()=>({dataset:{}}),head:{appendChild:noop},documentElement:{appendChild:noop},addEventListener:noop};
const window={document,addEventListener:noop};
vm.runInNewContext(src,{window,document,console,setTimeout:()=>0,clearTimeout:noop,URL,atob,crypto:{},localStorage:{getItem:()=>null,setItem:noop,removeItem:noop},sessionStorage:{getItem:()=>null,setItem:noop}});
const hex=n=>n.repeat(64),b64=h=>Buffer.from(h,'hex').toString('base64url');
const material={owner:b64(hex('1')),owner_salt:b64(hex('2')),community_root:b64(hex('3')),root_epoch:2,
  held_roots:[{epoch:1,key:b64(hex('6'))}],channels:[{id:b64(hex('4')),key:b64(hex('5')),epoch:1,name:'private',priors:[{epoch:0,key:b64(hex('7'))}]}],
  relays:['wss://nostr.computingcache.com'],name:'Vector room'};
const event=(id,at,d,doc)=>({event:{id,kind:33302,created_at:at,tags:[['d',d]]},doc});
const rows=[
  event('f'.repeat(64),10,'0',{frags:2,entries:[{community_id:b64(hex('a')),current:material,added_at:100}]}),
  event('0'.repeat(64),11,'0',{frags:2,entries:[{community_id:b64(hex('b')),current:material,added_at:200}]}),
  event('1'.repeat(64),11,'1',{frags:2,entries:[{community_id:b64(hex('c')),seed:material,added_at:250}],tombstones:[{community_id:b64(hex('a')),removed_at:300}]})
];
const got=window.PCConcord.decodeMembershipLists(rows);
if(got.entries.length!==2||got.entries[0].community_id!==hex('b'))throw new Error('newest fragment coordinate did not win');
if(got.entries[0].current.community_id!==hex('b')||got.entries[0].current.owner!==hex('1')||got.entries[0].current.channels[0].key!==hex('5')||
   got.entries[0].current.held_roots[0].key!==hex('6')||got.entries[0].current.channels[0].priors[0].key!==hex('7'))
  throw new Error('Vector base64url join material was not normalized at every key depth');
const seedOnly=got.entries.find(e=>e.community_id===hex('c'));
if(!seedOnly||seedOnly.current.community_id!==hex('c')||seedOnly.current.owner!==hex('1'))
  throw new Error('released seed-only membership was dropped instead of using seed as current');
if(got.tombstones.length!==1||got.tombstones[0].community_id!==hex('a'))throw new Error('fragment tombstone was lost');
const complete={owner:'old-owner',owner_salt:'salt',community_root:'old-root',root_epoch:1,
  channels:[{id:'general-id',key:'general-key',name:'general'}],held_roots:[{epoch:0,key:'old-key'}],
  relays:['wss://old.example']};
const snapshot={owner:'new-owner',community_root:'new-root',root_epoch:2,name:'Updated name',
  relays:['wss://new.example']};
const merged=window.PCConcord.mergeArmadaBundle(complete,snapshot);
if(merged.community_root!=='new-root'||merged.owner!=='new-owner'||merged.channels[0].key!=='general-key'||
   merged.held_roots[0].key!=='old-key'||merged.relays.length!==2)
  throw new Error('membership refresh discarded invite-only history material');
const poolEvent={id:'pool-membership',kind:13302,created_at:20,tags:[],content:'pool'},legacyEvent={id:'legacy-membership',kind:33302,created_at:10,tags:[['d','0']],content:'legacy'};
const queries=[];
/* A POOL HIT IS NOT A COMPLETE ANSWER, and treating it as one is what made a joined community
   disappear. Armada leaves list SHARDS across relays and the vault is replaceable, so the copy on
   the user's own relay can be real and stale at the same time — measured here, an account holding
   a 13302 from 08-27 and a 33302 from 08-24 locally, with the newer material only on the
   compatibility relays. The union must still happen; what was wrong was doing it on every startup
   AND on every 60/120s recovery tick, in parallel with the pool read. */
const distributed=await window.PCConcord.membershipEvents({
  relayQuery:async()=>[poolEvent],
  relayQueryFrom:async(relays,_filters,options)=>{queries.push({relays,options});return[legacyEvent];},
},hex('9'));
if(!distributed.some(e=>e.id===poolEvent.id)||!distributed.some(e=>e.id===legacyEvent.id))
  throw new Error('a pool hit cancelled the external union and hid the distributed shard');
if(!queries.length||!queries.every(q=>q.relays.includes('wss://relay.ditto.pub')&&q.relays.includes('wss://relay.damus.io')&&q.options.allowBlocked&&q.options.failureCooldown>=1800000))
  throw new Error('the union omitted the recovery relays or lost its bounded long circuit');
/* ONCE PER SESSION. Every pass after the first is local-only, which is what makes the recovery
   timer free — the actual complaint was sockets reopening for ever, not opening once. */
const swept=queries.length;
await window.PCConcord.membershipEvents({
  relayQuery:async()=>[poolEvent],
  relayQueryFrom:async(relays,_filters,options)=>{queries.push({relays,options});return[legacyEvent];},
},hex('9'));
if(queries.length!==swept)throw new Error('the recovery timer reopens external relays on every tick');
/* …AND SO DOES THE "recovery" PASS. It is not person-triggered — render() runs it on an ordinary
   launch that already has a room open, and an active room may only touch the relays in its own
   bundle (concord_runtime.mjs pins that). One bounded cross-relay recovery per session. */
await window.PCConcord.membershipEvents({
  relayQuery:async()=>[],
  relayQueryFrom:async(relays,_filters,options)=>{queries.push({relays,options});return[legacyEvent];},
},hex('9'),{external:true,legacyRecovery:true});
if(queries.length!==swept)throw new Error('the recovery pass re-swept from inside an open room');
/* A DIFFERENT ACCOUNT GETS ITS OWN SWEEP — structural, because the mark's key CONTAINS the
   pubkey. Pinned anyway so a later "simplify the key" cannot silently make one account's
   "already looked" speak for the next one signed in on the same page. */
const before=queries.length;
await window.PCConcord.membershipEvents({
  relayQuery:async()=>[poolEvent],
  relayQueryFrom:async(relays,_filters,options)=>{queries.push({relays,options});return[legacyEvent];},
},hex('7'));
if(queries.length<=before)throw new Error('a second account inherited the first account\'s session mark');
console.log('vector membership runtime ok');

/* A ROOM WHOSE BUNDLE CARRIES NO CHANNELS CANNOT OPEN ONE, whatever its sidebar list says.
   Measured on a real account: three of four saved communities listed 1, 13 and 7 channels beside a
   `cord.bundle` whose own channel list was EMPTY — the shape an Armada vault snapshot produces —
   and `inspectChat` then threw "channel is not readable with this membership" on every four-second
   live-sync tick for the whole visit. `inspectControl` accepts that bundle (it only needs an owner
   and a root), so the emptiness has to be asked about directly. */
const jm = window.PCConcord.hasJoinMaterial;
if(jm({channels:[{id:'c1'}]})!==true) throw new Error('a bundle WITH channels was called unusable');
for(const empty of [null, undefined, {}, {channels:[]}, {channels:'nope'}])
  if(jm(empty)!==false) throw new Error('a bundle with no channel material passed as joinable: '+JSON.stringify(empty));
console.log('vector membership runtime ok (join material)');
