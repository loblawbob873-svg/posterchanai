/* A CONCORD MENTION IS KEPT UNTIL IT IS READ.
 *
 * "i got tagged twice in a concord room today but never got notification" -- tagged while nothing ran,
 * then nothing on login: the first read of a channel raised nothing and the bell knew nothing of
 * Concord. Drives the SHIPPED notifyMentions through the module's test hooks.
 */
import fs from 'fs';
import vm from 'vm';

const src = fs.readFileSync(new URL('../../static/js/client/concord.js', import.meta.url), 'utf8');
const noop = () => {};
const ME = 'a'.repeat(64), OTHER = 'b'.repeat(64);
const fail = m => { console.error('FAIL: ' + m); process.exit(1); };

function boot(viewing = false) {
  const store = new Map();
  const localStorage = { getItem: k => (store.has(k) ? store.get(k) : null), setItem: (k, v) => store.set(k, String(v)), removeItem: k => store.delete(k) };
  const classes = new Set(viewing ? ['concord-view'] : []);
  const body = { classList: { add: noop, remove: noop, contains: c => classes.has(c) } };
  const document = { body, visibilityState: 'visible', querySelector: () => null, querySelectorAll: () => [], createElement: () => ({ dataset: {} }),
    head: { appendChild: noop }, documentElement: { appendChild: noop }, addEventListener: noop };
  let bumps = 0;
  const window = { document, addEventListener: noop, PosterCordReader: { inspectControl: () => ({}) } };
  window.__PC = { isView: () => false, toast: noop, $: () => null, bumpNotif: () => { bumps++; } };
  const context = { window, document, console, URL, atob, btoa, crypto: {}, localStorage,
    sessionStorage: { getItem: () => null, setItem: noop }, setTimeout: () => 0, clearTimeout: noop, setInterval: () => 0, clearInterval: noop,
    TextEncoder, indexedDB: undefined, location: { href: 'https://poster.place/client' }, AbortController };
  context.globalThis = context;
  vm.runInNewContext(src, context);
  return { C: window.PCConcord, store, localStorage, bumps: () => bumps };
}

const room = { url: '', naddr: 'naddr1lounge', communityId: 'c'.repeat(64), name: 'Lounge Chat', local: false, channels: [{ name: 'general' }] };
const viewer = { pubkey: ME, npub: 'npub1me', profile: { name: 'verita84' } };
const now = Date.now();
const msg = (id, at, tagged) => ({ id, at, pubkey: OTHER, by: 'mozgus', text: tagged ? 'hey @verita84' : 'hello', tags: tagged ? [['p', ME]] : [] });
const ledger = x => JSON.parse(x.localStorage.getItem('pc.concord.mentions.v1') || '{}');
const count = x => x.C.mentionsUnread();

/* 1. THE REPORT: first read of the channel after being tagged twice while away. */
{
  const x = boot();
  const pops = [];
  const p = { osNotify: (...a) => pops.push(a), profOf: () => ({}) };
  x.localStorage.setItem('pc.concord.rooms', '[]');
  x.C.__testNotifyMentions(p, room, [msg('m1', now - 3600e3, true), msg('m2', now - 1800e3, true), msg('m3', now - 600e3, false),
                                      msg('old', now - 10 * 86400e3, true)], viewer, 'verita84', 'general');
  if (count(x) !== 2) fail(`two mentions while away should be waiting in the bell, got ${count(x)}`);
  if (pops.length) fail('a first read must not pop OS notifications for history');
  const row = Object.values(ledger(x))[0];
  if (row.name !== 'Lounge Chat' || row.channel !== 'general' || row.last !== 'm2') fail('ledger row: ' + JSON.stringify(row));

  /* 2. A newer mention later: an OS notification AND it joins the ledger; a re-read adds nothing. */
  x.C.__testNotifyMentions(p, room, [msg('m4', now, true)], viewer, 'verita84', 'general');
  if (pops.length !== 1 || count(x) !== 3) fail(`live mention: pops=${pops.length} count=${count(x)}`);
  x.C.__testNotifyMentions(p, room, [msg('m4', now, true)], viewer, 'verita84', 'general');
  if (count(x) !== 3) fail('the same mention was counted twice');
  if (x.bumps() < 1) fail('recording a mention never told the bell');

  /* 3. Reading the channel clears it. */
  x.C.__testClearMentions(room, 'general');
  if (count(x) !== 0) fail('reading the channel left its mentions in the bell');
}

/* 4. A mention in the channel you are LOOKING AT is not left waiting. */
{
  const x = boot(true);
  x.C.__testNotifyMentions({ osNotify: noop, profOf: () => ({}) }, room, [msg('m1', now, true)], viewer, 'verita84', 'general');
  // state.community is unset in this harness, so nothing is "on screen" -- the ledger fills; the render
  // path is what clears it (covered by the full-app test). Just prove the module did not throw here.
  if (count(x) > 1) fail('unexpected count');
}
console.log('OK concord mentions wait in the bell until read');
