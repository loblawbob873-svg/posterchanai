/* MESSAGES ARRIVE, THEY ARE NOT FETCHED FOR.
 *
 * The live tick runs every four seconds, but the query it makes carries `minInterval:60000` — so on
 * any community whose relays are not already in the shared pool a message could take a FULL MINUTE
 * to appear. Tightening the timer cannot fix that; polling a relay that can push is the wrong shape.
 *
 * This drives the real startChatLive/flushChatLive with a fake subscription: an event is pushed,
 * and the channel's store must contain it without any query having been made.
 */
import fs from 'fs';
import vm from 'vm';
const src = fs.readFileSync(process.env.PC_CONCORD_SOURCE || new URL('../../static/js/client/concord.js', import.meta.url), 'utf8');
const noop = () => {};
const store = {};
const localStorage = {
  getItem: k => (k in store ? store[k] : null),
  setItem: (k, v) => { store[k] = String(v); },
  removeItem: k => { delete store[k]; },
};
const document = {querySelector:()=>null, querySelectorAll:()=>[], createElement:()=>({dataset:{}}),
  head:{appendChild:noop}, documentElement:{appendChild:noop}, addEventListener:noop,
  /* NOT on screen. What is under test is that a PUSHED wrap is opened once, as a batch, and
     lands in the channel's store — the repaint half is the render suite's job, and driving it
     here would only mean stubbing the whole DOM to watch a store write. */
  body:{classList:{add:noop, remove:noop, contains:()=>false}}};
const window = {document, addEventListener:noop};

const BUNDLE = {community_id:'a'.repeat(64), channels:[], relays:['wss://r.example']};
const CH = {name:'general', id:'chan-1', streamPubkeys:['b'.repeat(64)]};
const ROOM = {protocol:'cord', name:'PosterChan', communityId:'cid-1', naddr:'cid-1',
              channels:[CH], cord:{bundle:BUNDLE}};
store['pc.concord.invites'] = JSON.stringify([ROOM]);

const timers = [];
const PCOS={isOn:()=>true,parkedSlot:()=>true};window.PCOS=PCOS;
vm.runInNewContext(src, {window, document, console, PCOS,
  setTimeout:(f,ms)=>{timers.push(f);return timers.length;}, clearTimeout:noop,
  URL, atob, crypto:{}, localStorage, sessionStorage:{getItem:()=>null, setItem:noop}});
const api = window.PCConcord;

let opened = [];
window.PosterCordReader = {
  inspectControl: () => ({name:'PosterChan', description:'', banned:[], channels:[CH],
                          controlPubkeys:['b'.repeat(64)]}),
  inspectChat: async (bundle, ctrl, id, wraps) => {
    opened.push((wraps || []).length);
    return {messages:(wraps||[]).map((w,i)=>({id:'m'+w.id, pubkey:'c'.repeat(64),
              text:'pushed '+w.id, at:1000+i, kind:9, tags:[]})),
            reactions:[], reactionUrls:[]};
  },
};

let subFilters = null, subLive = null, onEvent = null, externalEvent = null, closed = 0, externalClosed = 0;
const externalEvents=[];
window.Relay = {
  subscribe: (filters, h) => { subFilters=filters;subLive=h&&h.live;onEvent=h&&h.onEvent;return 'real-string-sub-id'; },
  subscribeFrom: (urls, filters, h) => { externalEvent=h&&h.onEvent;externalEvents.push(externalEvent);const close=()=>{externalClosed++;};close.hasTargets=true;close.ready=Promise.resolve(true);return close; },
  waitForSubscription: async () => false,
  close: id => { if(id!=='real-string-sub-id')throw new Error('wrong managed subscription id');closed++; },
};
const p = {
  relayQuery: async () => { throw new Error('the live path must not poll'); },
  relayQueryFrom: async () => { throw new Error('the live path must not poll'); },
  verifyRelayEvents: async e => e, profOf: () => ({}), toast: noop,
  viewer: () => ({pubkey:'c'.repeat(64), profile:{}}), enc: s => String(s), $: () => null,
};

api.__testState({community:0, channel:'general', controls:['cid-1', [{id:'ctrl-1'}]]});
api.startChatLive(p, ROOM, CH);

if (!subFilters) throw new Error('chat never subscribed — it is still polling');
if (subLive !== true) throw new Error('the chat subscription is not live');
const f = subFilters[0];
if (!(f.kinds || []).includes(1059))
  throw new Error('the subscription does not ask for the channel wrap kind: ' + JSON.stringify(f));
