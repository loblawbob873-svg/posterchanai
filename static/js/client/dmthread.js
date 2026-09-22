/* The direct-message conversation pane — the bubbles and their actions, the composer and its
 * drafts, replies, the hide/report menu, and the refresh that keeps an open thread current. Split
 * out of app.js.
 *
 * Loaded on first use: app.js keeps a small block of entry points (search for `_dmThreadDeps`) and
 * builds this factory the first time a conversation is opened. The inbox list, the gift-wrap
 * ingest and the unread badge stayed in app.js — they run from login, with no thread on screen —
 * and `_scheduleDmRefresh` (an arriving message asks for it) refreshes a pane only this module
 * draws, so it does nothing before the module exists. The code below is BYTE-IDENTICAL to what it
 * replaced apart from its reads of app.js's live `let` bindings, which the parser rewrote to
 * `S.<name>` (getters/setters on `dep.state`) at exact identifier offsets.
 */
window.PCDmThreadFactory = function(dep){
  const S = dep.state;   // live app.js bindings: S.CFG, S.ME, S.VIEW, S._dmHandoffScroll, S._dmScrollTop, S.dmActive, S.signer
  const {
    $, $$, NT, _DM_INIT, _DM_STEP, _applyAutoMuteToView, _decorateDmFileAtts, _dmClock,
    _dmDayLabel, _dmFull, _dmShown, _restoreDmScroll, applyEmojis, attachEmojiAutocomplete,
    copyValue, decorateEncAtts, decryptMsg, dmEncOn, dmPeers, dmPickGif, dmPickMedia, emojiName,
    enc, ensureDmInboxList, ingestDM, isMutedAuthor, linkify, needProfile, niceNip05,
    openMenuPopover, profOf, renderMessages, renderProfileView, sendDm, toast, toggleMute,
    uploadBlob, uploadSharedEnc, wireImgAttach,
  } = dep;
  // Coalesce a STORM of incoming-message renders into ONE every 350ms. On load, the NIP-17 sub replays
  // your WHOLE DM history and unwraps each message — rendering per message was the "window keeps moving"
  // thrash (and re-scrolled to bottom each time). One debounced render absorbs the whole burst.
  let _dmRefreshTimer=null, _dmThreadSig='';
  // Hold the message pane at the bottom while it SETTLES. Scrolling once is not enough: bubbles are
  // painted as placeholders and patched after decryption, and images load later still — every one of
  // those makes the pane taller than it was when we measured it, leaving you above the newest message.
  // Re-pin over the next few frames and as each image lands, and give up the moment the user scrolls
  // themselves (they are reading history, not waiting for us).
  function _dmPinBottom(m){
    if(!m) return;
    let stop=false;
    /* WATCH WHERE THE PANE ACTUALLY IS, don't just keep shoving it down.
     *
     * 'touchmove'/'wheel' were the only way out of this loop, and on a phone that is not enough: a
     * fling scrolls on ONE touchmove and then coasts, and the momentum scroll that follows is not a
     * touch event at all. Worse, the loop is re-armed for four seconds by things that happen long
     * after the render — every message decrypting, every attachment decorating — so scrolling up in a
     * DM was undone again and again ("I can't scroll up, it keeps moving me down").
     *
     * So remember the offset we last SET, and compare it to where the pane is now. Anything that
     * moved it other than us is the user, and we stop immediately rather than fight them. Kinetic
     * scrolling, a scrollbar drag, a keyboard — all of them look the same to this and none of them
     * needed their own listener. */
    let mine=-1;
    const pin=()=>{
      if(stop || !m.isConnected) return;
      if(mine>=0 && Math.abs(m.scrollTop-mine)>4){ done(); return; }   // moved by someone who isn't us
      m.scrollTop=m.scrollHeight; mine=m.scrollTop;
    };
    const done=()=>{ stop=true; try{ obs.disconnect(); }catch(_){ } };
    // Watch for the CONTENT changing rather than guessing how long it takes. Decryption patches each
    // bubble as it finishes — over hundreds of milliseconds on a long thread — and a fixed frame budget
    // expired long before that, which is why the pane still opened part-way up.
    const obs=new MutationObserver(pin);
    try{ obs.observe(m, { childList:true, subtree:true, characterData:true }); }catch(_){ }
    ['wheel','touchmove','keydown'].forEach(ev=>m.addEventListener(ev, done, {passive:true, once:true}));
    m.querySelectorAll('img').forEach(im=>{ if(!im.complete) im.addEventListener('load', pin, {once:true}); });
    pin();
    setTimeout(pin, 0);            // after the current layout pass
    setTimeout(done, 4000);        // hard stop — never hold the pane hostage
  }
  let _dmLastPk='';   // which conversation the pane last rendered — a CHANGE means a fresh open
  // What you have TYPED but not sent, per peer. renderDmThread rebuilds the pane — textarea included —
  // and an incoming DM triggers exactly that 350ms after its toast (_scheduleDmRefresh), so mid-sentence
  // the box went empty: the toast and the wipe are the same event. This survives a rebuild from ABOVE us
  // (renderMessages replaces the whole #feed); the reuse path in renderDmThread avoids the rebuild
  // entirely for the common case. It also holds any ATTACHMENT — the strip is derived from the image
  // URLs sitting in this same text (wireImgAttach), so losing the text lost the upload too.
  // MEMORY only, deliberately never ClientSettings/localStorage: an unsent DM must not outlive the tab.
  const _dmDrafts = new Map();
  function _threadSig(pk){ const arr=dmPeers.get(pk)||[]; return pk+'|'+(arr.length?(arr[arr.length-1].id||''):'')+'|'+(_dmShown.get(pk)||_DM_INIT); }
  /* One message's body html. Shared by the first paint and by the patch that lands when a message
   * finishes decrypting — which is EVERY message, since DMs decrypt lazily. The patch used to be a bare
   * `linkify(text)`, so a lazily-decrypted message lost the two things this adds: the reply quote block
   * (a reply showed its leading "> " as body text) and the rumor's own NIP-30 custom emoji (a
   * :shortcode: stayed a literal shortcode). Its caller also has to run decorateEncAtts afterwards — the
   * thread's single decorate pass happens BEFORE decryption, so an encrypted attachment on a message
   * that had not decrypted yet sat at "🔒 decrypting…" for the life of the pane.
   *
   * A reply arrives as "> quoted\n\nmessage" (see _dmReply): render that leading quote as a block so it
   * reads like a reply instead of a stray angle bracket. */
  function _dmBodyHtml(m){
    let text=String(m.text||'');
    // An encrypted URL is a ciphertext locator, not media. Remove only that exact token from the
    // normal linkifier and append a verified/decrypted attachment slot instead.
    for(const a of (m.atts||[])) if(a.enc) text=text.split(a.url).join('').trim();
    const files=(m.atts||[]).map((a,i)=>`<button class="dm-file-att" data-dm-file="${enc(m.id)}" data-ai="${i}">🔒 decrypting attachment…</button>`).join('');
    /* The card is passive until Play, exactly like Social and Concord. PCWebxdc owns one delegated
       handler for every card, so lazy DM decryption/repainting cannot double-bind it. A bare .xdc
       derives its multiplayer topic from xdcMessageId — the stable inner NIP-17 rumor id. */
    let xdcCard='';
    try{
      if(window.PCWebxdc&&PCWebxdc.appOf&&PCWebxdc.cardHtml){
        const app=PCWebxdc.appOf({id:m.xdcMessageId||m.id,kind:m.nip17?14:4,
          tags:m.tags||[],content:text});
        if(app){ text=text.replace(app.url,'').trim(); xdcCard=PCWebxdc.cardHtml(app); }
      }
    }catch(_){}
    const mq = /^>\s?([^\n]*)\n\n([\s\S]*)$/.exec(text);
    const _em = ev => applyEmojis(ev, { tags: m.em||[] });   // the RUMOR's own NIP-30 tags
    const body=mq ? `<span class="b-quote">${enc(mq[1])}</span>${_em(linkify(mq[2]))}` : _em(linkify(text));
    return body+files+xdcCard;
  }
  function _scheduleDmRefresh(){
    if(_dmRefreshTimer || S.VIEW!=='messages') return;
    _dmRefreshTimer=setTimeout(()=>{ _dmRefreshTimer=null; if(S.VIEW!=='messages') return;
      if(S.dmActive){
        // Skip the rebuild if the VISIBLE window is unchanged — the NIP-17 replay streams OLDER history
        // in, which doesn't touch the newest message we show, so re-rendering would just flicker.
        const sig=_threadSig(S.dmActive); if(sig===_dmThreadSig) return; _dmThreadSig=sig;
        renderDmThread(S.dmActive);
      } else renderMessages(); }, 350);
  }
  // ---- message actions: long-press (touch) / right-click (desktop) ----------------------------
  // Reply is sent as a QUOTED PREFIX rather than a NIP-10 e-tag: the kind-14 rumor's tags are built
  // in three separate signer paths (worker, window.nostr, NIP-46), and a tag other clients may not
  // render buys nothing over a quote every client already shows correctly.
  let _dmReply = null;                      // {id, text, mine} for the open thread
  const _DM_HIDDEN_KEY = 'dmHidden';        // ids hidden by "Delete for me" (local, per this client)
  function _dmHidden(){ try{ return new Set(ClientSettings.get(_DM_HIDDEN_KEY, []) || []); }catch(_){ return new Set(); } }
  function _dmHide(id){ const h=_dmHidden(); h.add(id); ClientSettings.set(_DM_HIDDEN_KEY, [...h].slice(-2000)); }
  function _dmQuoteOf(text){
    const one = String(text||'').replace(/\s+/g,' ').trim();
    return one.length > 120 ? one.slice(0,117)+'…' : one;
  }
  function _dmReplyBanner(){
    const bar = $('#dm-replybar'); if(!bar) return;
    if(!_dmReply){ bar.hidden = true; bar.innerHTML=''; return; }
    bar.hidden = false;
    bar.innerHTML = `<div class="dm-rb-body"><span class="dm-rb-who">${_dmReply.mine?'Replying to yourself':'Replying'}</span>
      <span class="dm-rb-txt">${enc(_dmQuoteOf(_dmReply.text))}</span></div>
      <button class="mini" id="dm-rb-x" title="Cancel reply" aria-label="Cancel reply"><svg class="ic x-ic" aria-hidden="true"><use href="#i-close"></use></svg></button>`;
    const x=$('#dm-rb-x',bar); if(x) x.onclick=()=>{ _dmReply=null; _dmReplyBanner(); const i=$('#dm-in'); if(i) i.focus(); };
  }
  function _dmMsgMenu(anchorEl, pk, mid){
    const all = dmPeers.get(pk)||[];
    const m = all.find(x=>String(x.id)===String(mid));
    if(!m) return;
    const items = [['reply','↩ Reply'], ['copy','⧉ Copy text'], ['hide','🗑 Delete for me','danger']];
    openMenuPopover(anchorEl, items, a=>{
      if(a==='reply'){ _dmReply = {id:m.id, text:m.text||'', mine:!!m.mine}; _dmReplyBanner();
        const i=$('#dm-in'); if(i) i.focus(); return; }
      if(a==='copy'){ copyValue(m.text||'', 'copied', 'Copy this message:'); return; }
      if(a==='hide'){ _dmHide(m.id); toast('deleted for you'); renderDmThread(pk); return; }
    });
  }
  // Long-press opens the same menu touch users expect; right-click does it on desktop. 450ms, and a
  // finger that MOVES is a scroll, not a press — otherwise scrolling a thread pops menus constantly.
  function _wireBubbleActions(box, pk){
    if(!box) return;
    box.addEventListener('contextmenu', e=>{ const b=e.target.closest('.bubble'); if(!b) return;
      e.preventDefault(); _dmMsgMenu(b, pk, b.dataset.mid); });
    let t=null, sx=0, sy=0;
    const clear=()=>{ if(t){ clearTimeout(t); t=null; } };
    box.addEventListener('touchstart', e=>{ const b=e.target.closest('.bubble'); if(!b) return;
      const p=e.touches[0]; sx=p.clientX; sy=p.clientY;
      clear(); t=setTimeout(()=>{ t=null; try{ navigator.vibrate && navigator.vibrate(12); }catch(_){}
        _dmMsgMenu(b, pk, b.dataset.mid); }, 450); }, {passive:true});
    box.addEventListener('touchmove', e=>{ const p=e.touches[0];
      if(Math.abs(p.clientX-sx)>10 || Math.abs(p.clientY-sy)>10) clear(); }, {passive:true});
    box.addEventListener('touchend', clear, {passive:true});
    box.addEventListener('touchcancel', clear, {passive:true});
  }

  async function renderDmThread(pk){
    const wrap=$('#dm-thread'); if(!wrap)return;
    // Backfill this conversation's FULL history once (the initial bulk fetch caps at limit:300 across
    // ALL peers, so a busy inbox leaves old threads showing only their newest message). NIP-17 is
    // already fully loaded by its no-limit live sub; this targeted query backfills LEGACY kind-4 for
    // this peer. Runs in the background so the thread still renders instantly from what's cached.
    if(!_dmFull.has(pk)){ _dmFull.add(pk);
      Relay.query([{kinds:[4], authors:[pk], '#p':[S.ME.pubkey]}, {kinds:[4], authors:[S.ME.pubkey], '#p':[pk]}])
        .then(evs=>{ let added=false; for(const e of (evs||[])){ Store.saveEvent(e); if(ingestDM(e)) added=true; }
          if(added) _scheduleDmRefresh(); })   // re-render only if new msgs arrived (no loop: _dmFull set)
        .catch(()=>{});
    }
    const p=profOf(pk); needProfile(pk);
    const _hid=_dmHidden();
    const all=(dmPeers.get(pk)||[]).filter(m=>!_hid.has(m.id));   // "Delete for me" — local only
    // PAGINATION: render only the last N messages (1 on open) so a long thread opens instantly on mobile;
    // "Load older" reveals 20 more each tap.
    const shown=Math.min(all.length, _dmShown.get(pk)||_DM_INIT);
    const start=all.length-shown;
    const msgs=all.slice(start);
    // Was the user pinned to the bottom before this re-render? If they'd scrolled up to read, DON'T
    // yank them back down when a background message lands (part of "the window keeps moving").
    // "Was the user pinned to the bottom?" is only a meaningful question about the SAME conversation.
    // Measured across a switch it carried the previous chat's scroll state into the new one: open a
    // conversation after scrolling up in another and it landed mid-history instead of on the latest
    // message. A different peer is a fresh open, and a fresh open always shows the newest.
    const _prev=$('#dm-msgs');
    const _fresh = (_dmLastPk !== pk);
    const _atBottom = _fresh || !_prev || (_prev.scrollHeight - _prev.scrollTop - _prev.clientHeight < 80);
    const _wantTop=S._dmScrollTop;   // "load older" wants to stay at the top after this render
    const older = start>0 ? `<button class="dm-older" id="dm-older"><svg class="ic b-ic" aria-hidden="true"><use href="#i-upload"></use></svg>Load older (${start})</button>` : '';
    // Paint the thread chrome + bubbles IMMEDIATELY, BEFORE decrypting. Decryption is ECDH+AES in the
    // crypto worker, which on a throttled/high-latency link is often busy verifying the incoming feed —
    // awaiting it first left the whole pane blank ("clicked and no messages show"). Undecrypted bubbles
    // render a "decrypting…" placeholder and get patched in place below (no re-render, no scroll jump).
    // Grouped, dated, timestamped — the three things that make a thread read as a conversation:
    //  * consecutive messages from the SAME sender within 5 minutes stack tightly (one tail, not a
    //    column of identical pills);
    //  * a day separator whenever the date changes;
    //  * a small time on every bubble, so you can tell when something was said.
    const GROUP_GAP = 5 * 60;
    const bubble=(m, prev)=>{
      const startsGroup = !prev || prev.mine!==m.mine || (m.t||0)-(prev.t||0) > GROUP_GAP;
      const newDay = !prev || new Date((prev.t||0)*1000).toDateString() !== new Date((m.t||0)*1000).toDateString();
      const sep = newDay && m.t ? `<div class="dm-day"><span>${enc(_dmDayLabel(m.t))}</span></div>` : '';
      // A reply arrives as "> quoted\n\nmessage" (see _dmReply). Render that leading quote as a
      // block so it reads like a reply instead of a stray angle bracket.
      const body = m.text==null ? '<span class="muted small">decrypting…</span>' : _dmBodyHtml(m);
      return `${sep}<div class="bubble ${m.mine?'out':'in'}${startsGroup?' grp':' cont'}" data-mid="${m.id}">`
        + `<span class="b-txt">${body}</span>`
        + `<span class="b-meta">${enc(_dmClock(m.t))}${m.mine?'<span class="b-tick" title="sent">✓</span>':''}</span></div>`;
    };
    const _msgsHtml = `${older}${msgs.map((m,i)=>bubble(m, msgs[i-1])).join('')}`;
    const _peerName = emojiName(pk, p.name||p.display_name||niceNip05(p.nip05)||(NT().nip19.npubEncode(pk).slice(0,14)+'…'));
    const _muteLabel = isMutedAuthor(pk)?'🔊 Unmute':'🔇 Mute';
    // A refresh of the conversation ALREADY on screen touches only the message list. Rebuilding the
    // pane wholesale is what ate a half-typed message every time a DM arrived, and the draft is not
    // the only casualty: the caret, the focus, the grown height of the box and the list scroll are
    // all state the DOM holds and a rebuild throws away. So when the same peer is already mounted,
    // swap the bubbles and leave everything else — chrome and composer — exactly where it is.
    const _reuse = !!(_prev && _dmLastPk===pk && document.getElementById('dm-in'));
    if(_reuse){
      // Replace the CONTENTS of #dm-msgs, never the element: its long-press and lightbox handlers are
      // bound to the container itself and are not re-bound below, so keeping it is what stops them
      // double-firing. The topbar can still go stale (a mute toggled, a profile that just loaded), so
      // repaint those two in place.
      _prev.innerHTML = _msgsHtml;
      decorateEncAtts(_prev);   // bubbles are rebuilt here, so their 🔒 placeholders are new ones
      _decorateDmFileAtts(_prev);
      { const mb=$('#dm-mute'); if(mb) mb.textContent=_muteLabel; }
      { const nm=wrap.querySelector('.dm-peer-name'); if(nm) nm.innerHTML=_peerName; }
    } else {
    wrap.innerHTML=`<div class="topbar"><button class="mini" id="dm-back" aria-label="Back"><svg class="ic b-ic" aria-hidden="true"><use href="#i-arrow-left"></use></svg></button> <b class="dm-peer-name name" data-prof="${pk}" style="cursor:pointer">${_peerName}</b><span class="spacer"></span><button class="mini dm-lock" id="dm-lock" aria-label="Encrypt attachments">🔒</button><button class="mini" id="dm-mute" title="Mute this sender">${_muteLabel}</button></div>
      <div class="dm-msgs" id="dm-msgs">${_msgsHtml}</div>
      <div class="dm-compose">
        <div class="dm-replybar" id="dm-replybar" hidden></div>
        <div class="dm-atts" id="dm-atts" hidden></div>
        <div class="dm-row">
          <button class="mini" id="dm-attach" title="attach"><svg class="ic b-ic" aria-hidden="true"><use href="#i-paperclip"></use></svg></button>
          <button class="mini" id="dm-files" title="your Files"><svg class="ic x-ic" aria-hidden="true"><use href="#i-folder"></use></svg></button>
          ${S.CFG.gif_enabled?`<button class="mini" id="dm-gif" title="GIF"><svg class="ic b-ic" aria-hidden="true"><use href="#i-film"></use></svg></button>`:''}
          <input type="file" id="dm-file" multiple hidden>
          <textarea class="input dm-in" id="dm-in" rows="1" placeholder="Message…">${enc(_dmDrafts.get(pk)||'')}</textarea>
          <span class="dm-sendstate" id="dm-sendstate" role="status" aria-live="polite"></span>
          <button class="dm-sendbtn" id="dm-send" title="Send" aria-label="Send"><svg class="ic x-ic" aria-hidden="true"><use href="#i-send"></use></svg></button>
        </div></div>`;
    _applyAutoMuteToView();
    // Back must do something on DESKTOP too. It only removed `has-active`, which is what shows the
    // thread as a full-screen overlay on a phone — on a two-pane desktop layout that class changes
    // nothing, so the button looked broken. Now it also clears the open thread and the row highlight,
    // returning the right pane to its empty state on every layout.
    $('#dm-back').onclick=()=>{
      $('#dm-list').classList.remove('has-active');
      S.dmActive=null;
      $$('.dm-peer').forEach(e=>e.classList.remove('active'));
      const th=$('#dm-thread'); if(th) th.innerHTML='<div class="empty">Select a conversation, or start one.</div>';
    };
    { const nm=wrap.querySelector('.dm-peer-name'); if(nm) nm.onclick=()=>renderProfileView(pk); }
    // Mute the DM sender straight from the conversation. Muting drops back to the list (the thread
    // is filtered out); toggleMute re-renders Messages so it disappears immediately.
    // Close the thread only AFTER the mute actually lands — toggleMute now no-ops on a relay failure, so
    // closing first would dismiss the conversation while the mute silently didn't apply.
    { const mb=$('#dm-mute'); if(mb) mb.onclick=async()=>{ const wasMuted=isMutedAuthor(pk); await toggleMute(pk);
        if(!wasMuted && isMutedAuthor(pk)){ S.dmActive=null; const dl=$('#dm-list'); if(dl) dl.classList.remove('has-active'); } }; }
    const inp=$('#dm-in');
    // Paste-to-attach + removable preview strip (📎 Attach / 🌸 Files / 🎬 GIF also feed it via 'input').
    const _syncAtts = wireImgAttach(inp, $('#dm-atts'), {enc:true});
    decorateEncAtts($('#dm-msgs'));   // first paint of this thread
    _decorateDmFileAtts($('#dm-msgs'));
    $('#dm-attach').onclick=e=>{
      const rows=[['file','📎 File']];
      if(window.PCWebxdc&&PCWebxdc.attach) rows.push(['webxdc','🎮 Multiplayer mini app']);
      openMenuPopover(e.currentTarget,rows,a=>{ if(a==='file') $('#dm-file').click(); else if(a==='webxdc') PCWebxdc.attach(inp); });
    };
    // 🔒 is opt-in and OFF by default: an encrypted file is unreadable to anyone not running this
    // client, so it can't be the silent default for a conversation with a Damus user. Remembered per
    // device (not per thread) — someone who encrypts once usually means it.
    //
    // It lives in the THREAD TOPBAR, not in the composer row. Measured at 360px with the GIF button
    // present, a fifth control in that row cut the message box from 150px to 104px — about six
    // visible characters. The topbar carries a name and one button at every width, and a sticky
    // preference belongs there rather than beside the per-message actions anyway.
    { const lk=$('#dm-lock');
      const paint=()=>{ if(!lk) return; const on=dmEncOn();
        lk.classList.toggle('on', !!on);
        lk.title = on ? 'Attachments are encrypted — only this conversation can open them (other Nostr clients cannot)'
                      : 'Attachments upload readable by anyone with the link. Click to encrypt them.'; };
      if(lk) lk.onclick=()=>{ ClientSettings.set('dmEncryptAtts', !dmEncOn()); paint();
        toast(dmEncOn() ? '🔒 Attachments will be encrypted' : '🔓 Attachments upload readable'); };
      paint(); }
    $('#dm-file').onchange=async e=>{ const files=[...e.target.files]; const encOn=dmEncOn();
      for(let i=0;i<files.length;i++){ try{
        if(encOn) toast('encrypting '+(i+1)+'/'+files.length+'…');
        const url=encOn ? await uploadSharedEnc(files[i]) : await uploadBlob(files[i]);
        inp.value+=(inp.value&&!/\s$/.test(inp.value)?' ':'')+url; }catch(err){ toast('upload failed: '+((err&&err.message)||err)); } }
      e.target.value=''; _syncAtts(); inp.focus(); };
    $('#dm-files').onclick=dmPickMedia(inp);
    { const g=$('#dm-gif'); if(g) g.onclick=dmPickGif(inp); }
    let _dmSending=false;
    const send=async()=>{ if(_dmSending) return; const t=inp.value.trim(); if(!t)return; _dmSending=true;   // guard: a 2nd Enter before the send resolves must not send twice
      /* SIGNING IS REMOTE for PosterChan Signer logins and may take seconds. Show that fact on the
       * very first frame; an unchanged icon made a healthy signer round-trip indistinguishable from
       * a dead click, so people pressed Send repeatedly and waited with no idea what was happening. */
      const busyBtn=$('#dm-send'), busyState=$('#dm-sendstate');
      if(busyBtn){ busyBtn.disabled=true; busyBtn.classList.add('busy'); busyBtn.title='Waiting for PosterChan Signer'; busyBtn.setAttribute('aria-label','Waiting for PosterChan Signer'); }
      if(busyState) busyState.textContent=(typeof S.signer!=='undefined' && S.signer && S.signer.mode==='local')?'Sending…':'Waiting for signer…';
      // A reply goes out as a quote block the receiving client already renders, followed by a blank
      // line and the actual message.
      const _body = _dmReply && _dmReply.text
        ? '> ' + _dmQuoteOf(_dmReply.text) + '\n\n' + t
        : t;
      // Drop the draft BEFORE sending, not after: sendDm re-renders the pane the moment our own copy
      // lands, so a draft still in the map at that point would be seeded straight back into the fresh
      // box and the message you just sent would reappear as unsent text. A failure puts it back — the
      // rule stays "clear ONLY on success, a failed send keeps the text + attachment".
      _dmDrafts.delete(pk);
      try{ ensureDmInboxList(); await sendDm(pk, _body); _dmReply=null; _dmReplyBanner();
        // The re-render inside sendDm may have replaced the composer, so clear whichever box is LIVE.
        const box=document.getElementById('dm-in')||inp; box.value=''; _syncAtts();
        // Setting .value in code fires no 'input', so nothing shrank the grown box back to one row or
        // dimmed the send button — 'dm-reset' was listened for and never dispatched.
        box.dispatchEvent(new Event('dm-reset')); }
      catch(e){
        _dmDrafts.set(pk, t);
        const box=document.getElementById('dm-in'); if(box && !box.value){ box.value=t; box.dispatchEvent(new Event('dm-reset')); }
        toast('dm failed: '+((e&&e.message)||e)); }
      finally{ _dmSending=false;
        if(busyBtn && busyBtn.isConnected){ busyBtn.disabled=false; busyBtn.classList.remove('busy'); busyBtn.title='Send'; busyBtn.setAttribute('aria-label','Send'); }
        if(busyState && busyState.isConnected) busyState.textContent='';
      } };
    attachEmojiAutocomplete($('#dm-in'));   // `:shortcode` suggestions in DMs too (the rumor carries the tags)
    $('#dm-send').onclick=send; $('#dm-in').onkeydown=e=>{ if(e.key==='Enter' && !e.shiftKey){ e.preventDefault(); send(); } };
    // One row that GROWS to a cap (messenger behaviour), and a send button that only lights up when
    // there's something to send. The old box was a fixed 2 rows with a resize handle, which ate the
    // thread on a phone and never fit a long message.
    { const ta=$('#dm-in'), sb=$('#dm-send');
      const grow=()=>{ ta.style.height='auto'; ta.style.height=Math.min(ta.scrollHeight, 132)+'px';
        // Read the attachment strip from the DOM — there is no module-level attachment array here
        // (wireImgAttach owns it), and referencing a name that doesn't exist would throw.
        const hasAtt = !!(document.querySelector('#dm-atts') || {}).children?.length;
        if(sb) sb.classList.toggle('on', !!ta.value.trim() || hasAtt); };
      // Keep the draft current on every keystroke, so a rebuild from ABOVE this function (renderMessages
      // replacing the whole #feed) still has it to seed the new box with.
      const keep=()=>{ if(ta.value) _dmDrafts.set(pk, ta.value); else _dmDrafts.delete(pk); };
      ta.addEventListener('input', ()=>{ grow(); keep(); });
      // after send() clears it, and on open
      ta.addEventListener('dm-reset', ()=>{ grow(); keep(); });
      grow(); }
    _wireBubbleActions($('#dm-msgs'), pk);
    }   // end of the fresh-render branch — its body is left at the original indentation on purpose, so
        // the diff that introduced the reuse path shows the two lines that changed and not the 60 that
        // moved sideways.
    // Both paths: the "Load older" button lives INSIDE #dm-msgs, so it is destroyed by either update
    // and has to be re-bound each time. The reply banner is idempotent (it assigns its handler).
    { const ob=$('#dm-older'); if(ob) ob.onclick=()=>{ _dmShown.set(pk, Math.min((_dmShown.get(pk)||_DM_INIT)+_DM_STEP, all.length)); S._dmScrollTop=true; renderDmThread(pk); }; }
    _dmReplyBanner();
    { const m=$('#dm-msgs'); if(m){ if(S._dmScrollTop){ S._dmScrollTop=false; m.scrollTop=0; } else if(_atBottom) _dmPinBottom(m); } }
    if(S._dmHandoffScroll){
      /* The generic DOM snapshot restores an absolute scrollTop once. A cold monitor decrypts and
       * grows bubbles after that, so a bottom-pinned conversation drifts upward and a person reading
       * history loses the same distance from the bottom. Restore the durable distance semantics
       * after the thread exists; the decrypt tail below keeps a pinned pane pinned as it grows. */
      const state=S._dmHandoffScroll;S._dmHandoffScroll=null;
      const apply=()=>{if(S.dmActive===pk)_restoreDmScroll($('#dm-msgs'),state);};
      apply();requestAnimationFrame(()=>requestAnimationFrame(apply));
    }
    _dmLastPk=pk;
    _dmThreadSig=_threadSig(pk);   // mark what we just rendered so a debounced refresh won't re-render it
    // Decrypt the visible slice lazily and patch each bubble in place. document.querySelector targets the
    // LIVE pane, so if a background renderMessages() rebuilt the DOM mid-decrypt we still patch the
    // element that's actually on screen (not a detached one). Bail if the user navigated away.
    let _patched=false;
    for(const mm of msgs){ if(mm.text==null){ await decryptMsg(pk, mm); if(S.dmActive!==pk) return;
      const el=document.querySelector('#dm-msgs .bubble[data-mid="'+mm.id+'"]');
      if(el){
        // Patch ONLY the text span. Replacing the bubble's whole innerHTML (what this used to do)
        // destroyed the .b-txt/.b-meta structure the moment a message decrypted — which is every
        // message, since DMs decrypt lazily — so timestamps vanished and patched bubbles laid out
        // differently from unpatched ones.
        const _t = el.querySelector('.b-txt');
        if(_t) _t.innerHTML = _dmBodyHtml(mm);
        else el.innerHTML = _dmBodyHtml(mm);
        decorateEncAtts(_t || el);   // 🔒 placeholders only exist once the text is in — the pane's own pass ran before this
        _decorateDmFileAtts(_t || el);
        _patched=true;
      } } }
    // Bubbles grew from placeholders to full text — if we were pinned to the bottom, stay pinned.
    // `_atBottom` was measured before the FIRST message was decrypted, and the loop above awaits an
    // ECDH+AES round per message — on a long thread that is seconds. Trusting it here yanked anyone
    // who read history in the meantime back down to the newest message. Ask the pane where it is NOW.
    if(_patched && _atBottom && !_wantTop){
      const m=$('#dm-msgs');
      if(m && m.scrollHeight - m.scrollTop - m.clientHeight < 80) _dmPinBottom(m);
    }
  }

  return {
    _scheduleDmRefresh, renderDmThread,
  };
};
