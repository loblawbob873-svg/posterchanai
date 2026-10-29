// The SHIPPED concord-git.js refresh, against a fake relay door that counts dials per relay: the outside index is
// asked when a repository's own relays are unknown, and then at most every 30 minutes -- not once per minutely refresh.
import fs from 'node:fs';import vm from 'node:vm';import assert from 'node:assert/strict';
let now=1_800_000_000_000;
const asked=[];
const owner='a'.repeat(64),coord='30617:'+owner+':ngit';
const ann={id:'e'.repeat(64),kind:30617,pubkey:owner,created_at:1,tags:[['d','ngit'],['relays','wss://relay.ngit.dev']],content:'',sig:''};
const door={queryFrom:async(relays)=>{for(const r of relays)asked.push(r);return relays.includes('wss://index.ngit.dev')?[ann]:[];}};
const store={};
const ctx=vm.createContext({console,URL,TextEncoder,TextDecoder,setTimeout,clearTimeout,setInterval:()=>0,clearInterval(){},
  Date:class extends Date{static now(){return now;}},
  localStorage:{getItem:k=>store[k]??null,setItem:(k,v)=>{store[k]=String(v);},removeItem:k=>{delete store[k];}},
  document:{hidden:false,addEventListener(){},getElementById:()=>null,body:{classList:{contains:()=>false,add(){},remove(){}}}}});
ctx.window=ctx;ctx.PCConcordRelay=door;
vm.runInContext(fs.readFileSync('static/js/client/concord-git.js','utf8'),ctx);
const G=ctx.PCConcordGit,index=()=>asked.filter(u=>u==='wss://index.ngit.dev').length;
// Relays unknown: the index is what finds them.
await G._refresh(coord,[]);
assert.equal(index(),1,'a repository with no known relays asks the index');
// Every minute for 29 minutes: its own relays, never the index again.
for(let i=1;i<30;i++){now+=60_000;await G._refresh(coord,[]);}
assert.equal(index(),1,'the index was asked again inside 30 minutes: '+index());
assert.ok(asked.filter(u=>u==='wss://relay.ngit.dev').length>=29,'the repository\'s own relays are still refreshed');
// After 30 minutes, once more for a newer announcement.
now+=60_000;await G._refresh(coord,[]);
assert.equal(index(),2,'after 30 minutes the index is asked for a newer announcement');
// A repository NOBODY has an announcement for (deleted, never on the index): its relays stay unknown, and it used to
// ask the index on every minutely refresh, forever (index.ngit.dev 10 dials / 10 min on poster.place, 2026-10-29).
const lost='30617:'+'b'.repeat(64)+':gone';
const before=index();
for(let i=0;i<10;i++){await G._refresh(lost,[]);now+=60_000;}
assert.equal(index()-before,1,'a repository the index does not know asked it on every refresh: '+(index()-before)+' in 10 minutes');
now+=60_000;await G._refresh(lost,[]);
assert.equal(index()-before,2,'after 10 minutes an unknown repository asks the index again');
console.log('OK');
