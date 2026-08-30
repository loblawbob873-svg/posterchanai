/* THE CACHED HALF OF A ROOM OPEN MUST DECRYPT ONE CHANNEL, NOT ALL OF THEM.
 *
 * hydrateRoomStreams replays the encrypted on-disk cache before it opens a socket. That replay was
 * one serial loop over EVERY channel, decrypting a 300-envelope page each, and the network pass
 * could not begin until all of it had finished. It is NIP-44 on the main thread: measured against
 * Soapbox's real community, a 300-wrap page costs ~560ms, so its thirteen channels spent five to
 * seven seconds decrypting twelve conversations nobody had opened — before the first relay was even
 * asked. And it got WORSE with use, because the cache it re-reads is the thing that grows.
 *
 * This drives the real hydrateRoomStreams and counts which channels had been decrypted at the
 * moment it resolved. The selected one must be in; the other twelve must not be waited for.
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
/* A REAL setTimeout, not queueMicrotask: the prefetch yields between channels so the renderer can
   paint, and collapsing that to a microtask would let the whole loop run before hydration returns —
   the harness would then agree with the bug. */
vm.runInNewContext(src, {window, document, console, setTimeout, clearTimeout, URL, atob,
  crypto:{}, localStorage, sessionStorage:{getItem:()=>null, setItem:noop}});
const api = window.PCConcord;

const KEY = n => String(n).repeat(64).slice(0, 64);
const CH = n => ({name:'c'+n, id:'id'+n, streamPubkeys:[KEY(n)]});
const CHANNELS = [CH(0), CH(1), CH(2), CH(3), CH(4), CH(5), CH(6), CH(7)];
const BUNDLE = {community_id:'a'.repeat(64), channels:[], relays:['wss://r.example']};
const ROOM = {protocol:'cord', name:'Armada Room', communityId:'cid-1', naddr:'cid-1',
              channels:CHANNELS, cord:{bundle:BUNDLE}};
store['pc.concord.invites'] = JSON.stringify([ROOM]);
api.__testState({community:0, channel:'c0'});

/* Every channel has a full cached page, so nothing here is skipped for being empty. */
const PAGE = Array.from({length:300}, (_, i) => ({id:'w'+i, created_at:i}));
/* `page()` answers {events:[...]}, NOT a bare array — cachedEnvelopePage reads `got.events` and
   silently returns [] for anything else. A fixture that hands back an array makes every channel
   look like it has no cached history, so the loop under test never runs and the check passes
   against the very code it exists to catch. That happened; this comment is why the shape matters. */
window.PCConcordCache = {
  get: async () => PAGE,
  page: async () => ({events: PAGE}),
  put: async () => {},
};

/* Decryption is what costs, so the fake charges for it — the same shape as the real reader. */
const decrypted = [];
window.PosterCordReader = {
  inspectControl: () => ({name:'Armada Room', description:'', banned:[], channels:CHANNELS,
                          controlPubkeys:['b'.repeat(64)]}),
  inspectChat: async (bundle, controls, channelId) => {
    decrypted.push(channelId);
    await new Promise(r => setTimeout(r, 12));
    return {messages:[], reactions:[], reactionUrls:[], pollVotes:[]};
  },
};

const p = {
  toast: noop, profOf: () => ({}), enc: s => String(s), $: () => null,
  viewer: () => ({pubkey:'c'.repeat(64), profile:{}}), isView: () => true,
  relaySubscribe: () => 'sub-cached', relayClose: () => {},
  relayQuery: async () => [], relayQueryFrom: async () => [], verifyRelayEvents: async e => e,
};

await api.hydrateRoomStreams(p, 0, 'cid-1');
const atResolve = decrypted.slice();

/* Let the background prefetch finish, so the second half can be checked too. */
await new Promise(r => setTimeout(r, 600));

process.stdout.write(JSON.stringify({
  selected: 'id0',
  decryptedAtResolve: atResolve,
  decryptedEventually: decrypted.slice(),
  channels: CHANNELS.length,
}));
