/* "could not refresh room history: no channels readable yet — 8 relay(s) asked, 0 control event(s)
 * held, 0 new" — REPRODUCED, and it was a room that asked NOBODY.
 *
 * Reported by the operator from the app (the PosterChan community) while the PosterChan bot, a
 * member, read the same community perfectly on the server. Measured on the operator's desktop: the
 * stored join bundle is byte-identical to what its own invite opens to, and every one of its relays
 * serves the control stream it decrypts into channels. The membership was fine. The READ never went
 * out.
 *
 * `Relay.queryFrom` deliberately SKIPS a relay in two situations and returns `[]` for it, with no
 * error: (1) another read of the same slot is still in flight on that relay (`_queryFromActive`),
 * and (2) the relay failed recently and is resting (`_queryFromCooldown`, 30 MINUTES for Concord).
 * For a member of a split-control community (every community this client mints) the control plane
 * has no AUTH key, so its slot is the BARE RELAY URL — shared with every other room on the same
 * relays, i.e. every Armada/Vector community, all of which live on the same handful of relays and
 * are swept in the background. So:
 *
 *   A. another community's read is in flight on those relays when this room opens -> every relay
 *      is skipped -> zero control events -> "no channels readable yet — 8 relay(s) asked";
 *   B. one bad moment on a phone's radio (every relay failed once) -> every relay rests for thirty
 *      minutes -> every open of the room in that half hour asks nobody, gets nothing, and says it
 *      asked eight relays.
 *
 * The bot never sees either: it is one process reading one room on a server's wired network. A
 * browser with an on-device control cache never sees it either, because the cache paints the
 * channels before the network is asked. A fresh install / a cleared WebView does.
 *
 * This drives the SHIPPED concord.js hydrateRoomStreams through the SHIPPED relay.js queryFrom
 * against a fake WebSocket, with a REAL community minted by the shipped PosterCord and read by the
 * shipped PosterCordReader. Only the sockets are fake.
 *
 * Prints one JSON line: {baseline, busy, cooled, rotated}, each {ok, error, hydrated}; cooled.first is the
 * message the room gave while the radio was really down.
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
  ['../../static/js/client/cord-protocol.js', 'PosterCord'], ['../../static/js/client/cord-reader.js', 'PosterCordReader']])
  vm.runInThisContext(read(path) + `\nglobalThis.${name}=${name};`);
const NT = NostrTools;

/* THE RELAYS. Every one of them holds the communities' events and answers every REQ — after a
 * delay, so a read can be in flight while another one starts. `down` refuses connections. */
const events = [];
const relayState = { down: false, delay: 0 };
class Socket {
  constructor(url) {
    this.url = url; this.readyState = 0;
    setTimeout(() => {
      if (this.readyState !== 0) return;
      if (relayState.down) { this.readyState = 3; this.onerror?.(); this.onclose?.(); return; }
      this.readyState = 1; this.onopen?.();
    }, 0);
  }
  send(wire) {
    const m = JSON.parse(wire);
    if (m[0] !== 'REQ') return;
    const [, id, ...filters] = m;
    const hits = events.filter(e => filters.some(f => (!f.kinds || f.kinds.includes(e.kind)) && (!f.authors || f.authors.includes(e.pubkey))
      && (f.until == null || e.created_at <= f.until)));
    setTimeout(() => {
      if (this.readyState !== 1) return;
      for (const e of hits) this.onmessage?.({ data: JSON.stringify(['EVENT', id, e]) });
      this.onmessage?.({ data: JSON.stringify(['EOSE', id]) });
    }, relayState.delay);
  }
  close() { if (this.readyState === 3) return; this.readyState = 3; this.onclose?.(); }
}
globalThis.WebSocket = Socket;
globalThis.Worker = class { postMessage(m) { queueMicrotask(() => this.onmessage?.({ data: { id: m.id, ok: true,
  data: (m.args?.events || []).map(e => ({ id: e.id, valid: NT.verifyEvent(e) })) } })); } terminate() {} };
vm.runInThisContext(read('../../static/js/client/relay.js'));

