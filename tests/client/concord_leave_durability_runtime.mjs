/* LEAVING A CONCORD COMMUNITY MUST SURVIVE THE NEXT DEVICE.
 *
 * Reported twice: "make sure you can leave communities! on laptop, i loaded Communities and it
 * brought me back to Soapbox which I left many times". Leaving publishes a tombstone into the
 * Armada membership vault (kind 13302), which is durable and account-wide — but the OWNER of a
 * community also announces its invite as a public kind-1, and discovery replays that announcement
 * on every reconnect. `recoverOwnedInvite` turns it back into a joined room, and the only thing
 * that ever stopped it was a LOCALSTORAGE ledger, which a second device does not have.
 *
 * This runtime drives the shipped client twice over one fake relay: device A joins and leaves,
 * device B (fresh storage, same account) syncs the vault and then receives the owner's own
 * announcement. Device B must stay out.
 */
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
  window.__PC = { isView: () => false, toast: noop, $: () => null };
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

const bundle = { owner: OWNER, community_root: hex('3'), channels: [{ id: 'gen', name: 'general' }], relays: ['wss://relay.example'] };

function api() {
  return {
    viewer: () => ({ pubkey: OWNER, npub: 'npub1owner' }),
    toast: noop,
    nip44enc: async (_pk, plain) => 'E' + plain,
    nip44dec: async (_pk, ct) => {
      if (typeof ct !== 'string' || ct[0] !== 'E') throw new Error('not for us');
      return ct.slice(1);
    },
    publish: async (kind, content, tags) => ({
      ev: { id: 'ev' + (++seq), kind, pubkey: OWNER, created_at: 1000 + seq, tags: tags || [], content },
    }),
    relayPublishTo: async (_relays, ev) => { relay.push(ev); return true; },
    relayQuery: async filters => query(filters),
    relayQueryFrom: async (_relays, filters) => query(filters),
    verifyRelayEvents: async e => e,
  };
}

const room = {
  url: INVITE, naddr: NADDR, communityId: COMMUNITY, name: 'Soapbox', description: '',
  channels: [{ name: 'general', private: false }], local: false,
  cord: { bundle },
};

/* The owner's own public announcement of the invite — exactly what discovery replays. */
const announcement = {
  url: INVITE, naddr: NADDR, secret: 's3cr3t', name: 'Soapbox', description: 'Soapbox',
  source: { pubkey: OWNER, created_at: 900, content: 'join ' + INVITE },
};

const fail = m => { console.error('FAIL: ' + m); process.exit(1); };

/* ---- Device A: join, then leave. -------------------------------------------------------- */
const a = boot();
a.localStorage.setItem('pc.concord.invites', JSON.stringify([room]));
if (!(await a.PC.persistArmadaMembership(api(), room))) fail('device A could not publish its membership');
await a.PC.leaveArmadaMembership(api(), room);
if (!relay.length) fail('leaving published nothing to the membership vault');
/* What the Leave button does after the publish returns. */
a.localStorage.setItem('pc.concord.invites',
  JSON.stringify(a.PC.removeCommunityByIdentity(JSON.parse(a.localStorage.getItem('pc.concord.invites')), COMMUNITY).rooms));
if (JSON.parse(a.localStorage.getItem('pc.concord.invites')).length) fail('leave did not remove the room locally');

/* Device A itself must stay out, including against its own replayed announcement. */
a.PC.recoverOwnedInvite(api(), { ...announcement });
if (JSON.parse(a.localStorage.getItem('pc.concord.invites')).some(r => r.naddr === NADDR))
  fail('device A re-joined from its own announcement after leaving');

/* ---- Device B: a second device on the same account, empty local storage. ----------------- */
const b = boot();
await b.PC.syncArmadaMemberships(api(), { pubkey: OWNER });
let rooms = JSON.parse(b.localStorage.getItem('pc.concord.invites') || '[]');
if (rooms.length) fail('the vault tombstone did not keep the left community off a fresh device');

/* Discovery replays the owner's own kind-1 announcement. THIS is the resurrection. */
b.PC.recoverOwnedInvite(api(), { ...announcement });
rooms = JSON.parse(b.localStorage.getItem('pc.concord.invites') || '[]');
if (rooms.length) fail('the owner\'s own announcement re-joined a community the account had left');

/* …and a membership pass after it must not leave one behind either. */
await b.PC.syncArmadaMemberships(api(), { pubkey: OWNER });
rooms = JSON.parse(b.localStorage.getItem('pc.concord.invites') || '[]');
if (rooms.length) fail('a membership sync kept a room resurrected from an announcement (' + JSON.stringify(rooms.map(r => r.naddr)) + ')');

/* ---- Rejoining must still work, and must survive the tombstone that is still in the vault. */
const c = boot();
c.localStorage.setItem('pc.concord.invites', JSON.stringify([room]));
if (!(await c.PC.persistArmadaMembership(api(), room))) fail('re-joining could not publish membership');
const d = boot();
await d.PC.syncArmadaMemberships(api(), { pubkey: OWNER });
rooms = JSON.parse(d.localStorage.getItem('pc.concord.invites') || '[]');
if (!rooms.some(r => r.communityId === COMMUNITY))
  fail('a deliberate re-join was swallowed by the old tombstone');

console.log('concord leave durability runtime ok');
