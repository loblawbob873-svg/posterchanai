/* A COMMUNITY LEFT ON ONE DEVICE STAYS LEFT ON EVERY DEVICE -- even when the leaving device never opened it.
 *
 * Reported: "the chinese room I left came back on desktop" (an armada.buzz invite). A device only knows a
 * community's 32-byte id once it has opened the room (the id lives in the decrypted bundle). Leaving a room
 * this device knew only by its invite went down the "no id -> remember it on this device only" path, so the
 * account's membership vault never got a tombstone and every other device kept -- or restored -- the room.
 * Drives the shipped concord.js with one fake relay shared by three devices.
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

/* Device A joins: the account's vault now holds the membership. */
{
  const a = boot();
  a.localStorage.setItem('pc.concord.invites', JSON.stringify([room]));
  if (!(await a.PC.persistArmadaMembership(api(), room))) fail('device A could not publish the membership');
}

/* Device B knows the room ONLY by its invite (never opened here: no bundle, no community id) and leaves. */
{
  const b = boot();
  const bare = { url: INVITE, naddr: NADDR, communityId: NADDR, name: 'Soapbox', channels: [], local: false };
  b.localStorage.setItem('pc.concord.invites', JSON.stringify([bare]));
  try { await b.PC.leaveArmadaMembership(api(), bare); }
  catch (e) { fail('leaving threw: ' + (e && e.message)); }
}

/* Device C, fresh, reads the account: the community must not come back. */
{
  const c = boot();
  try { await c.PC.syncArmadaMemberships(api(), { pubkey: OWNER }); } catch (e) { fail('sync threw: ' + (e && e.message)); }
  const rooms = JSON.parse(c.localStorage.getItem('pc.concord.invites') || '[]');
  if (rooms.some(r => r.communityId === COMMUNITY || r.url === INVITE))
    fail('a community left on another device came back on a fresh one -- the leave never reached the account');
}

/* And the guard against "fixed it by forgetting everything": a community nobody left is still there. */
{
  const OTHER = hex('b'), OTHER_INVITE = `https://armada.buzz/invite/naddr1other#k3y`;
  const other = { ...room, url: OTHER_INVITE, naddr: 'naddr1other', communityId: OTHER, name: 'Other', cord: { bundle: { ...bundle, community_id: OTHER } } };
  const a = boot();
  a.localStorage.setItem('pc.concord.invites', JSON.stringify([other]));
  if (!(await a.PC.persistArmadaMembership(api(), other))) fail('could not join the second community');
  const c = boot();
  await c.PC.syncArmadaMemberships(api(), { pubkey: OWNER });
  const rooms = JSON.parse(c.localStorage.getItem('pc.concord.invites') || '[]');
  if (!rooms.some(r => r.communityId === OTHER)) fail('a community nobody left disappeared from a fresh device');
}

console.log('concord leave reaches the account runtime ok');
