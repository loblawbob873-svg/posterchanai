/* Settings — the settings screen (every tab of it: profile, timeline, notifications, sidebar,
 * relays, media, cache, zaps, keys, accounts, …), the signer screen (apps signed in through this
 * device, bunker links, NIP-55), and the relay-list editor. Split out of app.js.
 *
 * Loaded on first use: app.js keeps a small block of entry points (search for `_settingsDeps`) and
 * builds this factory the first time Settings or the signer screen opens. `_renderSignerApps` and
 * `drawRelayRows` repaint screens that only exist once this module does, so they do nothing before
 * it loads. The code below is BYTE-IDENTICAL to what it replaced in app.js apart from its reads of
 * app.js's live `let` bindings, which the parser rewrote to `S.<name>` (getters/setters on
 * `dep.state`) at exact identifier offsets.
 *
 * Stayed in app.js: normalizeRelay (every relay path uses it), the public fallback relays and
 * fetchFromPublicRelays (a thread fetch falls back to them), `_nostrPrefsLoaded`, and the signer's
 * startup battery check.
 */
window.PCSettingsFactory = function(dep){
  const S = dep.state;   // live app.js bindings: S.AUTO_NEW_POSTS, S.BLUR_NSFW, S.CFG, S.GUEST, S.IS_ADMIN, S.ME, S.MUTED_WORDS, S.NEW_POSTS_PILL, S.NO_IMAGES, S.VIEW, S._aiAuth, S._autoMuteEngine, S._autoMuteEpoch, S._autoMuteLoading, S._autoMuteMessage, S._blossomOK, S._livePending, S._nip65Confirmed, S._nostrPrefsLoaded, S._setRelays, S._vimPane
  const {
    $, $$, BUNDLED, INSTANCE_SETTINGS_TABS, MusicOffline, Nip46, Nip46Signer, Nwc, THEMES,
    THEME_SLUGS, _BCH_DEFAULTS, _CARRY_KEY, _LIVE_READ_PX, _XMR_DEFAULTS, _ZAP_DEFAULTS,
    _applyAutoMuteToView, _applyMediaCacheBudget, _autoMuteDocLoad, _autoMuteDocReady,
    _autoMuteDocSave, _autoMuteStored, _cacheAutoMute, _capPlugin, _clientBuild, _deleteAllMyNotes,
    _fillMediaCacheStat, _fillMusicOfflineStat, _flushPending, _fmtBytes, _hasNativeTor,
    _instanceBase, _langOptions, _loadAutoMute, _loginProviders, _navHideHtml, _navLabel,
    _normInstance, _notificationPane, _paintAutoMuteControls, _parsePresets, _postEffectsOn,
    _prefTouched, _scheduleAutoMutes, _setFediOnly, _sheet, _signerBackgroundHint, _standalone,
    _stopCelebrations, _updateAutoMutes, _updateNewPostsPill, _wireNavHide,
    _wireNotificationSettings, _wirePushToggle, _wireStayConnected, _withPhoneShell, applyTheme,
    carryPrivateToRelays, closeModal, copyValue, defaultRelays, detectProto, enc, ensureAiSession,
    logout, modal, normalizeRelay, openQrScanner, publish, qrImg, renderMessages, renderView,
    restoreMediaServer, saveClientPrefsNostr, saveMutedWords, sign, siteDefaultTheme,
    stashPrivateBeforeRelayChange, stopNarration, switchView, timeAgo, toast, uiConfirm, uiPrompt,
    userRelays,
  } = dep;

  /* The apps this device signs for — visible, and revocable one at a time.
   *
   * Without this the pairings were invisible: nothing on screen said an app was connected, and the
   * only way to end one was to clear site data, which ends all of them. A signer that cannot say
   * what it is signing for is not one anybody should trust with a key. */
  function _renderSignerApps(){
    const box = $('#signer-apps'); if(!box) return;
    const apps = (window.Nip46Signer ? Nip46Signer.list() : (typeof Nip46Signer !== 'undefined' ? Nip46Signer.list() : []));
    if(S.ME.mode !== 'local'){ box.innerHTML = ''; return; }
    /* When the NATIVE service owns steady state, the page's own tally sees nothing — the phone's
     * numbers are the only ones that can name a paired app stuck in a loop. Fetched once per
     * repaint, merged by pk, repainted when it lands (throttled: status() is a Keystore read). */
    if(Nip46Signer.nativeOn && !_renderSignerApps._busy
       && Date.now() - (_renderSignerApps._at || 0) > 5000){
      _renderSignerApps._busy = true;
      (async () => {
        try{
          const pl = _capPlugin('Signer', 'status');
          const st = pl && await pl.status();
          if(st && st.perApp){ _renderSignerApps._native = st.perApp; _renderSignerApps._at = Date.now(); }
        }catch(_){}
        _renderSignerApps._busy = false;
        // repaint with the merged numbers; the _at stamp above stops this from re-fetching
        if(_renderSignerApps._native) try{ _renderSignerApps(); }catch(_){}
      })();
    }
    const nat = _renderSignerApps._native || {};
    apps.forEach(a => { if(!a.stats && nat[a.pk]) a.stats = nat[a.pk]; });

    box.innerHTML = (apps.length
        ? '<div style="margin:6px 0 4px"><b>Signed in with this device</b></div>'
          + apps.map(a => `<div class="row" style="justify-content:space-between;align-items:center;gap:8px;margin:3px 0">
              <span>${enc(a.name || 'an app')}
                <span class="muted">· ${a.perms ? enc(String(a.perms.length)) + ' permissions' : 'all permissions'}${
                  /* WHEN IT WAS ADDED, not only when it was last used. Every primal.net login is
                   * called "PrimalWeb" and every login of ours is called "PosterChan", so a list of
                   * four is four identical rows — and one that has never been used carries no
                   * timestamp at all, which is exactly the row somebody is trying to find. Without
                   * this there is no way to tell which entry belongs to which device, and "revoke"
                   * is a guess. */
                  a.created ? ' · added ' + enc(timeAgo(a.created)) + ' ago' : ''}${
                  a.last ? ' · last used ' + enc(timeAgo(a.last)) + ' ago' : ' · ready to use'}</span></span>
              <button class="mini" data-revoke="${enc(a.pk)}">revoke</button></div>`).join('')
        : 'No apps are signed in with this device.')
      /* THE SECOND WAY IN, and it is not a nicety. Scanning the app's QR only works for apps that
       * SHOW one; nostrudel's signer login is a single text field wanting `bunker://…`, so without
       * this there is no way to log into it at all. One line, because space is tight on a phone. */
      + '<div class="row" style="margin-top:8px;gap:8px"><button class="mini" id="signer-bunker">'
      + 'Connect an app with a link</button>'
      /* Keep this visible even when the WebView has not recovered the native service's session
       * list yet. Android can still be signing for apps that this page currently reports as zero;
       * revokeAll() deliberately pushes an empty set into BOTH stores. Hiding the emergency stop
       * based on only one store made the control disappear in exactly that state. */
      + '<button class="mini" id="signer-revoke-all" style="color:var(--danger)">Revoke all apps</button>'
      + '</div>';

    $$('[data-revoke]', box).forEach(b => b.onclick = () => {
      Nip46Signer.revoke(b.dataset.revoke); toast('signed that app out'); _renderSignerApps(); });
    const bk = $('#signer-bunker', box);
    if(bk) bk.onclick = () => _showBunkerLink();
    const ra = $('#signer-revoke-all', box);
    if(ra) ra.onclick = async () => {
      if(!await uiConfirm('Revoke every app signed in with this device? They will all need to pair again.',
                          { ok:'Revoke all', cancel:'Keep them' })) return;
      Nip46Signer.revokeAll();
      _renderSignerApps._native = {};
      toast('all signer apps revoked');
      _renderSignerApps();
    };
    _signerBackgroundHint(box);
  }

  /* Hand out a bunker link: the QR for anything that can scan, the text for anything that cannot.
   *
   * Both are shown because the two clients that need this need different halves — a desktop app can
   * scan nothing, and a phone cannot easily paste into another phone. The copy goes through
   * `copyValue`, never `navigator.clipboard`, which is refused outright by the APK's WebView and by
   * the desktop build's app:// origin. */
  async function _showBunkerLink(){
    let uri;
    try{ uri = await Nip46Signer.bunkerUri(); }
    catch(e){ toast(String((e && e.message) || 'could not make a link')); return; }
    const img = qrImg(uri, 'bunker link');
    _sheet(
      '<h3 style="margin:0 0 6px">Connect an app</h3>'
      + '<div class="muted small" style="margin-bottom:8px">Paste this into the app’s “login with a '
      + 'signer” box, or scan it. It works once, and only for the next 10 minutes.</div>'
      + (img ? '<div style="text-align:center;margin-bottom:8px">' + img + '</div>' : '')
      + '<input id="bunker-uri" readonly value="' + enc(uri) + '" '
      + 'style="width:100%;font-size:11px;box-sizing:border-box">'
      + '<div class="row" style="gap:8px;margin-top:10px;justify-content:flex-end">'
      + '<button class="btn btn-ghost small" id="bunker-copy">Copy link</button>'
      + '<button class="btn btn-ghost small" id="bunker-done">Done</button></div>',
      (b, close) => {
        const f = b.querySelector('#bunker-uri');
        if(f) f.onclick = () => { try{ f.select(); }catch(_){} };
        b.querySelector('#bunker-copy').onclick = () => { copyValue(uri); toast('link copied'); };
        /* Closing the screen ends the offer AND the socket it needed. Minting a link reopens this
         * half's connection so it can hear a stranger; leaving it open after the window would be a
         * second idle socket on a phone for as long as the app lives, and would keep this half
         * listening to traffic the service is already handling. */
        b.querySelector('#bunker-done').onclick = () => {
          Nip46Signer._pending = null;
          if(Nip46Signer.nativeOn) Nip46Signer._standDown();
          close();
        };
      });
  }

  /* SIGNING IN THE BACKGROUND, on the phone. THERE IS NOW A SERVICE OF ITS OWN, and this comment
   * used to argue at length that there should not be. Both halves of that argument were wrong, so
   * the reasoning is kept rather than deleted — it is the reason the bug survived several rounds of
   * "fixed".
   *
   * It said the WebView was where the signing happened "because that is where the key is". The key
   * moved to the Keystore when the NIP-55 signer was built (`SignerKey`), so the native side has had
   * everything it needs to sign for a while — nothing was left in the WebView but the socket.
   *
   * It also said `StayAwakeService` already had the capability, so a second service would be "the
   * same code for no capability the first one lacks". That is the expensive mistake. StayAwake keeps
   * the PROCESS off the freezer so the WebView keeps its socket — a promise about the process, not
   * about the renderer. Chromium throttles a hidden page's timers to about one a minute regardless,
   * so a dropped socket was not redialled until the screen came on. Turning "stay connected" on did
   * not help, which is exactly what was reported, and pointed at the difference: "I have to wake the
   * phone for events to actually send from desktop."
   *
   * So the signer is `SignerRelayService` now — a foreground service holding its own WebSocket, with
   * no WebView in the path. It is started by the pairing itself rather than offered as an opt-in,
   * because unlike "stay connected" it is not a background convenience: pairing an app IS the
   * request to answer that app, and a signer that only answers while you are looking at it is not a
   * signer. It stops itself when nothing is paired, so the cost ends when the reason does. */
  /* SIGN FOR OTHER APPS ON THIS PHONE — the NIP-55 half, and the reason any of this is efficient.
   *
   * Another Nostr app fires an Intent at `nostrsigner:`; Android starts SignerActivity, it answers,
   * it exits. No service, no socket, no WebView, nothing running in between. That is categorically
   * cheaper than being reachable over a relay, which needs a browser engine resident and a
   * foreground service to keep it that way.
   *
   * THE KEY MOVES ONE WAY. It is handed to the native side and sealed under an AndroidKeyStore key;
   * there is no way to read it back out, deliberately (`SignerPlugin` has no `getKey`). That is a
   * security improvement on where it lives today — WebView storage is readable by any script that
   * gets into the page, and a Keystore key cannot be exported at all.
   *
   * LOCAL KEYS ONLY, for the obvious reason: with an extension or a remote signer this device does
   * not have a secret to hand over, and pretending otherwise would produce a signer that answers
   * every request with a failure. */
  async function _renderNip55(){
    const box = $('#signer-nip55'); if(!box) return;
    const P = _capPlugin('Signer', 'status');
    if(!P){ box.innerHTML=''; return; }                 // browser or desktop: nothing to register with
    if(S.ME.mode !== 'local'){
      box.innerHTML = '<div><b>Sign for other apps</b> — available when you sign in on this device '
        + 'with your key. Your key is in ' + (S.ME.mode === 'nip07' ? 'an extension' : 'a signer')
        + ', so this phone has nothing to hand over.</div>';
      return;
    }
    let st = {};
    try{ st = (await P.status()) || {}; }catch(_){ box.innerHTML=''; return; }
    /* `exposed`, not `have`. The background signer now stores a key of its own (SignerPlugin.arm),
     * so "there is a key on this phone" no longer means "other apps may use it" — and a panel that
     * conflated them would report this feature ON for everybody who merely paired a laptop.
     * `st.exposed === undefined` is an APK older than the split, where the two really were one. */
    const exposed = (st.exposed === undefined) ? !!st.have : !!st.exposed;
    if(exposed){
      box.innerHTML = '<div><b>Signing for other apps</b> — other Nostr apps on this phone can ask '
        + 'this one to sign, the way they would ask Amber. Nothing runs in the background; Android '
        + 'starts it only when an app asks. <button class="mini" id="nip55-off">turn off</button></div>';
      const b=$('#nip55-off',box);
      if(b) b.onclick=async()=>{ try{ await P.disable(); toast('this phone will no longer sign for other apps'); }
                                 catch(_){ toast('could not turn it off'); } _renderNip55(); };
      return;
    }
    box.innerHTML = '<div><b>Sign for other apps on this phone</b> — let other Nostr apps use this '
      + 'one as their signer, instead of installing Amber. Your key is sealed by Android and never '
      + 'leaves this device. <button class="mini" id="nip55-on">turn on</button></div>';
    const b=$('#nip55-on',box);
    if(b) b.onclick=async()=>{
      try{
        const sess = Session.load();
        const sec = sess && sess.sk;
        if(!sec) throw new Error('no local key in this session');
        const r = await P.enable({ sec });
        toast('this phone can now sign for other apps' + (r && r.pubkey ? '' : ''));
      }catch(e){ toast('could not turn it on: ' + ((e && (e.message||e.errorMessage)) || 'refused')); }
      _renderNip55();
    };
  }

  function renderSigner(){
    const feed=$('#feed');
    feed.innerHTML = `<div class="settings signer-app">
      <div class="signer-hero">
        <div class="signer-mark"><svg class="ic" aria-hidden="true"><use href="#i-shield"></use></svg><svg class="ic signer-mark-key" aria-hidden="true"><use href="#i-key"></use></svg></div>
        <div><h2>Your key, your approval</h2><p>Sign into Nostr apps without copying your private key around. PosterChan keeps it here and signs only when an app asks.</p></div>
      </div>
      <section class="set-card">
        <div class="set-head"><div><div class="set-title">Sign in another device</div>
          <div class="muted small">Scan the QR shown by a desktop or web app. Your private key never leaves this device.</div></div></div>
        <div class="set-body">
          <button class="btn btn-neon small" id="signer-scan-qr"${S.ME.mode === 'local' ? '' : ' disabled'}><svg class="ic b-ic" aria-hidden="true"><use href="#i-camera"></use></svg>Scan sign-in QR</button>
          ${S.ME.mode === 'local' ? '' : `<div class="muted small" style="margin-top:8px">Sign in here with a local key first. This session keeps its key in ${S.ME.mode === 'nip07' ? 'a browser extension' : 'another signer'}, so this device cannot sign for another app.</div>`}
          <div id="signer-apps" class="muted small" style="margin-top:12px"></div>
        </div>
      </section>
      <section class="set-card">
        <div class="set-head"><div><div class="set-title">Use with apps on this phone</div>
          <div class="muted small">Let Android Nostr apps ask PosterChan to sign, just like they would ask Amber.</div></div></div>
        <div class="set-body"><div id="signer-nip55" class="muted small"></div></div>
      </section>
    </div>`;
    { const sq=$('#signer-scan-qr'); if(sq) sq.onclick=()=>openQrScanner(); }
    _renderSignerApps();
    _renderNip55();
  }

  function renderSettings(){
    const feed=$('#feed');
    feed.innerHTML = `<div class="settings">
      <section class="set-card">
        <div class="set-head"><div><div class="set-title">Account</div>
          <div class="muted small">${enc(S.ME.npub.slice(0,20))}… · <span title="client build — the app.js ?v timestamp; matches on a fresh load">build ${_clientBuild()}</span></div></div></div>
        <div class="set-body">
          <div class="set-actions">
            <button class="btn btn-ghost small" id="set-copy-npub"><svg class="ic b-ic" aria-hidden="true"><use href="#i-key"></use></svg>Copy npub</button>
            ${S.IS_ADMIN?`<button class="btn btn-ghost small" id="set-admin" style="color:var(--neon,#0ff)"><svg class="ic b-ic" aria-hidden="true"><use href="#i-gear"></use></svg>Admin panel</button>`:''}
            ${S.ME.mode==='local'?`<button class="btn btn-ghost small" id="set-show-nsec" style="color:#ffcf2b"><svg class="ic b-ic" aria-hidden="true"><use href="#i-key"></use></svg>Show private key (nsec)</button>`:''}
            <button class="btn btn-ghost small hidden" id="set-google-link"><svg class="ic b-ic" aria-hidden="true"><use href="#i-key"></use></svg>Sign in with Google on other devices</button>
            <button class="btn btn-ghost small" id="set-sync-posts">⤓ Sync my data to this relay</button>
            <button class="btn btn-ghost small" id="set-logout"><svg class="ic b-ic" aria-hidden="true"><use href="#i-logout"></use></svg>Logout</button>
            <button class="btn btn-ghost small" id="set-del-notes" style="color:var(--danger)"><svg class="ic b-ic" aria-hidden="true"><use href="#i-trash"></use></svg>Delete all my posts</button>
            <button class="btn btn-ghost small" id="set-del-account" style="color:var(--danger)"><svg class="ic b-ic" aria-hidden="true"><use href="#i-trash"></use></svg>Delete my account</button>
          </div>
          <div class="muted small" id="set-sync-status">Pulls your posts back from other relays — and your notes, passwords, calendar and contacts from this server's private backup, if it has one.</div>
          <div class="muted small" id="set-del-notes-status"></div>
        </div>
      </section>
      <section class="set-card">
        <div class="set-head"><div><div class="set-title"><svg class="ic b-ic" aria-hidden="true"><use href="#i-bell"></use></svg>Notifications</div>
          <div class="muted small">Calls, messages, mentions, replies, reactions and zaps — delivered even when the app is closed.</div></div></div>
        <div class="set-body">
          <button class="btn btn-neon small" id="set-push-toggle"><svg class="ic b-ic" aria-hidden="true"><use href="#i-bell"></use></svg>Enable push notifications</button>
          <button class="btn small" id="set-push-test" style="margin-left:6px">Test</button>
          <div class="muted small" id="set-push-status" style="margin-top:6px"></div>
          <div id="set-stay-row" class="set-stay" hidden>
            <label><input type="checkbox" id="set-stay"> Stay connected in the background</label>
            <div class="muted small">Direct push normally reaches a closed PosterChan. This fallback
              keeps the client connection open on devices where push is unavailable, with the
              permanent notification Android requires, and starts again after a reboot.
              <strong>It uses more battery</strong>, so leave it off when push is working.</div>
          </div>
        </div>
      </section>
      <div id="user-settings"></div>
    </div>`;

    _wirePushToggle();
    _wireStayConnected();
    { const ab=$('#set-admin'); if(ab) ab.onclick=()=>switchView('admin'); }
    { const da=$('#set-del-account'); if(da) da.onclick=async()=>{
        if(!await uiConfirm('Permanently delete your account and all your AI chats + files on this server? This cannot be undone.')) return;
        try{ const auth=await sign(27235,'delete-account',[['p',S.ME.pubkey]]);
          const r=await fetch('/client/delete-account',{method:'POST',headers:{'Content-Type':'application/json'},
            body:JSON.stringify({pubkey:S.ME.pubkey,auth:btoa(JSON.stringify(auth))})}).then(r=>r.json());
          if(r && r.ok){ toast('account deleted'); Session.clear(); try{Relay.worker.call('clearKey',{});}catch(_){} setTimeout(()=>location.reload(),800); }
          else toast('delete failed: '+((r&&r.error)||''));
        }catch(_){ toast('delete failed'); }
      }; }
    { const dn=$('#set-del-notes'); if(dn) dn.onclick=()=>_deleteAllMyNotes(); }
    { const cn=$('#set-copy-npub'); if(cn) cn.onclick=()=> copyValue(S.ME.npub, 'npub copied', 'Your npub:'); }
    { const sn=$('#set-show-nsec'); if(sn) sn.onclick=async()=>{
        let r; try{ r=await Relay.worker.call('exportNsec', {}); }catch(_){ r=null; }
        const nsec=r&&r.nsec; if(!nsec){ toast('secret key not available on this login'); return; }
        modal(`<h3><svg class="ic h-ic" aria-hidden="true"><use href="#i-key"></use></svg>Your private key (nsec)</h3>
          <p class="muted small" style="color:#ff9b6b">Anyone with this key has FULL control of your account. Never share it. Store it somewhere safe — it's the only way to recover your account.</p>
          <div class="keyrow"><code id="nsec-val">${enc(nsec)}</code></div>
          <div class="set-actions"><button class="btn btn-neon small" id="nsec-copy"><svg class="ic b-ic" aria-hidden="true"><use href="#i-link"></use></svg>Copy nsec</button><button class="btn btn-ghost small" id="nsec-close">Close</button></div>`,
          root=>{
            $('#nsec-copy',root).onclick=()=> copyValue(nsec, 'nsec copied — keep it secret!', 'Your nsec (copy it):');
            $('#nsec-close',root).onclick=closeModal;
          });
      }; }
    // Attach Google to the key you ALREADY have, so it can sign you in elsewhere. Only offered when
    // the node runs Google sign-in — and only for a local key, because this uploads the secret key and
    // a signer login (NIP-07/46/55) has no key here to upload. The warning is not boilerplate: it is
    // the difference between "the server can identify me" and "the server can post as me".
    { const gl=$('#set-google-link'); if(gl && S.ME.mode==='local'){
        _loginProviders().then(p=>{ if(p && p.google) gl.classList.remove('hidden'); });
        gl.onclick=async()=>{
          if(!await uiConfirm('Link Google to this account?\n\nYour SECRET KEY is uploaded to this server so that signing in with Google can restore this identity on another device. The operator can then read it. Only do this if you trust this server.')) return;
          let nsec=''; try{ const r=await Relay.worker.call('exportNsec',{}); nsec=(r&&r.nsec)||''; }catch(_){}
          if(!nsec){ toast('secret key not available on this login'); return; }
          try{
            const auth=await sign(27235,'google-link',[['p',S.ME.pubkey]]);
            const r=await fetch('/api/auth/google/link/start',{method:'POST',headers:{'Content-Type':'application/json'},
              body:JSON.stringify({pubkey:S.ME.pubkey,auth:btoa(JSON.stringify(auth)),nsec})});
            const j=await r.json().catch(()=>({}));
            if(!r.ok || !j.auth_url) throw new Error(j.detail||'could not start');
            location.href=j.auth_url;
          }catch(e){ toast((e&&e.message)||'could not link Google'); }
        };
      } }
    { const lo=$('#set-logout'); if(lo) lo.onclick=async()=>{ if(await uiConfirm('Log out of this device?')) logout(); }; }
    { const sp=$('#set-sync-posts'); if(sp) sp.onclick=async()=>{
        const st=$('#set-sync-status'); if(st) st.textContent='syncing… pulling your data back from other relays.';
        try{ const auth=await sign(27235,'sync-posts',[['p',S.ME.pubkey]]);
          const r=await fetch('/client/sync-posts',{method:'POST',headers:{'Content-Type':'application/json'},
            body:JSON.stringify({pubkey:S.ME.pubkey,auth:btoa(JSON.stringify(auth))})}).then(r=>r.json());
          /* Say which half ran. Notes, the vault, the calendar and contacts are deliberately never
           * sent to the public relays, so they can only come back from a private mirror this server's
           * operator configured — and on a server with none there is no second copy to restore. That
           * has to be said out loud: "sync started" over a vault that was never backed up reads as a
           * promise, and the person only finds out it wasn't one when they need it. */
          /* Name only what this server can actually deliver. `private` means there is a mirror to
           * read from; `server_libs` additionally means the storage key those libraries were signed
           * with resolved. Claiming the calendar back without both is the expensive kind of wrong —
           * an empty calendar after a "successful" sync reads as "my data is gone" and the person
           * stops looking for it. */
          if(st) st.textContent = !r.ok ? ('failed: '+(r.error||''))
            : r.server_libs ? '✓ Sync started — your posts, notes, passwords, calendar and contacts will appear shortly.'
            : r.private ? '✓ Sync started — your posts, notes and passwords will appear shortly.'
                        : '✓ Sync started — your posts will appear shortly. This server keeps no private backup, so your notes, passwords and calendar can only be restored from a device that still has them.';
        }catch(_){ if(st) st.textContent='sync failed'; }
      }; }
    renderUserSettings();   // tabbed User Settings — incl. the moved Relays / Media / Zaps / Muted tabs
  }
  // Retrieve relay list (NIP-65 10002) + Blossom servers (BUD-03 10063) from Nostr. Only fills the
  // UI when this device hasn't set them locally — so a fresh device inherits your synced choices.
  async function loadNostrPrefs(){
    try{
      const evs=await Relay.query([{ authors:[S.ME.pubkey], kinds:[10002], limit:2 }]);
      const r10002=evs.filter(e=>e.kind===10002).sort((a,b)=>b.created_at-a.created_at)[0];
      if(r10002 && !userRelays().length){
        const urls=r10002.tags.filter(t=>t[0]==='r'&&t[1]).map(t=>normalizeRelay(t[1])).filter(Boolean);
        if(urls.length){ S._setRelays=urls; drawRelayRows(); }
      }
    }catch(_){}
    // Media server restore is the SINGLE source of truth (restoreMediaServer, also run at login); here we
    // just apply it (no-op if already set) then reflect the current choice into the settings inputs.
    await restoreMediaServer();
    const srv=ClientSettings.get('mediaServer','');
    if(srv && ClientSettings.get('blossomEnabled')){
      // A server was restored from Nostr AFTER the pane rendered — reflect it into the 3-way choice:
      // nostr.build → the 'nostrbuild' radio, anything else → 'custom' with the URL filled.
      const isNb=/(?:^|\/\/)(?:www\.)?nostr\.build\/?$/i.test(srv);
      const r=$(`input[name=media-mode][value=${isNb?'nostrbuild':'custom'}]`); if(r) r.checked=true;
      if(!isNb){ const mi=$('#set-media'); if(mi && !mi.value) mi.value=srv; }
      // Re-run the mode sync so the URL body enables/disables and the "uploads go to…" line updates.
      const r2=$('input[name=media-mode]:checked'); if(r2) r2.dispatchEvent(new Event('change'));
    }
  }
  // Per-user settings — faithful port of the old web-UI modal (6 tabs). Loads /api/auth/settings,
  // saves text/toggles via PUT, and wires the real connect flows (Telegram link,
  // Pleroma OAuth, Nostr key) to their existing endpoints.
  let _usMail=[];   // load relay/media prefs from Nostr ONCE per session, not on every re-render
  let _userSettingsRender=0;
  async function renderUserSettings(){
    const host=$('#user-settings'); if(!host) return;
    const generation=++_userSettingsRender,owner=S.ME&&S.ME.pubkey;
    const current=()=>generation===_userSettingsRender&&S.VIEW==='settings'&&(S.ME&&S.ME.pubkey)===owner&&$('#user-settings')===host&&host.isConnected!==false&&host.firstChild===renderedRoot;
    const fields=[...host.querySelectorAll('input,textarea,select')],values=()=>fields.map(el=>el.tagName==='SELECT'?[...el.options].map(o=>o.selected):[el.value,el.checked]);
    const originalValues=JSON.stringify(values()),hadEdits=fields.some(el=>el.tagName==='SELECT'?[...el.options].some(o=>o.selected!==o.defaultSelected):el.type==='checkbox'||el.type==='radio'?el.checked!==el.defaultChecked:el.value!==el.defaultValue);
    const unchanged=()=>!hadEdits&&JSON.stringify(values())===originalValues;
    let loading=null;
    if(!host.children.length&&!host.textContent.trim()){
      host.innerHTML='<section class="set-card" data-settings-loading><div class="set-body"><p role="status" aria-live="polite">Loading your settings…</p></div></section>';
      loading=host.querySelector('[role="status"]');
    }
    const renderedRoot=host.firstChild;
    const status=text=>{if(current()&&loading&&loading.isConnected)loading.textContent=text;};

    // /api/auth/settings needs the nostr-login session cookie. Establish it FIRST — otherwise the
    // very first open 401s (cookie not set yet) and shows "Couldn't load", and you had to click
    // Settings a second time once the session warmed (the flicker/"do it twice" bug).
    // Load settings FIRST. If this fails we must NOT render an empty editable form — saving it would
    // wipe the user's real settings with blanks (that's how telegram_notifications got cleared).
    // Retry with backoff: over a high-latency link (e.g. Thailand→US) the session cookie can lag the
    // first fetch → a 401 → the old one-shot "Couldn't load" (you had to hit Retry). ensureAiSession
    // caches only a GOOD session, so re-calling it re-establishes the cookie after a transient failure.
    // Perf/robustness (stale-while-revalidate): read the last-good cached settings up front so a slow or
    // flaky link doesn't strand the user on a spinner or the "Couldn't load" error. We still fetch fresh
    // and prefer it; the cache is only a FALLBACK when the network fails. It's REAL last-good data (never
    // an empty object), so rendering + saving from it can't wipe settings with blanks — the hazard is only
    // an empty form, which we still refuse below.
    let _cachedS=null; try{ const _c=localStorage.getItem('pc_settings_cache'); if(_c){ const p=JSON.parse(_c); if(p && typeof p==='object') _cachedS=p; } }catch(_){}
    let s=null;
    // Standalone: there is no /api/auth/settings, so the loop below would spend ~2.4s failing and then
    // show "Couldn't load your settings" — on the ONE screen a server-less user cannot do without, since
    // it is where relays and the instance are set. An empty object is the correct answer, not a fallback:
    // every field that lives on a server is a pane that INSTANCE_SETTINGS_TABS has already dropped, and
    // the wipe this guard exists to prevent needs a server to wipe.
    const _solo = _standalone();
    let authError=null;
    for(let attempt=0; _solo ? false : attempt<3; attempt++){
      const waiting=setTimeout(()=>status(S.ME&&S.ME.mode==='nip46'&&(Nip46._inflightP||(Nip46._queueP||[]).length)?'Waiting for your phone signer…':'Establishing your app session…'),1000);
      try{ await ensureAiSession(); }
      catch(e){ authError=e; break; }
      finally{clearTimeout(waiting);}          // no credential means no protected GET
      if(!current()||!unchanged()) return;   // stale account/view or edits made during authentication
      status('Loading your settings…');
      try{ const r=await fetch('/api/auth/settings'); if(!current()||!unchanged())return; if(r.ok){ s=await r.json(); break; }
           // 401 = the cached session is STALE (server session expired / restarted). ensureAiSession
           // caches _aiAuth forever, so re-calling it would just return the dead session — clear it so
           // the next attempt re-signs and re-establishes the cookie. Without this the retry loop is a
           // no-op and you're stuck on "Couldn't load your settings" until a full reload.
           if(r.status===401) S._aiAuth=null;
      }catch(_){}
      // NOTE: we do NOT short-circuit on the first attempt when a cache exists — the retry loop is what
      // recovers from a transient 401 (cookie lag on a high-latency link). Breaking early would serve STALE
      // settings that a later Save then writes back, reverting changes made on another device. The cache is
      // a genuine-offline fallback only (used below after all attempts fail).
      if(!current()||!unchanged())return;
      await new Promise(r=>setTimeout(r, 400*(attempt+1)));   // brief backoff before re-warming + refetching
      if(!current()||!unchanged())return;
    }
    if(!current()||!unchanged()) return;
    if(authError){
      host.innerHTML=`<section class="set-card"><div class="set-body"><div class="muted">${enc(
        (authError&&authError.message)||'could not establish your app session')}</div>
        <button class="btn btn-ghost small" id="us-retry">Retry</button></div></section>`;
      const retry=$('#us-retry',host); if(retry) retry.onclick=renderUserSettings;
      return;
    }
    if(_solo) s={};                    // no server to hold account settings — the client-side ones still apply
    else if(s && typeof s==='object'){ try{ localStorage.setItem('pc_settings_cache', JSON.stringify(s)); }catch(_){} }
    else if(_cachedS){ s=_cachedS; }   // network failed but we have last-good settings → show them, not an error
    if(!s || typeof s!=='object'){
      host.innerHTML='<section class="set-card"><div class="set-body"><div class="muted">Couldn’t load your settings.</div><button class="btn btn-ghost small" id="us-retry">Retry</button></div></section>';
      const rt=$('#us-retry'); if(rt) rt.onclick=renderUserSettings; return;
    }
    _usMail = Array.isArray(s.mail_accounts)? s.mail_accounts.slice() : [];
    // The ACCOUNT value seeds the select, not the localStorage cache. Preferring the cache made every
    // Save write the CACHE back to the account: a device that had painted the old site default
    // ("professional", still the fallback in client.py:_default_theme) cached it via applyTheme, the
    // dropdown then showed Professional no matter what the account said, and Save clobbered the account
    // with it — the "hit Save and my theme reverts to professional" bug, and why 80 accounts hold that
    // slug against a 'cyberpunk' column default. loadThemeFromServer already calls the account value
    // authoritative; this now agrees with it instead of contradicting it. Still no applyTheme() on open,
    // so an unsaved live preview is not reverted just by re-rendering.
    let _cachedTheme; try{ _cachedTheme=localStorage.getItem('pc_theme'); }catch(_){}
    let _curTheme=s.theme||_cachedTheme||siteDefaultTheme();
    if(!THEME_SLUGS.has(_curTheme)) _curTheme=siteDefaultTheme();   // stale/removed slug → don't desync the dropdown
    // Panes that need a server drop out entirely with no instance — see INSTANCE_SETTINGS_TABS.
    // Tor gets a tab only where there is a Tor control to put in it — the desktop shell (native tor)
    // or Android (Orbot). It is deliberately NOT in INSTANCE_SETTINGS_TABS: a relays-only install is
    // exactly where someone is most likely to want everything routed through Tor.
    const _torTab = (_hasNativeTor() || !!window.Capacitor) ? [['tor','Tor']] : [];
    /* 📱 Phone — the launcher, messages and dialer roles. Its own tab, and only on the packaged app:
     * the three switches ask ANDROID for a system role, so on the web and on the desktop shell there
     * is nothing for them to ask. Not in INSTANCE_SETTINGS_TABS, because none of it needs a server —
     * a phone running PosterChan with no instance at all is exactly where this matters most. */
    const _phoneTab = window.Capacitor ? [['phone','Phone']] : [];
    // 🧭 Sidebar is its OWN tab, not a block in Profile: it is ~35 switches, which inside a pane of
    // unrelated settings is a wall you scroll past rather than a thing you go to.
    const tabs=[['profile','Profile'],['timeline','Timeline'],['notifications','Notifications'],['sidebar','Sidebar'],['relays','Relays'],..._phoneTab,..._torTab,['media','Media'],['cache','Cache'],['zaps','Zaps'],['privacy','Privacy'],['muted','Muted'],['mail','Mail'],['telegram','Telegram'],['social','Social'],['keys','API Keys']]
      .filter(t => !(_standalone() && INSTANCE_SETTINGS_TABS.has(t[0])));
    // Standalone has no built-in relay for the switch to fall back TO, so "use my own relays" is not a
    // choice there — the list IS the relay config, always on. The switch is hidden and forced checked
    // rather than removed, so the one Save path below still reads it and needs no second branch.
    const relaysOn=_standalone() || !!ClientSettings.get('relaysEnabled'), blossomOn=!!ClientSettings.get('blossomEnabled');
    // Media destination as a 3-way choice (hydrated from the saved server): 'default' = automatic
    // (built-in server, or the nostr.build fallback for accounts without upload access), 'nostrbuild'
    // = always the public nostr.build, 'custom' = your own Blossom/NIP-96 URL. _isNb spots a saved
    // nostr.build so an existing pin restores as that explicit choice rather than as a custom URL.
    const _mediaSrv=ClientSettings.get('mediaServer','');
    const _isNb=/(?:^|\/\/)(?:www\.)?nostr\.build\/?$/i.test(_mediaSrv);
    const mediaMode = !blossomOn ? 'default' : (_isNb ? 'nostrbuild' : 'custom');
    // init the relay rows ONCE — renderUserSettings re-runs on connect/disconnect actions in other
    // tabs; re-seeding from saved values each time would wipe in-progress relay edits.
    // An EMPTY BOX is the least useful thing to show someone who just chose to stop depending on
    // this instance: they would have to already know which relays exist. Seeded with this node's own
    // relay (where their data is right now) plus the public set it syncs with, so the switch is one
    // click and an edit rather than research.
    if(!S._nostrPrefsLoaded){ S._setRelays=userRelays(); if(!S._setRelays.length) S._setRelays=defaultRelays(); }
    host.innerHTML=`<section class="set-card us">
      <div class="set-head"><div class="set-title">User Settings</div></div>
      <div class="us-tabs">${tabs.map((t,i)=>`<button class="us-tab${i===0?' active':''}" data-tab="${t[0]}">${t[1]}</button>`).join('')}</div>
      <div class="set-body">
        <div class="us-pane active" data-pane="profile">
          <label class="fld">Theme <span class="muted small">(applies instantly; saved to your account)</span>
            <select class="input" id="us-theme">${THEMES.map(t=>`<option value="${t[0]}"${_curTheme===t[0]?' selected':''}>${t[1]}</option>`).join('')}</select>
          </label>
          <div class="muted small" id="us-build" style="margin:-2px 0 6px">Build <code>${enc(String(window.__PC_BUILD||'unknown'))}</code>${_standalone()||BUNDLED?' \u00b7 this app':' \u00b7 this server'}</div>
          <label class="fld">Language <span class="muted small">(English is the default; Arabic switches the layout right-to-left)</span>
            <select class="input" id="us-lang">${_langOptions()}</select>
          </label>
          <label class="fld" style="flex-direction:row;justify-content:space-between;align-items:center"><svg class="ic fld-ico" aria-hidden="true"><use href="#i-compress"></use></svg>Data saver<label class="switch"><input type="checkbox" id="set-no-images" ${S.NO_IMAGES?'checked':''}><span class="slider"></span></label></label>
          <div class="muted small">Holds images &amp; videos until you tap them, skips link previews, and loads lighter feed pages — turn it on when you're low on data. Syncs across your devices.</div>
          <label class="fld"><svg class="ic fld-ico" aria-hidden="true"><use href="#i-compass"></use></svg>Screen the app opens on
            <select class="input" id="set-start-view">${(()=>{
              // Options come from the SIDEBAR, not a typed list, so a new feature joins by itself and a
              // view this deployment gates away (instance gating / nostr-only) is never offered. Rows the
              // user merely hid from the nav stay offered — hiding a door doesn't remove the room. The
              // timeline tabs are excluded (the select below owns that choice), as is Settings itself.
              const cur=ClientSettings.get('startView','social');
              const seen=new Set(['home','global','settings']);
              const rows=$$('.nav-item[data-view]').filter(b=>!b.classList.contains('hidden')&&!seen.has(b.dataset.view)&&seen.add(b.dataset.view))
                .map(b=>[b.dataset.view,_navLabel(b)]).filter(r=>r[1]);
              return `<option value="social"${rows.some(r=>r[0]===cur)?'':' selected'}>Social — the timeline (default)</option>`
                + rows.map(([v,l])=>`<option value="${v}"${cur===v?' selected':''}>${enc(l)}</option>`).join('');
            })()}</select>
          </label>
          <div class="muted small">Where the app starts. <b>Social</b> is the timeline (picked below); anything else — Notes, Messages, Calendar… — opens that screen first instead. Syncs across your devices; a device that doesn't have the chosen screen falls back to the timeline.</div>
          <label class="fld" style="flex-direction:row;justify-content:space-between;align-items:center" title="h j k l to move, gg / G for top and bottom. h and l cross between the nav rail, the feed and notifications."><svg class="ic fld-ico" aria-hidden="true"><use href="#i-terminal"></use></svg>Vim keys<label class="switch"><input type="checkbox" id="set-vim" ${ClientSettings.get('vimKeys',false)?'checked':''}><span class="slider"></span></label></label>
          <div class="muted small"><code>h j k l</code> to move, <code>gg</code> / <code>G</code> for top and bottom. <code>h</code> and <code>l</code> also cross between the nav rail, the feed and notifications. While on, <code>l</code> is movement, so React is <code>f</code> — <code>Alt</code>+<code>L</code> always works either way. Syncs across your devices.</div>
          ${BUNDLED ? `<label class="fld"><svg class="ic fld-ico" aria-hidden="true"><use href="#i-globe"></use></svg>Instance
            <div class="instance-pick" id="us-instance-pick"></div>
            <span class="input-row" style="display:flex;gap:6px;margin-top:6px"><input class="input" id="us-instance-inp" type="text" autocapitalize="none" autocorrect="off" spellcheck="false" placeholder="https://your-instance" value="${enc(_instanceBase())}"><button class="btn btn-ghost small" id="us-instance-go">Connect</button></span>
            <span class="input-row" style="display:flex;gap:6px;margin-top:6px"><button class="btn btn-ghost small${_standalone()?' active':''}" id="us-instance-none">${_standalone()?'✓ Relays only — no server':'Use relays only (no server)'}</button></span>
          </label>
          <div class="muted small">Which PosterChan server this app talks to for AI, media rendering and streams — your Nostr key and your posts never depend on it, they live on relays. Tap a quick-pick or type a domain, or paste a <code>.onion</code> address to connect over Tor. <b>Relays only</b> runs the app with no server at all: you keep Social, Messages, Notes, Passwords, Budget and the games, and the server-backed features are hidden until you name an instance again. Switching reloads the app.</div>
          ` : ''}
          ${_standalone() ? '' : `<label class="fld">News sources <span class="muted small">(one per line: url|name) — used by the <code>news</code> command</span><textarea class="input" id="us-news-src" rows="4">${enc(s.news_sources||'')}</textarea></label>`}
        </div>
        ${_notificationPane(s)}
        <div class="us-pane" data-pane="timeline">
          <div class="muted small">How the feed behaves: what lands in it, when it moves, and what a
            post does when you touch it.</div>
          <label class="fld"><svg class="ic fld-ico" aria-hidden="true"><use href="#i-home"></use></svg>Timeline the app opens on
            <select class="input" id="set-start-timeline">
              <option value="global"${ClientSettings.get('startTimeline','global')==='home'?'':' selected'}>Nostrverse (default)</option>
              <option value="home"${ClientSettings.get('startTimeline','global')==='home'?' selected':''}>Home — people you follow</option>
            </select>
          </label>
          <div class="muted small">Which feed you land on when the app starts. Nostrverse is everything the relays carry; Home is only the people you follow. You can still switch any time with the tabs above the timeline. Syncs across your devices.</div>
          <label class="fld" style="flex-direction:row;justify-content:space-between;align-items:center"><svg class="ic fld-ico" aria-hidden="true"><use href="#i-refresh"></use></svg>Auto-show new posts<label class="switch"><input type="checkbox" id="set-auto-new-posts" ${S.AUTO_NEW_POSTS?'checked':''}><span class="slider"></span></label></label>
          <label class="fld" style="flex-direction:row;justify-content:space-between;align-items:center"><svg class="ic fld-ico" aria-hidden="true"><use href="#i-chevron-up"></use></svg>Show the “new posts” button<label class="switch"><input type="checkbox" id="set-new-posts-pill" ${S.NEW_POSTS_PILL?'checked':''}><span class="slider"></span></label></label>
          <div class="muted small">On, new notes appear at the top of Home / Nostrverse as they arrive. Off, they wait behind a <b>↑ N new posts</b> button and only appear when you tap it — so the timeline never moves under you. Syncs across your devices.</div>
          <label class="fld" style="flex-direction:row;justify-content:space-between;align-items:center">Hide replies in timelines<label class="switch"><input type="checkbox" id="set-hide-replies" ${ClientSettings.get('hideReplies',false)?'checked':''}><span class="slider"></span></label></label>
          <div class="muted small">Keeps replies out of Home and Nostrverse so the feed reads as posts rather than conversation. A reply is still there when you open the thread it belongs to, and your own notifications are unaffected. Syncs across your devices.</div>
          <label class="fld" style="flex-direction:row;justify-content:space-between;align-items:center"><svg class="ic fld-ico" aria-hidden="true"><use href="#i-wand"></use></svg>Post effects<label class="switch"><input type="checkbox" id="set-post-effects" ${_postEffectsOn()?'checked':''}><span class="slider"></span></label></label>
          <div class="muted small">Celebratory animations on notes: confetti on congrats, a sunrise on <code>gm</code>, and drifting tears on 😭 reactions. Off by default. Syncs across your devices.</div>
          <label class="fld" style="flex-direction:row;justify-content:space-between;align-items:center">Hold a post to read it aloud<label class="switch"><input type="checkbox" id="set-read-aloud" ${ClientSettings.get('readAloudHold',true)?'checked':''}><span class="slider"></span></label></label>
          <div class="muted small">Press and hold any post and it is narrated. Turn it off if you hold posts by accident. While one is playing a <b>Stop reading</b> button sits at the bottom of the screen, and scrolling the post out of view stops it too.</div>
        </div>
        <div class="us-pane" data-pane="sidebar">${_navHideHtml()}</div>
        <div class="us-pane" data-pane="mail">
          <div class="muted small">IMAP/SMTP accounts for the <code>mail</code> command. First account is the default sender.</div>
          <div id="us-mail-list"></div>
          <button class="btn btn-ghost small" id="us-mail-add"><svg class="ic b-ic" aria-hidden="true"><use href="#i-plus"></use></svg>Add email account</button>
        </div>
        <div class="us-pane" data-pane="telegram">
          <div class="${s.telegram_chat_id?'us-ok':'muted small'}" id="us-tg-status">${s.telegram_chat_id?('<svg class="ic st-ico ok" aria-hidden="true"><use href="#i-check"></use></svg> Linked (chat '+enc(String(s.telegram_chat_id))+')'):'<svg class="ic st-ico warn" aria-hidden="true"><use href="#i-warn"></use></svg> Not linked — generate a key below and send it to your bot.'}</div>
          <div class="set-actions">
            <button class="btn btn-ghost small" id="us-tg-key">Generate link key</button>
            ${s.telegram_chat_id?'<button class="btn btn-ghost small" id="us-tg-unlink" style="color:var(--danger)">Unlink Telegram</button>':''}
          </div>
          <div id="us-tg-keybox" class="muted small"></div>
          <div class="muted small">What Telegram notifies you about now lives under
            <b>Notifications</b>, with everything else that decides when you are interrupted.</div>
        </div>
        <div class="us-pane" data-pane="social">
          ${typeof s.fedi_only==='boolean' ? `
          <label class="fld" style="flex-direction:row;justify-content:space-between;align-items:center">Fediverse-only mode<label class="switch"><input type="checkbox" id="us-fedi-only" ${s.fedi_only?'checked':''}><span class="slider"></span></label></label>
          <div class="muted small">Show only bridge posts in timelines. Send posts, replies, likes and reposts through your linked Fediverse account without publishing them to Nostr. Private app data and messages keep working. Requires a connected Fediverse account to post.</div>
          ` : ''}

          <div id="us-fedi-hide-options" ${s.fedi_only?'hidden':''}>
          <label class="fld" style="flex-direction:row;justify-content:space-between;align-items:center">Hide fediverse posts in timelines<label class="switch"><input type="checkbox" id="set-hide-fedi" ${ClientSettings.get('hideFediBridge',true)?'checked':''}><span class="slider"></span></label></label>
          <div class="muted small">On by default. The bridge mirrors whole fediverse timelines onto Nostr under stand-in keys — this keeps them out of Home and Nostrverse. Mentions, replies and DMs from fediverse people still reach you either way.</div>
          </div>

          <div class="us-conn"><div class="set-title small">Pleroma / Mastodon</div>
            <label class="fld">Instance URL<input class="input" id="us-plr-url" value="${enc(s.pleroma_instance_url||'')}" placeholder="https://pleroma.example"></label>
            ${s.pleroma_has_access_token
              ? `<div class="muted small">✓ Connected to ${enc(s.pleroma_instance_url||'')}</div><button class="btn btn-ghost small" id="us-plr-disc" style="color:var(--danger)">Disconnect</button>`
              : `<button class="btn btn-ghost small" id="us-plr-conn">Connect with OAuth</button>`}
            ${s.pleroma_has_access_token ? `<div class="muted small">Following a bridged fediverse account on Nostr also follows the real account here. Reconnect once if follows don't take (grants the follow permission).</div>` : ''}
            <label class="fld" style="flex-direction:row;justify-content:space-between;align-items:center">Bridge my fedi DMs &amp; notifications to Nostr<label class="switch"><input type="checkbox" id="us-fedi-bridge" ${s.fedi_bridge_enabled?'checked':''}><span class="slider"></span></label></label>
            <div class="muted small">Your fediverse DMs arrive as Nostr DMs and your notifications as Nostr events; replying/liking/reposting a bridged post posts back through this account. Needs a NIP-05 name on this instance.</div>
            <label class="fld" style="flex-direction:row;justify-content:space-between;align-items:center">Cross-post my posts to the Fediverse<label class="switch"><input type="checkbox" id="us-fedi-crosspost" ${s.fedi_crosspost_enabled?'checked':''}><span class="slider"></span></label></label>
            <div class="muted small">When on, your top-level Nostr notes are also posted to your linked Pleroma account as public posts. Replies stay where you make them.</div>
            <div class="us-stat muted small" id="us-plr-stat"></div>
          </div>
        </div>
        <div class="us-pane" data-pane="keys">
          <div class="muted small">API keys let external apps use the AI API as you.</div>
          <div class="set-actions"><input class="input" id="us-key-name" placeholder="Key name (optional)"><button class="btn btn-ghost small" id="us-key-new">Generate new key</button></div>
          <div id="us-key-list"></div>
        </div>
        <!-- Tor has its own pane. It was two blocks bolted to the end of Profile, which by then also
             carried the instance picker, the relays-only switch, the media cache and the email/news
             fields — and Tor is not a profile setting, it is how the whole app reaches the network.
             The wiring below is keyed on these ids and did not move. -->
        <!-- The phone shell (launcher / messages / dialer). Rendered by phoneshell.js, which is
             where the roles and their refusals are understood; this is only the pane it lives in. -->
        ${window.Capacitor ? '<div class="us-pane" data-pane="phone"><div id="phone-shell"></div></div>' : ''}
        <div class="us-pane" data-pane="tor">
          ${_hasNativeTor() ? `<div class="fld" id="us-ntor-row"><svg class="ic fld-ico" aria-hidden="true"><use href="#i-shield"></use></svg>Tor
            <label class="fld" style="flex-direction:row;justify-content:space-between;align-items:center;margin:6px 0 0">Route everything through Tor<label class="switch"><input type="checkbox" id="us-ntor-on"><span class="slider"></span></label></label>
            <div class="muted small" id="us-ntor-state" style="margin-top:4px">Checking…</div>
            <label class="fld" style="margin-top:8px">Exit country
              <select class="input" id="us-ntor-cc"></select>
            </label>
            <div class="muted small">Where your traffic leaves the Tor network — sites see an address in this country. <b>Any</b> is fastest and the most private; pinning one country narrows the pool of exits, so it is slower and more identifying. Not a guarantee: if no exit is available there, Tor will not connect rather than quietly use another.</div>
            <span class="input-row" style="display:flex;gap:6px;margin-top:8px">
              <button class="btn btn-ghost small" id="us-ntor-new">New circuit</button>
            </span>
          </div>` : ''}
          ${window.Capacitor ? `<div class="fld" id="us-tor-row"><svg class="ic fld-ico" aria-hidden="true"><use href="#i-shield"></use></svg>Tor
            <div class="muted small" id="us-tor-state" style="margin-top:4px">Checking for Orbot…</div>
            <span class="input-row" style="display:flex;gap:6px;margin-top:6px">
              <button class="btn btn-ghost small" id="us-tor-start">Start Orbot</button>
              <button class="btn btn-ghost small" id="us-tor-open">Open Orbot</button>
            </span>
          </div>` : ''}
        </div>
        <div class="us-pane" data-pane="relays">
          <label class="fld${_standalone()?' hidden':''}" style="flex-direction:row;justify-content:space-between;align-items:center">Use my own relays<label class="switch"><input type="checkbox" id="set-relays-on" ${relaysOn?'checked':''}><span class="slider"></span></label></label>
          <div class="muted small">${_standalone()
            ? 'These are the relays this app reads and publishes to. They are pre-filled with the ones PosterChan uses, so it works as it is — replace them with your own at any time. Events are signature-verified either way.'
            : 'By default this app uses the built-in relay. Turn this on to connect to your own relays instead — pre-filled with the relays PosterChan uses, so you are not starting from a blank box. Events from them are signature-verified.'}</div>
          <div class="set-body ${relaysOn?'':'disabled'}" id="set-relays-body">
            <div id="set-relay-list"></div>
            <div class="set-actions">
              <button class="btn btn-ghost small" id="set-relay-add"><svg class="ic b-ic" aria-hidden="true"><use href="#i-plus"></use></svg>Add relay</button>
              <button class="btn btn-ghost small" id="set-relay-ext"><svg class="ic b-ic" aria-hidden="true"><use href="#i-download"></use></svg>Import from extension</button>
              <button class="btn btn-ghost small" id="set-relay-carry" title="Republish your private libraries to the relays above"><svg class="ic b-ic" aria-hidden="true"><use href="#i-cloud"></use></svg>Copy my private data here</button>
            </div>
            <div class="muted small">Notes, Passwords and Budget live only on a relay, so they are
              copied to your new relays whenever you change this list. This button runs it again if
              one was unreachable at the time. (Your files index is signed by the server and stays
              where it is.)</div>
            <div class="set-actions">
              <input class="input" id="set-nip05" placeholder="you@domain.com" value="${enc(S.ME&&niceImport()||'')}">
              <button class="btn btn-ghost small" id="set-relay-nip05"><svg class="ic b-ic" aria-hidden="true"><use href="#i-download"></use></svg>Import from NIP-05</button>
            </div>
            <div class="muted small">Default built-in relay: <code>${enc(S.CFG.relay_url||'none')}</code></div>
          </div>
          <div class="set-actions"><button class="btn btn-neon small" id="set-relays-save">Save &amp; reload</button></div>
        </div>
        <div class="us-pane" data-pane="media">
          <label class="fld"><svg class="ic fld-ico" aria-hidden="true"><use href="#i-cloud"></use></svg>Where your uploads are stored</label>
          <div class="muted small">Photos, videos and files you attach to a post get uploaded to a media server, and its link goes into your note. Choose where:</div>
          <div class="media-modes">
            <label class="media-mode"><input type="radio" name="media-mode" value="default" ${mediaMode==='default'?'checked':''}>
              <span><b>Default</b> <span class="muted small">· recommended</span><span class="muted small" style="display:block;font-weight:400">Uses this instance's built-in server. New accounts without upload access automatically use nostr.build.</span></span></label>
            <label class="media-mode"><input type="radio" name="media-mode" value="nostrbuild" ${mediaMode==='nostrbuild'?'checked':''}>
              <span><b>nostr.build</b><span class="muted small" style="display:block;font-weight:400">Always use the public nostr.build server.</span></span></label>
            <label class="media-mode"><input type="radio" name="media-mode" value="custom" ${mediaMode==='custom'?'checked':''}>
              <span><b>My own server</b><span class="muted small" style="display:block;font-weight:400">A Blossom or NIP-96 server you control.</span></span></label>
          </div>
          <div class="set-body ${mediaMode==='custom'?'':'disabled'}" id="set-blossom-body">
            <label class="fld">Your server URL<input class="input" id="set-media" placeholder="https://your-blossom-server.com" value="${enc(mediaMode==='custom'?_mediaSrv:'')}"></label>
            <div class="media-presets"><span class="muted small">Quick pick:</span>
              <button type="button" class="btn btn-ghost small mp-preset" data-url="https://blossom.primal.net">Primal</button>
              <button type="button" class="btn btn-ghost small mp-preset" data-url="https://blossom.band">blossom.band</button>
            </div>
            <div class="muted small">Must be an <code>https://</code> Blossom or NIP-96 server that allows cross-origin (CORS) uploads.</div>
          </div>
          <div class="muted small" id="set-media-current" style="margin-top:12px"></div>
          <div class="set-actions"><button class="btn btn-neon small" id="set-media-save">Save &amp; reload</button></div>
        </div>
        <div class="us-pane" data-pane="zaps">
          <div class="muted small">Got the <b>Alby</b> (or any WebLN) browser extension? Zaps already use it — just tap ⚡. Otherwise connect a wallet with a <b>Nostr Wallet Connect</b> string (NIP-47) — handy on mobile or with Alby Hub / Coinos / Primal. Stored only in this browser.</div>
          <div class="set-actions"><button class="btn btn-cyan small" id="set-webln"><svg class="ic b-ic" aria-hidden="true"><use href="#i-zap"></use></svg>Connect Alby / WebLN extension</button></div>
          <input class="input" id="set-nwc" type="password" placeholder="nostr+walletconnect://… (for wallets without an extension)" value="${enc(ClientSettings.get('nwc',''))}">
          <div class="set-actions"><button class="btn btn-neon small" id="set-nwc-save">Save wallet</button>
            <button class="btn btn-cyan small" id="set-nwc-clear">Disconnect</button></div>
          <div class="muted small" id="set-nwc-status">${Nwc.configured()?'✓ NWC wallet connected — zaps pay instantly':''}</div>
          <label class="fld"><svg class="ic fld-ico" aria-hidden="true"><use href="#i-zap"></use></svg>Zap amounts <span class="muted small">(sats — your one-tap presets)</span><input class="input" id="us-zap-presets" value="${enc(ClientSettings.get('zapPresets','')||_ZAP_DEFAULTS.join(', '))}" placeholder="21, 100, 500, 1000, 5000"></label>
          <label class="fld">ɱ Monero tip amounts <span class="muted small">(XMR — your one-tap presets)</span><input class="input" id="us-xmr-presets" value="${enc(ClientSettings.get('xmrPresets','')||_XMR_DEFAULTS.join(', '))}" placeholder="0.001, 0.01, 0.1, 1"></label>
          <label class="fld"><svg class="ic fld-ico" aria-hidden="true"><use href="#i-coin"></use></svg>Bitcoin Cash tip amounts <span class="muted small">(BCH — your one-tap presets)</span><input class="input" id="us-bch-presets" value="${enc(ClientSettings.get('bchPresets','')||_BCH_DEFAULTS.join(', '))}" placeholder="0.001, 0.01, 0.05, 0.1"></label>
          <div class="muted small">Your one-tap amounts in the ⚡ zap / ɱ Monero tip dialogs. Comma-separated; synced to your other devices.</div>
        </div>
        <div class="us-pane" data-pane="cache">
          <div class="muted small">What this device keeps on disk so it works without the network.
            Both settings follow your account; what a device can actually hold is capped by its own
            free space, whatever is chosen here.</div>
          <label class="fld"><svg class="ic fld-ico" aria-hidden="true"><use href="#i-image"></use></svg>Media cache size
            <select class="input" id="set-media-cache">${[1,2,4,8,16,32].map(g=>`<option value="${g}"${(+ClientSettings.get('mediaCacheGB',4)===g)?' selected':''}>${g} GB${g===4?' (default)':''}</option>`).join('')}</select>
          </label>
          <div class="muted small">How much offline media (avatars, images, played videos) to keep cached on THIS device. Larger = fewer re-downloads on a slow/throttled link, but more storage used. The setting follows your account; what a device can actually hold is capped by its own free space.</div>
          <div class="muted small" id="media-cache-stat" style="margin-top:4px">Checking device storage…</div>
          <label class="fld"><svg class="ic fld-ico" aria-hidden="true"><use href="#i-music"></use></svg>Offline music limit
            <select class="input" id="set-music-offline">${(()=>{const cur=+ClientSettings.get('musicOfflineGB',0)||0;const opts=[0,5,10,20,50,100,250,500];if(!opts.includes(cur))opts.push(cur);return opts.sort((a,b)=>a-b).map(g=>`<option value="${g}"${cur===g?' selected':''}>${g?g+' GB':'No limit (default)'}</option>`).join('');})()}</select>
          </label>
          <div class="muted small">How much of your music library to keep playable offline on THIS device. <b>No limit</b> keeps everything you download — a library is not a cache, and nothing is ever evicted. Set a size and the tracks stored longest ago make room once it is reached. The setting follows your account, so a new device starts where you left off; what each device can actually hold is still capped by its own free space.</div>
          <div class="muted small" id="music-offline-stat" style="margin-top:4px">Checking…</div>
        </div>
        <div class="us-pane" data-pane="privacy">
          <div class="muted small">What this device gives away, and to whom. Everything here is off by
            default — turning any of it on costs you nothing but a little convenience.</div>
          <label class="fld" style="flex-direction:row;justify-content:space-between;align-items:center"><svg class="ic fld-ico" aria-hidden="true"><use href="#i-broom"></use></svg>Remove link trackers when I post<label class="switch"><input type="checkbox" id="set-clean-links" ${ClientSettings.get('cleanLinks',false)?'checked':''}><span class="slider"></span></label></label>
          <div class="muted small">Strips tracking parameters (<code>utm_*</code>, <code>fbclid</code>, <code>gclid</code>, YouTube's <code>si</code>…) out of every link in a post or reply, and unwraps click-wrappers like <code>google.com/url?q=</code> and Outlook safelinks, so what you share can't be tied back to you. Runs on this device with no network call — it's a published list of tracker names, not a guess. Off by default; you can always run it by hand from <b>🤖 AI → 🧹 Clean links</b> in the composer. Syncs across your devices.</div>
          <label class="fld" style="flex-direction:row;justify-content:space-between;align-items:center">Hide DM previews until opened<label class="switch"><input type="checkbox" id="set-hide-dm-prev" ${ClientSettings.get('hideDmPreview', false)?'checked':''}><span class="slider"></span></label></label>
          <div class="muted small">Don’t show the last message text in the Messages list — only reveal it when you open the conversation. Saved on this device.</div>
        </div>
        <div class="us-pane" data-pane="muted">
          <label class="fld" style="flex-direction:row;justify-content:space-between;align-items:center">Auto-mute people who mute you<label class="switch"><input type="checkbox" id="set-auto-mute" ${_autoMuteStored().enabled?'checked':''}><span class="slider"></span></label></label>
          <div class="muted small">Filters accounts whose public mute list includes you. Checks every 15 minutes while this app is open. Update now also removes automatic mutes when a newer public list no longer includes you. Private mutes cannot be detected. Your manual mute list stays separate. Saved for this account on this device.</div>
          <div class="set-actions"><button class="btn btn-neon small" id="set-auto-mute-update">Update now</button></div>
          <div class="muted small" id="set-auto-mute-status" role="status" aria-live="polite"></div>

          <label class="fld" style="flex-direction:row;justify-content:space-between;align-items:center">Blur sensitive / NSFW posts<label class="switch"><input type="checkbox" id="set-blur-nsfw" ${S.BLUR_NSFW?'checked':''}><span class="slider"></span></label></label>
          <div class="muted small">Posts flagged sensitive (NIP-36 content warning) are blurred behind a “Show” reveal. Turn this off to see them unblurred. Saved on this device.</div>
          <div class="muted small">Hide posts containing any of these words or phrases (case-insensitive, one per line). Saved to your Nostr mute list (NIP-51), so it follows you to other clients.</div>
          <textarea class="input" id="set-muted-words" rows="4" placeholder="one word or phrase per line">${enc([...S.MUTED_WORDS].join('\n'))}</textarea>
          <div class="set-actions"><button class="btn btn-neon small" id="set-words-save">Save muted words</button></div>
          <div class="muted small" id="set-words-status"></div>
        </div>
      </div>
      <button class="btn btn-neon" id="us-save">Save settings</button>
      <div class="muted small set-foot" id="us-save-status"></div>
    </section>`;
    // tab switching
    /* The Phone pane. Rendered whenever User Settings is drawn rather than on the tab click: the
       card reads three system roles, and a tab that shows a spinner on first open reads as broken on
       the one screen whose whole job is to say what state the phone is in. */
    { const ps=$('#phone-shell',host); if(ps) _withPhoneShell(m => m.renderSettings(ps)); }

    $$('.us-tab',host).forEach(b=> b.onclick=()=>{
      $$('.us-tab',host).forEach(x=>x.classList.toggle('active',x===b));
      $$('.us-pane',host).forEach(p=>p.classList.toggle('active', p.dataset.pane===b.dataset.tab));
      if(b.dataset.tab==='keys') usLoadKeys();
    });
    usRenderMail();

    // ---- moved into tabs: Relays / Media / Zaps / Muted (client-side, own save semantics) ----
    drawRelayRows();
    const syncRelays=()=>{ S._setRelays=$$('#set-relay-list .relay-row input').map(i=>i.value.trim()); };
    { const t=$('#set-relays-on'); if(t) t.onchange=e=>$('#set-relays-body').classList.toggle('disabled', !e.target.checked); }
    { const c=$('#set-relay-carry'); if(c) c.onclick=async()=>{
        c.disabled=true;
        try{
          const r = await carryPrivateToRelays({});
          // The guard returns without a toast, so say something — a button that silently does
          // nothing while an automatic run is in flight reads as broken.
          if(r.busy) toast('already copying — give it a moment');
          else if(r.noUser) toast('sign in first');
          else if(r.offline) toast('not connected to any relay yet');
          else if(!r.total) toast('nothing private to copy yet');
        } finally { c.disabled=false; }
      }; }
    // The chosen media destination ('default' | 'nostrbuild' | 'custom'), read from the radio group.
    const _mediaMode=()=> (($('input[name=media-mode]:checked')||{}).value) || 'default';
    // Map the chosen mode → the {enabled, url} we persist. 'default' = built-in/auto (no override);
    // 'nostrbuild' = pin the public server; 'custom' = the typed URL. Central so both Save paths agree.
    const _mediaChoice=()=>{ const m=_mediaMode();
      if(m==='custom')     return { enabled:true,  url:(($('#set-media')||{}).value||'').trim() };
      if(m==='nostrbuild') return { enabled:true,  url:'https://nostr.build' };
      return { enabled:false, url:'' }; };
    // Live "uploads go to X" line — truthful about the actual destination, including the nostr.build
    // fallback that Default mode gives accounts without built-in upload access (matches uploadTarget()).
    const _updMediaCurrent=()=>{ const el=$('#set-media-current'); if(!el) return;
      const m=_mediaMode();
      if(m==='custom'){ const url=(($('#set-media')||{}).value||'').trim();
        el.innerHTML = url ? `<svg class="ic st-ico ok" aria-hidden="true"><use href="#i-check"></use></svg> Uploads go to <b>your server</b>: <code>${enc(url)}</code>` : `<svg class="ic st-ico warn" aria-hidden="true"><use href="#i-warn"></use></svg> Enter your server URL above.`; return; }
      if(m==='nostrbuild'){ el.innerHTML=`<svg class="ic st-ico ok" aria-hidden="true"><use href="#i-check"></use></svg> Uploads go to the public <b>nostr.build</b> server.`; return; }
      // Default: built-in if this account has access, else the automatic nostr.build fallback.
      el.innerHTML = (S._blossomOK===false)
        ? `<svg class="ic st-ico ok" aria-hidden="true"><use href="#i-check"></use></svg> Your account has no upload access to this instance's built-in server, so uploads use the public <b>nostr.build</b> automatically.`
        : `<svg class="ic st-ico ok" aria-hidden="true"><use href="#i-check"></use></svg> Uploads go to this instance's <b>built-in server</b>${S.CFG.blossom_url?` (<code>${enc(S.CFG.blossom_url)}</code>)`:''}.`; };
    const _syncMediaUI=()=>{ const custom=_mediaMode()==='custom';
      const body=$('#set-blossom-body'); if(body) body.classList.toggle('disabled', !custom);
      _updMediaCurrent(); };
    $$('input[name=media-mode]').forEach(r=> r.onchange=_syncMediaUI);
    { const mi=$('#set-media'); if(mi) mi.addEventListener('input', _updMediaCurrent); }
    _syncMediaUI();
    // Quick-pick presets fill the custom URL field (and select the 'My own server' radio so Save applies it).
    $$('.mp-preset').forEach(b=> b.onclick=()=>{ const mi=$('#set-media'); if(mi) mi.value=b.dataset.url;
      const r=$('input[name=media-mode][value=custom]'); if(r) r.checked=true; _syncMediaUI(); });
    { const b=$('#set-relay-add'); if(b) b.onclick=()=>{ syncRelays(); S._setRelays.push(''); drawRelayRows(); }; }
    { const b=$('#set-relay-ext'); if(b) b.onclick=async()=>{ syncRelays(); await importExtensionRelays(); }; }
    { const b=$('#set-relay-nip05'); if(b) b.onclick=async()=>{ syncRelays(); await importNip05Relays($('#set-nip05').value.trim()); }; }
    { const b=$('#set-relays-save'); if(b) b.onclick=async()=>{
        syncRelays();
        const urls=[...new Set(S._setRelays.map(u=>normalizeRelay(u)).filter(Boolean))];
        const on=$('#set-relays-on').checked;
        ClientSettings.set('relaysEnabled', on);
        /* SAME RULE AS THE GLOBAL SAVE, and this button had the same hole. The note below already
         * refuses to PUBLISH a list left in a disabled editor; saving it locally is the same
         * mistake one step earlier — it overwrites the user's real list with seeded suggestions,
         * so switching off and on hands back relays they never chose. */
        if(on) ClientSettings.set('relays', urls);
        // This dedicated button is an explicit relay-list action, but turning the local override OFF
        // is not permission to re-announce the URLs left in the disabled editor. In particular, an
        // old device must not make its cached list newest again after another client changed NIP-65.
        try{ if(on && urls.length) await publish(10002,'',urls.map(u=>['r',u])); }catch(_){}
        toast('relays saved — reloading'); setTimeout(()=>location.reload(),600);
      }; }
    { const b=$('#set-media-save'); if(b) b.onclick=async()=>{
        const { enabled, url } = _mediaChoice();
        if(enabled && !url){ toast('enter your server URL'); return; }   // 'My own server' selected but blank
        if(enabled){
          // nostr.build is ALWAYS NIP-96; any other host is capability-probed (NIP-96 well-known) at save time.
          const proto=/(?:^|\/\/)(?:www\.)?nostr\.build\/?$/i.test(url) ? 'nip96' : await detectProto(url);
          ClientSettings.set('blossomEnabled', true);
          ClientSettings.set('mediaServer', url);
          ClientSettings.set('mediaProto', proto);
          // Persist to Nostr so a fresh device restores it: kind-10063 (BUD-03 Blossom) or kind-10096
          // (NIP-96 file-storage server list). Newest of the two wins on restore.
          try{ await publish(proto==='nip96'?10096:10063,'',[['server',url]]); }catch(_){}
        } else {
          // Default (automatic): clear locally AND publish EMPTY replaceable lists to both kinds, so no
          // other device re-restores a now-abandoned server (the stale-restore bug).
          ClientSettings.set('blossomEnabled', false);
          ClientSettings.set('mediaServer', '');
          ClientSettings.set('mediaProto', '');
          try{ await publish(10063,'',[]); }catch(_){}
          try{ await publish(10096,'',[]); }catch(_){}
        }
        toast('media server saved — reloading'); setTimeout(()=>location.reload(),600);
      }; }
    { const we=$('#set-webln'); if(we) we.onclick=async()=>{ const st=$('#set-nwc-status');
        if(!window.webln){ if(st) st.textContent='No WebLN extension found — install Alby, or paste an NWC string below.'; return; }
        try{ await window.webln.enable(); if(st) st.textContent='✓ Extension connected — tap ⚡ on any post to zap'; toast('⚡ wallet extension connected'); }
        catch(e){ if(st) st.textContent='Extension declined: '+((e&&e.message)||e); } }; }
    { const nb=$('#set-nwc-save'); if(nb) nb.onclick=()=>{ const st=$('#set-nwc-status'); const u=($('#set-nwc').value||'').trim();
        if(u && !Nwc.parse(u)){ if(st) st.textContent='Not a valid nostr+walletconnect:// string'; return; }
        ClientSettings.set('nwc', u); if(st) st.textContent=u?'✓ Wallet connected — zaps pay instantly':'cleared'; toast(u?'wallet saved':'wallet cleared'); }; }
    { const nc=$('#set-nwc-clear'); if(nc) nc.onclick=()=>{ ClientSettings.set('nwc',''); const i=$('#set-nwc'); if(i) i.value=''; const st=$('#set-nwc-status'); if(st) st.textContent='Disconnected'; toast('wallet disconnected'); }; }
    _wireNotificationSettings(host);
    // Blur-NSFW toggle: persist immediately (per-device) and re-render the open feed so it applies live.
    { const bn=$('#set-blur-nsfw'); if(bn) bn.onchange=()=>{
        S.BLUR_NSFW = bn.checked; ClientSettings.set('blurNsfw', S.BLUR_NSFW);
        toast(S.BLUR_NSFW?'sensitive posts blurred':'sensitive posts shown');
        if(['home','global','notifications','messages','bookmarks'].includes(S.VIEW)){ try{ renderView(true); }catch(_){} }
      }; }
    // Data saver (tap-to-load images): per-device now (instant) + synced to Nostr so it follows devices.
    { const vk=$('#set-vim'); if(vk) vk.onchange=()=>{
        ClientSettings.set('vimKeys', vk.checked); _prefTouched.add('vimKeys');
        saveClientPrefsNostr({ vimKeys: vk.checked });
        S._vimPane='feed';
        toast(vk.checked ? 'vim keys on — hjkl to move, gg / G for top and bottom' : 'vim keys off');
      }; }
    { const ni=$('#set-no-images'); if(ni) ni.onchange=()=>{
        S.NO_IMAGES = ni.checked; ClientSettings.set('noImages', S.NO_IMAGES); _prefTouched.add('noImages'); saveClientPrefsNostr({ noImages: S.NO_IMAGES });
        toast(S.NO_IMAGES?'data saver on — tap to load images':'images load automatically');
        if(['home','global','notifications','messages','bookmarks','profile'].includes(S.VIEW)){ try{ renderView(true); }catch(_){} }
      }; }
    // Auto-show new posts: persist + sync to Nostr. Turning it ON adopts whatever is already buffered
    // (the pill would otherwise sit there until the next live note); turning it OFF leaves the feed as-is.
    // Landing timeline: per-device (the boot view is chosen before the relay answers) + synced to Nostr.
    // Nothing to re-render — it only decides where the NEXT start lands.
    { const st=$('#set-start-timeline'); if(st) st.onchange=()=>{
        const v = st.value==='home' ? 'home' : 'global';
        ClientSettings.set('startTimeline', v); _prefTouched.add('startTimeline'); saveClientPrefsNostr({ startTimeline: v });
        toast(v==='home'?'opening on Home from now on':'opening on Nostrverse from now on');
      }; }
    // Landing SCREEN: same shape as the timeline above — per-device + synced, nothing to re-render
    // (it only decides where the NEXT start lands). The value is validated at BOOT (_startView), so a
    // slug from an older/newer build costs a fallback to the timeline, never a blank screen.
    { const sv=$('#set-start-view'); if(sv) sv.onchange=()=>{
        const v = /^[a-z0-9_-]{1,32}$/.test(sv.value) ? sv.value : 'social';
        ClientSettings.set('startView', v); _prefTouched.add('startView'); saveClientPrefsNostr({ startView: v });
        const lbl = (sv.options[sv.selectedIndex]||{}).text || v;
        toast(v==='social' ? 'opening on the timeline from now on' : 'opening on '+lbl+' from now on');
      }; }
    _wireNavHide();   // Settings → Profile → 🧭 Sidebar
    { const hr=$('#set-hide-replies'); if(hr) hr.onchange=()=>{
        ClientSettings.set('hideReplies', hr.checked);
        _prefTouched.add('hideReplies'); saveClientPrefsNostr({ hideReplies: hr.checked });
        toast(hr.checked?'replies hidden in timelines':'replies shown in timelines');
        // `fn` is captured when a timeline renders, so the change only takes effect on a re-render.
        if(S.VIEW==='home'||S.VIEW==='global') renderView(true);
      }; }
    { const hf=$('#set-hide-fedi'); if(hf) hf.onchange=()=>{
        ClientSettings.set('hideFediBridge', hf.checked);
        _prefTouched.add('hideFediBridge'); saveClientPrefsNostr({ hideFediBridge: hf.checked });
        toast(hf.checked?'fediverse posts hidden in timelines':'fediverse posts shown in timelines');
        if(S.VIEW==='home'||S.VIEW==='global') renderView(true);   // same captured-`fn` reason as above
      }; }
    { const ra=$('#set-read-aloud'); if(ra) ra.onchange=()=>{
        const on = ra.checked;
        ClientSettings.set('readAloudHold', on); _prefTouched.add('readAloudHold');
        saveClientPrefsNostr({ readAloudHold: on });
        if(!on) stopNarration();          // turning it off while one is playing must stop that one
        toast(on?'hold a post to hear it read aloud':'holding a post no longer reads it aloud');
      }; }
    { const np=$('#set-new-posts-pill'); if(np) np.onchange=()=>{
        S.NEW_POSTS_PILL = np.checked; ClientSettings.set('newPostsPill', S.NEW_POSTS_PILL);
        _prefTouched.add('newPostsPill'); saveClientPrefsNostr({ newPostsPill: S.NEW_POSTS_PILL });
        toast(S.NEW_POSTS_PILL?'the new posts button is back':'the new posts button is off');
        _updateNewPostsPill();      // take it off the screen now, not on the next arrival
      }; }
    { const an=$('#set-auto-new-posts'); if(an) an.onchange=()=>{
        S.AUTO_NEW_POSTS = an.checked; ClientSettings.set('autoNewPosts', S.AUTO_NEW_POSTS);
        _prefTouched.add('autoNewPosts'); saveClientPrefsNostr({ autoNewPosts: S.AUTO_NEW_POSTS });
        toast(S.AUTO_NEW_POSTS?'new posts appear automatically':'new posts wait behind the ↑ button');
        if(S.AUTO_NEW_POSTS){ const feed=$('#feed');
          if(feed && (S.VIEW==='home'||S.VIEW==='global') && S._livePending.length && feed.scrollTop <= _LIVE_READ_PX) _flushPending(); }
      }; }
    // 🧹 Remove link trackers: per-device (it takes effect on the next post, so there is nothing to
    // re-render) and synced to Nostr so it follows you to your phone.
    { const cl=$('#set-clean-links'); if(cl) cl.onchange=()=>{
        const on = cl.checked; ClientSettings.set('cleanLinks', on); _prefTouched.add('cleanLinks'); saveClientPrefsNostr({ cleanLinks: on });
        toast(on?'🧹 link trackers removed from your posts':'links posted exactly as you write them');
      }; }
    // Post effects (celebratory note animations): persist + sync to Nostr, then re-render so the change
    // shows immediately (turning OFF drops data-celebrate from notes; the sweep stops on its own).
    { const pe=$('#set-post-effects'); if(pe) pe.onchange=()=>{
        const on = pe.checked; ClientSettings.set('postEffects', on); _prefTouched.add('postEffects'); saveClientPrefsNostr({ postEffects: on });
        toast(on?'post effects on':'post effects off');
        if(!on) _stopCelebrations();                       // instant teardown — don't wait for the next sweep tick
        if(['home','global','notifications','messages','bookmarks','profile'].includes(S.VIEW)){ try{ renderView(true); }catch(_){} }
      }; }
    // Media cache size (per-device): persist + push the new byte budget to the service worker.
    { const mc=$('#set-media-cache'); if(mc) mc.onchange=()=>{
        const gb=+mc.value||4; ClientSettings.set('mediaCacheGB', gb); _applyMediaCacheBudget(gb);
        _prefTouched.add('mediaCacheGB'); saveClientPrefsNostr({ mediaCacheGB: gb });
        toast('media cache set to '+gb+' GB'); _fillMediaCacheStat(); }; }
    _fillMediaCacheStat();
    /* Offline music limit. Lowering it TRIMS IMMEDIATELY rather than at the next
     * download: someone reaching for this setting is usually reaching for space, and a limit that
     * takes effect the next time you happen to save a track has not given them any. */
    { const mo=$('#set-music-offline'); if(mo) mo.onchange=async ()=>{
        const gb=Math.max(0, +mo.value||0); ClientSettings.set('musicOfflineGB', gb);
        _prefTouched.add('musicOfflineGB'); saveClientPrefsNostr({ musicOfflineGB: gb });
        let freed=0; try{ freed=await MusicOffline.trim(); }catch(_){}
        toast(gb ? ('offline music limit set to '+gb+' GB' + (freed ? ' · freed '+_fmtBytes(freed) : ''))
                 : 'offline music: no limit');
        _fillMusicOfflineStat(); }; }
    _fillMusicOfflineStat();
    // Instance quick-pick (native app only). Chips for the default + recently-used instances; tapping one (or
    // Connect) switches the server the app talks to. The Nostr key is portable, so only the session re-establishes.
    { const pick=$('#us-instance-pick'), inp=$('#us-instance-inp'), go=$('#us-instance-go'), non=$('#us-instance-none');
      // Gated on BUNDLED, not on the base being non-empty: in standalone the base IS empty, and testing
      // it removed the picker precisely when it was the only way back to an instance.
      if(pick && inp && go && BUNDLED){
        const cur=_instanceBase();
        // ONE normaliser, shared with the sign-in chooser (_normInstance): the two surfaces set the same
        // setting, so a rule that held on one and not the other — which .onion scheme, which hosts count
        // as plausible — would be a bug the moment they disagreed.
        const norm=_normInstance;
        let recent=[]; try{ recent=JSON.parse(localStorage.getItem('pc_instances')||'[]'); }catch(_){}
        const opts=[...new Set([cur, 'https://poster.place', ...recent].map(norm).filter(Boolean))];
        pick.innerHTML=opts.map(u=>`<button class="instance-chip${u===cur?' on':''}" data-u="${enc(u)}">${enc(u.replace(/^https?:\/\//,''))}${u===cur?' ✓':''}</button>`).join('');
        const _switch=raw=>{ const u=norm(raw); if(!u){ toast('that doesn’t look like a valid instance domain'); return; }
          if(u===cur){ toast('already connected to '+u.replace(/^https?:\/\//,'')); return; }
          try{ localStorage.setItem('pc_instances', JSON.stringify([...new Set([u, ...opts])].slice(0,6))); }catch(_){}
          if(window.__PC_SET_INSTANCE__) window.__PC_SET_INSTANCE__(u);
          else { try{ localStorage.setItem('pc_instance', u); }catch(_){} location.reload(); } };
        pick.querySelectorAll('.instance-chip').forEach(b=> b.onclick=()=>_switch(b.dataset.u));
        go.onclick=()=>_switch(inp.value);
        // "Relays only" — clear the instance entirely. Confirmed, because the features that disappear are
        // not obvious from a button ("where did Meme Builder go?"), and it is one tap from Connect.
        if(non) non.onclick=async()=>{
          if(_standalone()){ toast('already running on relays only'); return; }
          if(!await uiConfirm('Run on relays only? AI, Meme Builder, News, Torrents and Server Stats need a server and will be hidden. Your key, posts, Notes, Passwords and Budget are on relays and are unaffected — you can name an instance again at any time.')) return;
          if(window.__PC_SET_INSTANCE__) window.__PC_SET_INSTANCE__('');
          else { try{ localStorage.setItem('pc_instance',''); }catch(_){} location.reload(); }
        };
      } }
    // Native Tor (desktop app). One switch and a country; the shell owns the tor process, the SOCKS proxy
    // and the fail-closed behaviour, so all this does is read and set.
    { const row=$('#us-ntor-row');
      if(row && _hasNativeTor()){
        const cb=$('#us-ntor-on'), st=$('#us-ntor-state'), cc=$('#us-ntor-cc'), nb=$('#us-ntor-new');
        const paint=(s)=>{
          s=s||{};
          if(cb) cb.checked=!!s.enabled;
          if(cc){
            const list=[['','Any country (fastest)'], ...((s.countries)||[])];
            cc.innerHTML=list.map(([v,n])=>`<option value="${enc(v)}"${(s.country||'')===v?' selected':''}>${enc(n)}</option>`).join('');
            cc.disabled=!s.enabled;
          }
          if(nb) nb.disabled=!(s.enabled && s.running);
          if(!st) return;
          if(!s.enabled){ st.textContent='Off — traffic goes out normally.'; return; }
          if(s.error){ st.innerHTML='<svg class="ic st-ico warn" aria-hidden="true"><use href="#i-warn"></use></svg> '+enc(s.error); return; }
          if(!s.running){ st.textContent='Starting Tor…'; return; }
          st.textContent = s.bootstrapped
            ? ('Connected through Tor' + (s.country ? ' — exiting in ' + (s.countryName||s.country.toUpperCase()) : '') + '.')
            : ('Connecting to the Tor network… ' + (s.progress==null?'':s.progress + '%'));
        };
        window.pcShell.tor.status().then(paint).catch(()=>{ if(st) st.textContent='Tor is unavailable in this build.'; });
        // The shell pushes progress as it bootstraps — otherwise the panel would sit on "Connecting…"
        // until the user reopened Settings, which is when they are most likely to be watching it.
        if(window.pcShell.tor.onStatus) window.pcShell.tor.onStatus(paint);
        if(cb) cb.onchange=()=>{ if(st) st.textContent=cb.checked?'Starting Tor…':'Turning Tor off…';
          window.pcShell.tor.set({ enabled:cb.checked }).then(paint).catch(()=>{ if(st) st.textContent='could not change Tor'; }); };
        if(cc) cc.onchange=()=>{ if(st) st.textContent='Rebuilding circuits…';
          window.pcShell.tor.set({ country:cc.value }).then(paint).catch(()=>{ if(st) st.textContent='could not set the exit country'; }); };
        if(nb) nb.onclick=()=>{ nb.disabled=true;
          window.pcShell.tor.newCircuit().then(()=>{ toast('new Tor circuit'); }).catch(()=>toast('could not get a new circuit'))
            .finally(()=>window.pcShell.tor.status().then(paint).catch(()=>{})); };
      } }
    // Tor / Orbot (native app only). We never claim traffic IS on Tor — an app can't honestly know that
    // without an external request, which would defeat the point. We report what's installed, whether the
    // current instance is an onion, and let the connection be the proof.
    { const row=$('#us-tor-row'), st=$('#us-tor-state'), sb=$('#us-tor-start'), ob=$('#us-tor-open');
      // _capPlugin, NOT a bare Capacitor.Plugins.Orbot: a natively-registered plugin whose JS we don't
      // import isn't pre-attached to Capacitor.Plugins (see _capPlugin's comment), and the bare lookup
      // would come back null — silently REMOVING the whole panel on exactly the devices it's for.
      const O=_capPlugin('Orbot','isInstalled');
      if(row && st){
        if(!O){ row.remove(); }
        else{
          const onion=/\.onion$/i.test((()=>{try{return new URL(window.__PC_API_BASE__||'').hostname;}catch(_){return '';}})());
          const paint=inst=>{
            st.innerHTML = (inst
              ? 'Orbot is installed. Add <b>PosterChan</b> to Orbot’s app list (or turn on full-device VPN mode) to route this app over Tor.'
              : 'Orbot is not installed. It’s the Tor app for Android — install it to reach <code>.onion</code> instances.')
              + (onion ? '<br>This app is pointed at a <b>.onion</b> instance, so it will only connect while Orbot is routing it.'
                       : '<br>This app is on a clearnet instance; Tor is optional.');
            if(sb) sb.style.display = inst ? '' : 'none';
            if(ob) ob.textContent = inst ? 'Open Orbot' : 'Get Orbot';
          };
          O.isInstalled().then(r=>paint(!!(r&&r.installed))).catch(()=>paint(false));
          if(sb) sb.onclick=()=>{ O.start().then(r=>toast(r&&r.requested?'asked Orbot to start':'could not reach Orbot')).catch(()=>toast('could not reach Orbot')); };
          if(ob) ob.onclick=()=>{ O.openApp().catch(()=>{}); };
        }
      } }
    // Hide-DM-preview toggle: persist per-device and re-render Messages so it applies immediately.
    { const hd=$('#set-hide-dm-prev'); if(hd) hd.onchange=()=>{
        ClientSettings.set('hideDmPreview', hd.checked);
        toast(hd.checked?'DM previews hidden':'DM previews shown');
        if(S.VIEW==='messages'){ try{ renderMessages(); }catch(_){} }
      }; }
    { const toggle=$('#set-auto-mute'), update=$('#set-auto-mute-update');
      if(toggle) toggle.onchange=async()=>{
        const wanted=toggle.checked, owner=S.ME&&S.ME.pubkey;
        let epoch=S._autoMuteEpoch;
        try{
          if(!owner || S.GUEST || !/^[0-9a-f]{64}$/.test(owner))throw new Error('Sign in to use automatic mutes');
          // Save the choice before loading code: closing/updating the app during a
          // slow module load must not discard a checked preference. Keep all history.
          const previous=_autoMuteStored(), next={...previous,owner,enabled:wanted};
          localStorage.setItem('pc_auto_mute:'+owner,JSON.stringify(next));
          epoch=++S._autoMuteEpoch;
          if(S._autoMuteEngine)S._autoMuteEngine.destroy();
          S._autoMuteEngine=null;S._autoMuteLoading=null;
          _cacheAutoMute(next);S._autoMuteMessage='';
          if(previous.enabled!==wanted)_applyAutoMuteToView();
          _scheduleAutoMutes();_paintAutoMuteControls();
          /* AND ON THE ACCOUNT, not just this browser. Awaited so the status line can say when it
           * did not land — a switch that silently only applies to the device you flipped it on is
           * this whole bug. A read must have answered first, or publishing would replace a real
           * preference with this device's default. */
          if(!_autoMuteDocReady()) await _autoMuteDocLoad();
          if(!await _autoMuteDocSave() && _autoMuteDocReady())
            S._autoMuteMessage='Saved on this device only — the relays did not accept the change.';
          if(wanted){
            await _loadAutoMute();
            if(S.ME&&S.ME.pubkey===owner && epoch===S._autoMuteEpoch)_updateAutoMutes();
          }
        }catch(error){if(S.ME&&S.ME.pubkey===owner && epoch===S._autoMuteEpoch)S._autoMuteMessage=error.message||'Could not save automatic mute settings';}
        if(S.ME&&S.ME.pubkey===owner && epoch===S._autoMuteEpoch)_paintAutoMuteControls();
      };
      if(update)update.onclick=()=>_updateAutoMutes();
      _paintAutoMuteControls();
    }
    { const wb=$('#set-words-save'); if(wb) wb.onclick=async()=>{
        const words=($('#set-muted-words').value||'').split('\n').map(w=>w.trim()).filter(Boolean);
        wb.disabled=true; const st=$('#set-words-status'); if(st) st.textContent='saving…';
        try{ const okv=await saveMutedWords(words);
          if(st) st.textContent = okv ? ('Saved — '+S.MUTED_WORDS.size+' muted word(s). New posts are filtered immediately.') : 'Couldn’t reach the relay — not saved, try again.'; }
        catch(e){ if(st) st.textContent='Save failed: '+((e&&e.message)||e); }
        finally{ wb.disabled=false; }
      }; }
    // Fill relays/media from Nostr (10002/10063) ONCE — renderUserSettings re-runs on many settings
    // sub-actions, and re-querying would clobber in-progress relay edits each time.
    if(!S._nostrPrefsLoaded){ S._nostrPrefsLoaded=true; loadNostrPrefs(); }
    usLoadKeys();   // populate API Keys immediately (not only on tab click)
    $('#us-mail-add').onclick=()=>{ _usMail.push({email:'',imap_server:'',imap_port:993,smtp_server:'',smtp_port:587,password:''}); usRenderMail(); };
    // Telegram link key
    { const k=$('#us-tg-key'); if(k) k.onclick=async()=>{ const box=$('#us-tg-keybox'); box.textContent='generating…';
        try{ const d=await fetch('/api/telegram/generate-key',{method:'POST'}).then(r=>r.json());
          if(!d.key){ box.textContent='failed: '+enc(d.detail||''); return; }
          // One-tap deep link (opens the bot with the key pre-filled → just tap Start). Fall back to
          // the manual command if the bot username is unknown.
          box.innerHTML = (d.deep_link
            ? `<a class="btn btn-cyan small" href="${enc(d.deep_link)}" target="_blank" rel="noopener"><svg class="ic b-ic" aria-hidden="true"><use href="#i-phone"></use></svg>Link Telegram${d.bot_username?(' (@'+enc(d.bot_username)+')'):''}</a><div class="muted small" style="margin-top:6px">or send <code>/start ${enc(d.key)}</code> to the bot manually</div>`
            : `Send this to the bot in Telegram: <code>/start ${enc(d.key)}</code>`);
        }catch(_){ box.textContent='failed'; } }; }
    { const u=$('#us-tg-unlink'); if(u) u.onclick=async()=>{ if(!await uiConfirm('Unlink Telegram?'))return;
        await fetch('/api/telegram/unlink',{method:'POST'}); toast('unlinked'); renderUserSettings(); }; }
    // Wait for an account link to actually land, then re-render.
    //
    // This used to rely SOLELY on the callback page doing window.opener.postMessage(...). When the OAuth page
    // opens as a tab rather than a popup (or the browser nulls `opener` under cross-origin-opener rules) that
    // message never arrives — so the token was saved server-side while the panel still said "Connect with
    // OAuth". The user re-authorises, it works again, and still looks broken. Poll the server for the truth
    // instead; the postMessage stays as a fast path when it does work.
    function _awaitLink(done, statusEl, msgName){
      let stop=false;
      const finish=()=>{ if(stop) return; stop=true; window.removeEventListener('message',h); renderUserSettings(); };
      const h=e=>{ if(e.data===msgName) finish(); };
      window.addEventListener('message',h);
      const t0=Date.now();
      (async function poll(){
        while(!stop && Date.now()-t0 < 180000){
          await new Promise(r=>setTimeout(r, 2000));
          if(stop) return;
          try{ const s=await fetch('/api/auth/settings').then(r=>r.ok?r.json():null); if(s && done(s)) return finish(); }catch(_){}
        }
        if(!stop){ stop=true; window.removeEventListener('message',h);
          if(statusEl) statusEl.textContent='still not linked — if you approved it, reload this page'; }
      })();
    }
    // Pleroma OAuth (opens the instance in a new tab; we then wait for the link to actually appear)
    { const c=$('#us-plr-conn'); if(c) c.onclick=async()=>{ const st=$('#us-plr-stat'); const url=$('#us-plr-url').value.trim(); if(!url){st.textContent='enter the instance URL';return;} st.textContent='registering app…';
        const r=await fetch('/api/pleroma/oauth/start',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({instance_url:url})}); const d=await r.json().catch(()=>({}));
        if(!r.ok){ st.textContent=d.detail||'failed'; return; } window.open(d.auth_url,'_blank'); st.textContent='waiting for authorization…';
        _awaitLink(s=>!!s.pleroma_has_access_token, st, 'pleroma_connected'); }; }
    { const d=$('#us-plr-disc'); if(d) d.onclick=async()=>{ if(!await uiConfirm('Disconnect Pleroma?'))return; await fetch('/api/pleroma/disconnect',{method:'POST'}); renderUserSettings(); }; }
    // Finance: remove the stored key
    // API keys
    $('#us-key-new').onclick=async()=>{ const name=$('#us-key-name').value.trim();
      const d=await fetch('/api/auth/api-keys',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name})}).then(r=>r.json()).catch(()=>({}));
      if(d && d.key){ await uiPrompt('Your new API key — copy it now, it is shown ONCE:', {value:d.key, ok:'Done', cancel:'Close'}); $('#us-key-name').value=''; usLoadKeys(); } else toast('create failed'); };   // in-app (not native alert — see feedback_no_native_dialogs)
    // Presets are only persisted on Save if the user actually edited them (else an unrelated Save clobbers).
    let _presetsEdited=false;
    { const zi=$('#us-zap-presets'), xi=$('#us-xmr-presets'), bi=$('#us-bch-presets'); if(zi) zi.oninput=()=>_presetsEdited=true; if(xi) xi.oninput=()=>_presetsEdited=true; if(bi) bi.oninput=()=>_presetsEdited=true; }
    { const fm=$('#us-fedi-only'); if(fm){
      const update=()=>{ const hide=$('#us-fedi-hide-options'), cross=$('#us-fedi-crosspost');
        if(hide) hide.hidden=fm.checked; if(cross) cross.disabled=fm.checked; };
      fm.onchange=update; update();
    } }
    // Save (text + toggles; connect flows persist themselves)
    $('#us-save').onclick=async()=>{
      // Amount presets are CLIENT prefs (not account columns): only touch them if the user actually EDITED
      // the fields this session — otherwise a Save that changed only (say) the email would force the
      // default-prefilled values over the user's custom presets on their other devices (replaceable-wipe).
      // Store '' when the list equals the built-in defaults, so future default changes still apply. AWAIT
      // the sync so a same-Save relay/media reload can't abort the fire-and-forget publish.
      if(_presetsEdited){
        const zp=_parsePresets(($('#us-zap-presets')||{}).value, _ZAP_DEFAULTS, true);
        const xp=_parsePresets(($('#us-xmr-presets')||{}).value, _XMR_DEFAULTS);
        const bp=_parsePresets(($('#us-bch-presets')||{}).value, _BCH_DEFAULTS);
        const zpStr = zp.join(',')===_ZAP_DEFAULTS.join(',') ? '' : zp.join(', ');
        const xpStr = xp.join(',')===_XMR_DEFAULTS.join(',') ? '' : xp.join(', ');
        const bpStr = bp.join(',')===_BCH_DEFAULTS.join(',') ? '' : bp.join(', ');
        ClientSettings.set('zapPresets', zpStr); ClientSettings.set('xmrPresets', xpStr); ClientSettings.set('bchPresets', bpStr);
        _prefTouched.add('zapPresets'); _prefTouched.add('xmrPresets'); _prefTouched.add('bchPresets');
        await saveClientPrefsNostr({ zapPresets: zpStr, xmrPresets: xpStr, bchPresets: bpStr });
      }
      // Every field here belongs to a pane that only exists when there IS a server, and with no
      // instance those panes are gone — so read them defensively. Unguarded `.value` on a missing
      // #us-email threw here, and because this runs BEFORE the client-side saves below it took the
      // relay and media edits down with it: the Save button did nothing at all, silently.
      const _fv=(id)=>{ const el=$(id); return el ? String(el.value||'').trim() : ''; };
      const _fc=(id)=>{ const el=$(id); return el ? !!el.checked : false; };
      const body={ notification_email:_fv('#us-email'), news_sources:($('#us-news-src')||{}).value||'',
        telegram_notifications:_fv('#us-tg-notif'), social_notif_enabled:_fc('#us-social-notif'),
                fedi_bridge_enabled:_fc('#us-fedi-bridge'),
        fedi_crosspost_enabled:_fc('#us-fedi-crosspost'),
        ...($('#us-fedi-only') ? {fedi_only:_fc('#us-fedi-only')} : {}),
        pleroma_instance_url:_fv('#us-plr-url'),
        theme:($('#us-theme')&&$('#us-theme').value)||'cyberpunk',
        mail_accounts:usCollectMail() };
      const st=$('#us-save-status'); if(st) st.textContent='saving…';
      // Persist the client-side tabs too, so the single Save button saves EVERYTHING (not just the
      // server account settings — the "relay/media edits silently dropped" bug). Reload only when the
      // relay/media config actually changed (those reconnect the app).
      let needReload=false;
      if($('#set-relays-on')){
        syncRelays();
        const urls=[...new Set(S._setRelays.map(u=>normalizeRelay(u)).filter(Boolean))];
        const on=$('#set-relays-on').checked;
        // Disabled controls may contain seeded fallback URLs; saving another tab must not reconnect.
        const relayChanged = on!==!!ClientSettings.get('relaysEnabled') || (on && JSON.stringify(urls)!==JSON.stringify(userRelays()));
        if(relayChanged){
          needReload=true;
          /* The private libraries have to follow, or the vault reads empty on the new relay and the
           * next save splits it across two. Two halves: pull them off the OLD relays NOW, while we
           * are still connected to them, and flag the republish for after the reconnect. */
          try{ localStorage.setItem(_CARRY_KEY, String(Date.now())); }catch(_){ }
          try{ await stashPrivateBeforeRelayChange(); }catch(_){ }
        }
        /* TURNING IT OFF MUST NOT WRITE A LIST. `urls` is read from the relay textarea, and that
         * control is DISABLED while the switch is off — it shows seeded fallback suggestions, not
         * a choice anybody made (the comment above already says so). Saving it anyway overwrote
         * the user's real list with our suggestions, so switching the setting off and back ON
         * restored relays they had never picked: reported as "i did turn the setting off and on"
         * and still three relays, with no way to get rid of them from the UI.
         *
         * The switch is the only thing OFF changes. The list is theirs and is left exactly as they
         * last saved it, so turning it on again gives them back what they chose. */
        ClientSettings.set('relaysEnabled', on);
        if(on) ClientSettings.set('relays', urls);
        // NIP-65 is replaceable: a write made from this device's stale localStorage becomes the newest
        // global relay list and overwrites a newer edit made in Amethyst or another client. The global
        // Save button covers every settings pane, so it may publish kind 10002 ONLY when the relay
        // controls themselves differ from their saved local baseline. A dedicated "Save & reload"
        // above remains the explicit force-publish route for someone intentionally restoring a list.
        /* …and it may go out ONLY when we have confirmed what is already published. A kind-10002
         * write replaces the user's global relay list, including relays this device has never seen;
         * "no relay answered" is not "they have none". Refusing is visible rather than silent,
         * because a save that quietly did half of what it said is the worse failure. */
        try{
          if(relayChanged && on && urls.length){
            if(S._nip65Confirmed) await publish(10002,'',urls.map(u=>['r',u]));
            else toast('relays saved on this device — not published, because your existing relay list could not be read');
          }
        }catch(_){}
      }
      if($('input[name=media-mode]')){
        const { enabled, url } = _mediaChoice();
        // Ignore a half-filled 'My own server' (blank URL) so the global Save doesn't wipe the current choice.
        if(!(enabled && !url)){
          if(enabled!==!!ClientSettings.get('blossomEnabled') || (enabled && url!==ClientSettings.get('mediaServer',''))) needReload=true;
          ClientSettings.set('blossomEnabled', enabled); ClientSettings.set('mediaServer', url);
          if(enabled){ const proto=/(?:^|\/\/)(?:www\.)?nostr\.build\/?$/i.test(url) ? 'nip96' : await detectProto(url);
            ClientSettings.set('mediaProto', proto);
            try{ await publish(proto==='nip96'?10096:10063,'',[['server',url]]); }catch(_){}
          } else { ClientSettings.set('mediaProto',''); try{ await publish(10063,'',[]); }catch(_){} try{ await publish(10096,'',[]); }catch(_){} }
        }
      }
      if($('#set-nwc')){ const u=($('#set-nwc').value||'').trim(); if(!u || Nwc.parse(u)) ClientSettings.set('nwc', u); }
      // With no instance there is no account to PUT to, and reporting "save failed" over a save that
      // fully succeeded (relays, media, theme, presets are all client-side) would be a lie.
      if(_standalone()){
        applyTheme(body.theme); toast('settings saved');
        if(st) st.textContent=needReload?'✓ Saved — reloading':'✓ Saved';
        if(needReload) setTimeout(()=>location.reload(),600);
        return;
      }
      try{ const r=await fetch('/api/auth/settings',{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
        if(r.ok){ applyTheme(body.theme); if(typeof body.fedi_only==='boolean') _setFediOnly(body.fedi_only); toast('settings saved');
          if(st) st.textContent=needReload?'✓ Saved — reloading':'✓ Saved';
          if(needReload) setTimeout(()=>location.reload(),600);
        } else if(st) st.textContent='save failed ('+r.status+')';
      }catch(_){ if(st) st.textContent='save failed'; }
    };
    // Live theme preview: apply on change without waiting for Save (revert is a page reload / re-save).
    { const ts=$('#us-theme'); if(ts) ts.onchange=()=>applyTheme(ts.value, false); }   // PREVIEW only (no persist); Save writes it
    /* Language applies IMMEDIATELY and persists itself, unlike the theme — which is a preview until
     * Save. The difference is not an inconsistency: a theme repaints, while switching language can
     * only translate what is on screen right now (see i18n.js), so everything already drawn stays in
     * the old language until it is redrawn. Leaving that half-state sitting behind an unpressed Save
     * button reads as "the language setting is broken". It is stored in localStorage rather than on
     * the account on purpose — it has to work with no server and be readable before the first paint,
     * which is the same reason the theme has its own no-flash cache. */
    { const ls=$('#us-lang'); if(ls) ls.onchange=()=>{
        if(!window.PCI18N) return;
        const want = ls.value;
        PCI18N.set(want).then(ok=>{
          if(!ok && want!=='en'){ toast('could not load that language — staying in English'); ls.value='en'; return; }
          if(want!=='en') toast('language changed — reload to translate everything already on screen');
        });
      }; }
  }
  function usRenderMail(){
    const wrap=$('#us-mail-list'); if(!wrap) return;
    wrap.innerHTML=_usMail.map((a,i)=>`<div class="us-mail" data-i="${i}">
      <input class="input" data-f="email" placeholder="email" value="${enc(a.email||'')}">
      <input class="input" data-f="imap_server" placeholder="IMAP server" value="${enc(a.imap_server||'')}">
      <input class="input" data-f="imap_port" placeholder="993" value="${enc(String(a.imap_port||993))}">
      <input class="input" data-f="smtp_server" placeholder="SMTP server" value="${enc(a.smtp_server||'')}">
      <input class="input" data-f="smtp_port" placeholder="587" value="${enc(String(a.smtp_port||587))}">
      <input class="input" data-f="password" type="password" placeholder="${a.password?'•••• (kept — blank keeps it)':'password'}">
      <button class="mini us-mail-del" data-i="${i}" title="remove"><svg class="ic x-ic" aria-hidden="true"><use href="#i-close"></use></svg></button>
      ${a.email ? `<button class="btn btn-ghost small us-mail-folders" data-email="${enc(a.email)}">📂 Folders</button>` : ''}
      </div>`).join('');
    $$('.us-mail-del',wrap).forEach(b=> b.onclick=()=>{ _usMail.splice(+b.dataset.i,1); usRenderMail(); });
    $$('.us-mail-folders',wrap).forEach(b=> b.onclick=()=> usMailFolders(b.dataset.email));
  }

  /* Which real folder plays each role, for one account.
   *
   * Detection (RFC 6154 special-use, then name heuristics) is right on most servers and wrong on
   * some — and when Sent is wrong, a message you just sent is filed where this app never looks,
   * which reads as "it never sent". This node has THREE sent-like folders (Sent, Sent Messages,
   * INBOX.Sent), so there is nothing for a heuristic to be confident about.
   *
   * "Detect automatically" is a real choice, not a blank: it clears the override so detection runs
   * again, rather than leaving the account with no Sent folder at all.
   */
  async function usMailFolders(email){
    const ROLES=[['sent','📤 Sent'],['drafts','📝 Drafts'],['trash','🗑 Trash'],
                 ['junk','⚠️ Spam / Junk'],['archive','🗄 Archive']];
    modal(`<h3>📂 Folders — ${enc(email)}</h3><div id="usf-body"><div class="spinner"></div></div>`);
    let d=null;
    try{ d=await fetch('/api/mail/folder-map?account='+encodeURIComponent(email)).then(r=>r.json()); }
    catch(_){}
    const box=$('#usf-body'); if(!box) return;
    if(!d || d.detail || !Array.isArray(d.folders)){
      box.innerHTML=`<div class="muted small">Could not reach that mailbox. Check the server and
        password above, save, then try again.</div>`;
      return;
    }
    if(!d.folders.length){ box.innerHTML='<div class="muted small">That account reported no folders.</div>'; return; }
    box.innerHTML=`<p class="muted small">Pick which folder on the server plays each role. Leave one
        on <b>Detect automatically</b> to keep using what the server reports.</p>
      <div class="usf-grid">${ROLES.map(([k,label])=>{
        const chosen=(d.mapping||{})[k]||'';
        const auto=(d.detected||{})[k];
        return `<label class="usf-row"><span class="usf-lbl">${label}</span>
          <select class="input usf-sel" data-role="${k}">
            <option value=""${chosen?'':' selected'}>Detect automatically${auto?` (${enc(auto)})`:''}</option>
            ${d.folders.map(f=>`<option value="${enc(f)}"${f===chosen?' selected':''}>${enc(f)}</option>`).join('')}
          </select></label>`; }).join('')}</div>
      <div class="row" style="margin-top:14px"><button class="btn btn-cyan" id="usf-save">Save folders</button></div>`;
    $('#usf-save').onclick=async()=>{
      const mapping={};
      $$('.usf-sel').forEach(sel=>{ if(sel.value) mapping[sel.dataset.role]=sel.value; });
      const btn=$('#usf-save'); btn.disabled=true; btn.textContent='Saving…';
      try{
        const r=await fetch('/api/mail/folder-map',{method:'PUT',headers:{'Content-Type':'application/json'},
                                                    body:JSON.stringify({account:email, mapping})}).then(r=>r.json());
        if(r && r.ok){ closeModal(); toast('folders saved'); }
        else throw new Error((r&&r.detail)||'failed');
      }catch(err){ toast('could not save: '+((err&&err.message)||'error')); btn.disabled=false; btn.textContent='Save folders'; }
    };
  }
  function usCollectMail(){
    const wrap=$('#us-mail-list'); if(!wrap) return _usMail;
    return $$('.us-mail',wrap).map(row=>{ const o={}; $$('input',row).forEach(inp=>{ const f=inp.dataset.f; let v=inp.value; if(f==='imap_port'||f==='smtp_port') v=parseInt(v,10)||(f==='imap_port'?993:587); if(f==='password'&&!v) return; o[f]=v; }); return o; }).filter(o=>o.email);
  }
  async function usLoadKeys(){
    const wrap=$('#us-key-list'); if(!wrap) return;
    let keys=[]; try{ keys=await fetch('/api/auth/api-keys').then(r=>r.json()); }catch(_){}
    wrap.innerHTML=(keys||[]).map(k=>`<div class="us-key"><div><b>${enc(k.name||'Default')}</b> <span class="muted small">${k.is_active?'active':'disabled'}</span></div>
      <div><button class="mini" data-tog="${k.id}">${k.is_active?'Disable':'Enable'}</button><button class="mini" data-del="${k.id}" style="color:var(--danger)">Delete</button></div></div>`).join('')||'<div class="muted small">No keys yet.</div>';
    $$('[data-tog]',wrap).forEach(b=> b.onclick=async()=>{ await fetch('/api/auth/api-keys/'+b.dataset.tog+'/toggle',{method:'PUT'}); usLoadKeys(); });
    $$('[data-del]',wrap).forEach(b=> b.onclick=async()=>{ if(!await uiConfirm('Delete this API key?'))return; await fetch('/api/auth/api-keys/'+b.dataset.del,{method:'DELETE'}); usLoadKeys(); });
  }
  function niceImport(){ const p=Store.profile(S.ME.pubkey)||{}; return p.nip05?String(p.nip05).replace(/^_@/,''):''; }
  function drawRelayRows(){
    const wrap=$('#set-relay-list'); if(!wrap) return;
    wrap.innerHTML = S._setRelays.map((u,i)=>`<div class="relay-row"><span class="rr-dot"></span><input class="input" value="${enc(u)}" placeholder="wss://relay.example.com" data-i="${i}"><button class="mini rr-del" data-i="${i}" title="remove"><svg class="ic x-ic" aria-hidden="true"><use href="#i-close"></use></svg></button></div>`).join('');
    $$('.rr-del',wrap).forEach(b=> b.onclick=()=>{ S._setRelays = $$('#set-relay-list input').map(x=>x.value.trim()); S._setRelays.splice(+b.dataset.i,1); if(!S._setRelays.length)S._setRelays=['']; drawRelayRows(); });
  }
  function mergeRelays(found){
    const have=new Set(S._setRelays.map(normalizeRelay).filter(Boolean));
    let added=0; for(const u of found){ const n=normalizeRelay(u); if(n && !have.has(n)){ have.add(n); added++; } }
    S._setRelays=[...have]; if(!S._setRelays.length)S._setRelays=['']; drawRelayRows();
    return added;
  }
  // NIP-07: window.nostr.getRelays() -> { url: {read,write} }
  async function importExtensionRelays(){
    if(!window.nostr || !window.nostr.getRelays){ toast('no extension relay list'); return; }
    try{ const r=await window.nostr.getRelays(); const urls=Array.isArray(r)?r:Object.keys(r||{});
      const n=mergeRelays(urls); toast(n?`added ${n} relay${n>1?'s':''}`:'no new relays'); }
    catch(_){ toast('extension import failed'); }
  }
  // NIP-05: resolve to a pubkey, then pull its relays from the nostr.json `relays` map, falling
  // back to the author's kind:10002 (NIP-65) relay-list event on the currently connected relay.
  async function importNip05Relays(addr){
    addr=(addr||'').trim().replace(/^@/,''); if(!addr.includes('@')){ toast('enter a name@domain address'); return; }
    const [name,domain]=addr.split('@');
    try{
      const j=await fetch(`https://${domain}/.well-known/nostr.json?name=${encodeURIComponent(name)}`).then(r=>r.json());
      const pk=j&&j.names&&j.names[name];
      let urls=(pk && j.relays && j.relays[pk]) ? j.relays[pk] : [];
      if((!urls||!urls.length) && pk){
        const ev=await Relay.query([{ authors:[pk], kinds:[10002], limit:1 }]);
        if(ev[0]) urls=ev[0].tags.filter(t=>t[0]==='r').map(t=>t[1]);
      }
      if(!urls||!urls.length){ toast('no relays found for that NIP-05'); return; }
      const n=mergeRelays(urls); toast(n?`added ${n} relay${n>1?'s':''}`:'no new relays');
    }catch(_){ toast('NIP-05 lookup failed'); }
  }
  return {
    _renderSignerApps, drawRelayRows, renderSettings, renderSigner,
  };
};
