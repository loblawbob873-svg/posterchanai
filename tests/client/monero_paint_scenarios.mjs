/* Every scenario the alt-tab paint has to get right, run against the shipped module. Prints JSON. */
import { boot, goodWallet, OK } from './monero_paint_runtime.mjs';

const out = {};
const hasSpinner = h => h.includes('class="spinner"');
const BAL = '1,500,000,000,000';

async function duringAHangingProbe(setup) {
  const w = boot({ fetcher: p => goodWallet()(p) });
  await setup(w);
  w.advance(60000);                       // past probe()'s 8s cache — alt-tab is not instant
  let release;
  const hang = new Promise(r => { release = r; });
  w.PC.authFetch = async () => { await hang; throw new Error('daemon still reconnecting'); };
  w.feed.innerHTML = '<div class="mw-previous-view">Search messages</div>';
  const pending = w.api.render();
  await new Promise(r => setTimeout(r, 20));
  const mid = w.feed.innerHTML;
  release();
  try { await pending; } catch (_) { /* the probe is meant to fail here */ }
  return { w, mid };
}

// 1. A wallet this device has already read must come back to what it read.
{
  const { mid } = await duringAHangingProbe(w => w.api.render());
  out.known = { spinner: hasSpinner(mid), balance: mid.includes(BAL),
                previousView: mid.includes('mw-previous-view') };
}

// 2. Never read: a spinner is the honest answer, and the old view must still be gone.
{
  const { mid } = await duringAHangingProbe(async () => {});
  out.unread = { spinner: hasSpinner(mid), previousView: mid.includes('mw-previous-view') };
}

// 3. A wallet last seen as unavailable repaints its own card, not a spinner.
{
  const { mid } = await duringAHangingProbe(async w => {
    w.PC.authFetch = async () => { throw new Error('connection refused'); };
    await w.api.render();
  });
  out.unavailable = { spinner: hasSpinner(mid), card: mid.includes('Local wallet unavailable'),
                      previousView: mid.includes('mw-previous-view') };
}

// 4. The early paint is a HEAD START, never the final word: the answer still replaces it.
{
  const w = boot({ fetcher: p => goodWallet()(p) });
  await w.api.render();
  w.advance(60000);
  w.PC.authFetch = p => (p.includes('/balance')
    ? OK({ balance: '9000000000000', unlocked_balance: '9000000000000' })
    : goodWallet()(p));
  await w.api.render();
  out.refresh = { stale: w.feed.innerHTML.includes(BAL),
                  fresh: w.feed.innerHTML.includes('9,000,000,000,000') };
}

console.log(JSON.stringify(out));
