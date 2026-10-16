/* A MEMBERSHIP WRITE REACHES THIS NODE'S RELAY -- the one a fresh session reads -- and a stale copy there is healed.
 *
 * "I left communities on my phone, I just logged in Firefox and I am in those communities" (2026-10-16). Measured: the
 * node's relay held the account's 10-08 membership fragment; the 14:10 one carrying the leaves existed only on the
 * outside CORD relays. Relay.publishTo skips a relay already in the managed pool unless `includeManaged` is passed,
 * and the instance relay is ALWAYS pooled -- so every join and leave went outside only (the 10-08 test asserted the
 * home relay was in the target LIST, and its fake relay stored whatever it was handed). Here the two relays are kept
 * apart and publishTo follows the real rule.
 *
*/
import fs from 'fs';
import vm from 'vm';

const src = fs.readFileSync(new URL('../../static/js/client/concord.js', import.meta.url), 'utf8');
const relaySrc = fs.readFileSync(new URL('../../static/js/client/relay.js', import.meta.url), 'utf8');
if (!/\.filter\(u => includeManaged \|\| !this\._conns\.has\(u\)\)/.test(relaySrc))
  { console.error('FAIL: relay.js publishTo no longer skips pooled relays -- re-check what this model assumes'); process.exit(1); }
const noop = () => {};
const hex = c => c.repeat(64);
const OWNER = hex('9');
const COMMUNITY = hex('a');
const NADDR = 'naddr1soapbox';
const INVITE = `https://armada.buzz/invite/${NADDR}#s3cr3t`;
const HOME = 'wss://relay.poster.place';
const relays = { home: [], outside: [] };
let seq = 0;
function matches(filter, ev) {
  if (filter.kinds && !filter.kinds.includes(ev.kind)) return false;
  if (filter.authors && !filter.authors.includes(ev.pubkey)) return false;
  if (filter['#d']) { const d = (ev.tags.find(t => t[0] === 'd') || [])[1] || ''; if (!filter['#d'].includes(d)) return false; }
  return true;
}
/* A relay keeps the newest copy of a replaceable/addressable coordinate (13302 / 33302:d). */
function store(list, ev) {
  const d = (ev.tags.find(t => t[0] === 'd') || [])[1] || '';
  const i = list.findIndex(e => e.kind === ev.kind && e.pubkey === ev.pubkey && ((e.tags.find(t => t[0] === 'd') || [])[1] || '') === d);
  if (i >= 0) { if (list[i].created_at > ev.created_at) return; list.splice(i, 1); }
  list.push(ev);
}
const read = (list, filters) => list.filter(ev => (filters || []).some(f => matches(f, ev)));
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
let clock = 1000;
function api({ outside = true } = {}) {
  return {
    viewer: () => ({ pubkey: OWNER, npub: 'npub1owner' }),
    toast: noop,
    homeRelay: () => HOME,
    relayUrls: () => [HOME, 'wss://nos.lol'],
    nip44enc: async (_pk, plain) => 'E' + plain,
    nip44dec: async (_pk, ct) => { if (typeof ct !== 'string' || ct[0] !== 'E') throw new Error('not for us'); return ct.slice(1); },
    signTemplate: async template => ({ ...template, created_at: ++clock, id: 'ev' + (++seq), sig: 'f'.repeat(128) }),
    /* Relay.publishTo's rule: the pooled home relay is skipped unless includeManaged. */
    relayPublishTo: async (urls, ev, opts) => {
      let n = 0;
      for (const u of urls || []) {
        if (u === HOME) { if (!(opts && opts.includeManaged)) continue; store(relays.home, ev); n++; }
        else if (outside) { store(relays.outside, ev); n++; }
      }
      return opts && opts.detailed ? { ok: n > 0, accepted: n, msg: n ? '' : 'no eligible relays' } : n;
    },
    relayQuery: async filters => read(relays.home, filters),                       // the pool: this node
    relayQueryFrom: async (_r, filters) => outside ? read(relays.outside, filters) : [],
    verifyRelayEvents: async e => e,
  };
}
const room = { url: INVITE, naddr: NADDR, communityId: COMMUNITY, name: 'Soapbox', description: '', channels: [{ name: 'general', private: false }], local: false, cord: { bundle } };
const fail = m => { console.error('FAIL: ' + m); process.exit(1); };
const roomsOf = d => JSON.parse(d.localStorage.getItem('pc.concord.rooms.v1.' + OWNER) || '[]');

/* The phone joins, then leaves. */
{
  const phone = boot();
  phone.localStorage.setItem('pc.concord.rooms.v1.' + OWNER, JSON.stringify([room]));
  if (!(await phone.PC.persistArmadaMembership(api(), room))) fail('could not join');
  if (!read(relays.home, [{ kinds: [33302, 13302], authors: [OWNER] }]).length) fail('the JOIN never reached this node\'s relay');
  await phone.PC.leaveArmadaMembership(api(), room);
}
/* Firefox, fresh, reading this node only (the outside relays are not asked or do not answer). */
{
  const firefox = boot();
  await firefox.PC.syncArmadaMemberships(api({ outside: false }), { pubkey: OWNER });
  if (roomsOf(firefox).some(r => r.communityId === COMMUNITY))
    fail('a community left on the phone came back in a fresh session -- the leave never reached this node\'s relay');
}
/* HEAL: this node holds an OLD copy (the join) while the outside relays hold the newer one (the leave). A device that
 * reads both hands the newer signed copy to this node, and the next fresh session is right. */
{
  relays.home.length = 0; relays.outside.length = 0;
  const a = boot();
  a.localStorage.setItem('pc.concord.rooms.v1.' + OWNER, JSON.stringify([room]));
  await a.PC.persistArmadaMembership(api(), room);                                  // home + outside: the join
  const stale = JSON.parse(JSON.stringify(relays.home));
  await a.PC.leaveArmadaMembership(api(), room);                                    // home + outside: the leave
  relays.home.length = 0; for (const e of stale) relays.home.push(e);              // home lost the leave (old clients)
  const reader = boot();
  await reader.PC.syncArmadaMemberships(api(), { pubkey: OWNER });
  await new Promise(r => setImmediate(r));
  const fresh = boot();
  await fresh.PC.syncArmadaMemberships(api({ outside: false }), { pubkey: OWNER });
  if (roomsOf(fresh).some(r => r.communityId === COMMUNITY)) fail('a stale membership copy on this node was not healed from the newer outside copy');
}
console.log('concord membership reaches this node runtime ok');
