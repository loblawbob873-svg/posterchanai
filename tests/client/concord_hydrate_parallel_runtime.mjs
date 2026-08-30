/* A ROOM IS NOT ONE SERIAL QUEUE, AND ONE BAD CHANNEL IS NOT A BAD ROOM.
 *
 * Every channel waited on the previous channel's relay round trip, so a ten-channel community cost
 * ten of them before the room was usable — reported as Concord being "slow as fuck", worst on the
 * big Armada rooms. And a single throw in that loop abandoned every channel behind it, surfaced as
 * "could not refresh room history", and left `hydrated` unset, so the whole room was fetched again
 * on the next click: the next failure likelier, the room slower still.
 *
 * This drives the real hydrateRoomStreams.
 */
import fs from 'fs';
import vm from 'vm';
const src = fs.readFileSync(new URL('../../static/js/client/concord.js', import.meta.url), 'utf8');
const noop = () => {};
const store = {};
const localStorage = {getItem:k=>(k in store?store[k]:null), setItem:(k,v)=>{store[k]=String(v);},
                     removeItem:k=>{delete store[k];}};
const document = {querySelector:()=>null, querySelectorAll:()=>[], createElement:()=>({dataset:{}}),
  head:{appendChild:noop}, documentElement:{appendChild:noop}, addEventListener:noop,
  body:{classList:{add:noop, remove:noop, contains:()=>false}}};
const window = {document, addEventListener:noop};
vm.runInNewContext(src, {window, document, console, setTimeout:(f,ms)=>{queueMicrotask(f);return 0;},
  clearTimeout:noop, URL, atob, crypto:{}, localStorage, sessionStorage:{getItem:()=>null, setItem:noop}});
const api = window.PCConcord;

const CH = n => ({name:'c'+n, id:'id'+n, streamPubkeys:['b'.repeat(64)]});
const CHANNELS = [CH(0), CH(1), CH(2), CH(3), CH(4), CH(5)];
const BUNDLE = {community_id:'a'.repeat(64), channels:[], relays:['wss://r.example']};
const ROOM = {protocol:'cord', name:'Armada Room', communityId:'cid-1', naddr:'cid-1',
              channels:[CH(0)], cord:{bundle:BUNDLE}};
store['pc.concord.invites'] = JSON.stringify([ROOM]);
api.__testState({community:0, channel:'c0'});

window.PosterCordReader = {
  inspectControl: () => ({name:'Armada Room', description:'', banned:[], channels:CHANNELS,
                          controlPubkeys:['b'.repeat(64)]}),
  inspectChat: async () => ({messages:[], reactions:[], reactionUrls:[], pollVotes:[]}),
};

let inFlight = 0, peak = 0;
const asked = [];
const p = {
  toast: noop, profOf: () => ({}), enc: s => String(s), $: () => null,
  viewer: () => ({pubkey:'c'.repeat(64), profile:{}}), isView: () => true,
  // A subId string + relayClose, matching app.js's real PC surface (see
  // tests/client/test_relay_subscription_contract.py — a fake with a contract the real module does
  // not have is how the never-closed Concord subscriptions stayed invisible).
  relaySubscribe: () => 'sub-hydrate',
  relayClose: () => {},
  relayQuery: async () => [],
  relayQueryFrom: async (relays, filters) => {
    const authors = (filters && filters[0] && filters[0].authors) || [];
    asked.push(authors.join(','));
    inFlight++; peak = Math.max(peak, inFlight);
    await new Promise(r => setTimeout(r, 5));
    inFlight--;
    /* ONE CHANNEL THE RELAY WILL NOT ANSWER FOR. */
    if (asked.length === 3) throw new Error('relay refused');
    return [];
  },
  verifyRelayEvents: async e => e,
};

await api.hydrateRoomStreams(p, 0, 'cid-1');

/* THE ROOM SURVIVED THE BAD CHANNEL. */
const after = JSON.parse(localStorage.getItem('pc.concord.invites'))[0];
if (!after) throw new Error('the room vanished when one channel failed');
if (!(after.channels || []).length)
  throw new Error('the room lost its channels when one of them failed');

/* AND IT WAS NOT ONE SERIAL QUEUE. The head is fetched alone on purpose; everything after it
   overlaps, so a room with six channels must have had more than one request in the air. */
if (peak < 2)
  throw new Error('channel history is still fetched one at a time (peak in flight: ' + peak + ')');

/* EVERY CHANNEL WAS ASKED FOR, not just the ones before the failure. */
if (asked.length < CHANNELS.length)
  throw new Error('a failing channel abandoned the ones behind it: asked ' + asked.length +
                  ' of ' + CHANNELS.length);

console.log('concord hydrate parallel runtime ok');
