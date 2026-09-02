/* PosterChan Monero micro-wallet UI.
 *
 * This module never handles wallet keys. It talks to the authenticated, same-origin local wallet
 * bridge and treats every failure as "external wallet mode". */
(function(root){
  'use strict';
  let PC=null, state=null, checkedAt=0, booted=false;
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
    if(!force && Date.now()-checkedAt<8000) return state;
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
      state={available:true,network:meta.network,warning:meta.warning,balance:bal.balance,
        unlocked_balance:bal.unlocked_balance,
        address:addr.address||(((addr.addresses||[])[0]||{}).address)||'',transfers};
    }catch(e){
      const detail=(e&&e.message)||String(e||'local wallet unavailable');
      try{console.error('[monero wallet] probe failed',e);}catch(_){}
      /* BUSY EITHER WAY, whoever's clock ran out first. The node answers 503 with its own wording
         when its RPC budget (8s by default) expires, which is the usual case. But that budget is an
         operator setting allowed up to 30s, and this client aborts at 20 — above 20 the abort wins
         and the message is ours, not the node's. Matching only the node's wording would make the
         "catching up" card quietly stop appearing on exactly the nodes whose wallet is slowest. */
      state={available:false,error:detail,network:'stagenet',
             busy:/still reading the chain|is busy|did not answer within/i.test(String(detail||''))};
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
    if(!PC||PC.VIEW!=='wallet')return;
    const f=document.getElementById('feed'); if(!f)return;
    if(!s.available){f.innerHTML='<div class="mw-wrap"><header class="mw-head"><span class="mw-logo">ɱ</span><div><h2>Monero Wallet</h2><span class="mw-net">LOCAL WALLET</span></div></header>'+warning(s)+(s.busy?busyHtml():fallbackHtml(s.error))+'</div>'; bind(); return;}
    const address=String(s.address||''), balance=s.balance_atomic!=null?s.balance_atomic:s.balance;
    f.innerHTML='<div class="mw-wrap"><header class="mw-head"><span class="mw-logo">ɱ</span><div><h2>Monero Wallet</h2><span class="mw-net">'+esc((s.network||'stagenet').toUpperCase())+(s.network==='stagenet'?' · testing only':'')+'</span></div><button class="btn btn-ghost small" id="mw-refresh">Refresh</button></header>'
      +warning(s)+'<section class="mw-balance"><span>Available balance</span><strong>'+xmr(balance,s.balance_atomic!=null)+' <small>XMR</small></strong><span class="muted small">'+xmr(s.unlocked_balance_atomic!=null?s.unlocked_balance_atomic:balance,s.unlocked_balance_atomic!=null||s.balance_atomic!=null)+' XMR unlocked</span></section>'
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
  function sendDialog(opts){
    opts=opts||{}; const preset=validAddress(opts.address,state&&state.network)?opts.address:'';
    PC.modal('<div class="mw-modal"><h3>'+(preset?'Tip '+esc(opts.name||'with Monero'):'Send Monero')+'</h3>'+warning()+'<button type="button" class="btn btn-cyan full mw-scan" id="mw-scan">Scan wallet QR</button><div class="mw-scan-stage hidden" id="mw-scan-stage"><video playsinline muted></video><span>Point at a Monero payment QR…</span><button type="button" class="btn btn-ghost small" id="mw-scan-cancel">Cancel scan</button></div><label>Recipient address<input class="input" id="mw-to" value="'+esc(preset)+'" autocomplete="off" spellcheck="false"></label><label>Amount (XMR)<input class="input" id="mw-amount" type="number" min="0.000000000001" step="0.0001" inputmode="decimal"></label><label>Note (stored only in your wallet)<input class="input" id="mw-note" maxlength="120"></label><button class="btn btn-neon full" id="mw-review">Review payment</button></div>',r=>{
      r.querySelector('#mw-scan').onclick=()=>scanPayment(r);
      r.querySelector('#mw-review').onclick=()=>{
        let to=r.querySelector('#mw-to').value.trim();
        // Pasting a complete payment URI into the address field is the permission-free scanner
        // fallback. Parse it as data and populate fields; never navigate to or render its contents.
        if(/^monero:/i.test(to)){if(!putPayment(r,to))return;to=r.querySelector('#mw-to').value.trim();}
        const val=r.querySelector('#mw-amount').value.trim(), note=r.querySelector('#mw-note').value.trim();
        if(!validAddress(to,state&&state.network)){PC.toast('check the Monero address for this network');return;}
        if(!(amount(val)>0)){PC.toast('enter an amount greater than zero');return;}
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
        }catch(e){button.disabled=false;button.textContent='Send now';PC.toast('payment not sent: '+((e&&e.message)||e));}
      };
    });
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
    const s=await probe(!!force);
    if(PC.VIEW!=='wallet')return;
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
  const TIP_WAIT_MS = 1200;
  async function tip(opts){
    let s = (state && Date.now()-checkedAt < 8000) ? state : null;
    if(!s){
      const asked = Promise.resolve(probe(false)).catch(()=>null);
      s = await Promise.race([asked, new Promise(r=>setTimeout(()=>r(null), TIP_WAIT_MS))]);
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
    const spendable = Number(String(s.unlocked_balance != null ? s.unlocked_balance : s.balance).replace(/,/g,''));
    if(!Number.isFinite(spendable) || spendable <= 0) return false;
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
    root.PCMoneroWallet={render:()=>render(false),tip,probe,openReceive,openSend,uri,validAddress,_format:xmr};
  }
  if(typeof module!=='undefined'&&module.exports)module.exports={uri,parsePaymentUri,validAddress,format:xmr,transferView,historyDate};
  if(typeof document!=='undefined')boot();
})(typeof window!=='undefined'?window:globalThis);
