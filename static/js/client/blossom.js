/* blossom.js — split out of app.js by scripts/split_client_module.mjs.
 *
 * Loaded by its own <script> tag BEFORE app.js, and built synchronously by app.js (`_blossomMod()`) at
 * the point where this code used to run — every render path asks mediaServer()/_absUrl()/
 * notificationAllowed() synchronously, the composer's paste/drop upload resolves its target
 * synchronously, and the top-level statements below (the media-fetch ceiling wrapped around
 * window.fetch, the focus/online/storage listeners) must run in their original order. The code below is
 * app.js's own, moved byte-for-byte; its reads of app.js's live `let` bindings were rewritten to
 * `_S.<name>` (getters/setters on `dep.state`) at exact identifier offsets, and everything else it uses
 * arrives through `dep`. tests/test_client_module_deps.py proves every name resolves.
 * The state object is `_S`, not `S`: the moved code declares an `S` of its own.
 */
window.PCBlossomFactory = function(dep){
  const _S = dep.state;   // live app.js bindings: _S.AUTO_NEW_POSTS, _S.CFG, _S.GUEST, _S.ME, _S.NEW_POSTS_PILL, _S.NO_IMAGES, _S.VIEW, _S._blossomOK, _S._fileTok, _S._livePending, _S._onLandingView
  const {
    $, $$, _LIVE_READ_PX, _applyMediaCacheBudget, _capPlugin, _flushPending, _postEffectsOn,
    _prefTouched, _pushPlugin, _standalone, _stopCelebrations, _updateNewPostsPill, applyMobileNav,
    applyNavGroups, applyNavHidden, applyNavOrder, enc, navHiddenSet, publish, pushState,
    renderView, sign, stopNarration, switchView, toast,
  } = dep;

  // ---------- Blossom uploads + file browser ----------
  // The user's custom Blossom server only applies once they've enabled the override in Settings;
  // otherwise everything uses the built-in server from /client/config.
  function mediaServer(){
    let s = ClientSettings.get('blossomEnabled') ? (ClientSettings.get('mediaServer')||'').trim() : '';
    // accept a bare host ("blossom.example.com") — without a scheme fetch() would treat it as a
    // RELATIVE path and POST to poster.place instead of the user's server.
    if (s && !/^https?:\/\//i.test(s)) s = 'https://' + s;
    if (s) return s.replace(/\/+$/, '');         // user's own server (their CORS rules apply)
    // No instance = no built-in Blossom to fall back to. `_serverOrigin() + '/blossom'` would resolve
    // against the app's own bundle origin, so every upload would POST into the app itself and fail with
    // nothing to point at. nostr.build is the public NIP-96 host the client already falls back to for
    // users without upload permission, so it is the honest default here too.
    if (_standalone()) return NOSTR_BUILD;
    // Built-in server: hit it SAME-ORIGIN via /blossom (the app that serves this client also mounts
    // the Blossom router), so uploads/list/delete need NO CORS preflight. blossom_public_url (e.g.
    // media.poster.place) is only the PUBLIC url the server returns for each blob — used for sharing,
    // not for the API call — so cross-origin CORS can never block an upload again.
    return _serverOrigin() + '/blossom';   // real instance origin (https://localhost in the bundled app is useless)
  }

  /* ONE STUCK FETCH TAKES THE WHOLE MEDIA HOST DOWN FOR THE REST OF THE SESSION.
   *
   * Chromium keeps a pool of 6 sockets PER ORIGIN. A fetch that is never answered and never aborted
   * holds one of them for as long as the page lives, so six of them retire the origin permanently —
   * and nothing reports it, because the requests do not fail: they queue. Measured on a real desktop
   * after ~16 minutes of ordinary use:
   *
   *   fetch https://poster.place/client/config          -> 405 in 6ms
   *   fetch https://media.poster.place/list/<pubkey>    -> still pending at 60,000ms
   *   fetch https://media.poster.place/<sha> (1.35 MB)  -> resolved after 303,290ms, 0 bytes
   *   same blob, curl on the SAME machine               -> 200, 1,348,335 bytes, 26ms
   *
   * CDP showed `Network.requestWillBeSent` and then nothing at all, and firing three more fetches
   * opened NO new socket — proof the request never reached the network. Restarting the shell fixed
   * it instantly, which is what made this look like a dozen unrelated features breaking at random:
   * Files "times out", a folder shows nothing, an upload that genuinely succeeded never appears,
   * and the wallpaper picker says there are no pictures. Every one of those is this.
   *
   * The ceiling is on TIME-TO-HEADERS, not on the transfer. fetch() resolves when the headers land
   * and the body streams afterwards, so a 4 GB download is untouched while a request that never
   * gets a single byte is released. A caller that passes its own signal is left completely alone. */
  (function boundMediaFetches(){
    // Kept INSIDE: this function is lifted whole by its test, and a constant left outside would make
    // the guard pass a review and throw the moment it actually fired.
    const _MEDIA_HEADERS_TIMEOUT_MS = 45000;
    if (typeof window === 'undefined' || typeof window.fetch !== 'function') return;
    if (window.__pcMediaFetchBound) return;
    window.__pcMediaFetchBound = true;
    const _fetch = window.fetch.bind(window);
    // `/<sha>`, `/<sha>.png`, `/list/<pubkey>`, each optionally under a mount path.
    const BLOSSOM_SHAPE = /^\/(?:[^/]+\/)*(?:list\/)?[0-9a-f]{64}(?:\.[a-z0-9]{1,8})?$/i;
    function _watched(url){
      try{
        const u = new URL(url, location.href);
        if (!/^https?:$/.test(u.protocol)) return false;
        if (u.origin === location.origin) return false;   // same-origin: not the pool at issue
        let ms = ''; try{ ms = mediaServer() || ''; }catch(_){ ms = ''; }
        if (ms){ try{ if (new URL(ms, location.href).origin === u.origin) return true; }catch(_){} }
        return BLOSSOM_SHAPE.test(u.pathname);
      }catch(_){ return false; }
    }
    window.fetch = function(input, init){
      let url = '', method = 'GET', hasSignal = !!(init && init.signal);
      try{
        if (typeof input === 'string'){ url = input; }
        else if (input && typeof input === 'object'){
          url = input.url || '';
          /* A Request ALWAYS carries a non-null `signal`, whether or not the caller supplied one
           * (verified), so reading it as "the caller brought its own" would silently exempt every
           * Request-shaped call from the ceiling. Only an explicit init.signal counts. */
          if (typeof input.method === 'string') method = input.method;
        }
        if (init && typeof init.method === 'string') method = init.method;
      }catch(_){ url = ''; }
      /* NEVER BOUND A BODY-SENDING REQUEST. For a GET, `fetch` resolves when the response headers
       * land and the body streams afterwards, so a ceiling here bounds the WAIT and not the
       * transfer. For an upload the headers do not arrive until the body has been sent, so the same
       * ceiling becomes a total-upload ceiling: `uploadBlob` PUTs to `mediaServer()` with no signal
       * of its own, and on the desktop, the APK, a standalone build and any custom-server web user
       * that origin is exactly what `_watched` matches. A 300 MB video — or a 16 MB Folder Sync
       * chunk on a phone uplink — would abort at 45s, and uploadBlob's catch reports an AbortError
       * as "check that the server allows cross-origin (CORS) uploads", i.e. a working server
       * blamed, for ever, for every file above about 45s of uplink. */
      if (!/^(GET|HEAD)$/i.test(method)) return _fetch(input, init);
      if (hasSignal || !_watched(url)) return _fetch(input, init);
      let ctl; try{ ctl = new AbortController(); }catch(_){ return _fetch(input, init); }
      const timer = setTimeout(() => { try{ ctl.abort(); }catch(_){ } }, _MEDIA_HEADERS_TIMEOUT_MS);
      let p;
      try{ p = _fetch(input, Object.assign({}, init || {}, { signal: ctl.signal })); }
      catch(e){ clearTimeout(timer); throw e; }
      return p.then(r => { clearTimeout(timer); return r; },
                    e => { clearTimeout(timer); throw e; });
    };
  })();
  const NOSTR_BUILD='https://nostr.build';   // NIP-96 fallback host (not Blossom — uploads via uploadNip96)
  // (`_blossomOK` — the built-in upload permission checkBlossomAccess fills in — is app.js's: upload.js reads it too.)
  // The effective UPLOAD target as {url, proto}. proto is 'blossom' (BUD-02 PUT /upload + kind-24242) or
  // 'nip96' (POST multipart + NIP-98). Priority: the user's own enabled server (proto inferred/stored) →
  // else, if this user has NO built-in Blossom permission, default to nostr.build (NIP-96) so a brand-new
  // user can still upload out of the box → else the built-in /blossom.
  // Server origin for ABSOLUTE URLs we hand out (blossom media, shareable web links). In the bundled app
  // self.location.origin is https://localhost — useless off-device (the AI/relay/other users can't fetch it)
  // — so prefer the real server base injected by the app shim. PWA: __PC_API_BASE__ undefined → own origin.
  // Standalone deliberately returns '' rather than the bundle's own origin: an app:// or
  // https://localhost URL is meaningless to every other reader on the network, and handing one out as
  // if it were a media link produces notes with permanently broken images. Empty makes each caller's
  // own guard fire instead.
  function _serverOrigin(){ if(_standalone()) return ''; return (window.__PC_API_BASE__) || (self.location&&self.location.origin) || ''; }
  // True when the bundled app talks to a PLAIN-HTTP instance (an .onion, or a LAN box). The page origin
  // is https://localhost, so every API call is cross-origin → the session cookie must be SameSite=None,
  // which browsers only honour with Secure, which they in turn refuse to set over http. There is simply
  // no cookie that can work here; auth rides the Authorization header (see _setAiToken) and, for the
  // <img src> URLs that can't carry a header, a ?t= capability token. Declared HERE, above _absUrl's
  // only reader, so the `let` can't be read from its temporal dead zone.
  function _cleartextInstance(){ const b=window.__PC_API_BASE__||''; return !!b && b.slice(0,5)==='http:'; }
  // (`_fileTok`, the /client/file ownership token _absUrl appends, is app.js's: ensureFileAuth fills it.)
  // Absolutize a root-relative URL to the instance origin. Critical in the bundled app: an <img>/<video>
  // src resolves against the PAGE origin (https://localhost), NOT through the fetch shim — so a server URL
  // like /api/files/… or /blossom/… would load from localhost. No-op for already-absolute URLs and in the PWA.
  function _absUrl(u){
    u=(u==null?'':String(u));
    if(!(u.charAt(0)==='/' && u.charAt(1)!=='/')) return u;
    // /client/file is cookie-gated, and against a cleartext instance the cookie can't exist — pass the
    // same short-lived ownership token in the query instead. Central here so every <img>/<video>/link
    // that goes through _absUrl is covered at once rather than at each construction site.
    // /[?&]t=/ — NOT indexOf('t='), which any future param ending in t (format=, count=) would satisfy,
    // silently skipping the token and 403ing the image.
    if(_S._fileTok && _cleartextInstance() && u.indexOf('/client/file/')===0 && !/[?&]t=/.test(u))
      u += (u.indexOf('?')<0?'?':'&') + 't=' + encodeURIComponent(_S._fileTok);
    return _serverOrigin()+u;
  }
  function _blossomBuiltin(){
    if(_standalone()) return { url:NOSTR_BUILD, proto:'nip96' };   // no instance → no built-in server
    return { url:_serverOrigin()+'/blossom', proto:'blossom' };
  }
  function uploadTarget(){
    if(ClientSettings.get('blossomEnabled')){
      let s=(ClientSettings.get('mediaServer')||'').trim();
      if(s){ if(!/^https?:\/\//i.test(s)) s='https://'+s; s=s.replace(/\/+$/,'');
        // nostr.build is ALWAYS NIP-96 — the hostname wins over any stored/legacy proto (a legacy
        // kind-10063 record would otherwise mis-restore it as 'blossom' and fail the PUT). Other
        // hosts use the proto detected (by capability) at save time; default blossom.
        const proto=/(^|\.)nostr\.build$/i.test((()=>{try{return new URL(s).hostname;}catch(_){return '';}})())
          ? 'nip96' : (ClientSettings.get('mediaProto','')||'blossom');
        return { url:s, proto }; }
    }
    if(_S._blossomOK===false) return { url:NOSTR_BUILD, proto:'nip96' };
    return _blossomBuiltin();
  }
  // Detect a media server's protocol by CAPABILITY (does it publish a NIP-96 well-known?) rather than
  // by hostname — so any NIP-96 host works, not just nostr.build. Falls back to a hostname guess if the
  // probe is blocked (CORS) or unreachable.
  async function detectProto(url){
    const base=url.replace(/\/+$/,'');
    /* DO NOT ASK A SERVER WE ALREADY KNOW THE ANSWER FOR.
     *
     * This node's own media server speaks Blossom -- that is not a discovery question, it is a fact
     * about the software we are running. Probing it for a NIP-96 well-known can only ever fail, and
     * the failure is LOUD: the host answers 405 with no CORS header, so the browser console shows
     * "Cross-Origin Request Blocked ... Access-Control-Allow-Origin missing", which reads like a
     * broken media server. Reported as exactly that. The probe stays for every OTHER host, which is
     * the case it was written for -- detecting a NIP-96 server by capability rather than hostname. */
    try{
      const mine=(_blossomBuiltin().url||'').replace(/\/+$/,'');
      if(mine && (base===mine || base===_serverOrigin().replace(/\/+$/,''))) return 'blossom';
      /* `blossom_url` is what /client/config publishes for this node's own media server -- which is
       * how media.poster.place is reached at all, and the host the console error names. */
      const ours=String((_S.CFG&&_S.CFG.blossom_url)||'').replace(/\/+$/,'');
      if(ours && (base===ours || new URL(base).host===new URL(ours).host)) return 'blossom';
    }catch(_){ }
    try{ const r=await fetch(base+'/.well-known/nostr/nip96.json'); if(r.ok && await r.json().catch(()=>null)) return 'nip96'; }catch(_){}
    return /(^|\.)nostr\.build$/i.test((()=>{try{return new URL(base).hostname;}catch(_){return '';}})())?'nip96':'blossom';
  }
  // Query whether ME can actually upload to the built-in Blossom server. Sets _blossomOK so uploadTarget
  // can fall back to nostr.build ONLY for users who genuinely can't. Cheap, cached; called on login.
  async function checkBlossomAccess(){
    try{
      if(!_S.ME||!_S.ME.pubkey){ _S._blossomOK=null; return; }
      if(!_S.CFG.blossom_enabled){ _S._blossomOK=false; return; }   // built-in server off → nobody can use it
      const r=await fetch('/client/blossom-access?pubkey='+encodeURIComponent(_S.ME.pubkey)).then(r=>r.json());
      // Prefer `allowed` (the REAL upload gate: whitelist OR admin/can_blossom OR operator key) — an admin
      // who isn't explicitly whitelisted must NOT be diverted to nostr.build. Fall back to `whitelisted`
      // for older backends that don't send `allowed`.
      _S._blossomOK = !!(r && (r.allowed!==undefined ? r.allowed : r.whitelisted));
    }catch(_){ _S._blossomOK=null; }   // unknown → keep built-in default (don't wrongly divert to nostr.build)
  }
  // Boot-time RESTORE of the synced media server (kind-10063 Blossom / kind-10096 NIP-96) so a fresh
  // device uploads to the user's chosen server without first opening Settings. DOM-free (safe on boot);
  // the Settings modal reads the same ClientSettings when it renders. No-op if this device already has one.
  async function restoreMediaServer(){
    try{
      if(ClientSettings.get('mediaServer','') || !_S.ME || !_S.ME.pubkey) return;
      // Retry the query: on a fresh session over a high-latency link (Thailand→US) the first REQ can
      // EOSE empty before the relay serves the user's own kind-10063/10096 event → the media server
      // "never persisted". The event DOES exist, so a couple of retries with backoff find it.
      let rMedia=null;
      for(let attempt=0; attempt<3 && !rMedia; attempt++){
        if(attempt>0) await new Promise(r=>setTimeout(r, 500*attempt));
        const evs=await Relay.query([{ authors:[_S.ME.pubkey], kinds:[10063,10096], limit:4 }]);
        rMedia=evs.filter(e=>e.kind===10063||e.kind===10096).sort((a,b)=>b.created_at-a.created_at)[0];
      }
      const srv=rMedia && (rMedia.tags.find(t=>t[0]==='server')||[])[1];
      if(!srv) return;
      ClientSettings.set('mediaServer', srv);
      ClientSettings.set('mediaProto', rMedia.kind===10096?'nip96':'blossom');
      ClientSettings.set('blossomEnabled', true);
    }catch(_){}
  }
  // Sync a small set of NON-sensitive CLIENT prefs to Nostr (kind-30078, d=pcai:client-prefs) so they
  // follow you across devices: the image data-saver + the remembered tip amount. (The "attach my XMR
  // address to my posts" opt-in is DELIBERATELY per-device localStorage only — auto-enabling an
  // address-linking privacy setting on another device is not consent the user gave there.)
  // (`_prefTouched` — the keys changed THIS session, which a late restore must not revert — is app.js's:
  // every settings toggle adds to it.)
  /* The prefs object; `{}` when the relays AGREED there is no document yet (so a first save is
     safe); null when nothing answered. "Could not ask" is never "there is nothing there": the doc
     is REPLACEABLE, so merging a patch onto an empty read republishes it as JUST that patch and
     takes every pref this device did not load with it — one flaky moment while remembering a Monero
     tip amount would drop the zap presets, the BCH presets and the data-saver. `Relay.query` marks
     a set `complete` when every relay EOSE'd, which is the only thing that separates the two. */
  async function _readPrefs(owner=(_S.ME&&_S.ME.pubkey)){   // read the current prefs event, with retries (a laggy first REQ can EOSE empty)
    let ev=null, answered=false;
    for(let a=0; a<3 && !ev; a++){ if(a) await new Promise(r=>setTimeout(r, 450*a));
      try{ const evs=await Relay.query([{ authors:[owner], kinds:[30078], '#d':['pcai:client-prefs'], limit:1 }]);
        if(evs && evs.complete === true) answered=true;
        ev=(evs||[]).sort((x,y)=>(y.created_at-x.created_at)||String(x.id||'').localeCompare(String(y.id||'')))[0]||null; }catch(_){} }
    if(!ev) return answered ? {} : null;
    try{ const value=JSON.parse(ev.content||'{}')||{};
      if(typeof value!=='object'||Array.isArray(value))return {};
      Object.defineProperty(value,'_pcPrefsCreatedAt',{value:Number(ev.created_at)||0});
      return value;
    }catch(_){ return {}; }
  }
  /* The panel's Hide button is in the page shell (client.html), which runs before any module; it hands
     the change to us so it is saved to the account like the Settings switch. */
  try{ if(window.PCRightPanel) window.PCRightPanel.onchange = shown => { _prefTouched.add('rightPanel'); saveClientPrefsNostr({ rightPanel: !!shown }); }; }catch(_){}
  let _prefsSaveChain = Promise.resolve();
  function saveClientPrefsNostr(patch){
    if(!_S.ME || !_S.ME.pubkey) return Promise.resolve();
    const owner=_S.ME.pubkey;
    // SERIALIZE writes: each read-modify-write runs after the previous one's publish, so two concurrent
    // saves (e.g. a data-saver toggle + a presets Save) can't each read a stale `cur` and clobber the other.
    _prefsSaveChain = _prefsSaveChain.catch(()=>{}).then(async()=>{
      try{
        if(!_S.ME || _S.ME.pubkey!==owner)return;
        // Merge only the changed key(s) into the CURRENT remote value, so changing one pref can't wipe the
        // others a laggy restore hasn't loaded yet (the replaceable-list-wipe class).
        const cur = await _readPrefs(owner);
        // Nothing answered — skip the write instead of replacing the document with this patch alone.
        // What the user just set is already in ClientSettings on this device, so nothing they did is
        // lost here; the next save that CAN read the document carries it up.
        if(cur === null || !_S.ME || _S.ME.pubkey!==owner) return;
        const state=_notificationState(owner);
        const createdAt=Math.max(Math.floor(Date.now()/1000),state.clock+1,(cur._pcPrefsCreatedAt||0)+1);
        const merged={...cur,...(patch||{})};
        if(cur._pcPrefsCreatedAt && cur._pcPrefsCreatedAt<state.clock)
          merged.notificationPrefs={..._notificationClean(cur.notificationPrefs),...state.values,...state.dirty};
        const result=await publish(30078, JSON.stringify(merged), [['d','pcai:client-prefs']],{createdAt});
        if(result && result.ok){const latest=_notificationState(owner);latest.clock=Math.max(latest.clock,createdAt);_notificationStore(owner,latest);}
      }catch(_){}
    });
    return _prefsSaveChain;
  }
  async function restoreClientPrefsNostr(){
    if(!_S.ME || !_S.ME.pubkey) return;
    const owner=_S.ME.pubkey;
    const pr = await _readPrefs(owner);
    if(!pr || owner!==_notificationOwner()) return;
    _hydrateNotificationPreferences(owner,pr.notificationPrefs,pr._pcPrefsCreatedAt);
    if(Object.keys(_notificationState(owner).dirty).length)_syncNotificationPreferences(owner);
    try{
      if(!_prefTouched.has('noImages') && typeof pr.noImages==='boolean' && pr.noImages!==_S.NO_IMAGES){
        _S.NO_IMAGES=pr.noImages; ClientSettings.set('noImages', _S.NO_IMAGES);
        if(['home','global','notifications','messages','bookmarks','profile'].includes(_S.VIEW)){ try{ renderView(true); }catch(_){} }
      }
      if(!_prefTouched.has('newPostsPill') && typeof pr.newPostsPill==='boolean' && pr.newPostsPill!==_S.NEW_POSTS_PILL){
        _S.NEW_POSTS_PILL=pr.newPostsPill; ClientSettings.set('newPostsPill', _S.NEW_POSTS_PILL);
        _updateNewPostsPill();
      }
      if(!_prefTouched.has('autoNewPosts') && typeof pr.autoNewPosts==='boolean' && pr.autoNewPosts!==_S.AUTO_NEW_POSTS){
        _S.AUTO_NEW_POSTS=pr.autoNewPosts; ClientSettings.set('autoNewPosts', _S.AUTO_NEW_POSTS);
        // Synced ON while notes are already parked behind the pill → adopt them now (see the toggle handler).
        if(_S.AUTO_NEW_POSTS){ const feed=$('#feed');
          if(feed && (_S.VIEW==='home'||_S.VIEW==='global') && _S._livePending.length && feed.scrollTop <= _LIVE_READ_PX) _flushPending(); }
      }
      /* SAVED AND NEVER LOADED IS THE DEFAULT OUTCOME HERE. Writing a key in a toggle handler
         syncs it OUT for free (the patch is merged whole), but the way back in is a clause per key,
         hand-written — so a setting with no clause turns itself off again on every other device and
         reads as the toggle not sticking. No re-render: the gesture is read live from
         ClientSettings on each press, so adopting the value is the whole job. */
      if(!_prefTouched.has('readAloudHold') && typeof pr.readAloudHold==='boolean'
         && pr.readAloudHold!==ClientSettings.get('readAloudHold', true)){
        ClientSettings.set('readAloudHold', pr.readAloudHold);
        if(!pr.readAloudHold) stopNarration();
      }
      /* The right panel follows the ACCOUNT ("make it sync to the account"): hidden on one machine is
         hidden on all of them. Stored as `rightPanel` = shown. The per-app hiding is separate and
         untouched -- this only ever chooses between "where it belongs" and "nowhere". */
      if(!_prefTouched.has('rightPanel') && typeof pr.rightPanel==='boolean' && window.PCRightPanel
         && pr.rightPanel===window.PCRightPanel.hidden()){
        window.PCRightPanel.set(!pr.rightPanel);
        try{ const sw=$('#set-right-panel'); if(sw) sw.checked=pr.rightPanel; }catch(_){}
      }
      if(!_prefTouched.has('desktopBuddy') && pr.desktopBuddy && typeof pr.desktopBuddy === 'object'){
        ClientSettings.set('desktopBuddy', pr.desktopBuddy);
        try{ window.PCBuddy && window.PCBuddy.refresh(); }catch(_){}
        try{ const sw=$('#set-desktop-buddy'); if(sw) sw.checked = pr.desktopBuddy.on !== false; }catch(_){}
      }
      // Re-render on restore: `fn` is captured when a timeline draws, so adopting the synced value
      // without redrawing would leave the feed showing whatever the previous setting produced.
      if(!_prefTouched.has('hideReplies') && typeof pr.hideReplies==='boolean'
         && pr.hideReplies!==ClientSettings.get('hideReplies', false)){
        ClientSettings.set('hideReplies', pr.hideReplies);
        if(_S.VIEW==='home'||_S.VIEW==='global'){ try{ renderView(true); }catch(_){} }
      }
      // Landing timeline. The boot view is picked from the LOCAL cache (the relay hasn't answered yet),
      // so a device that has never opened Settings lands on the default and only learns the synced
      // choice here — adopt it, and move them if they're still sitting on that landing view and haven't
      // navigated. Anything else would repaint a view they deliberately opened.
      if(!_prefTouched.has('startTimeline') && (pr.startTimeline==='home'||pr.startTimeline==='global')){
        const was = ClientSettings.get('startTimeline','global');
        ClientSettings.set('startTimeline', pr.startTimeline);
        if(pr.startTimeline!==was && _S._onLandingView && (_S.VIEW==='home'||_S.VIEW==='global') && _S.VIEW!==pr.startTimeline){
          try{ switchView(pr.startTimeline); _S._onLandingView = true; }catch(_){}
        }
      }
      // The landing SCREEN rides the same rules as the landing timeline above, except it NEVER moves
      // the current view: home↔global is swapping one feed for its sibling, but yanking someone onto
      // Notes mid-boot because another device said so is a hijack — the synced value waits for the
      // next launch, where _startView also re-checks it against what THIS deployment actually shows.
      if(!_prefTouched.has('startView') && typeof pr.startView==='string' && /^[a-z0-9_-]{1,32}$/.test(pr.startView))
        ClientSettings.set('startView', pr.startView);
      /* The two STORAGE BUDGETS. localStorage is what a reinstall — or an app update that moves the
       * storage origin — takes with it, which is not hypothetical here: it is what made a synced
       * folder "no longer there" after a Windows update. How much of your own library you want kept
       * is a decision about the library, not about the device, so it follows the account; what a
       * given device can actually hold is still bounded by its own free space, which is why the stat
       * line under each setting reports the OS ceiling.
       * `musicOfflineGB` is checked with `>= 0` and not for truthiness, because 0 IS a value here —
       * it means no limit, and it is the default, so a `||` would silently drop the commonest one. */
      if(!_prefTouched.has('musicOfflineGB') && typeof pr.musicOfflineGB==='number' && pr.musicOfflineGB>=0)
        ClientSettings.set('musicOfflineGB', pr.musicOfflineGB);
      // Adopting a media budget means telling the service worker: the number alone changes nothing.
      // Deliberately no music trim here — a smaller limit arriving from another device would DELETE
      // tracks during boot, with nothing done on this device to explain it. The next download applies it.
      if(!_prefTouched.has('mediaCacheGB') && typeof pr.mediaCacheGB==='number' && pr.mediaCacheGB>0
         && pr.mediaCacheGB!==ClientSettings.get('mediaCacheGB', 4)){
        ClientSettings.set('mediaCacheGB', pr.mediaCacheGB);
        try{ _applyMediaCacheBudget(pr.mediaCacheGB); }catch(_){}
      }
      /* The sidebar rows this account turned off. Applied the moment it lands rather than at the next
       * reload — a sidebar that tidies itself two seconds in is the whole point of syncing it, and
       * `applyNavHidden` is idempotent. An ABSENT key is left alone, never read as "hide nothing":
       * every other pref here follows that rule and this one has more to lose, since a device that
       * cleared it would publish the empty list back over everyone else's choices.
       * A stale editor is worse than a stale sidebar — the switches ARE the state as far as the next
       * save is concerned — so the open pane is re-checked in place instead of re-rendered, which
       * would throw away whatever else is half-typed in it. */
      if(!_prefTouched.has('navHidden') && Array.isArray(pr.navHidden)){
        ClientSettings.set('navHidden', pr.navHidden.map(v=>String(v==null?'':v).slice(0,60)).filter(Boolean).slice(0,200));
        applyNavHidden();
        try{ const off=navHiddenSet();
             $$('#nav-hide-list input[data-navkey]').forEach(cb=>{ if(!cb.disabled) cb.checked = !off.has(cb.dataset.navkey); }); }catch(_){}
      }
      // The sidebar ORDER follows the same rules as the hides directly above: absent = untouched,
      // applied on landing, and a locally-changed value wins over a late restore.
      if(!_prefTouched.has('navGroupOf') && pr.navGroupOf && typeof pr.navGroupOf === 'object'){
        ClientSettings.set('navGroupOf', pr.navGroupOf);
        applyNavGroups();
      }
      if(!_prefTouched.has('navUserGroups') && Array.isArray(pr.navUserGroups)){
        ClientSettings.set('navUserGroups', pr.navUserGroups);
        applyNavGroups();
      }
      if(!_prefTouched.has('navOrder') && Array.isArray(pr.navOrder)){
        ClientSettings.set('navOrder', pr.navOrder.map(v=>String(v==null?'':v).slice(0,60)).filter(Boolean).slice(0,200));
        applyNavOrder();
      }
      if(!_prefTouched.has('tlHidden') && Array.isArray(pr.tlHidden)){
        ClientSettings.set('tlHidden', pr.tlHidden.map(v=>String(v==null?'':v).slice(0,20)).filter(Boolean).slice(0,3));
        try{ if(['home','global','trending'].indexOf(_S.VIEW)>=0) renderView(true); }catch(_){}
      }
      if(!_prefTouched.has('mobileNav') && Array.isArray(pr.mobileNav)){
        // Empties survive: '' is a chosen absence, and filtering it out would shift every slot
        // to the left of where its owner put it.
        ClientSettings.set('mobileNav', pr.mobileNav.map(v=>String(v==null?'':v).slice(0,60)).slice(0,4));
        applyMobileNav();
      }
      if(!_prefTouched.has('vimKeys') && typeof pr.vimKeys==='boolean') ClientSettings.set('vimKeys', pr.vimKeys);
      if(!_prefTouched.has('cleanLinks') && typeof pr.cleanLinks==='boolean') ClientSettings.set('cleanLinks', pr.cleanLinks);   // 🧹 link-tracker removal follows across devices
      if(!_prefTouched.has('xmrTip') && pr.xmrTip!=null && String(pr.xmrTip)) ClientSettings.set('xmrLastAmt', String(pr.xmrTip));
      if(!_prefTouched.has('bchTip') && pr.bchTip!=null && String(pr.bchTip)) ClientSettings.set('bchLastAmt', String(pr.bchTip));
      if(!_prefTouched.has('zapPresets') && pr.zapPresets!=null) ClientSettings.set('zapPresets', String(pr.zapPresets));   // user-defined amount presets follow across devices
      if(!_prefTouched.has('xmrPresets') && pr.xmrPresets!=null) ClientSettings.set('xmrPresets', String(pr.xmrPresets));
      if(!_prefTouched.has('bchPresets') && pr.bchPresets!=null) ClientSettings.set('bchPresets', String(pr.bchPresets));
      if(!_prefTouched.has('postEffects') && typeof pr.postEffects==='boolean' && pr.postEffects!==_postEffectsOn()){
        ClientSettings.set('postEffects', pr.postEffects);       // celebratory note effects follow across devices
        if(!pr.postEffects) _stopCelebrations();                 // synced to OFF → tear down anything already firing
        if(['home','global','notifications','messages','bookmarks','profile'].includes(_S.VIEW)){ try{ renderView(true); }catch(_){} }
      }
    }catch(_){}
  }
  // Account-scoped alert preferences. Pending field edits survive reload and delayed relay replies.
  const _NOTIFICATION_TYPES = [['email','Email'],['dm','Direct messages'],['likes','Likes and reactions'],
    ['replies','Replies'],['quotes','Quote posts'],['mentions','Mentions'],['reposts','Reposts'],
    ['zaps','Zaps and tips'],['concord','Concord mentions'],['channels','Chat rooms'],
    ['sms','Text messages'],['reminders','Reminders'],['follows','New followers']];
  const _NOTIFICATION_SOUNDS = ['chime','soft','bright','off'];
  function _notificationOwner(){ return (_S.ME && _S.ME.pubkey)||''; }
  function _notificationClean(value){
    const out={};
    if(!value || typeof value!=='object' || Array.isArray(value))return out;
    for(const [key] of _NOTIFICATION_TYPES)if(typeof value[key]==='boolean')out[key]=value[key];
    if(_NOTIFICATION_SOUNDS.includes(value.sound))out.sound=value.sound;
    return out;
  }
  function _notificationState(owner=_notificationOwner()){
    let raw={};try{raw=JSON.parse(localStorage.getItem('pc_notification_prefs:'+owner)||'{}')||{};}catch(_){}
    return {values:_notificationClean(raw.values),dirty:_notificationClean(raw.dirty),clock:Number.isSafeInteger(raw.clock)?raw.clock:0};
  }
  function _notificationStore(owner,state){
    localStorage.setItem('pc_notification_prefs:'+owner,JSON.stringify(state));
  }
  /* PUSH PREFERENCES ARE A SECOND, PER-DEVICE SET — and the separation is the feature.
   *
   * The set above is account-wide and syncs over kind-30078, which is right for "what interrupts me
   * in the app" and wrong for a phone: turning likes off on the phone silenced them on the desktop
   * too, because one list governed both. These live in localStorage on the device that owns them,
   * never sync, and are mirrored to that device's OWN PushSubscription row (POST /api/push/prefs,
   * scoped by device_id/endpoint) — so a desktop cannot be affected by a phone's choices.
   *
   * DEFAULT ON, like the in-app set: an unset preference has to mean "send", because a silenced
   * alert is indistinguishable from a lost one. app/services/push_prefs.py fails open for the same
   * reason at the other end. */
  function _pushPrefKey(owner=_notificationOwner()){ return 'pc_push_prefs:'+owner; }
  function _pushPrefState(owner=_notificationOwner()){
    let raw={};try{raw=JSON.parse(localStorage.getItem(_pushPrefKey(owner))||'{}')||{};}catch(_){}
    const out={};
    for(const [key] of _NOTIFICATION_TYPES)if(typeof raw[key]==='boolean')out[key]=raw[key];
    return out;
  }
  /* ONE name for the default. `pushPreference` (what the switch draws) and `_pushPrefsWire` (what
   * the filters are told) have to agree, or the screen shows one thing and the phone does another. */
  const _PUSH_PREF_DEFAULT=true;
  /* WHAT THE FILTERS ARE TOLD: EVERY TYPE, NEVER THE SUBSET SOMEBODY HAPPENED TO TOUCH.
   *
   * `_pushPrefState` is the STORED shape and it is deliberately sparse — a key appears only once it
   * has been switched, so the default can change later without rewriting everybody's document. That
   * is right for storage and wrong for the wire: both filters FAIL OPEN on a missing key (see
   * app/services/push_prefs.py, which argues it at length), so a type that is off by default, or
   * off through some path that never wrote a boolean, arrives as "no opinion" and is SENT.
   *
   * Reported as mentions arriving with only DMs, Zaps, Concord mentions and Texts selected — the
   * same shape as "likes when I only have DM's and concord mentions selected" before it. Resolve
   * every known type to its effective value here, so the filters are answering about a complete
   * picture and there is no gap for fail-open to widen. */
  function _pushPrefsWire(owner=_notificationOwner()){
    const stored=_pushPrefState(owner), out={};
    for(const [key] of _NOTIFICATION_TYPES)
      out[key]= typeof stored[key]==='boolean' ? stored[key] : _PUSH_PREF_DEFAULT;
    return out;
  }
  function pushPreference(key){
    const v=_pushPrefState()[key];
    return v===undefined ? _PUSH_PREF_DEFAULT : v;
  }
  function setPushPreference(key,value){
    const owner=_notificationOwner();if(!owner)return false;
    if(!_NOTIFICATION_TYPES.some(([k])=>k===key))return false;
    const next={..._pushPrefState(owner),[key]:!!value};
    try{localStorage.setItem(_pushPrefKey(owner),JSON.stringify(next));}catch(_){}
    _paintNotificationSettings();
    // The phone's own copy, first and unconditionally — see below. No argument: storage is
    // already written, so `_pushPrefsWire()` resolves EVERY type rather than this sparse delta.
    void _pushPrefsToDevice();
    _mirrorPushPrefsSoon(owner);
    return true;
  }
  /* THE PHONE'S OWN COPY OF THE ANSWER, written whenever the answer changes.
   *
   * `DirectPushStore` is the device-side half of the filter, and its whole reason to exist is that
   * the server "can only filter what it was told, and the telling can fail". It used to be written
   * from INSIDE mirrorPushPrefs — after that function's early returns, behind the same
   * `pushState()==='on'`/`!_standalone()` preconditions, in the same try as the server call it is
   * supposed to back up. A backstop that shares the failure mode of the thing it backs up is not a
   * backstop: any of those returning early left the phone storing nothing, and `allowsType` then
   * failed open on every type, which is "getting push notifications for likes when I only have DMs
   * selected".
   *
   * So it is its own call now, made from the toggle, and it depends on nothing but the plugin being
   * there. Fails silently by design — a phone that cannot store the answer still has the server
   * filter, and both fail OPEN (see app/services/push_prefs.py). */
  async function _pushPrefsToDevice(prefs){
    try{
      const S=_capPlugin('PosterChanPush','setPrefs');
      if(!S)return false;
      await S.setPrefs({prefs:prefs||_pushPrefsWire()});
      return true;
    }catch(_){ return false; }
  }
  /* One signature per settings session, not one per toggle. Flipping five switches would otherwise
   * be five signer prompts on a NIP-46/Amber account, which is how a preference screen becomes
   * something people back out of. */
  let _pushMirrorTimer=null;
  function _pushMirrorKey(owner=_notificationOwner()){ return 'pc_push_prefs_sent:'+owner; }
  // The prefs the server was last told, canonically. localStorage is already per-device, so the
  // device identity is implicit and must NOT be part of the mark — an id that changes on reinstall
  // would make this re-sign on every boot, which is the cost this exists to avoid.
  function _pushMirrorMark(prefs){
    const p=prefs||{};
    return JSON.stringify(Object.keys(p).sort().map(k=>[k,p[k]]));
  }
  /* ONE RE-SEND FOR A DEVICE THE SERVER WAS NEVER TOLD ABOUT.
   *
   * `mirrorPushPrefs` only ever ran from a toggle or from registration, so a phone that registered
   * before the device_id derivation was fixed keeps `prefs = NULL` on its row until somebody happens
   * to flip a switch — i.e. the reported bug survives the fix for everybody who already has it. This
   * re-sends once, and only when what the server holds provably differs from what this device
   * believes, because the mirror costs a SIGNATURE and a prompt at every boot is how a preference
   * screen becomes something people turn off.
   *
   * The phone's own copy is written unconditionally on the way past: it needs no signature, no
   * server and no registration, and it is the half that survives all three failing. */
  function _resendPushPrefsOnce(owner=_notificationOwner()){
    if(!owner)return;
    void _pushPrefsToDevice();
    /* MARK AND COMPARE THE SAME SHAPE. The mark is WRITTEN from what was actually sent (`body.prefs`,
     * the complete map), so comparing it against the sparse stored state never matches — and this
     * would ask the signer on every single boot, which on a NIP-46/Amber account is a prompt. */
    let sent=null; try{ sent=localStorage.getItem(_pushMirrorKey(owner)); }catch(_){}
    if(sent!==null && sent===_pushMirrorMark(_pushPrefsWire(owner)))return;
    void mirrorPushPrefs(owner);
  }
  function _mirrorPushPrefsSoon(owner=_notificationOwner()){
    clearTimeout(_pushMirrorTimer);
    _pushMirrorTimer=setTimeout(()=>{void mirrorPushPrefs(owner);},2500);
  }
  /* Reporting must never be able to break the thing it reports on: this runs from a timer and from
   * inside a catch, so a throw here would escape the function that was handling a failure. */
  function _pushSyncSaid(text){
    try{ const el=document.querySelector&&document.querySelector('#us-push-sync-state');
         if(el)el.textContent=text; }catch(_){}
  }
  /* TELL THE ACCOUNT WHICH WRAPS WERE OURS, so none of this person's devices is pushed about a
   * message they sent. The device-local record (PushPlugin.notePublished) only ever covers the
   * device that published; this covers the phone in your pocket when you sent it from the desktop,
   * and Web Push, which has no device-local map at all.
   *
   * THE SIGNATURE IS CACHED, and that is not an optimisation. A self-auth proof is valid for five
   * minutes either side, so signing one per DM would mean a signer prompt per message on every
   * NIP-07 and Amber setup — turning a fix for an annoying notification into a far more annoying
   * one. One signature covers a window of sending.
   *
   * Entirely best-effort: it is called AFTER the message is published, never awaited by the send
   * path, and every failure is silent. The worst case is the notification this exists to prevent,
   * which is exactly what happens today. */
  let _sentAuth = null, _sentAuthAt = 0, _sentAuthOwner = '';
  const _SENT_AUTH_TTL = 240000;   // inside the server's 300s window, with room for a slow request
  async function _notePublishedWraps(ids){
    try{
      if(!Array.isArray(ids) || !ids.length) return false;
      if(_standalone()) return false;               // no instance, no push watcher to tell
      const owner = _S.ME && _S.ME.pubkey; if(!owner || _S.GUEST) return false;
      const now = Date.now();
      if(!_sentAuth || _sentAuthOwner !== owner || now - _sentAuthAt > _SENT_AUTH_TTL){
        const ev = await sign(27235, 'push-sent', [['p', owner]]);
        if((_S.ME && _S.ME.pubkey) !== owner) return false;   // the account changed while we were signing
        _sentAuth = btoa(JSON.stringify(ev)); _sentAuthAt = now; _sentAuthOwner = owner;
      }
      const r = await fetch('/api/push/sent', {method:'POST',
        headers:{'Content-Type':'application/json'},
        body: JSON.stringify({pubkey: owner, auth: _sentAuth, ids})}).then(x=>x.json()).catch(()=>null);
      // A rejected proof is usually an expired one; drop it so the next send mints a fresh one.
      if(!r || !r.ok) _sentAuth = null;
      return !!(r && r.ok);
    }catch(_){ _sentAuth = null; return false; }
  }

  async function mirrorPushPrefs(owner=_notificationOwner()){
    try{
      if(!owner || owner!==_notificationOwner() || _standalone())return false;
      const state=await pushState();
      if(state!=='on')return false;      // nothing registered here — nothing to scope them to
      const body={pubkey:owner,prefs:_pushPrefsWire(owner)};
      /* SAY WHICH DEVICE. Unscoped, the server applies them to every device of the account, which
       * would put the desktop's push back under the phone's choices — the exact thing this split
       * exists to prevent. */
      const P=_pushPlugin();
      if(P){ /* ASK THE PLUGIN WHO THIS DEVICE IS; DO NOT PARSE ITS ENDPOINT STRING.
              *
              * This used to split the endpoint and require `direct:<x>:<id>`. The plugin has always
              * answered `"pcdirect:" + deviceId` — TWO fields, and a first field that is not
              * `direct` — so `bits[0]==='direct'` was false and `bits[2]` undefined on every
              * Android build there has ever been. `device_id` was therefore never set, the guard
              * below returned false before the POST, and every phone's PushSubscription row kept
              * `prefs = NULL`. push_prefs fails open on NULL, deliberately, so the server sent
              * everything while the Notifications tab showed likes switched off: "i am getting push
              * notifications for likes when I only have DM's and concord mentions selected".
              *
              * `getEndpoint()` returns `deviceId` as its own field and always has. That is the
              * authoritative answer and it cannot drift with a string format. */
             try{ const ep=(await P.getEndpoint())||{};
                  const id=String(ep.deviceId||ep.device_id||'').trim();
                  if(id)body.device_id=id;
                  else{ const bits=String(ep.endpoint||'').split(':');   // last resort, both spellings
                        if(bits.length>1&&/^(pc)?direct$/.test(bits[0]))body.device_id=bits[bits.length-1]; } }catch(_){}
             /* AND KEEP A COPY ON THE PHONE — see _pushPrefsToDevice, which is where the write now
              * lives. Repeated here so a device that registered before ever touching a toggle is
              * told too; it no longer DEPENDS on this function being reached. */
             await _pushPrefsToDevice(body.prefs); }
      else { try{ const reg=await navigator.serviceWorker.ready,sub=await reg.pushManager.getSubscription();
                  if(sub&&sub.endpoint)body.endpoint=sub.endpoint; }catch(_){} }
      if(!body.device_id && !body.endpoint)return false;   // never fall back to "all my devices"
      const auth=await sign(27235,'push-prefs',[['p',owner]]);
      const r=await fetch('/api/push/prefs',{method:'POST',headers:{'Content-Type':'application/json'},
        body:JSON.stringify({...body,auth:btoa(JSON.stringify(auth))})}).then(x=>x.json()).catch(()=>null);
      /* SAY WHICH IT WAS. "Telling the server…" left standing for ever is a status line that lies,
       * and the thing it would be lying about is the notifications you asked to stop. Retried once
       * on the way out, because the common failure here is a moment without a network. */
      if(r&&r.ok){ _pushSyncSaid('Saved on this device, and this device only.');
        // WHAT THE SERVER WAS ACTUALLY TOLD, so a boot can tell "already mirrored" from "never
        // mirrored" without asking the signer again. An install that predates the device_id fix has
        // no marker and re-sends once; after that a toggle is the only thing that costs a signature.
        try{ localStorage.setItem(_pushMirrorKey(owner), _pushMirrorMark(body.prefs)); }catch(_){}
        return true; }
      _pushSyncSaid('Saved on this device. The server has not been told yet — retrying.');
      clearTimeout(_pushMirrorTimer);
      _pushMirrorTimer=setTimeout(()=>{void mirrorPushPrefs(owner);},30000);
      return false;
    }catch(_){ _pushSyncSaid('Saved on this device. The server has not been told yet — retrying.');
               clearTimeout(_pushMirrorTimer);
               _pushMirrorTimer=setTimeout(()=>{void mirrorPushPrefs(owner);},30000);
               return false; }
  }
  function notificationPreference(key){
    const state=_notificationState(),v={...state.values,...state.dirty}[key];
    return v===undefined ? (key==='sound'?'chime':true) : v;
  }
  function notificationAllowed(type){return !_NOTIFICATION_TYPES.some(([key])=>key===type) || notificationPreference(type)!==false;}
  function _hydrateNotificationPreferences(owner,remote,createdAt=0){
    if(!owner || owner!==_notificationOwner())return;
    const state=_notificationState(owner);
    if(remote===undefined || remote===null){_paintNotificationSettings();return;}
    if(createdAt && createdAt<state.clock)return;
    state.clock=Math.max(state.clock,createdAt||0);
    state.values={..._notificationClean(remote),...state.dirty};
    _notificationStore(owner,state);_paintNotificationSettings();
  }
  function setNotificationPreference(key,value){
    const owner=_notificationOwner(),patch=_notificationClean({[key]:value});
    if(!owner || !Object.keys(patch).length)return Promise.resolve(false);
    const state=_notificationState(owner);
    state.values={...state.values,...patch};state.dirty={...state.dirty,...patch};
    _notificationStore(owner,state);_paintNotificationSettings();
    return _syncNotificationPreferences(owner);
  }
  function _syncNotificationPreferences(owner=_notificationOwner()){
    if(!owner)return Promise.resolve(false);
    _prefsSaveChain=_prefsSaveChain.catch(()=>{}).then(async()=>{
      if(owner!==_notificationOwner())return false;
      try{
        const remote=await _readPrefs(owner);
        if(owner!==_notificationOwner() || remote===null)return false;
        _hydrateNotificationPreferences(owner,remote.notificationPrefs,remote._pcPrefsCreatedAt);
        const state=_notificationState(owner),patch={...state.dirty};
        if(!Object.keys(patch).length)return true;
        const base=remote.notificationPrefs==null || (remote._pcPrefsCreatedAt && remote._pcPrefsCreatedAt<state.clock)
          ? state.values : _notificationClean(remote.notificationPrefs);
        const merged={...base,...patch};
        // Equal-second replaceable events choose the lower ID, not the last submitted choice.
        const createdAt=Math.max(Math.floor(Date.now()/1000),state.clock+1,(remote._pcPrefsCreatedAt||0)+1);
        const ev=await sign(30078,JSON.stringify({...remote,notificationPrefs:merged}),[['d','pcai:client-prefs']],createdAt);
        if(owner!==_notificationOwner() || !ev || ev.pubkey!==owner)return false;
        const result=await Relay.publish(ev);
        if(!result || !result.ok)return false;
        Store.saveEvent(ev);
        const latest=_notificationState(owner);
        for(const [key,value] of Object.entries(patch))if(latest.dirty[key]===value)delete latest.dirty[key];
        latest.values={...merged,...latest.dirty};latest.clock=Math.max(latest.clock,createdAt);_notificationStore(owner,latest);
        if(owner===_notificationOwner())_paintNotificationSettings();
        return true;
      }catch(_){return false;}
    });
    return _prefsSaveChain;
  }
  function _notificationPane(s){
    return `<div class="us-pane" data-pane="notifications" data-notification-owner="${enc(_notificationOwner())}">
      ${_standalone()?'':`<label class="fld">Notification email<input class="input" id="us-email" value="${enc(s.notification_email||'')}" placeholder="you@example.com"></label>`}
      <div class="set-title small">App alerts</div>
      <p class="muted small">Choose which events interrupt you. Messages and notification history remain available. Changes save immediately and sync with your account.</p>
      ${_NOTIFICATION_TYPES.map(([key,label])=>`<label class="fld" style="flex-direction:row;justify-content:space-between;align-items:center">${label}<label class="switch"><input type="checkbox" data-notification-type="${key}" ${notificationPreference(key)?'checked':''}><span class="slider"></span></label></label>`).join('')}
      <label class="fld">App arrival sound<select class="input" id="us-notification-sound">${_NOTIFICATION_SOUNDS.map(sound=>`<option value="${sound}"${notificationPreference('sound')===sound?' selected':''}>${sound==='off'?'Silent':sound[0].toUpperCase()+sound.slice(1)}</option>`).join('')}</select></label>
      <div class="set-actions"><button class="btn btn-ghost small" id="us-notification-preview">Preview sound</button><button class="btn btn-ghost small" id="us-notification-sync">Sync now</button></div>
      <div class="muted small" id="us-notification-sync-state" role="status"></div>

      <div id="us-push-section" hidden>
        <div class="set-title small">Push notifications on this device</div>
        <p class="muted small">What reaches you when the app is CLOSED. Kept on this device and never
          synced, so a phone set to mentions only leaves every other device exactly as it is — these
          are a separate answer from the app alerts above.</p>
        ${_NOTIFICATION_TYPES.map(([key,label])=>`<label class="fld" style="flex-direction:row;justify-content:space-between;align-items:center">${label}<label class="switch"><input type="checkbox" data-push-type="${key}" ${pushPreference(key)?'checked':''}><span class="slider"></span></label></label>`).join('')}
        <div class="muted small" id="us-push-sync-state" role="status"></div>
      </div>

      ${_standalone()?'':`
      <div class="set-title small">Where else they go</div>
      <p class="muted small">Delivery outside this app. These belong to your account on this instance.</p>
      <label class="fld">Notify me on Telegram about <span class="muted small">(comma list: news,downloads,mentions,inbox)</span><input class="input" id="us-tg-notif" value="${enc(s.telegram_notifications||'')}"></label>
      <label class="fld" style="flex-direction:row;justify-content:space-between;align-items:center">Relay notifications to Telegram<label class="switch"><input type="checkbox" id="us-social-notif" ${s.social_notif_enabled?'checked':''}><span class="slider"></span></label></label>
      <div class="muted small">Linking and unlinking Telegram itself stays under the Telegram tab.</div>`}

      <p class="muted small">Phone calls, carrier texts and Android background notification channels use your phone’s notification settings.</p>
      ${window.Capacitor?'<button class="btn btn-ghost small" id="us-notification-android">Open Android app settings</button>':''}
    </div>`;
  }
  function _paintNotificationSettings(){
    const pane=$('[data-notification-owner]');if(!pane || pane.dataset.notificationOwner!==_notificationOwner())return;
    $$('[data-notification-type]',pane).forEach(el=>el.checked=notificationPreference(el.dataset.notificationType));
    // The per-device push set hydrates from ITS OWN store, never from the synced one — reading the
    // account values into these boxes is how the two would quietly become one list again.
    $$('[data-push-type]',pane).forEach(el=>el.checked=pushPreference(el.dataset.pushType));
    const sound=$('#us-notification-sound',pane);if(sound)sound.value=notificationPreference('sound');
    const status=$('#us-notification-sync-state',pane);if(status)status.textContent=Object.keys(_notificationState().dirty).length?'Saved on this device. Waiting to sync.':'Saved.';
  }
  function _wireNotificationSettings(host){
    const pane=$('[data-notification-owner]',host);if(!pane)return;
    const owner=pane.dataset.notificationOwner;
    $$('[data-notification-type]',pane).forEach(el=>el.onchange=()=>{
      if(owner===_notificationOwner())setNotificationPreference(el.dataset.notificationType,el.checked);
    });
    $$('[data-push-type]',pane).forEach(el=>el.onchange=()=>{
      if(owner!==_notificationOwner())return;
      setPushPreference(el.dataset.pushType,el.checked);
      const st=$('#us-push-sync-state',pane);if(st)st.textContent='Saved on this device. Telling the server…';
    });
    /* REVEALED ONLY WHERE PUSH EXISTS. pushState() is async and the pane is built synchronously, so
     * the section ships hidden: offering "what reaches you when the app is closed" on a device that
     * has never registered for push is a control that cannot do anything. */
    void (async()=>{ try{
      const section=$('#us-push-section',pane);if(!section)return;
      const on=(await pushState())==='on';
      if(owner===_notificationOwner())section.hidden=!on;
    }catch(_){} })();
    $('#us-notification-sound',pane).onchange=event=>{if(owner===_notificationOwner())setNotificationPreference('sound',event.target.value);};
    $('#us-notification-preview',pane).onclick=()=>{if(owner===_notificationOwner())notificationSound(true);};
    $('#us-notification-sync',pane).onclick=()=>{if(owner===_notificationOwner())_syncNotificationPreferences(owner);};
    const android=$('#us-notification-android',pane);if(android)android.onclick=async()=>{
      const plugin=_capPlugin('PosterChanPush','openBatterySettings');
      try{if(plugin)await plugin.openBatterySettings();else toast('Open Android Settings → Apps → PosterChan → Notifications.');}
      catch(_){toast('Open Android Settings → Apps → PosterChan → Notifications.');}
    };
    _paintNotificationSettings();
  }
  let _notificationLastSound=0;
  function notificationSound(preview=false){
    const sound=notificationPreference('sound');if(sound==='off')return;
    // Android owns background channels; don't overlay a second WebView chime on its native alert.
    if(!preview && window.Capacitor)return;
    const now=Date.now();if(!preview && now-_notificationLastSound<500)return;
    _notificationLastSound=now;
    try{
      const Audio=window.AudioContext||window.webkitAudioContext;if(!Audio)return;
      const audio=new Audio(),t=audio.currentTime;
      if(audio.state==='suspended')audio.resume().catch(()=>{});
      const gain=audio.createGain();gain.connect(audio.destination);
      gain.gain.setValueAtTime(0.0001,t);
      gain.gain.exponentialRampToValueAtTime(sound==='soft'?0.025:0.05,t+0.025);
      gain.gain.exponentialRampToValueAtTime(0.0001,t+0.65);
      const frequencies=sound==='bright'?[659.25,987.77]:sound==='soft'?[392,523.25]:[523.25,783.99];
      frequencies.forEach(f=>{const osc=audio.createOscillator();osc.type='sine';osc.frequency.value=f;osc.connect(gain);osc.start(t);osc.stop(t+0.7);});
      setTimeout(()=>{try{audio.close();}catch(_){}},800);
    }catch(_){}
  }
  function _notificationType(opts){
    opts=opts||{};if(opts.notificationType)return opts.notificationType;
    const tag=String(opts.tag||''),route=String(opts.route||'');
    if(tag==='pc-mail'||route==='mail')return 'email';
    if(tag==='pc-dm')return 'dm';
    if(tag.startsWith('concord-')||route.startsWith('concord:'))return 'concord';
    if(tag==='pc-reminder')return 'reminders';
    /* Every text is tagged per CONVERSATION (`sms:<address>`) so two people cannot collapse into
       one card — see sms.js notifyNew — which is why this matches the prefix rather than a literal. */
    if(tag==='sms'||tag.startsWith('sms:')||route==='texts')return 'sms';
    return '';
  }
  let _notificationRefreshAt=0;
  function _refreshNotificationPreferences(){
    if(!_notificationOwner() || Date.now()-_notificationRefreshAt<30000)return;
    _notificationRefreshAt=Date.now();_syncNotificationPreferences();
  }
  window.addEventListener('focus',_refreshNotificationPreferences);
  window.addEventListener('online',()=>{_notificationRefreshAt=0;_refreshNotificationPreferences();});
  window.addEventListener('storage',event=>{
    if(event.key==='pc_notification_prefs:'+_notificationOwner())_paintNotificationSettings();
  });
  // END ACCOUNT NOTIFICATION PREFERENCES


  return {
    _absUrl, _blossomBuiltin, _notePublishedWraps, _notificationPane, _notificationType,
    _resendPushPrefsOnce, _serverOrigin, _wireNotificationSettings, checkBlossomAccess,
    detectProto, mediaServer, mirrorPushPrefs, notificationAllowed, notificationPreference,
    notificationSound, restoreClientPrefsNostr, restoreMediaServer, saveClientPrefsNostr,
    setNotificationPreference, uploadTarget,
  };
};
