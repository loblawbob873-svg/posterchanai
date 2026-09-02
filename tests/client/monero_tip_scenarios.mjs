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

// 1. THE SAME CLICK MUST GIVE THE SAME ANSWER, whatever the wallet's latency is.
//    This used to race the probe against a 1200ms stopwatch, so an identical click returned the
//    built-in wallet or the external flow depending on which won.
{
  const answers = [];
  /* The delays STRADDLE the 1200ms stopwatch this used to race against — below it the race is
     always won and the bug is invisible, which is exactly how the first version of this test
     passed against the very code it was written to reject. */
  for (const delay of [0, 100, 1500, 2500]) {
    const inner = mainnetWallet();
    const w = boot({ fetcher: p => new Promise(r => setTimeout(() => r(inner(p)), delay)) });
    let opened = false; w.PC.modal = () => { opened = true; };
    // Immediately — a warm cache would answer before the race could be reached.
    answers.push(await w.api.tip({ address: ADDR, name: 'x' }));
  }
  out.deterministic = { answers, allSame: answers.every(a => a === answers[0]) };
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

// 8. AN EMPTY WALLET MUST HAND THE TIP BACK to the non-custodial flow, not open a send dialog
//    that monero-wallet-rpc will refuse ("monero rejected request?").
{
  const w = boot({ fetcher: p => (p.includes('/balance')
    ? OK({ balance: '0', unlocked_balance: '0' })
    : mainnetWallet()(p)) });
  await w.api.render();
  let opened = false; w.PC.modal = () => { opened = true; };
  out.emptyWallet = { answered: await w.api.tip({ address: ADDR, name: 'x' }), opened };
}

// 9. A wallet with funds still takes the local path.
{
  const w = boot({ fetcher: p => (p.includes('/balance')
    ? OK({ balance: '2.5', unlocked_balance: '2.5' })
    : mainnetWallet()(p)) });
  await w.api.render();
  w.PC.modal = () => {};
  out.fundedWallet = { answered: await w.api.tip({ address: ADDR, name: 'x' }) };
}

// 10. Locked funds are not spendable funds — a wallet whose balance is all still locking must also
//     hand the tip back rather than have the transfer refused.
{
  const w = boot({ fetcher: p => (p.includes('/balance')
    ? OK({ balance: '2.5', unlocked_balance: '0' })
    : mainnetWallet()(p)) });
  await w.api.render();
  w.PC.modal = () => {};
  out.lockedWallet = { answered: await w.api.tip({ address: ADDR, name: 'x' }) };
}

// 11. A LOCKED balance keeps the built-in wallet — Monero locks the change from every send for 10
//     blocks, so refusing on spendable balance made the wallet stop working for ~20 minutes after
//     each tip ("you fixed android but broke webui").
{
  const w = boot({ fetcher: p => (p.includes('/balance')
    ? OK({ balance: '0.00788058', unlocked_balance: '0', blocks_to_unlock: 4 })
    : mainnetWallet()(p)) });
  await w.api.render();
  let html = ''; w.PC.modal = h => { html = h; };
  const toasts = []; w.PC.toast = t => toasts.push(String(t));
  const answered = await w.api.tip({ address: ADDR, name: 'x' });
  out.lockedKeepsWallet = { answered, opened: !!html,
                            toldWhy: toasts.some(t => /unlocks in ~8 min/.test(t)) };
}

// 12. A wallet holding NOTHING still hands the tip to the external flow — that is the one case
//     where the built-in wallet genuinely cannot help.
{
  const w = boot({ fetcher: p => (p.includes('/balance')
    ? OK({ balance: '0', unlocked_balance: '0' })
    : mainnetWallet()(p)) });
  await w.api.render();
  let opened = false; w.PC.modal = () => { opened = true; };
  out.trulyEmpty = { answered: await w.api.tip({ address: ADDR, name: 'x' }), opened };
}

// 13. A FAILED PROBE MUST NOT LATCH. `warm()` fires as the module loads, which can be before the
//     wallet session is usable — that probe 401s. If the tip trusts the cache at any age, the
//     built-in wallet is refused for the rest of the page's life even once it is reachable.
{
  let allow = false;
  const inner = mainnetWallet();
  const w = boot({ fetcher: p => {
    if (!allow) throw new Error('this account cannot open the node wallet');
    return inner(p);
  }});
  await new Promise(r => setTimeout(r, 30));      // let warm() fail, as it does on a cold page
  allow = true;                                    // the session is ready now
  let opened = false; w.PC.modal = () => { opened = true; };
  out.recoversAfterEarlyFailure = { answered: await w.api.tip({ address: ADDR, name: 'x' }), opened };
}

// 14. THE SEND SHEET OFFERS THE SAME ONE-TAP AMOUNTS as the external flow, and remembers the last.
{
  const w = boot({ fetcher: p => (p.includes('/balance')
    ? OK({ balance: '2.5', unlocked_balance: '2.5' })
    : mainnetWallet()(p)) });
  await w.api.render();
  let html = ''; w.PC.modal = h => { html = h; };
  await w.api.tip({ address: ADDR, name: 'x', presets: ['0.001', '0.01', '0.1'], amount: '0.01' });
  out.presets = {
    chips: (html.match(/data-mw-amt=/g) || []).length,
    prefilled: /id="mw-amount"[^>]*value="0\.01"/.test(html),
    labelled: /\u0271 0\.01/.test(html),
  };
}

// 15. No presets configured is not an empty row of nothing.
{
  const w = boot({ fetcher: p => (p.includes('/balance')
    ? OK({ balance: '2.5', unlocked_balance: '2.5' })
    : mainnetWallet()(p)) });
  await w.api.render();
  let html = ''; w.PC.modal = h => { html = h; };
  await w.api.tip({ address: ADDR, name: 'x' });
  out.noPresets = { chips: (html.match(/data-mw-amt=/g) || []).length,
                    row: /mw-presets/.test(html) };
}

// 16. THE USER'S OWN WALLET — tried after the node's, before the external flow.
function meWallet({ enabled = true, unlocked = '1.5', balance = '1.5', blocks = 0 } = {}) {
  return p => {
    if (p.includes('/me/status'))  return OK({ enabled, network: 'mainnet' });
    if (p.includes('/me/balance')) return OK({ address: ADDR, balance, unlocked_balance: unlocked,
                                               blocks_to_unlock: blocks, outputs: 2 });
    if (p.includes('/me/pay'))     return OK({ tx_hash_list: ['c'.repeat(64)], recipients: 1 });
    throw new Error('this account cannot open the node wallet');   // the node wallet 403s
  };
}
{
  const w = boot({ fetcher: meWallet() });
  let html = ''; w.PC.modal = (h, on) => { html = h; };
  const answered = await w.api.meTip({ address: ADDR, name: 'x', presets: ['0.01'] });
  out.userWallet = { answered, sheet: /Your PosterChan wallet/.test(html),
                     saysCustodial: /held by this server/.test(html),
                     offersExternal: /external wallet/i.test(html) };
}
// 17. A user wallet with nothing spendable hands the tip on rather than opening a dead sheet.
{
  const w = boot({ fetcher: meWallet({ unlocked: '0', balance: '0.5', blocks: 4 }) });
  const toasts = []; w.PC.toast = t => toasts.push(String(t));
  let opened = false; w.PC.modal = () => { opened = true; };
  out.userWalletLocked = { answered: await w.api.meTip({ address: ADDR, name: 'x' }), opened,
                           toldWhy: toasts.some(t => /unlocks in ~8 min/.test(t)) };
}
// 18. A node that does not offer user wallets declines silently.
{
  const w = boot({ fetcher: meWallet({ enabled: false }) });
  let opened = false; w.PC.modal = () => { opened = true; };
  out.userWalletOff = { answered: await w.api.meTip({ address: ADDR, name: 'x' }), opened };
}
// 19. A wrong-network address is refused before any sheet opens.
{
  const w = boot({ fetcher: meWallet() });
  let opened = false; w.PC.modal = () => { opened = true; };
  out.userWalletWrongNet = { answered: await w.api.meTip({ address: '5' + 'C'.repeat(94) }), opened };
}

// 20. A NON-ADMIN OPENING THE WALLET SCREEN sees THEIR wallet, not the node's refusal.
{
  const w = boot({ fetcher: p => {
    if (p.includes('/me/status'))  return OK({ enabled: true, network: 'mainnet' });
    if (p.includes('/me/balance')) return OK({ address: ADDR, balance: '0.25',
                                               unlocked_balance: '0.25', blocks_to_unlock: 0, outputs: 3 });
    // every node-wallet route 403s for a normal user
    throw new Error('this account cannot open the node wallet — sign in as the node operator (the wallet is admin-only)');
  }});
  await w.api.render();
  const h = w.feed.innerHTML;
  out.userScreen = {
    showsAdminRefusal: /node operator|admin-only/.test(h),
    showsTheirBalance: /0\.25/.test(h),
    showsAddress: h.includes(ADDR),
    hasWithdraw: /mw-me-withdraw/.test(h),
    saysCustodial: /held by this server/.test(h),
  };
}
// 21. A node with NO user wallets still shows the node wallet's own message (nothing else to show).
{
  const w = boot({ fetcher: p => {
    if (p.includes('/me/status')) return OK({ enabled: false });
    throw new Error('this account cannot open the node wallet — sign in as the node operator (the wallet is admin-only)');
  }});
  await w.api.render();
  out.noUserWallets = { fallsBackToNodeMessage: /node operator|external-wallet mode/.test(w.feed.innerHTML) };
}

// 22. A WALLET THAT NEVER ANSWERS MUST NOT EAT THE TIP. `request()` begins with
//     `ensureAiSession()`, which for a Nostr login mints a bearer THROUGH THE SIGNER and can block
//     or wait on a human approval. Reported as "i am trying to zap a post and chose Monero wallet
//     and nothing happens".
{
  const w = boot({ fetcher: mainnetWallet() });
  let never;
  w.PC.ensureAiSession = () => new Promise(r => { never = r; });   // signer never answers
  const began = Date.now();
  const answered = await w.api.tip({ address: ADDR, name: 'x' });
  out.signerHangs = { tookMs: Date.now() - began, answered };
  if (never) never();
}
{
  const w = boot({ fetcher: mainnetWallet() });
  let never2;
  w.PC.ensureAiSession = () => new Promise(r => { never2 = r; });
  const began = Date.now();
  const answered = await w.api.meTip({ address: ADDR, name: 'x' });
  out.signerHangsMe = { tookMs: Date.now() - began, answered };
  if (never2) never2();
}

// 23. NOT SIGNED IN YET: the module must not record the session refusal as the wallet's own
//     error and paint it on the wallet screen. Reported as "not even the wallet works — sign in
//     with a Nostr account to start an app session", by somebody who WAS signed in.
{
  const w = boot({ fetcher: mainnetWallet() });
  w.PC.viewer = () => ({});                                  // boot: no identity yet
  w.PC.ensureAiSession = async () => { throw new Error('sign in with a Nostr account to start an app session'); };
  await w.api.render();
  const painted = w.feed.innerHTML;
  out.beforeSignIn = {
    showsSessionError: /start an app session/.test(painted),
    showsSpinner: /class="spinner"/.test(painted),
  };
  // and once signed in, a fresh probe answers properly rather than reusing the refusal
  w.PC.viewer = () => ({ pubkey: 'a'.repeat(64) });
  w.PC.ensureAiSession = async () => {};
  await w.api.render();
  out.afterSignIn = { showsBalance: /1,500,000,000,000|1\.5/.test(w.feed.innerHTML),
                      stillShowsSessionError: /start an app session/.test(w.feed.innerHTML) };
}

// 24. THE WALLET SCREEN must not spin for ever when the signer never answers. Reported as
//     "Monero wallet is not even loading now" — render() was the one path still unbounded.
{
  const w = boot({ fetcher: mainnetWallet() });
  let never;
  w.PC.ensureAiSession = () => new Promise(r => { never = r; });
  const began = Date.now();
  await w.api.render();
  out.screenHangs = { tookMs: Date.now() - began,
                      spinner: /class="spinner"/.test(w.feed.innerHTML),
                      saysSo: /did not answer/.test(w.feed.innerHTML),
                      hasRetry: /mw-retry/.test(w.feed.innerHTML) };
  if (never) never();
}

// 25. THE SIGNER EXTENSION IS DEAD — a browser fault, not a wallet one. Taken verbatim from the
//     console: "could not establish your app session: Could not establish connection. Receiving end
//     does not exist." at __pcNostrProvider, with "theme sync skipped" failing identically.
{
  const w = boot({ fetcher: mainnetWallet() });
  w.PC.ensureAiSession = async () => {
    throw new Error('could not establish your app session: Could not establish connection. Receiving end does not exist.');
  };
  await w.api.render();
  const h = w.feed.innerHTML;
  out.signerDead = {
    namesTheSigner: /signer extension is not responding/i.test(h),
    exoneratesTheWallet: /Nothing is wrong with the wallet/i.test(h),
    hasRetry: /mw-retry/.test(h),
    spinner: /class="spinner"/.test(h),
  };
}

console.log(JSON.stringify(out));