if (String((f.authors || [])[0]) !== 'b'.repeat(64))
  throw new Error('the subscription is not scoped to the channel stream: ' + JSON.stringify(f));

/* A BURST IS ONE BATCH. Opening a wrap is real cryptography; per event on the main thread is how a
   busy channel becomes a stutter. */
/* Deliver on the invite relay handle. The managed pool deliberately reports not-ready above. */
externalEvent({id:'w1', kind:1059});
externalEvent({id:'w2', kind:1059});
externalEvent({id:'w3', kind:1059});
if (timers.length !== 1)
  throw new Error('three arrivals scheduled ' + timers.length + ' flushes, not one');
onEvent({id:'ignored', kind:1});          // not a wrap
/* The timer callback fires the flush and returns — a setTimeout callback cannot be awaited, so
   the module launches it with `void`. Let the async chain settle before reading the store. */
timers[0]();
for (let i = 0; i < 20; i++) await new Promise(r => setImmediate(r));

if (opened.length !== 1) throw new Error('the batch was opened ' + opened.length + ' times');
if (opened[0] !== 3) throw new Error('the batch carried ' + opened[0] + ' wraps, not 3');

const saved = api.__testMessages('cid-1');
const texts = (saved || []).map(m => m.text).sort();
if (texts.length !== 3)
  throw new Error('pushed messages did not reach the channel store: ' + JSON.stringify(saved));

/* SWITCHING CHANNELS CLOSES THE OLD STREAM, or the channel you left feeds the one you opened. */
api.startChatLive(p, ROOM, {name:'other', id:'chan-2', streamPubkeys:['d'.repeat(64)]});
if (!closed) throw new Error('switching channels left the old subscription open');
if (!externalClosed) throw new Error('switching channels left the external room socket open');
const timerCount=timers.length;
externalEvents[0]({id:'late-old-room',kind:1059});
if(timers.length!==timerCount)throw new Error('a stale room callback scheduled a flush after close');

// A slow history refresh must not re-arm the room/channel the user has left.
window.PCOS={isOn:()=>true,parkedSlot:()=>true};
let releaseHistory;
p.relayQuery=()=>new Promise(resolve=>{releaseHistory=resolve});
p.relayQueryFrom=async()=>[];
window.PosterCordReader.inspectChat=async(_b,_c,_i,wraps)=>({messages:wraps.map(w=>({id:w.id,pubkey:'c'.repeat(64),text:'old room history',at:4000,kind:9,tags:[]})),reactions:[],reactionUrls:[]});
const refreshing=api.refreshActiveChannel(p);
for(let i=0;i<10;i++)await new Promise(r=>setImmediate(r));
api.__testState({channel:'other'});
api.startChatLive(p,ROOM,{name:'other',id:'chan-2',streamPubkeys:['d'.repeat(64)]});
releaseHistory([{id:'kept-old-room',kind:1059}]);await refreshing;
if(subFilters[0].authors[0]!=='d'.repeat(64))throw Error('stale history refresh replaced the selected channel live subscription');
if(!api.__testMessages('cid-1').some(m=>m.id==='kept-old-room'))throw Error('same-account old-room history was unnecessarily discarded');
if(api.__testMessages('cid-1.other').some(m=>m.id==='kept-old-room'))throw Error('old-room history leaked into selected channel');
console.log('history room-switch guard passed');

api.__testState({channel:'general'});
let owner='c'.repeat(64),decryptions=0;
p.viewer=()=>({pubkey:owner,profile:{}});
window.PosterCordReader.inspectChat=async()=>{decryptions++;return{messages:[],reactions:[],reactionUrls:[]}};
const oldRead=api.refreshActiveChannel(p);
for(let i=0;i<10;i++)await new Promise(r=>setImmediate(r));
owner='f'.repeat(64);
releaseHistory([{id:'private-old-account',kind:1059}]);await oldRead;
if(decryptions)throw Error('old account history was decrypted after account switch');
console.log('history account-switch guard passed');

owner='c'.repeat(64);
const leftRead=api.refreshActiveChannel(p);
for(let i=0;i<10;i++)await new Promise(r=>setImmediate(r));
store['pc.concord.invites']='[]';releaseHistory([{id:'left-room',kind:1059}]);await leftRead;
if(decryptions)throw Error('history decrypted after leaving room');
console.log('left-room history guard passed');
