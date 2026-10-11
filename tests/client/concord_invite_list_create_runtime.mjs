/* "community creation failed: Invite List sync is incomplete; retry before changing it" — REPRODUCED.
 *
 * Reported by the operator's user while creating a Concord community. Creating one writes the
 * creator's encrypted Invite List (kind 13303, replaceable) so the new link's signing key is never
 * lost, and that write refuses to go ahead unless the read before it heard from EVERY relay — a
 * replaceable list written after a partial read can drop newer entries. That rule is right.
 *
 * What was wrong is how the read was made. It went through `Relay.queryFrom`, which SKIPS a relay,
 * with no error, (A) while another read of the same slot is in flight on it — every Armada/Vector
 * community lives on the same handful of relays and is swept in the background — and (B) for up to
 * thirty minutes after it failed once. A skipped relay is a relay that never answered, so the read
 * was "incomplete" without asking anybody, and the whole new community was thrown away.
 *
 * This drives the SHIPPED concord.js mintPublicRoom through the SHIPPED relay.js queryFrom and the
 * SHIPPED cord-invite-links.js, with communities minted by the shipped PosterCord. Only sockets are
 * fake. Prints one JSON line: {baseline, busy, cooled, failing}.
 */
import fs from 'node:fs';
import vm from 'node:vm';

const read = p => fs.readFileSync(new URL(p, import.meta.url), 'utf8');
const store = new Map();
const localStorage = {
  getItem: k => (store.has(k) ? store.get(k) : null), setItem: (k, v) => store.set(k, String(v)),
  removeItem: k => store.delete(k), key: i => [...store.keys()][i] ?? null, get length() { return store.size; },
};
const noop = () => {};
const el = () => ({ dataset: {}, classList: { add: noop, remove: noop, contains: () => false, toggle: noop },
  style: {}, appendChild: noop, addEventListener: noop, removeEventListener: noop, querySelector: () => null,
  querySelectorAll: () => [], setAttribute: noop, getAttribute: () => null });
Object.assign(globalThis, {
  window: globalThis, self: globalThis, localStorage, sessionStorage: { getItem: () => null, setItem: noop, removeItem: noop },
  document: { querySelector: () => null, querySelectorAll: () => [], createElement: el, head: el(), documentElement: el(),
    body: { classList: { add: noop, remove: noop, contains: () => true, toggle: noop } }, addEventListener: noop,
    removeEventListener: noop, currentScript: null, visibilityState: 'visible' },
  location: { origin: 'https://localhost', protocol: 'https:', href: 'https://localhost/' },
});
Object.defineProperty(globalThis, 'navigator', { value: { onLine: true, userAgent: 'node' }, configurable: true });

for (const [path, name] of [['../../static/vendor/nostr/nostr.bundle.js', 'NostrTools'],
  ['../../static/js/client/cord-protocol.js', 'PosterCord'], ['../../static/js/client/cord-reader.js', 'PosterCordReader'],
  ['../../static/js/client/cord-invite-links.js', 'PCCordInviteLinks']])
  vm.runInThisContext(read(path) + `\nglobalThis.${name}=${name};`);
const NT = NostrTools;

/* THE RELAYS. They hold what is published to them and answer every REQ. `down` lists relays that
 * refuse connections; a REQ for kind 9999 is another room's sweep that stays in flight until
 * released. */
const events = [];
const relayState = { down: new Set(), held: [] };
class Socket {
  constructor(url) {
    this.url = url; this.readyState = 0;
    setTimeout(() => {
      if (this.readyState !== 0) return;
      if (relayState.down.has(url)) { this.readyState = 3; this.onerror?.(); this.onclose?.(); return; }
      this.readyState = 1; this.onopen?.();
    }, 0);
  }
  send(wire) {
    const m = JSON.parse(wire);
    if (m[0] !== 'REQ') return;
    const [, id, ...filters] = m;
    const answer = () => {
      if (this.readyState !== 1) return;
      const hits = events.filter(e => filters.some(f => (!f.kinds || f.kinds.includes(e.kind)) && (!f.authors || f.authors.includes(e.pubkey))));
      for (const e of hits) this.onmessage?.({ data: JSON.stringify(['EVENT', id, e]) });
      this.onmessage?.({ data: JSON.stringify(['EOSE', id]) });
    };
    if (filters.some(f => (f.kinds || []).includes(9999))) relayState.held.push(answer);
    else setTimeout(answer, 5);
  }
  close() { if (this.readyState === 3) return; this.readyState = 3; this.onclose?.(); }
}
globalThis.WebSocket = Socket;
globalThis.Worker = class { postMessage(m) { queueMicrotask(() => this.onmessage?.({ data: { id: m.id, ok: true,
  data: (m.args?.events || []).map(e => ({ id: e.id, valid: NT.verifyEvent(e) })) } })); } terminate() {} };
vm.runInThisContext(read('../../static/js/client/relay.js'));

