/* LEAVE AND MEMBERSHIP MUST NOT DIE ON A COMMUNITY ID THAT IS NOT A 32-BYTE KEY.
 *
 * Reported: "could not leave concord community just now, something about a 32 bit key" -- the error is
 * cordListB64's 'membership contains an invalid 32-byte key'. Drives the shipped concord.js.
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

/* ---- 1. A room with no 32-byte community id (never opened / NIP-29): Leave must not throw. --- */
{
  const x = boot();
  const bare = { url: '', naddr: 'naddr1neveropened', communityId: 'naddr1neveropened', name: 'Monero', channels: [], local: false };
  x.localStorage.setItem('pc.concord.invites', JSON.stringify([bare]));
  const before = relay.length;
  let ok;
  try { ok = await x.PC.leaveArmadaMembership(api(), bare); }
  catch (e) { fail('leaving a room with no 32-byte id threw: ' + (e && e.message)); }
  if (ok !== true) fail('leaving a room with no 32-byte id did not report success');
  if (relay.length !== before) fail('a leave with nothing to tombstone still published ' + (relay.length - before) + ' event(s)');
  if (!x.PC.wasLocallyLeft(OWNER, bare)) fail('the device did not remember leaving it, so discovery would bring it back');
}

/* ---- 2. ONE malformed entry in the stored list must not hide or block every other room. ----- */
{
  const a = boot();
  a.localStorage.setItem('pc.concord.invites', JSON.stringify([room]));
  if (!(await a.PC.persistArmadaMembership(api(), room))) fail('could not publish the membership');
  // Corrupt the newest membership document the way an older/other client might: add an entry whose
  // community_id is not a 32-byte key.
  const mine = relay.filter(ev => ev.pubkey === OWNER && typeof ev.content === 'string' && ev.content[0] === 'E');
  if (!mine.length) fail('no membership document was published to corrupt');
  const last = mine[mine.length - 1];
  const doc = JSON.parse(last.content.slice(1));
  if (!Array.isArray(doc.entries)) fail('membership document has no entries array: ' + Object.keys(doc));
  doc.entries.push({ ...doc.entries[0], community_id: 'naddr1bogus', current: { ...doc.entries[0].current, community_id: 'naddr1bogus' } });
  relay.push({ ...last, id: 'corrupt' + (++seq), created_at: (last.created_at || 0) + 5, content: 'E' + JSON.stringify(doc) });

  const b = boot();
  try { await b.PC.syncArmadaMemberships(api(), { pubkey: OWNER }); }
  catch (e) { fail('a membership sync threw on one malformed entry: ' + (e && e.message)); }
  const rooms = JSON.parse(b.localStorage.getItem('pc.concord.invites') || '[]');
  if (!rooms.some(r => r.communityId === COMMUNITY)) fail('one malformed entry hid every joined community from a fresh device');
  try { await b.PC.leaveArmadaMembership(api(), rooms.find(r => r.communityId === COMMUNITY)); }
  catch (e) { fail('one malformed entry blocked leaving a valid community: ' + (e && e.message)); }
}

console.log('concord leave bad key runtime ok');
