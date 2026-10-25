/* REMOVING A STALE COPY OF A COMMUNITY NEVER LEAVES THE REAL ONE ("Posterchan Community Not Loading", Vyram 2026-10-24).
 *
 * An invite link is addressable: one link key, d="". Every invite that key ever published lives at the SAME naddr, so a
 * device can hold two rows that share it -- the community joined with today's link and a copy left over from before
 * the community was re-keyed/refounded ("PO": opens, different channels). Leaving that copy matched the account's
 * vault by the shared naddr alone, and wrote a "left" record and a vault tombstone carrying it, which every device
 * replays: leaving the stale copy left the real community for the account. Drives the shipped concord.js.
 */
import fs from 'fs';
import vm from 'vm';

const src = fs.readFileSync(new URL('../../static/js/client/concord.js', import.meta.url), 'utf8');
const noop = () => {};
const hex = c => c.repeat(64);
const OWNER = hex('9');
const COMMUNITY = hex('a');
const NADDR = 'naddr1soapbox';
const INVITE = `https://poster.place/invite/${NADDR}#fresh`, OLD_INVITE = `https://poster.place/invite/${NADDR}#before-rekey`;

/* One fake relay shared by both devices — this is the account's membership vault. */
const relay = [];
let seq = 0;

function matches(filter, ev) {
  if (filter.kinds && !filter.kinds.includes(ev.kind)) return false;
  if (filter.authors && !filter.authors.includes(ev.pubkey)) return false;
  if (filter['#d']) {
    const d = (ev.tags.find(t => t[0] === 'd') || [])[1] || '';
    if (!filter['#d'].includes(d)) return false;
  }
  return true;
}
const query = filters => relay.filter(ev => (filters || []).some(f => matches(f, ev)));

function boot() {
  const store = new Map();
  const localStorage = {
    getItem: k => (store.has(k) ? store.get(k) : null),
    setItem: (k, v) => store.set(k, String(v)),
    removeItem: k => store.delete(k),
  };
  const classes = new Set();
  const body = { classList: { add: (...c) => c.forEach(x => classes.add(x)), remove: noop, contains: c => classes.has(c) } };
  const document = {
    body,
    querySelector: () => null,
    querySelectorAll: () => [],
    createElement: () => ({ dataset: {} }),
    head: { appendChild: noop },
    documentElement: { appendChild: noop },
    addEventListener: noop,
  };
  const window = {
    document,
    addEventListener: noop,
    /* A CORD reader that accepts any bundle: this test is about membership bookkeeping, not
     * cryptography, and a throwing reader would send the sync down the invite-hydration path. */
    PosterCordReader: {
      inspectControl: () => ({ name: 'Soapbox', channels: [{ id: 'gen', name: 'general', private: false }] }),
    },
  };
  /* Concord's rooms, ledger and cursors are kept PER ACCOUNT, keyed on this signed-in viewer. */
  window.__PC = { viewer: () => ({ pubkey: OWNER }), isView: () => false, toast: noop, $: () => null };
  const context = {
    window, document, console, URL, atob, btoa, crypto: {}, localStorage,
    sessionStorage: { getItem: () => null, setItem: noop },
    setTimeout: () => 0, clearTimeout: noop, setInterval: () => 0, clearInterval: noop,
    TextEncoder, indexedDB: undefined, location: { href: 'https://poster.place/client' },
    AbortController,
  };
  context.globalThis = context;
  vm.runInNewContext(src, context);
  return { PC: window.PCConcord, localStorage, store };
}

const bundle = { community_id: COMMUNITY, owner: OWNER, owner_salt: hex('2'),root_epoch:0, community_root: hex('3'), channels: [{ id: hex('4'),key:hex('5'),epoch:0, name: 'general' }], relays: ['wss://relay.example'] };

function api() {
  return {
    viewer: () => ({ pubkey: OWNER, npub: 'npub1owner' }),
    toast: noop,
    nip44enc: async (_pk, plain) => 'E' + plain,
    nip44dec: async (_pk, ct) => {
      if (typeof ct !== 'string' || ct[0] !== 'E') throw new Error('not for us');
      return ct.slice(1);
    },
    signTemplate: async template => ({...template,id:'ev'+(++seq),sig:'f'.repeat(128)}),
    relayPublishTo: async (_relays, ev) => { relay.push(ev); return true; },
    relayQuery: async filters => query(filters),
    relayQueryFrom: async (_relays, filters) => query(filters),
    verifyRelayEvents: async e => e,
  };
}

const real = {
  url: INVITE, naddr: NADDR, communityId: COMMUNITY, name: 'PosterChan', description: '',
  channels: [{ name: 'general', private: false }], local: false, cord: { bundle },
};
const fail = m => { console.error('FAIL: ' + m); process.exit(1); };
const KEY = 'pc.concord.rooms.v1.' + OWNER;
const rooms = d => JSON.parse(d.localStorage.getItem(KEY) || '[]');

async function scenario(label, stale, staleEntryRoom) {
  relay.length = 0;
  const d = boot();
  /* The account's vault: the real community, and (scenario b) the old one the copy was joined as. */
  d.localStorage.setItem(KEY, JSON.stringify([real]));
  if (!(await d.PC.persistArmadaMembership(api(), real))) fail(label + ': could not join the real community');
  if (staleEntryRoom) {
    d.localStorage.setItem(KEY, JSON.stringify([real, staleEntryRoom]));
    if (!(await d.PC.persistArmadaMembership(api(), staleEntryRoom))) fail(label + ': could not record the old community');
  }
  d.localStorage.setItem(KEY, JSON.stringify([real, stale]));
  try { await d.PC.leaveArmadaMembership(api(), stale); } catch (e) { fail(label + ': leaving the copy threw: ' + (e && e.message)); }
  if (d.PC.wasLocallyLeft(OWNER, real)) fail(label + ': leaving the stale copy marked the REAL community as left on this device');
  const fresh = boot();
  try { await fresh.PC.syncArmadaMemberships(api(), { pubkey: OWNER }); } catch (e) { fail(label + ': sync threw: ' + (e && e.message)); }
  if (!rooms(fresh).some(r => r.communityId === COMMUNITY)) fail(label + ': leaving the stale copy left the REAL community for the account');
  if (fresh.PC.wasLocallyLeft(OWNER, real)) fail(label + ': a fresh device took the real community as left');
  return fresh;
}

/* (a) the copy was never readable here: no bundle, no id of its own, only the shared naddr. */
await scenario('undecrypted copy', { url: OLD_INVITE, naddr: NADDR, communityId: NADDR, name: 'PO', channels: [], local: false });

/* (b) the copy IS the old community (refounded since): leaving it still leaves IT, for the account. */
const OLD = hex('c');
const old = { ...real, url: OLD_INVITE, communityId: OLD, name: 'PO', cord: { bundle: { ...bundle, community_id: OLD } } };
const f = await scenario('old community', old, old);
if (rooms(f).some(r => r.communityId === OLD)) fail('the old community the user left came back on a fresh device');

console.log('concord leaving a stale copy keeps the real community runtime ok');