/* TWO REAL COMMUNITIES on the same eight relays, as every Armada/Vector community is. */
const RELAYS = Array.from({ length: 8 }, (_, i) => `wss://r${i}.fixture`);
const ownerSk = NT.generateSecretKey(), owner = NT.getPublicKey(ownerSk), sign = e => NT.finalizeEvent(e, ownerSk);
const memberSk = NT.generateSecretKey(), member = NT.getPublicKey(memberSk);
async function community(name) {
  const made = await PosterCord.createCommunity({ name, owner, relays: RELAYS.slice(0, 5), base: 'https://poster.place', signEvent: sign });
  events.push(...made.events.filter(e => e.kind === 1059));
  /* The member's stored bundle names all eight relays (the vault merge unions relay lists). */
  const bundle = { ...PosterCord.openInvite(made.url, made.events).bundle, relays: RELAYS };
  return { name, communityId: made.communityId, naddr: made.url.split('/invite/')[1].split('#')[0], url: made.url, local: false,
    channels: [{ name: 'general', private: false }], cord: { bundle } };
}
const posterchan = await community('PosterChan'), other = await community('Another room on the same relays');
const ROOMS_KEY = 'pc.concord.rooms.v1.' + member, fresh = () => store.set(ROOMS_KEY, JSON.stringify([posterchan, other]));
fresh();

const toasts = [];
const p = {
  viewer: () => ({ pubkey: member, profile: {} }), toast: m => toasts.push(String(m)), enc: s => String(s),
  $: () => null, isView: () => true, profOf: () => ({}),
  relayQuery: async () => [], relayQueryFrom: (u, f, o) => Relay.queryFrom(u, f, o),
  verifyRelayEvents: async e => e, signTemplate: async e => NT.finalizeEvent(e, memberSk),
};
globalThis.__PC = p;
vm.runInThisContext(read('../../static/js/client/concord.js'));
const api = window.PCConcord;
const hydrated = name => !!((JSON.parse(store.get(ROOMS_KEY)).find(r => r.name === name) || {}).cord || {}).hydrated;
const attempt = async (index, identity) => {
  try { await api.hydrateRoomStreams(p, index, identity); return { ok: true }; }
  catch (e) { return { ok: false, error: String(e && e.message || e) }; }
};
const settle = ms => new Promise(r => setTimeout(r, ms));

/* BASELINE: nothing else in flight, every relay up — the fixture room IS readable. */
const baseline = await attempt(0, 'posterchan-baseline');
baseline.hydrated = hydrated('PosterChan');
fresh(); Relay._queryFromActive.clear(); Relay._queryFromCooldown.clear();

/* A. ANOTHER COMMUNITY'S READ IS IN FLIGHT ON THE SAME RELAYS when the PosterChan room opens. */
relayState.delay = 1500;
const busyOther = attempt(1, 'other');
await settle(50);
const busy = await attempt(0, 'posterchan');
await busyOther;
busy.hydrated = hydrated('PosterChan');

/* B. ONE BAD MOMENT ON THE RADIO, then the radio is back. */
fresh(); Relay._queryFromActive.clear(); Relay._queryFromCooldown.clear();
relayState.delay = 0; relayState.down = true;
const firstCooled = await attempt(0, 'posterchan-cold');
relayState.down = false;
const cooled = await attempt(0, 'posterchan-cold');
cooled.first = firstCooled.error || '';
cooled.hydrated = hydrated('PosterChan');

/* C. THE RELAYS ANSWER, AND WHAT THEY HOLD DOES NOT OPEN WITH THIS MEMBERSHIP — a stored bundle whose
 * root no longer matches the community's (a rotation this device never applied, or a merge from a
 * stale copy). This one is NOT a transport problem and must not be described as one. */
Relay._queryFromActive.clear(); Relay._queryFromCooldown.clear();
const stale = { ...posterchan, name: 'Stale', communityId: 'stale', naddr: 'stale',
  cord: { bundle: { ...posterchan.cord.bundle, community_root: 'ab'.repeat(32) } } };
store.set(ROOMS_KEY, JSON.stringify([stale]));
const rotated = await attempt(0, 'stale');

console.log(JSON.stringify({ baseline, busy, cooled, rotated }));
process.exit(0);
