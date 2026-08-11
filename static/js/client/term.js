/* #terminal — a real interactive shell, in the client.
 *
 * A PTY on a host the operator listed, over a WebSocket to /ws/ssh. xterm.js does the emulation (it
 * is VENDORED at /static/vendor/xterm — nothing here may load from a CDN, and the desktop/mobile
 * bundles copy static/vendor wholesale), and this file is the screen around it: pick a host, ask for
 * a password only when the host has no key, connect, and keep the PTY the size of the window.
 *
 * WHAT MAKES IT USABLE ON A PHONE, which is most of the work here:
 *
 *   A soft keyboard has no Ctrl, no Esc, no Tab and no arrows — and a shell without Ctrl-C is not a
 *   shell. So there is a key BAR: Esc, Tab, Ctrl (sticky, applies to the next key), the arrows,
 *   Home/End and Ctrl-C. It is the difference between a terminal you can use on a phone and a
 *   terminal you can look at on a phone.
 *
 *   The font scales with the viewport rather than being fixed, because 14px of monospace on a 390px
 *   screen is 27 columns and almost nothing prints usefully at 27 columns.
 *
 *   A visible input is what summons the soft keyboard at all: tapping a canvas does not. A hidden
 *   one-line field takes the focus, forwards what is typed, and is what the ⌨ button focuses.
 *
 * A SESSION IS A TMUX SESSION. The PTY lives on the server, not in this tab: the socket dropping —
 * routine over Tor, and routine on a phone that locks — leaves the shell running, and coming back
 * reattaches to it and replays what was missed. So this file keeps the session id (per instance, in
 * localStorage), reconnects on its own with a backoff, and offers any shell the ACCOUNT still has
 * running when there is no id to hand — the case a reload or a second device leaves you in.
 *
 * Detach and Kill are therefore two different buttons and always will be. One leaves your build
 * running; the other is the only thing that ends a session, since nothing here expires.
 *
 * The socket speaks the frames documented in app/routers/ssh_term.py.
 */
