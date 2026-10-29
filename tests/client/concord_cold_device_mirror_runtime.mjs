/* A COLD DEVICE READS THIS NODE'S COPY AT ONCE (2026-10-22, Firefox over Tor: "no rooms are loading"). Drives the
 * SHIPPED concord.js: with no mirror record, a room's stream read is not served by this node (it tunnels); after
 * seedMirrorFromNode asks /client/concord/mirror-state once, the same read goes to this node's relay. */
import fs from 'fs';
import vm from 'vm';
const src = fs.readFileSync(new URL('../../static/js/client/concord.js', import.meta.url), 'utf8');
const noop = () => {}, ME = 'a'.repeat(64), STREAM = 'd'.repeat(64), HOME = 'wss://poster.place/relay';
const fail = m => { console.error('FAIL: ' + m); process.exit(1); };
const store = new Map();
const localStorage = { getItem: k => (store.has(k) ? store.get(k) : null), setItem: (k, v) => store.set(k, String(v)), removeItem: k => store.delete(k) };
const document = { body: { classList: { add: noop, remove: noop, contains: () => false } }, visibilityState: 'visible', querySelector: () => null,
  querySelectorAll: () => [], createElement: () => ({ dataset: {} }), head: { appendChild: noop }, documentElement: { appendChild: noop }, addEventListener: noop };
const window = { document, addEventListener: noop, PosterCordReader: { inspectControl: () => ({}) } };
let fetches = 0, answer = { strict: true, groups: { ['b'.repeat(64)]: { authors: [STREAM], direct: [], at: 1 } } };
window.__PC = { viewer: () => ({ pubkey: ME }), isView: () => false, toast: noop, $: () => null, bumpNotif: noop, homeRelay: () => HOME,
  authFetch: async path => { fetches++; if (path !== '/client/concord/mirror-state') throw new Error(path); return { ok: true, json: async () => answer }; } };
const ctx = { window, document, console, URL, atob, btoa, crypto: {}, localStorage, sessionStorage: { getItem: () => null, setItem: noop },
  setTimeout, clearTimeout, setInterval: () => 0, clearInterval: noop, TextEncoder, indexedDB: undefined, location: { href: 'https://poster.place/client' }, AbortController };
ctx.globalThis = ctx;
vm.runInNewContext(src, ctx);
const C = window.PCConcord, p = window.__PC, read = [{ kinds: [1059], authors: [STREAM], limit: 50 }];
if (C.homeServes(p, ['wss://relay.dreamith.to'], read) !== '') fail('a cold device already trusted a mirror it never heard of');
if (C.homeRoute(p, ['wss://relay.dreamith.to'], read).h !== '') fail('asked a node that never said it answers an ask');
if (!(await C.seedMirrorFromNode(p))) fail('the node answered with a mirrored room and nothing was seeded');
if (C.homeServes(p, ['wss://relay.dreamith.to'], read) !== HOME) fail('after the node said it mirrors this stream, the read still tunnels');
if (C.homeServes(p, ['wss://relay.dreamith.to'], [{ kinds: [1059], authors: ['e'.repeat(64)] }]) !== '') fail('a stream the node does NOT mirror was sent to it');
const other = [{ kinds: [1059], authors: ['e'.repeat(64)] }], asked = C.homeRoute(p, ['wss://relay.dreamith.to'], other);
if (asked.h !== HOME || asked.prefix !== 'pcms') fail('a stream missing from this device\'s record was not ASKED of the node: ' + JSON.stringify(asked));
if (C.homeRoute(p, ['wss://relay.dreamith.to'], [{ kinds: [0], authors: [STREAM] }]).h !== '') fail('a non-stream read was sent to the mirror');
await C.seedMirrorFromNode(p);
if (fetches !== 1) fail(`asked the node ${fetches} times; once per account per page`);
console.log('concord cold device mirror: ok');
