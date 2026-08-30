/* AN UNCONFIRMED SUBSCRIPTION MUST STAY OPEN.
 *
 * startChatLive opens two subscriptions — the managed pool and direct sockets to the room's own
 * invite relays — and races two "did it open?" gates. It used to call stopChatLive() when BOTH
 * rejected, and both reject routinely on a perfectly healthy room:
 *
 *   waitForSubscription(pooled, urls) resolves true only if the pooled REQ reached one of the
 *   ROOM's urls. A Concord room's relays come from its invite bundle and are usually not among the
 *   signed-in user's account relays, so the pool correctly never sends there and the gate reports
 *   false after its 8s timeout — about a subscription that is alive.
 *
 *   external.ready is false whenever those third-party relays are slow or blocked from THIS
 *   browser. Measured from the server all four answer in under a second while Firefox logged
 *   "can't establish a connection" for three of them.
 *
 * The result was a live stream destroyed seconds after every room open, re-armed by the 4s tick
 * (stopChatLive clears chatSubKey), reopening sockets to up to eight relays and tearing them down
 * again for as long as the room was on screen: "major slow" and "not showing room messages".
 *
 * The existing live-messages runtime cannot see it — its external gate RESOLVES, so Promise.any
 * settles and the teardown branch is never reached. This drives the case that actually happens:
 * both gates reject.
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
  body:{classList:{add:noop, remove:noop, contains:()=>false}}};
const window = {document, addEventListener:noop};

const BUNDLE = {community_id:'a'.repeat(64), channels:[], relays:['wss://room-a.example','wss://room-b.example']};
const CH = {name:'general', id:'chan-1', streamPubkeys:['b'.repeat(64)]};
const ROOM = {protocol:'cord', name:'PosterChan', communityId:'cid-1', naddr:'cid-1',
              channels:[CH], cord:{bundle:BUNDLE}};
store['pc.concord.invites'] = JSON.stringify([ROOM]);

const timers = [];
vm.runInNewContext(src, {window, document, console,
  setTimeout:(f,ms)=>{timers.push(f);return timers.length;}, clearTimeout:noop,
  URL, atob, crypto:{}, localStorage, sessionStorage:{getItem:()=>null, setItem:noop}});
const api = window.PCConcord;

window.PosterCordReader = {
  inspectControl: () => ({name:'PosterChan', description:'', banned:[], channels:[CH],
                          controlPubkeys:['b'.repeat(64)]}),
  inspectChat: async (bundle, ctrl, id, wraps) => ({
    messages:(wraps||[]).map((w,i)=>({id:'m'+w.id, pubkey:'c'.repeat(64),
      text:'pushed '+w.id, at:1000+i, kind:9, tags:[]})), reactions:[], reactionUrls:[]}),
};

let onEvent = null, closed = 0, externalClosed = 0;
window.Relay = {
  subscribe: (filters, h) => { onEvent = h && h.onEvent; return 'sub-pooled'; },
  subscribeFrom: (urls, filters, h) => {
    const close = () => { externalClosed++; };
    close.hasTargets = true;
    // THE ROOM'S RELAYS DID NOT ANSWER THIS BROWSER. Both gates now reject.
    close.ready = Promise.resolve(false);
    return close;
  },
  // …and the managed pool never carried the room's relays, so it cannot confirm either.
  waitForSubscription: async () => false,
  close: id => { if (id !== 'sub-pooled') throw new Error('closed the wrong sub: ' + id);
                 closed++; },
};
const p = {
  relayQuery: async () => { throw new Error('the live path must not poll'); },
  relayQueryFrom: async () => { throw new Error('the live path must not poll'); },
  verifyRelayEvents: async e => e, profOf: () => ({}), toast: noop,
  viewer: () => ({pubkey:'c'.repeat(64), profile:{}}), enc: s => String(s), $: () => null,
};

api.__testState({community:0, channel:'general', controls:['cid-1', [{id:'ctrl-1'}]]});
api.startChatLive(p, ROOM, CH);

// Let both gates settle and Promise.any reject.
for (let i = 0; i < 30; i++) await new Promise(r => setImmediate(r));

if (closed || externalClosed)
  throw new Error('the subscription was TORN DOWN because neither gate could confirm it '
    + '(pooled closed=' + closed + ', external closed=' + externalClosed + '). Failing to prove a '
    + 'subscription opened is not proof that it did not — and the 4s tick then re-arms it, so the '
    + 'room reopens and destroys its sockets every few seconds for as long as it is on screen.');

// And it must still WORK: a wrap arriving on the pooled handle reaches the channel store.
if (!onEvent) throw new Error('no live handler survived — nothing can deliver a message');
onEvent({id:'w1', kind:1059});
if (!timers.length) throw new Error('an arriving wrap scheduled no flush — the stream is dead');
timers[timers.length - 1]();
for (let i = 0; i < 20; i++) await new Promise(r => setImmediate(r));

const saved = api.__testMessages('cid-1') || [];
if (!saved.some(m => m.text === 'pushed w1'))
  throw new Error('a message pushed on an unconfirmed subscription never reached the channel '
    + 'store: ' + JSON.stringify(saved));

console.log('concord unconfirmed subscription runtime ok');
