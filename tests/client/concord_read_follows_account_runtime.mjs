/* A CONCORD MENTION READ ON ONE DEVICE IS READ ON EVERY DEVICE.
 *
 * "old concord notifications appear as new notifications when you load concord on a different device".
 * Read state was localStorage only, and a device's first read of a channel puts the last week's mentions
 * in the bell -- so each new device re-announced what had been read on the others. Drives the SHIPPED
 * notifyMentions / noteChannelRead against a fake relay holding the account's pcai:concord-read doc.
 */
import fs from 'fs';
import vm from 'vm';

const src = fs.readFileSync(new URL('../../static/js/client/concord.js', import.meta.url), 'utf8');
const noop = () => {};
const ME = 'a'.repeat(64), OTHER = 'b'.repeat(64);
const fail = m => { console.error('FAIL: ' + m); process.exit(1); };
const tick = ms => new Promise(r => setTimeout(r, ms));

function boot() {
  const store = new Map();
  const localStorage = { getItem: k => (store.has(k) ? store.get(k) : null), setItem: (k, v) => store.set(k, String(v)), removeItem: k => store.delete(k) };
  const body = { classList: { add: noop, remove: noop, contains: () => false } };
  const document = { body, visibilityState: 'visible', querySelector: () => null, querySelectorAll: () => [], createElement: () => ({ dataset: {} }),
    head: { appendChild: noop }, documentElement: { appendChild: noop }, addEventListener: noop };
  const window = { document, addEventListener: noop, PosterCordReader: { inspectControl: () => ({}) } };
  window.__PC = { isView: () => false, toast: noop, $: () => null, bumpNotif: noop };
  const context = { window, document, console, URL, atob, btoa, crypto: {}, localStorage,
    sessionStorage: { getItem: () => null, setItem: noop }, setTimeout, clearTimeout, setInterval: () => 0, clearInterval: noop,
    TextEncoder, indexedDB: undefined, location: { href: 'https://poster.place/client' }, AbortController };
  context.globalThis = context;
  vm.runInNewContext(src, context);
  return { C: window.PCConcord };
}

/* One account's relays: whatever was published is what the next device reads. `mode` lets a test make
 * the relays silent (a timeout: no EOSE) or unreachable. */
function relays() {
  const r = { events: [], mode: 'ok', published: [] };
  r.p = {
    viewer: () => ({ pubkey: ME }),
    osNotify: noop, profOf: () => ({}),
    nip44enc: async (_pk, s) => 'enc:' + s,
    nip44dec: async (_pk, s) => s.slice(4),
    signTemplate: async t => ({ ...t, id: 'id' + r.published.length, pubkey: ME }),
    relayPublish: async ev => { r.published.push(ev); r.events = [ev]; return { ok: true }; },
    relayQuery: async () => {
      if (r.mode === 'down') throw new Error('no socket');
      const out = r.mode === 'silent' ? [] : r.events.slice();
      Object.defineProperty(out, 'complete', { value: r.mode !== 'silent' });
      return out;
    },
  };
  return r;
}

const room = { url: '', naddr: 'naddr1lounge', communityId: 'c'.repeat(64), name: 'Lounge Chat', local: false, channels: [{ name: 'general' }] };
const viewer = { pubkey: ME, npub: 'npub1me', profile: { name: 'verita84' } };
const now = Date.now();
const msg = (id, at, tagged) => ({ id, at, pubkey: OTHER, by: 'mozgus', text: tagged ? 'hey @verita84' : 'hello', tags: tagged ? [['p', ME]] : [] });
const history = [msg('m1', now - 3600e3, true), msg('m2', now - 1800e3, true), msg('m3', now - 600e3, false)];

/* 1. THE REPORT. The phone read #general; the laptop opens Concord for the first time. */
{
  const net = relays();
  const phone = boot();
  await phone.C.__testReadDoc(net.p);
  phone.C.__testNoteChannelRead(net.p, room, 'general', now - 600e3);
  await tick(1800);
  if (net.published.length !== 1) fail(`reading a channel should publish the account's read marks once, got ${net.published.length}`);
  const laptop = boot();
  laptop.C.__testNotifyMentions(net.p, room, history, viewer, 'verita84', 'general');
  await laptop.C.__testReadDoc(net.p);
  if (laptop.C.mentionsUnread() !== 0) fail(`mentions read on the phone came back as new on the laptop: ${laptop.C.mentionsUnread()}`);
}

/* 2. The guard against "fixed it by never counting a first read": nothing was read anywhere, so the
 *    mentions made while no device was running still wait in the bell. */
{
  const net = relays();
  const laptop = boot();
  laptop.C.__testNotifyMentions(net.p, room, history, viewer, 'verita84', 'general');
  await laptop.C.__testReadDoc(net.p);
  if (laptop.C.mentionsUnread() !== 2) fail(`unread mentions were dropped: ${laptop.C.mentionsUnread()}`);
}

/* 3. A relay that never answered is not "no read marks": nothing is written over the account's doc. */
for (const mode of ['silent', 'down']) {
  const net = relays();
  net.events = [{ kind: 30078, pubkey: ME, created_at: 1, content: 'enc:' + JSON.stringify({ v: 1, marks: { ['x\ngeneral']: 5 } }), tags: [['d', 'pcai:concord-read']] }];
  net.mode = mode;
  const dev = boot();
  dev.C.__testNoteChannelRead(net.p, room, 'general', now);
  await dev.C.__testReadDoc(net.p);
  await tick(1800);
  if (net.published.length) fail(`${mode}: published read marks without having read the account's copy`);
}

/* 4. Two devices reading DIFFERENT channels keep both marks (each save re-reads and merges). */
{
  const net = relays();
  const a = boot(), b = boot();
  await a.C.__testReadDoc(net.p); await b.C.__testReadDoc(net.p);   // both loaded the empty doc
  a.C.__testNoteChannelRead(net.p, room, 'general', now - 10);
  await tick(1800);
  b.C.__testNoteChannelRead(net.p, room, 'support', now - 20);
  await tick(1800);
  const last = JSON.parse(net.events[0].content.slice(4)).marks;
  if (Object.keys(last).length !== 2) fail('the second device erased the first device\'s read mark: ' + JSON.stringify(last));
}

console.log('concord read follows the account: ok');
process.exit(0);
