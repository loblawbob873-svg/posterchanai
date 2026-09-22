/* Live streams (NIP-53) — Discover → Streams, the stream page and its chat, the popped-out and
 * mini players, recordings/replays, and Go Live (OBS, a phone camera or a phone screen over WHIP).
 * Split out of app.js.
 *
 * Loaded on first use: app.js keeps a small block of entry points (search for `_streamsDeps`) and
 * builds this factory the first time one is called — opening Streams or a stream, going live, or
 * the owner's deferred sweeps a few seconds after login. The teardown entry points
 * (cleanupInlineStream, closeMini, …) do nothing until the module exists, so leaving a screen never
 * fetches it. The code below is BYTE-IDENTICAL to what it replaced in app.js apart from its reads
 * of app.js's live `let` bindings, which the parser rewrote to `S.<name>` (getters/setters on
 * `dep.state`) at exact identifier offsets.
 *
 * Stayed in app.js: the card/status helpers the profile and search lists draw with (streamCard,
 * streamStatus, streamHost, STREAM_RELAYS, the deleted-stream set), the chat subscription handle
 * renderView reads, the once-per-session replay latch startApp reads, copyValue, and the fetch
 * helpers other screens use (_fetchTimeout, _streamFetch, loadHls).
 */
window.PCStreamsFactory = function(dep){
  const S = dep.state;   // live app.js bindings: S.CFG, S.GUEST, S.LOGO, S.ME, S.VIEW, S._aiToken, S._streamChatPoll, S._streamChatSub, S._sweptReplays, S.signer
  const {
    $, $$, BUNDLED, FilesIdx, NT, STREAM_RELAYS, _capPlugin, _clearNav, _copyFrom, _dedupAddr,
    _deletedStreams, _fetchIceServers, _fetchTimeout, _guestPrompt, _instanceBase,
    _isDeletedStream, _mediaErrMsg, _popKeys, _preferH264, _streamAddr, _streamFetch, _webLink,
    closeModal, copyValue, decorateProfiles, doTip, enc, ensureAiSession, isDesktop, linkify,
    loadHls, mediaServer, modal, needProfile, profOf, publish, renderProfileView,
    requestStreamAccess, streamCard, streamHost, streamStatus, subModal, switchView, toast,
    uiConfirm,
  } = dep;

  let _streamsReadOwner=null;
  function _stopStreamsReads(){const owner=_streamsReadOwner;_streamsReadOwner=null;if(owner)owner.abort();}
  function _beginStreamsReads(){_stopStreamsReads();_streamsReadOwner=typeof AbortController==='function'?new AbortController():null;return _streamsReadOwner&&_streamsReadOwner.signal;}
  /* Mirror a stream event to the PUBLIC stream relays.
   *
   * publish() only reaches OUR relay. That was fine for the recording backfill, which already
   * compensated here — and was missing from the three that matter most: go-live, the viewer-count
   * refresh, and END. So a broadcast existed on poster.place and NOWHERE else: zap.stream, shosho and
   * Amethyst could not discover it, nobody outside could open it, and therefore nobody could chat or
   * react to it. Reported as "live now, I see no chats or reacts" — and there genuinely were none,
   * because to every other client the stream did not exist.
   *
   * END matters just as much as go-live: an ended stream whose `ended` event never left our relay
   * stays ● LIVE forever on every client that saw the `live` one, pointing at a dead feed.
   *
   * Best-effort and never awaited by the caller — a slow external relay must not delay going live. */
  function _mirrorStream(r){
    try{ if(r && r.ok && r.ev) return Relay.publishTo(STREAM_RELAYS, r.ev).catch(()=>{}); }
    catch(_){ }
    return Promise.resolve();
  }
  async function renderStreams(){
    const feed=$('#feed');
    const readSignal=_beginStreamsReads();
    /* PAINT THE LOCAL STORE FIRST. Relay.query is network-only and a WebSocket whose peer never
     * answers can remain pending indefinitely. Awaiting it before the first paint left the entire
     * Streams window as a black spinner. Cached streams and the Go Live control need no network
     * round-trip, so make the screen usable immediately and merge the network answer afterwards. */
    let evs=[];
    try{ evs=(Store.query([{ kinds:[30311], limit:80 }])||[]).slice(); }catch(_){ evs=[]; }
    /* MERGE IN WHAT WE ALREADY HOLD. Relay.query is network-only, so this list was whatever the
     * relays answered in that instant — and the one event most certain to be missing from it is the
     * one YOU published a second ago. _goLive publishes, requires the relay to have stored it, then
     * switches straight here; the REQ that follows can still come back without it, and since this
     * view has no live subscription, nothing ever re-queries. The stream sat invisible until a full
     * browser reload, on the one screen whose entire job is to show that it is live.
     *
     * publish() already saved it locally, so the fix is to stop depending on the round-trip. A
     * cached copy that is genuinely stale loses anyway: _dedupAddr keeps the NEWEST per address, so
     * the relay's version wins whenever it has one. */
    evs.forEach(e=>{ Store.saveEvent(e); needProfile(e.pubkey); });
    if(S.VIEW!=='streams') return;
    const paint=()=>{
      if(S.VIEW!=='streams') return;
      const rank=e=>({live:0,planned:1,ended:2}[streamStatus(e)] ?? 3);
      const streams=_dedupAddr(evs.filter(e=>!_isDeletedStream(e))).sort((a,b)=> rank(a)-rank(b) || b.created_at-a.created_at);
      // async now (it probes the feed before adopting), so the rejection has to be caught on the
      // PROMISE as well as synchronously — a bare call would surface as an unhandled rejection.
      try{ const _a=_adoptOwnLive(streams); if(_a&&_a.catch) _a.catch(()=>{}); }catch(_){}
      // Catch up any of YOUR ended streams whose recording finished while this tab was closed, so the
      // replay becomes visible to other clients. Once per view open, never awaited, never fatal.
      if(!S._sweptReplays){ S._sweptReplays = true; try{ _sweepUnstampedReplays(); }catch(_){} }
      const top=`<div class="streams-top">${_liveStream
        ? `<span class="live-badge">● LIVE</span><span class="muted small" id="stream-viewers">👁 …</span><button class="btn btn-ghost small" id="stream-end"><svg class="ic b-ic" aria-hidden="true"><use href="#i-stop"></use></svg>End stream</button>`
        : (!S.GUEST ? `<button class="btn btn-neon small" id="stream-golive"><svg class="ic b-ic" aria-hidden="true"><use href="#i-live"></use></svg>Go Live</button>` : '')}</div>`;
      feed.innerHTML = top + (streams.length ? `<div class="stream-grid">${streams.map(streamCard).join('')}</div>` : '<div class="empty">No live streams right now.</div>');
      decorateProfiles();
      { const gl=$('#stream-golive',feed); if(gl) gl.onclick=_goLive; const ge=$('#stream-end',feed); if(ge) ge.onclick=_endLive; }
      $$('.stream-card',feed).forEach(c=> c.onclick=ev=>{ if(ev.target.closest('[data-prof]')){ renderProfileView(c.dataset.pk); return; } const s=Store.get(c.dataset.id); if(s) openStream(s); });
    };
    paint();
    _maybeOfferAnnounce();   // surface a one-tap Announce if our OWN OBS feed is ingesting but unannounced
    /* Refresh after the useful local paint, with a hard deadline. This Promise.race is also needed
     * for relays that connect but never send EOSE: catch() cannot help when the promise never settles. */
    let fresh=[];
    try{
      fresh=await Promise.race([
        Relay.query([{ kinds:[30311], limit:80 }]),
        new Promise((_,reject)=>setTimeout(()=>reject(new Error('stream relay timed out')),8000))
      ]);
    }catch(_){ fresh=[]; }
    if(fresh && fresh.length && S.VIEW==='streams'){
      const have=new Set(evs.map(e=>e.id));
      fresh.forEach(e=>{ if(!have.has(e.id)){ have.add(e.id); evs.push(e); }
        Store.saveEvent(e); needProfile(e.pubkey); });
      paint();
    }
    // Merge in streams from the wider network (background — the local list already painted). External
    // relays are UNTRUSTED, so VERIFY each event's signature before saving/rendering — an unverified
    // forgery could spoof a host or (addressable) shadow a real user's stream in the local cache.
    Relay.queryFrom(STREAM_RELAYS, [{ kinds:[30311], limit:80 }], {signal:readSignal,purpose:'streams directory'}).then(ext=>{
      if((readSignal&&readSignal.aborted)||!ext || !ext.length || S.VIEW!=='streams') return;
      const have=new Set(evs.map(e=>e.id)); let added=false;
      ext.forEach(e=>{ if(have.has(e.id)) return;
        if(_isDeletedStream(e)) return;   // never resurrect a stream the user deleted from an external relay
        try{ if(!NT().verifyEvent(e)) return; }catch(_){ return; }   // drop unsigned/forged events
        evs.push(e); Store.saveEvent(e); needProfile(e.pubkey); added=true; });
      if(added) paint();
    }).catch(()=>{});
  }
  function openStream(e){
    S.VIEW='stream'; _clearNav(); $('#view-title').textContent='Stream';
    /* The desktop has to be TOLD, because this navigates in place: openStream is reached by tapping a
     * card inside the Streams window (and now by going live), and it sets VIEW directly rather than
     * going through renderView, which is where noteView normally runs. Without this the window still
     * believes it is holding the streams LIST, so a drag — anything that repaints it — throws the
     * stream away and puts the list back. */
    try{ if(window.PCOS && PCOS.noteView) PCOS.noteView('stream'); }catch(_){}
    const feed=$('#feed'); const hpk=streamHost(e); const p=profOf(hpk); needProfile(hpk);
    const _tag=(n)=>(e.tags.find(t=>t[0]===n)||[])[1]||'';
    const title=_tag('title')||'(untitled stream)';
    const summary=_tag('summary');
    const st=streamStatus(e);
    // An ENDED stream's `streaming` tag still points at the (now dead) live URL while `recording` holds
    // the replay — and most clients (zap.stream, OvenMediaEngine) leave BOTH tags on the event. Preferring
    // `streaming` unconditionally meant every external replay silently played a dead URL.
    const url = st==='ended' ? (_tag('recording')||_tag('streaming')) : (_tag('streaming')||_tag('recording'));
    const dtag=_tag('d');
    // The live-chat (kind-1311) `a` tag. An addressable coordinate is ALWAYS kind:<author>:<d> — the
    // `p …host` tag is metadata about who is on camera, not an address — and this used the host, which
    // is the same key only when the streamer published their own event. It isn't for 31% of the live
    // streams on the network: a bot account (Shobot, radio/TV relays) posts the 30311 for a host. Every
    // one of those showed an EMPTY chat here while 40 messages sat on the author coordinate, and our
    // own replies went into a room nobody else was reading. A few clients chat on the host coordinate
    // as well, so READ both and PUBLISH to the one the spec addresses.
    const saddr=`30311:${e.pubkey}:${dtag}`;
    const haddr=(hpk && hpk!==e.pubkey) ? `30311:${hpk}:${dtag}` : '';
    // The stream's own `relays` tag is where its client says the chat lives — poll those too, since a
    // fixed list can't know where an unfamiliar client put the room.
    const sRelays=(e.tags.filter(t=>t[0]==='relays')[0]||[]).slice(1).filter(u=>/^wss?:\/\//i.test(u));
    const isMine = !!(S.ME && e.pubkey===S.ME.pubkey);
    /* YOU ARE BROADCASTING THIS, FROM THIS DEVICE.
     *
     * Landing here after Go Live (see _afterGoLive) means the streamer now opens their own live
     * stream routinely, and playing it back is actively harmful: the audio arrives ~10 seconds late
     * through the speakers into the mic that is broadcasting it, and the browser downloads the exact
     * video this machine is uploading — on a laptop tethered to a phone that is the upload budget
     * gone. What is worth being on this page for is the chat, the headcount and the link. */
    const selfLive = isMine && st==='live' && !!(_liveStream || _phoneStream);
    let watchLink='';
    if(selfLive){ try{ watchLink=_webLink(NT().nip19.naddrEncode({ identifier:dtag, pubkey:e.pubkey,
                         kind:30311, relays:[S.CFG && S.CFG.relay_url].filter(Boolean) })); }catch(_){} }
    feed.innerHTML=`<div class="stream-view">
      <div class="row" style="justify-content:space-between"><button class="btn btn-ghost small" id="st-back"><svg class="ic b-ic" aria-hidden="true"><use href="#i-arrow-left"></use></svg>Streams</button><span style="display:flex;gap:6px">${isMine?'':`<button class="btn btn-neon small" id="st-tip"><svg class="ic b-ic" aria-hidden="true"><use href="#i-zap"></use></svg>Tip</button>`}<button class="btn btn-cyan small" id="st-chat-toggle"><svg class="ic b-ic" aria-hidden="true"><use href="#i-chat"></use></svg>Chat</button>${isDesktop()?`<button class="btn btn-cyan small" id="st-window" title="Open this stream and its live chat in a separate window you can drag to another monitor">🗔 Window</button>`:''}${isMine?`<button class="btn btn-ghost small" id="st-del" style="color:var(--danger,#e0245e)"><svg class="ic b-ic" aria-hidden="true"><use href="#i-trash"></use></svg>Delete</button>`:''}</span></div>
      <h1 class="av-title">${enc(title)}${st==='live'?' <span class="live-badge">● LIVE</span>':''}</h1>
      <div class="av-by"><img class="art-av" src="${enc(p.picture||S.LOGO)}" onerror="this.src='${S.LOGO}'"><span class="name" data-prof="${hpk}">${enc(p.name||p.display_name||'anon')}</span>${st?`<span class="muted small">· ${enc(st)}</span>`:''}<span class="muted small" id="st-viewers">${_viewersTag(e)?` · 👁 ${enc(_viewersTag(e))} watching`:''}</span></div>
      <div class="stream-layout${(!_chatPopout() && ClientSettings.get('streamChatHidden',false))?' chat-hidden':''}">
        <div class="stream-main">
          ${selfLive?`<div class="st-self">
            <div class="st-selfhd"><span class="live-badge">● LIVE</span> You’re broadcasting</div>
            <div class="muted small">The player is off on purpose — you are streaming from this
              device, so playing it back would echo your own mic and re-download the video you are
              uploading. Your chat and headcount are live${isDesktop()?' beside this':' below'}.</div>
            <div class="st-self-actions">
              ${watchLink?`<button class="btn btn-neon small" id="st-selflink"><svg class="ic b-ic" aria-hidden="true"><use href="#i-link"></use></svg>Copy watch link</button>`:''}
              <button class="btn btn-ghost small" id="st-selfprev"><svg class="ic b-ic" aria-hidden="true"><use href="#i-play"></use></svg>Preview it anyway (muted)</button>
            </div></div>`:''}
          ${(url||st==='ended')?`<video class="stream-player" id="st-video" controls playsinline${selfLive?' hidden':''}></video>
            <div class="muted small" id="st-note"></div>
            <div class="row">${isDesktop()&&url?`<button class="btn btn-ghost small" id="st-pop">⧉ Pop out player</button>`:''}${url?`<a class="btn btn-ghost small" href="${enc(url)}" target="_blank" rel="noopener"><svg class="ic b-ic" aria-hidden="true"><use href="#i-play"></use></svg>Open stream URL</a>`:''}</div>`:'<div class="empty">No stream URL provided.</div>'}
          ${summary?`<div class="about">${linkify(summary)}</div>`:''}
        </div>
        <div class="stream-chat" id="st-chat">
          <div class="st-chat-hd">💬 Live chat</div>
          <div class="scm-list" id="st-chat-msgs"><div class="muted small" style="padding:10px">No messages yet — say hi 👋</div></div>
          <div class="scm-input"><input class="input" id="st-chat-inp" placeholder="Say something…" maxlength="500" autocomplete="off"><button class="btn btn-neon small" id="st-chat-send">Send</button></div>
        </div>
      </div>
    </div>`;
    $('#st-back').onclick=()=>{ _closeStreamChat(); switchView('streams'); };
    /* Chat → the chat in its OWN window, player untouched. On a desktop the chat is always on
     * screen beside the video anyway, so a show/hide toggle was solving a problem nobody had; what
     * people actually want is the chat on the second monitor. Popping the WHOLE stream to get that
     * decoded the video twice.
     *
     * Still the toggle on a phone and inside a popout: there is no second window worth opening on a
     * handset, and a chat popout offering to pop its own chat out again is a loop. */
    { const ct=$('#st-chat-toggle'); if(ct){
        const canWindow = isDesktop() && !document.body.classList.contains('popout');
        if(canWindow){
          ct.title = 'Open the live chat in its own window — the stream keeps playing here';
          ct.onclick = () => openStreamWindow(e, true);
        } else ct.onclick=()=>{ const lay=feed.querySelector('.stream-layout'); if(!lay) return;
          const hidden=lay.classList.toggle('chat-hidden'); ClientSettings.set('streamChatHidden', hidden);
          // Actually stop the kind-1311 sub + poll when hidden (not just CSS-hide it) so 'hidden' means 'off'
          // — no background relay traffic — and re-open it when shown again.
          if(hidden) _closeStreamChat(); else _streamChat(saddr, haddr, sRelays); }; } }
    { const db=$('#st-del'); if(db) db.onclick=()=>_deleteStream(e); }
    // Tip the stream creator — ⚡ Lightning / ɱ Monero / 🟢 BCH chooser (doTip picks by what the host advertises).
    { const tb=$('#st-tip'); if(tb) tb.onclick=()=>doTip(e.id, hpk); }
    feed.querySelectorAll('[data-prof]').forEach(el=> el.onclick=()=>renderProfileView(el.dataset.prof));
    decorateProfiles();
    // An ENDED stream: prefer the saved recording (VOD) over the now-dead live URL. Resolve it by the
    // stream's publish token (the 30311 `d` tag) and play the Blossom URL — attachStream feeds a plain
    // mp4/fmp4 straight into <video>. `openStream._tok` guards against a race if you tap another stream
    // before the fetch resolves.
    openStream._view=e.id;   // guard on the (unique) event id, not the d-tag which can be empty
    if(st==='live') _startStreamViewers(hpk, dtag, e.id);   // only a live stream's headcount moves
    if(st==='ended'){
      const _view=e.id; { const n0=$('#st-note'); if(n0) n0.textContent='Loading recording…'; }
      (async()=>{
        // Resolve THIS broadcast's recording. Was `vods[0]` — the newest VOD for the publish token,
        // which on a token that has streamed more than once is somebody else's session: opening an old
        // stream played (and would have stamped) the latest recording.
        let vurl='';
        const _starts=(e.tags.find(t=>t[0]==='starts')||[])[1]||'';
        if(dtag){ try{ vurl = await _vodUrlFor(_tokenOfD(dtag), _starts); }catch(_){} }
        if(S.VIEW!=='stream'||openStream._view!==_view) return;   // navigated away / opened another stream
        const playUrl=vurl||url; const n2=$('#st-note');
        if(!playUrl){
          /* "no recording available" was stated flatly from the instant a stream ended — but the
           * recording does not exist for another ~60-90s (server-side concat + upload), so for that
           * whole window this told the streamer their replay had failed when it was still being made.
           * That is what made people think they had to sit and wait with the app open. Distinguish the
           * two: recent = still saving (and re-check, so it appears without a manual reload). */
          const _ends = parseInt((e.tags.find(t=>t[0]==='ends')||[])[1] || e.created_at, 10) || 0;
          const _fresh = _ends && (Date.now()/1000 - _ends) < 15 * 60;
          if(n2) n2.textContent = _fresh
            ? 'Saving your recording — about a minute. You can close the app; it finishes on the server.'
            : 'This stream ended — no recording available.';
          if(_fresh) setTimeout(()=>{ if(S.VIEW==='stream' && openStream._view===_view) openStream(e); }, 20000);
          return;
        }
        if(n2) n2.textContent = vurl ? '▶ Recorded stream' : '';
        attachStream(playUrl);
        if(vurl) _backfillRecordingTag(e, vurl);
      })();
    } else if(url && !selfLive){ attachStream(url); }
    /* Preview is OPT-IN and starts MUTED — the echo is the reason the player is off, so the way back
     * in must not be able to cause it with one click. Headphones or a muted mic make this fine, and
     * "is my encoder actually sending frames" is a real question, so it is one button away. */
    { const pv=$('#st-selfprev'); if(pv) pv.onclick=()=>{
        const v=$('#st-video'); if(!v || !url) return;
        v.hidden=false; v.muted=true; v.volume=0;
        attachStream(url);
        pv.remove();
        _streamNote('Preview is muted. Unmuting it while your mic is open will echo.');
      }; }
    // _copyFallback, not a bare catch: navigator.clipboard is REFUSED outright in a few of the
    // places this runs (an insecure origin, a WebView without permission), and this is the one
    // string a streamer has to be able to get out of the app.
    { const cl=$('#st-selflink'); if(cl) cl.onclick=()=> copyValue(watchLink, 'watch link copied', 'Your watch link:'); }
    { const pb=$('#st-pop'); if(pb) pb.onclick=()=>popOutStream(e); }
    { const wb=$('#st-window'); if(wb) wb.onclick=()=>openStreamWindow(e); }
    // …but a CHAT POPOUT always starts it: the whole window is the chat, and a remembered "hidden"
    // from the main tab would otherwise open an empty window with no sub running.
    if(_chatPopout() || !ClientSettings.get('streamChatHidden',false)) _streamChat(saddr, haddr, sRelays);
    /* Name the WINDOW after the stream. The chat popout hides the page title to give the messages
     * the whole window, so without this a user with two chats open has two windows both labelled
     * with the app name and no way to tell which is which from the taskbar. */
    if(_chatPopout()){ try{ document.title = '💬 ' + (title || 'Live chat'); }catch(_){} }
  }

  /* The MediaMTX publish token behind a 30311 `d` tag.
   * The `d` was historically the token itself, which is stable for a user's whole life — so every
   * broadcast replaced the previous one at the same address. New broadcasts use `<token>-<starts>`.
   * A hex token never contains `-`, and `starts` is a 10-digit unix time, so stripping a trailing
   * `-<digits>` recovers the token from either form (old events keep resolving). */
  function _tokenOfD(d){ return String(d||'').replace(/-\d{9,}$/, ''); }

  // NIP-53 `recording`: republish YOUR OWN ended kind-30311 with the VOD url on it, once the VOD exists.
  //
  // Why it has to happen here in the browser and not on the server: the 30311 is signed by the streamer's
  // key, which the server never has (nip07/nip46/nip55 all keep it out of reach — see stream_end_service),
  // and the VOD's sha256 does not exist until after the stream has ended and uploaded, so it cannot be
  // baked into the pre-signed "ended" event either. That left our replays invisible to every other client:
  // WE resolve them by publish token through our own API, but a `recording` tag is the only cross-client
  // way to say "the replay is here", so shosho (and zap.stream, and anything else) had nothing to show.
  //
  // Runs when the owner opens their own ended stream — the moment we are certain both halves exist (a
  // signed event we may republish, and a VOD url) without polling anything. Once stamped it never runs
  // again, because the tag is then present on the event we just read.
  async function _backfillRecordingTag(ev, vurl){
    try{
      if(!S.ME || !ev || ev.pubkey !== S.ME.pubkey) return;        // only the author can re-sign their own event
      /* A WRONG tag has to be correctable, not just a missing one. This used to bail on any existing
       * `recording`, which made a bad stamp permanent — and the matcher above did produce bad stamps,
       * pointing four separate broadcasts at one older recording whose blob no longer existed, so every
       * replay 404'd with no way back. Bail only when the tag ALREADY says what we would write. */
      const _cur = ((ev.tags||[]).find(t => t[0] === 'recording') || [])[1] || '';
      if(_cur === vurl) return;
      // Rebuild the SAME addressable event (same `d`), tags carried over verbatim so nothing else about the
      // stream changes — title, image, participants, status:ended all stay exactly as published.
      const tags = (ev.tags||[]).filter(t => t[0] !== 'recording').map(t => t.slice());
      tags.push(['recording', vurl]);
      const r = await publish(30311, ev.content || '', tags, { quiet: true });
      if(r && r.ok){
        // THE STAMPED EVENT MUST REACH THE RELAYS THE OTHER CLIENTS READ. publish() only hits our own
        // relay, and the stream is on the external STREAM_RELAYS too (that is where zap.stream/shosho
        // found it in the first place). Without this the `recording` tag existed only on poster.place:
        // every other client kept the older, untagged copy and showed the stream as unreplayable —
        // exactly the bug. _deleteStream already compensates the same way, for the same reason.
        try{ if(r.ev) await Relay.publishTo(STREAM_RELAYS, r.ev); }catch(_){ }
        toast('replay linked on your stream — other clients can find it now');
        // Reflect it locally so reopening the view does not try again before the relay read catches up.
        try{ ev.tags = tags; }catch(_){ }
      }
      return !!(r && r.ok);
    }catch(err){ /* never block playback on this — the replay already plays for us */ }
    return false;
  }

  /* STAMP THE REPLAY WITHOUT WAITING TO BE LOOKED AT.
   *
   * _backfillRecordingTag used to run from exactly ONE place: opening your own ended stream in this
   * client, with a VOD already finished. End a stream and never reopen it — which is what normally
   * happens — and no `recording` tag was ever published, so shosho had nothing to mark replayable no
   * matter how long it waited. The VOD is not instant either (concat + Blossom upload), so stamping
   * once at End would usually find nothing and give up.
   *
   * So: poll for this broadcast's VOD after the stream ends, with a bounded backoff, and stamp the
   * moment it appears. Cheap (one small JSON GET per tick, only while the tab is open and only for a
   * stream we just ended) and it stops at the first success. _sweepUnstampedReplays covers the tab
   * that was closed before the VOD finished. */
  async function _stampReplayWhenReady(addr, token, starts){
    const DELAYS = [15, 30, 60, 60, 120, 120, 300, 300, 600];   // ~28 min, then give up to the sweep
    for(const wait of DELAYS){
      await new Promise(r => setTimeout(r, wait * 1000));
      if(!S.ME) return;
      let vurl = '';
      try{ vurl = await _vodUrlFor(token, starts); }catch(_){ }
      if(!vurl) continue;
      // Re-read the event rather than trusting a stale copy: End, the viewer-count update and the
      // parked sentinel all re-sign this address, so the newest version is the only safe base.
      let ev = null;
      try{ ev = (await Relay.query([{ kinds:[30311], authors:[S.ME.pubkey], '#d':[addr], limit:1 }]))[0]; }catch(_){ }
      if(!ev) continue;
      if((ev.tags||[]).some(t => t[0]==='recording' && t[1])) return;   // someone already stamped it
      if(await _backfillRecordingTag(ev, vurl)) return;
    }
  }

  /* The VOD for ONE broadcast, not "the newest VOD this streamer has".
   * /vods/by-token returns every session for the publish token (it is stable for life), newest first,
   * so picking [0] attributes the latest recording to whichever old stream you happened to open. Match
   * on the session's start time — the same key stream_vod_service uses for its one-VOD-per-session
   * uniqueness index — and only fall back to newest when the event carries no `starts`. */
  /* ONE request per token, not one per event.
   *
   * The publish token is stable for life, so a streamer with a dozen past broadcasts has a dozen
   * kind-30311 events that all resolve to the SAME /vods/by-token list — and the replay sweep asks
   * per event. Measured in the server log: twelve identical requests for one token inside a single
   * second, answered identically twelve times. Nothing failed, so nothing said anything.
   *
   * Cached briefly, with concurrent callers sharing the in-flight promise, so a sweep costs one
   * round-trip per token however many broadcasts it covers. The window is deliberately SHORTER than
   * _stampReplayWhenReady's first retry (15s): that loop is waiting for a recording to appear, and a
   * cache that outlived its poll would just make it wait longer. A failed fetch is not cached at all
   * — "the request didn't work" must not be remembered as "there are no recordings". */
  const _VOD_TTL = 10000;
  const _vodLists = new Map();        // token -> { at, p }

  function _vodList(token){
    const now = Date.now();
    const hit = _vodLists.get(token);
    if(hit && (now - hit.at) < _VOD_TTL) return hit.p;
    const p = (async () => {
      const r = await _streamFetch('/api/streams/vods/by-token/' + encodeURIComponent(token));
      if(!(r && r.ok)){ _vodLists.delete(token); return null; }   // null = ask again, not "none exist"
      try{ return ((await r.json()) || {}).vods || []; }
      catch(_){ _vodLists.delete(token); return null; }
    })();
    _vodLists.set(token, { at: now, p });
    if(_vodLists.size > 64)
      for(const k of [..._vodLists.keys()].slice(0, 32)) _vodLists.delete(k);
    return p;
  }

  async function _vodUrlFor(token, starts){
    if(!token) return '';
    const vods = await _vodList(token);
    if(!vods || !vods.length) return '';
    const s = parseInt(starts, 10);
    if(!s) return vods[0].url || '';
    /* A RECORDING CANNOT START BEFORE THE BROADCAST WAS ANNOUNCED, and "no recording yet" must return
     * NOTHING rather than the nearest other one.
     *
     * This was Math.abs() over a 6-hour window, which is how four separate streams all ended up tagged
     * with the SAME recording — one belonging to a broadcast two hours earlier, whose blob no longer
     * existed, so every replay 404'd. The VOD is not written until the concat+upload finishes (~90s
     * after the stream ends), so at stamping time the only candidates were older streams, and an
     * absolute-difference match happily reached back and took one.
     *
     * The feed arrives at or after the announce, so a VOD that began before it (past a little clock
     * skew) is a DIFFERENT broadcast, always. Anything left is scored by how soon after the announce it
     * began, and it must begin within the hour — a recording that starts long after the announce
     * belongs to a later go-live on the same token. No candidate = '', and the caller retries. */
    const SKEW = 120, MAX_WAIT = 3600;
    let best = null, bestGap = Infinity;
    for(const v of vods){
      const gap = (parseInt(v.started_at, 10) || 0) - s;   // signed: negative = started BEFORE the announce
      if(gap < -SKEW || gap > MAX_WAIT) continue;
      if(Math.abs(gap) < Math.abs(bestGap)){ best = v; bestGap = gap; }
    }
    return best ? (best.url || '') : '';
  }

  /* The tab that was closed before the VOD finished never got to stamp. On startup, look at YOUR OWN
   * ended streams, and stamp any that have a finished recording and no `recording` tag yet. Bounded to
   * the recent ones so this is a handful of requests, not a crawl of your whole history. */
  async function _sweepUnstampedReplays(){
    try{
      if(!S.ME || S.GUEST) return;
      let mine = [];
      try{ mine = await Relay.query([{ kinds:[30311], authors:[S.ME.pubkey], limit:30 }]); }catch(_){ return; }
      for(const ev of mine){
        if(streamStatus(ev) !== 'ended') continue;
        const d = (ev.tags.find(t=>t[0]==='d')||[])[1] || '';
        const starts = (ev.tags.find(t=>t[0]==='starts')||[])[1] || '';
        if(!d) continue;
        // Deliberately NOT skipping events that already carry a `recording`: the sweep is also the
        // repair path for one that points at the wrong (or a since-deleted) VOD. _backfillRecordingTag
        // no-ops when the tag already matches, so a correct one costs a comparison and no publish.
        const cur = ((ev.tags||[]).find(t=>t[0]==='recording')||[])[1] || '';
        let vurl = '';
        try{ vurl = await _vodUrlFor(_tokenOfD(d), starts); }catch(_){ }
        if(vurl && vurl !== cur) await _backfillRecordingTag(ev, vurl);
      }
    }catch(_){ }
  }
  // Delete YOUR OWN stream: NIP-09 kind-5 addressed to the 30311's `a` (removes all versions) + its event id.
  async function _deleteStream(e){
    if(!S.ME || e.pubkey!==S.ME.pubkey){ toast('you can only delete your own stream'); return; }
    if(!await uiConfirm('Delete this stream? It’s removed from Nostr for everyone.')) return;
    const d=(e.tags.find(t=>t[0]==='d')||[])[1]||'';
    try{
      const r=await publish(5, '', [['a', `30311:${e.pubkey}:${d}`], ['e', e.id], ['k','30311']], {quiet:true,publicDeletion:true});
      if(!(r && r.ok)){ toast('couldn’t reach the relay — the stream was NOT deleted, try again'); return; }
      // The 30311 also lives on the external stream relays — publish() only hit our local one, so ask them to
      // remove it too (best-effort), else the profile/Streams re-fetch it from there and it comes back.
      try{ if(r.ev) Relay.publishTo(STREAM_RELAYS, r.ev).catch(()=>{}); }catch(_){}
      _forgetDeletedStream(e);
      try{ Store.removeEvent(e.id); }catch(_){}
      _closeStreamChat(); toast('stream deleted'); switchView('streams');
    }catch(_){ toast('couldn’t delete the stream'); }
  }
  function _forgetDeletedStream(e){
    const d=((e.tags||[]).find(t=>t[0]==='d')||[])[1]||'';
    _markStreamDeleted(_streamAddr(e));
    _clearEndSentinel();
    if(d) _endedStreams.add(d);
    if(_liveStream && ((_liveStream.d||_liveStream.token)===d)){ _stopLiveHb(); _liveStream=null; }
    if(_phoneStream && _phoneStream.token===d) _teardownPhoneStream();
    _closeStreamChat();
  }
  function _closeStreamChat(){
    if(S._streamChatSub){ try{ Relay.close(S._streamChatSub); }catch(_){} S._streamChatSub=null; }
    if(S._streamChatPoll){ clearInterval(S._streamChatPoll); S._streamChatPoll=null; }
  }
  function _streamChat(saddr, haddr, sRelays){
    _closeStreamChat();
    const addrs=[saddr, haddr].filter(Boolean);
    const relays=[...new Set([...STREAM_RELAYS, ...(sRelays||[])])];
    const box=$('#st-chat-msgs'); if(!box) return;
    const seen=new Set(); const msgs=[];
    const render=()=>{ if(!msgs.length) return;
      box.innerHTML=msgs.slice(-200).map(m=>{ const pr=profOf(m.pubkey)||{}; needProfile(m.pubkey);
        const nm=`<b class="scm-name" data-prof="${m.pubkey}">${enc(pr.name||pr.display_name||'anon')}</b>`;
        const av=`<img class="scm-av" data-prof="${m.pubkey}" src="${enc(pr.picture||S.LOGO)}" onerror="this.src='${S.LOGO}'">`;
        // NIP-25: "+" is a like and "-" a dislike; anything else is the emoji itself. A custom
        // `:shortcode:` reaction carries its image in an `emoji` tag, so show that rather than the
        // raw text — otherwise a reaction reads as literal ":pepe:".
        if(m.kind===7){
          const raw=(m.content||'').trim();
          const sc=raw.replace(/^:|:$/g,'');
          const et=(m.tags||[]).find(t=>t[0]==='emoji' && t[1]===sc);
          const face = et ? `<img class="scm-emoji" src="${enc(et[2]||'')}" alt="${enc(sc)}">`
                          : enc(raw==='+'||!raw ? '❤️' : raw==='-' ? '👎' : raw.slice(0,16));
          return `<div class="scm scm-react">${av}<div class="scm-body">${nm} <span class="scm-txt">reacted ${face}</span></div></div>`;
        }
        return `<div class="scm">${av}<div class="scm-body">${nm} <span class="scm-txt">${linkify(m.content||'')}</span></div></div>`; }).join('');
      box.scrollTop=box.scrollHeight;
      box.querySelectorAll('[data-prof]').forEach(el=> el.onclick=()=>renderProfileView(el.dataset.prof)); };
    /* Kind 7 belongs in the room too. The chat read 1311 ONLY, so every reaction sent to the stream
     * — by us or by any other NIP-53 client — was fetched, matched and then dropped on the floor,
     * while shosho and zap.stream showed a busy room and ours looked dead. A reaction to a live
     * stream IS chat: it is addressed to the same `a` coordinate and it is what most viewers send. */
    const onEv=ev=>{ if(!ev || (ev.kind!==1311 && ev.kind!==7) || seen.has(ev.id)) return; seen.add(ev.id);
      msgs.push(ev); msgs.sort((a,b)=>a.created_at-b.created_at);
      if(msgs.length>500) msgs.splice(0, msgs.length-500);   // bound memory on a long, busy chat
      if(S.VIEW==='stream') render(); };
    try{ S._streamChatSub=Relay.subscribe([{kinds:[1311,7], '#a':addrs, limit:150}], {onEvent:onEv, live:true}); }catch(_){}
    // Pull the same room from the public stream relays — this is what makes the chat cross-client rather
    // than poster.place-only. queryFrom is one-shot (no live external sub exists), so poll while it's open.
    const pull=()=>{ Relay.queryFrom(relays, [{kinds:[1311,7], '#a':addrs, limit:150}], {purpose:'stream chat'})
      .then(evs=>(evs||[]).forEach(onEv)).catch(()=>{}); };
    pull(); S._streamChatPoll=setInterval(()=>{ if(S.VIEW!=='stream'){ _closeStreamChat(); return; } pull(); }, 10000);
    const inp=$('#st-chat-inp'), send=$('#st-chat-send');
    const doSend=async()=>{ const t=(inp.value||'').trim(); if(!t) return;
      if(S.GUEST){ _guestPrompt(); return; } inp.value='';
      try{ const r=await publish(1311, t, [['a', saddr, (S.CFG&&S.CFG.relay_url)||'', 'root']]);
        if(r&&r.ok&&r.ev){ onEv(r.ev); Relay.publishTo(relays, r.ev).catch(()=>{}); }   // render only if stored (else a rejected line would ghost)
        else if(inp) inp.value=t; }   // failed → restore the text (publish() toasted); don't render a ghost
      catch(_){ toast('couldn’t send'); if(inp) inp.value=t; } };
    if(send) send.onclick=doSend;
    if(inp) inp.onkeydown=ev=>{ if(ev.key==='Enter' && !ev.shiftKey){ ev.preventDefault(); doSend(); } };
  }
  // ---------- Go Live (OBS streaming) — publish a NIP-53 kind-30311 pointing at the built-in MediaMTX HLS ----------
  let _liveStream=null;   // { token, title, hls } while this device is announcing a live stream
  let _phoneStream=null;  // { pc, local, token } while streaming from the phone camera via WHIP
  let _goingLive=false;   // re-entrancy guard while a phone go-live is mid-handshake (before _phoneStream is set)
  let _glOpening=false;   // …and while the Go Live SHEET is being fetched, so a slow open can't stack two of them
  // Tokens we've explicitly ENDED (or deleted) this session. _adoptOwnLive must never re-adopt these: after
  // End, the still-'live' 30311 lingers in cache and comes back from the external stream relays for a while
  // (the ended event hasn't federated there yet), so without this the next render silently re-adopts it and
  // the user is stuck on "End stream" with no way back to "Go Live". Cleared when they genuinely go live again.
  let _endedStreams=new Set();
  function _markStreamDeleted(addr){ if(!addr) return; _deletedStreams.add(addr);
    try{ ClientSettings.set('deletedStreams', [..._deletedStreams].slice(-300)); }catch(_){} }
  let _liveHb=null;
  function _stopLiveHb(){ if(_liveHb){ clearInterval(_liveHb); _liveHb=null; } }
  function _startLiveHb(){   // if the HLS 404s repeatedly, OBS stopped → mark the stream ended so it doesn't orphan as LIVE
    _stopLiveHb(); let miss=0;
    // Count viewers on the OBS/desktop path too. _startViewerPoll used to be called ONLY from the phone
    // broadcast overlay, so an OBS stream never polled MediaMTX and never wrote `current_participants` back
    // to its kind-30311 — which is why a desktop stream showed no viewer count anywhere, here or in any
    // other NIP-53 client. Every OBS/adopt path goes through this heartbeat, so it belongs here.
    _startViewerPoll();
    _liveHb=setInterval(async()=>{
      if(!_liveStream){ _stopLiveHb(); return; }
      let ok=false; try{ const r=await fetch(_liveStream.hls,{cache:'no-store'}); ok=r.ok; }catch(_){ ok=false; }
      miss = ok ? 0 : miss+1;
      if(miss>=3){ _stopLiveHb(); toast('stream stopped (OBS ended) — marking it ended'); _endLive(); }
    }, 45000);
  }
  /* "Did OUR SERVER answer?" — REACHABILITY, which is not the same question as "was I allowed in".
   *
   * This is the guard that decides whether a dead HLS url means "the broadcast is over" or "this
   * device is offline", and it was written as `apiUp = r.ok` against /api/streams/ingest — an
   * endpoint behind get_current_user. It answers 401 to a client that isn't carrying an app session,
   * which is the ordinary state of the Nostr client (`_aiToken` is only set by nostr-login, and the
   * call didn't send credentials either). `r.ok` is false for 401, so the guard concluded "my network
   * is the suspect" and returned — every time, on every open. The retirement it protects could
   * therefore never run once, which is why a stream fixed last night was still ● LIVE this morning.
   * A 401 is our server ANSWERING. Only a fetch that never resolves means we couldn't reach it.
   *
   * 5xx is the one status deliberately NOT trusted: the origin being sick is exactly when the HLS
   * probe also fails for a reason that has nothing to do with the broadcast, so it reads as ambiguous
   * and we leave the announcement alone. Anything else — 200, 401, 403, 404 — proves the app is up.
   *
   * It probes the ORIGIN OF THE DEAD HLS URL, not a relative path. Two reasons, and the first is a
   * false positive that would end a live broadcast: in the BUNDLED desktop/native app the client is
   * served from its own origin, so `/api/streams/ingest` resolves against the BUNDLE — it can answer
   * (or 404) perfectly happily while the device has no network at all, which is precisely the state
   * this guard exists to detect. The second is that a stream's `streaming` url names the instance that
   * actually hosts it, which is not necessarily the one this client is signed in to. Asking the same
   * server whose playlist just failed is the only probe that answers the real question. */
  async function _serverAnswers(hls){
    let origin='';
    try{ origin=new URL(hls, _instanceBase()||undefined).origin; }
    catch(_){ origin=_instanceBase(); }
    if(!origin) return false;   // standalone, no instance: nothing to ask, so nothing may be retired
    try{
      const r=await fetch(origin+'/api/streams/ingest', { credentials:'include',
                          headers:S._aiToken?{'Authorization':'Bearer '+S._aiToken}:{} });
      return !(r.status>=500);
    }catch(_){ return false; }
  }
  /* Retire one of OUR OWN 30311s that still says `live` but demonstrably isn't. Returns true when it
   * published the `ended` event, false when the stream might still be running (or we can't tell).
   *
   * Split out of _adoptOwnLive because a stale LIVE announcement is a public claim on relays we don't
   * control, and it was only ever re-examined when its owner happened to open Discover → Streams.
   * That is the wrong trigger for a wrong statement the whole network can see. _sweepStaleOwnLive
   * calls this at startup with events fetched by author, so it no longer depends on a view being
   * opened or on a stale event surviving in a global limit-80 feed. */
  async function _probeFeed(hls){
    if(!hls) return false;
    try{ const r=await fetch(hls,{cache:'no-store'}); return r.ok; }catch(_){ return false; }
  }
  async function _retireIfOver(mine){
    const hls=(mine.tags.find(t=>t[0]==='streaming')||[])[1]||'';
    let feedUp=await _probeFeed(hls);
    // CONFIRM a miss before acting. This used to be one probe, which was defensible when the only
    // caller was the Streams view a human had just opened; the startup sweep made it unattended and
    // ran it against every stream announcement we own, so a single CDN blip on a genuinely live feed
    // would publish `ended` mid-broadcast. That is the outcome this whole path calls far worse than
    // leaving a stale announcement up, so it costs one fetch and three seconds to not do it.
    if(!feedUp && hls){
      await new Promise(r=>setTimeout(r, 3000));
      feedUp=await _probeFeed(hls);
    }
    // AGE is the second guard, and it is what makes a single probe safe to act on. The heartbeat
    // needs THREE misses over ~2 minutes before it ends a stream, because OBS reconnects and a CDN
    // blips — one 404 is not proof a broadcast is over. So a recent announcement with a dead feed is
    // still ADOPTED by the caller, exactly as before, and left to that 3-strike path. Only an
    // announcement old enough that no reconnect explains it is retired outright.
    const startedAt=parseInt((mine.tags.find(t=>t[0]==='starts')||[])[1]||mine.created_at,10)||0;
    const stale=startedAt && (Date.now()/1000 - startedAt) > 900;   // 15 min
    if(!(hls && !feedUp && stale)) return false;
    if(!await _serverAnswers(hls)) return false;   // our own network is the suspect — leave it alone
    // Really over. Retire it so it stops claiming to be live everywhere, carrying `starts` and the
    // rest through _liveBase so the ended event keeps the broadcast's own identity.
    const sid0=(mine.tags.find(t=>t[0]==='d')||[])[1]||'';
    _endedStreams.add(sid0); _endedStreams.add(_tokenOfD(sid0));
    try{
      const s0={ token:_tokenOfD(sid0), d:sid0,
                 title:(mine.tags.find(t=>t[0]==='title')||[])[1]||'Live stream', hls,
                 starts:(mine.tags.find(t=>t[0]==='starts')||[])[1]||String(mine.created_at),
                 image:(mine.tags.find(t=>t[0]==='image')||[])[1]||'' };
      const r=await publish(30311, '', _liveBase(s0).concat(
        [['status','ended'], ['ends', String(Math.floor(Date.now()/1000))]]));
      // Mirror on a SIGNED EVENT, not on r.ok — deliberately not _mirrorStream here. The phantom being
      // cleared is standing on the PUBLIC relays (the stuck one this was written for was on primal and
      // not on ours at all), so making that cleanup conditional on our own relay accepting the ended
      // event first would leave the wrong claim up for the entire network on a local hiccup. Awaited,
      // because reaching those relays IS the job. `d` comes through _liveBase unchanged, so this
      // REPLACES the live announcement at its own address rather than adding a second event.
      if(r && r.ev){ try{ await Relay.publishTo(STREAM_RELAYS, r.ev); }catch(_){ } }
    }catch(_){ }
    return true;
  }
  /* Startup sweep: ask the connected relay for our own 30311s BY AUTHOR and retire any that are
   * still claiming to be live. A corrective event is still mirrored to the public relays below;
   * the startup audit itself must not construct Damus/nos.lol sockets over an unrelated view.
   *
   * Two reasons this can't be left to the Streams view. (1) It only ran when that view was opened,
   * so a broadcast could sit ● LIVE on zap.stream for days while its owner used the app normally —
   * which is exactly how this was reported, twice. (2) The list it filtered came from a generic
   * `{kinds:[30311], limit:80}` across relays that carry every stream on the network; a day-old
   * announcement of ours is not in the newest 80 (measured: nos.lol's window had already dropped it
   * while primal's had not), so even opening the view was not reliably enough. Asking by author is
   * bounded, cheap and exact.
   *
   * Never awaited, never fatal, once per session. */
  let _sweptStaleLive=false;
  async function _sweepStaleOwnLive(){
    if(_sweptStaleLive || S.GUEST || !S.ME || _liveStream) return;
    _sweptStaleLive=true;
    let evs=[];
    try{ evs=await Relay.query([{ kinds:[30311], authors:[S.ME.pubkey] }]); }catch(_){ }
    if(!evs || !evs.length) return;
    // Newest per address only: a replaceable event's older copies are not what any client shows, and
    // retiring against a superseded copy would publish an `ended` that is itself immediately stale.
    const best=new Map();
    evs.forEach(e=>{
      // Signature-verify before acting: these relays are untrusted, and a forged `live` event in our
      // name would otherwise make us publish an `ended` for a stream that never existed.
      try{ if(!NT().verifyEvent(e)) return; }catch(_){ return; }
      if(e.pubkey!==S.ME.pubkey) return;
      const d=(e.tags.find(t=>t[0]==='d')||[])[1]||'';
      const prev=best.get(d);
      if(!prev || e.created_at>prev.created_at) best.set(d, e);
    });
    for(const e of best.values()){
      if(_liveStream) return;                        // went live for real while the sweep ran
      if(streamStatus(e)!=='live') continue;
      const d=(e.tags.find(t=>t[0]==='d')||[])[1]||'';
      if(_endedStreams.has(d) || _endedStreams.has(_tokenOfD(d))) continue;
      try{ await _retireIfOver(e); }catch(_){ }
    }
  }
  // On (re)opening Streams, re-adopt our OWN still-live announcement so a reload doesn't strand it as
  // permanently LIVE (the End button reappears + the heartbeat resumes).
  async function _adoptOwnLive(streams){
    if(_liveStream || S.GUEST || !S.ME) return;
    const mine=(streams||[]).find(e=>e.pubkey===S.ME.pubkey && streamStatus(e)==='live'
                                     && !_endedStreams.has((e.tags.find(t=>t[0]==='d')||[])[1]));
    if(!mine) return;
    // ADOPT ONLY A STREAM THAT IS ACTUALLY STILL RUNNING. This used to adopt any 30311 of ours whose
    // status says `live`, which makes a stream that never got its `ended` event IMMORTAL: opening
    // Streams re-adopts it, the heartbeat republishes it, and the event's created_at marches forward
    // while its `starts` stays days in the past. Measured on a real account — a broadcast from two days
    // earlier was still being restamped ● LIVE on zap.stream every time the owner opened the app, which
    // is exactly "wtf, I am not streaming this anymore".
    //
    // The probe is only trusted when OUR OWN api answers: an unreachable HLS url means "the stream is
    // over" if the server is reachable, and "this device is offline" if it is not — and ending someone's
    // live broadcast because their wifi dropped is far worse than leaving a stale one up for longer.
    if(await _retireIfOver(mine)) return;
    // `d` is the SESSION id now; the MediaMTX token (what the HLS url and the VOD lookup need) is the
    // part before the `-<starts>` suffix. _tokenOfD also passes through an old event whose d IS the token.
    const sid=(mine.tags.find(t=>t[0]==='d')||[])[1];
    const tok=_tokenOfD(sid);
    if(!tok || _endedStreams.has(tok) || _endedStreams.has(sid)) return;
    // Carry `starts` over from the adopted event. Without it every later republish of this replaceable 30311
    // (the viewer count, the ended event) would invent a new start time — the stream would look like it began
    // seconds ago and its duration would reset for every client.
    const starts=(mine.tags.find(t=>t[0]==='starts')||[])[1]||String(mine.created_at);
    // Carry `image` over for the same reason as `starts` — re-adopting after a reload and then re-signing
    // (viewer count / end) would otherwise drop the cover we announced with.
    _liveStream={ token:tok, d:sid, title:(mine.tags.find(t=>t[0]==='title')||[])[1]||'Live stream',
                  hls:(mine.tags.find(t=>t[0]==='streaming')||[])[1]||'', starts,
                  image:(mine.tags.find(t=>t[0]==='image')||[])[1]||'' };
    if(_liveStream.hls) _startLiveHb();
    // Re-park the end-of-stream fallback: this stream may predate it, or the original park may have failed.
    // Harmless if one is already stored — the server keeps the "went live" state across a re-park.
    _parkEndSentinel(_liveStream);
  }
  // The #1 "I streamed but nothing shows anywhere" confusion: starting OBS makes the SERVER ingest, but the
  // kind-30311 that actually puts a stream on Discover / your profile / zap.stream can only be SIGNED here in
  // the browser — so ingesting isn't announcing. If our own OBS feed is live yet we never announced it, surface
  // a loud one-tap Announce banner instead of leaving the user to hunt for the button in the Go Live dialog.
  async function _maybeOfferAnnounce(){
    if(_liveStream || _phoneStream || _goingLive || S.GUEST || !S.ME || S.VIEW!=='streams') return;
    let info; try{ info=await fetch('/api/streams/ingest',{headers:S._aiToken?{'Authorization':'Bearer '+S._aiToken}:{}}).then(r=>r.json()); }catch(_){ return; }
    if(!info || !info.hls_url || info.enabled===false) return;
    let live=false; try{ const r=await fetch(info.hls_url,{cache:'no-store'}); live=r.ok; }catch(_){ }
    if(!live || _liveStream || S.VIEW!=='streams') return;   // re-check the guards after the awaits
    const feed=$('#feed'); const top=feed&&feed.querySelector('.streams-top');
    if(!top || feed.querySelector('#ingest-banner')) return;
    const b=document.createElement('div'); b.id='ingest-banner'; b.className='ingest-banner';
    b.innerHTML=`<div>🔴 <b>Your OBS stream is live on the server</b> — but not announced on Nostr yet, so it won’t show on Discover or your profile until you announce it.</div>`
      +`<div class="ib-row"><input class="input" id="ib-title" placeholder="Stream title" maxlength="120"><button class="btn btn-neon small" id="ib-go"><svg class="ic b-ic" aria-hidden="true"><use href="#i-live"></use></svg>Announce live</button></div>`;
    top.after(b);
    feed.querySelector('#ib-go').onclick=async(ev)=>{ const btn=ev.currentTarget; btn.disabled=true;
      const t=(feed.querySelector('#ib-title').value||'').trim()||'Live stream';
      try{ const ev=await _publishLive(info, t); _startLiveHb();
        toast('🔴 you’re live — announced on Nostr'); _afterGoLive(ev, false); }
      catch(_){ btn.disabled=false; toast('couldn’t announce the stream'); } };
  }
  async function _goLive(){
    if(S.GUEST){ _guestPrompt(); return; }
    if(_liveStream || _phoneStream || _goingLive){ toast('you’re already live'); switchView('streams'); return; }
    // Opening this sheet is two network round trips, so it is a button that can be slow — which is a
    // button that looks broken, and gets clicked again (stacking a second Go Live sheet on top of the
    // first). Say it's working, refuse to run twice, and BOUND the request: a stalled fetch has no
    // timeout of its own, so without this the entry can hang for minutes with nothing on screen.
    if(_glOpening) return; _glOpening=true;
    const _navGl=$('#nav-golive'); if(_navGl) _navGl.classList.add('gl-busy');
    let info, reached=true;
    try{
      try{ await ensureAiSession(); }catch(_){ }
      try{ info=await _fetchTimeout('/api/streams/ingest',{headers:S._aiToken?{'Authorization':'Bearer '+S._aiToken}:{}}, 20000).then(r=>r.json()); }
      catch(_){ reached=false; }
    } finally { _glOpening=false; if(_navGl) _navGl.classList.remove('gl-busy'); }
    // "I couldn't ask" is not "the server said no" — the old code reported an unreachable server as
    // "streaming isn't enabled on this server", which sends you to look at settings that are fine.
    if(!info && !reached){ toast('couldn’t reach the server — try again in a moment'); return; }
    // The server is the authority on permission: the sidebar/More gate can only hide the entry when
    // the session is already known (_aiAuth is null until ensureAiSession resolves, which for a
    // non-admin may not have happened yet), so the refusal has to read well on its own.
    if(info && info.error==='no_permission'){
      // Not "streaming is off" — streaming works, this account just isn't allowed yet. Offer the ask.
      modal(`<h3><svg class="ic h-ic" aria-hidden="true"><use href="#i-live"></use></svg>Go Live</h3><p>${enc(info.message||'Live streaming isn’t enabled for your account yet.')}</p>
        <div class="row" style="margin-top:16px;justify-content:center;gap:10px;flex-wrap:wrap">
          <button class="btn btn-neon" id="sreq"><svg class="ic b-ic" aria-hidden="true"><use href="#i-live"></use></svg>Request streaming access</button>
          <button class="btn btn-ghost" id="sreqx">Close</button></div>`, root=>{
        $('#sreq',root).onclick=e=>requestStreamAccess(e.target);
        $('#sreqx',root).onclick=()=>closeModal();
      });
      return; }
    if(!info || info.enabled===false || !info.rtmp_url){
      toast(info && info.message ? info.message : 'streaming isn’t enabled on this server'); return; }
    const canPhone = !!(info.whip_url && navigator.mediaDevices && window.RTCPeerConnection);
    // Screen share comes from either the native plugin (Android app → RTMP, captured outside the WebView)
    // or a desktop browser's getDisplayMedia over the SAME WHIP ingest the webcam uses. No MOBILE browser
    // implements getDisplayMedia, so this still hides itself there — but gating it on the plugin ALONE hid
    // it on desktop too, where the only route left was to go live with the webcam and then swap mid-stream.
    const canScreen = !!_screenPlugin()
      || !!(canPhone && navigator.mediaDevices && navigator.mediaDevices.getDisplayMedia);
    // Two URLs OBS's server+key split doesn't give you, both worth having on the clipboard:
    //   * the whole ingest url in one string, for encoders with a single URL box (ffmpeg, phone apps
    //     like Larix). Masked like the key, because it CONTAINS the key.
    //   * the watch link — the same https link the announcement post carries, available BEFORE you go
    //     live so you can paste it wherever you're telling people. The token is stable, so this is the
    //     same address every time you stream (see _announceStreamPost).
    const fullIngest = String(info.rtmp_url||'').replace(/\/+$/,'') + '/' + String(info.stream_key||'');
    let watchUrl='';
    try{
      const _r=[S.CFG && S.CFG.relay_url].filter(Boolean);
      // Mint this broadcast's `d` NOW, so the copyable watch link points at the event we are about to
      // publish. It has to happen here: the naddr identifier IS the `d`, and that is no longer the
      // (stable) token, so a link built from the token would address the wrong — or a stale — stream.
      info.d = info.token + '-' + Math.floor(Date.now()/1000);
      watchUrl=_webLink(NT().nip19.naddrEncode({identifier:info.d, pubkey:S.ME.pubkey, kind:30311, relays:_r}));
    }catch(_){ }
    modal(`<h3><svg class="ic h-ic" aria-hidden="true"><use href="#i-live"></use></svg>Go Live</h3>
      <label class="fld">Title<input class="input" id="gl-title" placeholder="What are you streaming?" maxlength="120" autofocus></label>
      <label class="muted small" style="display:flex;gap:8px;align-items:center;margin:6px 0"><input type="checkbox" id="gl-announce" checked> Also announce to followers (a post with a watch link)</label>
      ${info.record_available?`<label class="muted small" style="display:flex;gap:8px;align-items:center;margin:6px 0"><input type="checkbox" id="gl-record" ${info.record_enabled?'checked':''}> Save my streams — recorded and kept in your “Past streams”</label>`:''}
      ${info.quality_available?`<div class="gl-q">
        <div class="muted small">Quality — lower it if your connection is slow or you're on mobile data:</div>
        <div class="gl-qrow">
          ${['auto'].concat(info.quality_tiers||[]).map(q=>`<label class="gl-qopt"><input type="radio" name="gl-q" value="${enc(q)}" ${String(info.quality||'auto')===q?'checked':''}><span>${q==='auto'?'Auto':enc(q)+'p'}</span></label>`).join('')}
        </div>
      </div>`:''}
      ${canPhone || canScreen
        ? `<div class="gl-src">
             <div class="muted small" style="margin-bottom:4px">What do you want to broadcast?</div>
             ${canPhone ? `<label class="gl-opt"><input type="radio" name="gl-src" value="cam"> ${isDesktop()?'📹 This webcam':'📱 This phone’s camera'}</label>
             <div class="gl-sub hidden" id="gl-facing">
               <label class="gl-opt"><input type="radio" name="gl-facing" value="user"> 🤳 Front camera</label>
               <label class="gl-opt"><input type="radio" name="gl-facing" value="environment"> 📷 Back camera</label>
             </div>` : ''}
             ${canScreen ? `<label class="gl-opt"><input type="radio" name="gl-src" value="screen"> 🖥 This screen</label>` : ''}
             <label class="gl-opt"><input type="radio" name="gl-src" value="obs"> 🎛 OBS / another encoder</label>
           </div>`
        : `<p class="muted small">Stream from OBS (or any RTMP encoder) — Service: <b>Custom</b>:</p>`}
      <label class="fld">Server<span class="copyrow"><input class="input" id="gl-srv" readonly value="${enc(info.rtmp_url)}"><button class="btn btn-ghost small" data-copy="gl-srv">Copy</button></span></label>
      <label class="fld">Stream key<span class="copyrow"><input class="input" id="gl-key" type="password" readonly value="${enc(info.stream_key)}"><button class="btn btn-ghost small" data-copy="gl-key">Copy</button></span></label>
      <label class="fld">Stream URL <span class="muted small">— server + key in one, for encoders with a single URL box</span><span class="copyrow"><input class="input" id="gl-full" type="password" readonly value="${enc(fullIngest)}"><button class="btn btn-ghost small" data-copy="gl-full">Copy</button></span></label>
      ${watchUrl?`<label class="fld">Watch link <span class="muted small">— share this; it opens the player</span><span class="copyrow"><input class="input" id="gl-watch" readonly value="${enc(watchUrl)}"><button class="btn btn-ghost small" data-copy="gl-watch">Copy</button></span></label>`:''}
      <label class="fld">Cover image <span class="muted small">— the thumbnail on Discover → Streams</span></label>
      <img id="gl-img-prev" class="gl-img-prev hidden" alt="">
      <div class="gl-cover-acts"><button class="btn btn-ghost small" id="gl-img-pick"><svg class="ic b-ic" aria-hidden="true"><use href="#i-folder"></use></svg>Choose from your drive</button>
        <button class="btn btn-ghost small hidden" id="gl-img-clear"><svg class="ic b-ic" aria-hidden="true"><use href="#i-close"></use></svg>Clear</button></div>
      <input type="hidden" id="gl-img">
      <p class="muted small">Start OBS, then tap below to announce it on Nostr (Discover → Streams).</p>
      <div class="row gl-actions"><button class="btn btn-neon" id="gl-go" disabled><svg class="ic b-ic" aria-hidden="true"><use href="#i-live"></use></svg>Announce and Stream</button><button class="btn btn-ghost" id="gl-cancel">Close</button></div>
      <p class="muted small hidden" id="gl-need">↑ Pick what you want to broadcast first — the button turns on once you have.</p>`, root=>{
      const title=()=>($('#gl-title',root).value||'').trim()||'Live stream';
      const announce=()=>!!($('#gl-announce',root)||{}).checked;
      const cover=()=>($('#gl-img',root).value||'').trim();
      // Cover image: paste a URL or upload one to Blossom. Without it a stream renders as a bare ▶ tile,
      // which is why Discover → Streams looked empty.
      // Cover image: PICK ONE FROM YOUR BLOSSOM DRIVE. It used to be a URL box plus an upload button —
      // three ways to get this wrong (paste a dead link, upload a duplicate, or leave it blank and get
      // a bare ▶ tile on Discover → Streams). Your drive already holds the images you'd use.
      { const ip=$('#gl-img',root), pv=$('#gl-img-prev',root), pk=$('#gl-img-pick',root), cl=$('#gl-img-clear',root);
        const showPrev=()=>{ const u=cover();
          if(u){ pv.src=u; pv.classList.remove('hidden'); cl.classList.remove('hidden'); }
          else { pv.classList.add('hidden'); cl.classList.add('hidden'); } };
        // Default to your avatar — the same value _publishLive falls back to, but VISIBLE, so the
        // thumbnail isn't a surprise after you're already live.
        { const mine=(Store.profile(S.ME.pubkey)||{}).picture; if(mine && !ip.value) ip.value=mine; }
        showPrev();
        pv.onerror=()=>pv.classList.add('hidden');
        cl.onclick=()=>{ ip.value=''; showPrev(); };
        pk.onclick=()=> _pickBlossomImage(url=>{ ip.value=url; showPrev(); }); }
      $$('[data-copy]',root).forEach(b=> b.onclick=()=> _copyFrom($('#'+b.dataset.copy,root)));
      { const rc=$('#gl-record',root); if(rc) rc.onchange=()=>{ rc.disabled=true;
          _streamFetch('/api/streams/record',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({enabled:rc.checked})})
            // Keep `info` in step: _publishLive stamps it onto the stream so the end-of-stream message
            // only promises a recording when one is actually coming.
            .then(()=>{ info.record_enabled = rc.checked; toast(rc.checked?'recording on':'recording off'); })
            .catch(()=>{ toast('couldn’t save'); rc.checked=!rc.checked; })
            .finally(()=>{ rc.disabled=false; }); }; }
      // Quality: saved the moment you pick it, so it applies to the stream you are about to start (the
      // clamp reads the tier when the stream goes live, not when this sheet was opened).
      $$('input[name="gl-q"]',root).forEach(r=> r.onchange=()=>{
        const v=r.value;
        _streamFetch('/api/streams/quality',{method:'POST',headers:{'Content-Type':'application/json'},
          body:JSON.stringify({quality:v==='auto'?'':v})})
          .then(()=>toast(v==='auto'?'quality: node default':'quality: '+v+'p'))
          .catch(()=>toast('couldn\u2019t save the quality'));
      });
      $('#gl-cancel',root).onclick=closeModal;
      // Source is CHOSEN, never launched on click. Picking a source used to open the camera and start
      // publishing immediately, so the wrong lens was already broadcasting before you could switch it —
      // the reason "flip camera" existed at all. Nothing is captured until "Announce and Stream".
      const src=()=>{ const r=$('input[name="gl-src"]:checked',root); return r?r.value:''; };
      const facing=()=>{ const r=$('input[name="gl-facing"]:checked',root); return r?r.value:''; };
      const hasSrc=()=>!!$('input[name="gl-src"]',root);
      const sync=()=>{
        const s=src(), fw=$('#gl-facing',root);
        if(fw) fw.classList.toggle('hidden', s!=='cam');
        // No radios at all = a browser that can't capture (no WHIP/getUserMedia), so OBS is the ONLY
        // route and the button must stay live — gating on an unrendered choice locked those users out
        // of announcing entirely.
        const off = hasSrc() ? (!s || (s==='cam' && !facing())) : false;
        $('#gl-go',root).disabled = off;
        // …and SAY why it's off. A disabled neon button reads as a broken one: the sheet opens with no
        // source selected, so the first thing anyone does — type a title, hit Announce — is a click that
        // does nothing and explains nothing ("announce and stream button also does nothing").
        { const n=$('#gl-need',root); if(n) n.classList.toggle('hidden', !off); }
      };
      $$('input[name="gl-src"], input[name="gl-facing"]',root).forEach(r=> r.addEventListener('change', sync));
      sync();
      // Enter in the title is "go" — otherwise typing a title meant Tabbing past the checkboxes, the OBS
      // server/key boxes and their Copy buttons to reach the one button you actually wanted.
      $('#gl-title',root).addEventListener('keydown',e=>{
        if(e.key==='Enter' && !e.shiftKey && !e.ctrlKey && !e.metaKey && !e.altKey){ e.preventDefault(); $('#gl-go',root).click(); }
      });
      // Announcing is a relay round trip (sign → publish → the relay must have STORED it), so it is
      // never instant and can be slow. Without a busy state that is another button that "does nothing":
      // you click it again, and again, and each click signs and publishes another 30311. It says what
      // it's doing, refuses to run twice, and comes back to life if it fails.
      let _going=false;
      $('#gl-go',root).onclick=async(e)=>{
        if(_going) return; _going=true;
        const btn=e.currentTarget, was=btn.innerHTML;
        btn.disabled=true; btn.textContent='Announcing…';
        const revive=()=>{ _going=false; btn.innerHTML=was; sync(); };
        const t=title(), a=announce(), c=cover(), s=src()||'obs';
        if(s==='cam'){ const f=facing(); closeModal(); return _phoneGoLive(info, t, a, c, { facing:f }); }
        if(s==='screen'){ closeModal(); return _screenGoLive(info, t, a, c); }
        try{ const ev=await _publishLive(info, t, c); _startLiveHb();   // OBS path: HLS heartbeat detects OBS stopping
          if(a) await _announceStreamPost(info, t);
          closeModal(); toast('🔴 you’re live — announced on Nostr'); _afterGoLive(ev, false);
        }catch(_){ revive(); toast('couldn’t announce the stream — try again'); }
      };
    });
  }
  // Pick an image you already have on your Blossom drive. Used by Go Live's cover picker; kept
  // generic (takes a callback) so anything else needing "choose one of my images" can reuse it.
  // Filters to images by mime, newest first — the drive also holds encrypted app data and media the
  // user never picked as a picture, and showing those would be noise.
  //
  // THE SHEET OPENS BEFORE THE FIRST NETWORK AWAIT, and that is the whole point of the shape below.
  // It used to run the Files-index pull AND the /list fetch — neither of them time-bounded — and only
  // then call subModal(), so on a slow link "Choose from your drive" was a button that did NOTHING for
  // as long as those two took: no sheet, no spinner, no error. Reported as "the button no longer
  // works", which is exactly what it looks like, and it "eventually worked after many retries" because
  // the retry that landed was the one whose fetch came back. Now the sheet is up on the first frame and
  // the loading, the empty drive and the unreachable drive are three DIFFERENT things it says out loud.
  async function _pickBlossomImage(onPick){
    // One picker at a time. The sheet now opens instantly, so a double-click used to be a double SHEET
    // (the second one hiding a live first one). Asked of the DOM rather than a flag, because Escape and
    // the backdrop close the layer without telling us.
    if(($('#modal-root')||document).querySelector('.bp-modal')) return;
    const server=mediaServer();
    let imgs=[], cur='', ui=null;
    subModal(`<div class="bp-head"><h3><svg class="ic h-ic" aria-hidden="true"><use href="#i-image"></use></svg>Choose a cover</h3>
      <p class="muted small" id="bp-count">Reading your drive…</p>
      <div class="bp-folders hidden" id="bp-fwrap"><label class="bp-folderlbl">📁<select class="input bp-folder-sel" id="bp-fsel"></select></label></div></div>
      <div class="bp-grid" id="bp-grid"><div class="muted small">Loading your images…</div></div>
      <div class="row gl-actions"><button class="btn btn-ghost" id="bp-x">Cancel</button></div>`, (root, close)=>{
      root.classList.add('bp-modal');   // drops the modal's top padding so the sticky head can't jump
      ui={ root, close, grid:$('#bp-grid',root), cnt:$('#bp-count',root), fwrap:$('#bp-fwrap',root), fsel:$('#bp-fsel',root) };
      $('#bp-x',root).onclick=close;
      ui.fsel.onchange=()=>{ cur=ui.fsel.value||''; draw(); };
      // The cover grid is a grid like the emoji/effects pickers, so give it the SAME cursor: arrows (and
      // hjkl with Vim keys on) to move, Enter/Space to pick, Escape to close. It was mouse-only — Tab
      // reached the cells one at a time with no cursor to show where you were, across up to 180 of them.
      // `items()` is re-queried per keypress, so switching folder and redrawing the grid is handled
      // (including the cells not existing yet while the drive is still being read).
      // The folder <select> keeps the arrows for itself (_popKeys already exempts text fields and
      // selects), so ↑/↓ still changes folder while it has focus.
      _popKeys(root, '.bp-cell', el=>el.click(), close);
    });
    const alive=()=> !!(ui && ui.root && ui.root.isConnected);
    const say=(msg, count)=>{ if(!alive()) return; ui.cnt.textContent=count||''; ui.grid.innerHTML=`<div class="muted small">${enc(msg)}</div>`; };
    const urlOf=b=> b.url || (server+'/'+b.sha256);
    const draw=()=>{
      if(!alive()) return;
      const shown=imgs.filter(b=> cur==='' || (FilesIdx.folderOf(b.sha256)||'')===cur);
      ui.cnt.textContent = `${shown.length} image${shown.length===1?'':'s'}${cur?` in ${cur}`:' on your drive'} — tap one.`;
      ui.grid.innerHTML = shown.slice(0,180).map(b=>{
        const u=urlOf(b), nm=(FilesIdx.meta(b.sha256)||{}).name||'';
        return `<button type="button" class="bp-cell" data-url="${enc(u)}"${nm?` title="${enc(nm)}"`:''}>
          <img loading="lazy" src="${enc(u)}" alt="" onerror="this.closest('.bp-cell').remove()"></button>`;
      }).join('') || '<div class="muted small">Nothing in this folder.</div>';
      $$('.bp-cell',ui.grid).forEach(c=> c.onclick=()=>{ ui.close(); try{ onPick(c.dataset.url); }catch(_){ } });
    };

    // Folder names live in the encrypted Files index, which is only pulled when you open Files — pull
    // it here too or the picker would always show a flat, folderless drive. BOUNDED: a pull that hasn't
    // answered in 12s is treated exactly like one that failed (which this code already tolerated — it
    // falls back to the last index this device stored), because the alternative is the dead button.
    try{ FilesIdx.loadLocal(); }catch(_){ }
    try{ await Promise.race([ FilesIdx.ensure(), new Promise(r=>setTimeout(r, 12000)) ]); }catch(_){ }
    if(!alive()) return;   // closed while we were reading

    let list=null;
    try{ const r=await _fetchTimeout(server+'/list/'+S.ME.pubkey, {}, 20000); if(r.ok) list=await r.json(); }catch(_){ }
    if(!alive()) return;
    // "I could not ask" is not "you have nothing" — saying "no images yet" over an unreachable drive
    // sends you off to upload a picture you already have. Offer the retry instead.
    if(!list){
      say('Couldn’t reach your drive just now. Check your connection and try again.', '');
      const btn=document.createElement('button'); btn.className='btn btn-ghost small'; btn.textContent='Retry';
      btn.onclick=()=>{ ui.close(); _pickBlossomImage(onPick); };
      ui.grid.appendChild(btn);
      return;
    }
    // Only real images, and NEVER anything in an encrypted folder: those blobs are ciphertext, so a
    // viewer on another client would render a broken thumbnail. Newest first.
    imgs=(list||[])
      .filter(b=> /^image\//.test(b.type||''))
      .filter(b=>{ const m=FilesIdx.meta(b.sha256)||{}; return !m.enc && !FilesIdx.isEncFolder(FilesIdx.folderOf(b.sha256)); })
      .sort((a,b)=>(b.uploaded||0)-(a.uploaded||0));
    if(!imgs.length){
      say('There are no images on your Blossom drive yet. Upload one from Files (the 📁 group in the sidebar) and it’ll show up here.', '');
      return;
    }
    // Folders that actually contain a usable image (plus All), so you never tap into an empty one.
    const used=new Set(imgs.map(b=>FilesIdx.folderOf(b.sha256)||''));
    const folders=[['','🗂 All']].concat(
      FilesIdx.folders().filter(f=>!FilesIdx.isEncFolder(f) && used.has(f)).map(f=>[f,'📁 '+f]));
    if(folders.length>1){
      ui.fsel.innerHTML=folders.map(([v,l])=>`<option value="${enc(v)}">${enc(l)}</option>`).join('');
      ui.fwrap.classList.remove('hidden');
    }
    draw();
  }

  // Publish the NIP-53 kind-30311 "live" event + track it locally. Sets _liveStream FIRST so a relay
  // publish failure still leaves the stream trackable + endable (the End button / teardown still work).
  // ONE builder for the 30311's invariant tags. FOUR places re-sign this event (announce, viewer-count
  // update, end, and the parked end-sentinel) and it is REPLACEABLE — so a tag any one of them omits is
  // erased for every client. That's why `starts` is threaded through, and it's why `image` must be too:
  // set the cover once at go-live and the first viewer-count update would otherwise wipe it.
  function _liveBase(s){
    // NIP-53 `relays` tag: tells OTHER clients (zap.stream, Amethyst) which relays carry this stream's
    // kind-1311 chat. We publish/read chat on the local relay + STREAM_RELAYS (see _sendStreamChat /
    // the chat sub), but without advertising them here an external viewer subscribes to ITS OWN default
    // relays, finds nothing, and shows an empty chat even though messages are flowing. Must live in
    // _liveBase (not just the announce) — it's replaceable and re-signed in four places, so any omitter
    // would erase it for every client (same reason `image`/`starts` are threaded through).
    const chatRelays = Array.from(new Set(
      [ (S.CFG && S.CFG.relay_url) || '', ...STREAM_RELAYS ].map(u=>String(u||'').trim()).filter(Boolean)
    ));
    return [
      // ONE BROADCAST, ONE ADDRESS. This was `s.token` — the MediaMTX publish token, which is stable
      // for the user's whole life. kind 30311 is parameterized-REPLACEABLE, so every stream landed on
      // `30311:<pubkey>:<token>` and silently overwrote the previous one: last week's stream, its
      // recording link and its chat address all replaced by this week's. `s.d` is `<token>-<starts>`,
      // unique per go-live and still reducible to the token (_tokenOfD / stream_end_service.token_of).
      ['d', s.d || s.token], ['title', s.title], ['streaming', s.hls],
      ...(s.image ? [['image', s.image]] : []),
      ...(s.starts ? [['starts', s.starts]] : []),
      ['p', S.ME.pubkey, '', 'host'],
      ...(chatRelays.length ? [['relays', ...chatRelays]] : []),
    ];
  }
  async function _publishLive(info, title, image){
    _endedStreams.delete(info.token);   // going live again with this token — allow adoption once more
    const starts=String(Math.floor(Date.now()/1000));
    // Default the cover to your own avatar when none was given, so the announced event carries a real
    // `image` tag rather than relying on every client's fallback — zap.stream/Amethyst won't guess one.
    const cover=(image||'').trim() || (Store.profile(S.ME.pubkey)||{}).picture || '';
    // Prefer the id minted when the Go Live modal opened, so the watch link it already showed is the
    // one this stream actually publishes under. Fall back for callers that never opened that modal.
    _liveStream={ token:info.token, d:(info.d || (info.token+'-'+starts)), title, hls:info.hls_url, starts, image:cover,
                  // Only promise a replay when this node records AND this user opted in.
                  record: !!(info.record_available && info.record_enabled) };
    const r = await publish(30311, '', _liveBase(_liveStream).concat([['status','live']]));
    // If the relay didn't store the "live" event, the stream is ingesting but INVISIBLE on Nostr. Throw so the
    // go-live callers' existing catch surfaces "live — but couldn't announce yet" instead of a silent no-show.
    if(!(r && r.ok)) throw new Error('stream announce not stored');
    _mirrorStream(r);          // …and to the relays every OTHER client reads, or nobody can find it
    _parkEndSentinel(_liveStream);
    return r.ev;               // the event itself, so the caller can land you on your own stream
  }

  /* WHERE YOU LAND AFTER GOING LIVE.
   *
   * Going live used to leave you either on the Streams LIST (the OBS path) or staring at a
   * full-screen preview of your own face with nothing else reachable (the camera/screen paths) — so
   * the chat, the headcount and the share link, which are the entire reason to have a stream page,
   * were somewhere you had to go and find. Now the broadcast shrinks to its thumbnail and the app
   * opens YOUR stream.
   *
   * DESKTOP ONLY, deliberately. On a phone the full-screen overlay IS the interface — Stop, Mute,
   * Flip and Chat are its buttons, and shrinking it to a corner thumbnail to make room for a page
   * you did not ask for takes the controls away at the one moment you are most likely to need them.
   * Mobile keeps exactly what it had.
   *
   * `fromOverlay` distinguishes the two go-live routes: the camera/screen paths are holding a
   * full-screen self-view that has to be minimised first, the OBS path has no overlay at all (and
   * already navigated to the list on every platform, which is what mobile keeps doing). */
  function _afterGoLive(ev, fromOverlay){
    if(!isDesktop()){
      if(!fromOverlay) switchView('streams');
      return;
    }
    if(fromOverlay) _setMiniLive(true);
    switchView('streams');          // creates/focuses the Streams window in desktop mode
    if(ev) openStream(ev);          // …then land on the stream itself, not the list
  }
  // Ghost-LIVE guard. Our 30311 can only be signed HERE (the key never leaves the browser), so if this tab
  // dies mid-stream — closed, crashed, phone asleep — nothing would ever mark the stream ended and it would
  // sit "● LIVE" forever pointing at a dead HLS URL. So sign the "ended" twin NOW and park it with the
  // server, which publishes it once MediaMTX reports the feed is gone (app/services/stream_end_service.py).
  // No `ends` tag: this signature is fixed at go-live, so any end time it named would be a lie. _endLive()
  // still stamps an accurate one on the normal path, and being newer it wins the replaceable-event race.
  async function _parkEndSentinel(s){
    if(!s || !s.token) return;
    try{
      const ev=await S.signer.signEvent({ kind:30311, content:'', pubkey:S.ME.pubkey,
        created_at: Math.floor(Date.now()/1000)+5,   // must outrank the "live" event we just published
        tags: _liveBase(s).concat([['status','ended'], ['client','PosterChan AI']]) });
      const res=await _streamFetch('/api/streams/sentinel', { method:'POST', headers:{'Content-Type':'application/json'},
                                                              body: JSON.stringify({ event: ev }) });
      // fetch only rejects on a network error, so a 401/403/400 would sail through unnoticed and leave the
      // ghost-LIVE hole wide open while looking fine. Say so.
      if(!res || !res.ok) throw new Error('sentinel rejected: '+((res&&res.status)||'no response'));
    }catch(e){ console.warn('could not park the end-of-stream event', e);
      toast('heads up: if this tab closes mid-stream, the stream may stay marked LIVE'); }
  }
  function _clearEndSentinel(){   // we ended it ourselves — the server's fallback isn't needed
    try{ _streamFetch('/api/streams/sentinel', { method:'DELETE' }); }catch(_){}
  }
  // Optional kind-1 announcement to followers, carrying a WATCH LINK that actually works.
  //
  // A bare `nostr:naddr` was NOT that link. Other clients resolve a kind-30311 naddr by handing the viewer off
  // to zap.stream — which then can't play the stream (it reads the `streaming` url, which lives on OUR
  // instance) — and anyone without a Nostr client sees an opaque `nostr:naddr1…` string and nothing to click.
  // So lead with a plain https link to the stream on this instance (poster.place/<naddr>, which the client
  // resolves straight into the player) and keep the naddr AFTER it, for native clients that embed it properly.
  async function _announceStreamPost(info, title){
    try{
      const relays=[S.CFG && S.CFG.relay_url].filter(Boolean);   // include our relay so external clients can resolve the naddr
      const _d = info.d || info.token;   // same address the 30311 was published under
      const naddr=NT().nip19.naddrEncode({identifier:_d, pubkey:S.ME.pubkey, kind:30311, relays});
      await publish(1, `🔴 I’m live now: ${title}\n\n▶ Watch: ${_webLink(naddr)}\n\nnostr:${naddr}`,
        [['t','livestream'], ['a', `30311:${S.ME.pubkey}:${_d}`, '', 'root']]);
    }catch(_){}
  }
  // WebRTC gathers ICE, then we send the complete offer (non-trickle WHIP). Bounded so it never hangs;
  // cleans up its listener + timeout on every path.
  function _iceGatherComplete(pc){
    return new Promise(res=>{
      if(pc.iceGatheringState==='complete') return res();
      let done=false, to=null;
      const finish=()=>{ if(done) return; done=true; if(to) clearTimeout(to); pc.removeEventListener('icegatheringstatechange',chk); res(); };
      function chk(){ if(pc.iceGatheringState==='complete') finish(); }
      pc.addEventListener('icegatheringstatechange',chk); to=setTimeout(finish, 3000);
    });
  }
  // Go live straight from the phone/browser camera via WHIP (WebRTC ingest to the built-in MediaMTX).
  async function _phoneGoLive(info, title, doAnnounce, cover, opts){
    if(_liveStream || _phoneStream || _goingLive){ toast('you’re already live'); return; }
    _goingLive=true;   // set BEFORE the awaits so a second tap can't open a 2nd camera/PeerConnection
    const wantScreen = !!(opts && opts.screen);   // desktop "go live sharing this screen" — same WHIP publish
    // Which camera the user PICKED in Go Live, before anything was captured. Defaulting to 'user' and
    // letting them flip afterwards meant the wrong camera was already broadcasting by the time they could.
    let local=null, pc=null; let facing=(opts && opts.facing) || 'user';
    try{
      if(wantScreen){
        const disp=await navigator.mediaDevices.getDisplayMedia({video:{width:{ideal:1920},height:{ideal:1080}},audio:false});
        // _soleVideoTrack drops the system-audio track some Chromium builds hand back despite audio:false —
        // otherwise that capture leaks for the life of the page.
        const vt=_soleVideoTrack(disp);
        if(!vt) throw new DOMException('no screen track','NotFoundError');
        // Voiceover is a SEPARATE capture and deliberately optional: a screen share with no mic is still
        // worth broadcasting, so a refused/absent microphone must not abort going live.
        let mic=null; try{ mic=await navigator.mediaDevices.getUserMedia({audio:true}); }catch(_){}
        local=new MediaStream(mic ? [vt, ...mic.getAudioTracks()] : [vt]);
      } else {
        local=await navigator.mediaDevices.getUserMedia({video:{width:{ideal:1280},height:{ideal:720},facingMode:facing},audio:true});
      }
    }
    // Dismissing the screen picker throws NotAllowedError — the same name a BLOCKED camera throws — and
    // _mediaErrMsg's "Camera/mic blocked, click the 🔒 icon in the address bar" is nonsense for a picker
    // the user simply closed. Say what actually happened.
    catch(e){
      /* A display-capture NotFoundError is NOT "no camera or microphone found". That generic
       * formatter sent PosterChanOS users looking for a camera after they had explicitly selected
       * Record screen, while the thing that actually failed was the PipeWire/portal source. Keep
       * camera wording on the camera path and make the screen failure actionable. */
      const kind=e&&e.name;
      toast(wantScreen
        ? (kind==='NotAllowedError' ? 'screen share cancelled'
          : kind==='NotFoundError' ? 'no screen capture source was found — restart the PosterChanOS session; capture details are in screen-share.log'
          : 'screen share could not start' + (e&&e.message ? ': '+e.message : ''))
        : _mediaErrMsg(e));
      _goingLive=false; return; }
    toast('connecting…');
    try{
      let ice=[]; try{ const c=await _fetchIceServers(); ice=c.iceServers||[]; }catch(_){}
      pc=new RTCPeerConnection({iceServers:ice, iceCandidatePoolSize:1});
      local.getTracks().forEach(t=>pc.addTrack(t,local));
      // Prefer H264 for the video: MediaMTX can only remux H264/H265/AV1 to HLS, so a VP8 stream is
      // unwatchable (no playlist). The server also enforces this by munging the offer, but setting it here
      // means the phone hardware-encodes H264 from the start. Best-effort — unsupported browsers just skip it.
      _preferH264(pc);   // shared with calls — one implementation, so the two cannot drift apart
      const offer=await pc.createOffer(); await pc.setLocalDescription(offer);
      await _iceGatherComplete(pc);
      try{ await ensureAiSession(); }catch(_){}
      const r=await fetch(info.whip_url, { method:'POST',
        headers:Object.assign({'Content-Type':'application/sdp'}, S._aiToken?{'Authorization':'Bearer '+S._aiToken}:{}),
        body:pc.localDescription.sdp });
      if(!r.ok) throw new Error('stream server rejected the connection (WHIP '+r.status+')');
      await pc.setRemoteDescription({type:'answer', sdp:await r.text()});
      // Bandwidth saving: the browser already hardware-encodes; cap the upload bitrate/framerate so a
      // phone on mobile data doesn't blast a huge stream (and viewers get a lighter feed).
      try{ const vs=pc.getSenders().find(s=>s.track&&s.track.kind==='video');
        if(vs){ const pr=vs.getParameters(); pr.encodings=(pr.encodings&&pr.encodings.length)?pr.encodings:[{}];
          pr.encodings[0].maxBitrate=1500000; pr.encodings[0].maxFramerate=30; await vs.setParameters(pr); } }catch(_){}
    }catch(e){
      console.error('go-live: media connection failed', e);
      try{pc&&pc.close();}catch(_){} try{local.getTracks().forEach(t=>t.stop());}catch(_){} _goingLive=false;
      const why=(e&&e.message)||'media connection failed';
      toast('couldn’t go live — '+why);
      return;
    }
    let devId=null; try{ const s=local.getVideoTracks()[0].getSettings(); devId=s.deviceId||null; if(s.facingMode) facing=s.facingMode; }catch(_){}
    // `info` is kept so a later switch to the native screen share can build its RTMP target from the same
    // ingest (same token ⇒ same kind-30311, so viewers never have to re-open the stream).
    _phoneStream={ pc, local, token:info.token, facing, deviceId:devId, source:wantScreen?'screen':'camera', info };
    // The browser's own "Stop sharing" bar ends the display track directly. Same fallback the mid-stream
    // swap installs: drop back to the camera so the broadcast survives, and end it if there's no camera.
    if(wantScreen){ try{ local.getVideoTracks()[0].onended=()=>_onScreenShareEnded(_phoneStream); }catch(_){} }
    // Liveness for a phone stream is the WebRTC connection itself — NOT the HLS heartbeat (which 404s
    // during the WebRTC→HLS remux warm-up and would falsely kill a healthy stream). End if the pc drops.
    pc.onconnectionstatechange=()=>{ if(_phoneStream && _phoneStream.pc===pc && (pc.connectionState==='failed'||pc.connectionState==='closed')) _endLive(); };
    _goingLive=false;
    let _ev=null;
    try{ _ev=await _publishLive(info, title, cover); }catch(_){ toast('live — but couldn’t announce on Nostr yet'); }
    if(doAnnounce){ try{ await _announceStreamPost(info, title); }catch(_){} }
    _phoneLiveOverlay();
    toast(wantScreen ? '🔴 you’re live — sharing your screen'
                     : (isDesktop() ? '🔴 you’re live from your webcam' : '🔴 you’re live from your phone'));
    _afterGoLive(_ev, true);
  }
  function _phoneLiveOverlay(){
    let el=document.getElementById('phone-live'); if(el) el.remove();
    el=document.createElement('div'); el.id='phone-live'; el.className='phone-live';
    el.innerHTML=`<div class="pl-badge">● LIVE</div><div class="pl-viewers" id="pl-viewers">👁 0</div>
      <video id="pl-vid" autoplay playsinline muted></video>
      <div class="pl-note" id="pl-note" hidden>🖥 Sharing your screen</div>
      <div class="pl-actions"><button class="btn btn-ghost" id="pl-min"><svg class="ic b-ic" aria-hidden="true"><use href="#i-minimize"></use></svg>Minimize</button><button class="btn btn-ghost" id="pl-chat"><svg class="ic b-ic" aria-hidden="true"><use href="#i-chat"></use></svg>Chat</button><button class="btn btn-ghost" id="pl-mute"><svg class="ic b-ic" aria-hidden="true"><use href="#i-mic"></use></svg>Mute</button><button class="btn btn-ghost" id="pl-flip"><svg class="ic b-ic" aria-hidden="true"><use href="#i-refresh"></use></svg>Flip camera</button><button class="btn btn-ghost" id="pl-screen"><svg class="ic b-ic" aria-hidden="true"><use href="#i-tv"></use></svg>Share screen</button><button class="btn btn-neon" id="pl-stop"><svg class="ic b-ic" aria-hidden="true"><use href="#i-stop"></use></svg>Stop streaming</button></div>`;
    document.body.appendChild(el);
    _startViewerPoll();
    // A native screen share is captured OUTSIDE the WebView (MediaProjection), so there's no MediaStream to
    // preview here — previewing it in-app would be a hall of mirrors anyway. Show a placard instead.
    const v=$('#pl-vid',el);
    if(v && _phoneStream && !_phoneStream.native){ v.srcObject=_phoneStream.local; v.play&&v.play().catch(()=>{}); }
    $('#pl-stop',el).onclick=e=>{ e.stopPropagation(); _endLive(); };
    $('#pl-flip',el).onclick=e=>{ e.stopPropagation(); _flipCamera(); };
    // Read the chat WITHOUT dropping the broadcast: the overlay is full-screen, so the only way to reach the
    // stream's chat used to be to stop streaming. Minimize to the thumbnail (the PeerConnection / capture
    // service are untouched by this — the broadcast lives outside the DOM) and open the stream view.
    $('#pl-chat',el).onclick=e=>{ e.stopPropagation(); _setMiniLive(true); switchView('streams'); };
    $('#pl-mute',el).onclick=e=>{ e.stopPropagation(); _toggleMute(); };
    // The button is ALWAYS shown: hiding it on a browser without getDisplayMedia (mobile Chrome/Firefox, iOS
    // Safari — none of them implement screen capture) just looks like the feature is missing/broken. Say why.
    $('#pl-screen',el).onclick=e=>{ e.stopPropagation();
      if(!_canScreenShare()){ toast('this browser can’t share a screen — mobile browsers don’t allow screen capture; use the PosterChan app, or go live from a desktop browser'); return; }
      _toggleScreen(); };
    // Minimize to a floating thumbnail so the app stays usable WHILE broadcasting. This is purely visual —
    // the broadcast lives in the PeerConnection, not the DOM, so shrinking the overlay never interrupts it.
    $('#pl-min',el).onclick=e=>{ e.stopPropagation(); _setMiniLive(true); };
    el.onclick=()=>{ if(el.classList.contains('pl-mini')) _setMiniLive(false); };   // tap the thumbnail to come back
    _syncOverlayButtons();
  }
  // Mute the mic mid-broadcast, without interrupting the video. Two different mutes under one button: a camera
  // stream's audio is a WebRTC track we can simply disable (the sender keeps running, it just sends silence),
  // while a native screen share's mic is captured outside the WebView, so the plugin has to do it.
  async function _toggleMute(){
    const ps=_phoneStream; if(!ps) return;
    const want=!ps.muted;
    if(ps.native){
      const SS=_capPlugin('ScreenShare','setMuted'); if(!SS){ toast('can’t mute this stream'); return; }
      try{ const r=await SS.setMuted({ muted:want }); ps.muted=!!(r&&r.muted); }
      catch(_){ toast('couldn’t mute the mic'); return; }
    }else{
      const t=(ps.local&&ps.local.getAudioTracks&&ps.local.getAudioTracks()[0])||null;
      if(!t){ toast('this stream has no mic'); return; }
      t.enabled=!want; ps.muted=want;
    }
    const b=$('#pl-mute'); if(b) b.textContent=ps.muted?'🔇 Unmute':'🎤 Mute';
    toast(ps.muted?'mic muted — viewers can’t hear you':'mic on');
  }
  // How many people are actually watching. MediaMTX counts the readers on the path; the app just asks. Also
  // written back to the kind-30311 as `current_participants` (the NIP-53 tag other clients read to show
  // "N watching"), but only when it CHANGES — the event is replaceable, and re-signing it on a timer for an
  // unchanged number would be pointless relay churn.
  let _viewerPoll=null, _viewersLast=-1;
  function _startViewerPoll(){
    _stopViewerPoll(); _viewersLast=-1;
    const tick=async()=>{
      const s=_liveStream||_phoneStream; if(!s||!s.token) return;
      let n=0;
      try{ const r=await fetch('/api/streams/viewers/'+encodeURIComponent(s.token)); if(!r.ok) return; n=(await r.json()).viewers||0; }
      catch(_){ return; }
      // Two places show YOUR headcount: the phone broadcast overlay, and the Streams bar — which is the
      // only one a desktop/OBS broadcaster ever sees (there's no overlay on that path).
      { const el=document.getElementById('pl-viewers'); if(el) el.textContent='👁 '+n; }
      { const sv=document.getElementById('stream-viewers'); if(sv) sv.textContent=`👁 ${n} watching`; }
      // The stream DETAIL view's av-by count (#st-viewers) is rendered ONCE from the cached event's
      // current_participants and never refreshes — so the streamer saw a stale "0 watching" even as the
      // poll updated the badge. Keep it live off the same poll (n is the real headcount).
      { const stv=document.getElementById('st-viewers'); if(stv) stv.textContent=` · 👁 ${n} watching`; }
      if(n!==_viewersLast){ _viewersLast=n; _publishViewers(n); }
    };
    tick(); _viewerPoll=setInterval(tick, 20000);
  }
  function _stopViewerPoll(){ if(_viewerPoll){ clearInterval(_viewerPoll); _viewerPoll=null; } }
  // Re-sign the live 30311 with the current headcount, so viewers on ANY NIP-53 client (zap.stream, Amethyst)
  // see it too — not just this app. Best-effort: a failure here must never disturb the broadcast.
  async function _publishViewers(n){
    const s=_liveStream; if(!s||!s.token||!S.ME) return;
    try{ const r=await publish(30311, '', _liveBase(s).concat([['status','live'], ['current_participants', String(n)]]));
         _mirrorStream(r); }catch(_){ return; }
    // The stream can END while that publish is in flight. Re-parking afterwards would POST a fresh sentinel
    // (created_at = now+5, carrying NO `ends`) for a stream that's already over — outranking the accurate
    // ended event _endLive just published and erasing its end time. Only re-park if this is STILL the live
    // stream we started with.
    if(_liveStream!==s) return;
    // CRITICAL: the parked "ended" sentinel was signed at go-live with created_at = start+5, and this
    // republish is NEWER — so on a replaceable address it now outranks the sentinel, and the server's
    // ghost-LIVE fallback would be dropped by relays as stale. The stream would sit "● LIVE" forever over a
    // dead url if this tab died. Re-park a sentinel that outranks what we just published.
    await _parkEndSentinel(s);
  }
  // NB: the class is `pl-mini`, NOT `mini` — `.mini` is the global small-button class (and the winxp theme
  // overrides it with !important), which would hijack the thumbnail's styling.
  function _setMiniLive(on){
    const el=document.getElementById('phone-live'); if(!el) return;
    el.classList.toggle('pl-mini', !!on);
    if(on) toast('still live — tap the thumbnail to come back');
  }
  // Reflect the current source in the overlay: flip only makes sense for the camera, and the screen button
  // is a toggle back to camera while sharing.
  function _syncOverlayButtons(){
    const ps=_phoneStream; if(!ps) return;
    const screen=ps.source==='screen';
    const fb=$('#pl-flip'); if(fb) fb.style.display=screen?'none':'';   // flip is camera-only
    const sb=$('#pl-screen');
    if(sb){
      // The native (MediaProjection) share is a different transport — RTMP, captured outside the WebView — so
      // there's no track to swap back to the camera in place. Hide the toggle rather than offer one that would
      // have to tear the stream down to honour it; Stop and go live again to use the camera.
      if(ps.native) sb.hidden=true;
      else { sb.hidden=false; sb.textContent=screen?'📷 Camera':'🖥 Share screen'; }
    }
    const note=$('#pl-note'); if(note) note.hidden=!ps.native;
    const v=$('#pl-vid'); if(v){ v.hidden=!!ps.native;
      // Mirror ONLY the front (selfie) camera — a screen share (or the rear cam) must never be flipped.
      v.classList.toggle('rear', screen || ps.facing==='environment'); }
  }
  let _swapping=false;
  // Commit an acquired video track onto the live stream: replaceTrack() it onto the existing WHIP sender
  // (viewers see a seamless cut, no renegotiation), then swap the preview + stop the old track. Audio and the
  // peer connection are untouched. `apply(nt)` records source-specific state once the swap commits.
  // Once replaceTrack commits, nt (now the outgoing track) is never stopped by us.
  async function _commitPhoneVideo(ps, nt, apply){
    const sender=ps.pc.getSenders().find(s=>s.track&&s.track.kind==='video');
    if(!sender){ try{ nt.stop(); }catch(_){} return false; }
    try{ await sender.replaceTrack(nt); }
    catch(_){ try{ nt.stop(); }catch(_){} return false; }
    // Committed. If the stream ended mid-swap, _endLive already closed the pc; just drop this orphan.
    if(_phoneStream!==ps){ try{ nt.stop(); }catch(_){} return false; }
    try{ const old=ps.local.getVideoTracks()[0];
      if(old && old!==nt){ try{ old.onended=null; }catch(_){}   // clear any source's end-handler before WE stop it, so an intentional swap doesn't trigger a fallback
        ps.local.removeTrack(old); try{ old.stop(); }catch(_){} }
      if(!ps.local.getVideoTracks().includes(nt)) ps.local.addTrack(nt); }catch(_){}
    try{ apply && apply(nt); }catch(_){}
    const v=$('#pl-vid'); if(v){ v.srcObject=ps.local; v.play&&v.play().catch(()=>{}); }
    _syncOverlayButtons();
    return true;
  }
  // Take the first video track from an acquired stream and stop every other one. getDisplayMedia can return a
  // system/tab AUDIO track even with audio:false (some Chromium builds); we only use the video track, so that
  // capture would otherwise leak for the page's life.
  function _soleVideoTrack(ns){
    const nt=ns && ns.getVideoTracks()[0];
    try{ ns && ns.getTracks().forEach(t=>{ if(t!==nt){ try{ t.stop(); }catch(_){} } }); }catch(_){}
    return nt||null;
  }
  // Swap the OUTGOING video track without renegotiating. Returns 'busy' if another swap is in flight, false on
  // failure/cancel, true on success — callers (the screen-end fallback) rely on this to recover instead of
  // freezing on a dead track. Hardened against a concurrent _endLive: every await is followed by a
  // `_phoneStream===ps` re-check, and a stream that ended mid-acquire has its orphan track stopped (else the
  // camera/screen-capture stays live with no UI to end it — a privacy leak until reload).
  //
  // opts.release — camera→camera swaps only. MOST PHONES CANNOT HOLD TWO CAMERAS OPEN AT ONCE: the live track
  // owns the hardware, so acquiring the other one fails (NotReadableError/OverconstrainedError/NotFoundError)
  // while we're still streaming from it — that's the "couldn't switch video" flip failure. We still TRY the
  // concurrent acquire first (seamless where it works: desktops, some Androids), and only on failure release
  // the outgoing track and retry. If that retry also fails we must put the ORIGINAL camera back, or we've
  // killed the only video source and the broadcast is stuck on a dead track with no way out but Stop.
  async function _swapPhoneVideo(acquire, apply, opts){
    if(!_phoneStream || _swapping) return 'busy';
    _swapping=true;
    const silent=!!(opts&&opts.silent);   // probe mode: caller has a fallback, so don't toast on failure
    const release=!!(opts&&opts.release);
    const ps=_phoneStream;
    const done=(v,t)=>{ if(t && !silent) toast(t); _swapping=false; return v; };
    const fatal=e=>e&&(e.name==='NotAllowedError'||e.name==='AbortError');   // user cancelled/denied — a retry can't help
    const nocam=e=>e&&e.name==='PcNoCamera';                                 // caller already knows there's nothing to switch to
    let ns=null;
    try{ ns=await acquire(); }
    catch(e){
      if(fatal(e)) return done(false, '');
      if(nocam(e)) return done(false, 'no other camera found');
      if(!release || _phoneStream!==ps) return done(false, 'couldn’t switch video');
      // Free the camera, then retry. Viewers freeze on the last frame for the moment this takes; audio is
      // unaffected (separate track), and the sender keeps its now-dead track until we replace it.
      const old=ps.local.getVideoTracks()[0];
      if(!old) return done(false, 'couldn’t switch video');
      try{ old.onended=null; }catch(_){}
      try{ old.stop(); }catch(_){}
      try{ ns=await acquire(); }catch(_){ ns=null; }
      if(_phoneStream!==ps){ try{ ns&&ns.getTracks().forEach(t=>t.stop()); }catch(_){} return done(false); }
      if(!ns){
        const back=await _reacquireCamera(ps);
        if(back && await _commitPhoneVideo(ps, back, nt=>_applyCamera(nt, ps.facing, ps.deviceId))) return done(false, 'couldn’t switch video');
        // The camera is gone entirely — end the broadcast rather than leave viewers on a frozen dead frame.
        _swapping=false; toast('camera unavailable — stream ended'); _endLive(); return false;
      }
    }
    const nt=_soleVideoTrack(ns);
    if(_phoneStream!==ps || !nt){ try{ ns&&ns.getTracks().forEach(t=>t.stop()); }catch(_){} return done(false); }
    if(!await _commitPhoneVideo(ps, nt, apply)) return done(false, _phoneStream===ps ? 'couldn’t switch video' : '');
    return done(true);
  }
  // Re-open the camera we were just streaming from (used to undo a failed release-and-retry flip). `ideal`, not
  // `exact`, so a device that renumbered its cameras degrades to *a* camera instead of failing outright.
  async function _reacquireCamera(ps){
    const tries=[];
    if(ps.deviceId) tries.push(_camById(ps.deviceId,false));
    tries.push(_camByFacing(ps.facing||'user',false));
    tries.push({video:true, audio:false});
    for(const c of tries){
      try{ const s=await navigator.mediaDevices.getUserMedia(c); const t=_soleVideoTrack(s); if(t) return t; }catch(_){}
    }
    return null;
  }
  const _SIZE={width:{ideal:1280},height:{ideal:720}};
  const _camByFacing=(facing,exact)=>({video:Object.assign({}, _SIZE, {facingMode: exact?{exact:facing}:{ideal:facing}}), audio:false});
  const _camById=(id,exact)=>({video:Object.assign({}, _SIZE, {deviceId: exact?{exact:id}:{ideal:id}}), audio:false});
  // Record which camera we actually landed on. Trust getSettings() first, but fall back to what we ASKED for
  // — a device that reports no facingMode would otherwise keep a stale `facing` and render the rear camera
  // mirrored (and later restore the wrong camera).
  function _applyCamera(nt, wantFacing, wantId){
    const ps=_phoneStream; if(!ps) return;
    const s=(nt.getSettings&&nt.getSettings())||{};
    ps.source='camera';
    ps.deviceId = s.deviceId || wantId || ps.deviceId;
    ps.facing = s.facingMode || wantFacing || ps.facing;
  }
  // Restore the camera after a screen share. Prefer the EXACT camera that was live before (by deviceId,
  // `ideal` so a vanished device degrades instead of failing) — falling back to facing alone would drop the
  // user onto the selfie cam when they'd been on the rear one.
  function _toCamera(want){
    const ps=_phoneStream; const id=ps&&ps.deviceId;
    return _swapPhoneVideo(()=>navigator.mediaDevices.getUserMedia(id?_camById(id,false):_camByFacing(want,false)),
                           nt=>_applyCamera(nt, want, id||null));
  }
  // The next camera along, by deviceId — for devices where facingMode is ignored (tablets/laptops: the "flip
  // did nothing on my tablet" bug). Null when there's genuinely nothing to switch to.
  async function _otherCamId(ps){
    try{
      const cams=(await navigator.mediaDevices.enumerateDevices()).filter(d=>d.kind==='videoinput');
      if(cams.length<2) return null;
      const i=cams.findIndex(c=>c.deviceId && c.deviceId===ps.deviceId);
      // Unknown current camera → just take one that isn't the one we think we're on.
      return i<0 ? (cams.find(c=>c.deviceId && c.deviceId!==ps.deviceId)||cams[0]).deviceId
                 : cams[(i+1)%cams.length].deviceId;
    }catch(_){ return null; }
  }
  // Name the camera we just landed on. A flip that "did nothing" is indistinguishable from a flip that
  // worked-but-looks-similar (two cameras pointing at the same room), so always say which one we're on.
  async function _toastCamera(fallbackFacing){
    const ps=_phoneStream; if(!ps) return;
    let label='';
    try{ const cams=await navigator.mediaDevices.enumerateDevices();
      const c=cams.find(d=>d.kind==='videoinput' && d.deviceId && d.deviceId===ps.deviceId);
      label=(c&&c.label)||''; }catch(_){}
    if(!label) label=((ps.facing||fallbackFacing)==='environment')?'rear camera':'front camera';
    toast('📷 '+label);
  }
  // Flip cameras — front ↔ rear. The acquire thunk is the whole preference ladder, so _swapPhoneVideo can
  // re-run it verbatim after releasing the camera (see opts.release — the phone case, where the other camera
  // can't be opened while this one is live).
  // 1) Ask for the opposite facingMode with `exact`. On a phone that's a deterministic ONE-TAP selfie↔rear.
  //    `exact` matters twice over: it won't silently hand back the same camera, and its FAILURE is precisely
  //    how we detect a device that doesn't really implement facingMode.
  // 2) Only then cycle by deviceId. Doing this FIRST would be wrong: phones expose several rear lenses
  //    (wide/ultrawide/tele/depth), so blind deviceId cycling turns Flip into a lens carousel.
  async function _flipCamera(){
    const ps=_phoneStream; if(!ps) return;
    const want = ps.facing==='environment' ? 'user' : 'environment';
    let landedId=null;
    const acquire = async () => {
      landedId=null;
      try{ return await navigator.mediaDevices.getUserMedia(_camByFacing(want,true)); }
      catch(e){ if(e&&(e.name==='NotAllowedError'||e.name==='AbortError')) throw e; }
      const nextId=await _otherCamId(ps);
      if(!nextId){ const e=new Error('no other camera'); e.name='PcNoCamera'; throw e; }
      landedId=nextId;
      return navigator.mediaDevices.getUserMedia(_camById(nextId,true));
    };
    // On the deviceId path we can't read the new camera's facing, so `want` is the assumption — which keeps the
    // mirror (and a later restore) right on a normal 2-camera tablet.
    const r = await _swapPhoneVideo(acquire, nt=>_applyCamera(nt, want, landedId), {release:true});
    if(r===true) _toastCamera(want);
    return r;
  }
  // ---------- native screen share (Android app) ----------
  // NO mobile browser implements getDisplayMedia — not Chrome for Android, not the WebView the app runs in, not
  // iOS Safari. So on the phone the screen cannot reach getUserMedia/WebRTC at all, and the WHIP path the camera
  // uses is simply not available to it. The app therefore captures the screen NATIVELY (MediaProjection, in a
  // small Capacitor plugin) and pushes H264/AAC over RTMP to the SAME MediaMTX ingest OBS publishes to, using
  // the same per-user stream key. Everything else is untouched: same token, same kind-30311, same end-sentinel.
  const _screenPlugin=()=>_capPlugin('ScreenShare','start');
  let _screenListener=null;
  let _screenPending=null;   // armed while we're waiting for the FIRST 'connected' of a native share
  const _CONNECT_TIMEOUT=25000;
  // The plugin's start() resolves as soon as the capture service is launched — it does NOT wait for the RTMP
  // connect, which can still fail (bad key, encoder refused the screen size, MediaProjection denied). Those
  // arrive later as status events, so a share isn't "live" until 'connected' lands. Arm this BEFORE start()
  // so an immediate failure can't slip through between the two.
  function _armScreenConnect(){
    let settle;
    const p=new Promise((res,rej)=>{ settle={res,rej}; });
    const to=setTimeout(()=>{ if(_screenPending===settle){ _screenPending=null; settle.rej(new Error('the screen stream didn’t connect')); } }, _CONNECT_TIMEOUT);
    settle.done=()=>clearTimeout(to);
    _screenPending=settle;
    return p;
  }
  function _watchNativeScreen(SS){
    if(_screenListener) return;
    try{
      _screenListener=SS.addListener('screenShareStatus', s=>{
        const ev=(s&&s.event)||'', msg=(s&&s.message)||'';
        // Still handshaking: settle the go-live instead of consulting _phoneStream, which isn't assigned yet.
        // (Dropping these was how a stream that never carried a frame still got announced as LIVE.)
        if(_screenPending){
          const p=_screenPending;
          if(ev==='connected'){ _screenPending=null; p.done(); p.res(); }
          else if(ev==='stopped' || ev==='error'){ _screenPending=null; p.done(); p.rej(new Error(msg||'screen sharing stopped')); }
          return;   // 'connecting'/'reconnecting' — keep waiting
        }
        const ps=_phoneStream; if(!ps || !ps.native) return;
        if(ev==='reconnecting') toast('screen stream dropped — reconnecting…');
        // 'stopped' is the system's OWN stop (the status-bar chip, or the screen locking on Android 15) — the
        // capture is gone and no JS ran. Without this the app keeps saying LIVE over a dead feed.
        else if(ev==='stopped' || ev==='error'){
          if(ev==='error' && msg) toast('screen share failed: '+msg);
          _endLive();
        }
      });
    }catch(_){ _screenListener=null; }
  }
  function _dropNativeScreen(){
    const h=_screenListener; _screenListener=null;
    if(_screenPending){ const p=_screenPending; _screenPending=null; p.done(); p.rej(new Error('screen sharing stopped')); }
    // addListener resolves to the handle (Capacitor returns a promise), so unwrap before removing.
    try{ Promise.resolve(h).then(x=>{ try{ x && x.remove && x.remove(); }catch(_){} }).catch(()=>{}); }catch(_){}
  }
  // A screen capture lives in a foreground service, OUTSIDE the WebView — so it outlives the page. If Android
  // killed the Activity mid-share (memory pressure, task swiped away), we come back with no _phoneStream and
  // no way to reach that capture: the user's screen would keep being broadcast with nothing in the app saying
  // so. Stop any capture we've lost track of, on boot.
  async function _reconcileNativeScreen(){
    const SS=_capPlugin('ScreenShare','isStreaming'); if(!SS) return;
    try{
      const r=await SS.isStreaming();
      if(r && r.value && !_phoneStream){ try{ await SS.stop(); }catch(_){} toast('a screen share was still running — stopped it'); }
    }catch(_){}
  }
  // Go live straight from the screen (app only). No WebRTC involved — the plugin owns the whole feed — so the
  // stream's liveness is reported by the plugin's status events rather than a PeerConnection.
  async function _screenGoLive(info, title, doAnnounce, cover){
    if(_liveStream || _phoneStream || _goingLive){ toast('you’re already live'); return; }
    const SS=_screenPlugin();
    // Desktop browser: no plugin, but getDisplayMedia + the WHIP ingest do the same job. It's the webcam
    // path with a different capture, so hand off rather than duplicate the PeerConnection/WHIP dance.
    if(!SS){
      if(navigator.mediaDevices && navigator.mediaDevices.getDisplayMedia)
        return _phoneGoLive(info, title, doAnnounce, cover, { screen:true });
      toast('this browser can’t share a screen — use the PosterChan app, or a desktop browser');
      return;
    }
    if(!info.rtmp_native_url){ toast('this server is too old for in-app screen sharing'); return; }
    _goingLive=true;
    let granted=false;
    try{ const r=await SS.requestConsent(); granted=!!(r && r.granted); }
    catch(e){ _goingLive=false; toast((e&&e.message)||'couldn’t start screen sharing'); return; }
    if(!granted){ _goingLive=false; toast('screen sharing cancelled'); return; }
    _watchNativeScreen(SS);
    const connected=_armScreenConnect();
    // A failed start can still have left the capture service up — stop it, or the screen keeps being
    // recorded with nothing on screen to end it.
    try{ await SS.start({ url:info.rtmp_native_url }); }
    catch(e){ try{ SS.stop(); }catch(_){} _dropNativeScreen(); _goingLive=false; toast('couldn’t start the screen stream'); return; }
    // Track it (and show Stop) BEFORE waiting for the connect: the capture is already running, so from here
    // on there must always be something in the app that can turn it off.
    const ps={ pc:null, local:null, token:info.token, source:'screen', native:true, info };
    _phoneStream=ps; _phoneLiveOverlay(); toast('connecting…');
    // Only announce a stream that's actually carrying frames. Announcing on start() alone posted "🔴 I'm live"
    // to every follower for streams that then failed to connect — and a kind-1 can't be unsent.
    try{ await connected; }
    catch(e){ _goingLive=false; if(_phoneStream===ps){ toast((e&&e.message)||'the screen stream didn’t connect'); _endLive(); } return; }
    if(_phoneStream!==ps){ _goingLive=false; return; }   // they hit Stop while it was connecting
    _goingLive=false;
    let _ev=null;
    try{ _ev=await _publishLive(info, title, cover); }catch(_){ toast('live — but couldn’t announce on Nostr yet'); }
    if(doAnnounce){ try{ await _announceStreamPost(info, title); }catch(_){} }
    toast('🔴 you’re live — sharing your screen');
    _afterGoLive(_ev, true);
  }
  // Hand a running camera stream over to the screen, keeping the same token (so the kind-30311 — and every
  // viewer's open player — survives the switch). MediaMTX allows ONE publisher per path, so the camera has to
  // be gone before the screen connects; that's exactly why consent is asked for FIRST — a user who cancels the
  // system dialog keeps their camera stream untouched. Viewers see a brief freeze during the handover, and the
  // server's 20s unpublish grace re-probes the feed before ending anything, so the stream is never ghost-ended.
  async function _nativeToScreen(ps){
    const SS=_screenPlugin(); if(!SS || !ps.info || !ps.info.rtmp_native_url) return false;
    if(_swapping) return false;
    // Hold the camera-swap lock across the WHOLE handover, consent dialog included. A flip started while the
    // dialog is up would otherwise still be in flight when we null ps.pc, and _swapPhoneVideo's `_phoneStream
    // !== ps` guard wouldn't catch it (same object) — it would deref the closed pc, wedge `_swapping` true
    // forever, and leave the freshly-acquired camera track running for the whole screen share.
    _swapping=true;
    const release=v=>{ _swapping=false; return v; };
    let granted=false;
    try{ const r=await SS.requestConsent(); granted=!!(r && r.granted); }
    catch(e){ toast((e&&e.message)||'couldn’t start screen sharing'); return release(false); }
    if(!granted){ toast('screen sharing cancelled'); return release(false); }
    if(_phoneStream!==ps) return release(false);   // they ended the stream while the consent dialog was up
    _watchNativeScreen(SS);
    const connected=_armScreenConnect();
    // Mark it native BEFORE anything can await: from here the capture may start at any moment, and a Stop that
    // lands in the meantime must know to stop the service (otherwise the screen keeps broadcasting invisibly).
    ps.native=true; ps.source='screen';
    // Drop the WHIP publisher — MediaMTX allows one publisher per path. Clear the connection-state handler
    // first: closing the pc ourselves would otherwise look like the stream dying and _endLive would tear down
    // the very stream we're moving.
    try{ if(ps.pc) ps.pc.onconnectionstatechange=null; }catch(_){}
    try{ ps.pc && ps.pc.close(); }catch(_){}
    try{ ps.local && ps.local.getTracks().forEach(t=>t.stop()); }catch(_){}
    ps.pc=null; ps.local=null;
    const v=$('#pl-vid'); if(v) v.srcObject=null;
    _syncOverlayButtons();
    // The camera is already gone, so a failure here leaves nothing to fall back to: end the stream rather
    // than strand viewers on a frozen frame.
    // Carry the mute across, IN the start call. The native mic is a different capture from the camera stream's
    // audio track and starts unmuted, so muting it afterwards would put the user on air for the length of the
    // round-trip — real audio from someone who believes they're muted, which is the worst failure this feature
    // has. Passing it to start() means the service is already muted before it ever publishes a frame.
    try{ await SS.start({ url:ps.info.rtmp_native_url, muted: !!ps.muted }); await connected; }
    catch(e){ if(_phoneStream===ps){ toast((e&&e.message)||'couldn’t start the screen stream'); _endLive(); } return release(false); }
    { const b=$('#pl-mute'); if(b) b.textContent=ps.muted?'🔇 Unmute':'🎤 Mute'; }
    return release(true);
  }
  const _canScreenShare=()=>!!(navigator.mediaDevices && navigator.mediaDevices.getDisplayMedia) || !!_screenPlugin();
  // Toggle screen share ↔ camera. During screen share the mic keeps streaming (a separate audio track in the
  // browser; the native plugin captures it directly), so it's screen + voiceover either way.
  function _toggleScreen(){
    const ps=_phoneStream; if(!ps) return;
    if(ps.native) return;   // native share: no in-place way back to the camera (see _syncOverlayButtons)
    if(ps.source==='screen') return _toCamera(ps.facing||'user');
    // The app's WebView has no getDisplayMedia — capture the screen natively instead.
    if(!(navigator.mediaDevices && navigator.mediaDevices.getDisplayMedia)) return _nativeToScreen(ps);
    return _swapPhoneVideo(()=>navigator.mediaDevices.getDisplayMedia({video:{width:{ideal:1920},height:{ideal:1080}},audio:false}), nt=>{
      ps.source='screen';
      // The browser's OWN "Stop sharing" control ends this track directly. When it does, fall back to the
      // camera so the broadcast survives — and if that fallback can't run (no camera, or another swap was
      // mid-flight), end the stream rather than leave viewers frozen on a dead track.
      try{ nt.onended=()=>_onScreenShareEnded(ps); }catch(_){}
    });
  }
  async function _onScreenShareEnded(ps){
    if(_phoneStream!==ps || ps.source!=='screen') return;   // WE replaced it (onended was cleared) — nothing to do
    const r=await _toCamera(ps.facing||'user');
    if(r==='busy'){ setTimeout(()=>_onScreenShareEnded(ps), 400); return; }   // a swap was in flight — retry shortly
    if(!r && _phoneStream===ps && ps.source==='screen'){ toast('screen share ended'); _endLive(); }
  }
  // Release EVERY capture behind a phone stream. Kept as one function because there are two ways out (End the
  // stream, or Delete it) and a camera/screen capture left running by the path that forgot is a privacy leak,
  // not a leaked object: the native screen share in particular lives in a foreground service outside the
  // WebView, so if we don't stop it here nothing ever will.
  function _teardownPhoneStream(){
    const ps=_phoneStream; if(!ps) return;
    _phoneStream=null;
    _stopViewerPoll();
    if(ps.native){ const SS=_screenPlugin(); try{ SS && SS.stop(); }catch(_){} _dropNativeScreen(); }
    try{ if(ps.pc) ps.pc.onconnectionstatechange=null; }catch(_){}
    try{ ps.pc && ps.pc.close(); }catch(_){}
    try{ ps.local && ps.local.getTracks().forEach(t=>t.stop()); }catch(_){}
    const ov=document.getElementById('phone-live'); if(ov) ov.remove();
  }
  async function _endLive(){
    if(!_liveStream && !_phoneStream) return;
    _stopLiveHb();
    _teardownPhoneStream();
    const s=_liveStream; _liveStream=null;
    if(s){ _endedStreams.add(s.token);   // never auto-re-adopt this one — the End was intentional
      try{ const r=await publish(30311, '', _liveBase(s).concat([['status','ended'], ['ends', String(Math.floor(Date.now()/1000))]]));
        _mirrorStream(r);      // or the stream sits ● LIVE forever on every client that saw go-live
        _clearEndSentinel(); }catch(_){}
      // The recording does not exist yet (concat + Blossom upload take a while). Watch for it and stamp
      // the `recording` tag when it lands, so the replay is visible to other clients without the
      // streamer having to come back and reopen their own stream. Deliberately not awaited.
      try{ _stampReplayWhenReady((s.d || s.token), s.token, s.starts); }catch(_){} }
    /* SAY WHAT HAPPENS NEXT, AND THAT LEAVING IS SAFE. "stream ended" alone left people watching a
     * blank screen wondering whether they had to keep the app open for the replay to survive.
     * They do not: the concat/compress/upload runs in a server-side reaper task
     * (stream_end_service.start_stream_end_reaper -> stream_vod_service.process_pending), so closing
     * the tab, backgrounding the app or losing the phone cannot interrupt it. MEASURED across seven
     * real streams: 62-89s from end to the recording being live, and it does NOT track clip length
     * (a 3s clip took 79s, a 15s clip 63s) because the 30s reaper sweep dominates, not the encode.
     * Hence "about a minute" — honest for short streams; a long one is longer, so it is not promised
     * as a deadline. Deliberately NOT a blocking modal: warning people off an action that is
     * completely safe is both false and the fastest way to teach them to dismiss warnings. */
    toast(s && s.record
      ? 'Stream ended — saving your recording, about a minute. You can close the app; it finishes on the server.'
      : 'stream ended');
    if(S.VIEW==='streams') renderStreams();
  }
  // ---------- floating mini-player: keep a stream playing while you browse other views ----------
  // Moving the live <video> node (with its attached hls.js) OUT of #feed and into a fixed,
  // persistent container means a feed re-render can't kill it — playback simply continues.
  // _streamHls = the hls bound to the INLINE player; _miniHls = the one handed to the floating
  // mini-player. Keeping them separate means a feed re-render (cleanupInlineStream) can't tear down
  // a popped-out stream, and closing the mini can't tear down a newer inline stream.
  let _streamHls=null, _miniHls=null, _miniEv=null;
  /* IS THE STREAM PLAYING IN A DIFFERENT WINDOW?
   *
   * Desktop mode parks an unfocused window by MOVING its nodes into that window's own slot, where
   * they stay live and on screen. So `#st-video` sitting inside an `.osw-slot` means the stream
   * belongs to a window that is not the one being re-rendered — and tearing its hls down from here
   * kills a stream somebody is watching, in a window they did not touch.
   *
   * Reported (and confirmed by a second user) as: stream open in one window, Social in another, post
   * a note — the stream stops. Posting repaints the focused window, renderView() runs, and this used
   * to destroy the player unconditionally. Same shape as the parked-timeline bug: one global torn
   * down on behalf of a view that no longer owns it. */
  function _streamParked(){
    try{
      if(!window.PCOS) return false;
      // If the LIVE feed holds a player, this window owns one and may manage it. Asked this way
      // round rather than by getElementById, which answers with whichever element it finds first and
      // cannot tell two open stream windows apart.
      if(document.querySelector('#feed #st-video')) return false;
      return !!document.querySelector('.osw-slot #st-video');
    }catch(_){ return false; }
  }
  // The raw teardown, for the one caller that is REPLACING the player it owns.
  function _disposeInlineHls(){
    if(_streamHls){ try{ _streamHls.destroy(); }catch(_){} _streamHls=null; } _stopStreamViewers(); }
  function cleanupInlineStream(){
    if(_streamParked()) return;      // it is another window's player — not ours to stop
    _disposeInlineHls(); }
  // Headcount for the stream you're WATCHING. The 30311 is replaceable and the host re-signs it as the
  // number moves, so re-read it on a timer — otherwise the badge freezes at whatever the card happened to
  // carry when you opened it. Cleared by cleanupInlineStream on leaving the view.
  const _viewersTag = e => (e && (e.tags.find(t=>t[0]==='current_participants')||[])[1]) || '';
  let _streamViewerPoll=null;
  function _stopStreamViewers(){ if(_streamViewerPoll){ clearInterval(_streamViewerPoll); _streamViewerPoll=null; } }
  function _startStreamViewers(hpk, dtag, viewId){
    _stopStreamViewers(); if(!dtag) return;
    const tick=async()=>{
      if(S.VIEW!=='stream' || openStream._view!==viewId){ _stopStreamViewers(); return; }
      let evs=[]; try{ evs=await Relay.query([{ kinds:[30311], authors:[hpk], '#d':[dtag], limit:1 }]); }catch(_){ return; }
      const fresh=(evs||[]).sort((a,b)=>b.created_at-a.created_at)[0]; if(!fresh) return;
      if(S.VIEW!=='stream' || openStream._view!==viewId) return;   // navigated away during the query
      const n=_viewersTag(fresh); const el=document.getElementById('st-viewers');
      if(el) el.textContent = n ? ` · 👁 ${n} watching` : '';
    };
    tick();   // don't make the first refresh wait a whole interval — that's why the count only appeared after a reload
    _streamViewerPoll=setInterval(tick, 15000);
  }
  /* A SECOND WINDOW for a stream and its chat.
   *
   * The mini-player above is a floating div, so it can be dragged around the page and no further —
   * a browser window is the edge of the world for it. Watching on one monitor while working on
   * another needs a real OS window, which means window.open: a separate window in a browser, a
   * separate BrowserWindow in the desktop app, movable to any screen either way.
   *
   * It opens the stream's OWN deep link rather than a document assembled here, so the popup gets the
   * real player, the real live chat and the real signer, with no second implementation of any of
   * them to drift. Same origin, so it shares localStorage and is already signed in; `popout=1` is
   * what tells it to draw without the sidebar and nav it has no room for.
   *
   * The window NAME is per stream: clicking twice focuses the window you already have instead of
   * opening a second copy of the same broadcast, which would also be a second HLS pull. */
  // This window IS a chat popout (`?popout=1&chat=1`) — the player is hidden and the chat owns the
  // whole window. Read off the class rather than the query so it is one answer everywhere.
  function _chatPopout(){ try{ return document.body.classList.contains('popout-chat'); }catch(_){ return false; } }
  function openStreamWindow(ev, chatOnly){
    let naddr='';
    try{
      naddr = NT().nip19.naddrEncode({ identifier:(ev.tags.find(t=>t[0]==='d')||[])[1]||'',
                                       pubkey:ev.pubkey, kind:30311 });
    }catch(_){ toast('could not build a link to this stream'); return; }
    /* The window's URL has to be a document that this shell can actually SERVE.
     *
     * On the web /client/<naddr> is a real route, so appending the entity as a path segment works.
     * In the desktop bundle there is no server and no router: app:// reads a FILE off disk, and the
     * page is /index.html — so `app://posterchan/index.html/naddr1…` is a path that exists nowhere
     * and the new window rendered the scheme handler's own 404 body, reported as "a new window
     * opening saying not found".
     *
     * So the entity travels as a QUERY on the document that is ALREADY loaded, which is a real URL
     * in both shells. Strip any entity segment the current URL is carrying so the popout of a stream
     * opened from a deep link doesn't inherit it; `_entityFromPath` reads `?e=` for exactly this. */
    /* Strip BOTH path forms `_entityFromPath` decodes — the bech32 segment and `/users/<name>` —
     * because a path entity is read BEFORE the query, so anything left behind wins over the `?e=`
     * this URL is built around. Reached by opening the app from a shared profile link
     * (poster.place/users/alice): the popout would show alice's profile instead of the stream. */
    let doc = location.pathname
      .replace(/\/(?:nostr:)?(?:npub1|nprofile1|note1|nevent1|naddr1)[023456789acdefghjklmnpqrstuvwxyz]+\/*$/i, '')
      .replace(/\/users\/[^/]+\/*$/i, '')
      .replace(/\/+$/,'');
    /* A bundle serves FILES, so the only safe target is a real one. `location.pathname` is not
     * reliably that: _navUrl pushes `/note1…`, `/naddr1…` and `/` as you move around, and none of
     * those exist on disk — which is the same 404 ("not found" in a new window) this is here to fix,
     * just arrived at by a different route. Anything that is not an .html file becomes the entry
     * point.
     *
     * And never a bare "/" on the web: `GET /` is a 302 to /client (app/main.py) and a redirect
     * DROPS THE QUERY, so the window would open with neither `popout=1` nor the naddr and paint the
     * ordinary timeline — while this tab has already torn its own player down. Reachable in one
     * step: a shared root link leaves location.pathname at "/" for the rest of the session. */
    if(BUNDLED) doc = /\.html?$/i.test(location.pathname) ? location.pathname : '/index.html';
    else if(!doc) doc = '/client';
    const url = location.origin + doc + '?popout=1' + (chatOnly ? '&chat=1' : '')
                + '&e=' + encodeURIComponent(naddr);
    /* A DIFFERENT window name for the chat, or the two buttons fight over one window: `window.open`
     * with an existing name REPLACES that window's document, so popping the chat would close the
     * stream you are watching and vice versa. They are meant to be open together. */
    const name = (chatOnly ? 'pcchat_' : 'pcstream_') + naddr.slice(-24);
    /* Sized to the SCREEN, not to a fixed guess: at zoom 1 the chat needs real width beside the
     * player, and a 1180px cap on a 4K monitor is a small window on a big screen for no reason.
     * The chat alone is a COLUMN — it wants height, not width — so it gets a narrow window instead
     * of two thirds of the monitor with the message list stretched across it. */
    const w = chatOnly ? 420 : Math.max(900, Math.round(screen.availWidth * 0.7));
    const h = Math.max(600, Math.round(screen.availHeight * 0.8));
    let win=null;
    try{ win = window.open(url, name, 'width='+w+',height='+h+',menubar=no,toolbar=no'); }catch(_){}
    if(!win){ toast('your browser blocked the window — allow pop-ups for this site'); return; }
    try{ win.focus(); }catch(_){}
    /* THE CHAT POPOUT KEEPS THIS PAGE PLAYING. Only the chat moved — tearing the player down here
     * would stop the video the user is still watching, which is the opposite of what the button is
     * for. Just drop this page's chat sub (the new window opens its own) and collapse the column. */
    if(chatOnly){
      try{ _closeStreamChat(); }catch(_){}
      const lay=$('#feed') && $('#feed').querySelector('.stream-layout');
      if(lay) lay.classList.add('chat-hidden');
      toast('chat opened in its own window');
      return;
    }
    /* Stop THIS page pulling the same stream. Two windows decoding one broadcast is double the
     * bandwidth and double the CPU for a picture nobody is looking at, and on a laptop that is the
     * difference between quiet and loud. The chat sub goes with it; the popup has its own. */
    try{ _closeStreamChat(); }catch(_){}
    const v=$('#st-video');
    if(v){ try{ v.pause(); v.removeAttribute('src'); v.load(); }catch(_){} }
    try{ if(_streamHls){ _streamHls.destroy(); _streamHls=null; } }catch(_){}
    const n=$('#st-note'); if(n) n.textContent='▶ Playing in its own window.';
    toast('opened in a window — drag it to another monitor');
  }

  function popOutStream(ev){
    const v=$('#st-video'); if(!v) return;
    closeMini();   // only one mini at a time
    let mp=$('#mini-player'); if(!mp){ mp=document.createElement('div'); mp.id='mini-player'; mp.className='mini-player'; document.body.appendChild(mp); }
    const title=(ev.tags.find(t=>t[0]==='title')||[])[1]||'stream';
    mp.innerHTML=`<div class="mini-bar"><span class="mini-title">▶ ${enc(title)}</span><button class="mini-x" id="mini-open" title="back to stream">⤢</button><button class="mini-x" id="mini-close" title="close"><svg class="ic x-ic" aria-hidden="true"><use href="#i-close"></use></svg></button></div>`;
    mp.appendChild(v); v.removeAttribute('id');   // MOVE the playing element (hls stays attached → no interruption)
    _miniHls=_streamHls; _streamHls=null;          // hand hls ownership to the mini
    _miniEv=ev; mp.classList.add('on');
    $('#mini-close').onclick=()=>closeMini(true);
    $('#mini-open').onclick=()=>{ const e2=_miniEv; closeMini(); if(e2) openStream(e2); };
    _makeMiniDraggable(mp);   // drag by the bar; the corner resizes (CSS resize:both)
    v.play().catch(()=>{});
    toast('stream popped out — drag the title bar to move, drag the corner to resize');
  }
  // Make the pop-out player moveable (drag its title bar) — it also resizes via CSS `resize:both` on the
  // bottom-right corner. Pin it to left/top from its rendered rect first so both drag and resize behave.
  function _makeMiniDraggable(mp){
    const bar=mp.querySelector('.mini-bar'); if(!bar) return;
    const pin=()=>{ const r=mp.getBoundingClientRect(); mp.style.left=r.left+'px'; mp.style.top=r.top+'px'; mp.style.right='auto'; mp.style.bottom='auto'; };
    let sx=0, sy=0, ox=0, oy=0, drag=false;
    bar.addEventListener('pointerdown', e=>{
      if(e.target.closest('.mini-x')) return;   // the ⤢/✕ buttons aren't drag handles
      pin(); drag=true; sx=e.clientX; sy=e.clientY;
      const r=mp.getBoundingClientRect(); ox=r.left; oy=r.top;
      try{ bar.setPointerCapture(e.pointerId); }catch(_){}
      e.preventDefault();
    });
    bar.addEventListener('pointermove', e=>{ if(!drag) return;
      const w=mp.offsetWidth, h=mp.offsetHeight;
      let nx=Math.max(4, Math.min(ox+(e.clientX-sx), window.innerWidth - w - 4));
      let ny=Math.max(4, Math.min(oy+(e.clientY-sy), window.innerHeight - h - 4));
      mp.style.left=nx+'px'; mp.style.top=ny+'px';
    });
    const end=e=>{ drag=false; try{ bar.releasePointerCapture(e.pointerId); }catch(_){} };
    bar.addEventListener('pointerup', end); bar.addEventListener('pointercancel', end);
  }
  // `restore` is opt-in (the ✕ button) — popOutStream and ⤢ do their own thing and must NOT re-render.
  // popOutStream MOVES the live <video> out of the stream view and strips its id, so a bare teardown left
  // that page with no player and no way back: ⧉ then silently no-op'd on the missing #st-video and only
  // navigating away and re-opening the stream brought it back. Re-render if the user is still on it.
  function closeMini(restore){
    const mp=$('#mini-player'); if(!mp) return;
    const ev=_miniEv;
    if(_miniHls){ try{ _miniHls.destroy(); }catch(_){} _miniHls=null; }
    _miniEv=null; mp.classList.remove('on'); mp.innerHTML='';
    if(restore && ev && S.VIEW==='stream' && openStream._view===ev.id) openStream(ev);
  }
  // Most Nostr streams are HLS (.m3u8). Chrome/Firefox can't play that natively ("invalid MIME
  // type") — only Safari can — so route HLS through hls.js; play everything else (mp4/webm
  // recordings, native-HLS Safari) straight off the <video> src.
  function _streamNote(msg){ const n=$('#st-note'); if(n) n.textContent=msg; }
  // Does this URL need our hlsSession cookie? That cookie is set ONLY by our own HLS proxy and scoped to
  // `Path=/api/streams/hls/<token>/`, so match the path — NOT the origin. The native app reaches its own
  // server cross-origin (so same-origin would be wrong), and a deployment using a direct `stream_hls_base`
  // subdomain bypasses the proxy entirely and never has that cookie to send.
  function attachStream(url){
    const v=$('#st-video'); if(!v) return;
    _disposeInlineHls();   // drop any previous inline hls before attaching a new one — unconditional:
                           // this window is about to replace the player it owns, parked or not
    const isHls=/\.m3u8(\?|#|$)/i.test(url);
    const native=()=>{ v.onerror=()=>_streamNote('Could not play this video here — try the \u201cOpen stream URL\u201d link below.'); v.src=url; };
    if(!isHls){ native(); return; }   // mp4/webm VOD — <video> plays it directly
    // HLS: try hls.js FIRST, and fall back to native ONLY when it isn't usable. The reverse order (the old
    // `!canPlayType(...)` gate) is what produced "No video with supported format and MIME type found" on
    // Firefox for Android: it answers "maybe" to application/vnd.apple.mpegurl WITHOUT being able to play
    // HLS, so the gate skipped hls.js and handed the raw .m3u8 straight to <video>. canPlayType is
    // advisory; MediaSource support is the fact worth branching on — and this is the order hls.js documents.
    loadHls().then(()=>{
      if(window.Hls && window.Hls.isSupported()){
        // NO credentials. Our HLS proxy now holds MediaMTX's session server-side and injects it upstream,
        // so playback needs no cookie from the browser — same as any third-party CDN. Sending credentials
        // would be FATAL: CORS forbids `Access-Control-Allow-Origin: *` on a credentialed request (exactly
        // what our proxy and zap.stream's CloudFront both return), which is what used to black out viewers.
        /* THE BUFFER TARGET MUST FIT IN THE PLAYLIST, or the player spends its life reaching for media
         * that does not exist. MediaMTX publishes a ROLLING window — `hlsSegmentCount: 7` — so the whole
         * playlist is 7 × segment-duration. Asking for 30s of buffer from a 14s window means the target
         * is never reached, playback settles further back to chase it, and the moment it drifts past the
         * oldest segment there is nothing to fetch: stall, then a jump forward to whatever still exists.
         * That is a black screen followed by a skip.
         *
         * It hid for a long time because the streamer's keyframe interval was ~4.17s (x264's default
         * 250-frame GOP at 60fps), which made the window 29s — near enough to 30 that it never bit.
         * Fixing the keyframe interval to 2s halved the window to 14s and exposed it.
         *
         * Derived, not picked: stay comfortably inside the smallest window the server can produce
         * (7 × 2s), and let hls.js sit near the live edge as it does by default. */
        /* AND SIT BACK FROM THE LIVE EDGE. Measured on a real stream: the player was running with
         * 0.3s of media ahead of the playhead and a gap already in its buffer, while dropping zero
         * frames — decode was perfectly healthy, there was simply nothing in hand. Any jitter at all
         * empties a third of a second, and playback stops until the next segment lands.
         *
         * hls.js defaults to ~3 segments behind live, which was 12.5s of margin at the 4.17s segments
         * this stream used to produce and only 6s once the keyframe interval was corrected to 2s. So
         * shortening the segments — right for join latency — quietly halved the safety margin.
         *
         * Nobody watching a broadcast notices ten seconds of latency; everybody notices stuttering. */
        const h=new window.Hls({ maxBufferLength:10, maxMaxBufferLength:20,
                                 liveSyncDurationCount:5, liveMaxLatencyDurationCount:12 }); _streamHls=h;
        h.loadSource(url); h.attachMedia(v);
        h.on(window.Hls.Events.ERROR,(_e,d)=>{ if(d&&d.fatal) _streamNote('Could not play this stream here — try the \u201cOpen stream URL\u201d link below.'); });
        return;
      }
      // No MediaSource (iPhone Safari) — native HLS is the right path there.
      if(v.canPlayType('application/vnd.apple.mpegurl')){ native(); return; }
      _streamNote('This stream needs HLS playback, which isn\u2019t available in this browser — try the \u201cOpen stream URL\u201d link below.');
    }).catch(()=>{
      // hls.js itself failed to load; native is the only option left, so try it before giving up.
      if(v.canPlayType('application/vnd.apple.mpegurl')) native();
      else _streamNote('Couldn\u2019t load the video player — try the \u201cOpen stream URL\u201d link below.');
    });
  }
  return {
    _closeStreamChat, _forgetDeletedStream, _goLive, _reconcileNativeScreen, _stopStreamsReads,
    _sweepStaleOwnLive, _sweepUnstampedReplays, cleanupInlineStream, closeMini, openStream,
    renderStreams,
  };
};
