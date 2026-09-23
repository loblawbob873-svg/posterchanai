/* The music player — the queue, the transport, the floating widget and the phone/desktop media
 * session (lock screen, headset and car buttons, the Android widget, Bluetooth autoplay). Split out
 * of app.js.
 *
 * This one ships with the page rather than loading on demand: the player is armed at startup so a
 * car button pressed with the app in the background reaches a live session, and `MusicPlayer` is
 * read as an object from app.js and from window.__PC. app.js keeps the NAME, bound to a Proxy onto
 * the object in here (see `_lzProxy`), and the factory is built the first time anything touches it.
 *
 * The code below is BYTE-IDENTICAL to what it replaced apart from its reads of app.js's live `let`
 * bindings, which the parser rewrote to `S.<name>` (getters/setters on `dep.state`) at exact
 * identifier offsets — `_audioEl` among them: the audio element stays app.js's, because the music
 * phone settings and the desktop widget read it there.
 */
window.PCMusicPlayerFactory = function(dep){
  const S = dep.state;   // live app.js bindings: S.LOGO, S._audioEl
  const {
    FilesIdx, PC_setMusicPl, _capPlugin, _fmtTime, _trackMeta, _updateMusicListBtns, enc,
    musicTracks, renderMusicApp, toast, trackUrl,
  } = dep;
  const MusicPlayer = {
    el:null, min:false, cur:null, queue:[], shuffle:false, _loading:false, _history:[], _search:'',
    _eq:{low:0,mid:0,high:0}, _eqLoaded:false,
    _viz:{an:null,raf:0,failed:false,ro:null,filters:null},
    ensure(){
      if(this.el) return this.el;
      // append to <html> NOT <body>: body has zoom:.85 on desktop, which throws off a fixed body child's
      // position (it would mis-overlap the sidebar + block its clicks). Same fix as the popovers.
      const d=document.createElement('div'); d.id='music-player'; d.className='mp hidden'; document.documentElement.appendChild(d); this.el=d;
      if(!S._audioEl) S._audioEl=new Audio();
      this._loadEQ();
      S._audioEl.ontimeupdate=()=>this._tick();
      S._audioEl.onended=()=>this.next();
      S._audioEl.onplay=()=>{ this._render(); this._startViz(); this._media(); };
      S._audioEl.onpause=()=>{ this._render(); this._media(); };
      // Hardware / keyboard media keys (play/pause, ⏮/⏭) + OS lock-screen controls via MediaSession.
      if('mediaSession' in navigator){ const ms=navigator.mediaSession; try{
        ms.setActionHandler('play',          ()=>{ if(S._audioEl) S._audioEl.play(); });
        ms.setActionHandler('pause',         ()=>{ if(S._audioEl) S._audioEl.pause(); });
        ms.setActionHandler('previoustrack', ()=>this.prev());
        ms.setActionHandler('nexttrack',     ()=>this.next());
        ms.setActionHandler('stop',          ()=>this.close());
        try{ ms.setActionHandler('seekto', e=>{ if(S._audioEl && S._audioEl.duration && e.seekTime!=null) S._audioEl.currentTime=e.seekTime; }); }catch(_){}
        /* A CAR is the reason these two exist. A head unit (and Android Auto, and most Bluetooth
         * remotes) offers skip-within-track as well as skip-track, and with no handler registered
         * the buttons are simply dead — the platform will not synthesise them. `seekOffset` is what
         * the unit asked for; 10s is the convention when it does not say. */
        try{ ms.setActionHandler('seekbackward', e=>{ if(!S._audioEl) return;
          S._audioEl.currentTime=Math.max(0, S._audioEl.currentTime-((e&&e.seekOffset)||10)); this._media(); }); }catch(_){}
        try{ ms.setActionHandler('seekforward', e=>{ if(!S._audioEl) return;
          const d=S._audioEl.duration||0;
          S._audioEl.currentTime=Math.min(d||S._audioEl.currentTime+10, S._audioEl.currentTime+((e&&e.seekOffset)||10)); this._media(); }); }catch(_){}
      // Duration only exists once the track has loaded, and the OS wants it for the scrubber.
      S._audioEl.onloadedmetadata=()=>this._media();
      }catch(_){} }
      this._nativeInit();
      return d;
    },
    /* THE APK NEEDS ALL OF THAT AGAIN, NATIVELY. Everything above is navigator.mediaSession, which in
     * a BROWSER is the entire job — Chrome is what turns those calls into the lock-screen and shade
     * notification. A WebView accepts every one of them and shows NOTHING, because that surface lives
     * in Chrome and not in the WebView. So in the app the music played with no controls anywhere
     * outside it: no lock screen, no shade, no headset button, no widget. The MusicControls plugin is
     * the missing half — it publishes a real Android media session (and the home-screen widget) from
     * the state pushed here, and sends every press back as `musicTransport`, which we perform on the
     * audio element. The audio itself stays here: a track is an encrypted blob only this client can
     * decrypt, so there is nothing for a native player to open.
     * No-op everywhere else — _capPlugin returns null in the PWA and the desktop app. */
    _nativeInit(){
      if(this._nativeWired) return;
      const P=_capPlugin('MusicControls','addListener'); if(!P) return;
      this._nativeWired=true;
      try{ P.addListener('musicTransport', e=>{
        const a=(e&&e.action)||'', v=Number((e&&e.value)||0);
        /* THE ACK IS FIRST AND UNCONDITIONAL. It is the service's only proof that anybody received
         * the press, and it must not depend on the press being PERFORMABLE: a page that has just
         * reloaded holds no track, so `_nativePush` (below, and rightly) refuses to send state for a
         * player that has nothing to state — which would make a live, healthy client look exactly
         * like a dead one. The service would then wake an app that is already awake and write "the
         * player stopped responding" across a notification the user is looking at. */
        this._nativeAck();
        if(a==='play'){ this._resumeOrPlay(); }
        else if(a==='pause'){ if(S._audioEl) S._audioEl.pause(); }
        // …and a skip on a page holding nothing is a RESUME, not a skip: next() there walks an empty
        // queue (or `indexOf(null)` → track one of the library, which is not what ⏭ means to anyone).
        else if(a==='next'){ if(this.cur) this.next(); else this._resumeOrPlay(); }
        else if(a==='prev'){ if(this.cur) this.prev(); else this._resumeOrPlay(); }
        else if(a==='stop') this.close();
        else if(a==='seekTo'){ if(S._audioEl && S._audioEl.duration) S._audioEl.currentTime=Math.max(0,Math.min(v,S._audioEl.duration)); }
        else if(a==='seekBy'){ if(S._audioEl){ const d=S._audioEl.duration||0;
          S._audioEl.currentTime=Math.max(0, d ? Math.min(d,(S._audioEl.currentTime||0)+v) : (S._audioEl.currentTime||0)+v); } }
        /* …and this is also the RECEIPT. The service cannot tell a press that was performed from one
         * that landed in a dead page — emit() succeeds either way — so it treats the state push this
         * makes as proof somebody was here, and wakes the app when it does not arrive. Synchronous
         * on purpose: a `next` that has to fetch and decrypt a track would otherwise take longer to
         * answer than the check waits, and the app would be dragged to the foreground mid-song. */
        this._media();   // the OS believes what it was last told — tell it what the press actually did
      }); }catch(_){ this._nativeWired=false; return; }
      /* The service outlives this page, so it is told the options again on every load — a renderer
       * that died and came back has no memory of them, and the service may have been up throughout. */
      try{ const O=_capPlugin('MusicControls','setOptions');
        if(O){ const r=O.setOptions({autoplayBluetooth:this.autoplayBT()}); if(r&&r.catch) r.catch(()=>{}); }
      }catch(_){}
      /* And say hello immediately. A page that came back after a renderer death arrives with the
       * service possibly already latched "not responding" from the press that failed a moment ago —
       * this clears it as soon as there is somebody home, rather than at the end of whatever slow
       * cold path (relay, key, decrypt) eventually produces the first real state push. */
      this._nativeAck();
      this._nativeBeat();
    },
    /* "I am here" — no state, so it can be sent when there is nothing playing, which is exactly when
     * it matters. Never starts the service (the native side no-ops without one), so this cannot put a
     * notification on screen for a player that has never played. */
    _nativeAck(){
      const P=_capPlugin('MusicControls','ack'); if(!P) return;
      try{ const r=P.ack(); if(r&&r.catch) r.catch(()=>{}); }catch(_){}
    },
    /* A HEARTBEAT, BECAUSE PAUSED IS THE STATE THIS BREAKS IN. Nothing used to be pushed while the
     * player sat paused — _media() runs on play/pause and once a second WHILE PLAYING — so from the
     * service's side a player paused in a pocket and a player whose WebView Android has since thrown
     * away look identical for as long as the pause lasts, which in a car is the whole drive. This is
     * what makes those two distinguishable, and it is what MusicPlugin.status() reports as
     * `webSilenceMs`. Backgrounded timers are throttled to about once a minute after five minutes
     * hidden — deliberately not relied on for any decision, which is why an unanswered PRESS (not a
     * silent heartbeat) is what triggers a wake-up. */
    _nativeBeat(){
      if(this._beat) return;
      this._beat=setInterval(()=>{
        if(this._nativeOff) return;
        if(S._audioEl && !S._audioEl.paused) return;    // playing already pushes once a second
        // With a track, the full state; without one — a reloaded page, the case this exists for —
        // the bare ack, which is the only thing there is to say and the only thing that is needed.
        if(this.cur && this._nativeUp) this._nativePush(); else this._nativeAck();
      }, 15000);
    },
    /* PLAY, ASKED FOR BY SOMETHING THAT CANNOT SEE THIS PAGE — a car button, the lock screen, a
     * Bluetooth connection, the widget. None of them know whether the player still holds a track,
     * and after the WebView is reloaded it does not: the renderer dying takes MainActivity through
     * recreate(), which is a fresh page with an empty queue and a blank <audio>, while the service,
     * its media session and its notification all carry on untouched. `_audioEl.play()` against no
     * src resolves against nothing, so the press was silently lost — that is what "the controls
     * stop working until I open the app" looks like from in here. Falling back to the last track
     * this device played is what makes the press work anyway, with the app still in the background. */
    _resumeOrPlay(){
      this.ensure();
      /* A REFUSED play() MUST NOT LOOK LIKE A FROZEN PLAYER. This used to swallow the rejection, and
       * the one caller that cannot survive that is autoplay: a WebView refuses audio.play() with no
       * user gesture unless setMediaPlaybackRequiresUserGesture(false) is set (it now is, in
       * MainActivity) — so in a car the track loaded, the promise rejected into nothing, and the
       * player just sat there. Reported as "I seen song load, never played, just looked frozen".
       * Now it says which, in the one place that survives the app being closed. */
      if(S._audioEl && S._audioEl.src){ const r=S._audioEl.play();
        if(r&&r.catch) r.catch(e=>this._nativeBlocked(e)); return true; }
      /* The library, before asking it anything. Nothing else on a freshly-loaded page has hydrated
       * the files index yet — renderMusicApp is what normally does it, and the whole point of this
       * path is that nobody has opened a screen. Synchronous (localStorage), so it costs a read. */
      try{ FilesIdx.loadLocal(); }catch(_){}
      const last=this._lastTrack();
      if(last && musicTracks(null).some(t=>t.sha===last.sha)){ this.play(last.sha,{force:true, at:last.pos||0}); return true; }
      const q=musicTracks(null);
      if(q.length){ this.play(q[0].sha,{force:true}); return true; }
      return false;
    },
    /* Tell the SERVICE a play() was refused. The service is the half that outlives the page, and the
     * failure this exists for happens with nobody looking at the screen — a toast in a backgrounded
     * WebView is a message to an empty room. Details reads it back afterwards.
     * NotAllowedError is the autoplay-policy refusal specifically; anything else is passed through
     * by name rather than flattened, since "the file would not decode" is a different bug. */
    _nativeBlocked(e){
      try{
        const why=(e && (e.name||e.message)) ? String(e.name||e.message) : 'unknown';
        const P=_capPlugin('MusicControls','playBlocked');
        if(P){ const r=P.playBlocked({ reason:why }); if(r&&r.catch) r.catch(()=>{}); }
      }catch(_){}
    },
    _lastTrack(){ try{ const j=JSON.parse(localStorage.getItem('pc_music_last')||'null');
      return (j && j.sha) ? j : null; }catch(_){ return null; } },
    /* Where we were, for the resume above. Throttled to once every 5s per track: this is called from
     * the same once-a-second block that feeds the media session, and a localStorage write a second
     * for the length of an album is a synchronous disk write a second for nothing. */
    _rememberLast(){
      if(!this.cur) return;
      /* NOT WHILE A TRACK IS LOADING. play() sets `cur` to the new sha synchronously and only assigns
       * `_audioEl.src` after awaiting the decrypted URL — and `ontimeupdate` keeps firing on the OLD
       * source across that gap. Writing there files the previous track's position under the new
       * track's id, so the car-button resume starts the right song three minutes in, or at the end. */
      if(this._loading) return;
      const now=Date.now();
      if(this.cur===this._lastSaveSha && now-(this._lastSaveAt||0)<5000) return;
      this._lastSaveSha=this.cur; this._lastSaveAt=now;
      try{ localStorage.setItem('pc_music_last', JSON.stringify({
        sha:this.cur, pos:Math.floor((S._audioEl&&S._audioEl.currentTime)||0) })); }catch(_){}
    },
    /* "Start playing when a Bluetooth device connects" — per DEVICE (localStorage), not per account:
     * it is a fact about the phone that rides in the car, and the desktop that shares the account has
     * no Bluetooth stack to speak of. OFF by default; a phone that starts playing music by itself in
     * someone else's car is what people uninstall an app over. */
    autoplayBT(){ try{ return localStorage.getItem('pc_music_autoplay_bt')==='1'; }catch(_){ return false; } },
    setAutoplayBT(on){
      try{ localStorage.setItem('pc_music_autoplay_bt', on?'1':'0'); }catch(_){}
      const P=_capPlugin('MusicControls','setOptions'); if(!P) return;
      try{ const r=P.setOptions({autoplayBluetooth:!!on}); if(r&&r.catch) r.catch(()=>{}); }catch(_){}
    },
    /* ONE PLAYER, ONE AUDIO GRAPH. The equalizer belongs to MusicPlayer rather than the Music view,
     * so changing apps or closing/reopening the window cannot reset the sound. Values are device
     * preferences (headphones and speakers differ) and never need the signer or relay. */
    _loadEQ(){
      if(this._eqLoaded) return; this._eqLoaded=true;
      try{ const v=JSON.parse(localStorage.getItem('pc_music_eq')||'null');
        if(v) for(const k of ['low','mid','high']) if(isFinite(+v[k])) this._eq[k]=Math.max(-12,Math.min(12,+v[k]));
      }catch(_){}
    },
    _saveEQ(){ try{ localStorage.setItem('pc_music_eq',JSON.stringify(this._eq)); }catch(_){} },
    setEQ(band,value){
      if(!['low','mid','high'].includes(band)) return;
      this._loadEQ(); this._eq[band]=Math.max(-12,Math.min(12,Number(value)||0)); this._saveEQ();
      /* AudioContext creation is allowed here because this is called from a user gesture. The same
       * graph also feeds the visualizer; _setupViz is idempotent and never creates a second source. */
      this._setupViz();
      const f=this._viz.filters&&this._viz.filters[band]; if(f) f.gain.value=this._eq[band];
      this._paintEQ();
    },
    setEQPreset(name){
      const p={flat:[0,0,0],bass:[7,1,-2],vocal:[-2,5,2],bright:[-2,1,7]}[name]||[0,0,0];
      ['low','mid','high'].forEach((b,i)=>{ this._eq[b]=p[i]; }); this._saveEQ(); this._setupViz();
      if(this._viz.filters) for(const b of ['low','mid','high']) this._viz.filters[b].gain.value=this._eq[b];
      this._paintEQ();
    },
    _paintEQ(root){
      root=root||document;
      for(const b of ['low','mid','high']){
        const input=root.querySelector('#ma-eq-'+b), out=root.querySelector('#ma-eq-'+b+'-v');
        if(input) input.value=String(this._eq[b]);
        if(out) out.textContent=(this._eq[b]>0?'+':'')+this._eq[b]+' dB';
      }
      root.querySelectorAll('.ma-eq-preset').forEach(x=>{
        const p={flat:[0,0,0],bass:[7,1,-2],vocal:[-2,5,2],bright:[-2,1,7]}[x.dataset.eq]||[];
        x.classList.toggle('on',p.length===3&&p.every((v,i)=>v===this._eq[['low','mid','high'][i]]));
      });
    },
    bindEqualizer(root){
      if(!root) return; this._loadEQ(); this._paintEQ(root);
      root.querySelectorAll('.ma-eq input[data-band]').forEach(x=>x.oninput=()=>this.setEQ(x.dataset.band,x.value));
      root.querySelectorAll('.ma-eq-preset').forEach(x=>x.onclick=()=>this.setEQPreset(x.dataset.eq));
    },
    _nativePush(){
      /* Only once something is playing, and never after the player was closed. update() starts a
       * foreground service, so pushing with no current track is a notification about nothing — and
       * pushing after a close RAISES ONE AGAIN: close() pauses the audio, the 'pause' event lands a
       * moment later, and its state update would rebuild the very notification the close just took
       * down. `_nativeOff` is cleared by play(), which is the only thing that should bring it back. */
      if(!this.cur || this._nativeOff) return Promise.resolve(false);
      const P=_capPlugin('MusicControls','update'); if(!P) return Promise.resolve(false);
      const m=_trackMeta(this.cur), d=S._audioEl?S._audioEl.duration:0;
      try{
        const r=P.update({ title:(m&&m.name)||'Track', artist:'PosterChan',
          playing:!!(S._audioEl && !S._audioEl.paused),
          position:(S._audioEl&&S._audioEl.currentTime)||0,
          duration:(isFinite(d)&&d>0)?d:0 });
        /* A refused service start rejects, and an unhandled rejection is a console error every
         * second. It is also the one thing that tells the heartbeat whether there is a service on
         * the other end at all: without it a player whose notification was taken down some other way
         * would keep trying a background foreground-service start every 15s, for ever, and every one
         * of those is refused. */
        if(r&&r.then) return r.then(()=>{ this._nativeUp=true; return true; }, ()=>{ this._nativeUp=false; return false; });
        this._nativeUp=true; return Promise.resolve(true);
      }catch(_){ this._nativeUp=false; return Promise.resolve(false); }
    },
    /* Launched by the widget — OR by the service, when a press went unanswered and it woke the app to
     * get it performed (MusicService.revive). Both put the same extra on the same launch intent, so
     * there is one mechanism here rather than two that could disagree about what a press means.
     * Consumed (never re-read) on the native side, so a later resume can't replay the press and
     * restart music the user has since paused. */
    consumeLaunch(){
      const P=_capPlugin('MusicControls','consumeLaunchAction'); if(!P) return;
      Promise.resolve(P.consumeLaunchAction()).then(r=>{
        const a=r&&r.action; if(!a) return;
        if(a==='pause'){ if(S._audioEl) S._audioEl.pause(); return; }
        /* play / next / prev all end in "make some sound". On a page that still holds a track the
         * verb means what it says; on one that does not — which is the whole reason the service had
         * to wake us — every one of them is a resume, and _resumeOrPlay is what knows how.
         *
         * The FALLTHROUGH is the point of the shape: a press that could not be performed opens the
         * Music screen rather than dropping silently, because the launch extra is consumed natively
         * and there is no second chance at it. The app has already been dragged to the foreground;
         * landing the user somewhere they can press play themselves is the least it can do. */
        if(a==='play' || a==='next' || a==='prev'){
          this.ensure();
          let done=false;
          if(this.cur && a==='next'){ this.next(); done=true; }
          else if(this.cur && a==='prev'){ this.prev(); done=true; }
          else done=this._resumeOrPlay();
          /* A WIDGET TAP THAT SUCCEEDS STILL OPENS THE REGULAR MUSIC APP. Returning here left the
           * person on whatever PosterChan screen happened to be open with only the floating mini
           * player over it. Rendering the library does not recreate `_audioEl`, so playback keeps
           * the current track and position. */
          if(done){ this._render(); renderMusicApp(); return; }
        }
        renderMusicApp();
      }).catch(()=>{});
    },
    _media(){
      // Push the current track + play state to the OS media UI so the media keys show the right song.
      // The native push is FIRST and unconditional: in the APK it is the only one of the two that
      // produces a lock screen, and gating it behind the browser API would tie the app's controls to
      // a WebView feature that does not do the job (see _nativeInit).
      this._nativePush();
      if(!('mediaSession' in navigator)) return;
      try{
        const m=this.cur?_trackMeta(this.cur):null;
        if(window.MediaMetadata){
          const metadata={title:(m&&m.name)||'Track',artist:'PosterChan',album:'Library'};
          /* Chromium's MediaImage loader rejects Electron's app:// scheme. _media runs on every
           * position heartbeat, so supplying the packaged relative logo produced a fresh console
           * error and failed image load every second. The native Android session does not use this
           * field; on ordinary HTTPS clients retain the artwork, and packaged Desktop omits only
           * the unsupported browser-MediaSession image. */
          try{const art=new URL(S.LOGO,location.href);
            if(art.protocol==='http:'||art.protocol==='https:'||art.protocol==='data:')
              metadata.artwork=[{src:art.href,sizes:'512x512',type:'image/png'}];
          }catch(_){}
          navigator.mediaSession.metadata=new MediaMetadata(metadata);
        }
        navigator.mediaSession.playbackState=(S._audioEl && !S._audioEl.paused)?'playing':'paused';
        /* WHERE WE ARE IN THE TRACK. Without this a car's display has a title and nothing else: no
         * elapsed time, no remaining time, and a scrubber many head units disable outright because
         * they have no duration to draw. It is also what keeps the position honest after a seek —
         * the OS does not watch the audio element, it believes what it was last told.
         *
         * Guarded, because setPositionState THROWS on anything it considers impossible (a NaN or
         * Infinite duration before metadata arrives, a position past the end) and that throw would
         * otherwise take the whole media update with it. */
        if(navigator.mediaSession.setPositionState && S._audioEl){
          const d=S._audioEl.duration;
          if(isFinite(d) && d>0){
            navigator.mediaSession.setPositionState({
              duration:d, playbackRate:S._audioEl.playbackRate||1,
              position:Math.min(Math.max(0,S._audioEl.currentTime||0), d) });
          }
        }
      }catch(_){}
    },
    /* THE QUEUE IS THE LIST YOU CHOSE. Every path that rebuilds it -- ⏭ on an empty queue, a track
     * the queue does not hold, the desktop widget's Shuffle -- used to rebuild it from the whole
     * LIBRARY, so shuffle inside a playlist wandered out of it ("shuffle is no longer working on
     * playlists"). The chosen playlist (`_pl`, kept in step with the Music app's chips) wins; with
     * none chosen it is the library, as before. */
    refreshQueue(){ const pl=this._plTracks(); this.queue=(pl && pl.length ? pl : musicTracks(null)).map(t=>t.sha);
      if(this.cur && !this.queue.includes(this.cur)) this.queue.unshift(this.cur); },
    async play(sha, opts){
      this.ensure();
      this._nativeOff=false;   // playing again is what brings the OS controls back after a close
      /* Tapping the PLAYING track in the list means pause/resume. Transport buttons must not get that
       * treatment: with a short queue every one of them resolves to the current track — next() wraps
       * (i+1)%1 back to itself, prev() likewise, and the shuffle pick can land on it — so ⏮ ⏭ and
       * shuffle all silently PAUSED instead of playing. `force` says "this is a transport action,
       * start it from the top". */
      if(!(opts&&opts.force) && sha===this.cur && S._audioEl.src){ this.toggle(); return; }
      if(opts&&opts.force && sha===this.cur && S._audioEl.src){       // same track, deliberately restarted
        try{ S._audioEl.currentTime=0; await S._audioEl.play(); }catch(_){}
        this._render(); return;
      }
      // keep a STABLE play order — only (re)build the queue when it's empty or doesn't contain this track,
      // NOT on every track, so auto-advance plays in order instead of jumping around ("shuffling everything").
      if(!this.queue.length || !this.queue.includes(sha)) this.refreshQueue();
      if(!this.queue.includes(sha)) this.queue.unshift(sha);
      // remember the outgoing track so ⏮ returns to it (matters in shuffle — next() is random, so the
      // queue-order-previous isn't what you just heard). `opts.back` = we're navigating backward, don't record.
      if(this.cur && this.cur!==sha && !(opts&&opts.back)){ this._history.push(this.cur); if(this._history.length>200) this._history.shift(); }
      this.cur=sha; this._loading=true; this.el.classList.remove('hidden'); this._render();
      try{ const u=await trackUrl(sha);
        if(this.cur!==sha) return;   // a newer ⏭/⏮ superseded this load while we awaited the URL —
                                     // don't clobber _audioEl.src (that's the "skip plays the wrong song" bug)
        this._loading=false; S._audioEl.src=u;
        /* Establish Android's foreground MediaSession BEFORE sound starts. Previously onplay fired
         * the native update asynchronously after play() succeeded; a quick HOME in that gap made
         * Android refuse the background FGS start, leaving the WebView process unprotected and the
         * track liable to die when the native launcher replaced the app surface. A native failure
         * is deliberately non-fatal: browser/PWA playback has no plugin and must still proceed. */
        await Promise.race([this._nativePush(), new Promise(resolve=>setTimeout(()=>resolve(false),600))]);
        await S._audioEl.play();
        /* …and pick up where this device left off, for a resume that nobody is looking at (a car
         * button, a Bluetooth connection). AFTER play(), because currentTime is only settable once
         * the track is seekable and a value set before that is silently dropped. */
        // Clamped like every other seek in this object: a stored position from a track that has since
        // been replaced (same sha, re-uploaded shorter) is past the end, which throws or lands at 0.
        if(opts && opts.at>0){ try{ const d=S._audioEl.duration;
          S._audioEl.currentTime=(isFinite(d)&&d>0) ? Math.min(opts.at, Math.max(0,d-1)) : opts.at; }catch(_){} } }
      catch(e){ if(this.cur===sha){ this._loading=false; toast('play failed: '+(e.message||e)); } }
      if(this.cur===sha) this._render();
    },
    toggle(){ if(S._audioEl){ if(S._audioEl.paused) S._audioEl.play(); else S._audioEl.pause(); } },
    next(){ if(!this.queue.length) this.refreshQueue();
      if(!this.queue.length) return; let i=this.queue.indexOf(this.cur);
      i=this.shuffle ? this._randIdx(i) : (i+1)%this.queue.length; this.play(this.queue[i], {force:true}); },
    _randIdx(cur){ if(this.queue.length<2) return 0; let r; do{ r=Math.floor(Math.random()*this.queue.length); }while(r===cur); return r; },   // don't replay the same track
    prev(){ if(S._audioEl && S._audioEl.currentTime>3){ S._audioEl.currentTime=0; return; }   // >3s in = restart current
      if(this._history.length){ this.play(this._history.pop(), {back:true, force:true}); return; }   // back to the track actually played before
      if(!this.queue.length) this.refreshQueue();
      if(!this.queue.length) return; let i=this.queue.indexOf(this.cur); this.play(this.queue[(i-1+this.queue.length)%this.queue.length], {back:true, force:true}); },
    seekTo(f){ if(S._audioEl && S._audioEl.duration) S._audioEl.currentTime=Math.max(0,Math.min(1,f))*S._audioEl.duration; },
    /* THE SCRUBBER — one implementation, bound to whichever transport is on screen.
     *
     * The floating widget's bar was click-only. That lets you jump to a point but not FIND one, and
     * finding one is the whole job: you cannot aim at the second chorus by clicking blind and hoping.
     * The Music app — the desktop's Music window, and ☰ More → Music on a phone — had no bar at all,
     * only prev/play/next, so a long track could not be moved through in any way whatsoever. Both
     * call this, so a fix to one is a fix to both.
     *
     * POINTER events, not mouse: the same code then works for a finger, a pen and a trackpad, and
     * setPointerCapture is what keeps a drag alive after the pointer leaves the bar — which on a
     * 7px-tall strip happens on the first vertical wobble of a thumb, i.e. immediately.
     *
     * While a drag is in progress `_scrub` holds the position and _tick must not paint over it, or
     * the handle springs back to the playhead on every timeupdate (~4x a second) and the bar fights
     * the finger dragging it. Seeking on MOVE as well as on release would restart the decoder dozens
     * of times per drag, so the audio is moved once, at the end; the fill follows continuously so
     * the drag still looks live. */
    bindSeek(bar){
      if(!bar || bar._pcSeek) return; bar._pcSeek=true;
      const fillEl=()=>bar.querySelector('.mp-seek-fill');
      const fracOf=e=>{ const r=bar.getBoundingClientRect();
        return r.width ? Math.max(0, Math.min(1, (e.clientX-r.left)/r.width)) : 0; };
      const paint=f=>{ const el=fillEl(); if(el) el.style.width=(f*100)+'%'; };
      const seekable=()=>!!(S._audioEl && isFinite(S._audioEl.duration) && S._audioEl.duration>0);
      const end=e=>{
        if(this._scrub==null) return;
        const f=this._scrub; this._scrub=null;
        try{ bar.releasePointerCapture(e.pointerId); }catch(_){}
        this.seekTo(f); this._media(); this._tick();
      };
      bar.onpointerdown=e=>{
        if(!seekable() || e.button>0) return;
        e.preventDefault();
        this._scrub=fracOf(e); paint(this._scrub);
        try{ bar.setPointerCapture(e.pointerId); }catch(_){}
      };
      // …and if the bar was replaced under the drag, stop tracking rather than moving a dead node.
      bar.onpointermove=e=>{ if(this._scrub==null) return;
        if(!bar.isConnected){ this._scrub=null; return; }
        this._scrub=fracOf(e); paint(this._scrub); };
      bar.onpointerup=end;
      // A cancelled pointer (a system gesture, a call arriving, the tab losing the touch) never
      // sends pointerup. Without this the bar stays stuck to a drag that ended, and _tick is frozen.
      bar.onpointercancel=end;
      /* Keyboard, because a scrubber that only answers a pointer is unusable without one and this is
       * one line of arithmetic. ±5s per press, ±1 minute on Page keys, Home/End for the ends. */
      bar.onkeydown=e=>{
        if(!seekable()) return;
        const d=S._audioEl.duration, t=S._audioEl.currentTime||0;
        let to=null;
        if(e.key==='ArrowRight') to=t+5; else if(e.key==='ArrowLeft') to=t-5;
        else if(e.key==='PageUp') to=t+60; else if(e.key==='PageDown') to=t-60;
        else if(e.key==='Home') to=0; else if(e.key==='End') to=Math.max(0,d-1);
        if(to==null) return;
        e.preventDefault();
        S._audioEl.currentTime=Math.max(0, Math.min(d, to)); this._media(); this._tick();
      };
    },
    /* The Music APP's transport, ticked from here rather than from _musicAppNow.
     *
     * _musicAppNow only runs on state CHANGES, and a position that moves only when you press
     * something is not a timeline. This has to run BEFORE _tick's own early return, because the
     * widget is hidden exactly when the app is mounted — which is exactly when this bar is the only
     * one on screen. */
    _tickApp(){
      const bar=document.getElementById('ma-seek'); if(!bar) return;
      const d=(S._audioEl&&S._audioEl.duration)||0, t=(S._audioEl&&S._audioEl.currentTime)||0;
      const ok=isFinite(d)&&d>0;
      const cur=document.getElementById('ma-cur'), dur=document.getElementById('ma-dur');
      if(dur) dur.textContent=ok?_fmtTime(d):'0:00';
      if(cur) cur.textContent=_fmtTime(this._scrub!=null&&ok ? this._scrub*d : t);
      bar.setAttribute('aria-valuetext', ok?_fmtTime(t)+' of '+_fmtTime(d):'not playing');
      if(this._scrub!=null) return;   // a drag owns the fill until it ends
      const f=bar.querySelector('.mp-seek-fill');
      if(f) f.style.width=(ok ? (t/d*100) : 0)+'%';
    },
    setMin(m){ this.min=m; this._render(); },
    close(){ this._nativeOff=true; if(S._audioEl) S._audioEl.pause(); if(this.el) this.el.classList.add('hidden');
      /* Take the native controls down WITH the player. A media notification (and a widget) still
       * offering to pause a track whose player has been closed is worse than none — its buttons work
       * on nothing, and on Android a lingering foreground service is what users go looking for in
       * battery settings. */
      const P=_capPlugin('MusicControls','stop');
      if(P){ try{ const r=P.stop(); if(r&&r.catch) r.catch(()=>{}); }catch(_){} } },
    _tick(){
      /* Where we are, for a resume nobody is watching. FIRST, above every early return below: the
       * widget is hidden while the Music app is mounted and the position block further down never
       * runs then — which is the screen most people press play from. It throttles itself to one
       * write per 5s per track, so being called on every timeupdate costs nothing. */
      this._rememberLast();
      /* The app can vanish without telling us — its window is closed, or the shared #feed moved to
       * another window. The widget was hidden BECAUSE the app was mounted, so if the app has gone and
       * something is still playing, hand the transport back. Without this, closing the Music window
       * left the music playing with no controls anywhere on screen. This runs several times a second
       * while playing, and the re-check costs one getElementById. */
      if(this.el && this.cur && this.el.classList.contains('hidden') && !document.getElementById('ma-lib')){
        this.el.classList.remove('hidden');
        this._render();
      }
      this._tickApp();   // before the early return: the widget is hidden exactly when the app is up
      /* THE OS AND THE DESKTOP WIDGET TICK WHETHER OR NOT OUR OWN PANEL IS ON SCREEN — and this
       * block sat BELOW the early return, which is the whole of the bug.
       *
       * The floating player is hidden exactly when the Music app is up (see _tickApp above) and is
       * hidden ALWAYS on the windowed desktop, where CSS takes it out entirely. So on the desktop
       * this never ran: the Now-playing widget's bar stood still and its clock read 0:00 for the
       * length of the track — reported as "widget on the desktop was not showing progress". On
       * Android it is the same defect one step quieter: open the Music screen and the lock-screen
       * position stops advancing, because _media() is in here too.
       *
       * Neither surface belongs to our panel, so neither may be gated on it. Still throttled to once
       * a SECOND — timeupdate fires ~4x — so the widget costs no new timer and cannot tick while
       * nothing plays, which is what put it here in the first place. */
      { const sec=Math.floor((S._audioEl&&S._audioEl.currentTime)||0);
        if(sec!==this._msSec){ this._msSec=sec; this._media();
          try{ if(window.PCOS && PCOS.musicChanged) PCOS.musicChanged(); }catch(_){}
        } }
      if(!this.el||this.el.classList.contains('hidden')||this.min) return;
      const f=this.el.querySelector('.mp-seek-fill'), c=this.el.querySelector('.mp-cur'), du=this.el.querySelector('.mp-dur');
      if(S._audioEl && S._audioEl.duration){
        // Not while a drag owns it — repainting from currentTime here is what snaps the handle back
        // to the playhead on every timeupdate and makes the bar fight the finger moving it.
        if(f && this._scrub==null) f.style.width=((S._audioEl.currentTime/S._audioEl.duration*100)||0)+'%';
        if(c) c.textContent=_fmtTime(this._scrub!=null ? this._scrub*S._audioEl.duration : S._audioEl.currentTime);
        if(du) du.textContent=_fmtTime(S._audioEl.duration); }
    },
    onChange:null,   // the Music app mirrors this widget's state; see _musicAppNow
    _render(){
      try{ if(this.onChange) this.onChange(); }catch(_){}
      // …and the desktop's Now-playing widget, which is not the Music app and cannot use onChange
      // (a single slot the app owns). No-op when the desktop is not up.
      try{ if(window.PCOS && PCOS.musicChanged) PCOS.musicChanged(); }catch(_){}
      /* ABANDON ANY DRAG. This rebuilds the widget's innerHTML, so the bar a pointer is currently
       * captured on is about to be detached — and a detached element never fires pointerup or
       * pointercancel, so `_scrub` would stay set for ever. Both _tick and _tickApp refuse to paint
       * the fill while it is set, so the progress bar and the elapsed time freeze on BOTH transports
       * while the audio keeps playing. Easy to hit: _render runs on every state change, including
       * the auto-advance when a track ends. */
      this._scrub=null;
      this.ensure(); const d=this.el;
      /* The floating widget and the Music APP are the same transport. When the app is on screen the
       * widget is a second copy of its own controls, which is what "it loads the new player and the
       * old one at the same time" was. Keyed on the app actually being MOUNTED rather than on a flag,
       * so the widget comes back by itself the moment the app's window loses the feed or closes. */
      /* In the packaged Android app the foreground MusicService notification/widget is the
       * background transport. Drawing this legacy floating panel over whichever PosterChan app the
       * person opens creates a SECOND player UI even though both drive the same audio element. Keep
       * the engine alive, but keep its old browser widget out of the APK. Reopening Music paints the
       * full app from this object's unchanged state. */
      if(_capPlugin('MusicControls','addListener') || document.getElementById('ma-lib')){
        d.classList.add('hidden'); return;
      } const m=this.cur?_trackMeta(this.cur):null; const name=(m&&m.name)||'—';
      const playing=S._audioEl && !S._audioEl.paused; const pl=this._loading?'…':(playing?'⏸':'▶');
      const playLabel=this._loading?'Loading track':(playing?'Pause':'Play');
      if(this.min){
        d.className='mp mp-mini'+(playing?' playing':'');
        d.innerHTML=`<span class="mp-eq${playing?' on':''}">🎵</span><span class="mp-title" title="${enc(name)}">${enc(name)}</span><button class="mp-play" title="${playLabel}" aria-label="${playLabel}">${pl}</button><button class="mp-exp" title="Expand" aria-label="Expand player"><svg class="ic x-ic" aria-hidden="true"><use href="#i-expand"></use></svg></button>`;
      } else {
        d.className='mp'+(playing?' playing':'');
        d.innerHTML=`<div class="mp-scan"></div><div class="mp-head"><span class="mp-logo">🎵 NEON PLAYER</span><button class="mp-min" title="Minimize"><svg class="ic x-ic" aria-hidden="true"><use href="#i-minimize"></use></svg></button><button class="mp-close" title="Close"><svg class="ic x-ic" aria-hidden="true"><use href="#i-close"></use></svg></button></div>
          <canvas class="mp-viz"></canvas>
          <div class="mp-now" title="${enc(name)}">${enc(name)}</div>
          <div class="mp-seek"><div class="mp-seek-fill"></div></div>
          <div class="mp-time"><span class="mp-cur">0:00</span><span class="mp-dur">0:00</span></div>
          <div class="mp-controls"><button class="mp-prev" title="Previous"><svg class="ic x-ic" aria-hidden="true"><use href="#i-prev"></use></svg></button><button class="mp-play mp-big">${pl}</button><button class="mp-next" title="Next"><svg class="ic x-ic" aria-hidden="true"><use href="#i-next"></use></svg></button><button class="mp-shuffle${this.shuffle?' on':''}" title="Shuffle"><svg class="ic b-ic" aria-hidden="true"><use href="#i-shuffle"></use></svg></button></div>
          <div class="mp-search-row">${this._plPickHtml()}<input class="mp-search" type="search" placeholder="🔍 Search tracks…" value="${enc(this._search||'')}"></div>
          <div class="mp-list">${this._listHtml()}</div>`;
      }
      this._wire(); this._tick(); _updateMusicListBtns();
      if(!this.min && S._audioEl && !S._audioEl.paused) this._startViz();
    },
    // Phone/tablet width: the expanded player is the whole screen (see the media query in client.css).
    _full(){ try{ return matchMedia('(max-width:820px)').matches; }catch(_){ return false; } },
    _wire(){
      const d=this.el, qq=s=>d.querySelector(s), b=(s,fn)=>{ const e=qq(s); if(e) e.onclick=fn; };
      b('.mp-play',()=>this.toggle()); b('.mp-min',()=>this.setMin(true)); b('.mp-exp',()=>this.setMin(false));
      b('.mp-close',()=>this.close()); b('.mp-prev',()=>this.prev()); b('.mp-next',()=>this.next());
      b('.mp-shuffle',()=>{ this.shuffle=!this.shuffle; this._render(); });
      this.bindSeek(qq('.mp-seek'));
      { const ps=qq('.mp-pl'); if(ps) ps.onchange=()=>{
          this._pl = ps.value || null;
          // Playing follows the list you just chose — a picker that only changes what is listed is
          // half a control, and ⏭ would still walk the set you left.
          const t = this._plTracks();
          if(t && t.length) this.queue = t.map(x=>x.sha); else this.refreshQueue();
          try{ PC_setMusicPl(this._pl); }catch(_){}   // …and the Music app follows, if it is open
          this._render(); }; }
      const srch=qq('.mp-search');
      if(srch){ srch.oninput=()=>{ this._search=srch.value; const lst=qq('.mp-list'); if(lst){ lst.innerHTML=this._listHtml(); this._wireList(); } };
        srch.onkeydown=e=>{ if(e.key==='Enter'){ const s=this._shownShas(); if(s.length) this.play(s[0]); } }; }   // Enter = play first match
      this._wireList();
      /* Dragging is a DESKTOP idea. Expanded on a phone this player fills the screen, so there is
       * nowhere to drag it to — and _drag writes inline left/top/right/bottom, which would fight the
       * full-screen rule and strand the panel half off the edge. The mini pill still drags: that one
       * floats over whatever you are doing and gets in the way. */
      if(!(this._full() && !this.min)){
        this._drag(this.min ? d : (qq('.mp-head')||d));   // minimized: the whole mini-bar is the drag handle
      }else{
        // A window narrowed after a desktop drag still carries those inline offsets.
        ['left','top','right','bottom','width'].forEach(k=>{ try{ d.style[k]=''; }catch(_){} });
      }
    },
    // Search the WHOLE Music library by name (not just the current queue); empty search = the queue.
    /* THE WIDGET SEES PLAYLISTS TOO. It carries its own list and its own search, so without this it
     * was the one surface where a playlist did not exist — pick "Runs" in the Music app, minimise,
     * and the mini player's list was the whole library again. `_pl` is the widget's own selection so
     * it still works with the app closed, and the two are kept in step through PC.musicPlaylist. */
    _pl: null,
    _plTracks(){
      const PL = window.PCPlaylists;
      const pl = this._pl && PL && PL.get(this._pl);
      if(!pl) return null;
      const live = new Map(musicTracks(null).map(t=>[t.sha,t]));
      return pl.tracks.map(sha=>live.get(sha)).filter(Boolean);
    },
    _libTracks(){ return (this._plTracks() || musicTracks(null)).map(t=>({sha:t.sha, name:(t.m&&t.m.name)||'track'})); },
    _plPickHtml(){
      const PL = window.PCPlaylists;
      const ls = PL ? PL.all() : [];
      if(!ls.length) return '';                      // no playlists → no control to explain
      return `<select class="mp-pl" aria-label="Playlist">
        <option value=""${this._pl?'':' selected'}>All music</option>
        ${ls.map(p=>`<option value="${enc(p.id)}"${this._pl===p.id?' selected':''}>${enc(p.name)}</option>`).join('')}
      </select>`;
    },
    _shownShas(){ const q=(this._search||'').trim().toLowerCase();
      if(q) return this._libTracks().filter(t=>t.name.toLowerCase().includes(q)).map(t=>t.sha);
      return this.queue; },
    _listHtml(){ const playing=S._audioEl && !S._audioEl.paused; const shas=this._shownShas();
      if(!shas.length) return `<div class="muted small" style="padding:10px;text-align:center">${this._search?'No matches':'No tracks in Music yet'}</div>`;
      return shas.map(sha=>{ const mm=_trackMeta(sha)||{}; return `<button class="mp-track${sha===this.cur?' on':''}" data-sha="${sha}"><span class="mp-tnum">${sha===this.cur&&playing?'▶':'♪'}</span><span>${enc(mm.name||'track')}</span></button>`; }).join(''); },
    _wireList(){ const d=this.el; if(!d) return; d.querySelectorAll('.mp-list .mp-track').forEach(t=> t.onclick=()=>this.play(t.dataset.sha)); },
    _drag(handle){ if(!handle) return; const d=this.el; let sx,sy,ox,oy,on=false;
      const move=e=>{ if(!on) return; const p=e.touches?e.touches[0]:e; d.style.left=Math.max(0,Math.min(innerWidth-50,ox+p.clientX-sx))+'px'; d.style.top=Math.max(0,Math.min(innerHeight-40,oy+p.clientY-sy))+'px'; if(e.cancelable)e.preventDefault(); };
      const up=()=>{ on=false; removeEventListener('mousemove',move); removeEventListener('mouseup',up); removeEventListener('touchmove',move); removeEventListener('touchend',up); };
      const down=e=>{ if(e.target.closest('button')) return; const p=e.touches?e.touches[0]:e; const r=d.getBoundingClientRect(); on=true; sx=p.clientX; sy=p.clientY; ox=r.left; oy=r.top; d.style.right='auto'; d.style.bottom='auto'; d.style.left=ox+'px'; d.style.top=oy+'px'; addEventListener('mousemove',move); addEventListener('mouseup',up); addEventListener('touchmove',move,{passive:false}); addEventListener('touchend',up); };
      handle.onmousedown=down; handle.ontouchstart=down; },
    _startViz(){ if(this._setupViz()){ try{ this._viz.ctx.resume(); }catch(_){} this._drawViz(); } },
    _setupViz(){ const v=this._viz; if(v.an) return true; if(v.failed) return false;
      try{ const AC=window.AudioContext||window.webkitAudioContext; if(!AC){ v.failed=true; return false; }
        v.ctx=new AC(); v.src=v.ctx.createMediaElementSource(S._audioEl); v.an=v.ctx.createAnalyser();
        const low=v.ctx.createBiquadFilter(), mid=v.ctx.createBiquadFilter(), high=v.ctx.createBiquadFilter();
        low.type='lowshelf'; low.frequency.value=180; mid.type='peaking'; mid.frequency.value=1200; mid.Q.value=.8;
        high.type='highshelf'; high.frequency.value=4200; v.filters={low,mid,high};
        this._loadEQ(); for(const b of ['low','mid','high']) v.filters[b].gain.value=this._eq[b];
        v.an.fftSize=128; v.an.smoothingTimeConstant=0.82;
        v.src.connect(low); low.connect(mid); mid.connect(high); high.connect(v.an); v.an.connect(v.ctx.destination); return true;
      }catch(e){ v.failed=true; return false; } },
    /* THE SPECTRUM BARS, AND WHY THIS LOOP IS WRITTEN SO CAREFULLY.
     *
     * It ran three expensive things on EVERY animation frame, and the third one is the one that
     * reached outside the widget: `cv.clientWidth` is a layout READ, and in desktop mode the page it
     * measures against holds live windows and a full timeline — thousands of nodes — so this forced a
     * style+layout flush of the whole document 60 times a second, interleaved with whatever the user
     * was dragging. That is the same thrash the icon drag was fixed for one commit earlier
     * ("three synchronous layouts of a live timeline per move"); playing a song reintroduced it as a
     * permanent background load, which is why the desktop went smooth again the moment Music closed.
     *
     * Also per frame, and cheaper to see but not to run: assigning `cv.width`/`cv.height` REALLOCATES
     * and clears the canvas backing store even when the number is identical, and the bar loop built 42
     * fresh linear gradients (~2500 a second) each used for exactly one shadowed fillRect.
     *
     * Now: the size is measured only when it CHANGES (ResizeObserver where there is one, a 500ms
     * re-measure where there is not), the gradient is rebuilt only then, and the loop paints at ~30fps
     * — a 42-bar spectrum with 0.82 smoothing is indistinguishable at 60, and the analyser's own
     * smoothing means we are not sampling anything the eye was getting. Everything the frame does is
     * now confined to the canvas. */
    _drawViz(){ const v=this._viz; if(!v.an) return; if(v.raf) cancelAnimationFrame(v.raf);
      const dpr=Math.min(2,window.devicePixelRatio||1), data=new Uint8Array(v.an.frequencyBinCount);
      const BUCKETS=24;                          // gradient granularity — see below
      let W=0, H=0, cx=null, canvas=null, grads=null, stale=true, lastMeasure=0;
      /* A bar's gradient runs over ITS OWN height, so a quiet bar shows the same cyan→pink ramp as a
       * loud one — that is the look, and a single canvas-height gradient would have thrown it away
       * (short bars all cyan). Keeping it costs nothing once the ramps are cut into height buckets and
       * built with the canvas instead of with every bar of every frame: 24 is finer than the eye can
       * follow on a 40px-tall widget. */
      const measure=(cv)=>{
        const w=Math.floor(cv.clientWidth*dpr), h=Math.floor(cv.clientHeight*dpr);
        if(w===W && h===H && cx) return;
        W=w; H=h; cx=null; grads=null;
        if(!W||!H) return;
        cv.width=W; cv.height=H;                 // only on a real change — this clears the canvas
        cx=cv.getContext('2d');
        cx.shadowColor='#00f0ff'; cx.shadowBlur=7*dpr;
        grads=[];
        for(let b=0;b<BUCKETS;b++){
          const bh=Math.max(2*dpr, H*(b+1)/BUCKETS);
          const g=cx.createLinearGradient(0,H,0,H-bh);
          g.addColorStop(0,'#00f0ff'); g.addColorStop(.5,'#7df0ff'); g.addColorStop(1,'#ff2bd6');
          grads.push(g);
        }
      };
      // The observer is what lets the loop stop measuring at all. Without one (an older WebView) fall
      // back to a re-measure twice a second, which is still 30x less often than every frame.
      const watch=(cv)=>{
        if(canvas===cv) return;
        canvas=cv; stale=true;
        if(v.ro){ try{ v.ro.disconnect(); }catch(_){} v.ro=null; }
        if(typeof ResizeObserver!=='undefined'){
          try{ v.ro=new ResizeObserver(()=>{ stale=true; }); v.ro.observe(cv); }catch(_){ v.ro=null; }
        }
      };
      const stop=()=>{ v.raf=0; canvas=null; if(v.ro){ try{ v.ro.disconnect(); }catch(_){} v.ro=null; } };
      let last=-1e9;
      const loop=(ts)=>{ const appCv=document.getElementById('ma-viz');
        const floating=this.el && !this.el.classList.contains('hidden') && !this.min
          ? this.el.querySelector('.mp-viz') : null;
        // Music's full app takes ownership while mounted; otherwise the exact same analyser paints
        // the persistent player. No duplicate graph, decoder, song, or animation loop.
        const cv=appCv || floating;
        if(!cv || !S._audioEl || S._audioEl.paused) return stop();
        v.raf=requestAnimationFrame(loop);
        const now=ts||0;
        if(now-last < 32) return;                 // ~30fps
        last=now;
        watch(cv);
        if(stale || (!v.ro && now-lastMeasure > 500)){ stale=false; lastMeasure=now; measure(cv); }
        if(!cx || !grads) return;
        v.an.getByteFrequencyData(data); cx.clearRect(0,0,W,H);
        const n=Math.min(data.length,42), bw=W/n, minH=2*dpr, bar=Math.max(1,bw-2*dpr);
        for(let i=0;i<n;i++){
          const h=Math.max(minH,(data[i]/255)*H);
          cx.fillStyle=grads[Math.min(BUCKETS-1, Math.max(0, Math.round(h/H*BUCKETS)-1))];
          cx.fillRect(i*bw+dpr,H-h,bar,h);
        }
      };
      v.raf=requestAnimationFrame(loop); },
  };

  return {

    get MusicPlayer(){ return MusicPlayer; },
  };
};
