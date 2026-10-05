/* timeline.js — split out of app.js by scripts/split_client_module.mjs.
 *
 * The Home / Nostrverse timeline. EAGER, not lazy: loaded by its own <script> tag BEFORE app.js and built by
 * app.js (`_timelineMod()`) at boot, where the block used to run, because every feed render calls
 * _noteNode / isSensitive / _tlNotes synchronously. The code below is
 * app.js's own, moved byte-for-byte; its reads of app.js's live `let` bindings were rewritten to
 * `S.<name>` (getters/setters on `dep.state`) at exact identifier offsets, and everything else it uses
 * arrives through `dep`. tests/test_client_module_deps.py proves every name resolves.
 */
window.PCTimelineFactory = function(dep){
  const S = dep.state;   // live app.js bindings (and the consts app.js declares BELOW the build point -- read lazily, or TDZ): S.AUTO_NEW_POSTS, S.BLUR_NSFW, S.CFG, S.FOLLOWS, S.GUEST, S.LOGO, S.ME, S.NEW_POSTS_PILL, S.VIEW, S._hiddenAt, S._liveFn, S._livePending, S._liveT, S._profObs, S._tl, S._tlForceTop, S._tlGen, S._tlMedia, S._tlPause, S._tlPaused, S._tlPausedAt, S._tlResume
  const {
    $, $$, _FEED_MAX_CARDS,
    _LIVE_READ_PX, _NEG_MAX_FETCH, _NEG_WINDOW, _TL_KINDS, _TL_SUB_VIEWS, _bgCss, _blossomDenied,
    _captureCamera, _dtLocal, _firstImage, _fmtBytes, _healGhostPairs, _hold, _insertAt,
    _leaveGuest, _putScroll, _registrationOpen, _tlScrollMemo, applySobLive,
    attachEmojiAutocomplete, attachMentionAutocomplete, blossomPicker, buildBgPost, compose,
    composeTranslate, decorateProfiles, enc, feedNoteHtml, gifPicker, hydrate, hydrateLinkCards,
    hydratePolls, imetaTagsFor, invalidateCounts, isMutedView, isReply, makeCardPreview,
    mentionTags, needProfile, openEmojiPopover, openMenuPopover, openThread, publish, renderView,
    requestBlossomAccess, subs, switchView, timelineFilter, tlHiddenSet, toast, updateOfflineBar,
    uploadBlob,
  } = dep;

  function _applyMediaCacheBudget(gb){
    try{ const bytes = Math.round((+gb || 4) * 1024 * 1024 * 1024);
      caches.open('pc-config-v1').then(c => c.put('/media-budget', new Response(String(bytes)))).catch(()=>{}); }catch(_){}
  }
  // Show the device's ACTUAL storage ceiling (Settings → Media). The media cache can never exceed what
  // the OS/WebView grants for this origin — on a bundled Android app that's a fraction of FREE disk and
  // persist() isn't reliably granted, so a near-full tablet caps far below the chosen budget. Surfacing
  // usage/quota lets the user see the real limit instead of guessing why caching "stops early".
  /* What the offline library is actually using. Counted from the store rather than estimated, and
   * shown next to the limit, because "4 GB" means nothing without "you are using 1.2 of it". */
  async function _fillMusicOfflineStat(){
    const el=$('#music-offline-stat'); if(!el) return;
    try{
      const u=await S.MusicOffline.usage();
      const budget=S.MusicOffline.budgetBytes();
      let tail = budget ? ' of ' + _fmtBytes(budget) : ' · no limit';
      if(!budget){
        try{
          if(navigator.storage && navigator.storage.estimate){
            const est = await navigator.storage.estimate();
            if(est && est.quota) tail += ' (this device allows ~' + _fmtBytes(est.quota) + ' in total)';
          }
        }catch(_){}
      }
      el.textContent = u.count
        ? (u.count + ' track' + (u.count===1?'':'s') + ' kept · ' + _fmtBytes(u.bytes) + tail)
        : 'No tracks kept offline on this device yet.';
    }catch(_){ el.textContent='Couldn’t read this device’s offline music.'; }
  }

  async function _fillMediaCacheStat(){
    const el=$('#media-cache-stat'); if(!el) return;
    try{
      if(!(navigator.storage && navigator.storage.estimate)){ el.textContent='This device doesn’t report storage stats.'; return; }
      const est=await navigator.storage.estimate();
      const persisted=(navigator.storage.persisted)?await navigator.storage.persisted().catch(()=>null):null;
      const g=n=>(n||0)/1073741824, f=n=>n<1?n.toFixed(2):n.toFixed(1);
      const used=g(est.usage), quota=g(est.quota), budget=+ClientSettings.get('mediaCacheGB',4);
      let msg=`This device: ~${f(used)} GB used of ~${f(quota)} GB the OS allows for the app.`;
      if(persisted===false) msg+=' Storage isn’t marked persistent (common in the Android app), so the OS caps it by free disk — freeing space on the device raises the ceiling.';
      if(quota>0 && quota < budget*0.6) msg+=` Your ${budget} GB setting is above this device’s limit, so caching stops at the device ceiling, not your setting.`;
      el.textContent=msg;
    }catch(_){ el.textContent=''; }
  }
  // Data saver = ONE manual toggle (NO_IMAGES): it holds content images/videos as tap-to-load
  // placeholders — that (not fewer posts) is where the bandwidth actually goes. No connection sniffing:
  // a carrier throttle looks like normal 5G to the browser, so auto-detection was unreliable.
  // We deliberately do NOT shrink the feed page size any more: text events are tiny, and a smaller page
  // just means MORE fetches to fill the same screen. On a high-latency throttled link (a mobile carrier
  // reaching a distant server) every extra round trip is the real cost, so shrinking pages made data
  // saver feel SLOWER to load new posts, not faster. Keep full pages; save data on media only.
  function _flim(n){ return n; }
  // A post counts as sensitive (and is blurred when BLUR_NSFW is on) if it carries a NIP-36
  // content-warning, OR a topic `t` tag in this set, OR an inline #nsfw-style hashtag — so the common
  // "#nsfw" convention auto-blurs even without a formal content-warning tag.
  const _NSFW_TAGS = new Set(['nsfw','porn','nude','nudity','sex','xxx','explicit','gore']);
  const _NSFW_RE = /(^|\s)#(nsfw|porn|nude|nudity|sex|xxx|explicit|gore)\b/i;
  function isSensitive(ev){
    if(!ev || !ev.tags) return false;
    if(ev.tags.some(t=>t[0]==='content-warning')) return true;
    if(ev.tags.some(t=>t[0]==='t' && _NSFW_TAGS.has(String(t[1]||'').toLowerCase()))) return true;
    return _NSFW_RE.test(ev.content||'');
  }
  let _liveSince = 0;   // sub start time — only events at/after this are "live" (prependable as new)
  function renderTimeline(view, reset){
    /* RE-ENTERING THE TIMELINE YOU ARE ALREADY ON IS A REPAINT, NOT AN ARRIVAL.
     *
     * "on desktop replying to a post is still bringing me to top of timeline." On the windowed
     * desktop, focusing a window calls `switchView` with the view that window is ALREADY showing —
     * so the save in switchView is skipped (`VIEW !== v` is false), `renderView(true)` lands here
     * with reset, and `_drawTimeline(false)` puts the reader at the top with no remembered offset to
     * come back to. The same thing happens anywhere else that repaints the current timeline.
     *
     * So the offset is taken from the live feed here, where it is still real, and put back after the
     * draw. Only when this IS the view already on screen: arriving from somewhere else, #feed still
     * holds the OLD view's content and its scrollTop means nothing about this one. */
    const fn = _tlFilter(view);
    /* A repaint that did NOT come through renderView still has its offset here — renderView's own
     * capture (above the spinner) covers the reset path, because by the time this runs on that path
     * the feed has already been blanked and scrollTop is 0. Both are needed; neither is enough. */
    const forceTop = S._tlForceTop === view;
    if(forceTop) delete _tlScrollMemo[view];
    if(S.VIEW === view && !forceTop){
      try{ const f0 = $('#feed'); const at = (f0 && f0.scrollTop) || 0;
           if(at > 0 && !(_tlScrollMemo[view] > 0)) _tlScrollMemo[view] = at; }catch(_){ }
    }
    if(reset){ S._tl = { oldest:0, loading:false, done:false, pages:0, eosed:false }; _resetLive(); _liveSince = Math.floor(Date.now()/1000); }
    _drawTimeline(false);
    /* PUT THEM BACK WHERE THEY WERE. The offset was saved by switchView on the way out; this is the
     * first moment the cards exist to scroll to.
     *
     * Consumed once, deliberately — `delete` before the frame, not after. The live re-renders that
     * fire as relay events arrive come back through here, and an offset that survived would yank a
     * reader back up the feed every time somebody posted. It answers "return me to where I was",
     * once, and then the scroll belongs to the person again.
     *
     * Deferred a frame because scrollTop cannot exceed a height that has not been laid out yet: set
     * synchronously against a feed the browser has not measured, it clamps to whatever fits and the
     * restore silently lands near the top. */
    try{
      const want = _tlScrollMemo[view];
      if(forceTop){
        delete _tlScrollMemo[view];
        S._tlForceTop='';
        const f=$('#feed');if(f)f.scrollTop=0;
      }else if(want > 0){
        delete _tlScrollMemo[view];
        _putScroll(want, () => S.VIEW === view);
      }
    }catch(_){ }
    if (subs[view]) Relay.close(subs[view]);
    /* The FIRST post that arrives for a timeline nobody can see is the one that closes it.
     *
     * renderView drops it the moment you navigate away, and that covers the sidebar. This is the leg
     * that cannot be forgotten: renderThread and renderProfile set VIEW themselves without going
     * through renderView, os.js closes a window without one, and any view added later may do the
     * same — so the decision is also taken here, where the waste actually shows up, off the same
     * function so the two answers cannot differ. One event is the entire cost of being late. */
    const onEvent = ev => {
      /* AN ARRIVING EVENT IS THE ONE PROOF OF BEING ONLINE THAT IS MEASURED RATHER THAN RETOLD.
       *
       * The banner is driven by Relay's STATUS stream, which reports CHANGES — so a socket that
       * died while the phone was asleep and was quietly replaced never announces itself, and the
       * banner latches on over a feed that is loading perfectly. Reported as "I can pull up another
       * app or webpage, but the app registers as offline". A relay just pushed us a note down a
       * socket; there is nothing left to argue about.
       *
       * Gated on the banner actually being up, because this runs once per event on a firehose and
       * the call is only ever worth making when it changes something on screen. */
      if(document.body.classList.contains('is-offline')){ try{ updateOfflineBar('ok'); }catch(_){} }
      if(S.VIEW!==view && !_parkedSlot(view)) _parkOffscreenTimelines();
      if (Store.saveEvent(ev)){ invalidateCounts(); applySobLive(ev); needProfile(ev.pubkey);
      // Only prepend as "live" if it's genuinely new — NOT a backfilled/synced event with an old
      // created_at (those jump to the top as if new). kind-5 deletions are NOT posts (render blank) —
      // watchDeletions handles them, so keep them out of the prepend. A small grace covers skew.
      /* `_parkedSlot(view)`, not `_tlParked()`. The latter is "VIEW is not a timeline", which is
       * false whenever the FOCUSED window is the other timeline — so with Home in front and
       * Nostrverse parked beside it, the global subscription's events were never buffered at all and
       * that window stayed frozen for as long as both were open. Asking whether THIS view has a
       * parked window is the question that was meant, and it is true in both arrangements. */
      if ((S.VIEW===view || _parkedSlot(view)) && ev.kind!==5 && _TL_KINDS.includes(ev.kind) && S._tl.eosed && ev.created_at >= _liveSince-120) _bufferLive(ev, fn, view); } };
    // Draw ONLY on the first EOSE. The relay re-EOSEs on reconnect; redrawing then wipes the feed.
    /* AN EOSE IS NOT A NAVIGATION, so it must not move the reader.
     *
     * This redrew with preserveScroll=false — resetting #feed to the top — and EOSE arrives at every
     * moment somebody is mid-feed: after publishing a post (the composer re-subscribes), after a
     * relay reconnects, after returning to a backgrounded tab. Reported as "I can scroll down, make
     * a post, and then it resets me at the top, there are several other scenarios where this happens
     * too, but it'd be hard to purposefully find them" — and they are hard to find on purpose
     * precisely because the trigger is a relay's end-of-stream, not anything the person did.
     *
     * Only a fresh ENTRY to a timeline starts at the top (renderTimeline), and only a deliberate
     * change of what is shown does (the media toggle). Everything else redraws where they are. */
    const markEosed = ()=>{
      /* NOT the place to declare us online, however tempting: `Relay.subscribe` fires onEose from a
       * 12-SECOND BACKSTOP when no relay answers at all (so callers that use EOSE as the
       * backlog→live boundary cannot freeze). Clearing the banner here would therefore announce a
       * connection precisely when there is none. The proof lives in onEvent, above. */
      clearTimeout(_eoseWatch); _eoseWatch=null;
      if(S.VIEW===view && !S._tl.eosed){ S._tl.eosed=true; _drawTimeline(true); }
    };
    /* NOBODY ANSWERED. A REQ written to a socket that cannot carry it is dropped in silence by
     * `relay.js _send` — no error, no event, no EOSE — and one dropped REQ freezes this timeline for
     * the whole visit: `_tl.eosed` never becomes true, and `_bufferLive` is gated on it, so the live
     * posts that DO arrive are discarded as well. That is the whole of "if I close and reopen the
     * app it shows new posts from a minute ago": a fresh boot is the only thing that ever asked
     * again. So ask again, once, on a socket `ready()` has had a chance to repair.
     *
     * Once. A second silence is a relay that is not answering, which is a fact about the network and
     * not something more asking will fix — the cached feed stays on screen, which is honest. */
    let _eoseWatch=null, _eoseRetried=false;
    const _watchEose = ()=>{
      clearTimeout(_eoseWatch);
      _eoseWatch=setTimeout(()=>{
        _eoseWatch=null;
        if(_eoseRetried || S._tl.eosed || myGen!==S._tlGen || S.VIEW!==view) return;
        _eoseRetried=true;
        if(subs[view]){ try{ Relay.close(subs[view]); }catch(_){} subs[view]=null; }
        fullSub();
      }, 7000);
    };
    // Re-entrancy token: a slow async negSync that resolves AFTER the user re-navigated (or re-rendered)
    // must not install — and leak — a subscription for a superseded render. setSub closes such orphans.
    const myGen = ++S._tlGen;
    const setSub = (s)=>{
      if(myGen!==S._tlGen || S.VIEW!==view){ try{ Relay.close(s); }catch(_){} return; }
      // Close what we are replacing. Two installers can now run in ONE generation (a resume, plus a
      // negSync that resolves afterwards), and a plain overwrite orphans the loser: still live, still
      // in Relay._subs, re-REQ'd on every reconnect, and reachable by nothing that could close it.
      if(subs[view] && subs[view]!==s){ try{ Relay.close(subs[view]); }catch(_){} }
      subs[view]=s;
    };
    /* OPENING THE FEED ASKS A SOCKET THAT CAN ANSWER — `await Relay.ready()`.
     *
     * This is the rule Trending was fixed with and the timeline itself never got, which is worse:
     * a REQ written to a CONNECTING socket is dropped with nothing said, and the moment a timeline
     * is most likely to be opened against one is right after a resume or a login. `ready()` also
     * REVIVES a zombie — a socket the browser still reports OPEN that has delivered nothing for
     * 30 seconds, which is exactly what a phone comes back from sleep holding. Without it the feed
     * subscribes into a dead pipe and sits there looking connected.
     *
     * The PAINT is untouched and still happens first, from the Store, above — only the network half
     * waits. Nothing on screen is delayed by this; what changes is that the ask actually lands. */
    const fullSub = ()=>{
      if(document.hidden && S._tlPaused) return;   // stay paused; _tlResume re-arms on return
      const go = ()=>{
        if(myGen!==S._tlGen || S.VIEW!==view) return;
        if(document.hidden && S._tlPaused) return;
        setSub(Relay.subscribe(timelineFilter(), { onEvent, onEose: markEosed }));
        _watchEose();
      };
      // Both arms run `go`: a pool that never came up still deserves the REQ (a relay may connect a
      // moment later and the subscription is already installed), and never asking is the bug.
      try{ Relay.ready(8000).then(go, go); }catch(_){ go(); }
    };
    /* Backgrounded, the timeline is pure waste: the relay keeps PUSHING every matching event down the
     * socket — on a busy web-of-trust relay that is the firehose — and the phone wakes its radio to
     * receive posts nobody is looking at. Calls, DMs and notifications must keep flowing, so the socket
     * stays; only this subscription is dropped, and re-armed on return.
     *
     * Resuming is the same fullSub() a reconnect already runs, and markEosed only draws on the FIRST
     * EOSE, so coming back cannot wipe the feed. The generation token makes a resume that lands after
     * the user navigated a no-op rather than a leaked subscription for a dead view. */
    S._tlPause = ()=>{
      const cur=subs[view];
      if(cur && myGen===S._tlGen){ try{ Relay.close(cur); }catch(_){} subs[view]=null; }
      /* Keep the FIRST instant. Android can report both visibilitychange and appStateChange; moving
       * this timestamp forward on the second signal would leave a gap between the two. */
      if(!S._tlPausedAt) S._tlPausedAt=Math.floor(Date.now()/1000);
      S._tlPaused=true;
    };
    let _resumeCatchAt=0;
    S._tlResume = ()=>{
      const wasPaused=S._tlPaused;
      S._tlPaused=false;
      /* Reconcile on EVERY foreground, not only after the 20-second battery pause fired. Android
       * commonly freezes the WebView during a short app switch while the grace timer itself is
       * throttled. The relay reconnect then replays missed notes through the live callback, where
       * they can remain behind the new-posts buffer until a cold restart. Querying from the actual
       * background instant redraws those notes from Store in timestamp order, preserving the card
       * being read. Native resume + appStateChange often arrive as a pair, so coalesce them. */
      const since=Math.max(0,(S._tlPausedAt||Math.floor((S._hiddenAt||Date.now())/1000))-120);
      S._tlPausedAt=0;
      if(myGen!==S._tlGen || S.VIEW!==view) return;
      /* Re-open live delivery immediately, then explicitly fetch the interval the phone missed.
       * Relying on a subscription's generic `limit` made catch-up depend on relay ordering and could
       * leave the feed minutes behind after a long sleep. Store deduplicates by event id;
       * _drawTimeline reads its timestamp-sorted feed and preserves the visible-card anchor. */
      if(wasPaused && !subs[view]) fullSub();
      if(Date.now()-_resumeCatchAt < 4000) return;
      _resumeCatchAt=Date.now();
      const catchFilters=timelineFilter().map(f=>Object.assign({},f,{since,
        limit:Math.max(Number(f.limit)||0,500)}));
      /* A foreground signal can precede the radio becoming usable. Do not spend the one catch-up
       * query on the CONNECTING/zombie socket; ready() repairs it and waits for an OPEN path. */
      Relay.ready(8000).then(()=>Relay.query(catchFilters)).then(evs=>{
        if(myGen!==S._tlGen || S.VIEW!==view) return;
        let changed=false;
        for(const ev of (evs||[])){
          if(Store.saveEvent(ev)){ changed=true; needProfile(ev.pubkey); }
        }
        if(changed) _drawTimeline(true);
      }).catch(()=>{});
    };
    // Phase 2 (NIP-77): reconcile the HOME feed with the relay via negentropy and fetch ONLY the events
    // we're missing, then keep a live-only sub — far less bandwidth than re-pulling the page every
    // reconnect. Gated on a WARM cache: negentropy is bounded to a recent window, so a cold/low-volume
    // cache would show a near-empty feed (older posts out of window) — those fall back to the plain
    // limit:80 REQ, which returns the newest N regardless of age. Hard fallback on ANY negentropy failure
    // (never loses data). The GLOBAL firehose is UNbounded — stays a plain limit REQ.
    if(view==='home' && S.FOLLOWS.size && Store.feed(fn).length >= 40){
      const negF = [{ kinds:_TL_KINDS, authors:[...S.FOLLOWS], since: Math.floor(Date.now()/1000) - _NEG_WINDOW }];
      Relay.negSync(negF).then(need=>{
        if(myGen!==S._tlGen || S.VIEW!==view) return;
        // Big delta (very stale cache) → cheaper + simpler to just limit-pull than to id-fetch hundreds.
        if(need.size > _NEG_MAX_FETCH){ fullSub(); return; }
        const after = ()=>{ markEosed();
          setSub(Relay.subscribe([{ kinds:_TL_KINDS, authors:[...S.FOLLOWS], since:_liveSince }], { onEvent, onEose: ()=>{} })); };  // live-only
        if(need.size){ Relay.query([{ ids:[...need], limit: need.size }]).then(evs=>{ evs.forEach(e=>{ if(Store.saveEvent(e)) needProfile(e.pubkey); }); after(); }).catch(after); }
        else after();
      }).catch(()=>{ if(myGen===S._tlGen && S.VIEW===view) fullSub(); });   // fallback: plain full REQ
    } else fullSub();
  }
  // Batched live updates: a busy global feed must NOT prepend + re-render per event (that pegged
  // the CPU and flashed). Buffer incoming notes and prepend them together a few times a second,
  // capping the feed and keeping scroll stable.
  /* ONE BUFFER PER TIMELINE, not one for the app.
   *
   * `home` and `global` are two live subscriptions with two different filters, and in desktop mode
   * they can be two windows on screen at the same time. A single `_liveBuf` + `_liveFn` merged both
   * streams and kept only the LAST writer's filter and view, so a flush delivered the whole mixed
   * batch to one window through the other's filter: unfollowed strangers from the firehose prepended
   * into the following-feed, while the window they belonged to got nothing. Keyed by view, each
   * batch keeps the filter and the destination it was collected for. */
  const _liveBufs = new Map();          // view -> { buf:[], fn }
  function _bufferLive(ev, fn, view){
    const k = view || S.VIEW;
    let b = _liveBufs.get(k);
    if(!b){ b = { buf:[], fn }; _liveBufs.set(k, b); }
    b.fn = fn; b.buf.push(ev);
    if(!S._liveT) S._liveT=setTimeout(flushLive, 1800);
  }
  /* Where a PARKED timeline's live posts belong.
   *
   * os.js owns the answer — it is the only thing that knows which window shows which view, and with
   * Home and Nostrverse both open the document holds two elements with `id="tl-notes"`, so
   * getElementById would silently answer with whichever opened first. The single-match DOM fallback
   * is for a client running an os.js from before parkedSlot existed: unambiguous, so it cannot pick
   * the wrong one; with two timelines open it declines to guess and the posts wait for the redraw. */
  function _parkedSlot(view){
    /* When os.js can answer, its answer is FINAL — including "no". The fallback used to run on any
     * null, which turned a deliberate refusal ("the one parked timeline is not the one that
     * buffered these") back into the wrong window, defeating the lookup it was there to back up.
     * It exists only for a client whose os.js predates parkedSlot. */
    try{ if(window.PCOS) return (PCOS.parkedSlot ? PCOS.parkedSlot(view) : _parkedSlotDom()) || null; }catch(_){}
    return null;
  }
  function _parkedSlotDom(){
    const all=document.querySelectorAll('[id="tl-notes"]');
    return all.length===1 ? all[0].closest('.osw-slot') : null;   // two open → decline rather than guess
  }
  /* A TIMELINE NOBODY IS LOOKING AT IS NOT SUBSCRIBED.
   *
   * `_tlBackground` asks "is this APP on screen?". This asks the other half of the same question —
   * "is this TIMELINE on screen?" — and nothing was asking it. renderTimeline closes and re-opens the
   * subscription for the view being ENTERED; nothing ever closed the one being LEFT. So one visit to
   * the timeline left the firehose REQ open for the rest of the session, streaming every
   * kind-1/6/1068/30023/40 on the relay into a Store nothing was painting, while the user sat in
   * Notes, the Vault, Files, Calendar or the Meme Builder. In classic mode BOTH could be open at once
   * — Home → Nostrverse never closed Home — so the app ran two firehoses to paint neither, and every
   * event off the parked one still cost a Store write, a count invalidation and a profile fetch.
   *
   * SCOPE IS THE WHOLE SAFETY ARGUMENT: this closes `subs.home`/`subs.global` and nothing else.
   * Notifications, DMs/gift wraps, calls, the NIP-46 signer, Notes, the vault, folder sync and every
   * other private document are their own subscriptions with their own `#p`/`authors`/`#d` filters, so
   * a like or a reply still arrives, still badges and still toasts while you are somewhere else.
   *
   * The desktop is why this cannot key on VIEW alone. There a timeline lives in its own WINDOW and is
   * still on screen while VIEW names whatever took focus — the same distinction `_flushLiveFor`
   * makes, asked the same way (`_parkedSlot`), so the two cannot disagree about what "on screen"
   * means. Parked is not hidden.
   *
   * Re-entering the view re-subscribes through renderTimeline's own `Relay.close(subs[view])` +
   * fullSub() path, which is what already ran on every return, so nothing new can go wrong there. */
  function _parkOffscreenTimelines(){
    for(const v of _TL_SUB_VIEWS){
      const id = subs[v];
      if(!id || S.VIEW===v) continue;
      if(_visibleSlot(v)) continue;
      try{ Relay.close(id); }catch(_){}
      subs[v] = null;
    }
  }
  /* A MINIMISED window is not parked, it is PUT AWAY — and `_parkedSlot` cannot tell you which.
   *
   * os.js uses one mechanism for both: an unfocused window and a minimised one are each `parked`
   * with their nodes moved into their own slot. That is right for the question `_flushLiveFor` asks
   * — "where do this view's live posts belong?" — because a minimised window's slot is still where
   * they belong, ready for the restore. It is the wrong question here. "Where do the posts go" and
   * "can anybody see them" are different, and only the second decides whether a firehose is worth
   * its radio: a minimised Social window held the whole Nostrverse open behind a taskbar button.
   *
   * The window element carries `.minimised` (os.js) and the slot lives inside it, so the DOM already
   * holds the answer and neither module needs a new contract for it. */
  function _visibleSlot(view){
    const slot = _parkedSlot(view);
    if(!slot) return null;
    try{ if(slot.closest('.osw.minimised')) return null; }catch(_){}
    return slot;
  }
  /* Is the timeline PARKED in an unfocused desktop-mode window?
   *
   * Desktop mode moves an unfocused window's DOM out of #feed into its own slot and hands the VIEW
   * global to whichever window took focus. So `VIEW` says 'profile' while the timeline is alive and
   * on screen in another window — and every painter keys on VIEW. `#tl-notes` existing while VIEW is
   * not a timeline view is exactly that state and nothing else. */
  function _tlParked(){
    return !!(window.PCOS && S.VIEW!=='home' && S.VIEW!=='global' && document.getElementById('tl-notes'));
  }
  function flushLive(){
    S._liveT=null;
    // Each timeline's batch is delivered on its own terms: the focused one paints into #feed, a
    // parked one into its own window. Draining them together through one destination is what mixed
    // the firehose into the following feed.
    for(const [view, b] of [..._liveBufs]){
      const evs = b.buf.splice(0);
      if(evs.length) _flushLiveFor(view, evs, b.fn);
    }
  }
  function _flushLiveFor(view, evs, fn){
    S._liveFn = fn;                     // _prependLive filters with this; set per batch, not per app
    if(S.VIEW!==view){
      /* KEEP them. The buffer has already been drained by the splice above, so returning here does
       * not defer these posts — it destroys them, and nothing ever backfills: markEosed only draws
       * on the FIRST EOSE, so refocusing the timeline showed exactly what it showed before. That is
       * "not showing new posts when other window is focused" in desktop mode.
       *
       * Parked, they are DRAWN into the window they belong to (see below). The pending list is not
       * a way to keep them here — it is read through a pill whose count is forced to zero on this
       * exact path, and emptied outright by the next _resetLive. */
      {
        /* PREPEND, and never stash. A parked window is ON SCREEN — it is parked because another
         * window took focus, not because it is hidden — and os.js MOVES its nodes into the slot
         * rather than copying them, so #tl-notes there is the real timeline with its real handlers.
         *
         * THE PENDING LIST IS NOT AN OPTION HERE, and that is the part that took two attempts.
         * `_updateNewPostsPill` computes its count as `(VIEW==='home'||VIEW==='global') ? … : 0`,
         * and `_tlParked()` is true only when VIEW is NEITHER — so every call from this path resolves
         * to zero and HIDES the pill. Posts routed there are invisible with no control to release
         * them, the feed stays frozen (the reported bug, intact), and refocusing runs `_resetLive()`
         * which empties `_livePending` outright, so they are destroyed rather than merely delayed.
         * The pill could not serve this window anyway: it floats over `.main` — the FOCUSED window —
         * and `_flushPending` prepends into `$('#feed')`, which belongs to whatever is in front, so
         * clicking it would pour timeline posts into a Profile or Post window.
         *
         * Mixing the two is worse than either: posts drawn here while older ones sat in the pending
         * list came out of order, because `_flushPending` later inserts its batch at `firstChild` —
         * above the newer posts already on screen.
         *
         * Reading position is safe without the pill: `_prependLive` measures scrollHeight before and
         * after and corrects scrollTop by the difference, so nothing moves under someone scrolled
         * down. That is what AUTO_NEW_POSTS protects against on the focused feed, and it is already
         * handled here — which is why this does not consult it. */
        const slot=_parkedSlot(view);
        if(slot && !S._tlMedia){
          _prependLive(evs, slot);
          /* Tell os.js where the window is now looking. It captures scrollTop ONCE, at park time,
           * and replays exactly that number on restore — so content inserted above the reading
           * position while parked leaves the saved offset pointing somewhere else, and refocusing
           * lands several posts away from where you were. _prependLive has already corrected the
           * slot's live scrollTop; this is what makes the restore read it. */
          try{ if(window.PCOS && PCOS.noteScroll) PCOS.noteScroll(slot); }catch(_){}
        }
        // _tlMedia (the media grid) does not take live prepends on any path — the grid would break.
        // New images appear on the next redraw, the same contract the focused timeline has.
      }
      return;
    }
    if(S.VIEW!=='home'&&S.VIEW!=='global') return;   // the focused view is not a timeline at all
    const feed=$('#feed'); if(!feed) return;
    if(S._tlMedia) return;   // media grid doesn't live-prepend (would break the grid) — new images show on redraw/re-entry
    // While the user is reading below the top, DON'T mutate the timeline under them (prepending +
    // hydrating link cards shifts content and is what made it "keep refreshing"). Stash the new
    // posts and surface them with a "↑ N new posts" pill; flush when they scroll back up / tap it.
    // …and with "auto-show new posts" off, hold them at ANY scroll position — including the top.
    if(!S.AUTO_NEW_POSTS || feed.scrollTop > _LIVE_READ_PX){
      for(const ev of evs) S._livePending.push(ev);
      if(S._livePending.length>300) S._livePending=S._livePending.slice(-300);
      _updateNewPostsPill(); return;
    }
    _prependLive(evs, feed);
  }
  // Notes live in #tl-notes, BELOW the inline composer + sticky tabs. Every live insert / scroll-back
  // append / trim must target that box — going straight at #feed would prepend new notes ABOVE the
  // composer and make the "cap at 200" scan (which walks direct children) match nothing at all.
  // Falls back to #feed for the views that render into it without a timeline header.
  function _tlNotes(feed){ return (feed && feed.querySelector('#tl-notes')) || feed; }
  /* A PIXEL IS NOT A PLACE IN A CHANGING FEED. Remember the first visible keyed card and its
   * distance from the viewport while reconciling; new cards inserted above it then cannot turn the
   * same scrollTop into a completely different post after resume/reconnect. */
  function _tlAnchor(feed){
    // At the composer/top, keep the viewport there when a new post is inserted.
    // Anchoring the first card below the composer instead scrolls down by the new card height.
    if(!feed || feed.scrollTop <= 2) return null;
    const edge=feed.getBoundingClientRect().top;
    for(const el of [..._tlNotes(feed).children]){
      if(!(el.dataset && el.dataset.key)) continue;
      const r=el.getBoundingClientRect();
      if(r.bottom > edge) return { key:el.dataset.key, dy:r.top-edge };
    }
    return null;
  }
  function _restoreTlAnchor(feed, place){
    if(!feed || !place) return false;
    const el=[..._tlNotes(feed).children].find(n=>n.dataset && n.dataset.key===place.key);
    if(!el) return false;
    feed.scrollTop += el.getBoundingClientRect().top-feed.getBoundingClientRect().top-place.dy;
    return true;
  }
  /* Restore a history entry by POST, not by pixel. The timeline may initially contain skeletons or
   * may still be rebuilding from Store when popstate fires, so wait for the keyed card just as the
   * pixel fallback waits for enough scroll height. */
  function _putAnchor(place, ok, budget){
    if(!place || !place.key) return;
    let tries=0;
    const put=()=>{
      if(++tries > (budget||40) || (ok && !ok())) return;
      let feed=null;
      try{ feed=$('#feed'); }catch(_){ }
      if(feed && _restoreTlAnchor(feed, place)) return;
      setTimeout(put,25);
    };
    put();
  }
  // Scroll-back has no ceiling: every page appends another 30-60 cards, each holding full-resolution
  // <img>/<video> the device must keep decoded and (for video) a live media player. _prependLive's
  // 200-card cap deliberately stops applying the moment you paginate — it would delete the history you
  // just paged in — so a long scroll grew the DOM until something gave. In a browser that is a slow tab;
  // in the Android app the WebView's RENDER process is what runs out of memory, and Android kills it
  // (and, before MainActivity.surviveRenderProcessDeath, the whole app) with no error at all. That is the
  // "app keeps closing" report: no exception to find, because the JS never got to run.
  //
  // So bound it. Drop the oldest-rendered cards off the TOP and correct scrollTop by exactly the height
  // that went away, measured rather than summed (offsetHeight misses margins, and a few px of drift per
  // page is a visible jump on a phone) — the post being read does not move. Scrolling back above the cap
  // reaches the top of the trimmed list; pull-to-refresh redraws it from the Store, which still has it.
  function _capFeedDom(feed, box){
    if(!feed || !box) return;
    const cards=[...box.children].filter(el=>el.classList.contains('note')||el.classList.contains('reply-pair'));
    const excess=cards.length-_FEED_MAX_CARDS; if(excess<=0) return;
    const beforeH=feed.scrollHeight, top=feed.scrollTop;
    for(let i=0;i<excess;i++) cards[i].remove();
    feed.scrollTop=Math.max(0, top-(beforeH-feed.scrollHeight));
  }
  // The identity of a timeline CARD. A repost renders the ORIGINAL's content and carries the original's
  // data-id, so two reposts of one note are one card — which is why dedupe and reconcile both key on
  // this rather than on ev.id.
  function _noteKey(ev){ return ev.kind===6 ? ((ev.tags.find(t=>t[0]==='e')||[])[1]||ev.id) : ev.id; }
  // Build ONE timeline card, stamped with its key. Every path that puts a card in the timeline goes
  // through here (first draw, EOSE reconcile, live prepend, scroll-back append) — _drawTimeline's
  // reconcile matches on data-key, so a card inserted without one would be treated as foreign and
  // destroyed + rebuilt on the next redraw, which is the flash the reconcile exists to remove.
  function _noteNode(ev){
    const d=document.createElement('div'); d.innerHTML=feedNoteHtml(ev);
    const node=d.firstElementChild; if(node) node.dataset.key=_noteKey(ev);
    return node;
  }
  function _prependLive(evs, feed){
    const box=_tlNotes(feed);
    const sp=feed.querySelector('.spinner'); if(sp)sp.remove(); const em=feed.querySelector('.empty'); if(em)em.remove();
    evs.sort((a,b)=>b.created_at-a.created_at);
    const frag=document.createDocumentFragment();
    for(const ev of evs){ if(isMutedView(ev))continue; if(S._liveFn&&!S._liveFn(ev))continue;
      const dispId = _noteKey(ev);
      if(feed.querySelector('.note[data-id="'+dispId+'"]')) continue;   // don't double-insert
      const node=_noteNode(ev); if(node){ node.classList.add('pc-rise'); frag.appendChild(node); } }
    if(!frag.childElementCount) return;
    const atTop=feed.scrollTop<100, beforeH=feed.scrollHeight;
    box.insertBefore(frag, box.firstChild);
    if(!atTop) feed.scrollTop += (feed.scrollHeight - beforeH);   // keep scroll stable on prepend (feed is the scroller)
    // cap the feed at 200 — but NOT once the user has paginated older posts, or we'd delete the
    // scroll-back history they just loaded as soon as a new live note arrives at the top.
    // Trim whole top-level cards off the BOTTOM (a reply is a .reply-pair wrapper, not a bare .note).
    // 200 on a fresh feed; once the user has paged older, the cap loosens to the general ceiling instead
    // of switching off entirely — deleting the scroll-back they just loaded is what the old exemption
    // existed to avoid, but "never trim again" left live prepends growing the DOM without any bound at
    // all, which is the other half of what kills the Android WebView (see _capFeedDom). Trimming here is
    // scroll-safe: flushLive only calls us within _LIVE_READ_PX of the top (or via the pill, which
    // scrolls to top first), so the cards coming off the bottom are never the ones being read.
    { const keep=S._tl.pages===0 ? 200 : _FEED_MAX_CARDS;
      const cards=[...box.children].filter(el=>el.classList.contains('note')||el.classList.contains('reply-pair'));
      for(let i=keep;i<cards.length;i++) cards[i].remove(); }
    _healGhostPairs(box);
    decorateProfiles(); hydrateLinkCards(feed); hydratePolls(feed);
  }
  // "new posts" pill — only on the live timelines; clicking it jumps to top and shows them
  function _newPostsPill(){
    const feed=$('#feed');
    // Desktop mode moves #feed into the focused app window.  Keeping this control on the shell's
    // original .main leaves it underneath the windows, so new posts are counted but the button is
    // invisible.  Follow the live feed into its window; in browser/mobile mode this remains .main.
    const host=(feed&&feed.closest('.osw-body'))||document.querySelector('.main')||document.body;
    let p=document.getElementById('new-posts-pill');
    if(!p){ p=document.createElement('button'); p.id='new-posts-pill'; p.className='new-posts-pill hidden';
      p.onclick=()=>{ const feed=$('#feed'); if(feed) feed.scrollTop=0; _flushPending(); };
    }
    if(p.parentNode!==host) host.appendChild(p);
    return p;
  }
  // The pill floats over .main (which doesn't scroll — #feed does), so its resting place has to clear
  // whatever sits at the TOP of the feed. The CSS constant only ever had to clear the topbar, because the
  // pill could only appear once you'd scrolled past _LIVE_READ_PX; with auto-show off it appears at
  // scrollTop 0 too, where the inline composer is — and a button parked over a textarea eats the clicks
  // meant for it. Measure the tab bar rather than guessing a height: it's directly under the composer at
  // the top and sticks to the feed's top edge once you scroll, so "just below it" is right in both states,
  // on desktop (no topbar) and mobile (topbar) alike. The measured distance is in ZOOMED pixels (display
  // scaling puts a `zoom` on <body> — 0.72 at 1400px wide) while `style.top` is read UNZOOMED, so the raw
  // rect delta lands the pill ~28% too high, back on the very bar it was meant to clear: divide by the
  // effective scale, recovered from rect-vs-offset width (offsetWidth is the unzoomed one) rather than by
  // parsing a zoom the breakpoints own.
  function _placePill(p){
    const main=p.parentElement, feed=$('#feed'), tabs=feed&&feed.querySelector('.tl-tabs');
    if(!main||!tabs) return;                       // no timeline header to measure → keep the CSS default
    const box=main.getBoundingClientRect(), z=(main.offsetWidth ? box.width/main.offsetWidth : 1) || 1;
    p.style.top = Math.max(0, (tabs.getBoundingClientRect().bottom - box.top + 10) / z) + 'px';
  }
  function _updateNewPostsPill(){
    const p=_newPostsPill();
    const n=(S.NEW_POSTS_PILL && (S.VIEW==='home'||S.VIEW==='global'))?S._livePending.length:0;
    if(n>0){ p.textContent='↑ '+n+' new post'+(n>1?'s':''); _placePill(p); p.classList.remove('hidden'); } else p.classList.add('hidden');
  }
  function _flushPending(){
    const feed=$('#feed'); if(!feed) return;
    const evs=S._livePending.splice(0); if(evs.length) _prependLive(evs, feed);
    _updateNewPostsPill();
  }
  function _resetLive(){ S._livePending=[]; _updateNewPostsPill(); }
  function _hidePill(){ const p=document.getElementById('new-posts-pill'); if(p) p.classList.add('hidden'); }
  // ditto.pub-style timeline header: an inline compose bar, then the Home ⇄ Nostrverse ⇄ Trending tab
  // switch. Home has no sidebar row any more, so these tabs are the ONLY way to reach it — they must
  // render on EVERY one of those views, and in the media-grid branch too, or toggling ▦ would strand the
  // user with no way back. ▦ is omitted on Trending: that view is an engagement RANKING drawn straight
  // from the relay, not the Store-backed feed the media grid re-slices.
  // Inline composer text, held OUTSIDE the DOM so it survives a tab switch (which resets #feed).
  let _tlCmpText='';
  let _tlAutoId=null;   // id of the inline composer's rolling auto-draft (src:'tl'); survives reload via the Draft itself
  // AI helpers, module-level so BOTH composers use one implementation. They were private to compose()'s
  // closure, which is why the timeline composer previously had to open the modal just to reach them.
  let _aiLastTags='';   // the exact hashtag block we last appended — only strip THIS on re-run
  async function _aiEnhance(ta, setSt){
    const m=(ta.value||'').match(/https?:\/\/[^\s]+/i);
    if(!m){ toast('paste a link into the post first'); return; }
    setSt('summarizing link…');
    try{
      const r=await fetch('/client/compose-from-url',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({url:m[0]})}).then(r=>r.json());
      if(r&&r.text){ ta.value=r.text; _aiLastTags=''; setSt(''); ta.dispatchEvent(new Event('input')); }
      else setSt('couldn’t summarize: '+((r&&r.error)||'no content'));
    }catch(_){ setSt('summarize failed'); }
  }
  // 🖼️ Framed card — the 🎨 background post with a border drawn round it. Shared by both composers (the
  // timeline's inline one and the modal), which keep their own armed-background state, so the caller
  // passes its state in and takes the new value back rather than this reaching for a global.
  async function _aiFramedCard(ta, setSt, o){
    const v=(ta.value||'').trim();
    if(!v){ toast('write something, or paste a link'); return; }
    // THE point of this option: paste a link, get the FULL AI summary laid out as a card. Summarize when
    // the box holds no real draft yet — nothing but a URL, OR just a short headline beside it (how a
    // SHARED article opens the composer: "Title\n\nlink"). Without this the title counted as "words", so
    // the summarizer was skipped and the card showed the bare headline instead of the AI description — the
    // "new post modal is just the link title" bug. A real written post (multi-line, or long) is left alone.
    const m=v.match(/https?:\/\/\S+/);
    const words=S._BG_WORDS(v);
    const looksLikeBareLinkOrTitle = !!m && (!words || (!words.includes('\n') && words.length < 200));
    if(looksLikeBareLinkOrTitle){
      setSt('summarizing link…');
      try{
        const r=await fetch('/client/compose-from-url',{method:'POST',headers:{'Content-Type':'application/json'},
          body:JSON.stringify({url:m[0]})}).then(r=>r.json());
        if(!r || !r.text){ setSt('couldn’t summarize: '+((r&&r.error)||'no content')); return; }
        ta.value=r.text; ta.dispatchEvent(new Event('input'));
      }catch(_){ setSt('summarize failed'); return; }
    } else if(!words){
      toast('write something, or paste a link'); return;   // no link and no words → nothing to draw
    }
    const on=!o.framed;
    o.set(on);
    if(!on){ setSt('framed card off'); return; }
    // A frame on nothing is nothing: the border is drawn onto the background image, so without a
    // background armed this silently does nothing at post time. Open the swatches and say so.
    if(!o.hasBg){ if(o.reveal) o.reveal(); setSt('🖼️ framed card on — now pick a background'); }
    else setSt('🖼️ framed card on');
  }
  async function _aiHashtags(ta, setSt){
    const body=(ta.value||'').trim();
    const hasImage=/(?:!\[|https?:\/\/\S+\.(?:png|jpe?g|gif|webp)\b|\/blossom\/|media\.)/i.test(ta.value||'');
    if(!body && !hasImage){ toast('write something first'); return; }
    setSt('finding hashtags…');
    try{
      const r=await fetch('/client/hashtags',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({text:body, has_image:hasImage})}).then(r=>r.json());
      if(r&&r.hashtags){
        let base=body;   // strip ONLY the block we appended last time, never the user's own trailing tags
        if(_aiLastTags && base.endsWith(_aiLastTags)) base=base.slice(0, base.length-_aiLastTags.length).replace(/\s+$/,'');
        ta.value = base + (base?'\n\n':'') + r.hashtags; _aiLastTags=r.hashtags;
        setSt(''); ta.dispatchEvent(new Event('input'));
      } else setSt('no hashtags: '+((r&&r.error)||'try again'));
    }catch(_){ setSt('hashtags failed'); }
  }
  // AI → 😀 Suggest emoji: the node reads the draft, picks up to 5 INSTANCE custom emoji (matching its
  // keywords against the emoji filenames server-side — the pack is thousands of names, far too many to
  // put in a prompt) and appends them. Re-running replaces only the block we appended last time, never
  // emoji the user typed. Shared by the timeline composer and the New post / Reply modal.
  async function _aiEmojiSuggest(ta, setSt){
    const body=(ta.value||'').trim();
    if(!body){ toast('write something first'); return; }
    setSt('picking emoji…');
    try{
      const r=await fetch('/client/emoji-suggest',{method:'POST',headers:{'Content-Type':'application/json'},
        body:JSON.stringify({text:body, limit:5})}).then(r=>r.json());
      const list=(r&&r.emojis)||[];
      if(!list.length){ setSt(r&&r.error ? ('no emoji: '+r.error) : 'nothing matched this post'); return; }
      const block=list.map(e=>':'+e.s+':').join(' ');
      const prev=ta.dataset.aiEmoji||'';
      let base=body;
      if(prev && base.endsWith(prev)) base=base.slice(0, base.length-prev.length).replace(/\s+$/,'');
      ta.value = base + (base?' ':'') + block; ta.dataset.aiEmoji=block;
      setSt(''); ta.dispatchEvent(new Event('input',{bubbles:true}));
      S.InstEmoji.load();   // warm the map so publish() can tag them without a round trip
    }catch(_){ setSt('emoji suggestion failed'); }
  }
  // AI → 🧹 Clean links: strip tracking parameters (utm_*, fbclid, si, …) out of every URL in the
  // draft and unwrap click-wrappers (google /url?q=, l.facebook.com/l.php?u=, Outlook safelinks).
  // It sits in the AI menu with the rest of the compose tools, but it runs ENTIRELY OFFLINE in
  // static/js/client/urlclean.js — the decision "is this parameter a tracker" is a published list,
  // not a judgement, and a model that guesses wrong rewrites a link into a 404. Shared by both
  // composers (timeline inline + the New post / Reply modal).
  function _cleanLinksIn(ta){
    if(!window.PCUrlClean) return null;
    const r = window.PCUrlClean.cleanText(ta.value || '');
    if(!r.count) return r;
    ta.value = r.text;
    ta.dispatchEvent(new Event('input', {bubbles:true}));
    return r;
  }
  function _cleanLinksCmd(ta, setSt){
    if(!window.PCUrlClean){ setSt('link cleaner not loaded'); return; }
    if(!/https?:\/\//i.test(ta.value||'')){ setSt('no links in this post'); return; }
    const r = _cleanLinksIn(ta);
    if(!r || !r.count){ setSt('🧹 no trackers found — links are already clean'); return; }
    setSt(`🧹 cleaned ${r.count} link${r.count===1?'':'s'}`);
    // Say WHAT changed, not just how many: this rewrites the thing you pasted, so the one case that
    // matters is being able to see it did the right thing (and Ctrl+Z the textarea if it didn't).
    // Elided in the middle — a cleaned URL is still often long enough to fill a phone's toast.
    const _short=u=> u.length<=64 ? u : (u.slice(0,40)+'…'+u.slice(-20));
    toast(r.count===1 ? ('🧹 '+_short(r.changes[0][1])) : `🧹 cleaned ${r.count} links`);
  }
  // Auto-clean at post time, when the user has turned it on in Settings. Runs on the TEXTAREA (not
  // on the built event) so the cleaned text is what you see, and so imeta/mention tags are derived
  // from the URLs that actually get published — computing them first would leave imeta pointing at
  // a URL no longer in the content.
  function _autoCleanOnPost(ta){
    if(!ClientSettings.get('cleanLinks', false)) return 0;
    const r = _cleanLinksIn(ta);
    return (r && r.count) || 0;
  }
  // Guests get this where the composer would be: whose instance this is, and the two things they might
  // want to do about it. It sits ABOVE the timeline rather than in a corner because that is the moment
  // someone decides whether to join — the old bottom bar said "log in to interact" without ever saying
  // what this place IS, or offering signup as its own step.
  const SOURCE_URL = 'https://github.com/loblawbob873-svg/posterchanai';
  function _guestCardHtml(){
    // A custom site_name wins; the stock one becomes the product's full name rather than the bare
    // "PosterChan" the setting defaults to.
    const nm = (S.CFG && S.CFG.name || '').trim();
    const name = (nm && nm !== 'PosterChan') ? nm : 'PosterChan AI';
    const src = (S.CFG && S.CFG.source_url) || SOURCE_URL;   // older backends don't send it → constant
    return `<div class="guest-card" id="guest-card">
      <img class="guest-logo" src="${enc((S.CFG && S.CFG.logo_url) || S.LOGO)}" onerror="this.src='${S.LOGO}'" alt="">
      <div class="guest-name">${enc(name)}</div>
      <div class="guest-acts">
        ${_registrationOpen() ? '<button class="btn btn-neon" id="guest-signup">Sign up</button>' : ''}
        <button class="btn btn-cyan" id="guest-login2">Log in</button>
        <a class="btn guest-src" href="${enc(src)}" target="_blank" rel="noopener noreferrer">Source</a>
      </div>
    </div>`;
  }
  function _timelineHeaderHtml(){
    const canPost = !!(S.ME && !S.GUEST);
    const av=(Store.profile(S.ME&&S.ME.pubkey)||{}).picture||S.LOGO;
    return (canPost?`<div class="tl-cmp" id="tl-cmp" role="form" aria-label="Write a post">
        <img class="tl-cmp-av" src="${enc(av)}" onerror="this.src='${S.LOGO}'" alt="">
        <div class="tl-cmp-body">
          <textarea class="tl-cmp-ta" id="tl-cmp-ta" rows="1" placeholder="How was your weekend?"></textarea>
          <div class="tl-cmp-tools">
            <button class="tl-cmp-btn" id="tl-cmp-attach" title="Attach an image or file"><svg class="ic b-ic" aria-hidden="true"><use href="#i-paperclip"></use></svg></button>
            <button class="tl-cmp-btn" id="tl-cmp-react" title="Emoji / GIF"><svg class="ic b-ic" aria-hidden="true"><use href="#i-smile"></use></svg></button>
            <button class="tl-cmp-btn" id="tl-cmp-poll" title="Poll"><svg class="ic b-ic" aria-hidden="true"><use href="#i-chart"></use></svg></button>
            <button class="tl-cmp-btn" id="tl-cmp-ai" title="AI tools"><svg class="ic b-ic" aria-hidden="true"><use href="#i-ai"></use></svg></button>
            <!-- Background / Schedule / Sensitive live behind ⋯ deliberately. Five icons + Post is
                 exactly what fits one row at 360px (~269px of 279px); a sixth wraps Post onto its
                 own line, which is the regression the .tl-cmp-tools comment already records. -->
            <button class="tl-cmp-btn" id="tl-cmp-more" title="More — background, schedule, sensitive">⋯</button>
            <span class="muted small tl-cmp-status" id="tl-cmp-status"></span>
            <button class="btn btn-neon tl-cmp-post" id="tl-cmp-post">Post</button>
            <input type="file" id="tl-cmp-file" multiple hidden>
          </div>
          <div class="tl-cmp-cw hidden" id="tl-cmp-cwrow"><input class="input" id="tl-cmp-cwreason" maxlength="120" placeholder="🔞 sensitive — reason (optional)"></div>
          <div class="cmp-bg-strip hidden" id="tl-cmp-bgs" aria-label="post background"></div>
          <div class="cmp-cardprev hidden" id="tl-cmp-cardprev" aria-label="card preview"></div>
          <div class="cmp-sched-row hidden" id="tl-cmp-schedrow"><span class="muted small">Publish at</span><input type="datetime-local" id="tl-cmp-sched-at" class="input"><span class="sched-chips"><button type="button" class="sched-chip" data-min="10">+10m</button><button type="button" class="sched-chip" data-min="60">+1h</button><button type="button" class="sched-chip" data-min="1440">+1d</button></span><button class="btn btn-neon small" id="tl-cmp-sched-go">Schedule</button><span class="muted small sched-when" id="tl-cmp-sched-when"></span></div>
          <div class="tl-cmp-poll hidden" id="tl-cmp-pollbox">
            <div class="muted small">Poll options</div>
            <div id="tl-cmp-poll-opts"><input class="input poll-opt-in" placeholder="Option 1"><input class="input poll-opt-in" placeholder="Option 2"></div>
            <div class="row"><button class="btn btn-ghost small" id="tl-cmp-poll-add"><svg class="ic b-ic" aria-hidden="true"><use href="#i-plus"></use></svg>Add option</button>
              <label class="muted small" style="margin-left:auto"><input type="checkbox" id="tl-cmp-poll-multi"> Allow multiple</label></div>
          </div>
        </div>
      </div>`:_guestCardHtml())
      +`<div class="tl-tabs" role="tablist">
        ${(() => { const off = tlHiddenSet(); return [['home','Home'],['global','Nostrverse'],['trending','Trending']]
          .filter(([t]) => !off.has(t) || S.VIEW === t)   // the tab you are ON always shows, or the row lies about where you are
          .map(([t,l]) => `<button class="tltab${S.VIEW===t?' on':''}" data-tl="${t}" role="tab" aria-selected="${S.VIEW===t}">${l}</button>`).join(''); })()}
        ${S.VIEW==='trending' ? '' : `<button class="tltab-media${S._tlMedia?' on':''}" id="tl-media-tab" title="Toggle media grid (this feed, images only)" aria-label="Toggle media grid"><svg class="ic x-ic" aria-hidden="true"><use href="#i-grid"></use></svg></button>`}
      </div>`;
  }
  function _bindTimelineHeader(feed){
    $$('.tltab',feed).forEach(b=> b.onclick=()=>{ const v=b.dataset.tl; if(v!==S.VIEW) switchView(v); });
    // Guest card: Sign up goes STRAIGHT to the signup pane. Landing on the login form and having to
    // find "create an account" is the step that loses people who have no key yet.
    { const gs=$('#guest-signup',feed); if(gs) gs.onclick=()=>_leaveGuest('signup');
      const gl=$('#guest-login2',feed); if(gl) gl.onclick=()=>_leaveGuest('login'); }
    // ▦ media grid — lives here rather than the topbar because it only ever applied to these two views.
    // Toggling redraws just #tl-notes, so flip our own state class directly (the header isn't re-rendered).
    { const mb=$('#tl-media-tab',feed); if(mb) mb.onclick=()=>{ S._tlMedia=!S._tlMedia; ClientSettings.set('tlMedia', S._tlMedia);
        mb.classList.toggle('on', S._tlMedia); _drawTimeline(false); }; }
    const box=$('#tl-cmp',feed); if(!box) return;
    const ta=$('#tl-cmp-ta',box), st=$('#tl-cmp-status',box), post=$('#tl-cmp-post',box);
    /* ON A PHONE THE TOOL ROW EARNS ITS SPACE ONLY ONCE YOU'RE TYPING. Six buttons and Post sit at
     * the very top of the feed on every visit, paid for in posts-above-the-fold; until the box is
     * engaged they are decoration. `.cmp-open` gates a mobile-only CSS collapse — focus opens it,
     * blur closes it ONLY when the box is empty (a half-typed post must never lose its Post
     * button), and a restored auto-draft opens it on arrival for the same reason. */
    { const cmp=$('#tl-cmp',box)||box.closest&&box.closest('.tl-cmp')||$('#tl-cmp');
      if(cmp && ta){
        /* COLLAPSING MUST CLEAR THE INLINE HEIGHT. grow() writes `style.height` with a 54px floor,
         * and an inline style beats any CSS collapse — reported precisely: "you're just hiding
         * them, the feeds didn't move up". Collapsed, the inline height goes and the stylesheet's
         * compact row wins; opened, the box re-grows to its text. */
        const openState=()=>{
          const on = document.activeElement===ta || !!(ta.value&&ta.value.trim());
          cmp.classList.toggle('cmp-open', on);
          if(!on) ta.style.height='';
          else{ try{ ta.style.height='auto'; ta.style.height=Math.min(Math.max(ta.scrollHeight,54),560)+'px'; }catch(_){} }
        };
        ta.addEventListener('focus', ()=>{ cmp.classList.add('cmp-open'); });
        ta.addEventListener('blur', ()=>setTimeout(openState, 150));   // let a tool-button tap land first
        ta.addEventListener('input', openState);
        setTimeout(openState, 0);                                       // the restored draft case
      } }
    attachMentionAutocomplete(ta);
    // Auto-draft parity with the modal composer: the inline box used to keep text only in a module var
    // (_tlCmpText), lost on reload/crash. Persist what you type as ONE rolling Draft (marked src:'tl' so
    // mount can find it again), debounced; dropped once the post/schedule lands (in reset()).
    const _tlCw=()=>{ const r=$('#tl-cmp-cwrow',box), rr=$('#tl-cmp-cwreason',box);
      return (r && !r.classList.contains('hidden')) ? {cw:true, cwReason:((rr&&rr.value)||'').trim()} : {}; };
    const _tlSaveDraft=()=>{ const t=(ta.value||'').trim(); if(!t) return;
      try{ _tlAutoId = S.Drafts.save({id:_tlAutoId, text:t, src:'tl', ..._tlCw()}) || _tlAutoId; }catch(_){} };
    const _tlDropDraft=()=>{ clearTimeout(_tlAutoT); if(_tlAutoId){ try{ S.Drafts.remove(_tlAutoId); }catch(_){} _tlAutoId=null; } };
    let _tlAutoT=null;
    const _tlAutosave=()=>{ clearTimeout(_tlAutoT); _tlAutoT=setTimeout(()=>{ if((ta.value||'').trim()) _tlSaveDraft(); else _tlDropDraft(); }, 1200); };
    // Always open, like ditto — a one-line box that only unfolds on focus read as a search field and hid
    // the attach/post controls behind an extra interaction. It just grows with the text from here.
    // Grow to fit, up to a share of the VIEWPORT rather than a flat 320px — on a desktop that cap wasted
    // most of the window, on a phone it is still the floor. Past the cap the box scrolls (its bar is hidden
    // in CSS); it used to just clip, so a long post became invisible and unscrollable.
    const _taMax=()=>Math.min(560, Math.max(320, Math.round((window.innerHeight||800)*0.42)));
    const grow=()=>{ ta.style.height='auto'; ta.style.height=Math.min(Math.max(ta.scrollHeight,54),_taMax())+'px'; };
    // Declared before reset() so it can disarm the 🎨 choice: clearing only the button's .on class
    // would leave a background armed and silently style the NEXT post too.
    let _tlBg=null, _tlBgClear=()=>{}, _tlBgPreview=()=>{};
    let _tlBgFramed=false;   // 🖼️ Framed card (AI menu) — a border on the 🎨 background post
    // Clear the PANELS too, not just the text — a posted poll leaving its options behind would silently
    // attach them to the next thing you wrote.
    const reset=()=>{ _tlCmpText=''; ta.value=''; st.textContent=''; _aiLastTags=''; _tlDropDraft();
      const pb=$('#tl-cmp-pollbox',box), cw=$('#tl-cmp-cwrow',box);
      if(pb){ pb.classList.add('hidden');
        const w=$('#tl-cmp-poll-opts',box);
        if(w) w.innerHTML='<input class="input poll-opt-in" placeholder="Option 1"><input class="input poll-opt-in" placeholder="Option 2">';
        const mu=$('#tl-cmp-poll-multi',box); if(mu) mu.checked=false; }
      if(cw){ cw.classList.add('hidden'); const r=$('#tl-cmp-cwreason',box); if(r) r.value=''; }
      _tlBgClear(); _tlBgFramed=false;
      const bgs=$('#tl-cmp-bgs',box); if(bgs) bgs.classList.add('hidden');
      $$('.tl-cmp-btn.on',box).forEach(b=>b.classList.remove('on'));
      grow(); };
    if(_tlCmpText) ta.value=_tlCmpText;   // restore text carried across a tab switch
    else { try{ const d=S.Drafts.all().filter(x=>x && x.src==='tl' && !x.del && (x.text||'').trim()).sort((a,b)=>(b.ts||0)-(a.ts||0))[0];
      if(d){ ta.value=d.text; _tlCmpText=d.text; _tlAutoId=d.id; } }catch(_){} }   // restore the rolling inline draft across a reload
    grow();
    ta.addEventListener('input', ()=>{ _tlCmpText=ta.value; grow(); _tlAutosave(); _tlBgPreview(); });
    ta.addEventListener('keydown', e=>{ if((e.ctrlKey||e.metaKey) && e.key==='Enter'){ e.preventDefault(); post.click(); } });
    const upload=async files=>{ files=(files||[]).filter(Boolean); if(!files.length) return;
      for(let i=0;i<files.length;i++){ st.textContent=`uploading ${i+1}/${files.length}…`;
        try{ const url=await uploadBlob(files[i], {folder:'Posts'}); ta.value+=(ta.value?'\n':'')+url; _tlCmpText=ta.value; grow(); _tlAutosave(); }
        catch(err){ if(_blossomDenied(err)){ requestBlossomAccess(); st.textContent='🔒 No upload access — requested it from the admin.'; }
          else st.textContent='upload failed: '+((err&&err.message)||err); return; } }
      st.textContent=''; };
    // 📎 offers the SAME choices as the full New Post modal — Camera (app only) / Local / Blossom.
    // The inline composer went straight to the file dialog, so the only way to attach something you
    // had ALREADY uploaded was to open the full modal instead.
    $('#tl-cmp-attach',box).onclick=()=>{
      const opts = window.Capacitor ? [['camera','📷 Camera'],['local','🖼️ Photos / files'],['blossom','📁 Files']]
                                    : [['local','💻 Local'],['blossom','📁 Files']];
      /* THE TIMELINE COMPOSER IS A SECOND ATTACH MENU, and it is the one people actually use — the
       * modal is what you get from the New Post button. A feature added to one of them and not the
       * other is invisible to almost everybody, which is exactly how this shipped the first time
       * ("i don't see mini app"). If a third composer ever appears, it needs this line too. */
      if(window.PCWebxdc && PCWebxdc.attach) opts.push(['webxdc','🎮 Mini app (.xdc)']);
      openMenuPopover($('#tl-cmp-attach',box), opts, a=>{
        if(a==='camera') _captureCamera(ta, box);
        else if(a==='local') $('#tl-cmp-file',box).click();
        else if(a==='blossom') blossomPicker(ta);
        else if(a==='webxdc') PCWebxdc.attach(ta);
      });
    };
    $('#tl-cmp-file',box).onchange=async e=>{ await upload([...e.target.files]); e.target.value=''; };
    ta.addEventListener('paste', e=>{ const f=[...((e.clipboardData&&e.clipboardData.files)||[])]; if(f.length){ e.preventDefault(); upload(f); } });
    // 😀 works fully INLINE — openEmojiPopover/_insertAt/gifPicker are module-level, so there's nothing to
    // duplicate and no reason to send you to a modal just to add an emoji.
    attachEmojiAutocomplete(ta);   // type `:wave` → suggestions (same InstEmoji as the 😀 picker)
    { const rb=$('#tl-cmp-react',box); if(rb) rb.onclick=e=>{ e.stopPropagation();
        const items=[['emoji','😀 Emoji']]; if(S.CFG.gif_enabled) items.push(['gif','🎬 GIF']);
        openMenuPopover(rb, items, a=>{
          if(a==='emoji') openEmojiPopover(rb, em=>{ _insertAt(ta, em); _tlCmpText=ta.value; grow(); _tlAutosave(); });
          else if(a==='gif') gifPicker(ta); }); }; }
    // 📊 poll builder — inline. Its options are read at publish time, so there's no state to sync.
    const pollBox=$('#tl-cmp-pollbox',box), cwRow=$('#tl-cmp-cwrow',box);
    { const pb=$('#tl-cmp-poll',box);
      if(pb) pb.onclick=()=>{ const on=pollBox.classList.toggle('hidden')===false; pb.classList.toggle('on',on);
        if(on){ const f=$('.poll-opt-in',pollBox); if(f) f.focus(); } };
      const add=$('#tl-cmp-poll-add',box);
      if(add) add.onclick=()=>{ const wrap=$('#tl-cmp-poll-opts',box); const i=document.createElement('input');
        i.className='input poll-opt-in'; i.placeholder='Option '+(wrap.children.length+1); wrap.appendChild(i); i.focus(); }; }
    // 🔞 content warning (NIP-36) — toggled from the ⋯ menu.
    const toggleCw=()=>{ const on=cwRow.classList.toggle('hidden')===false;
      if(on){ const r=$('#tl-cmp-cwreason',box); if(r) r.focus(); } };
    // 🤖 AI — inline, using the module-level helpers (they used to be private to compose()).
    { const ab=$('#tl-cmp-ai',box); if(ab) ab.onclick=e=>{ e.stopPropagation();
        // The label carries the state: this is a toggle in a menu that closes on pick, so without the ✓
        // there is nothing anywhere telling you the next post will be framed.
        openMenuPopover(ab, [['enhance','✨ AI Enhancer'],['tags','# Hashtags'],['emoji','😀 Suggest emoji'],['translate','🌐 Translate'],
                             ['card', (_tlBgFramed?'🖼️ Framed card ✓':'🖼️ Framed card')]], a=>{
          const setSt=m=>{ st.textContent=m; };
          if(a==='enhance') _aiEnhance(ta, setSt);
          else if(a==='tags') _aiHashtags(ta, setSt);
          else if(a==='emoji') _aiEmojiSuggest(ta, setSt);
          else if(a==='card') _aiFramedCard(ta, setSt, {
            framed: _tlBgFramed, hasBg: !!_tlBg, set: v=>{ _tlBgFramed=v; _tlBgPreview(); },
            reveal: ()=>{ if(bgsRow && bgsRow.classList.contains('hidden')) toggleBg(); } });
          else if(a==='translate') composeTranslate(ta, ab); }); }; }
    // 🎨 Background post — same swatches and renderer the modal uses (CMP_BGS/renderBgPost are
    // module-level), so the two composers can't drift. Short text only; picking one is exclusive.
    const bgsRow=$('#tl-cmp-bgs',box);
    let toggleBg=()=>{};
    {
      if(bgsRow){
        const none=document.createElement('button'); none.type='button'; none.className='cmp-swatch cmp-swatch-none on';
        none.title='no background'; none.textContent='✕'; bgsRow.appendChild(none);
        const marks=[none];
        // Show the REAL card, not a card-styled textarea (see makeCardPreview): the box holds your draft,
        // the thumbnail holds what will actually be posted.
        const _tlCardPrev=makeCardPreview($('#tl-cmp-cardprev',box));
        _tlBgPreview=()=>_tlCardPrev(ta.value, _tlBg, _tlBgFramed);
        const pick=(el,bg)=>{ _tlBg=bg; marks.forEach(m=>m.classList.toggle('on', m===el));
          if(!bg) _tlBgFramed=false;   // same as the modal: ✕ drops the frame too
          _tlBgPreview(); };
        none.onclick=()=>pick(none,null);
        _tlBgClear=()=>pick(none,null);   // reset() disarms the background after a post
        // The only thing a card cannot be made from is NO WORDS (a bare link). Length and links are no
        // longer refusals: buildBgPost puts the hook on the card and everything else — the rest of the
        // text, the source link — underneath it, where a URL is still clickable.
        const _bgWhyNot=()=>S._BG_WORDS(ta.value) ? ''
          : 'that is just a link — use 🤖 AI → 🖼️ Framed card to turn it into a card';
        S.CMP_BGS.forEach(bg=>{ const s=document.createElement('button'); s.type='button'; s.className='cmp-swatch';
          s.title=bg.id; s.style.background=_bgCss(bg);
          if(bg.deco) s.textContent=bg.deco[0];   // holiday swatches are recognisable, as in the modal
          bgsRow.appendChild(s); marks.push(s);
          s.onclick=()=>{ const why=_bgWhyNot(); if(why){ st.textContent=why; return; } pick(s,bg); }; });
        // Keep the thumbnail in step with what you are typing (the modal already did this) — otherwise the
        // preview shows the card as it was when you picked the swatch, not the one you are about to post.
        // …and drop it again if the words are all deleted, leaving nothing to draw.
        ta.addEventListener('input', ()=>{ _tlBgPreview();
          if(_tlBg && _bgWhyNot()){ pick(none,null);
          st.textContent='background removed — nothing left to put on the card';
          setTimeout(()=>{ if(st.textContent.startsWith('background removed')) st.textContent=''; }, 2800); } });
        toggleBg=()=>{ const show=bgsRow.classList.toggle('hidden')===false;
          if(!show && _tlBg) pick(none,null);   // collapsing the row must not leave a hidden background armed
        };
      } }
    // One builder for Post / 🎨 / ⏰ so a scheduled or styled post is byte-identical to an immediate one.
    // Returns null (after setting a status message) when the input isn't publishable.
    const buildEvent=async()=>{
      // Strip trackers BEFORE anything reads the text — imeta/mention tags are derived from the URLs
      // in it, so cleaning afterwards would leave them pointing at a URL that is no longer there.
      { const n=_autoCleanOnPost(ta); if(n) toast(`🧹 cleaned ${n} link${n===1?'':'s'}`); }
      const text=ta.value.trim(); if(!text){ st.textContent='write something first'; return null; }
      const isPoll=!pollBox.classList.contains('hidden');
      const tags=[];
      if(isPoll){
        const labels=$$('.poll-opt-in',pollBox).map(i=>i.value.trim()).filter(Boolean);
        if(labels.length<2){ st.textContent='add at least 2 poll options'; return null; }
        tags.push(['polltype', $('#tl-cmp-poll-multi',box).checked?'multiplechoice':'singlechoice']);
        labels.forEach((l,i)=>tags.push(['option','opt'+(i+1), l]));
      }
      const cwOf=t=>{ if(!cwRow.classList.contains('hidden')) t.push(['content-warning', ($('#tl-cmp-cwreason',box).value||'').trim()]); };
      // 🎨 renders the text to an image and posts THAT, so the styled text survives in every client.
      // A poll can't also be a background (its question must stay readable text), so poll wins.
      if(_tlBg && !isPoll){
        if(!S._BG_WORDS(text)){ st.textContent='nothing to put on the card — write something, or use 🤖 AI → 🖼️ Framed card to summarize the link'; return null; }
        st.textContent='rendering…';
        const built=await buildBgPost(text, _tlBg, _tlBgFramed);
        st.textContent='uploading…';
        if(built.trimmed) toast('card shows the opening — the rest did not fit on it');
        // imeta from the CONTENT, not the bare image URL: the note may now also carry the source link,
        // which has no _MEDIA_META entry and is correctly skipped.
        const btags=[]; imetaTagsFor(built.content).forEach(t=>btags.push(t)); cwOf(btags);
        mentionTags(built.content).forEach(t=>{ if(!btags.some(x=>x[0]==='p'&&x[1]===t[1])) btags.push(t); });
        return {kind:1, content:built.content, tags:btags, what:_tlBgFramed?'posted 🖼️':'posted 🎨'};
      }
      mentionTags(text).forEach(t=>{ if(!tags.some(x=>x[0]==='p'&&x[1]===t[1])) tags.push(t); });
      imetaTagsFor(text).forEach(t=>tags.push(t));
      cwOf(tags);
      // A poll is kind 1068 (NIP-88); its question is the post text and the options are tags.
      return {kind:isPoll?1068:1, content:text, tags, what:isPoll?'poll posted':'posted'};
    };
    post.onclick=async()=>{
      if(!ta.value.trim()) return;
      post.disabled=true; st.textContent='posting…';
      try{
        const ev=await buildEvent();
        if(!ev){ post.disabled=false; return; }
        const r=await publish(ev.kind, ev.content, ev.tags);
        if(r && r.ok){ reset(); toast(ev.what); if(S.VIEW==='home'||S.VIEW==='global') renderView(true); return; }
        st.textContent='';   // publish() raises its own failure toast
      }catch(err){
        if(typeof _blossomDenied==='function' && _blossomDenied(err)){ requestBlossomAccess(); st.textContent='🔒 No upload access — requested it from the admin.'; }
        else st.textContent='post failed: '+((err&&err.message)||err);
      }
      post.disabled=false;
    };
    // ⏰ Schedule — signs the note with a FUTURE created_at; the backend broadcasts it at that time.
    let toggleSched=()=>{};
    { const srow=$('#tl-cmp-schedrow',box), sat=$('#tl-cmp-sched-at',box),
            sgo=$('#tl-cmp-sched-go',box), swhen=$('#tl-cmp-sched-when',box);
      if(srow && sat && sgo){
        // Live readout so the time you picked is unambiguous rather than a silent default.
        const updWhen=()=>{ const ts=Math.floor(new Date(sat.value).getTime()/1000);
          if(!sat.value || isNaN(ts)){ swhen.textContent=''; return; }
          const mins=Math.round((ts-Date.now()/1000)/60);
          const rel = mins<1?'now' : mins<60?`in ${mins} min` : mins<1440?`in ${(mins/60).toFixed(mins%60?1:0)} h` : `in ${Math.round(mins/1440)} d`;
          swhen.textContent=`→ publishes ${new Date(ts*1000).toLocaleString()} (${rel})`; };
        sat.addEventListener('input', updWhen);
        $$('.sched-chip',srow).forEach(c=> c.onclick=()=>{ sat.value=_dtLocal(new Date(Date.now()+(+c.dataset.min)*60000)); updWhen(); });
        toggleSched=()=>{ const show=srow.classList.toggle('hidden')===false;
          if(show){ sat.min=_dtLocal(new Date(Date.now()+60*1000));
            if(!sat.value) sat.value=_dtLocal(new Date(Date.now()+10*60*1000)); updWhen(); sat.focus(); } };
        sgo.onclick=async()=>{
          const whenTs=Math.floor(new Date(sat.value).getTime()/1000);
          if(!sat.value || isNaN(whenTs)){ st.textContent='pick a date & time'; return; }
          if(whenTs < Math.floor(Date.now()/1000)+30){ st.textContent='pick a time at least a minute from now'; return; }
          sgo.disabled=true; st.textContent='scheduling…';
          try{
            const ev=await buildEvent();
            if(!ev){ sgo.disabled=false; return; }
            const r=await S.Scheduled.create(ev.kind, ev.content, ev.tags, whenTs);
            if(r && r.ok){ reset(); srow.classList.add('hidden'); sat.value=''; swhen.textContent='';
              toast('scheduled'); if(S.VIEW==='drafts') renderView(true); }
            else st.textContent='schedule failed'+((r&&r.error)?': '+r.error:'');
          }catch(err){ st.textContent='schedule failed: '+((err&&err.message)||err); }
          sgo.disabled=false;
        };
      } }
    // ⋯ overflow — keeps the icon row at five so Post stays on one line at 360px.
    // 🧹 Clean links lives HERE rather than in 🤖 AI (it does not use the model — see _cleanLinksCmd)
    // and rather than as a sixth icon, which is the exact regression the row's own comment records.
    { const mb=$('#tl-cmp-more',box); if(mb) mb.onclick=e=>{ e.stopPropagation();
        openMenuPopover(mb, [['bg','🎨 Background'],['sched','⏰ Schedule'],['cw','🔞 Sensitive'],
                             ['clean','🧹 Clean links']], a=>{
          if(a==='bg') toggleBg();
          else if(a==='sched') toggleSched();
          else if(a==='clean') _cleanLinksCmd(ta, m=>{ st.textContent=m; });
          else if(a==='cw') toggleCw(); }); }; }
  }
  // THE timeline predicate — who gets to appear in Home/Global. There used to be two copies of this,
  // one here and one in renderTimeline, and they drifted the moment anything was added: "hide replies"
  // shipped filtering only the live-prepend path, so replies vanished from the socket and stayed in the
  // drawn list. Same two-render-paths trap as the AI-chat media rows. One function, both callers.
  // Skeleton cards for the cold timeline. A spinner on a blank screen is the most dated loading pattern
  // there is, and it tells you nothing about what is coming; a skeleton shows the SHAPE of the content
  // immediately, which reads as faster at identical speed. The .skel shimmer utility already existed in
  // client.css and had never been used once — 53 bare spinners instead.
  //
  // Also fixes a real flash: with zero notes and no EOSE yet, _reconcileNotes showed "No posts yet",
  // i.e. the empty state was rendered while the feed was still loading. Loading and empty are different
  // states and now look different.
  function _skelNotes(n){
    let out = '';
    for(let i=0;i<n;i++){
      const w = 55 + ((i*37) % 40);          // vary the last line so the block doesn't read as a grid
      out += '<div class="skel-note" aria-hidden="true">'
           +   '<div class="skel skel-av"></div>'
           +   '<div class="skel-body">'
           +     '<div class="skel skel-line skel-name"></div>'
           +     '<div class="skel skel-line"></div>'
           +     '<div class="skel skel-line" style="width:' + w + '%"></div>'
           +   '</div>'
           + '</div>';
    }
    return out;
  }
  // TIMELINE ONLY. This is reached solely from _drawTimeline, which returns early unless the view is
  // home/global — deliberately, because hiding the fediverse from your feed must NOT hide fediverse
  // people from Notifications or Messages. Those are addressed to YOU: swallowing a bridged mention or
  // DM would read as the bridge being broken, and you would never know a reply had arrived.
  function _tlFilter(view){
    const hideR = ClientSettings.get('hideReplies', false);
    // NO "hide the fediverse" here any more. That switch (on by default) existed while the Pleroma
    // bridge mirrored whole remote timelines onto Nostr; with the native fediverse server those posts
    // are this node's own, so everybody -- a logged-out visitor on the main page included -- sees them.
    const follows = view==='home' ? (e=>S.FOLLOWS.has(e.pubkey)) : null;
    return ev => (!follows || follows(ev))
              && !(hideR && isReply(ev));
  }
  function _drawTimeline(preserveScroll){
    if(S.VIEW!=='home' && S.VIEW!=='global') return;
    const feed=$('#feed'); if(!feed) return;
    const top=preserveScroll?feed.scrollTop:0;
    const place=preserveScroll?_tlAnchor(feed):null;
    const fn = _tlFilter(S.VIEW);
    /* Once scroll-back has loaded older pages, a resume redraw must include them. Reconciling only
     * the newest 200 deleted the loaded tail, made posts appear missing, and left scrollTop pointing
     * at a newer card. The DOM remains bounded by the same 400-card ceiling used by pagination. */
    const notes = Store.feed(e=>fn(e)&&!isMutedView(e))
      .slice(0, S._tl.pages===0 ? 200 : _FEED_MAX_CARDS);
    // seed the scroll-back cursor from the initial draw only — once the user has paged older, a late
    // EOSE redraw must NOT move the cursor forward (it would re-query an already-loaded range)
    if(notes.length && S._tl.pages===0) S._tl.oldest = notes[notes.length-1].created_at;
    // The header (inline composer + tabs) is built ONCE and kept alive; only #tl-notes is re-rendered.
    // This runs on every EOSE and on a 350ms live-event debounce, so reassigning feed.innerHTML here would
    // wipe the composer — and whatever the user was midway through typing — several times a minute.
    let notesEl=$('#tl-notes',feed);
    if(!notesEl){
      // The ＋ is the LAST child of #feed and position:sticky. #feed is the real scroll container, so its
      // bottom edge is genuinely the visible bottom — unlike .main/.app, whose height is derived from the
      // body{zoom:.85} / calc(100dvh/.85) pair and ends up taller than the viewport in Firefox, which is
      // what pushed every fixed/absolute FAB off-screen.
      feed.innerHTML = _timelineHeaderHtml() + '<div id="tl-notes"></div>'
        + (S.ME && !S.GUEST ? '<button class="tl-fab" id="tl-fab" title="New post" aria-label="New post"><svg class="ic b-ic" aria-hidden="true"><use href="#i-plus"></use></svg></button>' : '');
      _bindTimelineHeader(feed);
      { const fb=$('#tl-fab',feed); if(fb) fb.onclick=()=>compose(); }
      notesEl=$('#tl-notes',feed);
    }
    if(S._tlMedia){
      // Media grid: the SAME feed (your follows / the nostrverse), image posts only, as a picture
      // grid — reuses Pics' _firstImage + .pics-grid/.pic-card styling. Scroll-back grows it (events
      // accumulate in Store); like the old Pics view it doesn't live-prepend (see flushLive).
      const pics=[]; const seen=new Set();
      for(const e of notes){ const img=_firstImage(e); if(!img||seen.has(e.id)) continue; seen.add(e.id); pics.push({e,img}); }
      notesEl.innerHTML = pics.length
        ? `<div class="pics-grid">${pics.map(x=>{ const cw=S.BLUR_NSFW && isSensitive(x.e);
            return `<div class="pic-card${cw?' cw':''}" data-id="${x.e.id}">${_hold(`<img src="${enc(x.img)}" loading="lazy" onerror="this.closest('.pic-card')&&this.closest('.pic-card').remove()">`, x.img)}${cw?'<span class="pic-cw">🔞</span>':''}</div>`; }).join('')}</div>`
        : `<div class="empty">No media in this feed yet. ${S.VIEW==='home'?'Follow people or check Nostrverse.':''}</div>`;
      $$('.pic-card',notesEl).forEach(c=> c.onclick=()=> openThread(c.dataset.id));
      if(preserveScroll) feed.scrollTop=top;
      return;
    }
    if(S._profObs) S._profObs.disconnect();   // drop observations on the notes we may replace (hydrate re-observes)
    // Nothing yet AND the relay has not finished answering: that is LOADING, not empty.
    if(!notes.length && !S._tl.eosed){ notesEl.innerHTML = _skelNotes(6); if(preserveScroll) feed.scrollTop=top; return; }
    _reconcileNotes(notesEl, notes, `No posts yet. ${S.VIEW==='home'?'Follow people or check Nostrverse.':''}`);
    // BEFORE hydrate, so a card this puts back is decorated and observed like every other one. The
    // reconcile reuses keyed cards, so nothing else in a redraw ever looks inside one again.
    _healGhostPairs(notesEl, true);
    hydrate(notesEl); if(preserveScroll && !_restoreTlAnchor(feed, place)) feed.scrollTop=top;
  }
  // Bring `box`'s cards in line with `notes` by KEY, reusing every card that is already there.
  //
  // This used to be `box.innerHTML = notes.map(feedNoteHtml).join('')`, and that one line is most of why
  // the timeline looked unstable. Entering home/global draws twice — once from the Store cache, then again
  // on EOSE (renderTimeline → _drawTimeline, then markEosed → _drawTimeline) — so every card the user was
  // already looking at was destroyed and rebuilt a moment later. Rebuilt <img>s go back to being unloaded,
  // so all the media re-reserves and re-decodes, `.note{animation:fade}` re-runs on all 200 cards at once
  // (the "flash"), and any expanded/quoted/poll state is lost. Reconciling instead means the second draw
  // is usually a no-op, and a draw that DOES have new posts touches only those.
  //
  // Deliberately not a virtual DOM: the list is keyed and monotonic (newest first), so one ordered walk
  // with a moving insertion point is O(n) and about fifteen lines.
  function _reconcileNotes(box, notes, emptyMsg){
    // Index what's already rendered. Anything unkeyed is scaffolding (.empty, .load-sentinel) or a card
    // from before this function existed — drop it so the walk below starts from a known state.
    const have=new Map();
    for(const el of [...box.children]){
      const k=el.dataset && el.dataset.key;
      if(k && !have.has(k)) have.set(k, el); else el.remove();
    }
    // One card per KEY: two reposts of the same note collapse to one (they render identical content),
    // and the walk below assumes it never revisits a key.
    const seen=new Set(), want=[];
    for(const ev of notes){ const k=_noteKey(ev); if(seen.has(k)) continue; seen.add(k); want.push([k,ev]); }
    if(!want.length){
      for(const el of have.values()) el.remove();
      box.innerHTML = `<div class="empty">${emptyMsg}</div>`;
      return;
    }
    // `ref` is the next already-correct child. Insert or move each wanted card in front of it; when the
    // card IS ref, the order already holds and ref advances. Untouched runs cost one comparison each.
    let ref=box.firstElementChild;
    for(const [k,ev] of want){
      const node=have.get(k);
      if(node===ref){ ref=ref.nextElementSibling; continue; }
      const el = node || _noteNode(ev);
      if(el) box.insertBefore(el, ref);
    }
    for(const [k,el] of have) if(!seen.has(k)) el.remove();
  }

  return {
    _aiEmojiSuggest, _aiFramedCard, _applyMediaCacheBudget, _autoCleanOnPost, _bindTimelineHeader,
    _capFeedDom, _cleanLinksCmd, _drawTimeline, _fillMediaCacheStat, _fillMusicOfflineStat, _flim,
    _flushPending, _hidePill, _noteKey, _noteNode, _parkOffscreenTimelines, _putAnchor,
    _restoreTlAnchor, _timelineHeaderHtml, _tlAnchor, _tlNotes, _updateNewPostsPill, isSensitive,
    renderTimeline,
  };
};

