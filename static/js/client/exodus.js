/* CloudOS multi-chain wallets: scoped balances, recovery backups and transfer status.
 * Unknown balances stay distinct from zero. Wallet/account changes invalidate late responses.
 */
(function(){
  const root = typeof window !== 'undefined' ? window : globalThis;
  let PC = null, booted = false;
  let _state = null, _busy = false, _seq = 0, _wallets = [], _balances = null;
  let _selected = {wallet:'default',portfolio:0}, _scope = '', _range = '7d';
  let _balanceTimer = null;

  const esc = (v) => String(v == null ? '' : v).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));

  async function request(path, opts){
    const selected = {..._selected}, scope = accountScope();
    if(PC && PC.ensureAiSession) await PC.ensureAiSession();
    if(scope !== accountScope()) throw new Error('The active account changed');
    if(!path.endsWith('/wallets')) path += (path.includes('?')?'&':'?') + 'wallet=' + encodeURIComponent(selected.wallet) + '&portfolio=' + selected.portfolio;
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

  function accountScope(){
    return String(root.__PC_API_BASE__ || root.location?.origin || '') + ':' + (PC?.viewer?.()?.pubkey || '');
  }
  function current(generation){ return generation===_seq && _scope===accountScope() && (!PC?.isView || PC.isView('exodus')); }
  function money(value){
    if(value == null || !/^\d+(?:\.\d+)?$/.test(String(value))) return '—';
    const [whole, fraction=''] = String(value).split('.');
    return '$' + BigInt(whole).toLocaleString('en-US') + '.' + (fraction+'00').slice(0,2);
  }
  function coinLogo(symbol){
    const key=String(symbol).toLowerCase();
    return /^[a-z]{2,5}$/.test(key) ? `<img class="ex-asset-logo" src="/static/vendor/cryptocurrency-icons/${key}.svg" width="34" height="34" alt="">` : '';
  }
  function walletHeader(status){
    const choices=_wallets.length?_wallets:[{id:_selected.wallet,name:status?.label||'Main wallet'}];
    const portfolios=status?.portfolios?.length?status.portfolios:[{id:0,name:'Main portfolio'}];
    return `<header class="ex-header"><div><span class="ex-eyebrow">EXODUS · MULTICHAIN</span><h2 class="ex-title">Your portfolio</h2></div>
      <div class="ex-switches"><label>Wallet<select id="ex-wallet" aria-label="Selected wallet">${choices.map(w=>`<option value="${esc(w.id)}" ${w.id===_selected.wallet?'selected':''}>${esc(w.name)}</option>`).join('')}</select></label>
      <label>Portfolio<select id="ex-portfolio" aria-label="Selected portfolio">${portfolios.map(p=>`<option value="${p.id}" ${p.id===_selected.portfolio?'selected':''}>${esc(p.name)}</option>`).join('')}</select></label>
      <button class="btn" id="ex-add-wallet">+ Wallet</button>${status?.exists?'<button class="btn" id="ex-add-portfolio">+ Portfolio</button>':''}</div></header>`;
  }
  function chart(history){
    const now=Date.now()/1000, cutoff=_range==='1d'?now-86400:_range==='7d'?now-604800:0;
    const points=(history?.points||[]).filter(p=>Number(p.at)>=cutoff&&Number.isFinite(Number(p.usd))&&Number(p.usd)>=0);
    if(points.length<2) return `<div class="ex-chart-empty">${history?.available===false?'History is temporarily unavailable.':points.length?'Your first snapshot is saved. More points appear as complete balances are recorded.':'Your value history starts with the first complete balance snapshot.'}</div>`;
    const values=points.map(p=>Number(p.usd)),lo=Math.min(...values),hi=Math.max(...values),first=Number(points[0].at),last=Number(points.at(-1).at);
    const coordinates=points.map((p,i)=>[18+(Number(p.at)-first)/Math.max(1,last-first)*664,hi===lo?90:160-(values[i]-lo)/(hi-lo)*130]);
    const path=coordinates.map(([x,y],i)=>`${i?'L':'M'}${x.toFixed(2)},${y.toFixed(2)}`).join(' ');
    return `<svg class="ex-value-chart" viewBox="0 0 700 190" role="img" aria-label="Observed portfolio value in US dollars"><path class="ex-chart-fill" d="${path} L${coordinates.at(-1)[0]},180 L18,180 Z"/><path class="ex-chart-line" d="${path}"/></svg>
      <div class="ex-chart-labels"><span>${esc(new Date(first*1000).toLocaleDateString())}</span><span>${esc(new Date(last*1000).toLocaleDateString())}</span></div>`;
  }
  function dashboard(data){
    const value=data?.valuation, total=value?.complete?value.total:value?.known_total;
    const holdings=Object.entries(value?.assets||{}).filter(([,v])=>Number(v.usd)>0).sort((a,b)=>Number(b[1].usd)-Number(a[1].usd));
    const sum=holdings.reduce((n,[,v])=>n+Number(v.usd),0);
    let offset=0;
    const segments=holdings.map(([symbol,v],i)=>{
      const size=Number(v.usd)/sum*364.4247, segment=`<circle class="ex-ring-part" data-ex-color="${i%6}" cx="80" cy="80" r="58" stroke-dasharray="${size} ${364.4247-size}" stroke-dashoffset="${-offset}"/>`;offset+=size;return segment;
    }).join('');
    const complete=value?.complete, caption=!value?'Loading balances and prices…':complete?'Total portfolio value':'Known holdings · incomplete total';
    return `<section class="ex-dashboard"><div class="ex-value-card"><div class="ex-value-caption">${caption}</div><div class="ex-total">${money(total)}</div><span class="ex-currency">USD</span>
      ${value?.missing?.length?`<p class="ex-hint">Balance or current price unavailable for ${value.missing.map(esc).join(', ')}.</p>`:''}
      ${value?.prices_at?`<p class="ex-price-time">Prices updated ${esc(new Date(value.prices_at*1000).toLocaleTimeString())} · CoinGecko</p>`:''}
      <div class="ex-chart-heading"><h3>Value history</h3><div class="ex-ranges">${[['1d','1D'],['7d','7D'],['all','All']].map(([id,label])=>`<button class="btn small" data-ex-range="${id}" aria-pressed="${_range===id}">${label}</button>`).join('')}</div></div>
      <div id="ex-history-chart">${chart(data?.history)}</div><p class="ex-hint">Recorded portfolio values include deposits and withdrawals.</p></div>
      <div class="ex-allocation-card"><h3>Asset allocation</h3><svg class="ex-allocation-ring" viewBox="0 0 160 160" role="img" aria-label="Allocation of known holdings"><circle class="ex-ring-track" cx="80" cy="80" r="58"/>${segments}</svg>
      <div class="ex-allocation-count">${holdings.length}<small>assets with value</small></div><ul class="ex-allocation-list">${holdings.slice(0,5).map(([symbol,v],i)=>`<li><span class="ex-dot" data-ex-color="${i%6}"></span>${esc(symbol)}<b>${(Number(v.usd)/sum*100).toFixed(1)}%</b></li>`).join('')}</ul>
      ${!holdings.length?'<p class="ex-hint">Your asset mix appears here when balances are available.</p>':''}${!complete&&holdings.length?'<p class="ex-hint">Allocation covers known holdings only.</p>':''}</div></section>`;
  }
  function wireChart(){
    document.querySelectorAll('[data-ex-range]').forEach(button=>button.onclick=()=>{
      _range=button.dataset.exRange;
      document.querySelectorAll('[data-ex-range]').forEach(b=>b.setAttribute('aria-pressed',String(b===button)));
      const el=document.querySelector('#ex-history-chart');if(el)el.innerHTML=chart(_balances?.history);
    });
  }
  function wireSelectors(){
    const wallet=document.querySelector('#ex-wallet'),portfolio=document.querySelector('#ex-portfolio');
    const select=()=>{try{sessionStorage.setItem('exodus-selection:'+_scope,JSON.stringify(_selected));}catch(_){};render();};
    if(wallet)wallet.onchange=()=>{if(_busy){wallet.value=_selected.wallet;return;}_selected={wallet:wallet.value,portfolio:0};select();};
    if(portfolio)portfolio.onchange=()=>{if(_busy){portfolio.value=String(_selected.portfolio);return;}_selected={..._selected,portfolio:Number(portfolio.value)};select();};
    for(const [id,kind] of [['ex-add-wallet','wallet'],['ex-add-portfolio','portfolio']]){
      const button=document.getElementById(id);
      if(button)button.onclick=()=>{
        if(_busy)return;
        let panel=document.getElementById('ex-panel');
        if(!panel){panel=document.createElement('div');panel.id='ex-panel';document.querySelector('.ex-wrap').append(panel);}
        const generation=_seq;
        panel.innerHTML=`<form class="ex-panel" id="ex-add-form"><h3>Add a ${kind}</h3><label>Name<input id="ex-new-name" maxlength="80" required placeholder="${kind==='wallet'?'Savings wallet':'Long-term holdings'}"></label>
          ${kind==='wallet'?'<label>Recovery phrase (optional)<textarea id="ex-new-phrase" rows="3" autocomplete="off" spellcheck="false" placeholder="Leave blank to create a new wallet"></textarea></label><details><summary>Import an existing Monero wallet</summary><label>Monero recovery words<textarea id="ex-new-monero" rows="3" autocomplete="off" spellcheck="false" placeholder="The separate 25-word Monero backup"></textarea></label></details><p class="ex-hint">Each wallet has its own backup. Keep both recovery phrases when importing a separate Monero wallet.</p>':'<p class="ex-hint">A separate set of addresses under this wallet’s existing recovery phrase. The current portfolio stays available.</p>'}
          ${kind==='wallet'?recoveryFormat('ex-new-format'):''}
          <div class="ex-actions"><button class="btn btn-cyan" type="submit">Add ${kind}</button><button class="btn" type="button" id="ex-add-cancel">Cancel</button></div><p id="ex-add-error" role="status"></p></form>`;
        document.getElementById('ex-add-cancel').onclick=()=>{if(!_busy)panel.replaceChildren();};
        document.getElementById('ex-add-form').onsubmit=async event=>{
          event.preventDefault();if(_busy||!current(generation))return;
          const name=document.getElementById('ex-new-name').value.trim();if(!name)return;
          const phrase=document.getElementById('ex-new-phrase'), mnemonic=phrase?.value.trim()||'';
          const moneroPhrase=document.getElementById('ex-new-monero'),moneroMnemonic=moneroPhrase?.value.trim()||'';
          const derivation=document.getElementById('ex-new-format')?.value;
          const submit=event.currentTarget.querySelector('[type=submit]');submit.disabled=true;_busy=true;
          try{
            const result=await request('/api/wallet/exodus/'+(kind==='wallet'?'wallets':'portfolios'),J(kind==='wallet'?{label:name,...(mnemonic?{mnemonic}:{}),...(moneroMnemonic?{moneroMnemonic}:{}),...(derivation==='cloudos-v1'?{derivation}:{})}:{name}));
            if(phrase)phrase.value='';if(moneroPhrase)moneroPhrase.value='';
            if(!current(generation))return;
            _selected=kind==='wallet'?{wallet:result.id,portfolio:0}:{..._selected,portfolio:result.portfolios.at(-1).id};
            select();
          }catch(error){if(current(generation)){document.getElementById('ex-add-error').textContent=error.message;submit.disabled=false;}}
          finally{_busy=false;}
        };
      };
    }
  }

  /* ---------------------------------------------------------------- drawing */
  function recoveryFormat(id){
    return `<details><summary>Recovery format</summary><label>Format from your backup
      <select id="${id}"><option value="exodus-v1">Exodus / current CloudOS</option>
      <option value="cloudos-v1">Legacy CloudOS (cloudos-v1)</option></select></label>
      <p class="ex-hint">Choose Legacy only when your CloudOS backup says cloudos-v1. It preserves the original Bitcoin, Solana and XRP addresses.</p></details>`;
  }

  function custodyNote(){
    return `<details class="ex-custody"><summary>Server-managed wallet · Back up your recovery phrase</summary>
      <p>Your recovery phrase is encrypted on this server. The server can unlock it to sign
      transactions, so its operator can access the wallet keys. You can export your phrase and
      restore supported assets in a compatible wallet. Keep an offline backup.</p></details>`;
  }

  function emptyView(status){
    const lib = status && status.library === false;
    return `<div class="ex-wrap">
      ${walletHeader(status)}
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
                 <details><summary>Import an existing Monero wallet</summary>
                   <label>Monero recovery words<textarea id="ex-monero-phrase" rows="3" autocomplete="off" spellcheck="false"
                     placeholder="The separate 25-word Monero backup"></textarea></label></details>
                 <p class="ex-hint">Use the exact recovery words from your backup. Existing Monero wallets
                   use a separate 25-word phrase; do not assume the 12 words restore historical Exodus XMR.</p>
                 ${recoveryFormat('ex-restore-format')}
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
  const SENDABLE = new Set(['ETH', 'MATIC', 'BNB', 'AVAX', 'XMR']);
  const canSend = (sym) => SENDABLE.has(String(sym || '').toUpperCase());

  function row(sym, name, cell, quote){
    // Monero belongs to this selected recovery phrase and portfolio.
    if(String(sym).toUpperCase() === 'XMR'){
      const known = cell?.known === true && cell.amount != null && /^\d+(?:\.\d+)?$/.test(String(cell.amount));
      const note = String((cell && cell.note) || '');
      return `<li class="ex-coin">
        <div class="ex-coin-id">${coinLogo('XMR')}<b>XMR</b><small>${esc(name)}</small></div>
        <div class="ex-coin-amt">${known
          ? `<b>${esc(String(cell.amount == null ? '0' : cell.amount))}</b> <em>XMR</em>`
          : `<span class="ex-unknown" title="The Monero wallet could not be reached. This is not a zero balance.">unavailable</span>`}
          ${quote ? `<small class="ex-fiat">${quote.usd == null ? 'Value unavailable' : money(quote.usd)}</small>` : ''}${note ? `<small class="ex-hint">${esc(note)}</small>` : ''}</div>
        <button class="btn small ex-receive" data-sym="XMR">Receive</button>
        <button class="btn small ex-send" data-sym="XMR">Send</button><button class="btn small" id="ex-xmr-backup">Back up Monero</button>
      </li>`;
    }
    /* THREE STATES, NOT TWO. A chain that answered, a chain that answered nothing, and a chain
       nobody could reach — the last one must never borrow the second one's zero. */
    const known = cell?.known === true && cell.amount != null && /^\d+(?:\.\d+)?$/.test(String(cell.amount));
    const amount = known ? String(cell.amount == null ? '0' : cell.amount) : '';
    return `<li class="ex-coin">
      <div class="ex-coin-id">${coinLogo(sym)}<b>${esc(sym)}</b><small>${esc(name)}</small></div>
      <div class="ex-coin-amt">${known
        ? `<b>${esc(amount)}</b> <em>${esc(sym)}</em>`
        : `<span class="ex-unknown" title="This chain's provider could not be reached. This is not a zero balance.">unavailable</span>`}${quote ? `<small class="ex-fiat">${quote.usd == null ? 'Value unavailable' : money(quote.usd)}</small>` : ''}${cell?.note ? `<small class="ex-hint">${esc(cell.note)}</small>` : ''}</div>
      <button class="btn small ex-receive" data-sym="${esc(sym)}">Receive</button>
      ${canSend(sym) ? `<button class="btn small ex-send" data-sym="${esc(sym)}">Send</button>` : ''}
    </li>`;
  }

  function walletView(status, balances){
    const chains = (status && status.chains) || [];
    const b = (balances && balances.balances) || {};
    const anyUnknown = chains.some(c => !(b[c.symbol] && b[c.symbol].known));
    return `<div class="ex-wrap">
      ${walletHeader(status)}
      ${dashboard(balances)}
      ${custodyNote()}
      ${status && !status.backedUp
        ? `<div class="ex-note ex-warn">You have not written down the recovery phrase yet. Without
             it, this wallet exists only on this server. <button class="btn small" id="ex-reveal-now">Show it</button></div>`
        : ''}
      <div class="ex-holdings-title"><h3>Your assets</h3><span>Balances by network</span></div><ul class="ex-coins">${chains.map(c => row(c.symbol, c.name, b[c.symbol], balances?.valuation?.assets?.[c.symbol])).join('')}</ul>
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
      <div id="ex-send-out"></div><button class="btn small" id="ex-send-status">Check transfer status</button>
    </div>`;
  }

  /* ---------------------------------------------------------------- the view */
  function scheduleBalances(mine, status, feed, delay){
    clearTimeout(_balanceTimer);
    _balanceTimer=setTimeout(()=>{
      if(!current(mine))return;
      // Keep an active send/import/recovery form and its keyboard focus untouched.
      if(_busy || document.querySelector('#ex-panel')?.childElementCount){
        scheduleBalances(mine,status,feed,15000);return;
      }
      refreshBalances(mine,status,feed);
    },delay);
  }

  async function refreshBalances(mine, status, feed){
    let balances = null;
    try{ balances = await request('/api/wallet/exodus/balances?valuation=true'); }catch(_){ balances = null; }
    if(!current(mine))return;
    _balances=balances;
    const panel=document.querySelector('#ex-panel'), active=document.activeElement;
    const focused=panel?.contains(active), selection=focused&&typeof active.selectionStart==='number'?[active.selectionStart,active.selectionEnd]:null;
    feed.innerHTML = walletView(status, balances);
    if(panel)document.querySelector('#ex-panel').replaceWith(panel);
    wireWallet();
    if(focused){active.focus({preventScroll:true});if(selection)active.setSelectionRange(...selection);}
    const incomplete=!balances || Object.values(balances.balances||{}).some(cell=>cell?.known!==true);
    scheduleBalances(mine,status,feed,incomplete?15000:60000);
  }

  async function render(){
    clearTimeout(_balanceTimer);
    const scope = accountScope();
    if(scope !== _scope){
      _scope=scope;_wallets=[];_state=null;_balances=null;_selected={wallet:'default',portfolio:0};
      try{const saved=JSON.parse(sessionStorage.getItem('exodus-selection:'+scope)||'null');if(saved&&/^(default|[0-9a-f]{32})$/.test(saved.wallet)&&Number.isInteger(saved.portfolio)&&saved.portfolio>=0&&saved.portfolio<16)_selected=saved;}catch(_){}
    }
    const feed = PC && PC.$ ? PC.$('#feed') : null;
    if(!feed) return;
    const mine = ++_seq;
    feed.innerHTML = '<div class="spinner"></div>';
    let status = null;
    try{
      const list=await request('/api/wallet/exodus/wallets');
      if(!current(mine))return;
      _wallets=Array.isArray(list.wallets)?list.wallets:[];
      status = await request('/api/wallet/exodus/status');
    }
    catch(e){
      if(!current(mine)) return;
      feed.innerHTML = `<div class="ex-wrap">${walletHeader(_state)}<div class="empty">The wallet is unavailable: ${esc(e.message)}</div><button class="btn" id="ex-refresh">Retry</button><div id="ex-panel"></div></div>`;wireSelectors();document.querySelector('#ex-refresh').onclick=render;
      return;
    }
    if(!current(mine)) return;
    _state = status; _balances = null;
    if(!status.exists){ feed.innerHTML = emptyView(status); wireEmpty(); return; }

    // Paint the wallet with no balances first, then fill them in. Nine chains over the network is
    // seconds; a spinner in front of an address somebody wants to copy is the delay this avoids.
    feed.innerHTML = walletView(status, null);
    wireWallet();
    await refreshBalances(mine,status,feed);
  }

  function wireEmpty(){
    wireSelectors();
    const generation=_seq;
    const $ = (s) => document.querySelector(s);
    const box = $('#ex-restore-box');
    const r = $('#ex-restore'); if(r) r.onclick = () => box && box.classList.toggle('hidden');
    const c = $('#ex-create');
    if(c) c.onclick = async () => {
      if(_busy) return;
      _busy = true; c.disabled = true;
      try{
        if(PC.uiConfirm && !await PC.uiConfirm(
          'Create a wallet on this server?\n\nThis node will generate and hold the recovery phrase. '
          + 'Whoever runs this server can spend it. Treat it as a hot wallet.', { ok: 'Create' })){c.disabled=false;return;}
        if(!current(generation))return;
        await request('/api/wallet/exodus/create', J({})); if(current(generation))await render();
      }
      catch(e){ PC.toast && PC.toast('wallet not created: ' + e.message); c.disabled = false; }
      finally{ _busy = false; }
    };
    const go = $('#ex-restore-go');
    if(go) go.onclick = async () => {
      if(_busy) return;
      const phrase = String(($('#ex-phrase') || {}).value || '').trim();
      if(!phrase) return PC.toast && PC.toast('paste the recovery phrase first');
      _busy = true; go.disabled = true;
      try{ await request('/api/wallet/exodus/create', J({ mnemonic: phrase, moneroMnemonic: String(($('#ex-monero-phrase')||{}).value||'').trim()||null, derivation: $('#ex-restore-format').value })); if(current(generation))await render(); }
      catch(e){ PC.toast && PC.toast(e.message); go.disabled = false; }
      finally{ _busy = false; }
    };
  }

  function wireWallet(){
    wireSelectors();
    wireChart();
    const generation=_seq;
    const $ = (s) => document.querySelector(s);
    const refresh = $('#ex-refresh'); if(refresh) refresh.onclick = () => render();
    const panel = $('#ex-panel');
    document.querySelectorAll('.ex-receive').forEach(b => b.onclick = async () => {
      const sym = b.dataset.sym;
      try{
        const got = await request('/api/wallet/exodus/addresses');
        if(!current(generation)||!panel?.isConnected)return;
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
      if(!current(generation)||!panel?.isConnected)return;
      panel.innerHTML = sendPanel(sym, from);
      const close = $('#ex-panel-close'); if(close) close.onclick = () => { panel.innerHTML = ''; };
      const go = $('#ex-send-go');
      const requestId=root.crypto.randomUUID().replaceAll('-', '');
      const checkStatus=async()=>{
        if(go)go.disabled=true;
        try{
          const result=await request('/api/wallet/exodus/send-status',J({symbol:sym}));
          if(!current(generation)||!panel.isConnected||!go.isConnected)return;
          const out=$('#ex-send-out');
          if(result.state==='idle'||result.state==='not_sent'){
            go.disabled=false;if(out)out.textContent=result.state==='not_sent'?'The previous attempt did not reach the network.':'';
          }else if(out){out.textContent=(result.state==='accepted'?'Sent. Transaction ':'Transfer remains unconfirmed. Transaction ')+(result.hash||'');}
        }catch(error){if(current(generation)&&go.isConnected){const out=$('#ex-send-out');if(out)out.textContent='Transfer status could not be confirmed. Check again before sending.';}}
      };
      const statusButton=$('#ex-send-status');if(statusButton)statusButton.onclick=checkStatus;
      checkStatus();
      if(go) go.onclick = async () => {
        if(_busy) return;
        const to = String(($('#ex-to') || {}).value || '').trim();
        const amount = String(($('#ex-amt') || {}).value || '').trim();
        if(!to || !amount) return PC.toast && PC.toast('an address and an amount, please');
        _busy = true; go.disabled = true;
        const out = $('#ex-send-out');
        let submitted=false;
        try{
          if(PC.uiConfirm && !await PC.uiConfirm(
            'Send ' + amount + ' ' + sym + ' to\n' + to + '?\n\nThis cannot be undone.',
            { ok: 'Send', danger: true })) return;
          if(!current(generation))return;
          submitted=true;
          const res = await request('/api/wallet/exodus/send', J({ symbol: sym, to, amount, requestId }));
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
          go.disabled = !(e.status>=400 && e.status<500);
          if(go.disabled && out)out.textContent='The send outcome could not be confirmed. Check the receiving address or chain activity before trying again.';
        }finally{ _busy = false; if(!submitted && go.isConnected)go.disabled=false; }
      };
    });
    const reveal = async (monero=false) => {
      if(PC.uiConfirm && !await PC.uiConfirm(
        'Show the recovery phrase?\n\nAnyone who reads it can spend this wallet. Make sure nobody '
        + 'is looking at your screen.', { ok: 'Show it', danger: true })) return;
      if(!current(generation))return;
      try{
        const got = await request('/api/wallet/exodus/'+(monero?'reveal-monero':'reveal'), J({}));
        if(!current(generation))return;
        const panelEl = $('#ex-panel');
        if(panelEl) panelEl.innerHTML = `<div class="ex-panel">
          <h3>${monero?'Monero recovery phrase':'Wallet recovery backup'}</h3>
          <div class="ex-note ex-warn">${esc(got.warning || '')}</div>
          <code class="ex-phrase">${esc(got.mnemonic || '')}</code>
          ${got.moneroMnemonic?`<h4>Separate Monero recovery phrase</h4><code class="ex-phrase">${esc(got.moneroMnemonic)}</code><p>Keep both phrases to recover all assets.</p>`:''}
          ${got.derivation==='cloudos-v1'?'<p>This legacy CloudOS wallet uses different Bitcoin, Solana and XRP paths. When restoring in CloudOS, choose Legacy CloudOS under Recovery format. An ordinary Exodus phrase restore uses different addresses for these assets.</p>':''}
          <div class="ex-actions">
            <button class="btn small" id="ex-backup-download">Download backup</button>
            <button class="btn small" id="ex-panel-close">Hide</button>
          </div></div>`;
        const download = $('#ex-backup-download');
        if(download)download.onclick=async()=>{
          if(!current(generation))return;
          try{
            if(!PC.saveBlobAs)throw new Error('File saving is unavailable in this app build');
            const backup={format:'cloudos-wallet-backup-v1',...(monero?{moneroMnemonic:got.mnemonic}:{mnemonic:got.mnemonic,derivation:got.derivation,...(got.moneroMnemonic?{moneroMnemonic:got.moneroMnemonic}:{})})};
            await PC.saveBlobAs(new Blob([JSON.stringify(backup,null,2)],{type:'application/json'}),'wallet-recovery.json');
          }catch(error){PC.toast&&PC.toast(error.message);}
        };
        const close = $('#ex-panel-close'); if(close) close.onclick = () => { panelEl.innerHTML = ''; };
      }catch(e){ PC.toast && PC.toast(e.message); }
    };
    const rv = $('#ex-reveal'); if(rv) rv.onclick = () => reveal();
    const rn = $('#ex-reveal-now'); if(rn) rn.onclick = () => reveal();
    const xm = $('#ex-xmr-backup'); if(xm)xm.onclick = () => reveal(true);
  }

  function boot(){
    if(booted) return;
    PC = root.__PC; if(!PC) return setTimeout(boot, 40);
    booted = true;
  }
  boot();
  root.PCExodus = { render, _row: row, _custodyNote: custodyNote, _canSend: canSend, _dashboard:dashboard, _walletView:walletView, _chart:chart, _money:money };
})();
