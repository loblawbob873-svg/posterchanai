/* ClientSettings.get() parses the settings blob ONCE, not per read -- and stays exactly as correct as before.
 *
 * Profiled on a PosterChanOS desktop (2026-10-08): get() re-read and JSON.parsed the whole 153 KB
 * `pc_nostr_settings` on every call, and DM unwrapping calls it once per message -- 419 ms of a 6 s window on
 * the page that draws the taskbar and answers the pointer. Runs the SHIPPED store.js against a localStorage
 * that counts reads. */
const fs = require('fs'), vm = require('vm'), assert = require('assert/strict');
const src = fs.readFileSync(process.env.PC_STORE_SOURCE || require('path').join(__dirname, '../../static/js/client/store.js'), 'utf8');
const data = {}; let reads = 0; const listeners = {};
const localStorage = { getItem: k => { if (k === 'pc_nostr_settings') reads++; return k in data ? data[k] : null; },
  setItem: (k, v) => { data[k] = String(v); }, removeItem: k => { delete data[k]; } };
const window = { localStorage, addEventListener: (t, f) => { (listeners[t] = listeners[t] || []).push(f); }, dispatchEvent() {} };
const ctx = { window, localStorage, console, setTimeout, clearTimeout, setInterval, clearInterval, Date, JSON, Map, Set, Math,
  Promise, structuredClone, performance: { now: () => 0 }, document: { addEventListener() {}, visibilityState: 'visible' },
  navigator: {}, location: { search: '', href: 'x' } };
ctx.self = ctx.globalThis = ctx; window.window = window;
vm.createContext(ctx); vm.runInContext(src, ctx);
const S = window.ClientSettings;
const follows = Array.from({ length: 1500 }, (_, i) => 'f'.repeat(40) + i);
data.pc_nostr_settings = JSON.stringify({ dmSeen: 100, followsCache: follows, followsSafetyCache: follows, muted: ['a'] });

reads = 0;
for (let i = 0; i < 2000; i++) assert.equal(S.get('dmSeen', 0), 100);
assert.ok(reads <= 1, `2000 reads of one setting parsed the blob ${reads} times`);

// A value handed out is the caller's to change; it must not change the next read.
const m = S.get('muted', []); m.push('b');
assert.deepEqual(S.get('muted', []), ['a'], 'a caller changing the returned list changed the stored setting');

// Our own write is visible at once.
S.set('dmSeen', 200); assert.equal(S.get('dmSeen', 0), 200);

// Another monitor's page or a window writes: the `storage` event drops the cache.
data.pc_nostr_settings = JSON.stringify({ ...JSON.parse(data.pc_nostr_settings), dmSeen: 300 });
assert.equal(S.get('dmSeen', 0), 200, 'precondition: without the event this page still holds its copy');
for (const f of listeners.storage || []) f({ key: 'pc_nostr_settings' });
assert.equal(S.get('dmSeen', 0), 300, 'a write from another page was never seen');

// A missing key still answers the default; a falsy stored value is not the default.
assert.equal(S.get('nope', 'dflt'), 'dflt');
S.set('zero', 0); assert.equal(S.get('zero', 5), 0);
console.log('settings cache: one parse per change, copies out, other pages invalidate');