(function(){
  function init(){
    const PC = window.__PC;
    if(!PC){ return setTimeout(init, 50); }
    const { $, $$, enc, toast, authFetch } = PC;

    const XT = () => window.Terminal;              // xterm's global, once its <script> has run
    let term = null, fit = null, ws = null, host = null, ro = null;
    let hosts = [], connected = false, ctrl = false, mounted = null;
    let sid = '', cursor = 0, retry = 0, retryT = null, want = false, live = [];

    /* The session id is kept PER INSTANCE — sessions live in one node's process, so carrying an id
     * from another instance can only ever produce a "that session is no longer running". */
    const SKEY = () => 'pc_tty_sid:' + (window.__PC_API_BASE__ || location.origin);
    function _remember(id){
      sid = id || '';
      try{ sid ? localStorage.setItem(SKEY(), sid) : localStorage.removeItem(SKEY()); }catch(_){}
    }
    function _recall(){ try{ return localStorage.getItem(SKEY()) || ''; }catch(_){ return ''; } }

    // Character cell width scales with the screen: a fixed 14px leaves a phone with ~27 columns,
    // which is too narrow for `ls -l`, let alone anything that draws a box.
    function fontSize(){
      const w = Math.min(window.innerWidth, window.innerHeight * 1.6);
      if(w < 420) return 10;
      if(w < 700) return 11;
      if(w < 1100) return 13;
      return 14;
    }

    function _shellHtml(){
      return `<div class="tty-wrap">
        <div class="tty-bar">
          <select class="input tty-host" id="tty-host" aria-label="Host"></select>
          <button class="btn btn-neon small" id="tty-go">Connect</button>
          <button class="btn btn-ghost small hidden" id="tty-stop" title="Leave it running">Detach</button>
          <button class="btn btn-ghost small hidden tty-kill" id="tty-kill" title="End this session">Kill</button>
          <span class="tty-state" id="tty-state"></span>
        </div>
        <div class="tty-sessions" id="tty-sessions" hidden></div>
        <div class="tty-screen" id="tty-screen"></div>
        <div class="tty-keys" id="tty-keys" hidden>
          <button data-k="Escape">esc</button>
          <button data-k="Tab">tab</button>
          <button data-k="ctrl" class="tty-ctrl">ctrl</button>
          <button data-k="ArrowUp">↑</button>
          <button data-k="ArrowDown">↓</button>
          <button data-k="ArrowLeft">←</button>
          <button data-k="ArrowRight">→</button>
          <button data-k="Home">home</button>
          <button data-k="End">end</button>
          <button data-k="^C" class="tty-int">^C</button>
          <button data-k="kbd" class="tty-kbd">⌨</button>
        </div>
        <input class="tty-catch" id="tty-catch" autocomplete="off" autocorrect="off"
               autocapitalize="off" spellcheck="false" aria-label="Terminal input">
      </div>`;
    }

    function _state(msg, cls){
      const s = $('#tty-state'); if(!s) return;
      s.textContent = msg || '';
      s.className = 'tty-state' + (cls ? ' ' + cls : '');
    }

    async function loadHosts(){
      try{
        // The bundled apps authenticate with a BEARER, not a cookie (they are cross-origin to the
        // instance), and that token is minted lazily. Without this the first visit to the Terminal
        // is a 401 that reads as "you are not allowed" — see the same call in every other authed
        // screen. It is a no-op once the session exists.
        try{ await PC.ensureAiSession(); }catch(_){}
        const r = await authFetch('/api/ssh/hosts');
        if(r.status === 403){ hosts = []; _state('the SSH terminal is switched off, or you are not on its list', 'err'); return false; }
        const d = await r.json();
        hosts = (d && d.hosts) || [];
        if(d && d.available === false){
          _state('this node has no SSH library installed — run install.sh', 'err');
          return false;
        }
        return true;
      }catch(_){ _state('could not reach the server', 'err'); return false; }
    }

    function _mountTerm(){
      const box = $('#tty-screen'); if(!box || !XT()) return false;
      term = new (XT())({
        fontSize: fontSize(),
        fontFamily: 'ui-monospace, SFMono-Regular, Menlo, Consolas, "DejaVu Sans Mono", monospace',
        cursorBlink: true,
        convertEol: false,
        scrollback: 5000,
        theme: { background: '#07040f', foreground: '#d6e2ff', cursor: '#00f0ff',
                 selectionBackground: 'rgba(0,240,255,.28)' },
      });
      try{
        const F = window.FitAddon && (window.FitAddon.FitAddon || window.FitAddon);
        if(F){ fit = new F(); term.loadAddon(fit); }
      }catch(_){ fit = null; }
      term.open(box);
      _fit();
      term.onData(d => _send({ t: 'in', d }));
      /* The PTY has to be told the size, and it has to be told the size that xterm actually chose —
       * a mismatch is what makes a shell wrap in the wrong place and redraw over itself. */
      ro = (typeof ResizeObserver !== 'undefined') ? new ResizeObserver(() => _fit()) : null;
      if(ro) ro.observe(box);
      return true;
    }

    let _fitT = null, _sentSize = '';
    function _fit(){
      if(_fitT) clearTimeout(_fitT);
      // Coalesced: an on-screen keyboard opening fires a burst of resizes, and each one is a reflow
      // of the whole grid plus a frame on the wire.
      _fitT = setTimeout(() => {
        _fitT = null;
        if(!term) return;
        try{ term.options.fontSize = fontSize(); }catch(_){}
        try{ if(fit) fit.fit(); }catch(_){}
        /* A ZERO-SIZED BOX IS NOT A SIZE. In desktop mode the Terminal's window is PARKED when
         * another window takes focus — its nodes are moved aside, which fires the ResizeObserver
         * against an element with no layout. FitAddon declines to compute anything then, so
         * `term.cols` still holds the last good value and this is only a wasted frame — but a
         * genuinely small transient (a window animating open) would be sent, and the remote app
         * would redraw itself at that width. Neither is worth a resize_pty on the far end. */
        const c = term.cols | 0, r = term.rows | 0;
        if(c < 2 || r < 2) return;
        /* AND ONLY WHEN IT CHANGED. Dragging a desktop window's edge produces a resize every frame;
         * coalescing turns that into one frame per 80ms of settling, which is still a SIGWINCH storm
         * at the far end — and a full-screen program redraws its entire display on each one. The
         * common case of _fit (a reconnect, a repaint, a focus) is that nothing moved at all. */
        const sig = c + 'x' + r;
        if(sig === _sentSize) return;
        _sentSize = sig;
        _send({ t: 'size', cols: c, rows: r });
      }, 80);
    }

    function _send(o){ try{ if(ws && ws.readyState === 1) ws.send(JSON.stringify(o)); }catch(_){} }

    function _wsUrl(){
      const base = (window.__PC_API_BASE__ || location.origin).replace(/^http/, 'ws').replace(/\/+$/, '');
      return base + '/ws/ssh';
    }

    async function connect(){
      const sel = $('#tty-host'); if(!sel) return;
      host = sel.value;
      const h = hosts.find(x => x.name === host);
      if(!h){ _state('pick a host', 'err'); return; }
      // Only ask when the host has no key on the server. Asking anyway would train people to type a
      // password into a box that did not need one.
      let password = '';
      if(!h.keyed){
        password = await PC.uiPrompt(`Password for ${h.label}`, { password: true, ok: 'Connect' });
        if(password === null) return;
      }
      _remember('');                       // a fresh connect is a NEW session, not the old one
      cursor = 0;
      if(term) term.reset();
      _open({ host, password });
    }

    /* Reattach to a shell that is already running. No password is asked for and none is needed: the
     * login happened when the session was opened, and it is still open. */
    function attach(id, hostName){
      host = hostName || host;
      _remember(id);
      cursor = 0;                          // no local scrollback for it — replay from the start of
                                           // what the server still holds
      if(term) term.reset();
      _open({ resume: id, host: hostName || '' });
    }

    function _open(frame){
      want = true;
      /* The cache above is per-SOCKET, not per-terminal. A reattach opens a PTY that knows nothing
       * about what this client last sent — and on the cross-device path (start on the laptop, pick it
       * up on the phone) the size it is about to be told is the one thing that must not be skipped as
       * "unchanged". The open frame carries cols/rows, and the `ready` handler re-fits behind it. */
      _sentSize = '';
      // TEAR DOWN ANY EXISTING SOCKET FIRST. Attaching to another session from the strip is reachable
      // while one is already connected, and leaving the old socket alive means its `onclose` fires
      // into the NEW session's state a moment later — reported as a terminal that connects and then
      // immediately says it is reconnecting. Nulling onclose first is what makes this a detach rather
      // than a drop.
      if(ws){ try{ ws.onclose = null; ws.onmessage = null; ws.close(); }catch(_){} ws = null; }
      if(retryT){ clearTimeout(retryT); retryT = null; }
      connected = false;
      (async () => {
        try{ await PC.ensureAiSession(); }catch(_){}   // the socket's token
        if(!term && !_mountTerm()){ _state('the terminal could not start', 'err'); want = false; return; }
        if(!want) return;
        _state(frame.resume ? 'reattaching…' : 'connecting…');
        try{ ws = new WebSocket(_wsUrl()); }
        catch(_){ _state('could not open a connection', 'err'); return _later(); }
        ws.onopen = () => {
          /* The socket authenticates with the SAME bearer the rest of the app uses. It is sent in the
           * open frame rather than the URL: a query string lands in every proxy log between here and
           * the server, and this one is a credential. */
          _send(Object.assign({ t: 'open', cols: term.cols, rows: term.rows, cursor,
                                token: (window.__PC_TOKEN__ || '') }, frame));
          frame.password = '';             // never kept past the one frame that needs it
        };
        ws.onmessage = (ev) => {
          let m; try{ m = JSON.parse(ev.data); }catch(_){ return; }
          if(m.t === 'out'){
            term.write(m.d);
            // The CURSOR is what a reconnect resumes from, so it advances only for bytes that reached
            // the screen. Trusting a locally counted length instead would drift the first time a
            // multi-byte character straddled a frame.
            if(typeof m.seq === 'number') cursor = m.seq;
            return;
          }
          if(m.t === 'ready'){
            connected = true; retry = 0;
            _remember(m.sid || '');
            if(m.host) host = m.host;
            _state((m.resumed ? 'reattached to ' : 'connected to ') + host, 'ok');
            _chrome(true); _fit(); _focus();
            return;
          }
          if(m.t === 'gone'){
            // The shell really is gone (server restarted, or it was killed elsewhere). Say so once,
            // clearly — a silent new shell in the same window looks like your work vanished.
            _remember(''); cursor = 0;
            term.write('\r\n\x1b[33m' + m.m + '\x1b[0m\r\n');
            return;
          }
          if(m.t === 'err'){
            want = false;                  // a refusal is not something to retry into
            term.write('\r\n\x1b[31m' + m.m + '\x1b[0m\r\n'); _state(m.m, 'err');
            return;
          }
          if(m.t === 'end'){
            /* `end` MEANS THE SESSION IS OVER — the shell exited, or somebody killed it from another
             * device — as opposed to the socket merely dying, which is what `onclose` alone means.
             * Retrying into it would silently open a BRAND NEW login on the remote host, which looks
             * from here exactly like a reattach and is not one. */
            want = false; _remember(''); cursor = 0;
            if(m.m) _state(m.m); 
            _drop(); _sessions();
          }
        };
        ws.onclose = () => _drop();
        ws.onerror = () => {};             // onclose follows and is where the retry lives
      })();
    }

    /* THE SOCKET WENT AWAY. The shell did not — that is the whole point — so this reconnects rather
     * than reporting a disconnection, as long as we still have a session id to reattach to. */
    function _drop(){
      if(ws){ try{ ws.onclose = null; ws.close(); }catch(_){} ws = null; }
      connected = false;
      if(want && sid){ _chrome(true); return _later(); }
      _chrome(false);
    }

    function _later(){
      if(retryT) return;
      // Backoff, capped: a phone that wakes up on a dead network should not hammer the node, and a
      // circuit that comes back should not take a minute to be noticed. Also stop after a while, or a
      // laptop in a bag reconnects all night.
      if(retry > 8){ want = false; _state('gave up reconnecting — press Connect', 'err'); _chrome(false); return; }
      const wait = Math.min(8000, 500 * Math.pow(1.7, retry++));
      _state('reconnecting in ' + Math.round(wait / 1000) + 's…');
      retryT = setTimeout(() => {
        retryT = null;
        if(!want || !sid) return;
        _open({ resume: sid, host });
      }, wait);
    }

    // Come back to a foregrounded tab immediately rather than waiting out the backoff — a phone
    // unlocking is the single most common way this socket dies.
    function _wake(){
      if(document.visibilityState !== 'visible') return;
      if(!want || !sid || connected || !mounted) return;
      if(retryT){ clearTimeout(retryT); retryT = null; }
      retry = 0; _open({ resume: sid, host });
    }

    function _bye(){
      want = false;
      if(retryT){ clearTimeout(retryT); retryT = null; }
      if(ws){ try{ ws.onclose = null; ws.close(); }catch(_){} ws = null; }
      connected = false; _chrome(false);
    }

    /* DETACH: leave, keep the shell running. The id is KEPT, which is what "Connect" then reattaches
     * to and what makes closing the app harmless. */
    function detach(){
      _send({ t: 'detach' });
      _bye();
      _state(sid ? 'detached — still running' : 'disconnected');
      try{ term && term.write('\r\n\x1b[90m— detached; still running —\x1b[0m\r\n'); }catch(_){}
      _sessions();
    }

    /* KILL: end it. Nothing here expires, so this is the only way a session ends. */
    async function kill(id){
      const target = id || sid;
      if(!target) return;
      const ok = await PC.uiConfirm('End this session? Anything running in it is stopped.',
                                    { ok: 'Kill', danger: true });
      if(!ok) return;
      if(target === sid && ws){ _send({ t: 'close' }); }
      else {
        try{ await authFetch('/api/ssh/sessions/kill', { method: 'POST',
             headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ sid: target }) }); }
        catch(_){}
      }
      if(target === sid){ _bye(); _remember(''); cursor = 0; _state('session ended'); }
      _sessions();
    }

    /* Shells this ACCOUNT still has running. Without this a reload — or a second device — leaves a
     * session alive and unreachable, since the id lives in one browser's localStorage. */
    async function _sessions(){
      const box = $('#tty-sessions'); if(!box) return;
      try{
        const r = await authFetch('/api/ssh/sessions');
        live = ((await r.json()) || {}).sessions || [];
      }catch(_){ live = []; }
      const others = live.filter(x => x.sid !== sid || !connected);
      box.hidden = !others.length;
      if(!others.length){ box.innerHTML = ''; return; }
      box.innerHTML = '<span class="tty-sess-lbl">still running</span>' + others.map(x =>
        `<span class="tty-sess"><b>${enc(x.host || '?')}</b>`
        + `<i>${_ago(x.age)}</i>`
        + `<button data-att="${enc(x.sid)}" data-host="${enc(x.host || '')}">Attach</button>`
        + `<button data-kill="${enc(x.sid)}" class="tty-kill">Kill</button></span>`).join('');
    }

    function _ago(sec){
      sec = Math.max(0, sec | 0);
      if(sec < 90) return sec + 's';
      if(sec < 5400) return Math.round(sec / 60) + 'm';
      return Math.round(sec / 3600) + 'h';
    }

    function _chrome(on){
      const go = $('#tty-go'), stop = $('#tty-stop'), kb = $('#tty-kill'),
            keys = $('#tty-keys'), sel = $('#tty-host');
      if(go) go.classList.toggle('hidden', on);
      if(stop) stop.classList.toggle('hidden', !on);
      if(kb) kb.classList.toggle('hidden', !on);
      if(sel) sel.disabled = on;
      if(keys) keys.hidden = !on;
    }

    function _focus(){
      // xterm takes the keyboard on a desktop; a phone needs a real focused input to raise the soft
      // keyboard, so the hidden catcher is what gets focused there.
      const touch = window.matchMedia && window.matchMedia('(pointer: coarse)').matches;
      if(touch){ const c = $('#tty-catch'); if(c) c.focus(); }
      else if(term) term.focus();
    }

    /* The key bar. `ctrl` is STICKY — press it, then press a letter — because a phone cannot hold two
     * keys at once, and a Ctrl that needs holding is a Ctrl that does not exist. */
    const SEQ = { Escape: '\x1b', Tab: '\t', ArrowUp: '\x1b[A', ArrowDown: '\x1b[B',
                  ArrowRight: '\x1b[C', ArrowLeft: '\x1b[D', Home: '\x1b[H', End: '\x1b[F', '^C': '\x03' };
    function _key(k){
      if(k === 'kbd'){ _focus(); return; }
      if(k === 'ctrl'){ ctrl = !ctrl; const b = $('.tty-ctrl'); if(b) b.classList.toggle('on', ctrl); return; }
      const s = SEQ[k];
      if(s) _send({ t: 'in', d: s });
      _focus();
    }

    function _wire(){
      const sel = $('#tty-host');
      if(sel) sel.innerHTML = hosts.length
        ? hosts.map(h => `<option value="${enc(h.name)}">${enc(h.name)} — ${enc(h.label)}</option>`).join('')
        : '<option value="">no hosts configured</option>';
      { const b = $('#tty-go'); if(b) b.onclick = () => connect(); }
      { const b = $('#tty-stop'); if(b) b.onclick = () => detach(); }
      { const b = $('#tty-kill'); if(b) b.onclick = () => kill(); }
      { const box = $('#tty-sessions'); if(box) box.onclick = (ev) => {
          const a = ev.target.closest('[data-att]'); if(a) return attach(a.dataset.att, a.dataset.host);
          const k = ev.target.closest('[data-kill]'); if(k) return kill(k.dataset.kill); }; }
      { const k = $('#tty-keys'); if(k) k.onclick = (ev) => {
          const b = ev.target.closest('[data-k]'); if(!b) return;
          ev.preventDefault(); _key(b.dataset.k); }; }
      const c = $('#tty-catch');
      if(c){
        // Everything typed into the catcher goes down the wire and the field is emptied again, so it
        // never accumulates a line the shell has already echoed.
        c.oninput = () => {
          let d = c.value; c.value = '';
          if(!d) return;
          if(ctrl){
            const ch = d[0].toLowerCase();
            const code = ch.charCodeAt(0) - 96;
            if(code > 0 && code < 27) d = String.fromCharCode(code) + d.slice(1);
            ctrl = false; const b = $('.tty-ctrl'); if(b) b.classList.remove('on');
          }
          _send({ t: 'in', d });
        };
        c.onkeydown = (ev) => {
          if(ev.key === 'Enter'){ ev.preventDefault(); _send({ t: 'in', d: '\r' }); return; }
          if(ev.key === 'Backspace'){ ev.preventDefault(); _send({ t: 'in', d: '\x7f' }); return; }
          if(SEQ[ev.key]){ ev.preventDefault(); _send({ t: 'in', d: SEQ[ev.key] }); }
        };
      }
      window.addEventListener('resize', _fit);
    }

    async function render(){
      const feed = $('#feed'); if(!feed) return;
      // NOT `PC.VIEW = 'terminal'` — the bridge exposes VIEW as a getter with no setter, so that
      // assignment silently does nothing. renderView() has already set it before dispatching here;
      // every other module only ever READS it.
      //
      // classList.add, NEVER `className =`. Assigning replaces the list, dropping the base `.feed`
      // class that supplies flex:1/overflow-y:auto — and nothing puts it back, so the TIMELINE stops
      // scrolling for the rest of the session after one visit here. Every other module adds its
      // class (meme.js: `feed.classList.add('feed-meme')`); this file was the only one that did not.
      feed.classList.add('feed-term');
      /* TEAR THE OLD ONE DOWN FIRST.
       *
       * Nothing calls unmount(): renderView replaces #feed.innerHTML and never tells a view it is
       * gone. So leaving the Terminal used to leave the socket open — a live login on a remote host
       * with nobody watching it — and coming back found `term` still set while the element it was
       * bound to had been destroyed, so `if(!term && !_mountTerm())` skipped the remount and every
       * byte of output went into a detached node: "connected", over a black screen, until a reload. */
      unmount();
      feed.innerHTML = _shellHtml();
      mounted = feed;
      _state('');
      const ok = await loadHosts();
      _wire();
      document.addEventListener('visibilitychange', _wake);
      if(ok && !hosts.length) _state('no hosts configured — add some in Admin → Nodes', 'err');
      if(ok){
        await _sessions();
        /* COME BACK TO THE SHELL YOU LEFT. The id this device remembers is reattached to on sight —
         * leaving the Terminal and returning, or reopening the app, should land you back in your
         * session rather than at a host picker with your work invisible behind it. Anything the
         * ACCOUNT has running that this device has no id for is offered in the list instead, which
         * is what makes a session started on the laptop resumable on the phone. */
        const prev = _recall();
        if(prev && live.some(x => x.sid === prev)){
          const s0 = live.find(x => x.sid === prev);
          if(s0 && $('#tty-host')) $('#tty-host').value = s0.host || '';
          attach(prev, s0 && s0.host);
        }else if(prev){
          _remember('');           // it is gone; do not offer to reattach to nothing
        }
      }
      // xterm is a separate <script>; if it has not run yet the screen would be a blank box with no
      // explanation, which is the failure mode this app has been bitten by all week.
      if(!XT()) _state('the terminal library did not load', 'err');
    }

    function unmount(){
      // LEAVING THE SCREEN IS DETACHING, never killing: the shell keeps running and the id is kept,
      // so coming back reattaches. This used to close the socket AND that was the end of the session.
      _bye();
      document.removeEventListener('visibilitychange', _wake);
      if(ro){ try{ ro.disconnect(); }catch(_){} ro = null; }
      if(term){ try{ term.dispose(); }catch(_){} term = null; fit = null; }
      if(_fitT){ clearTimeout(_fitT); _fitT = null; }
      window.removeEventListener('resize', _fit);
      mounted = null;
    }

    window.PCTerm = { render, unmount, isOpen: () => !!mounted, connected: () => connected,
                      sessionId: () => sid };
  }
  init();
})();
