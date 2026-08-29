/* A COMMUNITY THAT COULD NOT BE READ MUST STILL EXIST.
 *
 * `channelsView` in Armada's own reader builds the readable channel set from the community's
 * CONTROL EVENTS and from nothing else, so with none of them `inspectChat` refuses every id —
 * correctly. Two ways to get that wrong, and I shipped both in one afternoon:
 *
 *   1. remembering the empty answer (`[]` is truthy, so the "have I fetched these?" guard reads a
 *      failed fetch as success and the room stays unreadable for the whole session);
 *   2. RETURNING on the miss — which, inside hydrateRoomStreams, skips everything after it, so a
 *      community that merely could not be READ stopped being hydrated and vanished from the list.
 *
 * The second was reported within minutes as "my posterchan concord community just disappeared".
 * Neither is visible from the source of one function; both need the failure driven.
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
  body:{classList:{add:noop, remove:noop, contains:()=>true}}};
const window = {document, addEventListener:noop};

const ROOM = {protocol:'cord', name:'PosterChan', communityId:'cid-1', naddr:'cid-1',
  url:'https://armada.buzz/invite/naddr1#frag',
  channels:[{name:'general', id:'chan-general'}],
  cord:{bundle:{community_id:'a'.repeat(64), channels:[], relays:['wss://r.example']}}};
store['pc.concord.invites'] = JSON.stringify([ROOM]);

vm.runInNewContext(src, {window, document, console, setTimeout:(f)=>{return 0;}, clearTimeout:noop,
  URL, atob, crypto:{}, localStorage, sessionStorage:{getItem:()=>null, setItem:noop}});
const api = window.PCConcord;

/* The reader answers, the RELAY does not: a control query that comes back empty. This is one
   unreachable relay at the wrong moment, which is all it takes. */
window.PosterCordReader = {
  inspectControl: () => ({name:'PosterChan', description:'', banned:[], channels:[],
                          controlPubkeys:['b'.repeat(64)]}),
  inspectChat: async () => { throw new Error('channel is not readable with this membership'); },
};
const p = {
  relayQueryFrom: async () => [],          // nothing comes back
  relayQuery: async () => [],
  verifyRelayEvents: async e => e,
  profOf: () => ({}),
  toast: noop, enc: s => String(s), $: () => null, viewer: () => ({pubkey:'c'.repeat(64)}),
  isView: () => true,
};

let threw = null;
try { await api.hydrateRoomStreams(p, 0, 'cid-1'); } catch (e) { threw = e; }

/* THE ROOM SURVIVES. Not "the room loaded" — it could not, and that is honest — but it is still
   in the list, so the next attempt can reach it and the person can still see their community. */
const after = JSON.parse(localStorage.getItem('pc.concord.invites') || '[]');
if (!after.length)
  throw new Error('a community that could not be read was removed from the saved list' +
                  (threw ? ' (threw: ' + threw.message + ')' : ''));
if (!after.some(r => r.communityId === 'cid-1'))
  throw new Error('the community lost its identity during a failed hydration');

console.log('concord hydration runtime ok');
