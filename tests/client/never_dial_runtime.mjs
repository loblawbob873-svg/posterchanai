/* relay.damus.io is NEVER dialled ("we should never connect to the damus relay, it's super rate-limited", 2026-10-27).
 * Drives the SHIPPED relay.js against a recording WebSocket: every path a feature uses to reach an outside relay --
 * queryFrom (with and without `allowBlocked`, the opt-out Concord and the DM inbox lookup pass), subscribeFrom and a
 * pool connect -- and prints the URLs a socket was actually opened to. */
import fs from 'node:fs';
import vm from 'node:vm';

const read = p => fs.readFileSync(new URL(p, import.meta.url), 'utf8');
const noop = () => {};
const store = new Map();
Object.assign(globalThis, {
  window: globalThis, self: globalThis,
  localStorage: { getItem: k => store.get(k) ?? null, setItem: (k, v) => store.set(k, String(v)), removeItem: k => store.delete(k) },
  sessionStorage: { getItem: () => null, setItem: noop, removeItem: noop },
  document: { querySelector: () => null, querySelectorAll: () => [], addEventListener: noop, removeEventListener: noop,
    visibilityState: 'visible', body: { classList: { add: noop, remove: noop, contains: () => true } } },
  location: { origin: 'https://localhost', protocol: 'https:', href: 'https://localhost/' },
});
Object.defineProperty(globalThis, 'navigator', { value: { onLine: true, userAgent: 'node' }, configurable: true });
const opened = [];
class Socket {
  constructor(url) {
    this.url = url; this.readyState = 0; opened.push(url);
    setTimeout(() => { if (this.readyState === 0) { this.readyState = 1; this.onopen?.(); } }, 0);
  }
  send(wire) {
    const m = JSON.parse(wire);
    if (m[0] === 'REQ') setTimeout(() => { if (this.readyState === 1) this.onmessage?.({ data: JSON.stringify(['EOSE', m[1]]) }); }, 0);
  }
  close() { if (this.readyState !== 3) { this.readyState = 3; this.onclose?.(); } }
}
globalThis.WebSocket = Socket;
globalThis.Worker = class { postMessage(m) { queueMicrotask(() => this.onmessage?.({ data: { id: m.id, ok: true, data: [] } })); } terminate() {} };
vm.runInThisContext(read('../../static/js/client/relay.js'));

const DAMUS = 'wss://relay.damus.io', OTHER = 'wss://nos.lol';
const f = [{ kinds: [10050], authors: ['a'.repeat(64)], limit: 1 }];
await Relay.queryFrom([DAMUS, OTHER], f, { timeout: 300, max: 2, exact: true, urgent: true });
await Relay.queryFrom([DAMUS + '/', OTHER], f, { timeout: 300, max: 2, exact: true, urgent: true, allowBlocked: true });
try { const s = Relay.subscribeFrom && Relay.subscribeFrom([DAMUS], f, noop); s?.close?.(); } catch (_) {}
try { Relay.connect && Relay.connect([DAMUS]); } catch (_) {}
await new Promise(r => setTimeout(r, 50));
console.log(JSON.stringify({ opened: [...new Set(opened)] }));
process.exit(0);
