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
const src = fs.readFileSync(new URL('../../static/js/client/concord.js', import.meta.url), 'utf8');
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
vm.runInNewContext(src, {window, document, console,
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

let subFilters = null, subLive = null, onEvent = null, closed = 0;
const p = {
  relaySubscribe: (filters, h) => { subFilters = filters; subLive = h && h.live; onEvent = h && h.onEvent;
                                    return {close(){ closed++; }}; },
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
onEvent({id:'w1', kind:1059});
onEvent({id:'w2', kind:1059});
onEvent({id:'w3', kind:1059});
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

console.log('concord live messages runtime ok');
