/* A ROOM WHOSE PLANE KEY IS NOT HELD MUST NOT BE REBUILT ON EVERY LIVE TICK.
 *
 * Reported: "why does my desktop no longer work! posterchan not responding". The PosterChanOS desktop's
 * renderer had run nine hours and swung between 4 and 7 GB at ~180% CPU until it crashed; 18,502 of the
 * last 20,000 log lines were "Concord room subscription failed Error: Concord plane key is not held by
 * this membership" -- two rooms, retried from scratch on every 4 s tick. Drives the shipped concord.js.
 */
import fs from 'fs';
import vm from 'vm';

const src = fs.readFileSync(new URL('../../static/js/client/concord.js', import.meta.url), 'utf8');
const noop = () => {};
const hex = c => c.repeat(64);
const OWNER = hex('9');
const fail = m => { console.error('FAIL: ' + m); process.exit(1); };

function boot(reader) {
  const store = new Map();
  const localStorage = { getItem: k => (store.has(k) ? store.get(k) : null), setItem: (k, v) => store.set(k, String(v)), removeItem: k => store.delete(k) };
  const body = { classList: { add: noop, remove: noop, contains: () => false } };
  const document = { body, querySelector: () => null, querySelectorAll: () => [], createElement: () => ({ dataset: {} }),
    head: { appendChild: noop }, documentElement: { appendChild: noop }, addEventListener: noop };
  const subs = [];
  const warnings = [];
  const window = { document, addEventListener: noop, PosterCordReader: reader,
    Relay: { subscribeFrom: (relays, filters) => { subs.push(filters); return noop; } } };
  /* Concord's rooms, ledger and cursors are kept PER ACCOUNT, keyed on this signed-in viewer. */
  window.__PC = { viewer: () => ({ pubkey: OWNER }), isView: () => false, toast: noop, $: () => null };
  const context = { window, document, console: { ...console, warn: (...a) => warnings.push(a.join(' ')) }, URL, atob, btoa, crypto: {}, localStorage,
    sessionStorage: { getItem: () => null, setItem: noop }, setTimeout: () => 0, clearTimeout: noop, setInterval: () => 0, clearInterval: noop,
    TextEncoder, indexedDB: undefined, location: { href: 'https://poster.place/client' }, AbortController };
  context.globalThis = context;
  vm.runInNewContext(src, context);
  return { C: window.PCConcord, localStorage, subs, warnings };
}

const p = { viewer: () => ({ pubkey: OWNER, npub: 'npub1owner' }) };
const bundle = { community_id: hex('a'), owner: OWNER, relays: ['wss://relay.example'] };
const room = (streams) => ({ url: '', naddr: 'naddr1room', communityId: hex('a'), name: 'Monero', local: false,
  channels: [{ id: 'gen', name: 'general', private: false, streamPubkeys: streams }], cord: { bundle } });

/* ---- 1. Not held: ten ticks make ONE attempt and ONE warning, not ten. ---- */
{
  let attempts = 0;
  const reader = { inspectControl: () => ({}), createPlaneAuth: () => { attempts++; throw new Error('Concord plane key is not held by this membership'); } };
  const x = boot(reader);
  if (typeof x.C.__testStartRoomsLive !== 'function') fail('concord.js exposes no __testStartRoomsLive hook');
  x.localStorage.setItem('pc.concord.rooms.v1.'+OWNER, JSON.stringify([room([hex('b')])]));
  for (let i = 0; i < 10; i++) x.C.__testStartRoomsLive(p);
  if (attempts !== 1) fail(`an unheld room was rebuilt ${attempts} times in 10 ticks (want 1)`);
  if (x.warnings.length !== 1) fail(`${x.warnings.length} warnings in 10 ticks (want 1): ${x.warnings[0] || ''}`);

  /* ---- 2. The membership CHANGES (a rekey brings a new stream key): tried again at once. ---- */
  x.localStorage.setItem('pc.concord.rooms.v1.'+OWNER, JSON.stringify([room([hex('c')])]));
  x.C.__testStartRoomsLive(p);
  if (attempts !== 2) fail(`a changed membership was not retried (attempts=${attempts})`);
}

/* ---- 3. A room whose key IS held still subscribes, once, and stays subscribed. ---- */
{
  let attempts = 0;
  const reader = { inspectControl: () => ({}), createPlaneAuth: () => { attempts++; return null; } };
  const x = boot(reader);
  x.localStorage.setItem('pc.concord.rooms.v1.'+OWNER, JSON.stringify([room([hex('b')])]));
  for (let i = 0; i < 5; i++) x.C.__testStartRoomsLive(p);
  if (x.subs.length !== 1) fail(`a held room should subscribe exactly once, got ${x.subs.length}`);
  if (x.warnings.length) fail('a held room warned: ' + x.warnings[0]);
}
console.log('OK unheld Concord rooms are tried once per membership state, held rooms still subscribe');
