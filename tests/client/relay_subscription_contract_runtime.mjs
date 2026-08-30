/* WHAT DOES Relay.subscribe ACTUALLY RETURN? Measured against the shipped relay.js.
 *
 * Everything downstream turns on the answer. It is a subId STRING, closed with `Relay.close(id)` —
 * it is NOT an object with a `.close()` method. concord.js nevertheless guarded both of its stop
 * paths with `typeof sub.close === 'function'`, which is false for a string every time, so the chat
 * and discovery subscriptions were never closed for the life of the page.
 *
 * The whole Concord test suite missed it because every fake `relaySubscribe` returned
 * `{close(){ …counted… }}`, and the tests then asserted on the count. The fixtures agreed with the
 * bug, so the leak was evidence there was no leak.
 *
 * So this measures the real module rather than describing it, and the Python beside it holds every
 * fake to the same shape.
 */
import fs from 'fs';
import vm from 'vm';

const src = fs.readFileSync(new URL('../../static/js/client/relay.js', import.meta.url), 'utf8');
const noop = () => {};

class FakeWS {
  constructor(u){ this.url = u; this.readyState = 1; this.sent = []; FakeWS.made.push(this); }
  send(s){ this.sent.push(JSON.parse(s)); }
  close(){ this.readyState = 3; }
}
FakeWS.made = [];

const ctx = {
  console, setTimeout, clearTimeout, setInterval, clearInterval, setImmediate,
  WebSocket: FakeWS, URL, atob, btoa,
  crypto: (await import('crypto')).webcrypto,
  localStorage: { _d: {}, getItem(k){ return this._d[k] ?? null; },
                  setItem(k, v){ this._d[k] = String(v); }, removeItem(k){ delete this._d[k]; } },
  navigator: { onLine: true },
  // relay.js builds a signature-verification Worker at load; it is not what is under test here.
  Worker: class { constructor(){} postMessage(){} addEventListener(){} terminate(){} },
  document: { addEventListener: noop },
  location: { origin: 'https://x.test', protocol: 'https:', host: 'x.test' },
};
ctx.window = ctx; ctx.self = ctx; ctx.globalThis = ctx;
vm.createContext(ctx);
vm.runInContext(src, ctx);

const R = ctx.window.Relay;
const fail = m => { throw new Error(m); };

if (!R) fail('relay.js did not publish window.Relay');
if (typeof R.subscribe !== 'function') fail('Relay.subscribe is gone');
if (typeof R.close !== 'function') fail('Relay.close is gone — nothing can end a subscription');

/* A CONNECTED pool, so the REQ and the CLOSE are really written to a socket.
 *
 * Without this `_conns` is empty, subscribe writes nothing, and the CLOSE assertion below has no
 * socket to inspect — it was originally guarded with `if (conn)` and therefore silently skipped,
 * which made it pass against a Relay.close() with the CLOSE frame deleted. A check that quietly
 * does nothing is the failure mode this whole file is about. */
R.configure({ urls: ['wss://r.example'], verify: false });
if (!FakeWS.made.length)
  fail('the fake pool never opened a socket, so the CLOSE assertion below would prove nothing');

const id = R.subscribe([{ kinds: [1] }], { onEvent: noop, live: true });

const conn0 = FakeWS.made[0];
if (!conn0.sent.some(m => m[0] === 'REQ' && m[1] === id))
  fail('subscribe() wrote no REQ for ' + id + ' to the open socket');

if (typeof id !== 'string')
  fail('Relay.subscribe returned a ' + typeof id + '. If this is now an object, every caller that '
     + 'does Relay.close(id) is broken — and the fakes in tests/client must change with it.');
if (id && typeof id.close === 'function')
  fail('the subscription handle grew a .close() method. That is fine, but concord.js and the test '
     + 'fakes both branch on exactly this, so they must be updated in the same commit.');

// It must be CLOSEABLE by the id, which is the half that was unreachable from outside app.js.
R.close(id);

/* And closing must actually stop it: a CLOSE frame goes out and the sub is forgotten, so a later
   event for that id cannot still be dispatched into a view the reader has left. */
if (!conn0.sent.some(m => m[0] === 'CLOSE' && m[1] === id))
  fail('Relay.close(id) sent no CLOSE frame for ' + id + ' — the relay keeps streaming into a '
     + 'subscription the client believes it has ended, and the per-connection subscription cap '
     + 'fills up with subs nobody is reading');

/* Closing twice, and closing something that was never open, must not throw: both stop paths in
   concord.js are called from teardown where an exception strands the rest of the cleanup. */
R.close(id);
R.close('sub-never-opened');

console.log('relay subscription contract ok: subscribe -> string, closed by Relay.close(id)');
