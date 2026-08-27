import fs from 'fs';
import vm from 'vm';
const src=fs.readFileSync(new URL('../../static/js/client/concord.js',import.meta.url),'utf8');
const noop=()=>{};
const document={querySelector:()=>({}),createElement:()=>({dataset:{}}),head:{appendChild:noop},documentElement:{appendChild:noop},addEventListener:noop};
const window={document,addEventListener:noop};
vm.runInNewContext(src,{window,document,console,setTimeout:()=>0,clearTimeout:noop,URL,atob,crypto:{},localStorage:{getItem:()=>null,setItem:noop,removeItem:noop},sessionStorage:{getItem:()=>null,setItem:noop}});
const hex=n=>n.repeat(64),b64=h=>Buffer.from(h,'hex').toString('base64url');
const material={owner:b64(hex('1')),owner_salt:b64(hex('2')),community_root:b64(hex('3')),root_epoch:2,channels:[{id:b64(hex('4')),key:b64(hex('5')),epoch:1,name:'private'}],relays:['wss://nostr.computingcache.com'],name:'Vector room'};
const event=(id,at,d,doc)=>({event:{id,kind:33302,created_at:at,tags:[['d',d]]},doc});
const rows=[
  event('f'.repeat(64),10,'0',{frags:2,entries:[{community_id:b64(hex('a')),current:material,added_at:100}]}),
  event('0'.repeat(64),11,'0',{frags:2,entries:[{community_id:b64(hex('b')),current:material,added_at:200}]}),
  event('1'.repeat(64),11,'1',{frags:2,tombstones:[{community_id:b64(hex('a')),removed_at:300}]})
];
const got=window.PCConcord.decodeMembershipLists(rows);
if(got.entries.length!==1||got.entries[0].community_id!==hex('b'))throw new Error('newest fragment coordinate did not win');
if(got.entries[0].current.community_id!==hex('b')||got.entries[0].current.owner!==hex('1')||got.entries[0].current.channels[0].key!==hex('5'))throw new Error('Vector base64url join material was not normalized');
if(got.tombstones.length!==1||got.tombstones[0].community_id!==hex('a'))throw new Error('fragment tombstone was lost');
console.log('vector membership runtime ok');
