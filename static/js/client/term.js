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
 * sessionStorage), reconnects on its own with a backoff, and offers any shell the ACCOUNT still has
 * running when there is no id to hand — the case a reload or a second device leaves you in.
 *
 * Detach and Kill are therefore two different buttons and always will be. One leaves your build
 * running; the other is the only thing that ends a session, since nothing here expires.
 *
 * The socket speaks the frames documented in app/routers/ssh_term.py.
 *
 * AND ON POSTERCHANOS THERE IS NO SOCKET AT ALL. The machine IS the node: going out over a
 * WebSocket, through SSH, back to the computer you are sitting at, to get a shell on it, is absurd
 * — and PosterChanOS runs with no PosterChan server, so there is nothing to SSH to. The desktop
 * hands us a real PTY instead (desktop/localterm.js), and it speaks the SAME frames the server
 * does: `out`, `ready`, `end`, `err`. So everything below — the reconnect, the detach, the key bar,
 * the cursor, the session strip — is shared, and the two transports cannot drift apart. `link` is
 * whichever one is in use.
 */
(function(){
  function init(){
    const PC = window.__PC;
    if(!PC){ return setTimeout(init, 50); }
    const { $, $$, enc, toast, authFetch, publish } = PC;

    const XT = () => window.Terminal;              // xterm's global, once its <script> has run
    /* The desktop's local PTY, when PosterChan IS the desktop. Absent everywhere else, and then
     * every line that mentions it is unreachable rather than broken. */
    const LOCAL = () => window.pcTerm || null;
    /* SHELL HISTORY AS EPHEMERAL NOSTR EVENTS. `termhist.js` holds every judgement — what counts as
     * a command, and the rule that stops it publishing a password — and shipped wired to NOTHING,
     * which is the shape that looks finished from every angle except the one that matters. This is
     * the wiring: keystrokes in, shell output in, publishable lines out, and other devices' lines
     * in from the relay. Absent when the module is not loaded, which is every surface that has no
     * terminal. */
    const HIST = () => window.PCTermHistory || null;
    const LOCAL_HOST = { name: 'local', label: 'this computer', keyed: true, local: true };
    const isLocal = (h) => String(h || '') === 'local';
    const localSid = (id) => 'local:' + id;
    const isLocalSid = (x) => /^local:/.test(String(x || ''));

    /* The host the NEXT mount should open, when something asked for one by name (Ctrl+Enter, the
     * start menu's "Terminal on this computer"). Cleared as soon as it is honoured — it is an
     * instruction for one opening, never a preference. */
    let _want = '';

    let term = null, fit = null, ws = null, link = null, host = null, ro = null;
    let hosts = [], connected = false, ctrl = false, mounted = null;
    let sid = '', cursor = 0, retry = 0, retryT = null, want = false, live = [];
    let findHits = [], findAt = -1;
    /* A mount can cross awaits (host list, session list). Raising the same Terminal window twice
     * starts a second render before the first resumes; without a generation token BOTH continuations
     * mount xterm and attach input, so every physical key is written twice. */
    let renderEpoch = 0;
    // Set on `ready`, fires once the socket has HELD. Only then does a reconnect count as
    // having worked — see the ready handler.
    let provenT = null;

    /* The session id is kept PER BROWSER TAB AND INSTANCE. localStorage is shared by every tab:
     * opening a second Terminal made it recall and attach to the first one's PTY, so both windows
     * wrote into the same shell and each rendered the same echo (the reported "double typing").
     * sessionStorage survives reloads but is isolated between tabs/windows, which is exactly the
     * lifetime of this terminal view. Running shells remain discoverable through the session list. */
    const SKEY = () => 'pc_tty_sid:' + (window.__PC_API_BASE__ || location.origin);
    function _remember(id){
      sid = id || '';
      try{ sid ? sessionStorage.setItem(SKEY(), sid) : sessionStorage.removeItem(SKEY()); }catch(_){}
    }
    function _recall(){ try{ return sessionStorage.getItem(SKEY()) || ''; }catch(_){ return ''; } }

    // Character cell width scales with the screen: a fixed 14px leaves a phone with ~27 columns,
    // which is too narrow for `ls -l`, let alone anything that draws a box.
    /* THE PAGE'S SCALE, WHICH THE TERMINAL DELIBERATELY DOES NOT SHARE. See `.tty-fit` in the
     * stylesheet: the terminal host undoes `body{zoom}` so that xterm's hit-testing and its
     * rendering are measured in one space. Undoing the zoom without undoing its EFFECT would render
     * every glyph 1/zf bigger and fit half as many rows, so the font is scaled by the same factor
     * the host removed. Measured: at 1366 the grid goes 42 -> 19 rows without this and 30 with it. */
    function pageZoom(){
      try{
        const z = parseFloat(getComputedStyle(document.body).zoom);
        return (z > 0 && z <= 1) ? z : 1;
      }catch(_){ return 1; }
    }

    function fontSize(){
      const w = Math.min(window.innerWidth, window.innerHeight * 1.6);
      let px;
      if(w < 420) px = 10;
      else if(w < 700) px = 11;
      else if(w < 1100) px = 13;
      else px = 14;
      /* FLOOR, NOT ROUND, and the difference is columns rather than taste. The host is shrunk by
       * the page's zoom and the font is scaled by the same factor, so the column count should come
       * out unchanged -- but rounding 13 x 0.67 = 8.7 UP to 9 makes every glyph 3% wider than
       * proportional, and a phone-width box lost its thirtieth column: `ls -l` stops lining up.
       * Rounding down cannot cost a column. 8px is the practical floor; below it xterm's glyph
       * cache renders mush. */
      return Math.max(8, Math.floor(px * pageZoom()));
    }

    /* ── SHELL HISTORY ─────────────────────────────────────────────────────────────────────────
     *
     * The commands you ran, on the terminals you have open right now, wherever they are. Ephemeral
     * kinds are forwarded by relays and never written down, so this is not a synced document that
     * would then have to be deleted — it is a conversation between your own open terminals, and it
     * ends when they close.
     *
     * NIP-44 to your OWN key. A relay operator sees an encrypted blob of unknown length; nobody
     * else can read a command, and neither can this instance.
     *
     * NEVER WITHOUT A KEY. Signed out, there is nothing to encrypt to and nothing to sign with, so
     * the collector is not even built — a terminal still works, it simply has no history.
     */
    let hist = null, ring = null, histOff = null, histSeen = null;

    const _histLabel = () => {
      try{
        const n = (navigator.platform || navigator.userAgent || '').slice(0, 24);
        return n || 'a device';
      }catch(_){ return 'a device'; }
    };

    function _histStart(){
      const H = HIST();
      if(!H || hist) return;
      let me = ''; try{ me = (PC.me && PC.me() && PC.me().pubkey) || (window.ME && ME.pubkey) || ''; }catch(_){}
      if(!me) return;
      hist = H.makeCollector();
      ring = H.makeRing();
      histSeen = new Set();
      /* Everything published SINCE NOW. A terminal opened today has no business replaying what a
       * relay happens to have held on to, and an ephemeral event has no business being held on to
       * at all — `since` says which of those two we are relying on. */
      try{
        if(!window.Relay || typeof Relay.subscribe !== 'function') return;
        histOff = Relay.subscribe([{ kinds: [H.KIND], authors: [me], since: Math.floor(Date.now() / 1000) }],
          async (ev) => {
            if(!ev || !ev.id || histSeen.has(ev.id)) return;
            histSeen.add(ev.id);
            let o = null;
            try{ o = JSON.parse(await PC.nip44dec(me, ev.content)); }catch(_){ return; }
            if(!o || !o.c) return;
            ring.add(String(o.c), Number(o.at) || (ev.created_at * 1000), String(o.d || ''));
            _histPaint();
          });
      }catch(_){ histOff = null; }
    }

    function _histStop(){
      if(histOff){ try{ histOff(); }catch(_){} histOff = null; }
      hist = null; ring = null; histSeen = null;
    }

    /** Keystrokes on the way to the shell. Returns nothing; publishing is its own decision. */
    function _histTyped(d){
      if(!hist) return;
      let lines = [];
      try{ lines = hist.typed(d) || []; }catch(_){ return; }
      for(const l of lines){
        if(!l.publish) continue;              // not echoed, too long, or secret-shaped — see termhist.js
        _histPublish(l.line);
      }
    }

    /** Bytes coming back. Echo is the permission — see termhist.js. */
    function _histSaw(d){ if(hist){ try{ hist.saw(d); }catch(_){} } }

    async function _histPublish(line){
      const H = HIST(); if(!H || !ring) return;
      const at = Date.now();
      /* Added LOCALLY first, so ↑ on this device works whether or not a relay is reachable — the
       * publish is how it reaches your OTHER terminals, not how it reaches this one. */
      ring.add(line, at, '');
      _histPaint();
      let me = ''; try{ me = (window.ME && ME.pubkey) || ''; }catch(_){}
      if(!me) return;
      try{
        const ct = await PC.nip44enc(me, JSON.stringify({ c: line, at, d: _histLabel() }));
        const evt = H.historyEvent(ct, at);
        /* `noQueue` — the Outbox replays what it holds when a socket comes back, and an ephemeral
         * event replayed ten minutes late is a command that arrives on another device long after
         * the moment it belonged to. Quiet, because a history line failing to publish is not
         * something to interrupt somebody's terminal about. */
        await publish(evt.kind, evt.content, evt.tags, { quiet: true, noQueue: true });
      }catch(_){}
    }

    /* THE PANEL, and it is a PANEL rather than ↑ on purpose. The shell already owns ↑ — readline's
     * own history, which knows about multi-line commands and searching and is not ours to fight
     * over. What the shell cannot know is what you ran on your OTHER computer, so that is the only
     * thing offered here. A line is TYPED IN, not run: it lands at the prompt for you to read,
     * edit and press Enter on, because a command from another machine may name a path this one
     * does not have. */
    function _histPaint(){
      const box = $('#tty-hist-panel'), btn = $('#tty-hist');
      if(!box || !btn) return;
      const rows = ring ? ring.merged() : [];
      btn.classList.toggle('hidden', !rows.length);
      if(box.hidden) return;
      box.innerHTML = rows.length
        ? rows.slice().reverse().map((r, i) =>
            `<button class="tty-hist-row" data-h="${rows.length - 1 - i}">
               <span class="tty-hist-cmd">${enc(r.line)}</span>
               ${r.from ? `<span class="tty-hist-from">${enc(r.from)}</span>` : ''}</button>`).join('')
        : '<div class="tty-hist-none">Nothing yet. Commands you run appear here, on every terminal '
          + 'you have open.</div>';
      $$('.tty-hist-row', box).forEach(b => b.onclick = () => {
        const r = rows[Number(b.dataset.h)];
        if(r) _send({ t: 'in', d: r.line });     // typed, never run — see above
        box.hidden = true;
        try{ if(term) term.focus(); }catch(_){}
      });
    }

    function _histToggle(){
      const box = $('#tty-hist-panel'); if(!box) return;
      box.hidden = !box.hidden;
      _histPaint();
    }

    function _shellHtml(){
      return `<div class="tty-wrap">
        <div class="tty-bar">
          <select class="input tty-host" id="tty-host" aria-label="Host"></select>
          <button class="btn btn-neon small" id="tty-go" title="Open a separate terminal session">New tab</button>
          <button class="btn btn-ghost small hidden" id="tty-stop" title="Leave it running">Detach</button>
          <button class="btn btn-ghost small hidden tty-kill" id="tty-kill" title="End this session">Kill</button>
          <button class="btn btn-ghost small hidden" id="tty-hist"
                  title="Commands from your terminals">History</button>
          <button class="btn btn-ghost small" id="tty-find" title="Find in terminal (Ctrl+Shift+F)">Find</button>
          <span class="tty-state" id="tty-state"></span>
        </div>
        <div class="tty-find" id="tty-find-panel" hidden>
          <input class="input" id="tty-find-input" type="search" autocomplete="off"
                 spellcheck="false" placeholder="Find in terminal" aria-label="Find in terminal">
          <button class="btn btn-ghost small" id="tty-find-prev" title="Previous match">↑</button>
          <button class="btn btn-ghost small" id="tty-find-next" title="Next match">↓</button>
          <span id="tty-find-count" aria-live="polite">0 matches</span>
          <button class="btn btn-ghost small" id="tty-find-close" title="Close find" aria-label="Close find">×</button>
        </div>
        <div class="tty-hist" id="tty-hist-panel" hidden></div>
        <div class="tty-sessions tty-tabs" id="tty-sessions" aria-label="Terminal tabs"></div>
        <div class="tty-screen"><div class="tty-fit" id="tty-screen"></div></div>
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

    /* THIS MACHINE FIRST, when there is one. On PosterChanOS it is the only host anybody wants, and
     * it is the one the server cannot offer — a node's SSH host list is other computers. */
    function _withLocal(list){
      const rest = (list || []).filter(h => h && h.name !== 'local');
      return LOCAL() ? [LOCAL_HOST].concat(rest) : rest;
    }

    async function loadHosts(){
      /* WITH NO SERVER THERE IS STILL A TERMINAL. PosterChanOS runs with no instance configured, and
       * every line below this asks a server something. Answering "the SSH terminal is switched off"
       * on the machine whose own shell is sitting right there would be absurd — so the local host
       * stands on its own, and the server's list is added to it if there is a server. */
      if(!(window.__PC_API_BASE__ === undefined ? true : window.__PC_API_BASE__) && LOCAL()){
        hosts = _withLocal([]); return true;
      }
      try{
        // The bundled apps authenticate with a BEARER, not a cookie (they are cross-origin to the
        // instance), and that token is minted lazily. Without this the first visit to the Terminal
        // is a 401 that reads as "you are not allowed" — see the same call in every other authed
        // screen. It is a no-op once the session exists.
        try{ await PC.ensureAiSession(); }catch(_){}
        const r = await authFetch('/api/ssh/hosts');
        if(r.status === 403){
          hosts = _withLocal([]);
          if(!hosts.length){ _state('the SSH terminal is switched off, or you are not on its list', 'err'); return false; }
          return true;
        }
        const d = await r.json();
        hosts = _withLocal((d && d.hosts) || []);
        if(d && d.available === false && !LOCAL()){
          _state('this node has no SSH library installed — run install.sh', 'err');
          return false;
        }
        return true;
      }catch(_){
        /* A node that cannot be reached is not a machine without a shell. */
        hosts = _withLocal([]);
        if(hosts.length) return true;
        _state('could not reach the server', 'err'); return false;
      }
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

      /* FIND LIVES IN THE RENDERER, not in the shell. Sending Ctrl+F into readline searches command
       * history; Ctrl+Shift+F searches everything xterm still holds, including program output and
       * the 5,000-line scrollback. The buffer API is public xterm API, so this works in the web,
       * Android and desktop bundles without another CDN or a version-sensitive private property. */
      try{ term.attachCustomKeyEventHandler((ev) => {
        if(ev.type === 'keydown' && (ev.ctrlKey || ev.metaKey) && ev.shiftKey
            && String(ev.key).toLowerCase() === 'f'){
          ev.preventDefault(); _findOpen(); return false;
        }
        return true;
      }); }catch(_){}

      /* HIGHLIGHT COPIES, RIGHT-CLICK PASTES -- the two things every terminal has done for thirty
       * years and this one did not. Without them a command is copied out of a browser and retyped
       * by hand.
       *
       * Copy through `pcClip.write` when the app offers it, because there is no working web
       * clipboard here: `navigator.clipboard` is absent outside a secure context and
       * execCommand('copy') is refused, which is the whole reason that bridge exists. The web build
       * falls back to the client's own copyValue, which knows the same trick.
       *
       * NO TOAST ON COPY. It fires on every drag of the mouse, and a notification per selection is
       * noise -- the selection highlighting IS the feedback. */
      try{
        term.onSelectionChange(() => {
          let sel = '';
          try{ sel = term.getSelection() || ''; }catch(_){ sel = ''; }
          if(!sel) return;
          try{ if(window.pcClip && window.pcClip.write) return void window.pcClip.write(sel); }catch(_){}
          try{ PC && PC.copyValue && PC.copyValue(sel, '', ''); }catch(_){}
        });
      }catch(_){}

      /* Paste on the RELEASE of the right button, and prevent the browser menu. `term.paste()` is
       * xterm's own, which sends the text through the same path as typing -- including bracketed
       * paste, so a shell that supports it does not execute a multi-line paste line by line. */
      try{
        box.addEventListener('contextmenu', async (e) => {
          e.preventDefault();
          let text = '';
          try{ if(window.pcClipRead && window.pcClipRead.read) text = await window.pcClipRead.read(); }catch(_){}
          if(!text){
            try{ text = await navigator.clipboard.readText(); }catch(_){ text = ''; }
          }
          if(!text){ try{ PC && PC.toast && PC.toast('nothing to paste'); }catch(_){} return; }
          try{ term.paste(text); }catch(_){}
        });
      }catch(_){}
      term.onData(d => { _histTyped(d); _send({ t: 'in', d }); });
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
        /* FOCUS MAY CHANGE Z-ORDER, NEVER TERMINAL GEOMETRY. PosterChanOS parks/moves the shared
         * feed while another window takes focus, and that transition fires ResizeObserver with a
         * temporary content size. Fitting xterm there sends SIGWINCH to the PTY and makes the
         * background terminal visibly rewrap. A deliberate edge resize focuses its frame before
         * changing the box, so ignoring background measurements loses no real resize. */
        try{
          const frame = box && box.closest && box.closest('.osw');
          if(frame && !frame.classList.contains('focused')) return;
        }catch(_){}
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

    /* ONE WAY OUT, whichever transport is carrying it. Everything in this file that talks to a
     * shell goes through here — the key bar, the catcher, the resize, detach and close — so adding
     * the local PTY did not mean auditing thirty call sites for "is this a socket". */
    function _send(o){ try{ if(link && link.send) link.send(o); }catch(_){} }

    /* Tear the current transport down without it reporting itself as a drop. Attaching to another
     * session while one is connected is reachable from the strip, and leaving the old one alive
     * means its close lands in the NEW session's state a moment later — reported as a terminal that
     * connects and then immediately says it is reconnecting. */
    function _unlink(){
      if(link){ try{ link.close(); }catch(_){} link = null; }
      ws = null;
    }

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
      // password into a box that did not need one — and this machine's own shell never needs one:
      // you are already logged in to it, which is how you are looking at this.
      let password = '';
      if(!h.keyed && !h.local){
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
      // TEAR DOWN ANY EXISTING TRANSPORT FIRST. Attaching to another session from the strip is
      // reachable while one is already connected, and leaving the old one alive means its close
      // lands in the NEW session's state a moment later — reported as a terminal that connects and
      // then immediately says it is reconnecting.
      _unlink();
      if(retryT){ clearTimeout(retryT); retryT = null; }
      connected = false;
      /* THIS MACHINE'S OWN SHELL takes the local path and never opens a socket. Decided from the
       * frame rather than from `host`, because a reattach carries only the session id — and a local
       * session id says which transport it belongs to, which is exactly what it is for. */
      if(isLocal(frame.host) || isLocalSid(frame.resume)) return _openLocal(frame);
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
          _frame(m);
        };
        link = {
          kind: 'ws',
          send(o){ try{ if(ws && ws.readyState === 1) ws.send(JSON.stringify(o)); }catch(_){} },
          close(){ if(ws){ try{ ws.onclose = null; ws.onmessage = null; ws.close(); }catch(_){} } },
        };
        ws.onclose = () => _drop();
        ws.onerror = () => {};             // onclose follows and is where the retry lives
      })();
    }

    /* THE LOCAL PTY. A shell on THIS machine, through the desktop bridge — no socket, no server, no
     * SSH. It emits the server's own frames into `_frame`, so nothing downstream knows the
     * difference, and the session survives this page being reloaded exactly the way a server-side
     * one does: the PTY lives in the desktop process, and `backlog` is what a fresh page redraws
     * from. That matters more here than over SSH, because the WebView holding this page is the half
     * Android and Chromium take away under memory pressure. */
    function _openLocal(frame){
      const T = LOCAL();
      if(!T){ _frame({ t: 'err', m: 'this build has no shell of its own' }); return; }
      let id = String(frame.resume || '').replace(/^local:/, '');
      let stop = null, gone = false;
      link = {
        kind: 'local',
        send(o){
          if(gone || !id) return;
          if(o.t === 'in') T.write(id, o.d);
          else if(o.t === 'size') T.resize(id, o.cols, o.rows);
          /* `detach` deliberately does nothing here — that is its whole meaning. The shell lives in
           * the desktop process, so leaving the screen must leave it running, and `close` is the
           * only thing that ends it. Identical to the server side. */
          else if(o.t === 'close'){ gone = true; T.close(id); }
        },
        close(){ gone = true; if(stop){ try{ stop(); }catch(_){} stop = null; } },
      };
      (async () => {
        if(!term && !_mountTerm()){ _state('the terminal could not start', 'err'); want = false; return; }
        if(!want) return;
        _state(frame.resume ? 'reattaching…' : 'starting a shell…');
        try{
          let b = null;
          let fresh = false;
          if(id){
            b = await T.backlog(id, Number(cursor) || 0);
            /* A remembered id can name a shell that died while the app was shut. Saying so is the
             * difference between "your work is gone" and a silent new shell in the same window
             * pretending to be the old one. */
            if(!b) id = '';
          }
          if(!id){
            const s0 = await T.start({ cols: (term && term.cols) || 80, rows: (term && term.rows) || 24 });
            id = String(s0 && s0.id || '');
            if(!id) throw new Error('the shell would not start');
            fresh = true;
            /* START ALREADY PRODUCED OUTPUT. localterm creates Posterfetch synchronously and a fast
             * login shell can print its prompt before this renderer has subscribed. Setting b=null
             * discarded both, leaving a real new PTY as a blank screen that looked exactly like the
             * old tab was still active. Read from cursor zero before attaching; anything produced
             * after this read is delivered by the subscription installed immediately below. */
            b = await T.backlog(id, 0);
          }
          if(gone) return;
          stop = T.onData((ev) => { if(String(ev.id) === id) _frame(ev); });
          await T.attach(id);
          _frame({ t: 'ready', sid: localSid(id), host: 'local', resumed: !fresh });
          if(b){
            /* Redraw what was missed, and say so when the gap is bigger than what is still kept —
             * a fragment presented as the whole history is how scrollback loses its middle. */
            if(b.truncated) term.write('\r\n\x1b[90m— earlier output is no longer kept —\x1b[0m\r\n');
            if(b.d) _frame({ t: 'out', d: b.d, seq: b.seq });
            if(!b.alive) _frame({ t: 'end', m: 'that shell has exited' });
          }
        }catch(e){
          _frame({ t: 'err', m: String((e && e.message) || e) });
        }
      })();
    }

    /* WHAT A SHELL SAID, from either transport. The local PTY emits the same four things the server
     * does, so this is the only place that has to understand them. */
    function _frame(m){
      if(!m) return;
      if(!term) return;
          if(m.t === 'out'){
            term.write(m.d);
            _histSaw(m.d);
            // The CURSOR is what a reconnect resumes from, so it advances only for bytes that reached
            // the screen. Trusting a locally counted length instead would drift the first time a
            // multi-byte character straddled a frame.
            if(typeof m.seq === 'number') cursor = m.seq;
            return;
          }
          if(m.t === 'ready'){
            connected = true;
            /* RESETTING THE COUNTER HERE IS WHAT MADE A BROKEN TERMINAL SILENT.
             * `ready` says the server accepted us, not that the connection WORKS — and the failure
             * that hid for a whole afternoon happened immediately after it, when the replay frame
             * killed the relay. Every cycle therefore went: reattach, ready, retry=0, socket dies,
             * reconnect — so the backoff never climbed, `gave up reconnecting` below was
             * unreachable, and clicking Attach did nothing at all, for ever, with nothing on screen
             * and nothing in the console. A connection has to HOLD to count as one. */
            if(provenT) clearTimeout(provenT);
            provenT = setTimeout(function(){ provenT = null; retry = 0; }, 5000);
            _remember(m.sid || '');
            if(m.host) host = m.host;
            _state((m.resumed ? 'reattached to ' : 'connected to ') + host, 'ok');
            _chrome(true); _fit(); _focus();
            /* A NEW PTY IS A NEW TAB. Starting one used to update `sid` but never repaint the tab
             * strip, so the shell existed while the only visible tab was still the previous one.
             * The next press appeared to do nothing useful and switching was impossible until a
             * full render happened. Refresh after the ready frame—the first point at which the
             * server/local bridge has assigned the distinct session id. */
            _sessions();
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
            /* SAY SOMETHING, ALWAYS. `closed_reason` is empty for most ordinary endings, so
             * `if(m.m)` meant the session tore itself down in total silence — the screen simply
             * stopped being a terminal, which reads as "it did nothing". */
            const why = m.m || 'the session ended';
            _state(why);
            try{ term.write('\r\n\x1b[33m' + why + '\x1b[0m\r\n'); }catch(_){}
            _drop(); _sessions();
          }
    }

    /* THE SOCKET WENT AWAY. The shell did not — that is the whole point — so this reconnects rather
     * than reporting a disconnection, as long as we still have a session id to reattach to. */
    function _drop(){
      _unlink();
      // The socket is gone, so it never held: cancelling this is what lets `retry` keep climbing.
      if(provenT){ clearTimeout(provenT); provenT = null; }
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
      if(provenT){ clearTimeout(provenT); provenT = null; }
      _unlink();
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
      if(isLocalSid(target)){
        /* No server to ask: the shell is a process in the desktop, and this is the only thing that
         * ends it. Ending the one we are attached to goes through the link so the session's own
         * teardown runs; any other is ended by id. */
        if(target === sid && link) _send({ t: 'close' });
        else { try{ LOCAL() && LOCAL().close(String(target).replace(/^local:/, '')); }catch(_){} }
      }
      else if(target === sid && link){ _send({ t: 'close' }); }
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
      live = [];
      /* THIS MACHINE'S OWN SHELLS. They are listed the same way and for the same reason: a reload,
       * or closing the window, leaves one running with nothing pointing at it. */
      if(LOCAL()){
        try{
          const mine = await LOCAL().list();
          for(const x of (mine || []))
            live.push({ sid: localSid(x.id), host: 'local', age: Math.round((x.idle || 0)), alive: x.alive });
        }catch(_){}
      }
      try{
        const r = await authFetch('/api/ssh/sessions');
        live = live.concat(((await r.json()) || {}).sessions || []);
      }catch(_){}
      /* THESE ARE TABS, not a recovery list. Every row names a distinct PTY; selecting one tears
       * down only the viewing transport and attaches this xterm to that PTY. The processes and
       * input streams never merge. Keeping the active shell in the strip is what makes the model
       * visible: two terminals look like two tabs, instead of one terminal plus an obscure
       * “still running” diagnostic people quite reasonably did not recognize as tab support. */
      const tabs = live.slice();
      if(sid && !tabs.some(x => x.sid === sid))
        tabs.unshift({ sid, host:host || 'local', age:0, alive:true });
      box.hidden = false;
      box.innerHTML = '<span class="tty-sess-lbl">tabs</span>' + tabs.map((x, n) =>
        `<span class="tty-sess tty-tab${x.sid === sid ? ' active' : ''}" data-tab="${enc(x.sid)}"
               data-host="${enc(x.host || '')}"><b>${enc(x.host || 'terminal')} ${n + 1}</b>`
        + `<i>${_ago(x.age)}</i>`
        + `<button data-kill="${enc(x.sid)}" class="tty-kill" title="Close tab"
                   aria-label="Close terminal tab">×</button></span>`).join('')
        + '<button class="tty-tab-new" id="tty-tab-new" title="New terminal tab">+</button>';
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
      /* New tab is useful WHILE a terminal is connected. Hiding it at precisely that moment left
       * only the tiny `+` in the session strip and made the primary control behave like Connect,
       * not tabs. Each press calls connect(), which first detaches this viewer and then creates a
       * distinct PTY; the old PTY remains alive and selectable in the strip. */
      if(go) go.classList.remove('hidden');
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

    function _findScan(step){
      const input = $('#tty-find-input'), count = $('#tty-find-count');
      const q = String(input && input.value || '');
      findHits = []; findAt = -1;
      if(term && q){
        try{
          const b = term.buffer.active, needle = q.toLocaleLowerCase();
          for(let row = 0; row < b.length; row++){
            const line = b.getLine(row); if(!line) continue;
            const text = line.translateToString(true), hay = text.toLocaleLowerCase();
            let col = 0;
            while((col = hay.indexOf(needle, col)) >= 0){
              findHits.push({ row, col, len:q.length }); col += Math.max(1, needle.length);
            }
          }
        }catch(_){ findHits = []; }
      }
      if(findHits.length){ findAt = step < 0 ? findHits.length - 1 : 0; _findShow(); }
      else {
        try{ term && term.clearSelection(); }catch(_){}
        if(count) count.textContent = q ? 'No matches' : '0 matches';
      }
    }

    function _findShow(){
      if(!term || !findHits.length) return;
      findAt = (findAt + findHits.length) % findHits.length;
      const h = findHits[findAt], count = $('#tty-find-count');
      try{ term.select(h.col, h.row, h.len); term.scrollToLine(h.row); }catch(_){}
      if(count) count.textContent = (findAt + 1) + ' of ' + findHits.length;
    }

    function _findMove(step){
      if(!findHits.length) return _findScan(step);
      findAt += step; _findShow();
    }

    function _findOpen(){
      const panel = $('#tty-find-panel'), input = $('#tty-find-input');
      if(!panel || !input) return;
      panel.hidden = false;
      requestAnimationFrame(() => { input.focus(); input.select(); });
    }

    function _findClose(){
      const panel = $('#tty-find-panel'); if(panel) panel.hidden = true;
      try{ term && term.clearSelection(); }catch(_){}
      findHits = []; findAt = -1; _focus();
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
      { const b = $('#tty-find'); if(b) b.onclick = _findOpen; }
      { const b = $('#tty-find-prev'); if(b) b.onclick = () => _findMove(-1); }
      { const b = $('#tty-find-next'); if(b) b.onclick = () => _findMove(1); }
      { const b = $('#tty-find-close'); if(b) b.onclick = _findClose; }
      { const input = $('#tty-find-input'); if(input){
          input.oninput = () => _findScan(1);
          input.onkeydown = (ev) => {
            if(ev.key === 'Escape'){ ev.preventDefault(); _findClose(); }
            else if(ev.key === 'Enter'){ ev.preventDefault(); _findMove(ev.shiftKey ? -1 : 1); }
          };
        } }
      { const box = $('#tty-sessions'); if(box) box.onclick = (ev) => {
          const k = ev.target.closest('[data-kill]'); if(k){ ev.stopPropagation(); return kill(k.dataset.kill); }
          const add = ev.target.closest('#tty-tab-new'); if(add) return connect();
          const a = ev.target.closest('[data-tab]');
          if(a && a.dataset.tab !== sid) return attach(a.dataset.tab, a.dataset.host); }; }
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
      const epoch = ++renderEpoch;
      feed.innerHTML = _shellHtml();
      mounted = feed;
      _state('');
      /* The collector is built here rather than on the first keystroke: it has to be watching the
       * shell's OUTPUT from the beginning, because echo is what gives it permission to publish a
       * line and a collector armed halfway through one has seen no echo for it. */
      _histStart();
      _histPaint();
      const ok = await loadHosts();
      if(epoch !== renderEpoch || mounted !== feed) return;
      _wire();
      { const hb = $('#tty-hist'); if(hb) hb.onclick = _histToggle; }
      document.addEventListener('visibilitychange', _wake);
      if(ok && !hosts.length) _state('no hosts configured — add some in Admin → Nodes', 'err');
      if(ok){
        await _sessions();
        if(epoch !== renderEpoch || mounted !== feed) return;
        /* COME BACK TO THE SHELL YOU LEFT. The id this device remembers is reattached to on sight —
         * leaving the Terminal and returning, or reopening the app, should land you back in your
         * session rather than at a host picker with your work invisible behind it. Anything the
         * ACCOUNT has running that this device has no id for is offered in the list instead, which
         * is what makes a session started on the laptop resumable on the phone. */
        /* A SHELL ON THIS MACHINE, ASKED FOR BY NAME. Ctrl+Enter on PosterChanOS means "give me a
         * terminal here" the way $mod+Return does in sway, and the one thing it must not do is
         * reattach to somebody's SSH session on another computer — which is exactly what happened,
         * because "come back to the shell you left" is remembered per DEVICE and the shell this
         * device last left was `server1`. Measured on the test machine: pressing the Terminal icon
         * reattached to `verita84@server1.lan` over the network while the local PTY sat unused, and
         * that is the whole of "still no terminal app for the laptop, all I see is our remote
         * terminal".
         *
         * So the keystroke NAMES the machine, and naming it wins over the memory. Clicking the
         * icon still reattaches, because that is the right answer for a session you left running. */
        if(_want === 'local' && LOCAL() && hosts.some(h => h && h.local)){
          _want = '';
          if($('#tty-host')) $('#tty-host').value = 'local';
          connect();
          if(!XT()) _state('the terminal library did not load', 'err');
          return;
        }
        _want = '';
        const prev = _recall();
        if(prev && live.some(x => x.sid === prev)){
          const s0 = live.find(x => x.sid === prev);
          if(s0 && $('#tty-host')) $('#tty-host').value = s0.host || '';
          attach(prev, s0 && s0.host);
        }else{
          if(prev) _remember('');   // it is gone; do not offer to reattach to nothing
          /* AN EMPTY TERMINAL STARTS A SESSION. Previously this happened only when `local` was the
           * sole configured host, so adding one saved SSH server made the desktop Terminal open to
           * an inert picker. `loadHosts` deliberately puts this computer first on PosterChanOS;
           * elsewhere the person's first configured host remains the selected default. Existing
           * sessions are never replaced — they remain as tabs and a remembered one was attached
           * above. */
          if(!live.length && hosts.length) connect();
        }
      }
      // xterm is a separate <script>; if it has not run yet the screen would be a blank box with no
      // explanation, which is the failure mode this app has been bitten by all week.
      if(!XT()) _state('the terminal library did not load', 'err');
    }

    function unmount(){
      ++renderEpoch;                 // every pending continuation now belongs to a dead screen
      // LEAVING THE SCREEN IS DETACHING, never killing: the shell keeps running and the id is kept,
      // so coming back reattaches. This used to close the socket AND that was the end of the session.
      _bye();
      document.removeEventListener('visibilitychange', _wake);
      if(ro){ try{ ro.disconnect(); }catch(_){} ro = null; }
      if(term){ try{ term.dispose(); }catch(_){} term = null; fit = null; }
      if(_fitT){ clearTimeout(_fitT); _fitT = null; }
      window.removeEventListener('resize', _fit);
      /* The relay subscription goes with the screen. Left running it decrypts other devices'
       * commands into a ring nothing will ever draw, for the rest of the session. */
      _histStop();
      mounted = null;
    }

    /* Open the terminal ON THIS COMPUTER. Sets the intent and lets whoever is routing views do the
     * opening, so there is one path into this screen rather than a second mount that has to know
     * about windows, the feed and the desktop. Answers whether a local PTY exists at all, so a
     * caller can say so instead of opening a terminal that will offer a host picker. */
    function openLocal(){
      if(!LOCAL()) return false;
      _want = 'local';
      /* ALREADY ON THE SCREEN is the case that would otherwise do nothing: the view is mounted, so
       * nothing re-runs the block that reads `_want`. Connect here instead. */
      if(mounted && hosts.some(h => h && h.local)){
        _want = '';
        if($('#tty-host')) $('#tty-host').value = 'local';
        connect();
      }
      return true;
    }

    window.PCTerm = { render, unmount, isOpen: () => !!mounted, connected: () => connected,
                      openLocal, sessionId: () => sid };
  }
  init();
})();
