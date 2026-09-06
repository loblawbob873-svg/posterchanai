/* Run the SHIPPED Relay.subscribeFrom against a fake WebSocket that can be dropped.
 *
 * The bug it covers is invisible to any assertion about the handle: after the far end went away the
 * caller's `stop` function, `stop.ready` and `stop.hasTargets` were all exactly as they had been on
 * a healthy subscription. Only the socket was gone. So the measurement has to be "did it dial
 * again", which needs a fake that counts dials and can close on command. */
import fs from 'node:fs';
import vm from 'node:vm';

const src = fs.readFileSync(new URL('../../static/js/client/relay.js', import.meta.url), 'utf8');
const plan = JSON.parse(process.argv[2]);

const dials = [];
let now = 0;
const timers = [];   // {at, fn, id}
let nextTimer = 1;

class FakeWS {
  constructor(url) {
    this.url = url; this.readyState = 0; this.sent = [];
    this.onopen = this.onmessage = this.onclose = this.onerror = null;
    dials.push({ url, at: now });
    FakeWS.all.push(this);
    queueMicrotask(() => {
      if (this.readyState !== 0) return;
      /* A relay that is GONE refuses the connection: error then close, never open. That is the
         case the backoff exists for, and a fake that always connects cannot produce it. */
      if (FakeWS.dead) { this.onerror && this.onerror(); this.close(); return; }
      this.readyState = 1; this.onopen && this.onopen();
    });
  }
  send(s) { this.sent.push(s); }
  close() { if (this.readyState === 3) return; this.readyState = 3; this.onclose && this.onclose(); }
  drop() { this.close(); }          // the far end goes away
}
FakeWS.all = [];
FakeWS.dead = false;

const ctx = {
  console, queueMicrotask, Promise, JSON, Math, Set, Map, Array, Object, String, Number, Date,
  WebSocket: FakeWS,
  setTimeout: (fn, ms) => { const id = nextTimer++; timers.push({ at: now + (ms || 0), fn, id }); return id; },
  clearTimeout: (id) => { const i = timers.findIndex(t => t.id === id); if (i >= 0) timers.splice(i, 1); },
  setInterval: () => 0, clearInterval: () => {},
  localStorage: { _d: {}, getItem(k){ return this._d[k] || null; },
                  setItem(k, v){ this._d[k] = String(v); }, removeItem(k){ delete this._d[k]; } },
  navigator: { onLine: true }, location: { origin: 'https://example.test', protocol: 'https:' },
  crypto: (await import('node:crypto')).webcrypto,
  Worker: function(){ this.postMessage = () => {}; this.onmessage = null; this.terminate = () => {}; },
  addEventListener: () => {}, document: { addEventListener: () => {} },
};
ctx.window = ctx; ctx.self = ctx; ctx.globalThis = ctx;
vm.createContext(ctx);
vm.runInContext(src, ctx);

// Advance the fake clock, firing whatever is due.
const advance = async (ms) => {
  const end = now + ms;
  for (;;) {
    const due = timers.filter(t => t.at <= end).sort((a, b) => a.at - b.at)[0];
    if (!due) break;
    timers.splice(timers.indexOf(due), 1);
    now = due.at;
    due.fn();
    await new Promise(r => queueMicrotask(r));
  }
  now = end;
  await new Promise(r => queueMicrotask(r));
};

const stop = ctx.window.Relay.subscribeFrom(['wss://room.example'], [{ kinds: [1059] }],
  { onEvent: () => {}, timeout: 0, live: !!plan.live });
await new Promise(r => queueMicrotask(r));

if (plan.deadRelay) FakeWS.dead = true;
if (plan.stopFirst) stop();
if (plan.drop) FakeWS.all.filter(w => w.readyState !== 3).forEach(w => w.drop());
await advance(plan.advanceMs || 0);
if (plan.stopAfter) { stop(); await advance(60000); }

process.stdout.write(JSON.stringify({
  dials: dials.length,
  reqs: FakeWS.all.reduce((n, w) => n + w.sent.length, 0),
  sockets: FakeWS.all.length,
  openSockets: FakeWS.all.filter(w => w.readyState === 1).length,
  pendingTimers: timers.length,
}));
