/* AN EMPTY CHANNEL AND AN UNREADABLE ONE MUST NOT SAY THE SAME THING.
 *
 * Runs the SHIPPED emptyChannelHtml against the SHIPPED reach bookkeeping. A source-literal test
 * cannot see this: the wrong sentence is grammatical, confident and drawn perfectly.
 *
 * The measurement behind it: two joined Vector rooms whose relay set includes
 * wss://asia.vectorapp.io/nostr, which answers every kind-1059 filter with `auth-required` and
 * then rejects the AUTH itself ("relay needs serviceUrl to be configured before AUTH can work").
 * Its history is unreadable to any client, and half an hour of failure cooldown later queryFrom
 * skips it entirely and returns []. Concord drew "This is the start of this encrypted channel".
 */
import fs from 'node:fs';import vm from 'node:vm';import assert from 'node:assert/strict';

const el=(tag)=>{const n={tag,children:[],dataset:{},style:{},classList:{add(){},remove(){},contains(){return false;}},
  appendChild(c){n.children.push(c);return c;},setAttribute(){},removeAttribute(){},addEventListener(){},
  querySelector(){return null;},querySelectorAll(){return [];},remove(){},get textContent(){return '';},set textContent(_){}};
  return n;};
const store=m=>({getItem:k=>(k in m?m[k]:null),setItem:(k,v)=>{m[k]=String(v);},removeItem:k=>{delete m[k];}});

const room={name:'Politics',communityId:'2e26f107',naddr:'naddr-politics',url:'',
  channels:[{name:'general',id:'chan-1'}],cord:{bundle:{relays:['wss://a.example','wss://b.example']}}};

const ctx={console,setTimeout,clearTimeout,setInterval,clearInterval,JSON,Date,Math,URL,Promise,Error,
  localStorage:store({'pc.concord.invites':JSON.stringify([room]),'pc.concord.active':'0'}),
  sessionStorage:store({}),
  document:{body:{classList:{contains:()=>false,add(){},remove(){}}},head:el('head'),documentElement:el('html'),
    querySelector:()=>null,querySelectorAll:()=>[],createElement:el,addEventListener(){},removeEventListener(){}},
  addEventListener(){},removeEventListener(){},matchMedia:()=>({matches:false,addEventListener(){},removeEventListener(){}}),
  requestAnimationFrame:fn=>setTimeout(fn,0),navigator:{},location:{href:'https://example.test/'}};
ctx.window=ctx;ctx.globalThis=ctx;ctx.self=ctx;
const enc=s=>String(s==null?'':s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
ctx.__PC={$:()=>null,$$:()=>[],enc,viewer:()=>({pubkey:'a'.repeat(64)}),isView:()=>false,toast(){},LOGO:''};
vm.createContext(ctx);
vm.runInContext(fs.readFileSync(process.env.PC_CONCORD_SOURCE||new URL('../../static/js/client/concord.js',import.meta.url),'utf8'),ctx);
const C=ctx.window.PCConcord;
C.acceptHandoff({room:'naddr-politics',channel:'general'});
const storeId='naddr-politics';   // channelStoreId(room,'general') is the room identity

// 1. Nothing measured yet is not a claim either way — the original copy stands.
let html=C.emptyChannelHtml(ctx.__PC,room);
assert(html.includes('start of this encrypted channel'),html);
assert(!html.includes('cc-retry-channel'),'nothing has been measured, so nothing is being reported');

// 2. Every relay answered and there was nothing: that IS an empty channel.
C.noteChannelReach(storeId,{ok:['wss://a.example','wss://b.example']},['wss://a.example','wss://b.example']);
html=C.emptyChannelHtml(ctx.__PC,room);
assert(html.includes('start of this encrypted channel'),html);

// 3. One relay refused: say so, name it, and offer the retry.
C.noteChannelReach(storeId,{ok:['wss://a.example'],failed:['wss://b.example']},['wss://a.example','wss://b.example']);
html=C.emptyChannelHtml(ctx.__PC,room);
assert(!html.includes('start of this encrypted channel'),'an unread channel must not claim to be new: '+html);
assert(html.includes('could not be read'),html);
assert(html.includes('b.example'),'the relay that did not answer is the whole diagnosis: '+html);
assert(html.includes('1 of 2'),html);
assert(html.includes('cc-retry-channel'),'a state the user can fix needs the button that fixes it');

// 4. A relay held out by OUR failure cooldown counts the same — that is the state this sat in.
C.noteChannelReach(storeId,{ok:['wss://a.example'],cooled:['wss://b.example']},['wss://a.example','wss://b.example']);
assert(C.emptyChannelHtml(ctx.__PC,room).includes('could not be read'));

// 5. Ordinary rate limiting is NOT a failure: a live tick that asked nobody must not cry wolf.
C.noteChannelReach(storeId,{ok:['wss://a.example','wss://b.example']},['wss://a.example','wss://b.example']);
C.noteChannelReach(storeId,{held:['wss://a.example','wss://b.example']},['wss://a.example','wss://b.example']);
assert(C.emptyChannelHtml(ctx.__PC,room).includes('start of this encrypted channel'),
  'a pass that reached no socket teaches nothing and must not erase what the last real read measured');

// 6. A stream this membership holds no key for is unread, not empty.
C.noteChannelReach(storeId,{ok:['wss://a.example','wss://b.example'],unheld:['f'.repeat(64)]},
  ['wss://a.example','wss://b.example']);
const unheld=C.emptyChannelHtml(ctx.__PC,room);
assert(unheld.includes('could not be read')&&unheld.includes('membership key'),unheld);

// 7. A local test room keeps its own copy — it has no relays to report on.
assert(C.emptyChannelHtml(ctx.__PC,{...room,local:true}).includes('local test room'));

// 8. A community name cannot close an attribute or open a tag.
C.noteChannelReach(storeId,{ok:[],failed:['wss://"><script>evil</script>.example']},['wss://x']);
assert(!C.emptyChannelHtml(ctx.__PC,room).includes('<script>'),'relay hosts are attacker-supplied text');

console.log('concord unread channel runtime ok');
