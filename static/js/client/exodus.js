/* The multi-chain wallet screen: what you hold, where to receive it, and the phrase behind it.
 *
 * THE SCREEN SAYS WHOSE KEYS THESE ARE, FIRST AND WITHOUT BEING ASKED. This wallet is custodial —
 * the node generates the seed, stores it and signs with it. A person who believes they hold their
 * own keys and does not is one server failure away from a loss they never agreed to risk, so the
 * custody line is above the balances, not in a help page, and the create button repeats it.
 *
 * "COULD NOT ASK" IS NEVER "ZERO", AND THAT DISTINCTION IS THE WHOLE UI. A balance nobody could
 * fetch and a balance of nothing look identical as digits and mean opposite things: one says a
 * provider is down, the other says your coins are gone. The server answers `known` per chain and
 * this file renders the two differently — a number, or the word unavailable. It never falls back
 * to 0, and it never hides a chain it could not read, because a missing row reads as "you don't
 * have that coin".
 */
(function(){
  const root = typeof window !== 'undefined' ? window : globalThis;
  let PC = null, booted = false;
  let _state = null, _busy = false, _seq = 0;

  const esc = (v) => (PC && PC.enc ? PC.enc(String(v == null ? '' : v)) : String(v == null ? '' : v));

  async function request(path, opts){
    if(PC && PC.ensureAiSession) await PC.ensureAiSession();
    const fetcher = (PC && PC.authFetch) ? PC.authFetch : fetch;
    const res = await fetcher(path, Object.assign({
      credentials: 'include', cache: 'no-store', headers: { Accept: 'application/json' },
    }, opts || {}));
    let body = null;
    try{ body = await res.json(); }catch(_){ body = null; }
    if(!res.ok){
      /* The server's own sentence, when it sent one. Its refusals are written to be read by a
         person — "this account already has a wallet", "that is not a valid BIP-39 recovery phrase"
         — and replacing them with a status code throws away the only useful part. */
      const why = (body && (body.detail || body.msg)) || ('HTTP ' + res.status);
      const err = new Error(String(why)); err.status = res.status; throw err;
    }
    return body || {};
  }

  const J = (o) => ({ method: 'POST', headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
                      body: JSON.stringify(o || {}) });

  /* ---------------------------------------------------------------- drawing */
  function custodyNote(){
    return `<div class="ex-custody" role="note"><b>This node holds the keys.</b> The server makes
      your recovery phrase, keeps it, and signs with it — so whoever runs this server can spend this
      wallet. Write the phrase down and treat this as a hot wallet: small amounts, nothing you
      cannot afford to lose.</div>`;
  }

  function emptyView(status){
    const lib = status && status.library === false;
    return `<div class="ex-wrap">
      <h2 class="ex-title">Wallet</h2>
      ${custodyNote()}
      ${lib ? `<div class="ex-note ex-bad">This node has not installed the wallet library, so a
                 wallet cannot be created here yet. The operator needs to run the installer's
                 dependency step.</div>`
            : `<p class="ex-lead">One recovery phrase, ${(status && status.chains || []).length} coins.
                 Create a new wallet, or restore one you already have.</p>
               <div class="ex-actions">
                 <button class="btn btn-cyan" id="ex-create">Create a wallet</button>
                 <button class="btn" id="ex-restore">Restore from a phrase</button>
               </div>
               <div class="ex-restore hidden" id="ex-restore-box">
                 <label>Recovery phrase
                   <textarea id="ex-phrase" rows="3" spellcheck="false" autocomplete="off"
                     placeholder="the twelve words, in order"></textarea></label>
                 <p class="ex-hint">Checked before anything is stored: one wrong word is a valid
                   phrase for a different wallet, and it would show you somebody else's empty
                   balance with nothing to say why.</p>
                 <button class="btn btn-cyan" id="ex-restore-go">Restore</button>
               </div>`}
      ${status && status.excluded && status.excluded.XMR
        ? `<p class="ex-hint">${esc(status.excluded.XMR)}</p>` : ''}
    </div>`;
  }

  /* WHICH CHAINS CAN ACTUALLY SEND. Receiving works everywhere; spending is implemented for the
     EVM chains only, and the button is absent rather than present-and-refusing — a control that
     always says no is a worse answer than no control. The server refuses these too, because a
     button the page hides is not a rule. */
  const SENDABLE = new Set(['ETH', 'MATIC', 'BNB', 'AVAX']);
  const canSend = (sym) => SENDABLE.has(String(sym || '').toUpperCase());

  function row(sym, name, cell){
    /* THREE STATES, NOT TWO. A chain that answered, a chain that answered nothing, and a chain
       nobody could reach — the last one must never borrow the second one's zero. */
    const known = !!(cell && cell.known);
    const amount = known ? String(cell.amount == null ? '0' : cell.amount) : '';
    return `<li class="ex-coin">
      <div class="ex-coin-id"><b>${esc(sym)}</b><small>${esc(name)}</small></div>
      <div class="ex-coin-amt">${known
        ? `<b>${esc(amount)}</b> <em>${esc(sym)}</em>`
        : `<span class="ex-unknown" title="This chain's provider could not be reached. This is not a zero balance.">unavailable</span>`}</div>
      <button class="btn small ex-receive" data-sym="${esc(sym)}">Receive</button>
      ${canSend(sym) ? `<button class="btn small ex-send" data-sym="${esc(sym)}">Send</button>` : ''}
    </li>`;
  }

  function walletView(status, balances){
    const chains = (status && status.chains) || [];
    const b = (balances && balances.balances) || {};
    const anyUnknown = chains.some(c => !(b[c.symbol] && b[c.symbol].known));
    return `<div class="ex-wrap">
      <h2 class="ex-title">Wallet${status && status.label ? ' · ' + esc(status.label) : ''}</h2>
      ${custodyNote()}
      ${status && !status.backedUp
        ? `<div class="ex-note ex-warn">You have not written down the recovery phrase yet. Without
             it, this wallet exists only on this server. <button class="btn small" id="ex-reveal-now">Show it</button></div>`
        : ''}
      <ul class="ex-coins">${chains.map(c => row(c.symbol, c.name, b[c.symbol])).join('')}</ul>
      ${anyUnknown ? `<p class="ex-hint">“unavailable” means this node could not reach that chain's
        provider just now — it does <b>not</b> mean the balance is zero.</p>` : ''}
      <div class="ex-actions">
        <button class="btn" id="ex-refresh">Refresh</button>
        <button class="btn" id="ex-reveal">Show recovery phrase</button>
      </div>
      <div id="ex-panel"></div>
    </div>`;
  }

  function receivePanel(sym, address){
    let qr = '';
    try{ if(root.PCQR && root.PCQR.svg) qr = root.PCQR.svg(address, { size: 180 }); }catch(_){ qr = ''; }
    return `<div class="ex-panel">
      <h3>Receive ${esc(sym)}</h3>
      ${qr ? `<div class="ex-qr">${qr}</div>` : ''}
      <code class="ex-addr">${esc(address)}</code>
      <div class="ex-actions">
        <button class="btn small" id="ex-copy" data-v="${esc(address)}">Copy address</button>
        <button class="btn small" id="ex-panel-close">Close</button>
      </div>
      <p class="ex-hint">Send only ${esc(sym)} to this address. Coins sent on the wrong chain are
        usually unrecoverable.</p>
    </div>`;
  }

  function sendPanel(sym, from){
    return `<div class="ex-panel">
      <h3>Send ${esc(sym)}</h3>
      <p class="ex-hint">From <code>${esc(from)}</code></p>
      <label>To <input id="ex-to" spellcheck="false" autocomplete="off" placeholder="0x…"></label>
      <label>Amount (${esc(sym)}) <input id="ex-amt" inputmode="decimal" autocomplete="off" placeholder="0.01"></label>
      <p class="ex-hint">Check the address character by character. An address one character wrong is
        still a valid address — it is simply one nobody holds the key to, and the coins are gone
        with no way back.</p>
      <div class="ex-actions">
        <button class="btn btn-cyan" id="ex-send-go">Send</button>
        <button class="btn small" id="ex-panel-close">Cancel</button>
      </div>
      <div id="ex-send-out"></div>
    </div>`;
  }

  /* ---------------------------------------------------------------- the view */
  async function render(){
    const feed = PC && PC.$ ? PC.$('#feed') : null;
    if(!feed) return;
    const mine = ++_seq;
    feed.innerHTML = '<div class="spinner"></div>';
    let status = null;
    try{ status = await request('/api/wallet/exodus/status'); }
    catch(e){
      if(mine !== _seq) return;
      feed.innerHTML = `<div class="empty">The wallet is unavailable: ${esc(e.message)}</div>`;
      return;
    }
    if(mine !== _seq) return;
    _state = status;
    if(!status.exists){ feed.innerHTML = emptyView(status); wireEmpty(); return; }

    // Paint the wallet with no balances first, then fill them in. Nine chains over the network is
    // seconds; a spinner in front of an address somebody wants to copy is the delay this avoids.
    feed.innerHTML = walletView(status, null);
    wireWallet();
    let balances = null;
    try{ balances = await request('/api/wallet/exodus/balances'); }catch(_){ balances = null; }
    if(mine !== _seq || !PC.isView || !PC.isView('exodus')) return;
    feed.innerHTML = walletView(status, balances);
    wireWallet();
  }

  function wireEmpty(){
    const $ = (s) => document.querySelector(s);
    const box = $('#ex-restore-box');
    const r = $('#ex-restore'); if(r) r.onclick = () => box && box.classList.toggle('hidden');
    const c = $('#ex-create');
    if(c) c.onclick = async () => {
      if(_busy) return;
      if(PC.uiConfirm && !await PC.uiConfirm(
        'Create a wallet on this server?\n\nThis node will generate and hold the recovery phrase. '
        + 'Whoever runs this server can spend it. Treat it as a hot wallet.', { ok: 'Create' })) return;
      _busy = true; c.disabled = true;
      try{ await request('/api/wallet/exodus/create', J({})); await render(); }
      catch(e){ PC.toast && PC.toast('wallet not created: ' + e.message); c.disabled = false; }
      finally{ _busy = false; }
    };
    const go = $('#ex-restore-go');
    if(go) go.onclick = async () => {
      if(_busy) return;
      const phrase = String(($('#ex-phrase') || {}).value || '').trim();
      if(!phrase) return PC.toast && PC.toast('paste the recovery phrase first');
      _busy = true; go.disabled = true;
      try{ await request('/api/wallet/exodus/create', J({ mnemonic: phrase })); await render(); }
      catch(e){ PC.toast && PC.toast(e.message); go.disabled = false; }
      finally{ _busy = false; }
    };
  }

  function wireWallet(){
    const $ = (s) => document.querySelector(s);
    const refresh = $('#ex-refresh'); if(refresh) refresh.onclick = () => render();
    const panel = $('#ex-panel');
    document.querySelectorAll('.ex-receive').forEach(b => b.onclick = async () => {
      const sym = b.dataset.sym;
      try{
        const got = await request('/api/wallet/exodus/addresses');
        const addr = (got.addresses || {})[sym];
        if(!addr) return PC.toast && PC.toast('no address for ' + sym);
        if(panel){
          panel.innerHTML = receivePanel(sym, addr);
          const close = $('#ex-panel-close'); if(close) close.onclick = () => { panel.innerHTML = ''; };
          const copy = $('#ex-copy');
          /* copyValue, never navigator.clipboard: the APK's WebView refuses it and so does the
             desktop's app:// origin, and this is an address somebody is about to send money to. */
          if(copy) copy.onclick = () => PC.copyValue
            ? PC.copyValue(addr, 'address copied', 'Copy the ' + sym + ' address:')
            : (PC.toast && PC.toast(addr));
        }
      }catch(e){ PC.toast && PC.toast('address unavailable: ' + e.message); }
    });
    document.querySelectorAll('.ex-send').forEach(b => b.onclick = async () => {
      const sym = b.dataset.sym;
      let from = '';
      try{ from = ((await request('/api/wallet/exodus/addresses')).addresses || {})[sym] || ''; }catch(_){ }
      if(!panel) return;
      panel.innerHTML = sendPanel(sym, from);
      const close = $('#ex-panel-close'); if(close) close.onclick = () => { panel.innerHTML = ''; };
      const go = $('#ex-send-go');
      if(go) go.onclick = async () => {
        if(_busy) return;
        const to = String(($('#ex-to') || {}).value || '').trim();
        const amount = String(($('#ex-amt') || {}).value || '').trim();
        if(!to || !amount) return PC.toast && PC.toast('an address and an amount, please');
        if(PC.uiConfirm && !await PC.uiConfirm(
          'Send ' + amount + ' ' + sym + ' to\n' + to + '?\n\nThis cannot be undone.',
          { ok: 'Send', danger: true })) return;
        _busy = true; go.disabled = true;
        const out = $('#ex-send-out');
        try{
          const res = await request('/api/wallet/exodus/send', J({ symbol: sym, to, amount }));
          /* THE 202. A send whose outcome is unknown is not a failure, and must never be offered a
             retry button: a retry is a second real payment. Say so, and stop. */
          if(res && res.unsure){
            if(out) out.innerHTML = `<div class="ex-note ex-warn">${esc(res.msg || '')}</div>`;
            return;
          }
          if(out) out.innerHTML = `<div class="ex-note">Sent. Transaction
            <code>${esc((res && res.hash) || '')}</code></div>`;
          PC.toast && PC.toast('sent');
        }catch(e){
          if(out) out.innerHTML = `<div class="ex-note ex-bad">${esc(e.message)}</div>`;
          go.disabled = false;
        }finally{ _busy = false; }
      };
    });
    const reveal = async () => {
      if(PC.uiConfirm && !await PC.uiConfirm(
        'Show the recovery phrase?\n\nAnyone who reads it can spend this wallet. Make sure nobody '
        + 'is looking at your screen.', { ok: 'Show it', danger: true })) return;
      try{
        const got = await request('/api/wallet/exodus/reveal', J({}));
        const panelEl = $('#ex-panel');
        if(panelEl) panelEl.innerHTML = `<div class="ex-panel">
          <h3>Recovery phrase</h3>
          <div class="ex-note ex-warn">${esc(got.warning || '')}</div>
          <code class="ex-phrase">${esc(got.mnemonic || '')}</code>
          <div class="ex-actions">
            <button class="btn small" id="ex-panel-close">Hide</button>
          </div></div>`;
        const close = $('#ex-panel-close'); if(close) close.onclick = () => { panelEl.innerHTML = ''; };
      }catch(e){ PC.toast && PC.toast(e.message); }
    };
    const rv = $('#ex-reveal'); if(rv) rv.onclick = reveal;
    const rn = $('#ex-reveal-now'); if(rn) rn.onclick = reveal;
  }

  function boot(){
    if(booted) return;
    PC = root.__PC; if(!PC) return setTimeout(boot, 40);
    booted = true;
  }
  boot();
  root.PCExodus = { render, _row: row, _custodyNote: custodyNote, _canSend: canSend };
})();
