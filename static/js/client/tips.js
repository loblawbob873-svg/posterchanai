/* Tipping — the ⚡ method sheet, Lightning zaps (NWC or an invoice), Monero tips (built-in wallet or
 * any wallet), Bitcoin Cash tips, the private Concord zap, and the notes that record them. Split out
 * of app.js.
 *
 * Loaded on first use: app.js keeps a small block of entry points (search for `_tipsDeps`) and
 * builds this factory the first time somebody tips. The code below is BYTE-IDENTICAL to what it
 * replaced in app.js apart from its reads of app.js's live `let` bindings, which the parser rewrote
 * to `S.<name>` (getters/setters on `dep.state`) at exact identifier offsets.
 *
 * Stayed in app.js: what cards and settings read synchronously — amounts and senders of zaps,
 * presets, the NWC connection, LNURL resolution, and the Bitcoin Cash address helpers.
 */
window.PCTipsFactory = function(dep){
  const S = dep.state;   // live app.js bindings: S.CFG, S.GUEST, S.ME
  const {
    $, $$, NT, Nwc, _lightningAddress, _notePaymentXmr, _payTarget, _paymentAddress,
    _paymentChoices, _prefTouched, _withModule, _xmrFeePct, bchOf, bchPresets, closeModal,
    copyValue, corsJson, enc, isBchAddr, isXmrAddr, lnurlResolve, modal, profOf, publish, qrImg,
    saveClientPrefsNostr, sign, toast, uiConfirm, xmrOf, xmrPresets, zapPresets,
  } = dep;
  // Combined tip entry (the ⚡ button). If the author supports BOTH Lightning and Monero, let the user
  // pick; otherwise go straight to whichever they have (Lightning is the default, so doZap still surfaces
  // "no lightning address" when they have neither).
  function _tipMethodSheet(profile,methods,go){
    if(methods.length>=2){
      modal(`<h3>Tip ${enc(profile.name||profile.display_name||'')}</h3>
        <p class="muted small">How would you like to tip?</p>
        <div class="tip-choices">${methods.map(([k,l,d,c])=>`<button class="btn ${c} full" data-m="${k}">${l}<span class="muted small"> — ${d}</span></button>`).join('')}</div>`,
        root=>{ $$('.tip-choices [data-m]',root).forEach(b=> b.onclick=()=>{ closeModal(); go(b.dataset.m); }); });
    } else if(methods.length===1) go(methods[0][0]);
    else go('ln');
  }
  async function doTip(noteId, pk, cardXmr, cardIsNote=true){
    const viewer=S.ME&&S.ME.pubkey;
    const p=profOf(pk)||{};
    const hasLn=!!(await _lightningAddress(pk,p));
    const ev=noteId?Store.get(noteId):null;
    // Prefer the address resolved at render (passed from the card) — the note may since have been evicted
    // from Store, which would otherwise drop its per-note monero_address tag and misroute the tip.
    const xmrAddr = (cardIsNote && cardXmr && isXmrAddr(cardXmr)) ? cardXmr : (_notePaymentXmr(ev) || await _paymentAddress(pk,'monero',xmrOf(p)));
    const hasXmr=isXmrAddr(xmrAddr);
    const bchAddr=await _paymentAddress(pk,'bitcoincash',bchOf(p)),hasBch=isBchAddr(bchAddr);
    // Whatever payment routes the author advertises, offered together. 2+ → a chooser; exactly 1 → straight
    // in; none → doZap (which shows the "no lightning address" toast).
    const methods=[];
    if(hasLn)  methods.push(['ln',  '⚡ Lightning',      'instant zap',              'btn-neon']);
    if(hasXmr) methods.push(['xmr', 'ɱ Monero',         'private, from your wallet', 'btn-cyan']);
    if(hasBch) methods.push(['bch', '🟢 Bitcoin Cash',  'on-chain, from your wallet','btn-cyan']);
    const targets=await _paymentChoices(pk);
    const first=new Set();
    targets.forEach((t,i)=>{
      const handled=(t.type==='lightning'&&hasLn)||(t.type==='monero'&&hasXmr)||(t.type==='bitcoincash'&&hasBch);
      if(handled&&!first.has(t.type)){first.add(t.type);return;}
      methods.push(['target-'+i,enc(PCPaymentTargets.names[t.type]||t.type),enc(t.address),'btn-ghost']);
    });
    if((S.ME&&S.ME.pubkey)!==viewer)return;
    const go=m=>{ if((S.ME&&S.ME.pubkey)!==viewer)return;
      if(m==='ln') doZap(noteId,pk); else if(m==='xmr') doXmrTip(noteId,pk,xmrAddr);
      else if(m==='bch') doBchTip(pk,bchAddr);else if(m.startsWith('target-'))_payTarget(noteId,pk,targets[Number(m.slice(7))]); };
    _tipMethodSheet(p,methods,go);
  }
  async function doZap(noteId, pk, selectedAddress){
    const viewer=S.ME&&S.ME.pubkey;
    const p=profOf(pk); const addr=await _lightningAddress(pk,p);
    if((S.ME&&S.ME.pubkey)!==viewer)return;
    const destination=selectedAddress||addr;
    if(!destination){ toast('no lightning address on this profile'); return; }
    _lightningAmountSheet(p,amt=>{if((S.ME&&S.ME.pubkey)===viewer)_runZap(noteId,pk,amt,destination);});
  }
  function _lightningAmountSheet(profile,onAmount){
    const presets=zapPresets();
    modal(`<h3><svg class="ic h-ic" aria-hidden="true"><use href="#i-zap"></use></svg>Zap ${enc(profile.name||profile.display_name||'')}</h3>
      <div class="zap-presets">${presets.map(a=>`<button class="zap-amt" data-amt="${a}">${a>=1000?(a/1000)+'k':a} sats</button>`).join('')}</div>
      <div class="row" style="gap:8px;margin-top:10px"><input class="input" id="zap-custom" type="number" min="1" placeholder="custom amount (sats)"><button class="btn btn-neon small" id="zap-go"><svg class="ic b-ic" aria-hidden="true"><use href="#i-zap"></use></svg>Zap</button></div>`,
      root=>{
        $$('.zap-amt',root).forEach(b=> b.onclick=()=>{ closeModal(); onAmount(+b.dataset.amt); });
        $('#zap-go',root).onclick=()=>{ const v=parseInt(($('#zap-custom',root)||{}).value||'0',10); if(v>0){ closeModal(); onAmount(v); } else toast('enter an amount'); };
        const ci=$('#zap-custom',root); if(ci) ci.addEventListener('keydown',e=>{ if(e.key==='Enter') $('#zap-go',root).click(); });
      });
  }
  async function _runZap(noteId, pk, amt, selectedAddress){
    const viewer=S.ME&&S.ME.pubkey,current=()=>S.ME&&S.ME.pubkey===viewer;
    const p=profOf(pk); const addr=selectedAddress||await _lightningAddress(pk,p);
    if(!current() || !addr || !amt || amt<1) return;
    toast('preparing zap…');
    try{
      const lnurl=await lnurlResolve(addr);
      if(!current())return;
      if(!lnurl||!lnurl.callback){ toast('couldn\'t resolve '+addr); return; }
      const msat=amt*1000;
      let url=lnurl.callback+(lnurl.callback.includes('?')?'&':'?')+'amount='+msat;
      if(lnurl.allowsNostr){
        const zr=await sign(9734,'',[['relays',S.CFG.relay_url||''],['amount',String(msat)],['p',pk]].concat(noteId?[['e',noteId]]:[]));
        if(!current())return;
        url+='&nostr='+encodeURIComponent(JSON.stringify(zr));
      }
      const inv=await corsJson(url);
      if(!current())return;
      const pr=inv && inv.pr; if(!pr){ toast('no invoice'+(inv&&inv.reason?': '+inv.reason:'')); return; }
      // 1) an installed WebLN extension (Alby etc.) — the most direct one-click path → 2) a
      // configured NWC wallet (great when there's no extension, e.g. on mobile) → 3) show the invoice.
      if(window.webln){ try{ await window.webln.enable(); if(!current())return; await window.webln.sendPayment(pr); toast('⚡ zapped '+amt+' sats'); return; }catch(e){} }
      if(!current())return;
      if(Nwc.configured()){ try{ toast('paying via your wallet…'); await Nwc.payInvoice(pr); toast('⚡ zapped '+amt+' sats'); return; }
        catch(e){ toast('wallet: '+((e&&e.message)||e)); } }
      invoiceModal(pr, amt);
    }catch(e){ toast('zap failed: '+e.message); }
  }
  /* Armada CORD.md private zaps omit both LNURL `nostr` and `comment`, then announce the paid
   * invoice and its preimage inside the sealed channel. External invoice handoff cannot provide
   * that proof, so this is intentionally limited to WebLN/NWC wallets that return it. */
  async function payPrivateConcordZap(pk, amountSats){
    const profile=profOf(pk)||{},addr=await _lightningAddress(pk,profile),amount=Number(amountSats);
    if(!addr)throw new Error('this member has no Lightning address');
    if(!Number.isSafeInteger(amount)||amount<1)throw new Error('enter a whole-sat amount');
    const lnurl=await lnurlResolve(addr);if(!lnurl||!lnurl.callback)throw new Error('could not resolve '+addr);
    const amountMsats=amount*1000;
    if((Number(lnurl.min)||0)>amountMsats||(Number(lnurl.max)||Infinity)<amountMsats)throw new Error('amount is outside this wallet\'s limits');
    const url=lnurl.callback+(lnurl.callback.includes('?')?'&':'?')+'amount='+amountMsats;
    const invoice=await corsJson(url),bolt11=invoice&&invoice.pr;if(!bolt11)throw new Error('wallet did not return an invoice'+(invoice&&invoice.reason?': '+invoice.reason:''));
    let paid=null;
    if(window.webln){await window.webln.enable();paid=await window.webln.sendPayment(bolt11);}
    else if(Nwc.configured())paid=await Nwc.payInvoice(bolt11);
    else throw new Error('connect an NWC or WebLN wallet; private zaps need payment proof');
    const preimage=String(paid&&(paid.preimage||(paid.result&&paid.result.preimage))||'').toLowerCase();
    if(!/^[0-9a-f]{64}$/.test(preimage))throw new Error('payment completed, but the wallet did not return proof for the room tally');
    return {bolt11,preimage,amountMsats};
  }
  /* Concord uses the same payment discovery and Monero wallet sheets as Social. Keep those rules
   * here: duplicating profile-field parsing or the three Monero wallet fallbacks in concord.js
   * would make the two choosers disagree as soon as either format changes. Lightning can publish a
   * cryptographically verified sealed tally; Monero remains a private on-chain payment whose
   * existing flow posts the sender's acknowledgement after payment. */
  async function startConcordTip(pk,onLightningAmount){
    const viewer=S.ME&&S.ME.pubkey;
    const profile=profOf(pk)||{},methods=[];
    if(await _lightningAddress(pk,profile))methods.push(['ln','⚡ Lightning','instant zap','btn-neon']);
    const xmr=await _paymentAddress(pk,'monero',xmrOf(profile));
    if(isXmrAddr(xmr))methods.push(['xmr','ɱ Monero','private, from your wallet','btn-cyan']);
    if((S.ME&&S.ME.pubkey)!==viewer)return;
    _tipMethodSheet(profile,methods,method=>{
      if((S.ME&&S.ME.pubkey)!==viewer)return;
      if(method==='xmr')return doXmrTip(null,pk,xmr);
      if(!methods.some(x=>x[0]==='ln'))return toast('this member has no Lightning or Monero address');
      _lightningAmountSheet(profile,onLightningAmount);
    });
  }
  function invoiceModal(pr, amt){
    modal(`<h3><svg class="ic h-ic" aria-hidden="true"><use href="#i-zap"></use></svg>Zap ${amt} sats</h3><p class="muted small">Pay with your Lightning wallet:</p>
      <a class="btn btn-neon full" href="lightning:${enc(pr)}">Open in wallet</a>
      <div class="keybox" style="margin-top:10px"><code id="z-inv">${enc(pr)}</code></div>
      <button class="btn btn-cyan full" id="z-copy">Copy invoice</button>`, root=>{
      $('#z-copy',root).onclick=()=>{ copyValue(pr, 'invoice copied', 'Lightning invoice:'); };
    });
  }
  // ---------- Monero tips (non-custodial) ----------
  // Mirrors the zap UX but there's no LNURL/invoice: the recipient publishes an XMR address in their
  // kind-0, and the SENDER pays from their own wallet (scan the QR, or the monero: deeplink opens a
  // wallet app prefilled). Nothing touches this server at all now — the QR is drawn in the page, so a
  // tip works offline and on a build with no instance. On confirmation we post a public kind-1 tip
  // note (the chosen "always post" behaviour) crediting the recipient — no cryptographic receipt for XMR.
  /* A Monero or BCH tip is INVISIBLE until the sender says so, and nothing here can find out.
   *
   * Lightning has a zap receipt: the recipient's own wallet publishes a kind-9735 and the
   * notification writes itself. An on-chain address tip has no receipt at all — Monero is private by
   * construction and nothing in this app watches a chain — so the only signal the recipient can ever
   * get is the tip NOTE the sender posts with "I sent it". Close the dialog instead and the money
   * arrives with nobody told: reported as "I received a Monero zap but no notification — the sender
   * had to DM me". Checked against the relays afterwards; no tip note was ever published, which is
   * exactly what that report looks like from this end.
   *
   * So a dismissal ASKS, once, rather than being the silent path. Only when the sender got far
   * enough to plausibly have paid — they opened their wallet, copied the address, or sat with the QR
   * on screen long enough to scan it (the desktop path, which leaves no other trace). Never
   * automatic: posting on someone's behalf because they closed a dialog would be worse than the
   * silence it replaces. */
  const _TIP_DWELL_MS = 10000;
  function _tipTellOnDismiss(root, opts){
    const st = { engaged:false, posted:false, opened:Date.now() };
    const host = document.getElementById('modal-root');
    if(S.GUEST || !root || !host) return st;                 // a guest cannot post the note anyway
    const obs = new MutationObserver(()=>{
      if(root.isConnected) return;                          // still open
      obs.disconnect();
      if(st.posted) return;                                 // "I sent it" — already told them
      if(!st.engaged && Date.now()-st.opened < _TIP_DWELL_MS) return;   // opened, thought better of it
      // After the modal has gone, so this is not a dialog stacked on a dialog.
      setTimeout(async ()=>{
        try{
          if(opts.shouldAsk&&!opts.shouldAsk())return;
          if(await uiConfirm(opts.ask, { ok:'Yes — tell them', cancel:'Not yet' })&&(!opts.shouldAsk||opts.shouldAsk())) opts.onYes();
        }catch(_){}
      }, 80);
    });
    obs.observe(host, { childList:true, subtree:true });
    return st;
  }
  async function doXmrTip(noteId, pk, cardXmr, cardIsNote=true){
    const viewer=S.ME&&S.ME.pubkey;
    const p=profOf(pk); const ev=noteId?Store.get(noteId):null;
    // Prefer the render-time address (passed from the card) so an evicted note doesn't lose its per-note tag.
    const addr = (cardIsNote && cardXmr && isXmrAddr(cardXmr)) ? cardXmr : (_notePaymentXmr(ev) || await _paymentAddress(pk,'monero',xmrOf(p)));
    if((S.ME&&S.ME.pubkey)!==viewer)return;
    if(!isXmrAddr(addr)){ toast('no Monero address on this post or profile'); return; }
    /* A local PosterChan micro-wallet gets first refusal. Its availability probe is deliberately
       fail-closed: browsers, old APKs and a stopped wallet service continue into the URI/QR flow
       below, so tipping never depends on this optional integration.

       THE MODULE HAS TO BE LOADED BEFORE IT CAN BE ASKED. `window.PCMoneroWallet` is set by
       monero-wallet.js, which is lazy-loaded when the Wallet SCREEN is opened — so in any session
       where the user had not visited Wallet, this test was false and the built-in wallet was never
       considered at all. Reported as "monero android app not using built-in wallet! desktop works
       but not android": nothing differed between the platforms except whether that screen had been
       opened in that session. `_withModule` is a no-op once loaded and answers null where the file
       is absent, so the fail-closed fallback below is unchanged. */
    try{
      const _xmrWallet = window.PCMoneroWallet
        || await _withModule('monero-wallet.js', 'PCMoneroWallet');
      if((S.ME&&S.ME.pubkey)!==viewer)return;
      const _tipOpts = {
        address:addr, name:p.name||p.display_name||'anon', noteId, pubkey:pk,
        /* THE SAME AMOUNTS THE EXTERNAL FLOW OFFERS. They are a user setting (`xmrPresets`, synced
           across devices), so they are passed in rather than duplicated in the wallet module —
           two lists of "your usual tip" would drift the first time somebody edited one. The last
           amount sent is offered the same way it is below. */
        presets:xmrPresets(), amount:ClientSettings.get('xmrLastAmt','')||'',
        onSent:(amount, txid, delivery={})=>{
          if((S.ME&&S.ME.pubkey)!==viewer)return;
          // Remember it here too, so tipping from the built-in wallet feeds the same memory the
          // external flow writes — otherwise your usual amount depends on which path you took.
          try{ if(amount){ ClientSettings.set('xmrLastAmt', String(amount)); _prefTouched.add('xmrTip');
                           saveClientPrefsNostr({ xmrTip: String(amount) }); } }catch(_){ }
          if(!delivery.doNotPost)_postXmrTipNote(noteId, pk, amount, addr, txid||'', '');
        }
      };
      /* THREE PATHS, IN THIS ORDER, AND EVERY ONE OF THEM MAY DECLINE.
       *   1. the NODE's wallet — the operator's own, admin-only, so a 403 for everybody else;
       *   2. the USER's wallet — held by this node for the person signed in (custodial);
       *   3. the URI/QR flow below — their own wallet, needs nothing from us and always works.
       * Each answers false when it cannot help, so the non-custodial path is never taken away. */
      if(_xmrWallet && await _xmrWallet.tip(_tipOpts)) return;
      if(_xmrWallet && _xmrWallet.meTip && await _xmrWallet.meTip(_tipOpts)) return;
    }catch(_){}
    if((S.ME&&S.ME.pubkey)!==viewer)return;
    const name=enc(p.name||p.display_name||'anon');
    const uri=a=>'monero:'+addr+(a?('?tx_amount='+encodeURIComponent(a)):'');
    /* THE THIRD PATH SAID NOTHING ABOUT THE FEE, AND IT IS THE ONE EVERY DECLINE FALLS INTO.
     *
     * Reported four times as "I see nothing about service fees when I zap". Both wallet sheets DO
     * state it — the operator's, and the per-user one down to "they receive 0.0098 XMR". This flow
     * did not, so on any node where the built-in wallet is unavailable, locked or empty the fee was
     * invisible on the only path left, and an operator who had just configured one could not tell
     * working-as-intended from a setting that never saved.
     *
     * What it says is that nothing is taken HERE, which is true and is the point: this pays them
     * directly from a wallet the server never touches. Saying so makes the fee legible as a
     * property of the custodial wallets rather than as a silence. */
    const _xmrFee = _xmrFeePct();
    const _extFee = _xmrFee > 0
      ? `<p class="muted small">No service fee on this route — it pays them directly from your own
           wallet. A tip sent from a wallet this server holds for you carries ${enc(String(_xmrFee))}%.</p>`
      : '';
    modal(`<h3>ɱ Tip ${name} · Monero</h3>
      <p class="muted small">Enter the amount → Open wallet (it pre-fills that amount) → pay → tap “I sent it”. Non-custodial: nothing touches this server.</p>${_extFee}
      <div class="row" style="gap:8px;margin:8px 0"><input class="input" id="xmr-amt" type="number" min="0" step="0.0001" value="${enc(ClientSettings.get('xmrLastAmt','')||'')}" placeholder="amount (XMR) — fills your wallet & shows in the note"><a class="btn btn-neon small" id="xmr-open" href="${uri('')}"><svg class="ic b-ic" aria-hidden="true"><use href="#i-phone"></use></svg>Open wallet</a></div>
      <div class="xmr-presets" id="xmr-presets">${xmrPresets().map(a=>`<button class="xmr-preset" data-amt="${a}">ɱ ${a}</button>`).join('')}</div>
      <div class="xmr-qr" id="xmr-qr"><div class="muted small">generating QR…</div></div>
      <div class="keybox" style="margin-top:8px"><code id="xmr-addr">${enc(addr)}</code></div>
      ${S.GUEST?'':`<details class="xmr-proof"><summary><svg class="ic h-ic" aria-hidden="true"><use href="#i-key"></use></svg>Attach a verifiable tx proof (advanced, optional)</summary>
        <p class="muted small">Optional — makes the tip publicly verifiable. In your wallet run <code>get_tx_proof &lt;txid&gt; ${enc(addr.slice(0,10))}…</code> and paste both below; anyone can then confirm the payment with <code>check_tx_proof</code>.</p>
        <input class="input" id="xmr-txid" placeholder="transaction id (64 hex)" autocomplete="off" spellcheck="false">
        <textarea class="input" id="xmr-prf" rows="2" placeholder="tx proof signature (OutProofV…)" spellcheck="false"></textarea></details>`}
      ${S.GUEST?'':'<label class="mw-check"><input type="checkbox" id="xmr-quiet-zap"> Do not post this zap</label>'}
      <div class="row" style="gap:8px;margin-top:8px"><button class="btn btn-cyan small" id="xmr-copy">Copy address</button><span class="spacer"></span>${S.GUEST?'':`<button class="btn btn-neon small" id="xmr-sent" title="post a public tip note crediting them"><svg class="ic b-ic" aria-hidden="true"><use href="#i-check"></use></svg>I sent it</button>`}</div>`,
      root=>{
        const amtEl=$('#xmr-amt',root), qrBox=$('#xmr-qr',root), openBtn=$('#xmr-open',root);
        const amtVal=()=>{ const n=parseFloat(amtEl.value); return (isFinite(n)&&n>0)?String(n):''; };   // omit blank / 0 / NaN — never tx_amount=0
        const qrFail='<div class="muted small">QR unavailable — scan or copy the address below.</div>';
        // Drawn in the page (see qrSrc). It was a POST per keystroke to /client/qr, which is why the
        // amount was debounced by 400ms and why an offline reader — or anyone on a build with no
        // server — got "QR unavailable" on a picture of an address the app was already showing them.
        const renderQr=()=>{ qrBox.innerHTML=qrImg(uri(amtVal()), 'Monero tip QR') || qrFail; };
        const sync=()=>{ openBtn.href=uri(amtVal()); };
        sync(); renderQr();
        // Closing this having (probably) paid must not be the silent path — see _tipTellOnDismiss.
        const quiet=$('#xmr-quiet-zap',root),mayPost=()=>!(quiet&&quiet.checked)&&(S.ME&&S.ME.pubkey)===viewer;
        const tell=_tipTellOnDismiss(root, {
          shouldAsk:mayPost,
          ask: 'Did you send the Monero tip? They are only told if you post the tip note — Monero '
             + 'payments are private, so nothing else can tell them.',
          onYes: ()=> mayPost()&&_postXmrTipNote(noteId, pk, amtVal(), addr, '', ''),
        });
        amtEl.addEventListener('input',()=>{ sync(); renderQr(); });
        $$('.xmr-preset',root).forEach(b=> b.onclick=()=>{ amtEl.value=b.dataset.amt; sync(); renderQr(); });   // one-tap amount
        openBtn.addEventListener('click',()=>{ tell.engaged=true; });
        $('#xmr-copy',root).onclick=()=>{ tell.engaged=true; copyValue(addr, 'address copied', 'Copy the Monero address:'); };
        { const s=$('#xmr-sent',root);
          if(quiet)quiet.onchange=()=>{if(s)s.title=quiet.checked?'Close without posting a zap':'Post a public tip note crediting them';};
          /* "Do not post this zap" SUPPRESSES THE POST AND NOTHING ELSE. `xmrLastAmt` is what
             pre-fills the next tip sheet on every route, and on THIS one — the wallet is theirs,
             not ours — it is the only trace of the payment the app keeps at all; both wallet
             routes write it from `onSent` whatever the choice was. Skipping it under the quiet
             branch made "your usual amount" depend on a privacy checkbox: 0.01 sent quietly was
             forgotten, the same 0.01 through either wallet remembered, and nothing said why. */
          const remember=a=>{ if(a){ ClientSettings.set('xmrLastAmt', a); _prefTouched.add('xmrTip'); saveClientPrefsNostr({ xmrTip: a }); } };
          if(s) s.onclick=async()=>{ if(tell.posted||(S.ME&&S.ME.pubkey)!==viewer)return;const a=amtVal();
          if(!mayPost()){remember(a);tell.posted=true;closeModal();return;}
          const txid=(($('#xmr-txid',root)||{}).value||'').trim().toLowerCase();
          const proof=(($('#xmr-prf',root)||{}).value||'').trim();
          if(txid && !/^[0-9a-f]{64}$/.test(txid)){ toast('txid should be 64 hex characters'); return; }
          if(proof && !txid){ toast('a proof also needs its transaction id'); return; }
          if((txid||proof) && !a && !await uiConfirm('Post without the amount? Enter it in the amount box so people see how much you tipped.')) return;
          if(tell.posted||!mayPost())return;
          remember(a);   // remember + sync the amount to Nostr (follows across devices)
          tell.posted=true;   // told them here — the dismissal must not ask again
          closeModal(); _postXmrTipNote(noteId, pk, a, addr, txid, proof); }; }
      });
  }
  async function _postXmrTipNote(noteId, pk, amt, addr, txid, proof){
    try{
      const who='nostr:'+NT().nip19.npubEncode(pk);
      // Point readers at where Monero tipping works (there's no cross-client XMR standard), so the
      // recipient/onlookers know how to receive/send XMR tips on Nostr.
      let body=`ɱ Tipped${amt?(' '+amt+' XMR'):''} ${who} via Monero`;
      if(txid) body+=`\n\nTx: ${txid}`+(proof?' 🔐 verifiable — check_tx_proof (proof attached)':'');
      body+=`\n\n— sent with PosterChan AI; add your XMR address at https://poster.place to receive Monero tips too`;
      const tags=[['p',pk],['t','monerotip'],['t','monero']].concat(noteId?[['e',noteId]]:[]).concat(amt?[['amount_xmr',String(amt)]]:[]);
      // Self-contained Monero tx proof (verify with check_tx_proof <txid> <addr> "" <proof>). One tag so
      // it can't be confused with the author's own monero_address tip-jar tag that publish() may add.
      if(proof && txid && isXmrAddr(addr)) tags.push(['monero_proof', txid, String(addr).trim(), proof]);
      else if(txid) tags.push(['txid', txid]);
      { const r=await publish(1, body, tags); if(r && r.ok) toast('ɱ tip note posted'); }   // failure toast by publish()
    }catch(e){ toast('could not post tip note'); }
  }
  // Open the payer's BCH wallet (bitcoincash:<addr>?amount=…), show a QR + copyable address. Optional
  // "I sent it" posts a public tip note crediting them (BCH has no cryptographic zap receipt; a txid,
  // if given, is verifiable on any explorer).
  async function doBchTip(pk,selectedAddress){
    const viewer=S.ME&&S.ME.pubkey,p=profOf(pk);
    const addr=String(selectedAddress||await _paymentAddress(pk,'bitcoincash',bchOf(p))).replace(/^bitcoincash:/i,'').trim();
    if((S.ME&&S.ME.pubkey)!==viewer)return;
    if(!isBchAddr(addr)){ toast('no Bitcoin Cash address on this profile'); return; }
    const name=enc(p.name||p.display_name||'anon');
    const uri=a=>'bitcoincash:'+addr+(a?('?amount='+encodeURIComponent(a)):'');
    modal(`<h3>🟢 Tip ${name} · Bitcoin Cash</h3>
      <p class="muted small">Enter the amount → Open wallet (it pre-fills that amount) → pay. Non-custodial: nothing touches this server.</p>
      <div class="row" style="gap:8px;margin:8px 0;align-items:center"><input class="input" id="bch-amt" type="number" min="0" step="0.0001" style="flex:1 1 auto;min-width:0;margin:0" value="${enc(ClientSettings.get('bchLastAmt','')||'')}" placeholder="amount (BCH) — fills your wallet"><a class="btn btn-neon small" id="bch-open" style="flex:0 0 auto;white-space:nowrap" href="${uri('')}"><svg class="ic b-ic" aria-hidden="true"><use href="#i-phone"></use></svg>Open wallet</a></div>
      <div class="xmr-presets" id="bch-presets">${bchPresets().map(a=>`<button class="xmr-preset bch-preset" data-amt="${a}"><svg class="ic b-ic" aria-hidden="true"><use href="#i-coin"></use></svg>${a}</button>`).join('')}</div>
      <div class="xmr-qr" id="bch-qr"><div class="muted small">generating QR…</div></div>
      <div class="keybox" style="margin-top:8px"><code id="bch-addr">${enc(addr)}</code></div>
      ${S.GUEST?'':`<details class="xmr-proof"><summary><svg class="ic h-ic" aria-hidden="true"><use href="#i-link"></use></svg>Attach the transaction id (optional)</summary>
        <p class="muted small">Optional — makes the tip publicly verifiable on any BCH explorer.</p>
        <input class="input" id="bch-txid" placeholder="transaction id (64 hex)" autocomplete="off" spellcheck="false"></details>`}
      <div class="row" style="gap:8px;margin-top:8px"><button class="btn btn-cyan small" id="bch-copy">Copy address</button><span class="spacer"></span>${S.GUEST?'':`<button class="btn btn-neon small" id="bch-sent" title="post a public tip note crediting them"><svg class="ic b-ic" aria-hidden="true"><use href="#i-check"></use></svg>I sent it</button>`}</div>`,
      root=>{
        const amtEl=$('#bch-amt',root), qrBox=$('#bch-qr',root), openBtn=$('#bch-open',root);
        const amtVal=()=>{ const n=parseFloat(amtEl.value); return (isFinite(n)&&n>0)?String(n):''; };   // omit blank / 0 / NaN
        const qrFail='<div class="muted small">QR unavailable — scan or copy the address below.</div>';
        const renderQr=()=>{ qrBox.innerHTML=qrImg(uri(amtVal()), 'BCH tip QR') || qrFail; };
        const sync=()=>{ openBtn.href=uri(amtVal()); };
        sync(); renderQr();
        // Same silence as Monero's, for the same reason: an on-chain tip has no receipt (see
        // _tipTellOnDismiss). A txid is verifiable AFTER the fact, but only if somebody posts it.
        const tell=_tipTellOnDismiss(root, {
          ask: 'Did you send the Bitcoin Cash tip? They are only told if you post the tip note.',
          onYes: ()=> (S.ME&&S.ME.pubkey)===viewer && _postBchTipNote(pk, amtVal(), addr, ''),
        });
        amtEl.addEventListener('input',()=>{ sync(); renderQr(); });
        $$('.bch-preset',root).forEach(b=> b.onclick=()=>{ amtEl.value=b.dataset.amt; sync(); renderQr(); });
        openBtn.addEventListener('click',()=>{ tell.engaged=true; });
        $('#bch-copy',root).onclick=()=>{ tell.engaged=true; copyValue(addr, 'address copied', 'Copy the BCH address:'); };
        { const s=$('#bch-sent',root); if(s) s.onclick=async()=>{ if((S.ME&&S.ME.pubkey)!==viewer)return; const a=amtVal();
          const txid=(($('#bch-txid',root)||{}).value||'').trim().toLowerCase();
          if(txid && !/^[0-9a-f]{64}$/.test(txid)){ toast('txid should be 64 hex characters'); return; }
          if(a){ ClientSettings.set('bchLastAmt', a); _prefTouched.add('bchTip'); saveClientPrefsNostr({ bchTip: a }); }   // remember + sync across devices
          tell.posted=true;   // told them here — the dismissal must not ask again
          closeModal(); _postBchTipNote(pk, a, addr, txid); }; }
      });
  }
  async function _postBchTipNote(pk, amt, addr, txid){
    try{
      const who='nostr:'+NT().nip19.npubEncode(pk);
      let body=`🟢 Tipped${amt?(' '+amt+' BCH'):''} ${who} via Bitcoin Cash 💚`;
      if(txid) body+=`\n\nTx: https://blockchair.com/bitcoin-cash/transaction/${txid}`;
      body+=`\n\n— sent with PosterChan; add your BCH address at https://poster.place to receive Bitcoin Cash tips too`;
      const tags=[['p',pk],['t','bchtip']].concat(amt?[['amount_bch',String(amt)]]:[]).concat(txid?[['bch_tx',txid]]:[]);
      const r=await publish(1, body, tags); if(r && r.ok) toast('🟢 tip note posted');   // failure toast by publish()
    }catch(e){ toast('could not post tip note'); }
  }

  return {
    doBchTip, doTip, doXmrTip, doZap, payPrivateConcordZap, startConcordTip,
  };
};
