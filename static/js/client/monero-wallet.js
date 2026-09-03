/* PosterChan Monero micro-wallet UI.
 *
 * This module never handles wallet keys. It talks to the authenticated, same-origin local wallet
 * bridge and treats every failure as "external wallet mode". */
(function(root){
  'use strict';
  let PC=null, state=null, checkedAt=0, booted=false, _probeSeq=0, _signerLogged=false;
  const esc=s=>String(s==null?'':s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const amount=v=>{ const n=Number(v); return Number.isFinite(n)&&n>=0?n:0; };
  /* RPC amounts arrive as decimal STRINGS. Never pass atomic units through Number: wallet balances
     routinely exceed 2^53 atomic units, where Number silently changes the amount on screen. */
  const atomicXmr=v=>{
    let raw=String(v==null?'0':v).trim();
    if(!/^-?\d+$/.test(raw))return '0';
    let neg=raw[0]==='-'; if(neg)raw=raw.slice(1);
    raw=raw.replace(/^0+(?=\d)/,'').padStart(13,'0');
    let whole=raw.slice(0,-12).replace(/\B(?=(\d{3})+(?!\d))/g,',');
    const frac=raw.slice(-12).replace(/0+$/,'');
    if(/^0+$/.test(raw))neg=false;
    return (neg?'−':'')+whole+(frac?'.'+frac:'');
  };
  const xmr=(v,isAtomic)=>{
    if(isAtomic)return atomicXmr(v);
    const n=Number(v||0);
    return (Number.isFinite(n)?n:0).toLocaleString(undefined,{minimumFractionDigits:0,maximumFractionDigits:12});
  };
  const validAddress=(s,network)=>new RegExp('^'+(network==='mainnet'?'[48]':'[57]')+'[1-9A-HJ-NP-Za-km-z]{94}(?:[1-9A-HJ-NP-Za-km-z]{11})?$').test(String(s||'').trim());
  const uri=(address, value, label)=>'monero:'+String(address||'').trim()
    +(amount(value)>0?'?tx_amount='+encodeURIComponent(String(value)):'')
    +(label?(amount(value)>0?'&':'?')+'recipient_name='+encodeURIComponent(label):'');
  function parsePaymentUri(value,network){
    const raw=String(value||'').trim();
    // A QR may contain the bare stagenet address. Everything else must be an actual monero: URI.
    if(validAddress(raw,network))return {address:raw,amount:'',recipient:''};
    const match=/^monero:([^?]+)(?:\?(.*))?$/i.exec(raw); if(!match)return null;
    let address=''; try{address=decodeURIComponent(match[1]);}catch(_){return null;}
    if(!validAddress(address,network))return null;
    const params=new URLSearchParams(match[2]||'');
    if(params.getAll('tx_amount').length>1||params.getAll('recipient_name').length>1)return null;
    const valueAmount=params.get('tx_amount')||'';
    if(valueAmount&&!/^(?:0|[1-9]\d*)(?:\.\d{1,12})?$/.test(valueAmount))return null;
    const recipient=params.get('recipient_name')||'';
    if(recipient.length>120||/[\u0000-\u001f\u007f]/.test(recipient))return null;
    return {address,amount:/^0(?:\.0+)?$/.test(valueAmount)?'':valueAmount,recipient};
  }

  const WALLET_TIMEOUT_MS = 20000;   // > the node's own 8s RPC budget, with room for its overhead
  async function request(path, opts){
    // Signing/login is interactive and can legitimately take longer than the network timeout. Starting
    // the timer before it meant a slow Android signer returned successfully and then handed fetch() an
    // already-aborted signal, so no wallet request ever reached the server.
    if(PC&&PC.ensureAiSession) await PC.ensureAiSession();
    /* THE CLIENT MUST NOT GIVE UP BEFORE THE SERVER IS ALLOWED TO ANSWER.
       This was 5s while the node is allowed 8s for the wallet RPC alone (`monero_wallet_rpc_timeout`,
       up to 30 by configuration) plus its own overhead. So every slow-but-successful call was aborted
       here and painted as "Local wallet unavailable · Retry local wallet" — while the server finished
       and logged a 200. That is exactly what the journal showed while the phone said the wallet was
       down: status, balance, address and history all 200, the operator authenticated, nothing refused.
       Raising it costs nothing in the case that matters: a wallet that is not running refuses the
       connection immediately, so this timer only ever fires while the node is genuinely working. */
    const ctl=new AbortController(), timer=setTimeout(()=>ctl.abort(),WALLET_TIMEOUT_MS);
    try{
      // Extension/Nostr login mints a bearer session, while the cookie may be absent after a server
      // restart. A bare fetch therefore returned 401 and mislabeled a configured wallet unavailable.
      const fetcher=PC&&PC.authFetch ? PC.authFetch : fetch;
      /* The base the bundle's fetch shim will actually prepend — read from the global it sets,
         NOT from a PC helper: `_serverOrigin` lives in app.js's factory argument list and is not on
         `window.__PC`, so calling it would throw exactly where an error is being reported. */
      const target=String((root.__PC_API_BASE__!==undefined?root.__PC_API_BASE__:'')||'')+path;
      let res;
      try{
        res=await fetcher(path,Object.assign({credentials:'include',cache:'no-store',signal:ctl.signal,
          headers:{'Accept':'application/json'}},opts||{}));
      }catch(err){
        /* "FAILED TO FETCH" NAMES NOTHING, AND THAT COST FIVE ROUNDS OF GUESSING.
           A fetch that dies at the network layer throws a bare TypeError whose message is those
           three words: no URL, no status, no distinction between DNS, a refused connection, a
           blocked mixed-content request, an abort, or a CORS rejection. On the web the wallet
           works and on Android it did not, and the only thing the screen could say was "Failed to
           fetch" — which is equally consistent with every one of those and points at none.
           The address and the kind are both known HERE, so say them: the next report arrives with
           the one fact that identifies the cause instead of the one word that does not. */
        const aborted=(err&&err.name==='AbortError')||ctl.signal.aborted;
        throw new Error(aborted
          ? ('the wallet did not answer within '+Math.round(WALLET_TIMEOUT_MS/1000)+'s: '+target)
          : ('could not reach '+target+' — '+((err&&err.message)||err)));
      }
      let body={}; try{ body=await res.json(); }catch(_){}
      /* A REFUSAL IS NOT AN OUTAGE, AND SAYING SO COST SEVERAL RELEASES.
         The wallet is admin-only. A Nostr sign-in that resolves to an ordinary account gets 403 on
         every route, the probe catches it, and the screen said "Local wallet unavailable · Retry
         local wallet" — which reads as a wallet service that is down and sent everyone looking at
         the daemon, the RPC and the client's own auth. It is neither: it is this account. */
      if(res.status===401||res.status===403)
        throw new Error('this account cannot open the node wallet — sign in as the node operator'
                        + ' (the wallet is admin-only)');
      if(!res.ok) throw new Error(body.detail||body.error||body.message||('wallet service returned '+res.status));
      return body;
    }finally{clearTimeout(timer);}
  }
  async function probe(force){
    /* THE CACHE IS ONLY A CACHE ONCE THERE IS SOMETHING IN IT.
     *
     * `checkedAt` is stamped BEFORE the request, so a second call arriving while the first is still
     * in flight took this early return and got the state as it was then — null. Harmless while
     * nothing probed concurrently, and immediate once the module warms itself on load: `paint(null)`
     * throws on `s.available` and the wallet screen dies. Requiring `state` makes the early return
     * mean what it says. */
    if(!force && state && Date.now()-checkedAt<8000) return state;
    /* THE LAST ANSWER TO ARRIVE IS NOT THE LATEST ANSWER.
     *
     * Two probes can be in flight — the module warms itself on load, and a view entry or the Retry
     * button asks again. Whichever finishes LAST used to win, so a slow early probe could overwrite
     * a newer, truer one: a wallet that had just been found unreachable would flip back to
     * "available" a second later, and the screen with it. Stamped and checked, an out-of-order
     * answer is discarded rather than believed. */
    const seq = ++_probeSeq;
    checkedAt=Date.now();
    try{
      const [meta,bal,addr,histResult]=await Promise.all([
        request('/api/wallet/xmr/status'),
        request('/api/wallet/xmr/balance'), request('/api/wallet/xmr/address'),
        // History is optional display data. A malformed/temporarily unavailable history response
        // must not hide an otherwise healthy wallet, balance and receive address.
        request('/api/wallet/xmr/history?limit=50').catch(error=>({__error:error}))]);
      const hist=histResult&&typeof histResult==='object'&&!histResult.__error?histResult:{};
      const transfers=[];
      /* `pool` is INCOMING UNCONFIRMED — a payment that has been broadcast and not yet mined.
         It used not to be requested at all, so the one window in which somebody checks ("they said
         they sent it") showed nothing. It is marked, because an unconfirmed credit is not the same
         promise as a confirmed one. */
      for(const kind of ['in','out','pending','failed','pool']) for(const row of (hist[kind]||[]))
        transfers.push(Object.assign({direction:kind, unconfirmed:(kind==='pool'||kind==='pending')},row));
      transfers.sort((a,b)=>Number(b.timestamp||b.height||0)-Number(a.timestamp||a.height||0));
      const _next={available:true,network:meta.network,warning:meta.warning,balance:bal.balance,
        unlocked_balance:bal.unlocked_balance,
        /* The configured service fee, so the operator's own send sheet can state it. */
        zap_fee_percent:Number(meta.zap_fee_percent || 0) || 0,
        /* HOW LONG UNTIL IT CAN BE SPENT. Monero locks the CHANGE from a send for 10 blocks, so the
           wallet goes to zero spendable immediately after a successful tip. Without this the screen
           can only say "0", which reads as the money having gone somewhere. */
        blocks_to_unlock:bal.blocks_to_unlock,
        /* HOW MANY PEOPLE THIS WALLET CAN PAY IN A ROW. Monero spends whole outputs and locks the
           change for 10 blocks, so a wallet made of one output can tip once and then not again for
           ~20 minutes however large the balance is. */
        outputs:bal.num_unspent_outputs,
        address:addr.address||(((addr.addresses||[])[0]||{}).address)||'',transfers};
      if(seq === _probeSeq) state=_next;
    }catch(e){
      const detail=(e&&e.message)||String(e||'local wallet unavailable');
      /* One line, not one per warmed probe. Two warms plus a render made the same extension
         failure appear three times, which reads as three faults. */
      try{ if(!/could not establish your app session|receiving end does not exist/i.test(String(detail||'')))
             console.error('[monero wallet] probe failed', e);
           else if(!_signerLogged){ _signerLogged = true;
             console.warn('[monero wallet] no app session — the Nostr signer extension is not responding'); }
      }catch(_){}
      /* BUSY EITHER WAY, whoever's clock ran out first. The node answers 503 with its own wording
         when its RPC budget (8s by default) expires, which is the usual case. But that budget is an
         operator setting allowed up to 30s, and this client aborts at 20 — above 20 the abort wins
         and the message is ours, not the node's. Matching only the node's wording would make the
         "catching up" card quietly stop appearing on exactly the nodes whose wallet is slowest. */
      /* A SESSION REFUSAL IS NOT A WALLET FAULT. "sign in with a Nostr account to start an app
         session" describes the CLIENT not being ready, and storing it as the wallet's state paints
         it on the wallet screen — where it reads as the wallet being broken. Left unrecorded, the
         next probe (by which time there is a session) answers properly. */
      /* A BROKEN APP SESSION IS NOT A BROKEN WALLET, and neither of these is about Monero.
       *
       *   "sign in with a Nostr account to start an app session"  — boot has not finished
       *   "could not establish your app session: Could not establish connection.
       *    Receiving end does not exist."                          — the NIP-07 EXTENSION's content
       *                                                              script cannot reach its own
       *                                                              background worker
       *
       * The second is a browser-extension fault: measured in the console alongside "theme sync
       * skipped" failing identically, because every authenticated call goes through the same signer.
       * Recording it as the wallet's state paints it on the wallet screen, where it reads as the
       * wallet being broken and sends somebody looking at monerod. */
      if(/start an app session|sign in with a nostr/i.test(String(detail||''))){
        if(seq === _probeSeq){ state=null; checkedAt=0; }
        return state;
      }
      if(/could not establish your app session|receiving end does not exist/i.test(String(detail||''))){
        if(seq === _probeSeq){
          state = {available:false, network:'stagenet', signer:true,
                   error:'Your Nostr signer extension is not responding, so this app has no session. '
                       + 'Reload the page, or disable and re-enable the extension.'};
          checkedAt = Date.now();
        }
        return state;
      }
      const _fail={available:false,error:detail,network:'stagenet',
             busy:/did not answer|is busy|still reading the chain/i.test(String(detail||''))};
      if(seq === _probeSeq) state=_fail;
    }
    return state;
  }
  function qr(text,alt){
    try{ const src=root.PCQR&&root.PCQR.dataUrl(String(text)); return src?'<img src="'+esc(src)+'" alt="'+esc(alt)+'">':''; }catch(_){return '';}
  }
  function warning(s){const main=s&&s.network==='mainnet';return '<div class="mw-warning" role="note"><b>'+(main?'MAINNET HOT WALLET — REAL FUNDS.':'Small tips only.')+'</b> '+(main?'Keep only small tipping funds here. ':'This is a hot spending wallet. ')+'Keep substantial Monero in Feather, Monero GUI, or hardware storage.</div>';}
  /* TWO DIFFERENT FACTS, TWO DIFFERENT SENTENCES. A refused connection means no wallet is running
     here and external-wallet mode is the correct description. A wallet that accepted the connection
     and then went quiet is SCANNING, and telling that person they are "in safe external-wallet
     mode" is both wrong and alarming — it is the state a node is in for hours after it is set up. */
  function busyHtml(){return '<section class="mw-card mw-syncing"><h3>Wallet is catching up</h3><p>It is reading blocks it has not seen yet and cannot answer until it has. Balances and history appear once it reaches the tip of the chain — nothing is lost, and tipping still works through your own wallet meanwhile.</p><button class="btn btn-cyan" id="mw-retry">Check again</button></section>';}
  function fallbackHtml(error){return '<section class="mw-card mw-unavailable"><h3>Local wallet unavailable</h3><p>This device is in safe external-wallet mode. Tips still open Feather, Monero GUI, or another installed wallet; PosterChan never receives your spend key.</p>'+(error?'<p class="muted small mw-error">'+esc(error)+'</p>':'')+'<button class="btn btn-cyan" id="mw-retry">Retry local wallet</button></section>';}
  function historyDate(v){
    if(v==null||v==='')return 'pending';
    if(typeof v==='string'&&!/^\d+$/.test(v))return v;
    let n=Number(v); if(!Number.isFinite(n)||n<=0)return 'pending';
    if(n<1e12)n*=1000; // Monero RPC timestamps are epoch seconds; tolerate milliseconds too.
    try{return new Date(n).toLocaleString();}catch(_){return 'pending';}
  }
  function transferView(t){
    const direction=String(t.direction||t.type||'').toLowerCase();
    // RPC `out` amounts are commonly positive. The bucket is authoritative, not the sign.
    const incoming=direction==='in';
    const atomic=t.amount_atomic!=null, raw=String(atomic?t.amount_atomic:(t.amount==null?0:t.amount)).replace(/^-/,'');
    return {incoming,amount:xmr(raw,atomic),date:historyDate(t.timestamp||t.date)};
  }
  function transferRows(rows){
    if(!Array.isArray(rows)||!rows.length)return '<div class="mw-empty">No transactions yet</div>';
    return '<div class="mw-history">'+rows.slice(0,50).map(t=>{
      const row=transferView(t), incoming=row.incoming;
      const pend=!!t.unconfirmed;
      return '<div class="mw-tx'+(pend?' mw-tx-pending':'')+'"><span class="mw-dir '+(incoming?'in':'out')+'">'+(incoming?'↓':'↑')+'</span><span><b>'+(incoming?'Received':'Sent')+(pend?' · unconfirmed':'')+'</b><small>'+esc(row.date)+'</small></span><strong>'+(incoming?'+':'−')+row.amount+' XMR</strong></div>';
    }).join('')+'</div>';
  }
  function paint(s){
    // The state request is async. A late balance/history response never owns the shared feed after
    // navigation (including login landing, resume, or a relay-driven repaint).
    // Nothing to paint is not an error — an in-flight probe can hand back no state at all.
    if(!s||!PC||PC.VIEW!=='wallet')return;
    const f=document.getElementById('feed'); if(!f)return;
    if(!s.available && s.signer){
      f.innerHTML = '<div class="mw-wrap"><header class="mw-head"><span class="mw-logo">\u0271</span>'
        + '<div><h2>Monero Wallet</h2><span class="mw-net">SIGNER</span></div></header>'
        + '<section class="mw-card mw-unavailable"><h3>Your signer extension is not responding</h3>'
        + '<p>' + esc(s.error) + '</p><p class="muted small">Nothing is wrong with the wallet or the '
        + 'Monero node \u2014 every signed-in feature needs the signer, which is why the theme and '
        + 'other server data fail at the same time.</p>'
        + '<button class="btn btn-cyan" id="mw-retry">Try again</button></section></div>';
      bind(); return;
    }
    if(!s.available){f.innerHTML='<div class="mw-wrap"><header class="mw-head"><span class="mw-logo">ɱ</span><div><h2>Monero Wallet</h2><span class="mw-net">LOCAL WALLET</span></div></header>'+warning(s)+(s.busy?busyHtml():fallbackHtml(s.error))+'</div>'; bind(); return;}
    const address=String(s.address||''), balance=s.balance_atomic!=null?s.balance_atomic:s.balance;
    f.innerHTML='<div class="mw-wrap"><header class="mw-head"><span class="mw-logo">ɱ</span><div><h2>Monero Wallet</h2><span class="mw-net">'+esc((s.network||'stagenet').toUpperCase())+(s.network==='stagenet'?' · testing only':'')+'</span></div><button class="btn btn-ghost small" id="mw-refresh">Refresh</button></header>'
      +warning(s)+'<section class="mw-balance"><span>Balance</span><strong>'+xmr(balance,s.balance_atomic!=null)+' <small>XMR</small></strong><span class="muted small">'+xmr(s.unlocked_balance_atomic!=null?s.unlocked_balance_atomic:balance,s.unlocked_balance_atomic!=null||s.balance_atomic!=null)+' XMR can be sent now</span></section>'
      +'<div class="mw-actions"><button class="btn btn-neon" id="mw-send">Send</button><button class="btn btn-cyan" id="mw-receive">Receive</button></div>'
      +'<div id="mw-sync"></div>'
      +'<section class="mw-card"><h3>Recent activity</h3>'+transferRows(s.transfers||s.history)+'</section>'
      +'<section class="mw-card mw-address"><h3>Receive address</h3><code>'+esc(address||'Wallet has not returned an address')+'</code><button class="btn btn-ghost small" id="mw-copy">Copy</button></section></div>';
    bind();
  }
  /* WHY A ZERO IS A ZERO.
   *
   * Reported as "people have been zapping my monero address but wallet still says 0". Measured on
   * the node: monerod was 284,871 blocks behind and syncing, so the wallet had never seen the blocks
   * those payments were in. `balance: 0` was correct and the screen was unreadable — an empty wallet
   * and a wallet that has not finished reading the chain looked identical, on the one screen where
   * that difference is somebody's money.
   *
   * Asked for AFTER the paint, never before it: `refresh` does real work on a wallet that is behind,
   * and the balance must not wait on it. An unknown answer says nothing at all rather than claiming
   * this wallet is up to date — the reassuring answer is the one that would be wrong. */
  let _syncAt=0, _syncing=null, _syncLast=null;
  async function syncNote(){
    const host=document.getElementById('mw-sync'); if(!host)return;
    /* ASK ONCE, NOT ONCE PER PAINT. `bind()` runs from every paint and `render` paints twice (the
       cached answer, then the fresh one), with `_watch` repainting behind that — so this fired a
       `/sync` per paint, and each one makes the node call `refresh`, which is real work on the very
       wallet we are reporting as too busy to answer. Cached briefly, and never two in flight. */
    let st=_syncLast;
    if(!st || Date.now()-_syncAt > 30000){
      if(!_syncing){
        _syncing = request('/api/wallet/xmr/sync')
          .then(r=>{ _syncLast=r; _syncAt=Date.now(); return r; })
          .catch(()=>null)
          .then(r=>{ _syncing=null; return r; });
      }
      st = await _syncing;
    }
    if(!st||!st.checked||!st.scanning)return;
    if(PC.VIEW!=='wallet')return;
    const again=document.getElementById('mw-sync'); if(!again)return;
    again.innerHTML='<section class="mw-card mw-syncing"><h3>Still catching up with the chain</h3>'
      +'<p>This node is reading blocks it has not seen yet, so recent payments are not counted in the '
      +'balance above. Nothing is lost — the balance fills in as it catches up.</p></section>';
  }

  function bind(){
    const by=id=>document.getElementById(id);
    try{ syncNote(); }catch(_){ }
    if(by('mw-retry'))by('mw-retry').onclick=()=>render(true);
    if(by('mw-refresh'))by('mw-refresh').onclick=()=>render(true);
    if(by('mw-send'))by('mw-send').onclick=()=>sendDialog({});
    if(by('mw-receive'))by('mw-receive').onclick=receiveDialog;
    if(by('mw-copy'))by('mw-copy').onclick=()=>copy(state.address);
  }
  async function copy(value){
    const text=String(value||'');
    try{await navigator.clipboard.writeText(text); PC.toast('address copied');return;}
    catch(_){}
    /* Clipboard permission is commonly denied in embedded desktop/mobile webviews. Keep the
       fallback inside PosterChan's own UI: native prompt() escapes the app window and is unusable
       on a phone. execCommand is deliberately only the last-resort compatibility path. */
    const box=document.createElement('textarea');
    box.value=text;box.readOnly=true;box.setAttribute('aria-label','Monero address');
    Object.assign(box.style,{position:'fixed',inset:'auto 0 0',opacity:'0',pointerEvents:'none'});
    document.body.appendChild(box);box.select();box.setSelectionRange(0,text.length);
    let copied=false;try{copied=!!document.execCommand&&document.execCommand('copy');}catch(_){}
    box.remove();PC.toast(copied?'address copied':'touch and hold the address to copy');
  }
  function receiveDialog(){
    if(!state||!validAddress(state.address,state.network)){PC.toast('wallet address unavailable');return;}
    const u=uri(state.address,'','PosterChan');
    PC.modal('<div class="mw-modal"><h3>Receive Monero</h3><span class="mw-net">'+esc((state.network||'stagenet').toUpperCase())+'</span><div class="mw-qr">'+(qr(u,'Monero receive QR')||'<span>QR unavailable</span>')+'</div><code>'+esc(state.address)+'</code><div class="mw-actions"><button class="btn btn-cyan" id="mw-modal-copy">Copy address</button><a class="btn btn-ghost" href="'+esc(u)+'">Open wallet</a></div></div>',r=>{r.querySelector('#mw-modal-copy').onclick=()=>copy(state.address);});
  }
  /* THE SAME ONE-TAP AMOUNTS THE EXTERNAL FLOW OFFERS.
   *
   * The built-in wallet's sheet had a bare number box while the URI/QR modal beside it had preset
   * chips and remembered the last amount — so using the wallet that is meant to be the SEAMLESS one
   * meant typing an amount every time. Reported as "it's missing the pre-filled zap amounts in it".
   *
   * The list is passed in by the caller (`xmrPresets()`, a synced user setting) rather than owned
   * here: two lists of "your usual tip" would drift the first time somebody edited one. */
  function _presetRow(opts){
    const list=(opts&&Array.isArray(opts.presets)?opts.presets:[]).filter(a=>amount(a)>0);
    if(!list.length) return '';
    return '<div class="xmr-presets mw-presets">'
      + list.map(a=>'<button type="button" class="xmr-preset" data-mw-amt="'+esc(String(a))+'">\u0271 '+esc(String(a))+'</button>').join('')
      + '</div>';
  }

  function sendDialog(opts){
    opts=opts||{}; const preset=validAddress(opts.address,state&&state.network)?opts.address:'';
    /* SAY WHY IT CANNOT SEND YET, at the top, before an amount is typed.
     *
     * Monero locks the change from a send for 10 blocks, so the spendable balance is zero for about
     * twenty minutes after every tip. Left unsaid, the send is accepted, the daemon refuses it, and
     * the wallet looks broken — which is exactly how it was reported. */
    /* THE OPERATOR'S OWN SHEET SAYS WHAT THE FEE IS, AND THAT IT IS NOT CHARGED HERE.
       Reported as "when I zap, i see nothing about the fee". Correct behaviour — this is the node's
       own wallet, so a cut would be the operator paying themselves and losing a miner fee — but an
       operator who has just configured a fee and then sees no mention of it anywhere cannot tell
       working-as-intended from the setting having failed to save. */
    const _feePct = Number(state && state.zap_fee_percent) || 0;
    const _feeNote = _feePct > 0
      ? '<p class="muted small">Service fee: <b>' + esc(String(_feePct)) + '%</b> on tips sent from a '
        + 'wallet this node holds for someone else, paid to you. This is your own node wallet, so '
        + 'nothing is taken from this tip.</p>'
      : '';
    const _lockedOnly = state && amount(state.balance) > 0 && !(amount(state.unlocked_balance) > 0);
    const _blocks = Number(state && state.blocks_to_unlock) || 0;
    const _lockNote = _lockedOnly
      ? '<div class="mw-warning" role="note"><b>Your balance is still locking.</b> '
        + esc(xmr(state.balance, false)) + ' XMR arrives in about '
        + esc(String(Math.max(1, _blocks) * 2)) + ' minutes ('
        + esc(String(Math.max(1, _blocks))) + ' block' + (_blocks === 1 ? '' : 's')
        + '). Monero locks the change from every payment — nothing is lost. '
        + 'Until then, tip from an external wallet.</div>'
      : '';
    PC.modal('<div class="mw-modal"><h3>'+(preset?'Tip '+esc(opts.name||'with Monero'):'Send Monero')+'</h3>'+warning()+_lockNote+_feeNote+'<button type="button" class="btn btn-cyan full mw-scan" id="mw-scan">Scan wallet QR</button><div class="mw-scan-stage hidden" id="mw-scan-stage"><video playsinline muted></video><span>Point at a Monero payment QR…</span><button type="button" class="btn btn-ghost small" id="mw-scan-cancel">Cancel scan</button></div><label>Recipient address<input class="input" id="mw-to" value="'+esc(preset)+'" autocomplete="off" spellcheck="false"></label><label>Amount (XMR)<input class="input" id="mw-amount" type="number" min="0.000000000001" step="0.0001" inputmode="decimal" value="'+esc(String(opts.amount||''))+'"></label>'+_presetRow(opts)+'<label>Note (stored only in your wallet)<input class="input" id="mw-note" maxlength="120"></label><button class="btn btn-neon full" id="mw-review">Review payment</button></div>',r=>{
      r.querySelector('#mw-scan').onclick=()=>scanPayment(r);
      // One tap fills the amount, exactly as it does in the external flow.
      Array.prototype.forEach.call(r.querySelectorAll('[data-mw-amt]'), b=>{
        b.onclick=()=>{ const el=r.querySelector('#mw-amount');
          if(el){ el.value=b.getAttribute('data-mw-amt'); try{ el.focus(); }catch(_){ } } };
      });
      r.querySelector('#mw-review').onclick=()=>{
        let to=r.querySelector('#mw-to').value.trim();
        // Pasting a complete payment URI into the address field is the permission-free scanner
        // fallback. Parse it as data and populate fields; never navigate to or render its contents.
        if(/^monero:/i.test(to)){if(!putPayment(r,to))return;to=r.querySelector('#mw-to').value.trim();}
        const val=r.querySelector('#mw-amount').value.trim(), note=r.querySelector('#mw-note').value.trim();
        if(!validAddress(to,state&&state.network)){PC.toast('check the Monero address for this network');return;}
        if(!(amount(val)>0)){PC.toast('enter an amount greater than zero');return;}
        /* The spendable balance is what a transfer can actually draw on. Checked HERE so the
           refusal names the reason and the countdown, instead of the daemon answering after the
           user has confirmed an irreversible payment. */
        /* SAY WHAT THE LIMIT IS, not merely that one was hit. "More than the wallet can spend
           right now" tells somebody their number is wrong without telling them the right one, so
           the only way to find it is to guess — and the balance on screen is the TOTAL, which is
           not what a transfer can draw on when part of it is still locking. */
        /* A missing balance means the cheap access check succeeded while wallet-rpc was busy.
           Let the server review the amount instead of turning unknown into zero and rejecting every
           payment in the browser. A known balance is still enforced here. */
        if(state && state.unlocked_balance != null
            && amount(val) > amount(state.unlocked_balance)){
          const have = xmr(state && state.unlocked_balance, false);
          PC.toast(_lockedOnly
            ? ('only ' + have + ' XMR can be sent right now — the rest unlocks in about '
               + (Math.max(1,_blocks)*2) + ' minutes')
            : ('only ' + have + ' XMR is available to send right now'));
          return;
        }
        confirmDialog({address:to,amount:val,note},opts);
      };
    });
  }
  function putPayment(r,text){
    const pay=parsePaymentUri(text,state&&state.network); if(!pay){PC.toast('That payment QR does not match the wallet network');return false;}
    r.querySelector('#mw-to').value=pay.address;
    if(pay.amount)r.querySelector('#mw-amount').value=pay.amount;
    if(pay.recipient)r.querySelector('#mw-note').value=pay.recipient;
    return true;
  }
  async function scanPayment(r){
    // APK: reuse the same native ZXing plugin as PosterChan's signer scanner.
    try{
      const plugin=root.Capacitor&&root.Capacitor.Plugins&&root.Capacitor.Plugins.QrScan;
      if(plugin&&typeof plugin.scan==='function'){
        const got=await plugin.scan(),text=String((got&&got.text)||'').trim();
        if(text)putPayment(r,text); return;
      }
    }catch(_){}
    // Modern browsers: BarcodeDetector + the browser camera. Others retain a useful paste route.
    let detector=null;
    try{if(root.BarcodeDetector){const formats=await root.BarcodeDetector.getSupportedFormats();if(formats.includes('qr_code'))detector=new root.BarcodeDetector({formats:['qr_code']});}}catch(_){}
    if(!detector||!navigator.mediaDevices||!navigator.mediaDevices.getUserMedia){
      const input=r.querySelector('#mw-to');input.focus();
      PC.toast('Camera scanning is unavailable — paste the Monero URI in Recipient address');return;
    }
    const stage=r.querySelector('#mw-scan-stage'),video=stage.querySelector('video'); stage.classList.remove('hidden');
    let stream=null,stopped=false;
    const stop=()=>{stopped=true;try{stream&&stream.getTracks().forEach(t=>t.stop());}catch(_){}stage.classList.add('hidden');};
    r.querySelector('#mw-scan-cancel').onclick=stop;
    try{stream=await navigator.mediaDevices.getUserMedia({video:{facingMode:'environment'}});video.srcObject=stream;await video.play();}
    catch(_){stop();PC.toast('Camera unavailable — paste the Monero URI instead');return;}
    const tick=async()=>{
      if(stopped||!r.isConnected){stop();return;}
      try{const codes=await detector.detect(video),text=codes&&codes[0]&&codes[0].rawValue;if(text){if(putPayment(r,text))stop();else setTimeout(tick,350);return;}}catch(_){}
      setTimeout(tick,250);
    }; tick();
  }
  function confirmDialog(pay,opts){
    PC.modal('<div class="mw-modal"><h3>Confirm payment</h3><div class="mw-confirm"><span>Send</span><strong>'+esc(pay.amount)+' XMR</strong><span>To</span><code>'+esc(pay.address)+'</code></div><label class="mw-check"><input type="checkbox" id="mw-understand"> I understand this Monero transaction cannot be reversed.</label><button class="btn btn-neon full" id="mw-confirm" disabled>Send now</button><a class="btn btn-ghost full" id="mw-external" href="'+esc(uri(pay.address,pay.amount,opts.name||''))+'">Open external wallet instead</a></div>',r=>{
      const check=r.querySelector('#mw-understand'),button=r.querySelector('#mw-confirm'); check.onchange=()=>button.disabled=!check.checked;
      button.onclick=async()=>{
        button.disabled=true;button.textContent='Sending…';
        try{
          const made=await request('/api/wallet/xmr/transfer/prepare',{method:'POST',headers:{'Accept':'application/json','Content-Type':'application/json'},body:JSON.stringify({address:pay.address,amount:pay.amount,description:pay.note||''})});
          const out=await request('/api/wallet/xmr/transfer/confirm',{method:'POST',headers:{'Accept':'application/json','Content-Type':'application/json'},body:JSON.stringify({confirmation:made.confirmation})});
          PC.closeModal();PC.toast('ɱ payment sent'); checkedAt=0;
          if(typeof opts.onSent==='function')opts.onSent(pay.amount,out.txid||out.tx_hash||'');
          if(PC.VIEW==='wallet')render(true);
        }catch(e){
          /* NEVER SAY "NOT SENT" ABOUT A TIMEOUT. Measured with real money: the wallet was given a
             read's 8-second budget for a transfer, the call timed out, this said "payment not
             sent" — and the transaction had already been broadcast. Two pending 0.001 XMR sends
             afterwards, from somebody who had been told the first one failed. An unknown must read
             as an unknown and must not invite a retry. */
          const msg = (e && e.message) || String(e);
          const unsure = /may have been sent|did not answer in time/i.test(msg);
          button.disabled = unsure; button.textContent = unsure ? 'Check your history' : 'Send now';
          PC.toast(unsure ? msg : ('payment not sent: ' + msg));
        }
      };
    });
  }
  /* THE WALLET SCREEN FOR SOMEBODY WHO IS NOT THE OPERATOR.
   *
   * The node wallet is admin-only, so every call a normal user makes to it is a 403 and the screen
   * showed them the refusal verbatim: "this account cannot open the node wallet — sign in as the
   * node operator". Reported by the first person who signed up. That message is correct and it is
   * addressed to the wrong audience — a user opening "Monero Wallet" wants THEIR wallet, and this
   * node keeps one for them. */
  function meWalletHtml(s){
    const locked = amount(s.balance) > 0 && !(amount(s.unlocked_balance) > 0);
    const mins = Math.max(1, Number(s.blocks_to_unlock) || 0) * 2;
    return '<div class="mw-wrap"><header class="mw-head"><span class="mw-logo">\u0271</span>'
      + '<div><h2>Monero Wallet</h2><span class="mw-net">' + esc(String(s.network || '').toUpperCase())
      + '</span></div><button class="btn btn-ghost small" id="mw-refresh">Refresh</button></header>'
      + '<div class="mw-warning" role="note"><b>This wallet is held by this server.</b> '
      + 'Keep only tipping amounts in it. You can withdraw to your own wallet at any time \u2014 '
      + 'and for anything you want to keep, use your own wallet instead.</div>'
      + '<section class="mw-balance"><span>Available balance</span><strong>'
      + esc(xmr(s.unlocked_balance, false)) + ' <small>XMR</small></strong>'
      + '<span class="muted small">' + esc(xmr(s.balance, false)) + ' XMR total'
      + (locked ? ' \u00b7 unlocks in about ' + esc(String(mins)) + ' min' : '') + '</span></section>'
      + '<div class="mw-actions"><button class="btn btn-cyan" id="mw-me-receive">Receive</button>'
      + '<button class="btn" id="mw-me-withdraw">Withdraw</button></div>'
      + '<section class="mw-card mw-address"><h3>Your receiving address</h3><code>'
      + esc(s.address || '') + '</code>'
      + '<button class="btn btn-ghost small" id="mw-me-copy">Copy</button></section></div>';
  }

  function meBind(s){
    const by = id => document.getElementById(id);
    if(by('mw-refresh')) by('mw-refresh').onclick = () => { _meAt = 0; render(true); };
    if(by('mw-me-copy')) by('mw-me-copy').onclick = () => copy(s.address);
    if(by('mw-me-receive')) by('mw-me-receive').onclick = () => {
      PC.modal('<div class="mw-modal"><h3>Receive Monero</h3>'
        + '<div class="mw-qr">' + qr(uri(s.address, '', ''), 'Your Monero address') + '</div>'
        + '<code>' + esc(s.address || '') + '</code>'
        + '<button class="btn btn-cyan full" id="mw-me-qcopy">Copy address</button></div>',
        r => { const b = r.querySelector('#mw-me-qcopy'); if(b) b.onclick = () => copy(s.address); });
    };
    if(by('mw-me-withdraw')) by('mw-me-withdraw').onclick = () => {
      /* THE WAY OUT. Custody without one is an IOU rather than a wallet, so it is on the screen
         itself and not buried in a menu. */
      PC.modal('<div class="mw-modal"><h3>Withdraw everything</h3>'
        + '<p class="muted small">Sends your whole balance to an address you control. '
        + 'There is no partial withdrawal: Monero sweeps the account.</p>'
        + '<label>Your Monero address<input class="input" id="mw-wd-to" autocomplete="off" spellcheck="false"></label>'
        + '<button class="btn btn-neon full" id="mw-wd-go">Withdraw</button></div>', r => {
          const go = r.querySelector('#mw-wd-go');
          go.onclick = async () => {
            const to = String((r.querySelector('#mw-wd-to') || {}).value || '').trim();
            if(!validAddress(to, s.network)){ PC.toast('check the Monero address for this network'); return; }
            go.disabled = true; go.textContent = 'Withdrawing…';
            try{
              await request('/api/wallet/xmr/me/withdraw', {method:'POST',
                headers:{'Accept':'application/json','Content-Type':'application/json'},
                body: JSON.stringify({address: to})});
              PC.closeModal(); PC.toast('withdrawal sent'); _meAt = 0; render(true);
            }catch(e){
              go.disabled = false; go.textContent = 'Withdraw';
              PC.toast('withdrawal failed: ' + ((e && e.message) || e));
            }
          };
        });
    };
  }

  async function render(force){
    if(!PC||PC.VIEW!=='wallet')return;
    /* CLAIM THE FEED BEFORE THE FIRST AWAIT, AND CLAIM IT WITH WHAT WE ALREADY KNOW.
     *
     * `#feed` is shared by every screen and `renderModuleView` only draws a spinner when the module
     * GLOBAL is missing — ours is loaded by a script tag, so it calls straight in here and nothing
     * has cleared the previous view. Awaiting probe() first therefore left the LAST view's DOM on
     * screen for the whole probe: opening Monero Wallet from Texts showed "Search messages" and
     * "Loading messages…" under the wallet's nav item, for as long as the backend took to answer.
     *
     * Painting a SPINNER fixed that and introduced the next report — "on desktop I alt-tab to
     * monero then monero wallet black screen with circle". Re-entering the view blanked a wallet
     * this device had already read and then waited on probe(), which is allowed 20 seconds and
     * takes most of them while a just-restarted monerod reconnects.
     *
     * `state` is the last answer this page got, so alt-tab paints the balance you were looking at
     * and updates it in place. The spinner is kept for the one case it is honest about: a view
     * that has never been read, where there is nothing to show. */
    const f=PC.$&&PC.$('#feed');
    if(state) paint(state);
    else if(f) f.innerHTML='<div class="spinner"></div>';
    /* BOUNDED HERE TOO. The deadline was put on `tip()` and `meTip()` and NOT on this, so opening
       the wallet screen still awaited a probe that begins with `ensureAiSession()` — which for a
       Nostr login goes through the signer and can wait on a phone, an approval, or nothing at all.
       The screen then sat on its spinner for ever. Reported as "Monero wallet is not even loading
       now": the same root cause as the tip that did nothing, through a different door. */
    let s=await _bounded(probe(!!force));
    if(PC.VIEW!=='wallet')return;
    if(s === TIMED_OUT){
      /* NO ANSWER IS A STATE, NOT A REASON TO KEEP SPINNING. Say so, and leave a way to ask again. */
      const f3 = document.getElementById('feed');
      if(f3 && !state) f3.innerHTML = '<div class="mw-wrap"><header class="mw-head">'
        + '<span class="mw-logo">\u0271</span><div><h2>Monero Wallet</h2>'
        + '<span class="mw-net">LOCAL WALLET</span></div></header>'
        + '<section class="mw-card mw-unavailable"><h3>The wallet did not answer</h3>'
        + '<p>It did not reply in time. If you have just signed in, give it a moment \u2014 '
        + 'signing in has to finish before the wallet can be read.</p>'
        + '<button class="btn btn-cyan" id="mw-retry">Try again</button></section></div>';
      if(f3 && !state) bind();
      return;
    }
    /* A REFUSAL FROM THE NODE WALLET IS NOT THIS USER'S ANSWER. It is admin-only, so for everybody
       else it always 403s — showing them that message is showing them somebody else's error. If
       this node keeps a wallet for them, that is the wallet this screen is about. */
    if(s && !s.available){
      try{
        const me = await meProbe(!!force);
        if(me && me.enabled){
          if(PC.VIEW!=='wallet')return;
          const f2 = document.getElementById('feed');
          if(f2){ f2.innerHTML = meWalletHtml(me); meBind(me); }
          return;
        }
      }catch(_){ }
    }
    paint(s);
    _watch(s);
  }

  /* COME BACK ON ITS OWN — a restarted daemon is not a permanent verdict.
   *
   * Reported after `monerod` was restarted: the screen sat on "Local wallet unavailable — this
   * device is in safe external-wallet mode" until somebody pressed Retry. Nothing was wrong by
   * then; the wallet needs a moment to reconnect and the probe had simply asked once, during it.
   * The wording makes it worse than a spinner would: "safe external-wallet mode" reads as a
   * DECISION this device has taken, not as one failed request.
   *
   * Backed off so a node with genuinely no wallet — the default — costs a handful of requests and
   * then one every half minute, and stopped the moment the view is left or the wallet answers. The
   * Retry button stays: a person asking is still allowed to ask immediately. */
  let _watchTimer=null, _watchDelay=0;
  function _stopWatch(){ if(_watchTimer){ clearTimeout(_watchTimer); _watchTimer=null; } _watchDelay=0; }
  function _watch(s){
    _stopWatch();
    if(s && s.available) return;                     // nothing to wait for
    _watchDelay = Math.min(_watchDelay ? _watchDelay*2 : 3000, 30000);
    _watchTimer = setTimeout(async ()=>{
      _watchTimer=null;
      if(!PC || PC.VIEW!=='wallet') return _stopWatch();   // left the screen: stop asking
      const next=await probe(true);
      if(!PC || PC.VIEW!=='wallet') return _stopWatch();
      /* Only repaint when the ANSWER changed. Redrawing the same failure every few seconds throws
       * away a scroll position and makes a quiet retry look like something going wrong. */
      if(next && next.available) paint(next);
      _watch(next);
    }, _watchDelay);
    try{ if(_watchTimer && _watchTimer.unref) _watchTimer.unref(); }catch(_){ }
  }
  /* TIPPING MUST NEVER WAIT ON THE LOCAL WALLET.
   *
   * `doXmrTip` calls this first and falls through to the non-custodial URI/QR flow when it answers
   * false — which is the right design and was undone by the await. `probe` is allowed 20 seconds,
   * and monero-wallet-rpc BLOCKS while it scans, so on a node that is catching up (hours, at
   * launch) tapping ɱ did nothing at all for twenty seconds before the modal appeared. The
   * fallback exists so tipping does not depend on this optional wallet; it has to not depend on
   * its LATENCY either.
   *
   * A fresh cached answer is used as-is. Otherwise the probe is started — it is still worth having
   * for the next tip — and given a moment to answer before we get out of the way. */
  /* THE SAME CLICK MUST GIVE THE SAME ANSWER.
   *
   * This raced the probe against a 1200ms stopwatch so a tip would never block on a scanning
   * wallet. It did fix the twenty-second freeze and it made the decision depend on WHO WON A RACE:
   * probe answers in time and you get the built-in wallet, a moment slower and the same click hands
   * you the external flow. Reported as "the monero chooser is like different each time! sometimes
   * it lets it from local wallet, sometimes not" — and a payment UI that behaves differently on
   * identical input is worse than a slow one.
   *
   * So the answer comes from what the wallet SAID, never from a timer. `warm()` asks once when this
   * module loads, so by the time anybody clicks ɱ the answer is almost always already here; when it
   * is not, this waits for it. `probe` has its own ceiling, and the wallet screen's own paths are
   * unchanged. */
  /* NEVER WARM BEFORE THERE IS SOMEBODY TO WARM FOR.
   *
   * Warming at module load is what makes the tip decision instant and deterministic — and the module
   * loads during boot, or on the first tip, which can both be BEFORE sign-in has finished. Every
   * wallet call starts with `ensureAiSession()`, which throws "sign in with a Nostr account to start
   * an app session" when there is no identity yet. That refusal was then stored as the wallet's own
   * error and printed on the wallet screen. Reported as "not even the wallet works — sign in with a
   * Nostr account to start an app session", by somebody who WAS signed in: the state had been
   * recorded a moment earlier, before they were.
   *
   * So a warm is skipped entirely until there is a viewer, and retried when there is one. A probe
   * asked for by a person (opening the screen, pressing Retry, tapping a tip) is never skipped —
   * that one has a session by definition. */
  function _haveViewer(){
    try{ const v = PC && PC.viewer && PC.viewer(); return !!(v && v.pubkey); }catch(_){ return false; }
  }
  let _warmTries = 0;
  function warm(){
    if(!_haveViewer()){
      // Boot is not instant and sign-in is not synchronous. Look again a few times, then stop.
      if(_warmTries++ < 20){
        /* `unref` for the same reason `_watch`'s timer has it: a pending retry keeps Node's event
           loop alive, so a test that drives this module never exits and times out instead of
           failing honestly. Browsers ignore the call. */
        const t = setTimeout(warm, 1500);
        try{ if(t && t.unref) t.unref(); }catch(_){ }
      }
      return;
    }
    try{ Promise.resolve(probe(false)).catch(()=>null); }catch(_){ }
    try{ Promise.resolve(meProbe(false)).catch(()=>null); }catch(_){ }
  }
  /* ── THE USER'S OWN WALLET ─────────────────────────────────────────────────────────────────
   *
   * Everything above this line is the NODE's wallet, which is admin-only: for anybody who is not
   * the operator every one of those calls is a 403, `tip()` answers false, and the zap falls
   * through to the external URI flow. That is why 98.9% of profiles could not be tipped in one tap.
   *
   * This is the other half — a wallet the node keeps for the signed-in user, one account each in a
   * pooled wallet. It is CUSTODIAL and the UI says so where it matters. It is tried AFTER the
   * operator's wallet and BEFORE the external flow, and it declines quietly whenever it cannot
   * help, so the non-custodial path is always still there. */
  let _meState = null, _meAt = 0;
  async function meProbe(force){
    if(!force && _meState && Date.now() - _meAt < 30000) return _meState;
    try{
      const st = await request('/api/wallet/xmr/me/status');
      if(!st || !st.enabled){ _meState = {enabled:false}; _meAt = Date.now(); return _meState; }
      const b = await request('/api/wallet/xmr/me/balance');
      _meState = {enabled:true, network:st.network, address:b.address, balance:b.balance,
                  unlocked_balance:b.unlocked_balance, blocks_to_unlock:b.blocks_to_unlock,
                  outputs:b.outputs,
                  /* The operator's cut, so the sheet can state it BEFORE anybody sends. A fee a
                     payer only discovers afterwards — by noticing the recipient got less than they
                     chose — is indistinguishable from the wallet being broken. */
                  fee_percent:Number(st.fee_percent || 0) || 0};
    }catch(e){
      /* A FAILURE IS NOT A DURABLE ANSWER — the same latch that made the node wallet stop working
         for the life of a page. Only a positive result is cached. */
      _meState = null; _meAt = 0;
      return {enabled:false, error:(e && e.message) || String(e)};
    }
    _meAt = Date.now();
    return _meState;
  }

  async function meTip(opts){
    let s = await _bounded(meProbe(false)); if(s === TIMED_OUT) s = null;
    if(!s || !s.enabled) return false;
    if(!validAddress(opts && opts.address, s.network)) return false;
    // Nothing spendable: hand it to the external flow rather than open a sheet that would be refused.
    if(!(amount(s.unlocked_balance) > 0)){
      const held = amount(s.balance), mins = Math.max(1, Number(s.blocks_to_unlock) || 0) * 2;
      if(held > 0){ try{ PC.toast('your wallet unlocks in ~' + mins + ' min — using an external wallet'); }catch(_){ } }
      return false;
    }
    meSendDialog(opts, s);
    return true;
  }

  function meSendDialog(opts, s){
    opts = opts || {};
    const presets = _presetRow(opts);
    PC.modal('<div class="mw-modal"><h3>\u0271 Tip ' + esc(opts.name || 'with Monero') + '</h3>'
      + '<div class="mw-warning" role="note"><b>Your PosterChan wallet.</b> '
      + esc(xmr(s.unlocked_balance, false)) + ' XMR available. This wallet is held by this server \u2014 '
      + 'keep only tipping amounts in it, and you can withdraw to your own wallet at any time.</div>'
      + '<label>Amount (XMR)<input class="input" id="mw-me-amt" type="number" min="0.000000000001" '
      + 'step="0.0001" inputmode="decimal" value="' + esc(String(opts.amount || '')) + '"></label>'
      + presets
      + (s.fee_percent > 0
          ? '<p class="muted small" id="mw-me-fee">This server takes ' + esc(String(s.fee_percent))
            + '% of a tip sent from your wallet here. Sending from your own wallet costs nothing '
            + '\u2014 use the link below.</p>'
          : '')
      + '<button class="btn btn-neon full" id="mw-me-send">Send tip</button>'
      + '<a class="btn btn-ghost full" id="mw-me-external" href="' + esc(uri(opts.address, '', opts.name || '')) + '">Use an external wallet instead</a>'
      + '</div>', r => {
        /* SHOW THE ACTUAL FIGURE, not just the percentage. "2%" of an amount somebody has not
           typed yet is not information; "they receive 0.0098" is. Recomputed as they type and on
           every preset, and it stays silent when there is no fee. */
        const fee = Number(s.fee_percent) || 0;
        const note = r.querySelector('#mw-me-fee');
        const restate = () => {
          if(!note || !(fee > 0)) return;
          const v = amount(String((r.querySelector('#mw-me-amt') || {}).value || '').trim());
          note.textContent = (v > 0)
            ? 'They receive ' + xmr(v * (100 - fee) / 100, false) + ' XMR \u2014 this server takes '
              + fee + '% (' + xmr(v * fee / 100, false) + ' XMR). Sending from your own wallet costs '
              + 'nothing; use the link below.'
            : 'This server takes ' + fee + '% of a tip sent from your wallet here. Sending from your '
              + 'own wallet costs nothing \u2014 use the link below.';
        };
        /* `oninput =`, not addEventListener — every other binding in this module is a property
           assignment, and the scenario harness stubs elements accordingly. Reaching for a listener
           here threw `amtEl.addEventListener is not a function` and took out 28 tip scenarios. */
        const amtEl = r.querySelector('#mw-me-amt');
        if(amtEl) amtEl.oninput = restate;
        Array.prototype.forEach.call(r.querySelectorAll('[data-mw-amt]'), b => {
          b.onclick = () => { const el = r.querySelector('#mw-me-amt');
            if(el){ el.value = b.getAttribute('data-mw-amt'); try{ el.focus(); }catch(_){ } restate(); } };
        });
        restate();
        const go = r.querySelector('#mw-me-send');
        go.onclick = async () => {
          const val = String((r.querySelector('#mw-me-amt') || {}).value || '').trim();
          if(!(amount(val) > 0)){ PC.toast('enter an amount greater than zero'); return; }
          if(amount(val) > amount(s.unlocked_balance)){
            // Same rule: name the amount they can actually send.
            const have = xmr(s.unlocked_balance, false);
            const mins = Math.max(1, Number(s.blocks_to_unlock) || 0) * 2;
            PC.toast(amount(s.balance) > amount(s.unlocked_balance)
              ? ('only ' + have + ' XMR can be sent right now — the rest unlocks in about ' + mins + ' minutes')
              : ('only ' + have + ' XMR is available to send right now'));
            return;
          }
          go.disabled = true; go.textContent = 'Sending…';
          try{
            const out = await request('/api/wallet/xmr/me/pay', {method:'POST',
              headers:{'Accept':'application/json','Content-Type':'application/json'},
              body: JSON.stringify({payments:[{address:opts.address, amount:val}]})});
            PC.closeModal(); PC.toast('\u0271 tip sent');
            _meAt = 0;                                    // the balance just changed
            if(typeof opts.onSent === 'function') opts.onSent(val, (out.tx_hash_list||[])[0] || '');
          }catch(e){
            // Same rule as the node wallet's send: a timeout is an unknown, not a failure.
            const msg = (e && e.message) || String(e);
            const unsure = /may have been sent|did not answer in time/i.test(msg);
            go.disabled = unsure; go.textContent = unsure ? 'Check your history' : 'Send tip';
            PC.toast(unsure ? msg : ('tip not sent: ' + msg));
          }
        };
      });
  }

  /* A WALLET THAT DOES NOT ANSWER MUST NOT EAT THE TIP.
   *
   * `request()` begins with `ensureAiSession()`, which for a Nostr login mints a bearer THROUGH THE
   * SIGNER — that can wait on a phone, on a human approval, or on nothing at all. Reported as "i am
   * trying to zap a post and chose Monero wallet and nothing happens": the tip button awaited a
   * probe that never resolved, so no dialog ever opened and the non-custodial QR flow — which needs
   * no session whatsoever — was never reached.
   *
   * The deadline is on the PROBE, not on the caller. Bounding the whole flow could let a dialog open
   * seconds after the fallback had already drawn one; bounding the probe means a slow wallet simply
   * declines, and the flow that always works takes over.
   *
   * It is not a coin toss between wallets: both probes are warmed when this module loads, so by the
   * time anybody clicks the answer is already here. This only fires when something is genuinely
   * stuck, and there a dead button is the worse outcome. */
  const PROBE_DEADLINE_MS = 2500;
  /* TWO KINDS OF "NO ANSWER", AND THEY ARE NOT THE SAME THING TO SAY.
   *   - the deadline expired      -> something is stuck; say so and offer Retry
   *   - the probe answered null   -> the app session is not ready yet (boot, sign-in in progress);
   *                                  a spinner is the honest answer and a "did not answer" card is
   *                                  a lie told to somebody who is three seconds from being ready.
   * The timeout resolves to a sentinel so the caller can tell them apart. */
  const TIMED_OUT = {__timeout:true};
  function _bounded(promise){
    return Promise.race([
      Promise.resolve(promise).catch(() => null),
      new Promise(r => setTimeout(() => r(TIMED_OUT), PROBE_DEADLINE_MS)),
    ]);
  }

  async function tip(opts){
    /* A FAILURE IS NOT A DURABLE ANSWER — and trusting one is what broke this.
     *
     * `warm()` asks as soon as the module loads, which can be BEFORE the wallet session is usable:
     * that probe 401s, `state` becomes `available:false`, and a tip that trusts the cache at any age
     * reads that stale failure for the rest of the page's life. Reported as "i open a post and click
     * zap, it's still not using the fucking built-in monero wallet" — the wallet was reachable and
     * funded the whole time; the client had latched a "no" recorded before it could have been a yes.
     *
     * Exactly the shape this codebase keeps paying for: a latch set BEFORE the attempt it describes.
     *
     * So only a POSITIVE answer is cached, and only briefly. Anything else asks again — which is
     * still deterministic, because the answer comes from the wallet rather than from a timer. */
    let s = (state && state.available && Date.now()-checkedAt < 30000) ? state : null;
    if(!s){
      /* `/status` authenticates the operator and names the network without touching wallet-rpc.
       * Use it as the click-time availability check. The old path waited for balance, address and
       * history RPCs; two browser/desktop surfaces asking together made one exceed 2.5s, so the
       * click silently abandoned a funded local wallet as "busy". Keep the detailed probe running
      * for balance display, but never make RPC contention decide whether the local sheet opens. */
      try{
        /* Start this beside the detailed probe, not after it. If the signer itself is stuck both
           deadlines expire together; serial deadlines made one click wait five seconds. */
        const access = _bounded(request('/api/wallet/xmr/status'));
        s = await _bounded(probe(true));
        if(s === TIMED_OUT || !s || (!s.available && s.busy)){
          const meta = await access;
          if(meta && meta !== TIMED_OUT && meta.network){
            /* Invalidate the still-running detailed probe. Its eventual busy result must not close
               this known-authorized path underneath the sheet we are about to open. */
            _probeSeq++;
            s = Object.assign({}, (state && state.available) ? state : {}, meta,
                              {available:true, network:meta.network});
            state=s; checkedAt=Date.now();
          }else s=null;
        }
      }catch(_){ s = null; }
    }
    if(!s||!s.available||!validAddress(opts&&opts.address,s.network))return false;
    /* AN EMPTY WALLET MUST NOT OFFER TO SPEND.
     *
     * Reported as "monero can't even zap" and then "monero rejected request?" — which is exactly
     * what happens: the wallet answers, so this path is taken, the send dialog opens, and the
     * transfer is refused by monero-wallet-rpc because there is nothing to send. On this node the
     * daemon is still catching up, so the balance is legitimately 0 and will be for hours.
     *
     * Answering false hands the tip back to the non-custodial URI/QR flow, which needs no local
     * wallet at all and is the thing that actually works right now. The local wallet takes over
     * again by itself the moment it has spendable funds. */
    /* A WALLET THAT CANNOT PAY MUST NOT STAND IN THE WAY OF PAYING.
     *
     * Three versions of this, and the middle one was mine: refusing on zero SPENDABLE balance sent
     * every zap external for the ~20 minutes after a send (Monero locks the change from a payment
     * for 10 blocks), so the built-in wallet "stopped working" after each use. Keeping the built-in
     * sheet instead fixed that and broke something worse — the sheet opens, refuses the amount, and
     * the zap cannot be made at all. Reported as "i can't zap again because it says I have to wait
     * 18 min despite nothing pending in my wallet": nothing IS pending; it is the change from their
     * own sends, and the number was right while the behaviour was useless.
     *
     * What somebody clicking ɱ wants is to pay. So a wallet with nothing spendable hands the tip to
     * the external flow — which works right now — and SAYS why, so the built-in wallet going quiet
     * for a while is explained rather than mysterious. It takes over again by itself. */
    const spendable = Number(String(s.unlocked_balance == null ? 0 : s.unlocked_balance).replace(/,/g,''));
    if(s.unlocked_balance != null && (!Number.isFinite(spendable) || spendable <= 0)){
      const held = Number(String(s.balance == null ? 0 : s.balance).replace(/,/g,''));
      const mins = Math.max(1, Number(s.blocks_to_unlock) || 0) * 2;
      if(Number.isFinite(held) && held > 0){
        try{ PC.toast('local wallet unlocks in ~' + mins + ' min (change from your last payment) — using your external wallet'); }catch(_){ }
      }
      return false;
    }
    sendDialog(opts||{});return true;
  }
  async function openReceive(){
    const s=await probe(true);
    if(!s.available){if(PC&&PC.switchView)PC.switchView('wallet');return false;}
    receiveDialog();return true;
  }
  async function openSend(){
    const s=await probe(true);
    if(!s.available){if(PC&&PC.switchView)PC.switchView('wallet');return false;}
    sendDialog({});return true;
  }
  function boot(){
    if(booted)return;PC=root.__PC;if(!PC)return setTimeout(boot,40);booted=true;
    /* WHAT THIS NODE CHARGES, for callers with no wallet sheet of their own — the external tip
       flow states it too, so there is no path on which the fee is invisible. Whichever probe has
       run supplies it; both endpoints carry the same number. */
    const feePercent = () => Number((_meState && _meState.fee_percent)
                                    || (state && state.zap_fee_percent) || 0) || 0;
    root.PCMoneroWallet={render:()=>render(false),tip,meTip,meProbe,probe,openReceive,openSend,uri,validAddress,feePercent,_format:xmr};
    /* Ask once, now. A tip taken from cache is instant AND deterministic; the alternative is
       deciding on a stopwatch, which is what made the chooser differ between identical clicks. */
    warm();
  }
  if(typeof module!=='undefined'&&module.exports)module.exports={uri,parsePaymentUri,validAddress,format:xmr,transferView,historyDate};
  if(typeof document!=='undefined')boot();
})(typeof window!=='undefined'?window:globalThis);
