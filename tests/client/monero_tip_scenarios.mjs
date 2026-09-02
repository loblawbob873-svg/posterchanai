/* Does tapping ɱ wait on a wallet that is busy scanning? Drives the SHIPPED tip(). Prints JSON. */
import { boot, goodWallet, OK } from './monero_paint_runtime.mjs';

const ADDR = '4' + 'A'.repeat(94);          // 95 chars, mainnet prefix
const out = {};

function mainnetWallet() {
  return path => {
    if (path.includes('/status'))  return OK({ network: 'mainnet' });
    if (path.includes('/balance')) return OK({ balance: '1.5', unlocked_balance: '1.5' });
    if (path.includes('/address')) return OK({ address: ADDR });
    if (path.includes('/history')) return OK({ in: [], out: [] });
    return OK({});
  };
}

// 1. A WALLET THAT NEVER ANSWERS must not hold up the non-custodial flow.
{
  const w = boot({ fetcher: mainnetWallet() });
  let release;
  const hang = new Promise(r => { release = r; });
  w.PC.authFetch = async () => { await hang; throw new Error('scanning'); };
  const began = Date.now();
  const answered = await w.api.tip({ address: ADDR, name: 'x' });
  out.slow = { tookMs: Date.now() - began, answered };
  release();
}

// 2. A wallet this page already read answers immediately, with no request at all.
{
  const w = boot({ fetcher: mainnetWallet() });
  await w.api.render();                       // fills the cache
  let asked = 0;
  const inner = mainnetWallet();
  w.PC.authFetch = p => { asked++; return inner(p); };
  let opened = false;
  w.PC.modal = () => { opened = true; };
  const began = Date.now();
  const answered = await w.api.tip({ address: ADDR, name: 'x' });
  out.cached = { tookMs: Date.now() - began, answered, requests: asked };
}

// 3. A wallet on the WRONG network must still refuse, so the URI flow takes over.
{
  const w = boot({ fetcher: p => (p.includes('/status') ? OK({ network: 'stagenet' })
                                                       : mainnetWallet()(p)) });
  await w.api.render();
  out.wrongNetwork = { answered: await w.api.tip({ address: ADDR, name: 'x' }) };
}

// 4. A BUSY wallet paints "catching up", not "external-wallet mode".
{
  const w = boot({ fetcher: mainnetWallet() });
  w.PC.authFetch = async () => { throw new Error('Local Monero wallet is busy — it is still reading the chain'); };
  await w.api.render();
  out.busyCard = { syncing: w.feed.innerHTML.includes('mw-syncing'),
                   catching: w.feed.innerHTML.includes('catching up'),
                   external: w.feed.innerHTML.includes('external-wallet mode') };
}

// 5. A genuinely absent wallet still says external-wallet mode.
{
  const w = boot({ fetcher: mainnetWallet() });
  w.PC.authFetch = async () => { throw new Error('Local Monero wallet is unavailable'); };
  await w.api.render();
  out.absentCard = { external: w.feed.innerHTML.includes('external-wallet mode'),
                     syncing: w.feed.innerHTML.includes('mw-syncing') };
}

// 6. The sync question is asked ONCE per visit, not once per paint.
{
  const w = boot({ fetcher: mainnetWallet() });
  let syncCalls = 0;
  const inner = mainnetWallet();
  w.PC.authFetch = p => {
    if (p.includes('/sync')) { syncCalls++; return OK({ checked: true, scanning: true, blocks_fetched: 9 }); }
    return inner(p);
  };
  await w.api.render();                       // paints twice: cached, then fresh
  await new Promise(r => setTimeout(r, 60));
  await w.api.render();                       // and again, as _watch would
  await new Promise(r => setTimeout(r, 60));
  const note = w.el('mw-sync');
  out.syncCalls = { calls: syncCalls,
                    banner: !!(note && note.innerHTML.includes('catching up')) };
}

// 7. A CLIENT-SIDE abort must read as busy too — the node's own timeout is configurable past ours.
{
  const w = boot({ fetcher: mainnetWallet() });
  w.PC.authFetch = async () => { throw new Error('the wallet did not answer within 20s: /api/wallet/xmr/status'); };
  await w.api.render();
  out.abortCard = { syncing: w.feed.innerHTML.includes('mw-syncing'),
                    external: w.feed.innerHTML.includes('external-wallet mode') };
}

console.log(JSON.stringify(out));