/* SCROLL REPORT — "scrolling down the timeline it keeps fighting and moving up", on an Android phone AND
 * tablet; "scrolling fast shows it easier", "scrolling slow has resistance". Every desktop reproduction
 * (touch emulation, live posts, reconnects, paging, slow images) scrolled cleanly, so this records what
 * the DEVICE does and Settings → Phone → "Copy scroll report" hands it over. Three things can move a
 * timeline under a finger, and each is logged with its cause:
 *   - CODE writing #feed.scrollTop / scrollTo / scrollBy (with the calling functions);
 *   - a card ABOVE the reading position changing height (the timeline turns scroll anchoring off, so
 *     nothing compensates) — with what changed: an image finishing, or a card laid out for the first
 *     time (content-visibility:auto measures cards as 420px until drawn);
 *   - the browser's own layout-shift entries.
 * Only recorded while a finger is down or within 1.5 s of lifting it, into small ring buffers: nothing
 * is sent anywhere, and the report holds no post text — keys and pixel numbers only. */
(function(){
  'use strict';
  if(typeof window === 'undefined' || window.PCScrollReport) return;
  const MAX = 60, rec = { writes:[], resizes:[], shifts:[], jumps:[], gestures:[], tasks:[], frames:[] };
  let touching = false, lastTouch = 0, lastY = null, dir = 0, feed = null, ro = null, mo = null;
  const live = () => touching || (Date.now() - lastTouch) < 1500;
  const push = (list, row) => { list.push(Object.assign({ t: Date.now() }, row)); if(list.length > MAX) list.shift(); };
  const callers = () => String((new Error()).stack || '').split('\n').slice(3, 7)
      .map(l => (l.match(/at\s+([\w$.<>]+)/) || [])[1] || (l.match(/^([\w$.<>]+)@/) || [])[1] || '?').join(' < ');
  function hookWrites(el){
    const d = Object.getOwnPropertyDescriptor(Element.prototype, 'scrollTop');
    if(!d || !d.set) return;
    try{
      Object.defineProperty(el, 'scrollTop', { configurable:true,
        get(){ return d.get.call(this); },
        set(v){ if(live()) push(rec.writes, { from: Math.round(d.get.call(this)), to: Math.round(v), by: callers() }); d.set.call(this, v); } });
      for(const fn of ['scrollTo', 'scrollBy']){
        const orig = el[fn];
        el[fn] = function(...a){ if(live()) push(rec.writes, { fn, args: JSON.stringify(a).slice(0, 60), by: callers() }); return orig.apply(this, a); };
      }
    }catch(_){ }
  }
  const heights = new WeakMap();
  function watchCards(){
    const box = feed && feed.querySelector('#tl-notes');
    if(!box || !window.ResizeObserver) return;
    if(!ro) ro = new ResizeObserver(entries => {
      if(!live()) return;
      const top = feed.getBoundingClientRect().top;
      for(const e of entries){
        const el = e.target, bb = e.borderBoxSize && (e.borderBoxSize[0] || e.borderBoxSize);
        const h = Math.round(bb && bb.blockSize != null ? bb.blockSize : el.offsetHeight), was = heights.get(el);   // the WHOLE box: padding and borders move the page too
        heights.set(el, h);
        if(was == null || was === h) continue;
        const r = el.getBoundingClientRect();
        if(r.bottom > top + 1) continue;                      // only cards ABOVE the reading position move it
        const imgs = [...el.querySelectorAll('img')].filter(i => !i.classList.contains('emoji-inline'));
        push(rec.resizes, { key: String(el.dataset.key || '').slice(0, 10), from: was, to: h, delta: h - was,
          imgs: imgs.length, imgsDone: imgs.filter(i => i.complete).length,
          cv: getComputedStyle(el).contentVisibility || '' });
      }
    });
    for(const el of box.children) if(!heights.has(el)){ heights.set(el, null); ro.observe(el, { box:'border-box' }); }
    if(!mo){ mo = new MutationObserver(() => watchCards()); mo.observe(box, { childList:true }); }
  }
  /* "RESISTANCE" IS TIME, NOT DISTANCE. A swipe feels stiff when the page answers it late: how long a touch
   * event waited before the page could run it (a busy main thread — and the timeline's own touchmove is
   * not passive, so the FIRST move of a swipe cannot scroll until it has run), a long task while the finger
   * is down, or a frame that took far longer than 16 ms. Recorded per swipe, in milliseconds. */
  let gesture = null, raf = 0;
  const wait = e => { const w = performance.now() - e.timeStamp; return w >= 0 && w < 60000 ? Math.round(w) : null; };
  function frames(){
    if(raf || !window.requestAnimationFrame) return;
    let prev = 0;
    const tick = now => {
      if(prev && now - prev >= 50) push(rec.frames, { ms: Math.round(now - prev) });
      prev = now;
      raf = live() ? requestAnimationFrame(tick) : 0;
    };
    raf = requestAnimationFrame(tick);
  }
  try{ new window.PerformanceObserver(list => { if(!live()) return;
      for(const e of list.getEntries()) push(rec.tasks, { ms: Math.round(e.duration) }); })
    .observe({ type:'longtask', buffered:false }); }catch(_){ }
  function attach(){
    const f = document.getElementById('feed');
    if(!f || f === feed) return;
    feed = f; hookWrites(f);
    let last = f.scrollTop;
    f.addEventListener('touchstart', e => { touching = true; lastY = e.touches[0] ? e.touches[0].clientY : null; watchCards();
      push(rec.gestures, { start: wait(e), firstMove: null, maxMove: 0, moves: 0, blocking: null }); gesture = rec.gestures[rec.gestures.length - 1]; frames(); }, { passive:true });
    f.addEventListener('touchmove', e => { const y = e.touches[0] ? e.touches[0].clientY : null;
      if(gesture){ const w = wait(e); gesture.moves++; if(gesture.firstMove == null){ gesture.firstMove = w; gesture.blocking = !!e.cancelable; }
        if(w > gesture.maxMove) gesture.maxMove = w; }
      if(y != null && lastY != null && Math.abs(y - lastY) > 2) dir = y < lastY ? 1 : -1;   // 1 = finger up = scrolling DOWN
      lastY = y; lastTouch = Date.now(); }, { passive:true });
    f.addEventListener('touchend', () => { touching = false; lastTouch = Date.now(); }, { passive:true });
    f.addEventListener('scroll', () => { const now = f.scrollTop, d = now - last; last = now;
      if(live() && dir === 1 && d < -40) push(rec.jumps, { from: Math.round(now - d), to: Math.round(now), delta: Math.round(d) }); }, { passive:true });
  }
  try{ new window.PerformanceObserver(list => { if(!live()) return;
      for(const e of list.getEntries()) push(rec.shifts, { value: +e.value.toFixed(4),
        nodes: (e.sources || []).slice(0, 3).map(s => { const n = s.node; const k = n && n.closest && n.closest('[data-key]');
          return (k ? 'card ' + String(k.dataset.key).slice(0, 10) : (n && (n.id || n.className || n.nodeName)) || '?') + ' ' +
            Math.round(s.previousRect.top) + '→' + Math.round(s.currentRect.top); }) }); })
    .observe({ type:'layout-shift', buffered:false }); }catch(_){ }
  document.addEventListener('touchstart', attach, { capture:true, passive:true });
  window.PCScrollReport = () => ({ v: 2, ua: navigator.userAgent, w: innerWidth, h: innerHeight, dpr: devicePixelRatio,
    zoom: getComputedStyle(document.body).zoom || '', desktop: document.body.classList.contains('os-on'),
    cards: feed && feed.querySelector('#tl-notes') ? feed.querySelector('#tl-notes').children.length : 0,
    writes: rec.writes, resizes: rec.resizes, shifts: rec.shifts, jumps: rec.jumps,
    gestures: rec.gestures, tasks: rec.tasks, frames: rec.frames });
})();
