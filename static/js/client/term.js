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
          <button class="btn btn-ghost small hidden" id="tty-stop">Disconnect</button>
          <span class="tty-state" id="tty-state"></span>
        </div>
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

    let _fitT = null;
    function _fit(){
      if(_fitT) clearTimeout(_fitT);
      // Coalesced: an on-screen keyboard opening fires a burst of resizes, and each one is a reflow
      // of the whole grid plus a frame on the wire.
      _fitT = setTimeout(() => {
        _fitT = null;
        if(!term) return;
        try{ term.options.fontSize = fontSize(); }catch(_){}
        try{ if(fit) fit.fit(); }catch(_){}
        _send({ t: 'size', cols: term.cols, rows: term.rows });
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
      try{ await PC.ensureAiSession(); }catch(_){}   // the socket's token, same reason as above
      if(!term && !_mountTerm()){ _state('the terminal could not start', 'err'); return; }
      term.reset();
      _state('connecting…');
      try{ ws = new WebSocket(_wsUrl()); }
      catch(_){ _state('could not open a connection', 'err'); return; }
      ws.onopen = () => {
        /* The socket authenticates with the SAME bearer the rest of the app uses. It is sent in the
         * open frame rather than the URL: a query string lands in every proxy log between here and
         * the server, and this one is a credential. */
        _send({ t: 'open', host, password, cols: term.cols, rows: term.rows,
                token: (window.__PC_TOKEN__ || '') });
        password = '';
      };
      ws.onmessage = (ev) => {
        let m; try{ m = JSON.parse(ev.data); }catch(_){ return; }
        if(m.t === 'out'){ term.write(m.d); return; }
        if(m.t === 'ready'){ connected = true; _state('connected to ' + host, 'ok'); _chrome(true); _focus(); return; }
        if(m.t === 'err'){ term.write('\r\n\x1b[31m' + m.m + '\x1b[0m\r\n'); _state(m.m, 'err'); return; }
        if(m.t === 'end'){ _bye(); }
      };
      ws.onclose = () => _bye();
      ws.onerror = () => _state('the connection failed', 'err');
    }

    function _bye(){
      if(connected) { try{ term && term.write('\r\n\x1b[90m— disconnected —\x1b[0m\r\n'); }catch(_){} }
      connected = false; _chrome(false);
      if(ws){ try{ ws.close(); }catch(_){} ws = null; }
    }
    function disconnect(){ _send({ t: 'close' }); _bye(); _state('disconnected'); }

    function _chrome(on){
      const go = $('#tty-go'), stop = $('#tty-stop'), keys = $('#tty-keys'), sel = $('#tty-host');
      if(go) go.classList.toggle('hidden', on);
      if(stop) stop.classList.toggle('hidden', !on);
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
      { const b = $('#tty-stop'); if(b) b.onclick = () => disconnect(); }
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
      if(ok && !hosts.length) _state('no hosts configured — add some in Admin → Nodes', 'err');
      // xterm is a separate <script>; if it has not run yet the screen would be a blank box with no
      // explanation, which is the failure mode this app has been bitten by all week.
      if(!XT()) _state('the terminal library did not load', 'err');
    }

    function unmount(){
      _bye();
      if(ro){ try{ ro.disconnect(); }catch(_){} ro = null; }
      if(term){ try{ term.dispose(); }catch(_){} term = null; fit = null; }
      if(_fitT){ clearTimeout(_fitT); _fitT = null; }
      window.removeEventListener('resize', _fit);
      mounted = null;
    }

    window.PCTerm = { render, unmount, isOpen: () => !!mounted, connected: () => connected };
  }
  init();
})();
