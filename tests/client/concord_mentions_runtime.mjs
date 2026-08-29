/* A MENTION IN A CHANNEL YOU ARE NOT LOOKING AT IS STILL A MENTION.
 *
 * notifyMentions was only ever called with `state.channel` — from the live merge and from render —
 * so the only channel that could raise a notification was the one already on screen, which is the
 * one you are reading. And its cursor was keyed on `room.naddr`, which a NIP-29 room and a room
 * joined by community id do not have: those got no mention notifications at all, because the guard
 * returned before looking at anything.
 *
 * This drives the real notifyMentions.
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
vm.runInNewContext(src, {window, document, console, setTimeout:()=>0, clearTimeout:noop,
  URL, atob, crypto:{}, localStorage, sessionStorage:{getItem:()=>null, setItem:noop}});
const api = window.PCConcord;

const ME = 'c'.repeat(64), THEM = 'd'.repeat(64);
const viewer = {pubkey: ME, npub: 'npub1me', profile: {name: 'verita'}};
const notes = [];
const p = {osNotify: (title, body, opts) => notes.push({title, body, opts}), profOf: () => ({})};

const msg = (at, text, tags = [], pubkey = THEM) =>
  ({id: 'm' + at, pubkey, by: 'them', text, at, kind: 9, tags});

/* A ROOM WITH NO naddr — a NIP-29 room, or one joined by community id. */
const ROOM29 = {protocol: 'nip29', communityId: 'cid-29', channels: [{name: 'general', id: 'c1'}]};

// First read records a cursor and says nothing: opening history must never alert.
api.notifyMentions(p, ROOM29, [msg(1000, 'hello @verita', [['p', ME]])], viewer, 'verita', 'general');
if (notes.length) throw new Error('opening history raised a notification: ' + JSON.stringify(notes));

// A genuinely newer mention, in a room that has no naddr at all.
api.notifyMentions(p, ROOM29, [msg(2000, 'ping @verita', [['p', ME]])], viewer, 'verita', 'general');
if (notes.length !== 1)
  throw new Error('a room without an naddr raised no mention notification: ' + JSON.stringify(notes));
if (!/general/.test(notes[0].title))
  throw new Error('the notification does not name the channel: ' + JSON.stringify(notes[0]));

// …and not twice for the same message.
api.notifyMentions(p, ROOM29, [msg(2000, 'ping @verita', [['p', ME]])], viewer, 'verita', 'general');
if (notes.length !== 1) throw new Error('the same mention notified twice');

// YOUR OWN MESSAGE IS NOT A MENTION OF YOU.
api.notifyMentions(p, ROOM29, [msg(3000, 'hi @verita', [['p', ME]], ME)], viewer, 'verita', 'general');
if (notes.length !== 1) throw new Error('your own message notified you');

/* PER CHANNEL, NOT PER ROOM. A newer post in #general must not suppress an older, newly fetched
   mention in #support — that is what one cursor for the whole community did. */
const before = notes.length;
api.notifyMentions(p, ROOM29, [msg(1500, 'over here @verita', [['p', ME]])], viewer, 'verita', 'support');
if (notes.length !== before)
  throw new Error("a channel's first read alerted instead of recording a cursor");
api.notifyMentions(p, ROOM29, [msg(1600, 'still here @verita', [['p', ME]])], viewer, 'verita', 'support');
if (notes.length !== before + 1)
  throw new Error('#support was suppressed by #general activity: ' + JSON.stringify(notes));

/* A ROOM WITH AN naddr KEEPS ITS EXISTING CURSOR, so changing the key does not re-announce
   history somebody has already read. */
const ROOMN = {protocol:'cord', naddr:'naddr1x', communityId:'cid-x', channels:[{name:'general', id:'c1'}]};
store['pc.concord.seen.cid-x:general'] = '5000';
const n0 = notes.length;
api.notifyMentions(p, ROOMN, [msg(4000, 'old @verita', [['p', ME]])], viewer, 'verita', 'general');
if (notes.length !== n0) throw new Error('a message older than the cursor was announced');

/* AND THE HYDRATION PATH IS WHAT SEES OTHER CHANNELS AT ALL. */
if (!/notifyMentions\(p,room,next,who,/.test(src))
  throw new Error('per-channel hydration no longer raises mentions');

console.log('concord mentions runtime ok');
