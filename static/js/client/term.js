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
    /* WHICH TAB THIS IS, on the host. It is half of the remote tmux session's name, so it is what
     * decides whether a new tab is a new shell or a second view of the one you are already in —
     * see _mux_name in app/services/ssh_service.py. Carried on every open frame, including a
     * resume, because a resume whose session has gone falls through to opening one. */
    let label = '';
    let hosts = [], connected = false, ctrl = false, mounted = null;
    let sid = '', cursor = 0, retry = 0, retryT = null, want = false, live = [];
    let findHits = [], findAt = -1;
    let followBottom = true, scrollingByUs = false;
    /* A monitor handoff crosses renderers. Tabs are PTY session identities, never numeric indexes:
     * the local/server session list may arrive in a different order on the destination monitor. */
    let handoffOrder = [], handoffScroll = null;
    const tabScroll = new Map();             // session id -> {pinned, aboveBottom}
    /* xterm's write callback means that its parser consumed the bytes; it does NOT mean Chromium
     * has finished laying out the enlarged scrollback element.  A large reattach measured in the
     * packaged desktop landed at scrollTop 2224 with an actual maximum of 3824 even though the
     * callback had called scrollToBottom().  Keep one generation of post-layout pinning alive until
     * the newest write has crossed two paint frames and a short layout quiescence.  Older callbacks
     * may never release the guard underneath a newer write. */
    let bottomPinEpoch = 0, bottomPinT = null, handoffRestoreT = null;

    function _stopFollowing(){
      /* A write may keep `scrollingByUs` true across two paints while xterm grows its viewport.
       * A real wheel/swipe/PageUp during that interval must win immediately; otherwise onScroll
       * mistakes the person's movement for xterm's layout movement and the next settle callback
       * yanks them back to the prompt. Invalidating the generation also makes every already queued
       * RAF/timer harmless. Scrolling back to baseY re-enables following through onScroll below. */
      followBottom = false;
      scrollingByUs = false;
      ++bottomPinEpoch;
      if(bottomPinT){ clearTimeout(bottomPinT); bottomPinT = null; }
    }

    /* A tap focuses the terminal and, on Android, opens the keyboard. It is not a scroll choice.
     * Keep this decision pure so touch/wheel surfaces cannot drift: only movement (or an upward
     * wheel) means the reader deliberately left current output. */
    function _scrollsAway(kind, delta){
      return kind==='touchmove' || (kind==='wheel' && Number(delta)<0);
    }

    function _pinBottomAfterLayout(){
      if(!term) return;
      const mine = ++bottomPinEpoch;
      scrollingByUs = true;
      const settle = () => {
        if(mine !== bottomPinEpoch || !term) return;
        try{ term.scrollToBottom(); }catch(_){}
        if(bottomPinT) clearTimeout(bottomPinT);
        bottomPinT = setTimeout(() => {
          if(mine !== bottomPinEpoch || !term) return;
          try{ term.scrollToBottom(); }catch(_){}
          scrollingByUs = false;
          bottomPinT = null;
        }, 80);
      };
      /* Two frames are intentional: the first commits xterm's canvas/row update and the second sees
       * the resulting viewport height. Backgrounded renderers may not deliver RAF promptly, so the
       * timer is also a bounded fallback rather than leaving user scrolling disabled forever. */
      try{ requestAnimationFrame(() => requestAnimationFrame(settle)); }
      catch(_){ settle(); }
      setTimeout(settle, 120);
    }
    /* A mount can cross awaits (host list, session list). Raising the same Terminal window twice
     * starts a second render before the first resumes; without a generation token BOTH continuations
     * mount xterm and attach input, so every physical key is written twice. */
    let renderEpoch = 0;
    /* Every connect/attach owns a generation. Authentication and PTY startup both cross awaits;
     * without this, a slower OLD tab switch can finish after the newest one and replace its link,
     * which is the exact shape of tabs sharing input or snapping back to the previous shell. */
    let openEpoch = 0;
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

    /* The host picker, repainted from `hosts` without disturbing a live session. Split out of
     * _wire so a server list that arrives LATE can be shown without re-binding the whole screen. */
    function _paintHosts(){
      const sel = $('#tty-host'); if(!sel) return;
      const keep = sel.value;
      sel.innerHTML = hosts.length
        ? hosts.map(h => `<option value="${enc(h.name)}">${enc(h.name)} — ${enc(h.label)}</option>`).join('')
        : '<option value="">no hosts configured</option>';
      if(keep && hosts.some(h => h.name === keep)) sel.value = keep;
    }

    /* THE OTHER HOSTS, FETCHED BEHIND THE LOCAL ONE. Never awaited by anything that opens a shell —
     * see loadHosts. Bounded, because the failure this exists for is a request that never answers. */
    let _hostsAsked = false;
    async function _hostsRefresh(){
      if(_hostsAsked) return; _hostsAsked = true;
      try{
        await _bounded(PC.ensureAiSession && PC.ensureAiSession(), 6000);
        const r = await _bounded(authFetch('/api/ssh/hosts'), 8000);
        if(!r || r.status === 403) return;
        const d = await r.json();
        const more = (d && d.hosts) || [];
        if(!more.length) return;
        hosts = _withLocal(more);
        _paintHosts();
      }catch(_){ /* a node that cannot be reached is not a machine without a shell */ }
    }

    /* A PROMISE THAT CANNOT HANG FOR EVER. `ensureAiSession` can be waiting on a SIGNER, and a
     * signer is a phone that may be asleep — there is no answer coming and no error either. */
    function _bounded(p, ms){
      if(!p || typeof p.then !== 'function') return Promise.resolve(p);
      return Promise.race([p, new Promise((_res, rej) =>
        setTimeout(() => rej(new Error('timed out')), ms || 8000))]);
    }

    async function loadHosts(){
      /* WITH NO SERVER THERE IS STILL A TERMINAL. PosterChanOS runs with no instance configured, and
       * every line below this asks a server something. Answering "the SSH terminal is switched off"
       * on the machine whose own shell is sitting right there would be absurd — so the local host
       * stands on its own, and the server's list is added to it if there is a server. */
      if(!(window.__PC_API_BASE__ === undefined ? true : window.__PC_API_BASE__) && LOCAL()){
        hosts = _withLocal([]); return true;
      }
      /* A MACHINE THAT IS ITS OWN SHELL NEVER WAITS FOR A SERVER TO OPEN IT.
       *
       * Everything below asks the instance something, and the two things it asks are the two most
       * likely to be broken at the moment somebody wants a terminal: `ensureAiSession` can be
       * waiting on a SIGNER (a phone that may be asleep — no answer coming, and no error either)
       * and the fetch can be waiting on a network that is down. Neither FAILS; both HANG, and
       * render() awaits this before it opens anything, so the local shell — a PTY already running
       * on this very machine, needing no key and no network — never appeared at all.
       *
       * On PosterChanOS the terminal is how somebody fixes a broken machine. It must not depend on
       * the parts that are broken. So the local host stands alone immediately, and the rest of the
       * list arrives behind it if it ever does. */
      if(LOCAL()){ hosts = _withLocal([]); _hostsRefresh(); return true; }
      try{
        // The bundled apps authenticate with a BEARER, not a cookie (they are cross-origin to the
        // instance), and that token is minted lazily. Without this the first visit to the Terminal
        // is a 401 that reads as "you are not allowed" — see the same call in every other authed
        // screen. It is a no-op once the session exists.
        try{ await _bounded(PC.ensureAiSession && PC.ensureAiSession(), 6000); }catch(_){}
        const r = await _bounded(authFetch('/api/ssh/hosts'), 8000);
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
      /* Follow a live prompt until the person deliberately scrolls up. Reopening/attaching always
       * starts at the current prompt; after that, reading old output is never yanked back down. */
      try{ term.onScroll((y) => {
        if(scrollingByUs)return;
        const b=term.buffer&&term.buffer.active;followBottom=!!b && y>=b.baseY;
      }); }catch(_){}

      /* xterm's onScroll has no user/programmatic source flag. Observe the input that can only mean
       * "I am reading scrollback" before xterm handles it, so it can cancel an in-flight replay or
       * live-output pin even when that pin currently owns the onScroll guard. */
      box.addEventListener('wheel', ev => { if(_scrollsAway('wheel',ev.deltaY)) _stopFollowing(); }, {passive:true});
      box.addEventListener('touchmove', ev => { if(_scrollsAway('touchmove')) _stopFollowing(); }, {passive:true});

      /* FIND LIVES IN THE RENDERER, not in the shell. Sending Ctrl+F into readline searches command
       * history; Ctrl+Shift+F searches everything xterm still holds, including program output and
       * the 5,000-line scrollback. The buffer API is public xterm API, so this works in the web,
       * Android and desktop bundles without another CDN or a version-sensitive private property. */
      try{ term.attachCustomKeyEventHandler((ev) => {
        if(_tabChord(ev)) return false;
        if(ev.type === 'keydown' && (ev.key === 'PageUp' || (ev.shiftKey && ev.key === 'ArrowUp')))
          _stopFollowing();
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

    let _fitT = null, _sentSize = '', _fitPixels = '';
    function _fit(){
      if(_fitT) clearTimeout(_fitT);
      // Coalesced: an on-screen keyboard opening fires a burst of resizes, and each one is a reflow
      // of the whole grid plus a frame on the wire.
      _fitT = setTimeout(() => {
        _fitT = null;
        if(!term) return;
        /* `_mountTerm` has its own block-scoped `box`; this sibling function cannot see it. The
         * old references below therefore threw ReferenceError inside their defensive try/catches,
         * silently disabling both the unfocused-window guard and pixel-size deduplication. Resolve
         * the live terminal host in this callback, after the debounce, so a parked/repainted window
         * is measured only if it is still the element this terminal owns. */
        const box = $('#tty-screen');
        if(!box || !box.isConnected) return;
        /* FOCUS MAY CHANGE Z-ORDER, NEVER TERMINAL GEOMETRY. PosterChanOS parks/moves the shared
         * feed while another window takes focus, and that transition fires ResizeObserver with a
         * temporary content size. Fitting xterm there sends SIGWINCH to the PTY and makes the
         * background terminal visibly rewrap. A deliberate edge resize focuses its frame before
         * changing the box, so ignoring background measurements loses no real resize. */
        try{
          const frame = box && box.closest && box.closest('.osw');
          if(frame && !frame.classList.contains('focused')) return;
        }catch(_){}
        /* Reattaching the live Terminal DOM after a focus change fires ResizeObserver even when its
         * box is exactly the same size. FitAddon can round a cell differently during that frame,
         * which sends SIGWINCH and corrupts full-screen Vim despite no user resize. Pixel geometry
         * is the authority: if it did not change, neither may the PTY grid. */
        let px='';
        try{
          const br=box.getBoundingClientRect();
          if(br.width < 2 || br.height < 2)return;
          px=Math.round(br.width*10)+'x'+Math.round(br.height*10);
          if(_fitPixels===px && _sentSize)return;
        }catch(_){}
        /* FitAddon reflows scrollback when the row count changes. If this terminal was following
         * the prompt, keep following across that reflow; otherwise the old viewport index can land
         * hundreds of rows above the new bottom after a window resize. A user already reading
         * history has followBottom=false and is deliberately left alone. */
        const followThisFit=followBottom;
        if(followThisFit)scrollingByUs=true;
        try{ term.options.fontSize = fontSize(); }catch(_){}
        /* Reparenting the live terminal during an app switch can deliver the first focused
         * ResizeObserver callback before xterm's viewport/canvas has finished reconnecting.  A
         * FitAddon exception there is transient.  Do not remember those pixels as successfully
         * fitted: otherwise the next stable callback sees the same rectangle, returns above, and
         * leaves the xterm grid/PTY at the pre-switch dimensions until somebody drags the window. */
        let fitOk=!fit;
        try{ if(fit){ fit.fit(); fitOk=true; } }catch(_){}
        if(followThisFit)_pinBottomAfterLayout();
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
        if(px&&fitOk)_fitPixels=px;
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

    /* LABELS ALREADY SPOKEN FOR ON THIS PAGE. `live` is refreshed on `ready`, which is AFTER the
     * open frame has gone out — so two quick presses of + would both read the same list, pick the
     * same label, and land in the same shell, which is the bug this whole mechanism exists to fix.
     * Requested labels are remembered here and never re-offered. */
    const asked = new Set();

    /* A LABEL NOBODY IS USING ON THIS HOST. `main` first, so the ordinary single-tab case keeps the
     * name a shell already has and reattaches to it across a restart of this node; then 2, 3, …
     *
     * Per HOST, because the tmux session's name is per (account, label) and two hosts are two
     * machines — `main` on server1 and `main` on nas are already different shells. */
    function _freeLabel(hostName){
      const taken = new Set();
      for(const x of live)
        if(x && String(x.host || '') === String(hostName || '') && x.label) taken.add(String(x.label));
      for(const k of asked){
        const i = k.indexOf('\u0000');
        if(i > 0 && k.slice(0, i) === String(hostName || '')) taken.add(k.slice(i + 1));
      }
      let pick = 'main';
      for(let n = 2; taken.has(pick); n++) pick = String(n);
      asked.add(String(hostName || '') + '\u0000' + pick);
      return pick;
    }

    function _resetForReplay(){
      /* xterm emits onScroll from reset(). That is an internal buffer change, not somebody choosing
       * to read history. If it lands unguarded it flips followBottom false before the resumed
      * session's backlog arrives, so a phone opens halfway up the terminal and has to be dragged
      * to the prompt by hand. Treat reset like every other programmatic reflow. */
      followBottom = true;
      if(!term) return;
      scrollingByUs = true;
      term.reset();
      _pinBottomAfterLayout();
    }

    function _restoreHandoffScroll(){
      const saved=handoffScroll;
      if(!saved || !term) return;
      if(handoffRestoreT){clearTimeout(handoffRestoreT);handoffRestoreT=null;}
      handoffScroll=null;
      followBottom=saved.pinned!==false;
      if(followBottom){ _pinBottomAfterLayout(); return; }
      const above=Math.max(0,Number(saved.aboveBottom)||0);
      const apply=()=>{
        if(!term || followBottom) return;
        try{
          const b=term.buffer&&term.buffer.active;
          if(b) term.scrollToLine(Math.max(0,Number(b.baseY||0)-above));
        }catch(_){}
      };
      try{ requestAnimationFrame(()=>requestAnimationFrame(apply)); }catch(_){ apply(); }
      setTimeout(apply,120);
    }

    function _scheduleHandoffScroll(){
      /* A handoff usually restores after replay's write callback, once xterm has grown the buffer.
       * A fully caught-up session has no replay frame, though, so that callback never exists. READY
       * schedules the no-output fallback; any real replay cancels it in _restoreHandoffScroll. */
      if(!handoffScroll||handoffRestoreT)return;
      handoffRestoreT=setTimeout(()=>{handoffRestoreT=null;_restoreHandoffScroll();},160);
    }

    /* A LOCAL REATTACH HAS TWO INPUTS: the cumulative backlog snapshot and the live IPC push.
     * Subscribe-before-snapshot is necessary (otherwise bytes printed between the two calls are
     * lost), but drawing pushes immediately is not safe. Chromium may hold an unfocused renderer's
     * IPC callback until focus returns. That old callback can then race the new snapshot and either
     * draw the same bytes twice or advance the cumulative cursor past bytes which have not been
     * drawn at all. Hold pushes behind one small gate until the snapshot has crossed `_frame`;
     * `_frame`'s sequence check then removes the overlap and the remaining suffix is delivered once.
     * `current` also invalidates callbacks already queued by Electron before removeListener ran. */
    function _makeLocalReplayGate(current, deliver){
      let pending=[], ready=false;
      return {
        push(ev){
          if(!current()) return;
          if(!ready){ pending.push(ev); return; }
          deliver(ev);
        },
        finish(snapshot){
          if(!current()){ pending=[]; return false; }
          if(snapshot) deliver(snapshot);
          ready=true;
          const queued=pending; pending=[];
          /* IPC preserves order, but sequence sorting makes the invariant explicit and keeps an
           * unsequenced `end` behind all output if a platform ever batches the callbacks. */
          queued.sort((a,b)=>{
            const as=typeof a.seq==='number'?a.seq:Number.MAX_SAFE_INTEGER;
            const bs=typeof b.seq==='number'?b.seq:Number.MAX_SAFE_INTEGER;
            return as-bs;
          });
          for(const ev of queued){ if(current()) deliver(ev); }
          return current();
        }
      };
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
      /* ASK WHO IS RUNNING BEFORE NAMING THIS TAB. The label has to miss every session this
       * ACCOUNT holds on that host, and one of them can have been opened on another device since
       * the strip was last painted — a stale list picks a name that is taken, and a taken name is
       * the same shell all over again. */
      try{ await _sessions(); }catch(_){}
      _remember('');                       // a fresh connect is a NEW session, not the old one
      cursor = 0;
      _resetForReplay();
      /* A NEW TAB NAMES ITSELF, and this line is the whole of "New tab actually opens a new shell".
       * With no label the server takes `main` for every one of them, and `tmux new-session -A` is
       * attach-or-create — so pressing + made a SECOND SSH CONNECTION ONTO THE SHELL ALREADY ON
       * SCREEN. Nothing failed, nothing logged: the same prompt came back, and the connection count
       * climbed by one per press (measured: three `server1` sessions, two of them replaying the
       * same 567 KB pane). */
      /* A shell on THIS machine takes no label: there is no tmux on the far end because there is
       * no far end, and giving one would name a local tab after a session that does not exist. */
      label = h.local ? '' : _freeLabel(host);
      _open({ host, password, label });
    }

    /* Reattach to a shell that is already running. No password is asked for and none is needed: the
     * login happened when the session was opened, and it is still open. */
    function attach(id, hostName, labelName){
      host = hostName || host;
      /* THE LABEL TRAVELS WITH THE RESUME even though the session is named by id. A resume whose
       * session is gone (this node restarted, the keeper was replaced) does not fail — it falls
       * through to OPENING one — and without the label that fallback lands in `main`, i.e. in
       * whichever tab happens to hold that name. */
      label = labelName || '';
      _remember(id);
      cursor = 0;                          // no local scrollback for it — replay from the start of
                                           // what the server still holds
      _resetForReplay();
      /* Explicit tab clicks intentionally go to the prompt. A monitor handoff is different: it is
       * the same visible tab crossing a seam, so reinstate whether the person was reading history
       * after reset's programmatic onScroll guard has been armed. */
      if(handoffScroll) followBottom=handoffScroll.pinned!==false;
      _open({ resume: id, host: hostName || '', label });
    }

    function _scrollChoice(){
      let aboveBottom=handoffScroll?Math.max(0,Number(handoffScroll.aboveBottom)||0):0;
      try{const b=term&&term.buffer&&term.buffer.active;
        if(b)aboveBottom=Math.max(0,Number(b.baseY||0)-Number(b.viewportY||0));}catch(_){}
      return {pinned:followBottom!==false,aboveBottom};
    }

    function switchTab(id, hostName, labelName){
      id=String(id||'');if(!id||id===sid)return false;
      if(sid)tabScroll.set(String(sid),_scrollChoice());
      const saved=tabScroll.get(id);
      handoffScroll=saved?{pinned:saved.pinned!==false,
        aboveBottom:Math.max(0,Number(saved.aboveBottom)||0)}:null;
      attach(id,hostName,labelName);return true;
    }

    function _cycleTab(step){
      const tabs=[...document.querySelectorAll('#tty-sessions [data-tab]')];
      if(tabs.length<2)return;
      let at=tabs.findIndex(x=>x.dataset.tab===sid);if(at<0)at=0;
      const next=tabs[(at+(step<0?-1:1)+tabs.length)%tabs.length];
      if(next&&next.dataset.tab!==sid)switchTab(next.dataset.tab,next.dataset.host,next.dataset.label);
    }

    /* The phone keyboard types through #tty-catch, not xterm's textarea. Keeping this chord only in
     * attachCustomKeyEventHandler made Ctrl+PageUp/PageDown work with a desktop keyboard and do
     * nothing on Android. Both input surfaces call this one owner; true means the key was consumed. */
    function _tabChord(ev){
      if(!ev || ev.type!=='keydown' || !(ev.ctrlKey||ev.metaKey)
          || (ev.key!=='PageUp'&&ev.key!=='PageDown')) return false;
      ev.preventDefault();
      _cycleTab(ev.key==='PageUp'?-1:1);
      return true;
    }

    function _open(frame){
      const opening = ++openEpoch;
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
        if(opening !== openEpoch) return;
        if(!term && !_mountTerm()){ _state('the terminal could not start', 'err'); want = false; return; }
        if(!want || opening !== openEpoch) return;
        _state(frame.resume ? 'reattaching…' : 'connecting…');
        try{ ws = new WebSocket(_wsUrl()); }
        catch(_){ _state('could not open a connection', 'err'); return _later(); }
        const mine = ws;
        ws.onopen = () => {
          if(opening !== openEpoch){ try{ mine.close(); }catch(_){} return; }
          /* The socket authenticates with the SAME bearer the rest of the app uses. It is sent in the
           * open frame rather than the URL: a query string lands in every proxy log between here and
           * the server, and this one is a credential. */
          _send(Object.assign({ t: 'open', cols: term.cols, rows: term.rows, cursor,
                                token: (window.__PC_TOKEN__ || '') }, frame));
          frame.password = '';             // never kept past the one frame that needs it
        };
        ws.onmessage = (ev) => {
          if(opening !== openEpoch) return;
          let m; try{ m = JSON.parse(ev.data); }catch(_){ return; }
          _frame(m);
        };
        link = {
          kind: 'ws',
          send(o){ try{ if(ws && ws.readyState === 1) ws.send(JSON.stringify(o)); }catch(_){} },
          close(){ try{ mine.onclose = null; mine.onmessage = null; mine.close(); }catch(_){} },
        };
        mine.onclose = () => { if(opening === openEpoch) _drop(); };
        mine.onerror = () => {};           // onclose follows and is where the retry lives
      })();
    }

    /* THE LOCAL PTY. A shell on THIS machine, through the desktop bridge — no socket, no server, no
     * SSH. It emits the server's own frames into `_frame`, so nothing downstream knows the
     * difference, and the session survives this page being reloaded exactly the way a server-side
     * one does: the PTY lives in the desktop process, and `backlog` is what a fresh page redraws
     * from. That matters more here than over SSH, because the WebView holding this page is the half
     * Android and Chromium take away under memory pressure. */
    function _openLocal(frame){
      const opening = openEpoch;
      const T = LOCAL();
      if(!T){ _frame({ t: 'err', m: 'this build has no shell of its own' }); return; }
      let id = String(frame.resume || '').replace(/^local:/, '');
      let stop = null, gone = false;
      const replay=_makeLocalReplayGate(
        ()=>!gone && opening===openEpoch,
        ev=>_frame(ev));
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
        close(){
          gone = true;
          if(stop){ try{ stop(); }catch(_){} stop = null; }
          if(id && T.detach){ try{ T.detach(id); }catch(_){} }
        },
      };
      (async () => {
        if(!term && !_mountTerm()){ _state('the terminal could not start', 'err'); want = false; return; }
        if(!want || opening !== openEpoch) return;
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
          }
          if(gone || opening !== openEpoch) return;
          /* Subscribe FIRST, then take the snapshot. This closes the otherwise unavoidable gap
           * where the shell can print after backlog() but before attach(). The cumulative sequence
           * guard in _frame makes the overlap safe whichever IPC message reaches us first. */
          stop = T.onData((ev) => { if(String(ev.id) === id) replay.push(ev); });
          await T.attach(id);
          b = await T.backlog(id, fresh ? 0 : (Number(cursor) || 0));
          if(gone || opening !== openEpoch) return;
          _frame({ t: 'ready', sid: localSid(id), host: 'local', resumed: !fresh });
          if(b){
            /* Redraw what was missed, and say so when the gap is bigger than what is still kept —
             * a fragment presented as the whole history is how scrollback loses its middle. */
            if(b.truncated) term.write('\r\n\x1b[90m— earlier output is no longer kept —\x1b[0m\r\n');
            /* An empty snapshot still carries the authoritative cumulative cursor. Advancing it
             * prevents a delayed pre-focus callback at that same sequence from being mistaken for
             * new output merely because there were zero replay bytes. */
            replay.finish(typeof b.seq==='number' ? { t: 'out', d: b.d || '', seq: b.seq } : null);
            if(!b.alive) replay.push({ t: 'end', m: 'that shell has exited' });
          }
          else replay.finish(null);
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
            /* Attach and backlog deliberately overlap so no byte can fall between them. If the
             * pushed event wins the race, its cumulative sequence makes the later snapshot old;
             * discard that snapshot instead of drawing the prompt/echo twice. */
            if(typeof m.seq === 'number' && m.seq <= cursor) return;
            /* xterm itself emits onScroll while a large replay grows baseY. That is layout, not the
             * person scrolling, but the old guard began only INSIDE this callback—after xterm had
             * already emitted and changed followBottom to false. Capture the choice and suppress
             * those internal events around the whole asynchronous write. Once the bytes are drawn,
             * land at the live prompt exactly once; a later real swipe still disables following. */
            const followThisWrite=followBottom;
            if(followThisWrite) scrollingByUs=true;
            term.write(m.d, function(){
              if(handoffScroll) _restoreHandoffScroll();
              else if(followThisWrite) _pinBottomAfterLayout();
            });
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
            // WHAT THE SERVER USED, not what was asked for — a resume answers with the label the
            // session was opened under, which is the one a reconnect has to name.
            if(m.label) label = m.label;
            if(label) asked.add(String(host || '') + '\u0000' + String(label));
            _state((m.resumed ? 'reattached to ' : 'connected to ') + host, 'ok');
            _chrome(true); _fit(); _focus();
            /* A reconnect is not a new terminal. If the person was reading scrollback when the
             * socket dropped, keep that deliberate position; forcing follow mode here yanked them
             * to the prompt on every phone wake/network change. Fresh opens and explicit tab
             * attaches already call _resetForReplay(), which arms followBottom before READY, so
             * they still open at current output. A terminal that was pinned also remains pinned. */
            if(followBottom) _pinBottomAfterLayout();
            else if(handoffScroll) _scheduleHandoffScroll();
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
        _open({ resume: sid, host, label });
      }, wait);
    }

    // Come back to a foregrounded tab immediately rather than waiting out the backoff — a phone
    // unlocking is the single most common way this socket dies.
    function _wake(){
      if(document.visibilityState !== 'visible') return;
      if(!want || !sid || connected || !mounted) return;
      if(retryT){ clearTimeout(retryT); retryT = null; }
      retry = 0; _open({ resume: sid, host, label });
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
    function _paintSessions(){
      const box = $('#tty-sessions'); if(!box) return;
      const tabs = live.slice();
      if(sid && !tabs.some(x => x.sid === sid))
        tabs.unshift({ sid, host:host || 'local', label, age:0, alive:true });
      if(handoffOrder.length){
        const rank=new Map(handoffOrder.map((id,n)=>[String(id),n]));
        tabs.sort((a,b)=>(rank.has(String(a.sid))?rank.get(String(a.sid)):1e9)
          -(rank.has(String(b.sid))?rank.get(String(b.sid)):1e9));
      }
      box.hidden = false;
      box.innerHTML = '<span class="tty-sess-lbl">tabs</span>' + tabs.map((x, n) => {
        const lab = String(x.label || '');
        const nm = (x.host || 'terminal') + (lab ? (lab === 'main' ? '' : ' ' + lab) : ' ' + (n + 1));
        return `<span class="tty-sess tty-tab${x.sid === sid ? ' active' : ''}" data-tab="${enc(x.sid)}"
               data-host="${enc(x.host || '')}" data-label="${enc(lab)}"
               title="${enc(nm)}"><b>${enc(nm)}</b>`
        + `<i>${_ago(x.age)}</i>`
        + `<button data-kill="${enc(x.sid)}" class="tty-kill" title="Close tab"
                   aria-label="Close terminal tab">×</button></span>`;
      }).join('')
        + '<button class="tty-tab-new" id="tty-tab-new" title="New terminal tab">+</button>';
    }

    async function _remoteSessions(){
      try{
        const r = await _bounded(authFetch('/api/ssh/sessions'), 8000);
        const remote = ((await _bounded(r.json(), 3000)) || {}).sessions || [];
        /* Replace only remote rows. A late server response must never erase a local PTY that was
         * opened while the network request was in flight. */
        live = live.filter(x => isLocalSid(x.sid)).concat(remote);
        _paintSessions();
      }catch(_){}
    }

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
      /* A local terminal is painted and returned to its opener before ANY instance request. The
       * remote tabs are useful, but connectivity is not a prerequisite for a shell on this device.
       * In particular authFetch can remain pending while the signer relay reconnects. */
      if(LOCAL()) _remoteSessions();
      else await _remoteSessions();
      /* THESE ARE TABS, not a recovery list. Every row names a distinct PTY; selecting one tears
       * down only the viewing transport and attaches this xterm to that PTY. The processes and
       * input streams never merge. Keeping the active shell in the strip is what makes the model
       * visible: two terminals look like two tabs, instead of one terminal plus an obscure
       * “still running” diagnostic people quite reasonably did not recognize as tab support. */
      _paintSessions();
      /* NAMED BY ITS LABEL, NEVER BY ITS POSITION. `${host} ${n+1}` renumbers every tab the moment
       * one of them is closed, so the tab you were in changes its name while you are looking at it —
       * and it says nothing about WHICH shell it is, which was invisible for as long as they were
       * all secretly the same one. The label IS the remote tmux session, so it is the honest name.
       * `main` is left implicit: one tab should read `server1`, not `server1 main`. */
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
      /* This chooses the host for NEW TAB; it does not retarget the running PTY. Keep it usable
       * while connected, or an automatically opened Local tab makes every saved server
       * unreachable even though the New tab button is still visible. */
      if(sel) sel.disabled = false;
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
      _paintHosts();
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
          if(a && a.dataset.tab !== sid) return switchTab(a.dataset.tab, a.dataset.host, a.dataset.label); }; }
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
          if(_tabChord(ev)) return;
          if(ev.key === 'Enter'){ ev.preventDefault(); _send({ t: 'in', d: '\r' }); return; }
          if(ev.key === 'Backspace'){ ev.preventDefault(); _send({ t: 'in', d: '\x7f' }); return; }
          if(SEQ[ev.key]){ ev.preventDefault(); _send({ t: 'in', d: SEQ[ev.key] }); }
        };
      }
      window.addEventListener('resize', _fit);
    }

    /* `host` is optional and is how PosterChan Code puts a real shell in its bottom panel.
     *
     * Default `$('#feed')` keeps the Terminal VIEW byte-identical in behaviour. What an embedder
     * gets is the same singleton, not a second one: there is one xterm, one PTY and one session id
     * here, and two live mounts would share `term`/`ws`/`sid` and write every keystroke twice (the
     * exact failure `renderEpoch` exists for). So whoever renders last owns it — which is correct
     * on the desktop, where only the FOCUSED window is ever rendered. */
    async function render(host){
      // An explicit host is a desktop-owned terminal window. With no host this is the shared feed,
      // and a deferred mount must not replace whichever app owns it now.
      if(!host && (!PC.isView || !PC.isView('terminal'))) return;
      const feed = host || $('#feed'); if(!feed) return;
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
          /* Opening Terminal is not the same operation as pressing New tab. Compositor ticks can
           * be delivered more than once (and a person can press the shortcut twice); creating a
           * PTY for each delivery left five identical-looking tabs fighting for the same screen.
           * Resume the remembered local tab, or the newest live local tab. Only the explicit +
           * button calls connect() when a terminal already exists. */
          const prev = _recall();
          const existing = live.find(x => x.sid === prev && isLocalSid(x.sid))
                        || live.find(x => isLocalSid(x.sid));
          if(existing) attach(existing.sid, 'local');
          else connect();
          if(!XT()) _state('the terminal library did not load', 'err');
          return;
        }
        _want = '';
        const prev = _recall();
        if(prev && live.some(x => x.sid === prev)){
          const s0 = live.find(x => x.sid === prev);
          if(s0 && $('#tty-host')) $('#tty-host').value = s0.host || '';
          attach(prev, s0 && s0.host, s0 && s0.label);
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
      ++openEpoch;
      ++renderEpoch;                 // every pending continuation now belongs to a dead screen
      // LEAVING THE SCREEN IS DETACHING, never killing: the shell keeps running and the id is kept,
      // so coming back reattaches. This used to close the socket AND that was the end of the session.
      _bye();
      document.removeEventListener('visibilitychange', _wake);
      if(ro){ try{ ro.disconnect(); }catch(_){} ro = null; }
      if(term){ try{ term.dispose(); }catch(_){} term = null; fit = null; }
      ++bottomPinEpoch;
      if(bottomPinT){ clearTimeout(bottomPinT); bottomPinT = null; }
      if(handoffRestoreT){ clearTimeout(handoffRestoreT); handoffRestoreT = null; }
      scrollingByUs = false;
      _fitPixels = '';
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
      /* ALREADY ON THE SCREEN is the case that would otherwise do nothing: the view is mounted, so
       * nothing re-runs the block that reads `_want`. It is also the important idempotence case:
       * a second shortcut focuses the current shell; it must not manufacture another PTY. */
      if(mounted && hosts.some(h => h && h.local)){
        if(term) _focus();
        return true;
      }
      _want = 'local';
      return true;
    }

    /* Move this terminal VIEW to another monitor without changing the shell behind it. The PTY is
     * owned by the desktop/server and is therefore attachable from the destination renderer; only
     * its id must cross before render() runs. */
    function adoptSession(id){
      return acceptHandoff({activeSid:id});
    }

    function handoffState(){
      const current=_scrollChoice();if(sid)tabScroll.set(String(sid),current);
      return {activeSid:String(sid||''),host:String(host||''),label:String(label||''),
        tabs:live.map(x=>({sid:String(x.sid||''),host:String(x.host||''),label:String(x.label||''),
          scroll:tabScroll.get(String(x.sid||''))||null}))
          .filter(x=>x.sid),scroll:current};
    }

    function acceptHandoff(state){
      state=state&&typeof state==='object'?state:{};
      const id=String(state.activeSid||state.sid||'');
      if(!id) return false;
      const tabs=Array.isArray(state.tabs)?state.tabs.filter(x=>x&&x.sid).map(x=>({
        sid:String(x.sid),host:String(x.host||''),label:String(x.label||''),scroll:x.scroll})):[];
      tabScroll.clear();tabs.forEach(x=>{if(x.scroll&&typeof x.scroll==='object')tabScroll.set(x.sid,{
        pinned:x.scroll.pinned!==false,aboveBottom:Math.max(0,Number(x.scroll.aboveBottom)||0)});});
      handoffOrder=tabs.map(x=>x.sid);
      if(tabs.length) live=tabs;
      const active=tabs.find(x=>x.sid===id);
      host=String(state.host||(active&&active.host)||host||'');
      label=String(state.label||(active&&active.label)||'');
      handoffScroll=state.scroll&&typeof state.scroll==='object'?{
        pinned:state.scroll.pinned!==false,aboveBottom:Math.max(0,Number(state.scroll.aboveBottom)||0)}:null;
      followBottom=!handoffScroll||handoffScroll.pinned!==false;
      _remember(id);
      _want=isLocalSid(id)?'local':'';
      cursor=0;
      return true;
    }

    window.PCTerm = { render, unmount, isOpen: () => !!mounted, connected: () => connected,
                      openLocal, sessionId: () => sid, adoptSession, handoffState, acceptHandoff };
  }
  init();
})();
