/* Profiles — the profile screen (header, tabs, notes/replies/media/articles/streams lists, the
 * cache-first paint and its refresh), the profile ⋯ menu, reports, admin permission toggles, Edit
 * profile, and the follows/followers lists. Split out of app.js.
 *
 * Loaded on first use: app.js keeps a small block of entry points (search for `_profileDeps`) and
 * builds this factory the first time a profile is opened. The code below is BYTE-IDENTICAL to what
 * it replaced in app.js apart from its reads of app.js's live `let` bindings, which the parser
 * rewrote to `S.<name>` (getters/setters on `dep.state`) at exact identifier offsets.
 */
window.PCProfileFactory = function(dep){
  const S = dep.state;   // live app.js bindings: S.FOLLOWS, S.IS_ADMIN, S.LOGO, S.ME, S.VIEW, S._navPushed, S._routing
  const {
    $, $$, FOLLOWERS, FilesIdx, NT, STREAM_RELAYS, _FEED_MAX_CARDS, _bindPaymentTargetEditor,
    _dedupAddr, _feedScrollable, _hidePill, _isDeletedStream, _kind0Tags, _loadPaymentTargets,
    _navTopHtml, _navUrl, _protectedProfileFollows, _rememberTlScroll, _shaFromUrl, _startTimeline,
    _syncRightbar, articleCard, bchDirect, bchOf, cleanupInlineStream, clearSentinel, closeModal,
    copyValue, decorateProfiles, decorateVerified, doBchTip, doBlock, doXmrTip, doZap, emojiName,
    enc, ensureMyFollowers, feedNoteHtml, followMany, hasMedia, hydrate, invalidateCounts,
    isBchAddr, isMutedAuthor, isReply, isXmrAddr, linkify, loadSentinel, mediaParts, modal,
    needProfile, niceNip05, noteHtml, openDMWith, openMenuPopover, openStream, profOf, publish,
    renderMe, renderView, showPaymentTargets, sign, startCall, streamCard, streamHost, switchView,
    timeAgo, toast, toggleFollow, toggleMute, uiConfirm, uploadBlob, xmrOf,
  } = dep;


  // ---------- profile ----------
  let _prof = { pk:null, tab:'notes', oldest:0, loading:false, done:false, limit:40, fill:null };
  let _profGen = 0;   // bumped per renderProfileView; async steps bail if superseded (opening B while A loads)
  // scroll-back for the active profile tab — pull older author notes, then re-fill the tab list
  // (notes/replies/media all derive from the author's kind-1 stream, so one fetch grows all three).
  async function loadOlderProfile(){
    if(_prof.loading || _prof.done || !_prof.pk || !_prof.oldest) return;
    _prof.loading=true; const pk=_prof.pk; const feed=$('#feed'); loadSentinel(feed);
    const until=_prof.oldest;
    let evs=[]; try{ evs=await Relay.query([{ authors:[pk], kinds:[1], until:until-1, limit:60 }]); }catch(_){}
    clearSentinel(feed);
    if(S.VIEW!=='profile' || _prof.pk!==pk){ _prof.loading=false; return; }
    let minTs=until;
    for(const e of evs){ Store.saveEvent(e); needProfile(e.pubkey); if(e.created_at<minTs) minTs=e.created_at; }
    invalidateCounts();
    // Profile scroll-back REDRAWS from the Store at a growing limit rather than appending, so the cap has
    // to be the limit itself — _capFeedDom can't help a view that rebuilds every card on the next page.
    // Same ceiling, same reason (see _capFeedDom). Stopping at the cap rather than merely clamping it is
    // the point: a clamped limit would keep querying the relay on every scroll to the bottom and redraw
    // the same 400 posts forever — a dead scroll that still burns the radio.
    _prof.limit += 60;
    if(_prof.limit >= _FEED_MAX_CARDS){ _prof.limit = _FEED_MAX_CARDS; _prof.done = true; }
    if(minTs<_prof.oldest) _prof.oldest=minTs;
    if(!evs.length || minTs>=until) _prof.done=true;
    if(_prof.fill){ _prof.fill(_prof.tab); hydrate(feed); }
    _prof.loading=false;
  }
  function renderProfile(pk){ renderProfileView(pk); }
  /* Ditto interoperates through kind-0 `fields`: [[label,url], ...]. Music is not a private Ditto
   * event kind; it is an ordinary labelled profile link whose target is audio. Keep every unknown
   * field intact and only claim the entries we can identify as playable audio. */
  function _profileMusicFields(p){
    const audio=/\.(?:mp3|mpga|m4a|aac|ogg|oga|opus|wav|flac)(?:[?#].*)?$/i;
    return (p&&Array.isArray(p.fields)?p.fields:[]).filter(x=>Array.isArray(x)&&x.length>1
      && /^https?:\/\//i.test(String(x[1]||'')) && (audio.test(String(x[1])) || /^🎶/.test(String(x[0]||''))));
  }
  /* PROFILE MUSIC LOOKS LIKE THE PLAYER, NOT LIKE A FORM CONTROL.
   *
   * It was a bare `<audio controls>` in a grey box — the browser's widget, on a page that has its
   * own visual language, next to a player (`.mp-*`) built in it. The equaliser is the cheap half of
   * that language: twelve bars on the same cyan→magenta ramp, animating ONLY while the track plays,
   * so a profile at rest is quiet and one that is playing is obviously playing from across the room.
   *
   * Deliberately CSS animation and not an AnalyserNode: a real spectrum needs the audio graph, and
   * a cross-origin track without CORS taints it and analyses to flat silence — the visualiser would
   * die on exactly the tracks people link. This one cannot fail that way, costs nothing to decode,
   * and stops dead when the audio is paused. `controls` stays: it is the accessible, keyboard-
   * reachable transport, and reinventing it here would be a second player to keep in step. */
  function _profileMusicHtml(p){
    const rows=_profileMusicFields(p);
    if(!rows.length) return '';
    /* The bar count is local on purpose. As a module constant it was outside the function, and the
     * tests that LIFT this renderer out of app.js and run it under node got a ReferenceError and no
     * markup at all — the same shape as the `_fromZapstore` call left behind by a deletion, which
     * cost this evening two separate bug reports. A renderer that only works with the rest of the
     * file loaded is a renderer that cannot be tested in isolation. */
    const bars = 12;
    const eq = `<div class="prof-eq" aria-hidden="true">${
      Array.from({length:bars}, (_,i)=>`<i style="--i:${i}"></i>`).join('')}</div>`;
    return `<div class="prof-music" aria-label="Profile music">${rows.map(([label,url])=>
      `<div class="prof-track"><div class="prof-track-head"><span class="prof-track-mark" aria-hidden="true">♪</span><div class="prof-track-name">${enc(String(label||'Track').replace(/^🎶\s*/,''))}</div></div>${eq}<audio controls preload="none" src="${enc(url)}"></audio></div>`
    ).join('')}</div>`;
  }
  /* The equaliser only runs while the audio does, so the class is driven by the element's own
   * events rather than by a guess. Bound wherever the block is (re)inserted — the first paint and
   * the background kind-0 refresh both replace this HTML wholesale, and a listener on a node that
   * has been thrown away is a listener on nothing. */
  function _bindProfileMusic(root){
    if(!root) return;
    root.querySelectorAll('.prof-track audio').forEach(a=>{
      const card=a.closest('.prof-track'); if(!card || a._pcEq) return;
      a._pcEq=1;
      const on=()=>card.classList.add('playing'), off=()=>card.classList.remove('playing');
      a.addEventListener('play',on); a.addEventListener('playing',on);
      a.addEventListener('pause',off); a.addEventListener('ended',off);
      /* A stalled or failed track must not leave the bars dancing over silence. */
      a.addEventListener('error',off); a.addEventListener('waiting',off);
    });
  }
  // Patch the already-painted profile header in place when a background kind-0 refresh changed it
  // (live rename / new avatar), so we never have to block the first paint on that refetch.
  /* Add (and bind) the tip controls once a profile turns out to carry payment addresses. Only ever
     ADDS — an address that has gone away leaves a stale button, which is a far smaller harm than
     tearing controls out from under a tap, and the next full render drops it anyway. */
  function _patchProfileTips(feed, pk, p, lightning){
    try{
      const pbody = feed.querySelector('.prof .pbody'); if(!pbody) return;
      const acts  = feed.querySelector('.prof .pactions') || feed.querySelector('#prof-menu') && feed.querySelector('#prof-menu').parentElement;
      const after = feed.querySelector('.prof .npubrow');
      const put = (el) => { if(after && after.parentElement) after.insertAdjacentElement('afterend', el); else pbody.appendChild(el); };
      const mk = (tag, cls, id, title, html) => { const e=document.createElement(tag); e.className=cls; e.id=id; e.title=title; e.innerHTML=html; return e; };

      const xmr = xmrOf(p);
      if(isXmrAddr(xmr) && !feed.querySelector('#prof-xmr')){
        const b = mk('button','ln-addr xmr','prof-xmr','tip Monero (XMR)',
          'ɱ ' + enc(xmr.slice(0,10)) + '…' + enc(xmr.slice(-6)));
        b.onclick = () => doXmrTip(null, pk); put(b);
      }
      if(isXmrAddr(xmr) && acts && !feed.querySelector('#xmrtip-prof') && !feed.querySelector('#prof-follow-self')){
        const t = mk('button','btn btn-ghost small','xmrtip-prof','tip Monero (XMR)','ɱ Tip');
        t.onclick = () => doXmrTip(null, pk);
        const menu = feed.querySelector('#prof-menu');
        if(menu) menu.insertAdjacentElement('beforebegin', t); else acts.appendChild(t);
      }
      const bch = bchOf(p);
      if(isBchAddr(bch) && !feed.querySelector('#prof-bch')){
        const b = mk('button','ln-addr bch','prof-bch','tip Bitcoin Cash (BCH)',
          '<svg class="ic b-ic" aria-hidden="true"><use href="#i-coin"></use></svg>'
          + enc(bch.slice(0,14)) + '…' + enc(bch.slice(-6)));
        b.onclick = () => doBchTip(pk); put(b);
      }
      const ln=lightning || p.lud16 || p.lud06;
      if(ln && !feed.querySelector('#prof-ln')){
        const b = mk('button','ln-addr','prof-ln','send a zap',
          '<svg class="ic b-ic" aria-hidden="true"><use href="#i-zap"></use></svg>' + enc(ln));
        b.onclick = () => doZap(null, pk); put(b);
      }
    }catch(_){ /* a tip button must never cost the header refresh */ }
  }

  function _patchProfileHeader(pk){
    const feed=$('#feed'); if(!feed) return; const p=Store.profile(pk)||{};
    const av=feed.querySelector('.pav'); if(av){ const s=p.picture||S.LOGO; if(av.getAttribute('src')!==s) av.src=s; }
    const bn=feed.querySelector('.prof .banner'); if(bn){ const want=p.banner?`<img src="${enc(p.banner)}" onerror="this.remove()">`:''; if(bn.innerHTML!==want) bn.innerHTML=want; }
    const h2=feed.querySelector('.prof .pbody h2'); if(h2){ const vchk=h2.querySelector('.vchk'); h2.innerHTML=emojiName(pk,p.name||p.display_name||'anon'); if(vchk) h2.appendChild(vchk); }
    const ab=feed.querySelector('.prof .about'); if(ab) ab.innerHTML=linkify(p.about||'');
    /* THE TIP AFFORDANCES ARE PROFILE FACTS TOO, AND THIS PATCH DID NOT TOUCH THEM.
       Reported against a real profile: "he added a payment target for xmr but no way to zap him".
       His kind-0 carries `monero_address`, `xmr` AND `cryptocurrency_addresses.monero`, and the
       address validates on the client and on the server — nothing was wrong with the data. The
       header is rendered ONCE from whatever `Store.profile(pk)` held at paint time, which on a
       cache-first or cold open is a profile with no addresses, and this function refreshed the
       avatar, banner, name, about and music but never the tip row. So the buttons could not appear
       for the rest of the visit. Exactly the feed card's bug (`_tipMarks`) on the profile screen.
       Added and BOUND here, because a button that appears and does nothing is the worse failure. */
    _patchProfileTips(feed, pk, p);
    const music=feed.querySelector('#prof-music');
    if(music){ music.innerHTML=_profileMusicHtml(p); _bindProfileMusic(music); }
  }

  /* A profile page normally loads only the newest handful of notes, so its local cache cannot tell
   * when an account first appeared. Search the relay's HISTORY instead: "has any event at or before
   * T" is monotonic, which lets us locate the first hour with ~16 tiny queries rather than download
   * somebody's entire publishing history. This only finds activity in the queried relays' retained
   * history, never an account creation date. Results are public and cached per pubkey for 30 days;
   * an incomplete/timed-out relay answer produces NO date. */
  async function _nostrFirstSeen(pk){
    const key='pc_first_seen_v2_'+pk, now=Math.floor(Date.now()/1000), ttl=30*86400;
    try{ const c=JSON.parse(localStorage.getItem(key)||'null');
      if(c && c.ts>0 && now-c.checked<ttl) return c.ts;
    }catch(_){}
    const hasBefore=async until=>{
      const r=await Relay.query([{authors:[pk],until,limit:1}],4500);
      if(!r.length && r.complete===false) throw new Error('incomplete historical lookup');
      return r.length>0;
    };
    try{
      if(!await hasBefore(now)) return 0;
      // Nostr was introduced in 2019. Starting there also catches deliberately backdated events in
      // the final result without making the binary-search interval needlessly enormous.
      let lo=1546300800-1, hi=now;
      while(hi-lo>3600){ const mid=Math.floor((lo+hi)/2);
        if(await hasBefore(mid)) hi=mid; else lo=mid;
      }
      const r=await Relay.query([{authors:[pk],until:hi,limit:500}],6000);
      if(!r.length || r.complete===false) return 0;
      const ts=r.reduce((n,e)=>e&&e.created_at&&(!n||e.created_at<n)?e.created_at:n,0);
      if(ts>0) try{ localStorage.setItem(key,JSON.stringify({ts,checked:now})); }catch(_){}
      return ts;
    }catch(_){ return 0; }
  }
  const _PROFILE_TOP = _navTopHtml('prof-back', 'Back to previous screen');
  function _bindProfileBack(feed, pk){
    const back=$('#prof-back',feed); if(!back) return;
    back.onclick=()=>{
      /* In the shell a profile may be a document frame over an untouched Social window; close that
       * frame. In a real Social toplevel it is ordinary in-window history, so pop the profile and
       * restore the exact timeline entry/scroll beneath it. A cold profile link has no app history
       * and returns to the configured starting timeline instead of navigating out of PosterChan. */
      try{ if(window.PCOS && PCOS.isOn() && PCOS.closeDoc
              && PCOS.closeDoc('prof:' + pk)) return; }catch(_){}
      if(S._navPushed>0){ try{ history.back(); return; }catch(_){} }
      switchView(_startTimeline());
    };
  }
  async function renderProfileView(pk){
    _rememberTlScroll();          // opening a profile is leaving the feed — see _rememberTlScroll
    // PosterChan OS: a profile opens in its OWN window, for the same reason a post does — opening
    // one from the timeline used to REPLACE the timeline, and with the sidebar hidden there was
    // then no way back to it. _routing is the back/forward button; see openThread.
    if(window.PCOS && PCOS.isOn() && pk && !renderProfileView._osIn){
      if(S._routing){
        try{ PCOS.focusDoc && PCOS.focusDoc('prof:' + pk); }catch(_){}
      }else{
        renderProfileView._osIn = 1;
        try{
          const mine = !!(S.ME && S.ME.pubkey === pk);
          if(PCOS.openDoc('prof:' + pk, mine ? 'My profile' : 'Profile', 'i-user',
                          () => renderProfileView(pk))) return;
        }finally{ renderProfileView._osIn = 0; }
      }
    }
    cleanupInlineStream();   // e.g. tapping the host's name from a stream
    _hidePill();
    try{ _navUrl('/'+NT().nip19.npubEncode(pk)); }catch(_){}   // shareable URL: poster.place/<npub>
    if(S.VIEW!=='profile'){ S.VIEW='profile'; $$('.nav-item[data-view]').forEach(b=>b.classList.toggle('active',b.dataset.view==='profile')); $('#view-title').textContent='Profile'; _syncRightbar(); }
    const myGen = ++_profGen;   // this render's token — every async step below bails if a newer profile opened
    const feed=$('#feed');
    // The profile is a NORMAL scrolling view, but the chat/DM/AI/translate views set an overflow:hidden
    // modifier class on #feed (full-height inner-scroll layout). Those are only toggled in the timeline
    // render path, so opening a profile straight from AI chat inherited feed-ai → the page couldn't
    // scroll ("stuck"). Clear them here so #feed scrolls again.
    _feedScrollable(feed);
    /* THE HEADER GOES UP FROM CACHE, and only the network waits are conditional on not having one.
     *
     * Everything that used to sit between the spinner and the first paint is a round trip: the socket
     * connect, a kind-0 refetch, and a notes query that RETRIES twice with backoff when it comes back
     * empty. On a phone that is seconds of blank screen for a profile the client can already draw —
     * your own, anyone you have read today, anyone whose post you just tapped through from.
     *
     * The cold path below is unchanged, deliberately. With nothing cached, painting early would mean
     * a header reading "anon" with no avatar for a second and then rewriting itself, which is worse
     * than a spinner — an empty answer must not be dressed up as an answer. So the split is on
     * whether there is anything real to show, not on a timeout. */
    const _cached = !!(Store.profile(pk) || Store.feed(e=>e.pubkey===pk).length);
    /* One notes fetch, used by both paths. Retries an EMPTY result: over a high-latency link
     * (Thailand→US) the first REQ can EOSE empty before the relay serves this author's notes → the
     * profile showed "0 posts" for an active account. Only when we got nothing AND have nothing
     * cached, so a genuinely-empty profile still resolves fast. */
    const _loadNotes = async () => {
      let notes=[];
      for(let attempt=0; attempt<3; attempt++){
        try{ notes=await Relay.query([{authors:[pk],kinds:[1,1068,6],limit:80}]); }catch(_){ notes=[]; }   // polls + reposts
        if(S.VIEW!=='profile' || myGen!==_profGen) return false;   // navigated away / a newer profile opened
        if(notes.length || Store.feed(e=>e.pubkey===pk).length) break;
        await new Promise(r=>setTimeout(r, 450*(attempt+1)));
      }
      notes.forEach(n=>Store.saveEvent(n));
      return S.VIEW==='profile' && myGen===_profGen;
    };
    if(!_cached){
      feed.innerHTML=_PROFILE_TOP+'<div class="spinner"></div>'; _bindProfileBack(feed,pk);
      // Opening a profile COLD — a pasted poster.place/<npub> link, a mention tap, a fresh launch — fired both
      // reads below at a still-CONNECTING socket, which silently drops them (relay.js `_send`): the header
      // rendered as "anon" and the notes retry loop below burned all 3 attempts against a dead socket.
      try{ await Relay.ready(); }catch(_){}
      if(myGen!==_profGen) return;   // a newer profile opened while we waited for the socket
      { const e=await Relay.query([{authors:[pk],kinds:[0],limit:1}]); for(const x of e)Store.saveProfile(x); }   // always refetch newest kind-0 so a renamed / re-avatar'd profile updates live (not just first view)
      if(myGen!==_profGen) return;   // a newer profile opened during the kind-0 fetch
      // Only the author's recent notes block the first paint. following/followers/pinned are loaded
      // in the BACKGROUND below — the followers query alone can pull up to 1000 kind-3 events, which
      // was the multi-second stall on every profile open.
      if(!await _loadNotes()) return;
    }
    const p=Store.profile(pk)||{}; const mine=pk===S.ME.pubkey;
    /* Nostr has no registration event. The date is filled asynchronously from a historical relay
     * search below; never derive it from this page's recent-note cache. */
    const npub=NT().nip19.npubEncode(pk);
    feed.innerHTML=_PROFILE_TOP+`<div class="prof"><div class="banner">${p.banner?`<img src="${enc(p.banner)}" onerror="this.remove()">`:''}</div>
      <div class="phead"><img class="pav" src="${enc(p.picture||S.LOGO)}" onerror="this.src='${S.LOGO}'">
        <div class="prof-actions"><button class="btn btn-ghost small" id="prof-pay">Pay</button>${mine?`<button class="btn btn-cyan small" id="edit-prof">Edit</button><button class="btn btn-ghost small" id="open-settings"><span class="lbl">⚙ Settings</span><span class="ic">⚙</span></button><button class="btn btn-ghost small prof-menu-btn" id="prof-menu" title="more"><svg class="ic b-ic" aria-hidden="true"><use href="#i-menu"></use></svg></button>`:`
          <button class="btn btn-ghost small" id="call-prof" title="voice/video call"><svg class="ic b-ic" aria-hidden="true"><use href="#i-phone"></use></svg>Call</button>
          <button class="btn btn-ghost small" id="zap-prof"><svg class="ic b-ic" aria-hidden="true"><use href="#i-zap"></use></svg>Zap</button>
          ${isXmrAddr(xmrOf(p))?`<button class="btn btn-ghost small" id="xmrtip-prof" title="tip Monero (XMR)">ɱ Tip</button>`:''}
          ${isBchAddr(bchOf(p))?`<button class="btn btn-ghost small" id="bchtip-prof" title="tip Bitcoin Cash (BCH)"><svg class="ic b-ic" aria-hidden="true"><use href="#i-coin"></use></svg>Tip</button>`:''}
          <button class="btn btn-ghost small prof-menu-btn" id="prof-menu" title="more"><svg class="ic b-ic" aria-hidden="true"><use href="#i-menu"></use></svg></button>`}</div></div>
      <div class="pbody"><h2>${emojiName(pk,p.name||p.display_name||'anon')}<span class="vchk" id="prof-vchk"></span></h2>
        ${niceNip05(p.nip05)?`<div class="muted small">${enc(niceNip05(p.nip05))}</div>`:''}
        <div class="npubrow"><code>${enc(npub.slice(0,24))}…</code><button class="mini icon-btn" id="copy-npub" title="Copy npub"><svg viewBox="0 0 16 16" width="15" height="15" fill="currentColor"><path d="M0 0h6v6H0zM2 2v2h2V2zM10 0h6v6h-6zM12 2v2h2V2zM0 10h6v6H0zM2 12v2h2v-2zM9 9h2v2H9zM13 9h3v2h-3zM9 13h2v3H9zM12 12h4v4h-2v-2h-2z"/></svg></button></div>
        ${p.lud16?`<button class="ln-addr" id="prof-ln" title="send a zap"><svg class="ic b-ic" aria-hidden="true"><use href="#i-zap"></use></svg>${enc(p.lud16)}</button>`:''}
        ${isXmrAddr(xmrOf(p))?`<button class="ln-addr xmr" id="prof-xmr" title="tip Monero (XMR)">ɱ ${enc(xmrOf(p).slice(0,10))}…${enc(xmrOf(p).slice(-6))}</button>`:''}
        ${isBchAddr(bchOf(p))?`<button class="ln-addr bch" id="prof-bch" title="tip Bitcoin Cash (BCH)"><svg class="ic b-ic" aria-hidden="true"><use href="#i-coin"></use></svg>${enc(bchOf(p).slice(0,14))}…${enc(bchOf(p).slice(-6))}</button>`:''}
        <div class="prof-joined" id="prof-joined" hidden title="Based on available relay history. Your actual join date may be earlier. This date is calculated automatically and cannot be changed in profile settings."><svg class="ic" aria-hidden="true"><use href="#i-clock"></use></svg><span>Earliest activity found</span><b></b></div>
        <div class="about">${linkify(p.about||'')}</div>
        <div id="prof-music">${_profileMusicHtml(p)}</div>
        <div class="follow-stats"><button class="statbtn" id="show-posts"><b>·</b> Posts</button><button class="statbtn" id="show-following"><b>·</b> Following</button><button class="statbtn" id="show-followers"><b>·</b> Followers</button></div>
      </div></div>
      <div class="prof-tabs"><button class="prof-tab active" data-tab="notes">Notes</button><button class="prof-tab" data-tab="replies">Replies</button><button class="prof-tab" data-tab="media">Media</button><button class="prof-tab" data-tab="articles">Articles</button><button class="prof-tab" data-tab="streams">Streams</button></div>
      <div id="prof-list"></div>`;
    _bindProfileBack(feed,pk);
    _nostrFirstSeen(pk).then(ts=>{
      if(!ts || S.VIEW!=='profile' || myGen!==_profGen || _prof.pk!==pk) return;
      const el=$('#prof-joined'); if(!el) return;
      const b=el.querySelector('b');
      if(b) b.textContent=new Date(ts*1000).toLocaleDateString(undefined,{year:'numeric',month:'short',day:'numeric'});
      el.hidden=false;
    });
    let pinnedHtml = '';   // filled by the deferred pinned query below; listFor() reads it live
    // Ids shown in the Pinned section, so the timeline below can leave them out. Without this every
    // pinned note renders TWICE on the profile — once pinned, once in its chronological place — which
    // reads as duplicate posts rather than as a highlight.
    let pinnedIds = new Set();
    const listFor=(tab)=>{
      const lim=_prof.limit;
      if(tab==='replies'){ const r=Store.query([{authors:[pk],kinds:[1,1111]}]).filter(isReply).slice(0,lim);
        return r.length ? r.map(feedNoteHtml).join('') : '<div class="empty">No replies yet.</div>'; }   // feedNoteHtml wraps a reply in the same reply-pair+context markup
      if(tab==='media'){ const m=Store.feed(e=>e.pubkey===pk && hasMedia(e)).slice(0,lim);
        if(!m.length) return '<div class="empty">No media yet.</div>';
        // gallery only — take each post's bare media tags (not the row/carousel wrapper) and grid them
        const items=m.map(e=>mediaParts(e.content).items.join('')).join('');
        return `<div class="media-grid">${items}</div>`; }
      if(tab==='articles'){ const a=_dedupAddr(Store.feed(e=>e.pubkey===pk && e.kind===30023)).slice(0,lim);
        return a.length ? a.map(articleCard).join('') : `<div class="empty">${_prof.artLoaded?'No articles yet.':'Loading…'}</div>`; }
      if(tab==='streams'){ const s=_dedupAddr(Store.byKind(30311).filter(e=> (e.pubkey===pk || streamHost(e)===pk) && !_isDeletedStream(e))).slice(0,lim);   // NOT Store.feed() — that allowlists kinds 1/6/1068/30023/40, so it silently drops every 30311
        return s.length ? `<div class="stream-grid prof-streams">${s.map(streamCard).join('')}</div>` : `<div class="empty">${_prof.streamsLoaded?'No streams yet.':'Loading…'}</div>`; }
      const n=Store.feed(e=>e.pubkey===pk && !isReply(e) && !pinnedIds.has(e.id)).slice(0,lim);
      if(n.length) return pinnedHtml + n.map(e=>noteHtml(e)).join('');
      /* "No posts yet." is a claim, and for a reply-heavy author it is a FALSE one — their replies
       * are on your timeline right now. Say which of the two this is, and offer the tab that has
       * them, rather than telling somebody an active account is empty. */
      const _reps = Store.query([{authors:[pk],kinds:[1,1111]}]).filter(isReply).length;
      return pinnedHtml + (_reps
        ? `<div class="empty">No top-level posts — everything loaded from this account is a reply.
             <button class="btn btn-cyan small prof-see-replies" style="margin-top:10px">See ${_reps} repl${_reps===1?'y':'ies'}</button></div>`
        : '<div class="empty">No posts yet.</div>');
    };
    // Guard against redundant re-renders: the lazy-fetch (+ hydrate/live-event churn) can call fillList
    // with byte-identical HTML, and re-setting innerHTML re-triggers the .stream-card fade → screen flicker.
    let _lastFill=null;
    /* THE LIST IS ALLOWED TO FAIL; THE PAGE IS NOT. Building it walks whatever this author has
     * published, and the card renderers are now individually guarded — but a tab is more than its
     * cards (a media grid, a stream grid, the pinned section), and this call sits BEFORE every
     * binding on the profile. An exception here used to leave a profile with no posts, no working
     * ⋯ menu and a dead "Copy npub", which is three bug reports for one throw. */
    const fillList=(tab)=>{
      const el=$('#prof-list'); if(!el) return;
      let h;
      try{ h=listFor(tab); }
      catch(e){
        try{ console.error('[profile] could not build the', tab, 'list', e); }catch(_){}
        h=`<div class="empty">Couldn’t show this list — ${enc(String((e&&e.message)||e).slice(0,140))}</div>`;
      }
      if(h===_lastFill) return; _lastFill=h; el.innerHTML=h;
      /* The "See N replies" button the Posts tab offers a reply-only author. Bound HERE, inside
       * fillList, because the list is re-rendered on every relay round and a handler attached
       * anywhere else would be dropped by the next innerHTML. Drives the real tab so the button and
       * the tab row cannot disagree about which one is active. */
      const _sr = el.querySelector('.prof-see-replies');
      if(_sr) _sr.onclick = () => {
        const t = $$('.prof-tab', feed).find(x => x.dataset.tab === 'replies');
        if(t) t.click();
        else { _prof.tab = 'replies'; fillList('replies'); hydrate(feed); }
      };
    };
    // Wire the Streams tab's cards to open the stream/VOD (author-name clicks still go to the profile).
    const _wireProfStreamClicks=()=>{ const el=$('#prof-list'); if(!el) return;
      el.querySelectorAll('.stream-card').forEach(c=>{ c.style.cursor='pointer';
        c.onclick=(ev)=>{ if(ev.target.closest('[data-prof]')) return; const s=Store.get(c.dataset.id); if(s) openStream(s); }; }); };
    // pagination cursor: oldest author kind-1 we hold (drives loadOlderProfile via `until`)
    const authorNotes=Store.feed(e=>e.pubkey===pk);
    _prof = { pk, tab:'notes', loading:false, done:false, limit:40, fill:fillList, following:[], followers:[],
              oldest: authorNotes.length ? authorNotes[authorNotes.length-1].created_at : 0 };
    fillList('notes');
    /* EVERYTHING FROM HERE TO THE BACKGROUND LOADS IS ONE STRAIGHT RUN OF BINDINGS, and a throw
     * anywhere in it silently truncates the page at that point: the header is already on screen and
     * looks right, while the tabs, ⋯, "Copy npub" and the follow stats below it were simply never
     * wired. That is not one bug report, it is three unrelated-sounding ones ("no posts", "the
     * hamburger menu isn't showing", "copying the npub does nothing"), none of which names a cause.
     *
     * So it says so instead. The block is deliberately NOT re-indented inside the try — the change
     * here is the guard, and a reformat would bury it in the diff.
     *
     * BUT A GUARD IS NOT INDEPENDENCE, and that distinction is why "copying the npub does nothing"
     * came back. One try around a straight run stops the exception escaping; it does NOT run the
     * statements after the throw. `hydrate` is by far the largest thing in here — every avatar,
     * every name, every verification badge on the page — so it is also the likeliest to throw, and
     * everything below it died with it while the message said only that something had. The small
     * bindings underneath do not depend on it or on each other: a Copy button, a tip button, a tab
     * row. Each one now stands on its own, so the page loses exactly what actually broke. */
    const _bind = (what, fn) => {
      try{ fn(); }
      catch(e){
        try{ console.error('[profile] ' + what + ' did not bind', e); }catch(_){}
        (_profBroke = _profBroke || []).push(what + ': ' + String((e && e.message) || e).slice(0, 90));
      }
    };
    let _profBroke = null;
    _bind('the avatars and names', () => hydrate(feed));
    /* Wrapped like every other binding here: an equaliser that fails to attach must cost the
     * equaliser, never the tabs, the follow stats or Copy npub below it — that is the whole reason
     * `_bind` exists on this screen. */
    _bind('the profile music equaliser', () => _bindProfileMusic(feed));
    _bind('the verified badge', () => decorateVerified($('#prof-vchk'), pk, p.nip05));
    /* COPY NPUB IS BOUND EARLY AND ON ITS OWN. It is one line, it can only fail if the element is
     * missing, and it is the single most reported casualty of everything above it. */
    _bind('Copy npub', () => { const cn=$('#copy-npub');
                               if(cn) cn.onclick=()=> copyValue(npub, 'npub copied', 'Their npub:'); });
    try{
    $$('.prof-tab',feed).forEach(t=> t.onclick=async()=>{ $$('.prof-tab',feed).forEach(x=>x.classList.toggle('active',x===t)); const tab=t.dataset.tab; _prof.tab=tab; fillList(tab); hydrate(feed);
      if(tab==='streams') _wireProfStreamClicks();
      // Articles (kind-30023) aren't part of the initial note load — lazy-fetch them once on first open.
      if(tab==='articles' && !_prof.artLoaded){ _prof.artLoaded=true;
        try{ const a=await Relay.query([{authors:[pk],kinds:[30023],limit:40}]); for(const e of (a||[])) Store.saveEvent(e); }catch(_){}
        if(S.VIEW==='profile' && _prof.pk===pk && _prof.tab==='articles'){ fillList('articles'); hydrate(feed); } }
      /* REPLIES GET THEIR OWN FETCH, because nothing else was ever going to find them.
       *
       * The profile loads ONE page — `kinds:[1,1068,6], limit:80` — and the Replies tab then
       * filters it. On an account that mostly posts, those 80 newest events are nearly all
       * top-level notes and reposts, so the tab showed whatever handful of replies happened to be
       * in them: reported as "I can only see 5 replies on My Profile". Nothing was lost and nothing
       * was broken — they were simply never asked for. Articles and streams have had their own
       * lazy query all along; this is the same idea for the one tab that did not.
       *
       * Nostr filters cannot express "has an e tag", so the only way is to page BACK by `until` and
       * filter here — exactly what the reply-heavy backfill below does for the opposite case. It
       * stops as soon as it has a screenful, so an account that replies constantly pays for one
       * page and a note-heavy one pays at most three. */
      if(tab==='replies' && !_prof.repliesLoaded){ _prof.repliesLoaded=true;
        const have=()=>Store.query([{authors:[pk],kinds:[1,1111]}]).filter(isReply).length;
        let oldest=Math.min.apply(null,(Store.feed(e=>e.pubkey===pk).map(e=>e.created_at||0)
                                        .filter(Boolean).concat([Math.floor(Date.now()/1000)])));
        for(let page=0; page<3 && have()<_prof.limit; page++){
          let older=[];
          try{ older=await Relay.query([{authors:[pk],kinds:[1,1111],until:oldest-1,limit:200}])||[]; }
          catch(_){ break; }
          if(!older.length) break;
          older.forEach(e=>Store.saveEvent(e));
          const stamps=older.map(e=>e.created_at||0).filter(Boolean);
          if(!stamps.length) break;
          oldest=Math.min.apply(null,stamps);
          if(S.VIEW!=='profile' || _prof.pk!==pk || _prof.tab!=='replies') return;   // they moved on
        }
        if(S.VIEW==='profile' && _prof.pk===pk && _prof.tab==='replies'){ fillList('replies'); hydrate(feed); } }
      // Streams (kind-30311) live + ended — lazy-fetch once (our relay + the wider stream network).
      if(tab==='streams' && !_prof.streamsLoaded){ _prof.streamsLoaded=true;
        try{ const s=await Relay.query([{authors:[pk],kinds:[30311],limit:60}]); for(const e of (s||[])) Store.saveEvent(e); }catch(_){}
        try{ let ext=await Relay.queryFrom(STREAM_RELAYS,[{authors:[pk],kinds:[30311],limit:60}],{purpose:'profile streams'});
          if(ext&&ext.length){ try{ const v=await Relay.worker.call('verifyBatch',{events:ext});
            const ok=new Set(v.filter(r=>r.valid).map(r=>r.id)); ext=ext.filter(e=>ok.has(e.id)); }catch(_){ ext=[]; }
            for(const e of ext) Store.saveEvent(e); } }catch(_){}
        if(S.VIEW==='profile' && _prof.pk===pk && _prof.tab==='streams'){ fillList('streams'); hydrate(feed); _wireProfStreamClicks(); } }
    });
    /* BACKFILL FOR A REPLY-HEAVY AUTHOR — AFTER the render, never before it.
     *
     * The Posts tab excludes replies (`!isReply`) while the timeline includes them, so an author
     * whose recent 80 events are nearly all replies gets "No posts yet." about an account whose
     * posts are filling your feed. Measured on one reported npub: 168 of its 200 most recent kind-1s
     * are replies, with 32 top-level ones further back. Nostr filters cannot express "no e tag", so
     * the only way to find them is to page BACK by `until`.
     *
     * THE FIRST CUT OF THIS PUT THE PAGING INSIDE _loadNotes, AND THAT WAS A REGRESSION: _loadNotes
     * gates the whole render (`if(!await _loadNotes()) return;`), so two extra relay round trips ran
     * BEFORE the list, the tabs, the ⋯ menu and Copy-npub were bound — delaying the profile and
     * widening the window in which a navigation abandons the render half-built. That is the exact
     * "no posts, dead hamburger, dead copy npub" triad it was supposed to help with.
     *
     * So it runs here instead: fire-and-forget, nothing awaits it, and it only repaints if it
     * actually found something and the user is still on this profile. Same shape as the articles and
     * streams lazy-loaders above. Bounded to two pages — a fill-in, not scroll-back. */
    (async () => {
      const top = () => Store.feed(e => e.pubkey === pk && !isReply(e)).length;
      if(top()) return;                                   // the common case: nothing to do
      let older = Store.feed(e => e.pubkey === pk);
      for(let page = 0; page < 2 && older.length; page++){
        const stamps = older.map(e => e.created_at || 0).filter(Boolean);
        if(!stamps.length) return;
        const oldest = Math.min.apply(null, stamps);
        try{ older = await Relay.query([{ authors:[pk], kinds:[1,1068,6], until: oldest - 1, limit: 80 }]) || []; }
        catch(_){ return; }
        if(!older.length) return;
        older.forEach(e => Store.saveEvent(e));
        if(S.VIEW!=='profile' || _prof.pk!==pk || myGen!==_profGen) return;   // they moved on; drop it
        if(top()){ if(_prof.tab === 'notes'){ fillList('notes'); hydrate(feed); } return; }
      }
    })();
    { const pay=$('#prof-pay',feed); if(pay)pay.onclick=()=>showPaymentTargets(pk); }
    _loadPaymentTargets(pk).then(lightning=>{
      if(S.VIEW==='profile' && _prof.pk===pk && myGen===_profGen)
        _patchProfileTips(feed,pk,Store.profile(pk)||{},lightning);
    }).catch(()=>{});
    { const ln=$('#prof-ln'); if(ln) ln.onclick=()=>doZap(null, pk); }
    { const xb=$('#prof-xmr'); if(xb) xb.onclick=()=>doXmrTip(null, pk); }
    { const xt=$('#xmrtip-prof'); if(xt) xt.onclick=()=>doXmrTip(null, pk); }
    { const bb=$('#prof-bch'); if(bb) bb.onclick=()=>doBchTip(pk); }
    { const bt=$('#bchtip-prof'); if(bt) bt.onclick=()=>doBchTip(pk); }
    // Posts has no list of its own — the Notes tab IS that list, so send them there rather than leaving a
    // dead-looking button next to two clickable stats.
    { const pb=$('#show-posts'); if(pb) pb.onclick=()=>{ const t=$$('.prof-tab',feed).find(x=>x.dataset.tab==='notes'); if(t) t.click(); }; }
    { const sf=$('#show-following'); if(sf) sf.onclick=()=>peopleModal('Following', _prof.following||[]); }
    { const sfw=$('#show-followers'); if(sfw) sfw.onclick=async()=>{   // lazy-load the follower LIST only when actually opened (count was already fetched via NIP-45)
      if(!_prof.followers || !_prof.followers.length){
        const fe=await Relay.query([{kinds:[3],'#p':[pk],limit:1000}]).catch(()=>[]);
        _prof.followers=[...new Set(fe.map(e=>e.pubkey))];
        // Correct the headline to the number of DISTINCT followers. The NIP-45 COUNT counts kind-3 *events*,
        // and kind-3 is replaceable — a follower who republished their follow list is counted several times
        // (that's the "133 count but 68 in the list" bug). The deduped list is the truth. Only fall back to
        // the reported COUNT when we actually hit the 1000 cap (a genuinely huge follower list we can't fully
        // pull), where the deduped length would UNDERcount.
        const fr=$('#show-followers b');
        // The deduped list is the true count (kind-3 is replaceable; the NIP-45 COUNT over-counts republished
        // lists — the 133-vs-68 bug). Show the distinct count. Never overwrite a real headline with 0 from an
        // empty/failed read. If we hit the 1000 cap the true total is higher, so show "1000+" — NOT the
        // inflated NIP-45 number (which is what `fr.textContent` still holds, so we must not max() with it).
        if(fr && _prof.followers.length){
          fr.textContent = (fe.length >= 1000) ? _prof.followers.length + '+' : String(_prof.followers.length);
        }
      }
      peopleModal('Followers', _prof.followers||[]);
    }; }
    if(mine){ const ep=$('#edit-prof'); if(ep) ep.onclick=()=>editProfile(p);
              const os=$('#open-settings'); if(os) os.onclick=()=>switchView('settings'); }
    else { const z=$('#zap-prof'); if(z)z.onclick=()=>doZap(null,pk);
      const cb=$('#call-prof'); if(cb)cb.onclick=()=>startCall(pk, {video:false}); }
    { const mn=$('#prof-menu'); if(mn)mn.onclick=()=>openProfileMenu(pk, mn); }   // ☰ on own + others' profiles
    }catch(e){
      try{ console.error('[profile] the page stopped binding partway', e); }catch(_){}
      (_profBroke = _profBroke || []).push(String((e&&e.message)||e).slice(0, 140));
    }
    if(_profBroke && _profBroke.length){
      const head=$('.pbody',feed);
      if(head) head.insertAdjacentHTML('beforeend',
        '<div class="muted small">⚠ part of this profile didn’t load — '
        + enc(_profBroke.join(' · ').slice(0, 220)) + '</div>');
    }
    /* …and when that header came out of the CACHE, go and check it — the same two reads the cold path
     * makes before painting, made after. A rename or a new avatar has to land without a reload, which
     * is exactly what _patchProfileHeader exists for, and fresh notes fill the list in place.
     *
     * `oldest` is re-taken afterwards because it is the scroll-back cursor: it was computed from what
     * the cache held, and paging from there would re-request notes the refresh has already brought
     * in. Every step re-checks the generation token — this is now a second async path through the
     * same view, and the one thing it must never do is patch a profile the user has left. */
    if(_cached) (async()=>{
      try{ await Relay.ready(); }catch(_){}
      if(S.VIEW!=='profile' || _prof.pk!==pk || myGen!==_profGen) return;
      try{ const e=await Relay.query([{authors:[pk],kinds:[0],limit:1}]); for(const x of e) Store.saveProfile(x); }catch(_){}
      if(S.VIEW!=='profile' || _prof.pk!==pk || myGen!==_profGen) return;
      _patchProfileHeader(pk);
      if(!await _loadNotes()) return;
      if(_prof.pk!==pk) return;
      const an=Store.feed(e=>e.pubkey===pk);
      if(an.length) _prof.oldest=an[an.length-1].created_at;
      fillList(_prof.tab); hydrate(feed);
      if(_prof.tab==='streams') _wireProfStreamClicks();
    })();
    // Background: following / followers / pinned — fetched in PARALLEL after the first paint and
    // patched in, so the profile opens instantly instead of waiting on (esp.) the 1000-event
    // followers query. Re-checks _prof.pk so a fast navigation away doesn't patch the wrong profile.
    (async()=>{
      // The follower COUNT and the contact-list (following) REQ race empty INDEPENDENTLY on a just-connected
      // socket, so a single fire can land followers but 0 following (or 0/0). Patch each stat the moment ITS
      // read arrives, and retry (up to 3x) until BOTH are in — a genuinely-empty field just exhausts the
      // retries and shows 0. This does NOT touch the notes/paint path above.
      let k3=[], followerCount=0, postCount=0, pinList=[];
      for(let attempt=0; attempt<3; attempt++){
        const [ak3, acount, apins, aposts] = await Promise.all([
          Relay.query([{authors:[pk],kinds:[3],limit:1}]).catch(()=>[]),
          Relay.count([{kinds:[3],'#p':[pk]}]).catch(()=>0),   // NIP-45 COUNT — don't pull 1000 contact-list blobs just to tally (the profile-open spike). The list is lazy-loaded on "Followers" click.
          Relay.query([{authors:[pk],kinds:[10001],limit:1}]).catch(()=>[]),
          // Post tally, also via COUNT — the Notes tab only ever holds a page of events, so counting what's
          // rendered would show "40" for everyone. kind 1 only: reposts/articles/streams have their own tabs.
          Relay.count([{authors:[pk],kinds:[1]}]).catch(()=>0),
        ]);
        if(S.VIEW!=='profile' || _prof.pk!==pk || myGen!==_profGen) return;
        if(acount>followerCount){ followerCount=acount; const fr=$('#show-followers b'); if(fr) fr.textContent=followerCount; }
        if(aposts>postCount){ postCount=aposts; const pr=$('#show-posts b'); if(pr) pr.textContent=postCount; }
        if(ak3.length){
          k3=ak3;
          const incoming=[...new Set(k3.sort((a,b)=>b.created_at-a.created_at)[0].tags
            .filter(t=>t[0]==='p'&&t[1]).map(t=>t[1]))];
          /* My profile must not bypass fetchFollows' wipe protection. It used to paint the raw
           * one-shot relay answer, so the taskbar/feed could correctly retain 1,163 follows while
           * the profile announced "Following 8". For our own identity, the already-protected live
           * set is authoritative whenever the response is a wipe-sized regression. Other people's
           * profiles still show their published contact list. */
          _prof.following=_protectedProfileFollows(pk,incoming);
          const ff=$('#show-following b'); if(ff) ff.textContent=_prof.following.length;
        }
        if(apins.length) pinList=apins;
        if(followerCount && k3.length) break;
        await new Promise(r=>setTimeout(r, 800*(attempt+1)));
      }
      // Write the FINAL values unconditionally — a genuinely-empty field (0 followers, or no contact list)
      // never triggered the in-loop patch, so without this it would keep the "·" placeholder instead of "0".
      { const fr=$('#show-followers b'); if(fr) fr.textContent=Number(followerCount)||0;
        const ff=$('#show-following b'); if(ff) ff.textContent=(_prof.following||[]).length;
        const pr=$('#show-posts b'); if(pr) pr.textContent=Number(postCount)||0; }
      const pinIds=pinList.length ? pinList.sort((a,b)=>b.created_at-a.created_at)[0].tags.filter(t=>t[0]==='e'&&t[1]).map(t=>t[1]) : [];
      if(pinIds.length){
        const got=await Relay.query([{ids:pinIds}]).catch(()=>[]); got.forEach(e=>Store.saveEvent(e));
        const pinned=pinIds.map(id=>Store.get(id)).filter(Boolean);
        if(pinned.length && S.VIEW==='profile' && _prof.pk===pk){
          pinnedIds = new Set(pinned.map(e=>e.id));   // set BEFORE fillList below, or the first paint duplicates
          pinnedHtml='<div class="search-section-title"><svg class="ic b-ic" aria-hidden="true"><use href="#i-pin"></use></svg>Pinned</div>'+pinned.map(e=>noteHtml(e)).join('');
          if(_prof.tab==='notes'){ fillList('notes'); hydrate(feed); }
        }
      }
    })();
  }
  // the profile "☰ more" menu — Follow / Message / Mute / Block, kept off the header for a clean look
  async function openProfileMenu(pk, anchorBtn){
    const mine = pk===S.ME.pubkey;
    const items = mine ? [['reports','🚩 Reports received']] : [
      ['follow', S.FOLLOWS.has(pk)?'➖ Unfollow':'➕ Follow'],
      ['message','✉️ Message'],
      ['mute', isMutedAuthor(pk)?'🔊 Unmute':'🔇 Mute'],
      ['reports','🚩 Reports received'],
    ];
    items.push(['relays','🖧 Relays']);   // view the relays this user publishes to (NIP-65)
    if(S.IS_ADMIN){
      // admin extras: one consolidated permissions panel (AI, Blossom, image/music/video/torrent)
      // + relay block. State is fetched inside openPermissions so the menu opens instantly.
      items.push(['caps','🔑 Permissions']);
      items.push(['relay-sync','🔄 Sync notes']);
      items.push(['purge-blossom','🗑️ Purge Blossom','danger']);
      items.push(['block','🚫 Block','danger']);
    }
    openMenuPopover(anchorBtn, items, async a=>{
      if(a==='follow'){ await toggleFollow(pk); renderProfileView(pk); return; }
      if(a==='message'){ openDMWith(pk); return; }
      if(a==='mute'){ await toggleMute(pk); renderProfileView(pk); return; }
      if(a==='reports') return showReports(pk);
      if(a==='relays') return showRelays(pk);
      if(a==='caps') return openPermissions(pk);
      if(a==='relay-sync') return doRelaySync(pk);
      if(a==='purge-blossom') return doPurgeBlossom(pk);
      if(a==='block') return doBlock(pk);
    });
  }
  // NIP-56 reports a user has RECEIVED (kind-1984 p-tagging them). Fetched from UPSTREAM relays via
  // /client/reports (the built-in relay only stores WoT-authored events, so reports about an arbitrary
  // user aren't local). Open to any user. Tap a report to see it in full (reason + reported post).
  async function showReports(pk){
    const who = (()=>{ const p=profOf(pk); return p.name||p.display_name||(NT().nip19.npubEncode(pk).slice(0,12)+'…'); })();
    modal(`<h3><svg class="ic h-ic" aria-hidden="true"><use href="#i-flag"></use></svg>Reports received — ${enc(who)}</h3><div id="rep-list" class="people-list"><div class="spinner"></div></div>`, async root=>{
      let reports=[];
      try{ const r=await fetch('/client/reports?pubkey='+encodeURIComponent(pk)).then(r=>r.json()); if(r&&r.ok) reports=r.reports||[]; }catch(_){}
      const h3=root.querySelector('h3'); if(h3) h3.textContent='🚩 Reports received — '+who+' ('+reports.length+')';
      const miss=[...new Set(reports.map(x=>x.reporter))].filter(a=>a&&!Store.haveProfile(a)).slice(0,200);
      if(miss.length){ try{ (await Relay.query([{authors:miss,kinds:[0],limit:miss.length}])).forEach(e=>Store.saveProfile(e)); }catch(_){} }
      const list=$('#rep-list',root); if(!list) return;
      list.innerHTML = reports.length ? reports.map((x,i)=>{
        const rp=Store.profile(x.reporter)||{};
        const rn=rp.name||rp.display_name||(NT().nip19.npubEncode(x.reporter).slice(0,12)+'…');
        const reason=(x.reason||'').trim();
        return `<div class="psearch rep-row" data-i="${i}"><img src="${enc(rp.picture||S.LOGO)}" onerror="this.src='${S.LOGO}'"><div class="pinfo"><b>${enc(rn)}</b><div class="muted small">🚩 ${enc(x.type||'other')}${reason?' · '+enc(reason.slice(0,120)):''} · ${timeAgo(x.created_at)}</div></div><span class="muted" style="align-self:center">›</span></div>`;
      }).join('') : '<div class="empty">No reports for this user. 🎉</div>';
      $$('.rep-row',list).forEach(el=> el.onclick=()=> showReportDetail(reports[+el.dataset.i]));
    });
  }
  // The full report: who filed it, the type, the full reason, and the reported post (fetched if we
  // can find it).
  async function showReportDetail(rep){
    if(!rep) return;
    const rp=Store.profile(rep.reporter)||{};
    const rn=rp.name||rp.display_name||(NT().nip19.npubEncode(rep.reporter).slice(0,16)+'…');
    const reason=(rep.reason||'').trim();
    modal(`<h3><svg class="ic h-ic" aria-hidden="true"><use href="#i-flag"></use></svg>Report</h3>
      <div class="report-detail">
        <div class="rd-row"><span class="muted small">Reported by</span> <b class="lnk" data-prof="${rep.reporter}">${enc(rn)}</b></div>
        <div class="rd-row"><span class="muted small">Type</span> <span class="rep-type">${enc(rep.type||'other')}</span> <span class="muted small">· ${timeAgo(rep.created_at)}</span></div>
        ${reason?`<div class="rd-reason">${linkify(reason)}</div>`:'<div class="muted small">No reason given.</div>'}
        ${rep.event?'<div id="rd-event"><div class="spinner"></div></div>':''}
      </div>`, async root=>{
      $$('[data-prof]',root).forEach(el=> el.onclick=()=>{ closeModal(); renderProfileView(el.dataset.prof); });
      if(rep.event){
        let ev=Store.get(rep.event);
        if(!ev){ try{ (await Relay.query([{ids:[rep.event]}])).forEach(e=>Store.saveEvent(e)); ev=Store.get(rep.event); }catch(_){} }
        const box=$('#rd-event',root); if(!box) return;
        box.innerHTML = ev ? ('<div class="muted small" style="margin:8px 0 4px">Reported post:</div>'+noteHtml(ev))
                           : `<div class="muted small">Reported post isn't available here (id ${enc(rep.event.slice(0,12))}…).</div>`;
        if(ev) decorateProfiles();
      }
    });
  }
  // admin: backfill this account's Nostr post history into the built-in relay (the "Sync a user's
  // data" action from Admin → Relay). Signed like doBlock so the server checks admin.
  async function doRelaySync(pk){
    if(!S.IS_ADMIN) return;
    try {
      const auth = await sign(27235, 'relay-sync', [['action','sync'],['p',pk]]);
      const r = await fetch('/client/relay-sync', { method:'POST', headers:{'Content-Type':'application/json'},
        body: JSON.stringify({ target: pk, auth: btoa(JSON.stringify(auth)) }) }).then(r=>r.json());
      toast(r.ok ? 'sync queued — notes backfilling 🔄' : ('sync failed: ' + (r.error||'')));
    } catch(e){ toast('sync failed'); }
  }
  // admin: delete ALL of this account's blobs from the built-in Blossom server (bytes + index rows).
  // Irreversible; signed like doBlock so the server checks admin.
  async function doPurgeBlossom(pk){
    if(!S.IS_ADMIN) return;
    if(!await uiConfirm('Purge ALL of this user\'s files from the Blossom server? This permanently deletes the stored bytes and cannot be undone.')) return;
    try {
      const auth = await sign(27235, 'blossom-purge', [['action','purge'],['p',pk]]);
      const r = await fetch('/client/blossom-purge', { method:'POST', headers:{'Content-Type':'application/json'},
        body: JSON.stringify({ target: pk, auth: btoa(JSON.stringify(auth)) }) }).then(r=>r.json());
      toast(r.ok ? ('purged '+(r.deleted||0)+' file(s) 🗑️') : ('purge failed: ' + (r.error||'')));
    } catch(e){ toast('purge failed'); }
  }
  // admin: per-user feature permissions (image/music/video/torrent) from the profile menu — replaces
  // the Admin → Users capability toggles.
  // admin: toggle a single per-user capability (e.g. can_torrent) inline from the profile menu.
  async function toggleCap(pk, cap, val){
    if(!S.IS_ADMIN) return;
    try{
      const auth = await sign(27235, 'user-caps', [['p',pk]]);
      const r = await fetch('/client/user-caps', { method:'POST', headers:{'Content-Type':'application/json'},
        body: JSON.stringify({ target: pk, caps:{[cap]:val}, auth: btoa(JSON.stringify(auth)) }) }).then(r=>r.json());
      toast(r.ok ? (val?'access granted':'access revoked') : ('failed: '+(r.error||'')));
    }catch(e){ toast('change failed'); }
  }
  // admin: one consolidated permissions panel for a user — AI access, Blossom uploads, and the
  // per-feature caps (image/music/video/torrent). Each maps to its own endpoint; on save we only
  // sign + call the ones that actually changed (fewer signer prompts).
  async function openPermissions(pk){
    if(!S.IS_ADMIN) return;
    let caps={}, aiOn=false, blossomOn=false, bridgeOn=false, streamOn=false;
    try{ const r=await fetch('/client/ai-access?pubkey='+encodeURIComponent(pk)).then(r=>r.json()); aiOn=!!(r&&r.enabled); }catch(_){}
    try{ const r=await fetch('/client/blossom-access?pubkey='+encodeURIComponent(pk)).then(r=>r.json()); blossomOn=!!(r&&r.whitelisted); }catch(_){}
    try{ const r=await fetch('/client/bridge-access?pubkey='+encodeURIComponent(pk)).then(r=>r.json()); bridgeOn=!!(r&&r.enabled); }catch(_){}
    try{ const r=await fetch('/client/stream-access?pubkey='+encodeURIComponent(pk)).then(r=>r.json()); streamOn=!!(r&&r.enabled); }catch(_){}
    try{ const r=await fetch('/client/user-caps?pubkey='+encodeURIComponent(pk)).then(r=>r.json()); if(r&&r.exists) caps=r.caps||{}; }catch(_){}
    let nipName='', nipDomain=location.host;
    try{ const r=await fetch('/client/admin-nip05?pubkey='+encodeURIComponent(pk)).then(r=>r.json()); if(r&&r.ok){ nipName=r.name||''; if(r.nip05) nipDomain=r.nip05.split('@')[1]||nipDomain; } }catch(_){}
    const _pp=profOf(pk)||{};
    const defNip=((_pp.name||_pp.display_name||'')).toLowerCase().replace(/[^a-z0-9_.\-]/g,'').replace(/^[._\-]+|[._\-]+$/g,'').slice(0,30);
    const C=[['can_image','🖼️ Image'],['can_music','🎵 Music'],['can_video','🎬 Video'],['can_torrent','🧲 Torrents']];
    const row=(id,checked,label)=>`<label class="fld" style="flex-direction:row;align-items:center;gap:8px"><input type="checkbox" ${id} ${checked?'checked':''}> ${label}</label>`;
    modal(`<h3><svg class="ic h-ic" aria-hidden="true"><use href="#i-key"></use></svg>Additional permissions</h3>
      ${row('id="perm-ai"', aiOn, '🤖 AI access')}
      ${row('id="perm-blossom"', blossomOn, '🌸 Blossom uploads')}
      ${row('data-cap="can_media"', !!caps.can_media, '📺 Media Center <span class="muted small">(browse and play shared libraries)</span>')}
      ${row('id="perm-stream"', streamOn, '🔴 Live streaming <span class="muted small">(Go Live)</span>')}
      <label class="fld" style="flex-direction:row;align-items:center;gap:8px"><input type="checkbox" id="perm-nip05" ${nipName?'checked':''}> 🪪 NIP-05 <span class="muted small">${enc((nipName||defNip||('user'+pk.slice(0,8)))+'@'+nipDomain)}</span></label>
      ${row('id="perm-bridge"', bridgeOn, '🌉 Bridge Access <span class="muted small">(create fedi account + enable bridge)</span>')}
      <hr style="border:none;border-top:1px solid var(--line,#333);margin:10px 0">
      <p class="muted small">AI features</p>
      ${C.map(([k,l])=>row('data-cap="'+k+'"', !!caps[k], l)).join('')}
      <button class="btn btn-neon full" id="caps-save">Save</button>`, root=>{
      $('#caps-save',root).onclick=async()=>{
        const wantAi=$('#perm-ai',root).checked, wantBl=$('#perm-blossom',root).checked;
        const wantStream=$('#perm-stream',root).checked;
        const out={}; let capsChanged=false;
        $$('[data-cap]',root).forEach(c=>{ out[c.dataset.cap]=c.checked; if(c.checked!==!!caps[c.dataset.cap]) capsChanged=true; });
        let ok=true;
        try{
          if(wantStream!==streamOn){
            const auth=await sign(27235,'stream-access',[['action',wantStream?'grant':'revoke'],['p',pk]]);
            const r=await fetch('/client/stream-access',{method:'POST',headers:{'Content-Type':'application/json'},
              body:JSON.stringify({target:pk,grant:wantStream,auth:btoa(JSON.stringify(auth))})}).then(r=>r.json());
            if(!(r&&r.ok)) ok=false;
          }
          if(wantAi!==aiOn){
            const auth=await sign(27235,'ai-access',[['action',wantAi?'grant':'revoke'],['p',pk]]);
            const r=await fetch('/client/ai-access',{method:'POST',headers:{'Content-Type':'application/json'},
              body:JSON.stringify({target:pk,grant:wantAi,auth:btoa(JSON.stringify(auth))})}).then(r=>r.json()); ok=ok&&r.ok;
          }
          if(wantBl!==blossomOn){
            const auth=await sign(27235,'blossom',[['action',wantBl?'grant':'revoke'],['p',pk]]);
            const r=await fetch('/client/blossom-access',{method:'POST',headers:{'Content-Type':'application/json'},
              body:JSON.stringify({target:pk,grant:wantBl,auth:btoa(JSON.stringify(auth))})}).then(r=>r.json()); ok=ok&&r.ok;
          }
          // NIP-05: simple toggle — grant their-own-name@domain when checked, remove when unchecked.
          const wantNip=$('#perm-nip05',root).checked;
          if(wantNip && !nipName){
            const auth=await sign(27235,'nip05',[['action','grant'],['p',pk]]);
            const r=await fetch('/client/admin-nip05',{method:'POST',headers:{'Content-Type':'application/json'},
              body:JSON.stringify({target:pk, name:(defNip||('user'+pk.slice(0,8))), auth:btoa(JSON.stringify(auth))})}).then(r=>r.json());
            ok=ok&&r.ok; if(r&&!r.ok&&r.error) toast(r.error);
          } else if(!wantNip && nipName){
            const auth=await sign(27235,'nip05',[['action','revoke'],['p',pk]]);
            const r=await fetch('/client/admin-nip05',{method:'POST',headers:{'Content-Type':'application/json'},
              body:JSON.stringify({target:pk,remove:true,auth:btoa(JSON.stringify(auth))})}).then(r=>r.json());
            ok=ok&&r.ok; if(r&&!r.ok&&r.error) toast(r.error);
          }
          if(capsChanged){
            const auth=await sign(27235,'user-caps',[['p',pk]]);
            const r=await fetch('/client/user-caps',{method:'POST',headers:{'Content-Type':'application/json'},
              body:JSON.stringify({target:pk,caps:out,auth:btoa(JSON.stringify(auth))})}).then(r=>r.json()); ok=ok&&r.ok;
          }
          const wantBridge=$('#perm-bridge',root).checked;
          if(wantBridge!==bridgeOn){
            toast(wantBridge?'creating fediverse account…':'disabling bridge…');
            const auth=await sign(27235,'bridge-access',[['action',wantBridge?'grant':'revoke'],['p',pk]]);
            const r=await fetch('/client/bridge-access',{method:'POST',headers:{'Content-Type':'application/json'},
              body:JSON.stringify({target:pk,grant:wantBridge,auth:btoa(JSON.stringify(auth))})}).then(r=>r.json());
            ok=ok&&r.ok; if(r&&!r.ok&&r.error) toast(r.error);
          }
          toast(ok?'permissions saved':'some changes failed'); closeModal();
        }catch(_){ toast('save failed'); }
      };
    });
  }
  // admin: grant/revoke this account's AI access (the can_ai flag). Signed like doBlock.
  async function toggleAiAccess(pk, grant){
    if(!S.IS_ADMIN) return;
    try{
      const auth = await sign(27235, 'ai-access', [['action', grant?'grant':'revoke'],['p',pk]]);
      const r = await fetch('/client/ai-access', { method:'POST', headers:{'Content-Type':'application/json'},
        body: JSON.stringify({ target: pk, grant, auth: btoa(JSON.stringify(auth)) }) }).then(r=>r.json());
      toast(r.ok ? (grant?'granted AI access 🤖':'revoked AI access') : ('failed: '+(r.error||'')));
    }catch(e){ toast('AI access change failed'); }
  }
  // admin: grant/revoke this account's Blossom upload access (adds/removes its npub from the
  // blossom_whitelist setting — Admin → Blossom). Signed like doBlock so the server checks admin.
  async function toggleBlossomAccess(pk, grant){
    if(!S.IS_ADMIN) return;
    try{
      const auth = await sign(27235, 'blossom', [['action', grant?'grant':'revoke'],['p',pk]]);
      const r = await fetch('/client/blossom-access', { method:'POST', headers:{'Content-Type':'application/json'},
        body: JSON.stringify({ target: pk, grant, auth: btoa(JSON.stringify(auth)) }) }).then(r=>r.json());
      toast(r.ok ? (grant?'granted Blossom access 🌸':'revoked Blossom access') : ('failed: '+(r.error||'')));
    }catch(e){ toast('blossom access change failed'); }
  }
  /* EDIT FROM THE NEWEST PROFILE THERE IS, NEVER FROM WHAT THE PAGE WAS PAINTED WITH.
   *
   * A kind-0 is REPLACEABLE: Save publishes the whole form, so a form filled from a stale copy
   * writes that stale copy back over the newer one. The profile page paints from the cache first
   * and patches its header when the relay answers -- but its Edit button kept the object it was
   * PAINTED with. Reported from the PosterChanOS desktop: the About line with "https://poster.place"
   * was saved (from another device) and shown in the header, and Edit Profile opened without it --
   * so the next Save would have deleted it. So the editor asks the relay for the latest kind-0 first
   * (bounded, so a dead relay costs seconds, not the editor) and opens with whatever is newest; the
   * Store keeps the newest by created_at, so an older answer can never downgrade it. */
  async function editProfile(p){
    const me = S.ME && S.ME.pubkey;
    if(me){
      const within = (ms, work) => Promise.race([work, new Promise(r => setTimeout(() => r(null), ms))]);
      try{
        if(window.Relay && Relay.ready) await within(2500, Relay.ready());
        const evs = await within(4000, Relay.query([{authors:[me], kinds:[0], limit:1}]));
        for(const e of (evs || [])) if(e && e.pubkey === me) Store.saveProfile(e);
      }catch(_){ /* offline: the freshest cached copy below is still better than the painted one */ }
    }
    const fresh = me ? Store.profile(me) : null;
    return _openProfileEditor(fresh || p || {});
  }
  function _openProfileEditor(p){
    /* Sticky for the same reason as the composers: a bio and a set of links are typed once, exist
     * nowhere else, and this sheet is a big target. The ✕ keeps it escapable. */
    modal(`<h3 class="cmp-hd">Edit profile<button class="modal-x" id="pf-close" title="Close" aria-label="Close">&#215;</button></h3>
      <label class="fld">Display name<input class="input" id="pf-name" placeholder="your name" value="${enc(p.name||p.display_name||'')}"></label>
      <!-- Music is deliberately near the top. On a phone it used to sit below eight profile fields
           and a tall bio, outside the first several screens of this scrolling sheet; the feature
           existed but "Edit profile" appeared to contain nothing related to it. -->
      <div class="fld pf-music-editor"><span>Profile music</span><div id="pf-music-list">${_profileMusicFields(p).map(([label,url])=>`<div class="pf-music-row"><input class="input pf-music-title" aria-label="Track title" placeholder="Track title" value="${enc(String(label||'').replace(/^🎶\s*/,''))}"><input class="input pf-music-url" aria-label="Audio URL" placeholder="https://…/track.mp3" value="${enc(url)}"><button class="btn btn-ghost small pf-music-remove" type="button" aria-label="Remove track">Remove</button></div>`).join('')}</div>
        <div class="row pf-music-actions"><button class="btn btn-ghost small" id="pf-music-add" type="button">Add audio URL</button><button class="btn btn-ghost small" id="pf-music-up" type="button">Upload music</button><input type="file" id="pf-music-file" accept="audio/*,.mp3,.mpga,.m4a,.aac,.ogg,.opus,.wav,.flac" multiple hidden></div></div>
      <label class="fld">NIP-05 identifier<input class="input" id="pf-nip05" placeholder="name@domain" value="${enc(p.nip05||'')}"></label>
      <label class="fld">⚡ Lightning address<input class="input" id="pf-lud16" placeholder="you@walletofsatoshi.com" value="${enc(p.lud16||'')}"></label>
      <label class="fld">ɱ Monero address<input class="input" id="pf-xmr" placeholder="4… or 8… (XMR — others can tip you)" value="${enc(xmrOf(p))}"></label>
      <label class="chk" style="display:flex;gap:8px;align-items:flex-start;margin:-4px 0 8px;font-size:13px"><input type="checkbox" id="pf-xmr-stamp" ${ClientSettings.get('xmrStampNotes',false)?'checked':''} style="margin-top:3px"><span class="muted">Attach my Monero address to every post so any client can tip me from a post (like Nosmero). <b>Less private</b> — it links all your posts to one address. Off = address only on your profile.</span></label>
      <label class="fld">🟢 Bitcoin Cash address<input class="input" id="pf-bch" placeholder="bitcoincash:q… (others can tip you)" value="${enc(bchDirect(p))}"></label>
      <details id="pf-payment-details"><summary>Payment targets</summary><div id="pf-payment-editor"></div></details>
      <label class="fld">Picture URL<input class="input" id="pf-pic" placeholder="https://…" value="${enc(p.picture||'')}"></label>
      <label class="fld">Banner URL<input class="input" id="pf-banner" placeholder="https://…" value="${enc(p.banner||'')}"></label>
      <label class="fld">About<textarea id="pf-about" placeholder="a few words about you">${enc(p.about||'')}</textarea></label>
      <div class="row"><button class="btn btn-cyan small" id="pf-up"><svg class="ic b-ic" aria-hidden="true"><use href="#i-image"></use></svg>Upload pic</button><input type="file" id="pf-file" accept="image/*" hidden><span class="spacer"></span><button class="btn btn-neon" id="pf-save">Save</button></div>`, root=>{
      root.classList.add('modal-sticky');
      _bindPaymentTargetEditor(root);
      { const x=$('#pf-close',root); if(x) x.onclick=()=>closeModal(); }
      // This node may have ASSIGNED this account a NIP-05 at signup that its kind-0 never carried —
      // e.g. the signup publish lost the race with the first socket. The name is a public read, so
      // prefill the empty field with it: the verified handle is then one Save away instead of a
      // string the user would have to already know.
      { const n5=$('#pf-nip05',root);
        if(n5 && !n5.value.trim()) fetch('/client/admin-nip05?pubkey='+encodeURIComponent(S.ME.pubkey))
          .then(r=>r.json()).then(r=>{ if(r && r.ok && r.nip05 && !n5.value.trim()) n5.value=r.nip05; })
          .catch(()=>{}); }
      $('#pf-up',root).onclick=()=>$('#pf-file',root).click();
      $('#pf-file',root).onchange=async e=>{ const f=e.target.files[0]; if(!f)return; try{ const _u=await uploadBlob(f); $('#pf-pic',root).value=_u;
        // Index it into the Files list too — otherwise a profile pic uploaded here lands on Blossom but
        // never shows under Files ("updated my pic but it's not in blossom"). Best-effort.
        try{ const _sha=_shaFromUrl(_u); if(_sha) FilesIdx.setFile(_sha,{name:f.name||'profile-pic', folder:'', mime:f.type||'', size:f.size, ts:Math.floor(Date.now()/1000)}); }catch(_){}
        toast('uploaded'); }catch(err){toast('upload failed');} };
      const musicList=$('#pf-music-list',root);
      const addMusic=(title,url)=>{
        const row=document.createElement('div'); row.className='pf-music-row';
        row.innerHTML=`<input class="input pf-music-title" aria-label="Track title" placeholder="Track title" value="${enc(title||'')}"><input class="input pf-music-url" aria-label="Audio URL" placeholder="https://…/track.mp3" value="${enc(url||'')}"><button class="btn btn-ghost small pf-music-remove" type="button" aria-label="Remove track">Remove</button>`;
        $('.pf-music-remove',row).onclick=()=>row.remove(); musicList.appendChild(row);
      };
      $$('.pf-music-remove',musicList).forEach(b=>b.onclick=()=>b.closest('.pf-music-row').remove());
      $('#pf-music-add',root).onclick=()=>addMusic('','');
      $('#pf-music-up',root).onclick=()=>$('#pf-music-file',root).click();
      $('#pf-music-file',root).onchange=async e=>{
        for(const f of Array.from(e.target.files||[])) try{
          // A Ditto profile field must remain publicly playable. Keep this copy unfiled; putting a
          // plaintext blob under encrypted Music creates an undecryptable personal-library row.
          const url=await uploadBlob(f,{noCompress:true});
          const title=(f.name||'Track').replace(/\.[^.]+$/,''); addMusic(title,url);
        }catch(err){ toast('music upload failed: '+((err&&err.message)||err)); }
        e.target.value='';
      };
      $('#pf-save',root).onclick=async()=>{ const _xmr=$('#pf-xmr',root).value.trim();
        if(_xmr && !isXmrAddr(_xmr)){ toast('that doesn\'t look like a Monero address (starts 4 or 8)'); $('#pf-xmr',root).focus(); return; }   // keeps the modal open → other edits aren't lost
        const _bch=$('#pf-bch',root).value.trim().replace(/^bitcoincash:/i,'');
        if(_bch && !isBchAddr(_bch)){ toast('that doesn\'t look like a Bitcoin Cash address'); $('#pf-bch',root).focus(); return; }
        { const sc=$('#pf-xmr-stamp',root); if(sc) ClientSettings.set('xmrStampNotes', !!sc.checked); }   // opt-in: attach my XMR to my posts — per-device only (NOT synced: it's an address-linking privacy choice)
        const meta={ ...p, name:$('#pf-name',root).value.trim(), nip05:$('#pf-nip05',root).value.trim(), lud16:$('#pf-lud16',root).value.trim(), picture:$('#pf-pic',root).value.trim(), banner:$('#pf-banner',root).value.trim(), about:$('#pf-about',root).value.trim() };
        /* Replace only the music entries we own. Ditto and other clients may put arbitrary labelled
         * links in `fields`; saving a song must never erase those unrelated profile fields. */
        const musicOld=new Set(_profileMusicFields(p));
        const fields=(Array.isArray(p.fields)?p.fields:[]).filter(x=>!musicOld.has(x));
        $$('.pf-music-row',root).forEach(row=>{ const title=$('.pf-music-title',row).value.trim(),url=$('.pf-music-url',row).value.trim();
          if(url && /^https?:\/\//i.test(url)) fields.push(['🎶'+(title||'Track'),url]); });
        if(fields.length) meta.fields=fields; else delete meta.fields;
        // Publish the Monero address to BOTH `monero_address` (the field most OTHER clients read — incl.
        // Nosmero — so they can tip you) AND `xmr` (what this client also reads); clearing removes every
        // alias. Previously we wrote ONLY `xmr` and DELETED monero_address, which hid your address from
        // those clients and even wiped a monero_address set elsewhere.
        /* Aliases are only rewritten when the address actually CHANGED.
         *
         * These deletes used to be unconditional, which meant saving ANY unrelated edit — a display
         * name — stripped `monero`/`bch_address`/`bitcoincash` from the profile. Someone whose other
         * client reads only `bch_address` would have had their tipping silently broken by us, having
         * touched nothing to do with money. Same failure as pre-filling the field from a bio scan:
         * writing over parts of someone's profile they never asked us to manage.
         *
         * Changed or cleared, folding the aliases in IS right — otherwise a stale old address stays
         * live under a key we no longer write, and money goes to the wrong place. */
        const _xmrWas = xmrOf(p), _bchWas = bchDirect(p);
        if(_xmr){
          meta.xmr=_xmr; meta.monero_address=_xmr;
          /* …and the MAP form, so a Garnet reader (the Amethyst fork with Monero tipping — stock
           * Amethyst has none) sees it. Reported as "I added a Monero address with poster.place and
           * it doesn't show up in Amethyst": the address was written correctly, to keys that client
           * does not read. Merged into whatever else is in the map rather than replacing it — the
           * same rule as the aliases below, since the other coins in there are not ours to manage. */
          if(_xmr !== _xmrWas){
            const _m = (p.cryptocurrency_addresses && typeof p.cryptocurrency_addresses==='object')
              ? Object.assign({}, p.cryptocurrency_addresses) : {};
            _m.monero = _xmr;
            meta.cryptocurrency_addresses = _m;
          }
          if(_xmr !== _xmrWas) delete meta.monero;
        } else if(_xmrWas){
          delete meta.xmr; delete meta.monero_address; delete meta.monero;
          /* Clearing has to reach the MAP too, or a stale address stays live under a key we no
           * longer write and money goes to the wrong place — the reason the aliases are folded in
           * on a change. The map itself is only dropped when Monero was the last thing in it; the
           * other coins there belong to whatever client put them there. */
          if(p.cryptocurrency_addresses && typeof p.cryptocurrency_addresses==='object'){
            const _m = Object.assign({}, p.cryptocurrency_addresses);
            for(const k of ['monero','xmr','XMR','Monero']) delete _m[k];
            if(Object.keys(_m).length) meta.cryptocurrency_addresses = _m;
            else delete meta.cryptocurrency_addresses;
          }
        }
        if(_bch){
          meta.bch=_bch; meta.bitcoincash_address=_bch;
          if(_bch !== _bchWas){ delete meta.bch_address; delete meta.bitcoincash; }
        } else if(_bchWas){
          delete meta.bch; delete meta.bitcoincash_address; delete meta.bch_address; delete meta.bitcoincash;
        }
        closeModal(); { const r=await publish(0, JSON.stringify(meta), _kind0Tags(S.ME && S.ME.pubkey));   // failure toast by publish()
          if(r && r.ok){ Store.saveProfile({pubkey:S.ME.pubkey,created_at:Math.floor(Date.now()/1000),content:JSON.stringify(meta)}); toast('profile saved'); renderMe(); renderProfileView(S.ME.pubkey); } } };
    });
  }
  // Show the relays a user publishes to (NIP-65 kind-10002), with read/write markers.
  async function showRelays(pk){
    modal(`<h3><svg class="ic h-ic" aria-hidden="true"><use href="#i-relay"></use></svg>Relays</h3><div id="rl-body" class="muted small">Loading…</div>`);
    let evs=[]; try{ evs=await Relay.query([{authors:[pk],kinds:[10002],limit:1}]); }catch(_){}
    const ev=(evs||[]).sort((a,b)=>b.created_at-a.created_at)[0];
    const body=$('#rl-body'); if(!body) return;
    const rs=ev ? (ev.tags||[]).filter(t=>t[0]==='r'&&t[1]) : [];
    if(!rs.length){ body.textContent='This user hasn’t published a relay list (NIP-65).'; return; }
    body.classList.remove('muted','small');
    body.innerHTML='<div class="prof-relays">'+rs.map(t=>{
      const mode = t[2] ? enc(t[2]) : 'read/write';
      return `<div class="prof-relay"><code>${enc(t[1])}</code><span class="muted small">${mode}</span></div>`;
    }).join('')+'</div>';
  }
  async function peopleModal(title, pks){
    modal(`<div style="display:flex;align-items:center;justify-content:space-between;gap:8px;flex-wrap:wrap"><h3 style="margin:0">${enc(title)} (${pks.length})</h3><button id="follow-all-back" class="btn btn-cyan small" style="display:none">Follow all back</button></div><div id="people-list" class="people-list"><div class="spinner"></div></div>`, async root=>{
      const miss=pks.filter(p=>!Store.haveProfile(p)).slice(0,300);
      if(miss.length){ try{ const evs=await Relay.query([{authors:miss,kinds:[0],limit:miss.length}]); evs.forEach(e=>Store.saveProfile(e)); }catch(_){} }
      await ensureMyFollowers();   // so we can flag mutuals ("Follows you") in any people list
      const list=$('#people-list',root); if(!list) return;
      list.innerHTML = pks.length ? pks.slice(0,400).map(p=>{ const m=Store.profile(p)||{};
        // "Follows you" badge = this person follows ME back (mutual). "Follow back" button = anyone I
        // don't follow yet — so the Following list shows who's reciprocal and the Followers list shows
        // who I haven't followed back.
        const followsMe = p!==S.ME.pubkey && FOLLOWERS.has(p);
        const canFollow = p!==S.ME.pubkey && !S.FOLLOWS.has(p);
        return `<div class="psearch" data-prof="${p}"><img src="${enc(m.picture||S.LOGO)}" onerror="this.src='${S.LOGO}'"><div class="pinfo"><b>${enc(m.name||m.display_name||NT().nip19.npubEncode(p).slice(0,14))}${followsMe?'<span class="follows-you">Follows you</span>':''}</b><div class="muted small">${enc(niceNip05(m.nip05)||'')}</div></div>${canFollow?`<button class="btn btn-cyan small pfollow" data-fb="${p}">Follow back</button>`:''}</div>`;
      }).join('') : '<div class="empty">Nobody here.</div>';
      $$('[data-prof]',list).forEach(el=> el.onclick=(ev)=>{ if(ev.target.closest('.pfollow')) return; closeModal(); renderProfileView(el.dataset.prof); });
      $$('.pfollow',list).forEach(b=> b.onclick=async(ev)=>{ ev.stopPropagation(); b.disabled=true; b.textContent='…';
        try{ if(await toggleFollow(b.dataset.fb)){ b.textContent='Following ✓'; b.classList.remove('btn-cyan'); b.classList.add('btn-ghost'); }
             else { b.disabled=false; b.textContent='Follow back'; } }   // relay didn't store it (publish() toasted) → leave the button actionable
        catch(_){ b.disabled=false; b.textContent='Follow back'; } });
      // "Follow all back": one-tap follow of everyone in this list I don't already follow (one publish).
      const followable=pks.filter(p=>p!==S.ME.pubkey && !S.FOLLOWS.has(p));
      const fab=$('#follow-all-back',root);
      if(fab && followable.length){
        fab.style.display=''; fab.textContent=`Follow all back (${followable.length})`;
        fab.onclick=async()=>{ fab.disabled=true; const orig=fab.textContent; fab.textContent='…';
          try{ const n=await followMany(followable);
            if(!n){ fab.disabled=false; fab.textContent=orig; return; }   // relay didn't store it (publish() toasted) → keep it actionable
            $$('.pfollow',list).forEach(b=>{ b.disabled=true; b.textContent='Following ✓'; b.classList.remove('btn-cyan'); b.classList.add('btn-ghost'); });
            fab.style.display='none'; toast(`followed ${n} back`);
            if(S.VIEW==='home') renderView(true);
          }catch(_){ fab.disabled=false; fab.textContent=orig; } };
      }
    });
  }
  return {
    doPurgeBlossom, editProfile, loadOlderProfile, renderProfile, renderProfileView,
  };
};
