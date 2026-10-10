/* Leaving a NIP-29 group must stick. */
import fs from 'fs';
import vm from 'vm';

const src = fs.readFileSync(new URL('../../static/js/client/concord.js', import.meta.url), 'utf8');
const noop = () => {};
const hex = c => c.repeat(64);
const OWNER = hex('9');
const COMMUNITY = hex('a');
const NADDR = 'naddr1soapbox';
const INVITE = `https://armada.buzz/invite/${NADDR}#s3cr3t`;

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
  /* Concord's rooms are kept PER ACCOUNT, keyed on this signed-in viewer. */
  window.__PC = { viewer: () => ({ pubkey: VIEW }), isView: () => false, toast: noop, $: () => null };
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


/* "I just left a community, it goes away in communities, then comes back."
 *
 * A NIP-29 group has no vault entry to tombstone, so leaving it is recorded only in this device's
 * left-ledger -- and the NIP-29 membership pass, which runs on a 60/120 s timer, re-added every group
 * the account's kind-10009 list still names without asking that ledger. Gone, then back a minute later.
 */
const VIEW = hex('7');
const GROUP_RELAY = 'wss://groups.example';
const GROUP = 'lounge';
const identity = 'nip29:' + GROUP_RELAY + '#' + GROUP;
relay.push({ id: 'list1', kind: 10009, pubkey: VIEW, created_at: 100, content: '',
             tags: [['group', GROUP, GROUP_RELAY, 'Lounge Chat'], ['group', 'other', GROUP_RELAY, 'Other']] });
const fail = m => { console.error('FAIL: ' + m); process.exit(1); };
function api() {
  return {
    viewer: () => ({ pubkey: VIEW, npub: 'npub1view' }), toast: noop,
    nip44enc: async (_pk, plain) => 'E' + plain, nip44dec: async () => { throw new Error('none'); },
    signTemplate: async t => ({ ...t, pubkey: VIEW, id: 'ev' + (++seq), sig: 'f'.repeat(128) }),
    relayPublishTo: async (_r, ev) => { relay.push(ev); return true; },
    relayQuery: async filters => query(filters),
    relayQueryFrom: async (_r, filters) => query(filters),
    verifyRelayEvents: async e => e,
  };
}
const a = boot();
const rooms = () => JSON.parse(a.localStorage.getItem('pc.concord.rooms.v1.'+VIEW) || '[]');
await a.PC.syncNip29Memberships(api(), { pubkey: VIEW });
if (!rooms().some(r => r.communityId === identity)) fail('the membership pass did not add the listed group at all');
const room = rooms().find(r => r.communityId === identity);
await a.PC.leaveArmadaMembership(api(), room);
a.localStorage.setItem('pc.concord.rooms.v1.'+VIEW, JSON.stringify(a.PC.removeCommunityByIdentity(rooms(), identity).rooms));
if (rooms().some(r => r.communityId === identity)) fail('leave did not remove the group');
/* the next tick of the membership timer */
await a.PC.syncNip29Memberships(api(), { pubkey: VIEW });
if (rooms().some(r => r.communityId === identity)) fail('the left group came back on the next NIP-29 membership pass');
if (!rooms().some(r => r.communityId === 'nip29:' + GROUP_RELAY + '#other')) fail('the group that was NOT left disappeared');
/* Another device -- empty storage, same account -- reads the list and must not get it back either. */
const b = boot();
await b.PC.syncNip29Memberships(api(), { pubkey: VIEW });
const bRooms = JSON.parse(b.localStorage.getItem('pc.concord.rooms.v1.'+VIEW) || '[]');
if (bRooms.some(r => r.communityId === identity)) fail('another device re-added the group: the account list still names it');
if (!bRooms.some(r => r.communityId === 'nip29:' + GROUP_RELAY + '#other')) fail('the list rewrite dropped a group that was NOT left');
/* No list readable -> nothing written (an empty read rewritten would drop every group). */
const before = relay.length;
const c = boot();
relay.splice(0, relay.length);
await c.PC.leaveArmadaMembership(api(), { protocol: 'nip29', communityId: 'nip29:wss://x.example#y', naddr: 'nip29:wss://x.example#y', groupId: 'y', relay: 'wss://x.example' });
if (relay.length) fail('a leave with no readable list published one anyway');
console.log('concord nip29 leave runtime ok');
