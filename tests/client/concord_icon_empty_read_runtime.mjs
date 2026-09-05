/* "Concord communities avatar keeps disappearing and coming back."
 *
 * `refreshRoomMetadata` runs every four seconds and its network read is throttled to sixty, so
 * most passes see only the envelope cache — which answers `[]` for a FAILURE exactly as it does
 * for a miss. `inspectControl` always returns an `icon` key and it is `""` whenever the folded
 * control state carried no metadata, so a thin pass handed `applyRoomIconMetadata` an empty icon,
 * which revoked the decrypted blob, deleted the stored pointer and SAVED that. The next complete
 * pass put it back. That is the flicker.
 *
 * Runs the shipped function, because the wipe is three lines deep inside it.
 */
import fs from 'fs';
import vm from 'vm';

const src = fs.readFileSync(new URL('../../static/js/client/concord.js', import.meta.url), 'utf8');
const noop = () => {};
const store = new Map();
const document = {
  body: { classList: { add: noop, remove: noop, contains: () => false } },
  querySelector: () => null, querySelectorAll: () => [],
  createElement: () => ({ dataset: {} }), head: { appendChild: noop },
  documentElement: { appendChild: noop }, addEventListener: noop,
};
const revoked = [];
const window = { document, addEventListener: noop, __PC: { isView: () => false, $: () => null, toast: noop } };
const context = {
  window, document, console, URL: Object.assign(function () {}, { createObjectURL: () => 'blob:new', revokeObjectURL: u => revoked.push(u) }),
  atob, btoa, crypto: {},
  localStorage: { getItem: k => (store.has(k) ? store.get(k) : null), setItem: (k, v) => store.set(k, String(v)), removeItem: k => store.delete(k) },
  sessionStorage: { getItem: () => null, setItem: noop },
  setTimeout: () => 0, clearTimeout: noop, setInterval: () => 0, clearInterval: noop,
  TextEncoder, AbortController, location: { href: 'https://poster.place/client' },
};
context.globalThis = context;
/* `new URL(...)` is used by the client for real parsing; only the two statics are stubbed. */
context.URL = URL;
context.URL.createObjectURL = () => 'blob:new';
context.URL.revokeObjectURL = u => revoked.push(u);
vm.runInNewContext(src, context);
const PC = window.PCConcord;

const fail = m => { console.error('FAIL: ' + m); process.exit(1); };
const pointer = { url: 'https://cdn.example/icon.enc', key: 'k'.repeat(64), nonce: 'n'.repeat(32), hash: 'a'.repeat(64) };
/* What `inspectControl(bundle,[])` answers: the community with NO control events folded in. A read
 * that matches it in every profile field folded no metadata at all. */
const seed = { name: 'Soapbox', description: '', icon: '' };
const thin = { name: 'Soapbox', description: '', icon: '' };

/* A room whose icon is already known — the state every joined community is in after one good read. */
const held = { name: 'Soapbox', communityId: 'c'.repeat(64), naddr: 'naddr1x', iconPointer: pointer };
if (await PC.applyRoomIconMetadata(held, thin, 'key-held', seed))
  fail('an empty control read reported an icon CHANGE, which is what gets saved');
if (!held.iconPointer) fail('an empty control read deleted the stored icon pointer');

/* Same for a plain URL icon, which is what a public community usually carries. */
const plain = { name: 'Soapbox', communityId: 'c'.repeat(64), naddr: 'naddr1x', icon: 'https://cdn.example/i.png' };
if (await PC.applyRoomIconMetadata(plain, thin, 'key-plain', seed))
  fail('an empty control read reported a change over a plain icon URL');
if (plain.icon !== 'https://cdn.example/i.png') fail('an empty control read wiped a plain icon URL');

/* A REAL icon still lands: the guard must not freeze the picture. */
const fresh = { name: 'Soapbox', communityId: 'c'.repeat(64), naddr: 'naddr1x', icon: 'https://cdn.example/i.png' };
if (!await PC.applyRoomIconMetadata(fresh, { ...thin, icon: 'https://cdn.example/new.png' }, 'key-fresh', seed))
  fail('a new icon was not applied');
if (fresh.icon !== 'https://cdn.example/new.png') fail('a new icon did not replace the old one');

/* A community that never had one gains nothing from a thin read either. (It still reports a
 * change the first time, writing the empty string it already means — harmless and pre-existing.) */
const bare = { name: 'Soapbox', communityId: 'key-bare', naddr: 'naddr1x' };
await PC.applyRoomIconMetadata(bare, thin, 'key-bare', seed);
if (bare.icon || bare.iconPointer) fail('a thin read invented an icon');

if (revoked.length) fail('an empty control read revoked a decrypted icon blob: ' + revoked.join(','));

/* A READ THAT DID FOLD METADATA STILL CLEARS. Otherwise the guard is a freeze, and an owner who
 * removes their community's picture could never take it off anybody's rail. The signal is the
 * read differing from the seed — here, a description the seed does not have. */
const cleared = { name: 'Soapbox', communityId: 'c'.repeat(64), icon: 'https://cdn.example/i.png' };
if (!await PC.applyRoomIconMetadata(cleared, { name: 'Soapbox', description: 'we moved', icon: '' }, 'key-cleared', seed))
  fail('a control read that carried real metadata could not clear the icon');
if (cleared.icon !== '') fail('a deliberate icon removal did not take');

/* And with no seed at all the caller is asserting its answer, which is the icon dialog's path. */
const asserted = { name: 'Soapbox', communityId: 'c'.repeat(64), icon: 'https://cdn.example/i.png' };
if (!await PC.applyRoomIconMetadata(asserted, { icon: '' }, 'key-asserted'))
  fail('an explicit removal with no seed was ignored');

console.log('concord icon empty-read runtime ok');
