/* Drives the SHIPPED static/js/client/monero-wallet.js under node with a stub PC, a stub #feed and
 * a fetch we control, so we can ask what is on screen DURING a probe that has not answered yet.
 * That instant is the whole bug and no static read of the file can see it. */
import { readFileSync } from 'node:fs';
import { runInNewContext } from 'node:vm';

const SRC = new URL('../../static/js/client/monero-wallet.js', import.meta.url);

export function boot({ fetcher }) {
  const feed = { innerHTML: '' };
  const RealDate = Date;
  let skew = 0;
  function FakeDate(...a) { return new RealDate(...a); }
  FakeDate.now = () => RealDate.now() + skew;
  FakeDate.prototype = RealDate.prototype;

  /* Elements the painted markup actually contains have to be findable, or code that fills them in
     after the paint (the sync banner) silently no-ops and a test of it proves nothing. Modelled the
     cheap way: an id present in the feed's HTML resolves to a stub element. */
  const made = new Map();
  const doc = {
    getElementById(id) {
      if (id === 'feed') return feed;
      if (!String(feed.innerHTML).includes('id="' + id + '"')) { made.delete(id); return null; }
      if (!made.has(id)) made.set(id, { id, innerHTML: '', onclick: null });
      return made.get(id);
    },
  };
  const ctx = {
    console: { log(){}, warn(){}, error(){} }, setTimeout, clearTimeout, AbortController, Promise, JSON, Math, Number, String, Object,
    Array, RegExp, isNaN, parseInt, parseFloat, Error, TypeError, URL, URLSearchParams,
    Date: FakeDate,
    document: doc,
    fetch: () => { throw new Error('bare fetch must not be used'); },
  };
  ctx.window = ctx;
  ctx.globalThis = ctx;
  ctx.__PC = {
    VIEW: 'wallet',
    $: sel => (sel === '#feed' ? feed : null),
    authFetch: (...a) => fetcher(...a),
    toast() {}, closeModal() {}, switchView() {},
  };
  runInNewContext(readFileSync(SRC, 'utf8'), ctx, { filename: 'monero-wallet.js' });
  return {
    feed,
    el: id => (made.get(id) || null),
    api: ctx.PCMoneroWallet,
    PC: ctx.__PC,
    advance(ms) { skew += ms; },
  };
}

export const OK = body => ({ ok: true, status: 200, json: async () => body,
                             headers: { get: () => 'application/json' }, text: async () => JSON.stringify(body) });

export function goodWallet() {
  return path => {
    if (path.includes('/status'))  return OK({ network: 'stagenet' });
    if (path.includes('/balance')) return OK({ balance: '1500000000000', unlocked_balance: '1500000000000' });
    if (path.includes('/address')) return OK({ address: '9wTestAddressAAAA' });
    if (path.includes('/history')) return OK({ in: [], out: [] });
    return OK({});
  };
}