const ownerSk = NT.generateSecretKey(), owner = NT.getPublicKey(ownerSk);
const conv = peer => NT.nip44.getConversationKey(ownerSk, peer);
const enc = async (peer, text) => NT.nip44.encrypt(text, conv(peer));
const dec = async (peer, text) => NT.nip44.decrypt(text, conv(peer));
const sign = async t => NT.finalizeEvent(t, ownerSk);
const published = [];
const toasts = [];
const p = {
  viewer: () => ({ pubkey: owner, profile: {} }), toast: m => toasts.push(String(m)), enc: s => String(s),
  $: () => null, isView: () => true, profOf: () => ({}), serverOrigin: () => 'https://poster.place', relayUrls: () => [],
  relayQuery: async () => [], relayQueryFrom: (u, f, o) => Relay.queryFrom(u, f, o),
  verifyRelayEvents: async e => e, signTemplate: sign, nip44enc: enc, nip44dec: dec,
  relayPublishTo: async (_urls, ev) => { events.push(ev); published.push(ev); return true; },
  relayPublishRoom: async (_urls, ev) => { events.push(ev); published.push(ev); return { ok: true }; },
  cordInviteLinksModule: async () => PCCordInviteLinks,
  cordDirectContext: () => ({ pubkey: owner, isCurrent: () => true,
    verify: async es => es.filter(e => NT.verifyEvent(e)), decrypt: dec, encrypt: enc, sign }),
};
globalThis.__PC = p;
vm.runInThisContext(read('../../static/js/client/concord.js'));
const api = window.PCConcord;
/* The relays a new community lands on: concord.js's own CORD_RELAYS (p.relayUrls() is empty here). */
const RELAYS = ['wss://jskitty.com/nostr', 'wss://asia.vectorapp.io/nostr', 'wss://nostr.computingcache.com', 'wss://relay.dreamith.to'];

const reset = () => { Relay._queryFromActive.clear(); Relay._queryFromCooldown.clear(); Relay._queryFromPurposeCooldown?.clear?.(); relayState.down.clear(); published.length = 0; toasts.length = 0; };
let lastRoom = null;
const attempt = async name => {
  lastRoom = null;
  const mark = published.length;
  try {
    const room = await api.mintPublicRoom(p, name, '');
    lastRoom = room;
    const kinds = published.slice(mark).map(e => e.kind);
    return { ok: true, created: !!(room && room.url && room.cord && room.cord.bundle), inviteListSaved: kinds.includes(13303),
      communityPublished: kinds.includes(1059), pending: String(room.inviteListPending || ''), secrets: !!room.cord?.secrets?.linkSignerSk };
  } catch (e) {
    const kinds = published.slice(mark).map(e => e.kind);
    return { ok: false, error: String(e && e.message || e), communityPublished: kinds.includes(1059) };
  }
};

/* BASELINE: nothing in flight, every relay up. */
reset();
const baseline = await attempt('Baseline');

/* A. ANOTHER ROOM'S SWEEP IS IN FLIGHT on the same relays while the community is created. */
reset();
const sweep = Relay.queryFrom(RELAYS, [{ kinds: [9999] }], { timeout: 60000, max: 8, purpose: 'concord room', allowBlocked: true });
await new Promise(r => setTimeout(r, 20));
const busy = await attempt('Busy');
for (const answer of relayState.held.splice(0)) answer();
await sweep;

/* B. ONE BAD MOMENT ON THE RADIO: every relay failed a background read once and is resting. */
reset();
for (const u of RELAYS) relayState.down.add(u);
await Relay.queryFrom(RELAYS, [{ kinds: [13302], authors: [owner] }], { timeout: 4000, max: 8, purpose: 'concord armada memberships', allowBlocked: true, failureCooldown: 1800000 });
relayState.down.clear();
const cooled = await attempt('Cooled');

/* C. ONE RELAY REALLY IS DOWN for the whole creation: it was asked and did not answer, so the list
 * read IS incomplete and must not be written over — but the community itself must not be lost. */
reset();
relayState.down.add(RELAYS[2]);
const failing = await attempt('Failing');

/* ...AND THE ADVICE IT GIVES WORKS: with the relay back, Links -> "Manage original link" (the adopt
 * path, using the key the room kept) saves the link into the Invite List. */
relayState.down.clear(); Relay._queryFromCooldown.clear();
if (lastRoom) {
  const kept = { ...lastRoom }; delete kept.inviteListPending;
  store.set('pc.concord.rooms.v1.' + owner, JSON.stringify([kept]));
  const mark = published.length, s = kept.cord.secrets;
  try {
    await api.changeOwnedInviteLink(p, kept, { token: s.token, signer_sk: s.linkSignerSk, community_id: kept.cord.bundle.community_id, url: kept.url }, { adopt: true });
    failing.recovered = published.slice(mark).some(e => e.kind === 13303);
  } catch (e) { failing.recovered = false; failing.recoverError = String(e && e.message || e); }
}

/* D. THE SAME RULE FOR AN EXISTING COMMUNITY: refreshing its owned links (after a rename/rotation)
 * reads the control registry, the Invite List and every link — with another room's sweep in flight. */
reset();
const owned = await api.mintPublicRoom(p, 'Owned', '');
delete owned.inviteListPending;
store.set('pc.concord.rooms.v1.' + owner, JSON.stringify([owned]));
const sweep2 = Relay.queryFrom(RELAYS, [{ kinds: [9999] }], { timeout: 60000, max: 8, purpose: 'concord room', allowBlocked: true });
await new Promise(r => setTimeout(r, 20));
let refresh;
try { refresh = { ok: true, refreshed: await api.refreshOwnedInviteLinks(p, JSON.parse(store.get('pc.concord.rooms.v1.' + owner))[0]) }; }
catch (e) { refresh = { ok: false, error: String(e && e.message || e) }; }
for (const answer of relayState.held.splice(0)) answer();
await sweep2;

console.log(JSON.stringify({ baseline, busy, cooled, failing, refresh }));
process.exit(0);
