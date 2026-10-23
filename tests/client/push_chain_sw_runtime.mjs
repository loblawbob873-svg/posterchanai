/* THE WEB PUSH HALF OF THE PUSH CHAIN: the SHIPPED sw.js, fed the server's REAL payload bytes.
 *
 * tests/test_push_chain_server_to_android.py captures the exact `data=` the server hands pywebpush for
 * each real triggering event and passes them here (PC_PUSH_PAYLOADS = [[label, text], ...]). The whole
 * service worker is evaluated as-is; only the platform is stubbed — `self.registration
 * .showNotification` and `clients.openWindow` RECORD. For every payload this dispatches a `push` with
 * no window open (a closed PWA, the case push exists for), then a `notificationclick` on what it
 * showed, and prints {label, shown:[{title, tag, body}], opened:[url]} per line.
 *
 * PC_SW_PATH chooses which deployment the worker believes it is: '/sw.js' (the bundled app) or
 * '/client/sw.js' (the web PWA) — the click URL's base differs, the route must not.
 */
import fs from 'node:fs'; import vm from 'node:vm';
const src = fs.readFileSync(new URL('../../static/js/client/sw.js', import.meta.url), 'utf8');
const payloads = JSON.parse(process.env.PC_PUSH_PAYLOADS || '[]');
const listeners = {};
const shown = [], opened = [];
const self = {
  location: { pathname: process.env.PC_SW_PATH || '/client/sw.js', origin: 'https://poster.place', protocol: 'https:' },
  addEventListener: (t, f) => { (listeners[t] = listeners[t] || []).push(f); },
  registration: { showNotification: async (title, opts) => { shown.push({ title, opts }); } },
  skipWaiting: async () => {},
};
const clients = { matchAll: async () => [], openWindow: async (u) => { opened.push(u); return null; }, claim: async () => {} };
const ctx = { self, clients, caches: { open: async () => ({}), keys: async () => [], match: async () => null, delete: async () => true },
  fetch: async () => { throw new Error('no network in this test'); }, URL, Response, Request, Headers, console,
  setTimeout, clearTimeout, Promise, Map, Set, JSON, Date, Math, TextEncoder, TextDecoder, structuredClone };
vm.createContext(ctx);
vm.runInContext(src, ctx, { filename: 'sw.js' });
if (!listeners.push || !listeners.notificationclick) throw new Error('sw.js no longer registers push/notificationclick');
const out = [];
for (const [label, text] of payloads) {
  shown.length = 0; opened.length = 0;
  const waits = [];
  const ev = { data: { json: () => JSON.parse(text), text: () => text }, waitUntil: (p) => waits.push(p) };
  for (const f of listeners.push) f(ev);
  await Promise.all(waits);
  const cards = shown.map(s => ({ title: s.title, tag: s.opts.tag === undefined ? null : s.opts.tag, body: s.opts.body,
                                  requireInteraction: !!s.opts.requireInteraction }));
  for (const s of [...shown]) {
    const w2 = [];
    const click = { notification: { data: s.opts.data, close() {} }, waitUntil: (p) => w2.push(p) };
    for (const f of listeners.notificationclick) f(click);
    await Promise.all(w2);
  }
  out.push(JSON.stringify({ label, shown: cards, opened: [...opened] }));
}
console.log(out.join('\n'));
