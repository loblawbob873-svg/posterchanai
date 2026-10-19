/* Run the SHIPPED Relay.publishHome / publishTo against fake sockets, with this node's relay ALREADY in the pool --
 * which is the state every logged-in page is in. The private-repo bug only exists in that state: publishTo skipped
 * the pooled home relay, sent nothing, and answered 0. */
import fs from 'node:fs';
import vm from 'node:vm';
import { installStateGlobals } from './client_source.mjs';

const src = fs.readFileSync(new URL('../../static/js/client/relay.js', import.meta.url), 'utf8');
const plan = JSON.parse(process.argv[2]);
const HOME = 'wss://relay.example/';
const sockets = [];

class FakeWS {
  constructor(url) {
    this.url = url; this.readyState = 0; this.sent = [];
    this.onopen = this.onmessage = this.onclose = this.onerror = null;
    sockets.push(this);
    setTimeout(() => { this.readyState = 1; this.onopen && this.onopen(); }, 1);
  }
  send(s) {
    this.sent.push(s);
    const m = JSON.parse(s);
    if (m[0] === 'EVENT' && !plan.silent)
      setTimeout(() => this.onmessage && this.onmessage({ data: JSON.stringify(['OK', m[1].id, !plan.refuse, plan.refuse ? 'blocked: no' : '']) }), 1);
  }
  close() { this.readyState = 3; }
}

const ctx = {
  console, queueMicrotask, Promise, JSON, Math, Set, Map, Array, Object, String, Number, Date,
  WebSocket: FakeWS, setTimeout, clearTimeout, setInterval: () => 0, clearInterval: () => {},
  localStorage: { _d: {}, getItem(k){ return this._d[k] || null; }, setItem(k, v){ this._d[k] = String(v); }, removeItem(k){ delete this._d[k]; } },
  navigator: { onLine: true }, location: { origin: 'https://example.test', protocol: 'https:' },
  crypto: (await import('node:crypto')).webcrypto,
  Worker: function(){ this.postMessage = () => {}; this.terminate = () => {}; },
  addEventListener: () => {}, document: { addEventListener: () => {} },
};
ctx.window = ctx; ctx.self = ctx; ctx.globalThis = ctx;
vm.createContext(installStateGlobals(ctx) && ctx);
vm.runInContext(src, ctx);
const R = ctx.window.Relay;
// The pool as the page builds it: connectRelays() -> Relay.configure({urls, home}), home among the urls.
if (plan.pooled === 'default') R.connect(HOME);      // no personal relay list: connectRelays -> Relay.connect
else if (plan.pooled) R.configure({ urls: [HOME, 'wss://other.example/'], verify: true, home: HOME });
else R._home = HOME;
if (plan.noHome) R._home = null;
await new Promise(r => setTimeout(r, 20));
sockets.length = 0;                    // the pool's own dials are not the publish
const ev = { id: 'e'.repeat(64), pubkey: 'p'.repeat(64), kind: 30617, created_at: 1, content: '',
             tags: [['d', 'secret'], ['private', 'true']], sig: 's'.repeat(128) };
const r = plan.call === 'oldPublishTo' ? await R.publishTo([HOME], ev, { timeout: 300 })
                                       : await R.publishHome(ev, { timeout: 300 });
const delivered = sockets.filter(w => w.sent.some(s => JSON.parse(s)[0] === 'EVENT')).map(w => w.url);
process.stdout.write(JSON.stringify({ r, delivered }));
