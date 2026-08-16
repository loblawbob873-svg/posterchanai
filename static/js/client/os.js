/* PosterChan OS — a windowed desktop over the existing client.
 *
 * Entered by clicking the instance logo above the search box; left again from the taskbar. The
 * choice is remembered per device, so it opens the way you left it.
 *
 * HOW IT RUNS THE EXISTING FEATURES, which is the whole design.
 *
 * Every feature in this client renders into ONE shared element, `#feed` — eleven modules and about
 * a hundred and thirty inline branches all call `$('#feed')` and paint into it. A desktop needs
 * several containers at once, and rewriting all of that to take a container would be a very large
 * change to very well-tested code.
 *
 * So the shell moves the ID instead. Each window owns a body element; the FOCUSED window's body
 * carries `id="feed"` and every other window's does not. `switchView()` then renders the feature
 * into the focused window exactly as it always has, with no change to any feature. Windows that are
 * not focused keep the DOM they last painted — they look like themselves, they are simply not being
 * updated — and refocusing re-renders from the module's own state (`S = {…}`), which is how these
 * modules already survive leaving and returning to a view.
 *
 * What that buys: no iframes. An iframe per window would be a second, third, fourth copy of a
 * 22,000-line app, each opening its OWN relay socket and subscriptions — after a day spent taking
 * relay load apart, that is not a trade worth making for background windows nobody is looking at.
 *
 * DESKTOP ONLY, deliberately: below 1024px the logo does nothing and the normal client stays. A
 * draggable window on a 360px screen fails every tap-target and overflow rule the mobile checks
 * enforce, and the phone already has a better answer for "switch between features".
 */
(function(){
  'use strict';

  const MIN_WIDTH = 1024;          // below this the desktop is not offered at all
  const KEY = 'osMode';            // ClientSettings: remembered across sessions
  const TASKBAR = 48;
  const SNAP = 8;                  // edge gutter when tiling

  let root = null, bar = null, desk = null;
  let wins = [];                   // [{id, view, title, el, body, min, max, rect}]
  let seq = 0, zTop = 10, on = false, startOpen = false;

  /* WINDOWS LIVE IN A BOUNDED BAND, and that bound is the whole of a bug that reads as "the start
   * menu stopped going over windows". zTop was a plain counter that only ever went up, and it is
   * bumped by every FOCUS — including routeView, which fires on ordinary navigation inside a single
   * window (timeline → profile → thread → back). So it is not a hoarder's problem: measured, ~310
   * view switches is enough, and past that a window's inline z-index climbs over the panels that
   * are supposed to sit ABOVE the desktop — the start menu (320), the notification flyout (330),
   * the background picker (335), the desktop toasts (345), and, inside the desk, the right-click
   * menu (420). The panel is still built, still painted and still positioned; it is simply behind
   * the window, so it takes no clicks and nothing is logged. Raising the panels only moves the
   * ceiling further away — the counter reaches any number eventually.
   * So a raise that would leave the band renumbers the open windows first, bottom to top, which
   * preserves their stacking exactly and cannot ever collide with a panel. */
  const Z_WIN_BASE = 10, Z_WIN_MAX = 200;
  function nextZ(){
    if(zTop >= Z_WIN_MAX){
      wins.slice()
        .sort((a, b) => (parseInt(a.el.style.zIndex, 10) || 0) - (parseInt(b.el.style.zIndex, 10) || 0))
        .forEach((x, i) => { x.el.style.zIndex = String(Z_WIN_BASE + i); });
      zTop = Z_WIN_BASE + wins.length - 1;
    }
    return ++zTop;
  }
  let realFeed = null;             // the client's own #feed element — MOVED into the focused window
  let realHome = null;             // …and where it belongs when the desktop closes

  const PC = () => window.__PC || {};
  const $ = (s, r) => (r || document).querySelector(s);
  const $$ = (s, r) => Array.from((r || document).querySelectorAll(s));
  const enc = s => String(s == null ? '' : s)
    .replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

  const settings = () => window.ClientSettings || { get: (k, d) => d, set(){} };
  const fits = () => window.innerWidth >= MIN_WIDTH;

  /* The launcher list is READ FROM THE SIDEBAR, not written out again here.
   *
   * The desktop icons and the start menu are meant to be "the features on the left navbar", so they
   * are taken from it at open time — including each item's own icon. A feature added to the nav
   * (or hidden by nostr_only, or by _viewNeedsInstance) appears or disappears here for free, and
   * there is no second list to forget to update.
   *
   * THAT INCLUDES WHAT THE USER SWITCHED OFF. Settings → Profile → 🧭 Sidebar removes rows from the
   * nav (`nav-off`, app.js), and "the features on the left navbar" has to mean it here too — a
   * desktop still carrying every game after Games was switched off is a preference that did not
   * work. It was read as "hide the door, keep the start menu" at first, which is wrong: the start
   * menu is not a way back, it is the same launcher, and the way back is the switch itself.
   *
   * The filter is `_navGone`, and it is applied ONLY to the launcher (`launchApps`). `apps()` itself
   * stays complete, because openApp and routeView use it to LOOK UP a view's label and icon — filter
   * there and hiding a row would stop the view opening from a link, which is the one thing this
   * preference must never do. */
  /* The app's NAME, without its unread count.
   *
   * The count is an `<i class="badge">` INSIDE the label span —
   * `<span>Messages<i id="dm-badge">99+</i></span>` — so `span.textContent` reads "Messages99+" and
   * that is what the desktop icon and its window title said.
   *
   * It used to be stripped with `.replace(/\d+$/, '')`, which fails on the badge it was written for
   * (the count is capped at the string "99+", which does not end in a digit) and mangles a name that
   * legitimately ends in one: CONNECT 4 was already showing on the desktop as "Connect".
   *
   * So take only the span's own TEXT nodes and let any element inside it — badge or otherwise — be
   * exactly as irrelevant as it is. The clone is the fallback for a label wrapped in some tag. */
  function _navLabel(btn){
    const span = btn.querySelector('span');
    if(!span) return '';
    let t = [...span.childNodes].filter(n => n.nodeType === 3)
                                .map(n => n.textContent).join('').trim();
    if(!t){
      const c = span.cloneNode(true);
      c.querySelectorAll('.badge, .pill, i').forEach(n => n.remove());
      t = (c.textContent || '').trim();
    }
    return t;
  }

  /* Is this sidebar row gone — switched off by the user, or gated off by the deployment?
   *
   * Bounded to the row and its own `.nav-group`, deliberately NOT a `closest('.hidden')` walk: the
   * whole app shell is `<div id="app" class="app hidden">` until sign-in, so an unbounded walk
   * answers "everything is gone" during boot and draws an empty desktop. */
  function _navGone(btn){
    if(!btn) return false;
    if(btn.classList.contains('nav-off') || btn.classList.contains('hidden')) return true;
    const g = btn.closest && btn.closest('.nav-group');
    return !!(g && (g.classList.contains('nav-off') || g.classList.contains('hidden')));
  }

  function apps(){
    const seen = new Set();
    return $$('.sidebar .nav .nav-item[data-view]').map(btn => {
      const view = btn.dataset.view;
      if(!view || seen.has(view)) return null;
      seen.add(view);
      const use = btn.querySelector('svg use');
      return { view, label: _navLabel(btn) || view, off: _navGone(btn),
               icon: use ? (use.getAttribute('href') || use.getAttribute('xlink:href') || '') : '' };
      // EXTRAS are not sidebar rows, but two of them SHADOW one (#nav-music, #nav-golive) — so they
      // answer to the same switch. `__profile` has no row and is never hidden by this.
    }).filter(Boolean).concat(EXTRAS.filter(x => x.when())
                                    .map(x => ({ ...x, off: _navGone(document.getElementById(_EXTRA_ROW[x.view])) })));
  }
  const _EXTRA_ROW = { __music: 'nav-music', __golive: 'nav-golive' };
  /* What the LAUNCHER draws: the desktop icons, the folders and the start menu. `apps()` is the
   * complete list and stays that way — see the banner above for why filtering it would break opening
   * a hidden view from a link. */
  function launchApps(){ return apps().filter(a => !a.off); }

  /* Entries that are not sidebar nav items. Go Live lives in the mobile "More" sheet and in the
   * rightbar, neither of which the desktop shows, so the launcher — which reads .nav-item[data-view]
   * — could never have found it. `act` marks it as something to RUN rather than a view to open in a
   * window: it is a dialog that ends in a live stream, and the stream itself opens the Streams app. */
  /* Desktop folders. The launcher reads the sidebar, which lists every game as its own entry — six
   * icons for one thing, eating a whole row of the desktop. Grouped exactly as the classic UI groups
   * them (More → Games), so the two surfaces cannot disagree about what is in the folder. A folder is
   * only shown when it actually has members, so a deployment with the games off sees nothing rather
   * than an empty folder. */
  const FOLDERS = [
    { key: 'games', label: 'Nostr Games', icon: '#i-gamepad',
      views: ['chess', 'ttt', 'hangman', 'connect4', 'blackjack', 'holdem', 'xdc'] },
  ];

  /* ---- YOUR layout -----------------------------------------------------------------------------
   *
   * Icons can be dragged into the order you want, dropped on one another to make a named folder, and
   * hidden from the desktop without leaving the start menu — and that arrangement follows the
   * ACCOUNT, not the browser. It is ONE kind-30078 document, `d = pcai:desktop`, NIP-44-encrypted to
   * your own key: the server cannot read which apps you use, and a second device draws the desktop
   * you arranged on the first.
   *
   * WHAT THE DOCUMENT SAYS, and what it deliberately does NOT. It stores your DECISIONS — the order
   * you arranged, the folders you made, the icons you hid — never the app list itself. The list is
   * still read from the sidebar at draw time, so a feature added to the nav (or hidden by
   * nostr_only, or by _viewNeedsInstance) appears on a customised desktop for free, at the end,
   * exactly as it does on an untouched one. A document that listed the apps would freeze the
   * launcher at the moment you first dragged something.
   *
   * The built-in FOLDERS above are a DEFAULT, not a rule, and they apply only to views the document
   * has no opinion about. So dragging Chess onto the desktop takes it out of Nostr Games for good,
   * while a game added later still lands in the folder with the rest.
   *
   * THREE THINGS THAT SILENTLY LOSE A LAYOUT, each one already the cause of a real bug elsewhere in
   * this client:
   *
   *  1. An empty READ and an unreachable relay are the same answer, and saving on the strength of
   *     the second replaces a real layout with the defaults — the replaceable-doc wipe. So `_wr` is
   *     set only once a relay has actually ANSWERED, and nothing is published before it is.
   *  2. The client cache evicts newest-N by created_at, which is right for the firehose and fatal
   *     for a document only its author can decrypt — minutes of reading the global feed would drop
   *     it. `pcai:desktop` is exempted in store.js `_isPinned`.
   *  3. Changing relays leaves the document behind on the pool you stopped using. It is carried over
   *     with the rest of the private libraries by `_CARRY_D` in app.js.
   *
   * `Relay.publish` ALWAYS resolves an object, so a save is checked with `r && r.ok` — `!!r` reports
   * a timeout as success. A save that did not land is ROLLED BACK on screen and said out loud: an
   * icon that moves and then moves back on the next reload is worse than one that refused to move.
   */
  const LAY_KIND = 30078;
  const D_LAY = 'pcai:desktop';
  /* `pos` — where icons are, once the user has MOVED one. Until then it is empty and the desktop
   *         lays itself out in a grid, which is the right default for "I have never touched this".
   * `bg`  — the wallpaper: the sha of a picture in the drive's `Backgrounds` folder. The sha, not a
   *         URL: the bytes are encrypted and only this client can decrypt them, so the URL is an
   *         object URL that exists for one session on one device. */
  /* `widgets` — the panels sitting ON the desktop (a ticker, the weather, the player, a note, a
   * search box), as `{id, type, x, y, size, cfg}`.
   *
   * x/y are FRACTIONS of the free area, not pixels, and the size is a NAME rather than a width. Icons
   * store pixels and clamp, which keeps them on screen but not where you put them: a widget placed
   * against the right edge of a 2560px monitor belongs against the right edge of the laptop that
   * opens the same account, not 1500px into the middle of it. Fractions reflow; a name lets the
   * actual size come from the screen, so a panel that is comfortable on a desktop does not cover a
   * tablet. Both are properties of the ARRANGEMENT, which is what this document holds. */
  const BLANK = () => ({ v: 1, folders: [], order: [], hidden: [], pos: {}, bg: '', widgets: [] });

  let _doc = null;        // the layout as last read/written; null = nothing read yet (draw defaults)
  let _docPk = '';        // …whose. An account switch must not paint the previous account's desktop.
  let _docAt = 0;         // created_at of the newest version we have seen
  let _wr = false;        // a relay ANSWERED, so a write cannot replace a layout we never read
  let _layWhy = '';       // why not, when not: 'relay' (nobody EOSEd) | 'signer' (would not decrypt)
  let _signerRetried = 0;       // how many automatic re-reads have been scheduled (see the ladder)
  /* When to re-read after a signer that did not answer, in ms. Spread over ~six minutes, because the
   * thing being waited for is not always a human with a phone — it can be a dead relay socket that
   * redials on its own backoff. */
  const _SIGNER_RETRY_AT = [20000, 45000, 90000, 180000];
  let _noticeSaid = '';         // the reason last announced, so a retry ladder says it once
  let _layLoading = null, _layLoadingPk = '', _laySub = null, _layChain = Promise.resolve();

  /* NEVER name a local binding `Relay` (or `Store`) in this file.
   *
   * That is not style: this module already reaches the pool as the GLOBAL — `Relay.conns()` in
   * netConns, `Relay.watch` in enter, `Relay.wake` in the tray — and every one of those is written
   * `window.Relay && Relay.foo && Relay.foo()`. A local `const Relay = () => window.Relay` shadows
   * the object across the whole IIFE, so `Relay.conns` is `undefined` on a FUNCTION, the guard
   * quietly evaluates false, and the taskbar reports "no relays are configured" on a client that is
   * connected and working — no error, nothing in the console, and classic mode fine because it
   * never goes through here. Shipped exactly once, by the desktop-layout change.
   * Asserted by tests/test_desktop_layout.py::ShadowTests. */
  const RelayOf = () => window.Relay;
  const StoreOf = () => window.Store;
  const LFILTER = () => ({ authors: [me().pubkey], kinds: [LAY_KIND], '#d': [D_LAY], limit: 1 });
  const _clone = o => JSON.parse(JSON.stringify(o));

  /* Whatever came out of the relay, in the shape the rest of this file may assume. A document
   * written by a newer client — or half-written — must not be able to throw while drawing the
   * desktop, because then there is no desktop. The invariants are enforced HERE and nowhere else:
   * a view lives in at most one folder, a folder is never inside another, a hidden view is not also
   * placed, and an empty folder is dropped rather than kept as a tile with nothing in it. */
  function _normDoc(o){
    const out = BLANK();
    const seenKey = new Set(), inFolder = new Set();
    const str = (v, n) => String(v == null ? '' : v).slice(0, n);
    for(const f of (Array.isArray(o && o.folders) ? o.folders : [])){
      const key = str((f && f.key) || '', 40).replace(/[^A-Za-z0-9_-]/g, '');
      if(!key || seenKey.has(key)) continue;
      const views = [];
      for(const v of (Array.isArray(f.views) ? f.views : [])){
        const s = str(v, 120);
        if(!s || s.indexOf('folder:') === 0 || inFolder.has(s)) continue;
        inFolder.add(s); views.push(s);
      }
      if(!views.length) continue;               // an empty folder is not a folder
      seenKey.add(key);
      out.folders.push({ key, label: str(f.label, 60) || 'Folder',
                         icon: str(f.icon, 60) || '#i-folder', views });
    }
    for(const v of (Array.isArray(o && o.hidden) ? o.hidden : [])){
      const s = str(v, 120);
      if(s && s.indexOf('folder:') !== 0 && !inFolder.has(s) && out.hidden.indexOf(s) < 0) out.hidden.push(s);
    }
    /* Free positions. Bounded and integer — they are written into style.left/top, and this document
     * is the one thing here a future client version could put anything in. A position for a view
     * that is inside a FOLDER is dropped: it has no place on the desktop to be at. */
    const posIn = (o && typeof o.pos === 'object' && o.pos) || {};
    for(const k in posIn){
      const key = str(k, 120);
      if(!key || inFolder.has(key)) continue;
      const v = posIn[k];
      if(!Array.isArray(v) || v.length < 2) continue;
      const x = Math.round(Number(v[0])), y = Math.round(Number(v[1]));
      if(!isFinite(x) || !isFinite(y)) continue;
      out.pos[key] = [Math.max(0, Math.min(20000, x)), Math.max(0, Math.min(20000, y))];
    }
    out.bg = /^[0-9a-f]{64}$/i.test(String((o && o.bg) || '')) ? String(o.bg).toLowerCase() : '';
    /* Widgets. Bounded in every direction, because this document is the one thing here that a future
     * client version — or a half-finished write — could put anything in, and it is read on every draw
     * of the desktop. An unknown TYPE is dropped rather than kept: it would draw an empty frame that
     * nothing can fill and nothing explains. `cfg` is a small flat bag of strings/numbers, capped, so
     * a widget can remember its city without this becoming a place to store documents. */
    const seenId = new Set();
    for(const w of (Array.isArray(o && o.widgets) ? o.widgets : [])){
      if(out.widgets.length >= WGT_MAX) break;
      if(!w || typeof w !== 'object') continue;
      const type = str(w.type, 24).replace(/[^a-z0-9_-]/gi, '');
      /* A type THIS client does not know is KEPT, not dropped.
       *
       * This sanitiser runs on read AND again on every write (`saveLayout(_normDoc(doc))`), so
       * dropping the row here does not merely hide the widget — it publishes a document with the
       * widget deleted, to every device. Arrange widgets on an up-to-date desktop, then let a cached
       * PWA or an older APK move a single icon, and that client's save wipes them for everyone with
       * nothing said. `order` and `hidden` already keep keys they do not recognise for exactly this
       * reason; widgets are the same problem. drawWidgets skips what it cannot draw. */
      if(!type) continue;
      const id = str(w.id, 40).replace(/[^A-Za-z0-9_-]/g, '') || (type + '-' + out.widgets.length);
      if(seenId.has(id)) continue;
      seenId.add(id);
      const num = (v, d) => { const n = Number(v); return isFinite(n) ? Math.max(0, Math.min(1, n)) : d; };
      const cfg = {};
      const cin = (w.cfg && typeof w.cfg === 'object' && !Array.isArray(w.cfg)) ? w.cfg : {};
      let n = 0;
      for(const k in cin){
        if(++n > 12) break;
        const key = str(k, 24).replace(/[^A-Za-z0-9_-]/g, '');
        const v = cin[k];
        if(!key) continue;
        if(typeof v === 'number' && isFinite(v)) cfg[key] = v;
        else if(typeof v === 'boolean') cfg[key] = v;
        else if(typeof v === 'string') cfg[key] = v.slice(0, WGT_TEXT_MAX);
      }
      out.widgets.push({ id, type,
                         x: num(w.x, 0), y: num(w.y, 0),
                         size: WGT_SIZES[str(w.size, 4)] ? str(w.size, 4) : 'm',
                         cfg });
    }
    const seenOrd = new Set();
    for(const v of (Array.isArray(o && o.order) ? o.order : [])){
      const s = str(v, 120);
      if(!s || seenOrd.has(s) || inFolder.has(s) || out.hidden.indexOf(s) >= 0) continue;
      seenOrd.add(s); out.order.push(s);
    }
    return out;
  }

  /* The whole of the arrangement, as pure arithmetic over (the sidebar, the document). Kept free of
   * the DOM so tests/test_desktop_layout.py can run the shipped code against a list of apps — the
   * parts that can be wrong here are wrong in ways nothing on screen announces: an app that quietly
   * stops appearing, a folder that eats an icon twice, a new feature that never shows up. */
  function computeLayout(list, doc){
    const byView = new Map((list || []).map(a => [a.view, a]));
    const d = _normDoc(doc || {});
    const hidden = new Set(d.hidden.filter(v => byView.has(v)));
    const placed = new Set();          // views the DOCUMENT has an opinion about
    const folders = [];
    for(const f of d.folders){
      const members = [];
      for(const v of f.views){ placed.add(v); if(byView.has(v)) members.push(byView.get(v)); }
      folders.push({ key: f.key, label: f.label, icon: f.icon, members, custom: true });
    }
    for(const v of d.order) placed.add(v);
    for(const v of d.hidden) placed.add(v);
    /* The built-in grouping, applied ONLY to what the document left alone. That is what makes a
     * default a default: drag a game out and it stays out, and a game added to the sidebar next
     * month still joins the folder the others are in — even a folder you have since renamed. */
    for(const bf of FOLDERS){
      const rest = bf.views.filter(v => !placed.has(v) && byView.has(v));
      if(!rest.length) continue;
      for(const v of rest) placed.add(v);
      const own = folders.find(f => f.key === bf.key);
      if(own){ for(const v of rest) own.members.push(byView.get(v)); continue; }
      folders.push({ key: bf.key, label: bf.label, icon: bf.icon,
                     members: rest.map(v => byView.get(v)), custom: false });
    }
    const item = f => ({ view: 'folder:' + f.key, label: f.label, icon: f.icon, folder: f });
    const byKey = new Map(folders.map(f => ['folder:' + f.key, f]));
    const items = [], used = new Set();
    for(const key of d.order){
      if(key.indexOf('folder:') === 0){
        const f = byKey.get(key);
        // A folder whose members have all gone is not drawn — same rule the built-in one has always
        // had, so a deployment with the games off sees nothing rather than an empty tile.
        if(f && f.members.length && !used.has(key)){ used.add(key); items.push(item(f)); }
        continue;
      }
      if(used.has(key) || hidden.has(key) || !byView.has(key)) continue;
      used.add(key); items.push(byView.get(key));
    }
    // Anything the order never mentioned, in sidebar order: this is the line that makes a new
    // feature appear on a desktop somebody arranged a year ago.
    for(const a of (list || [])){
      if(used.has(a.view) || hidden.has(a.view)) continue;
      const f = folders.find(x => x.members.indexOf(a) >= 0);
      if(f){ const k = 'folder:' + f.key; if(!used.has(k)){ used.add(k); items.push(item(f)); } continue; }
      used.add(a.view); items.push(a);
    }
    // `pos` only ever describes things that are ON the desktop — a stale entry for a hidden or
    // retired view would otherwise keep a place in the arithmetic that decides where windows open.
    const pos = {};
    for(const it of items) if(d.pos[it.view]) pos[it.view] = d.pos[it.view];
    return { items, folders, hidden: [...hidden].map(v => byView.get(v)), pos, bg: d.bg,
             widgets: Array.isArray(d.widgets) ? d.widgets : [] };
  }

  let _lay = null;
  function layout(){
    // An account switch keeps the module alive; drawing the previous account's desktop (or worse,
    // saving it back under the new key) is not a hypothetical — the switcher never reloads.
    const pk = (me() || {}).pubkey || '';
    // The signer-retry budget belongs to the ACCOUNT, not the page: switching accounts asks a
    // different key to decrypt a different document, and that deserves its own second chance.
    if(pk !== _docPk){ _doc = null; _docAt = 0; _wr = false; _layWhy = ''; _signerRetried = 0;
                       _noticeSaid = ''; _docPk = pk; unwatchLayout(); }
    _lay = computeLayout(launchApps(), _doc);
    return _lay;
  }

  /* What the desktop and start menu SHOW: the flat list with each folder's members collapsed into
   * one entry. apps() itself stays flat on purpose — routeView, openApp and the window bookkeeping
   * all key on real view names, and a folder is a presentation detail, not a place things live. So
   * switchView('chess') still opens a Chess window, from anywhere, folder or no folder. */
  function launcherItems(){ return layout().items; }

  // ---- reading and writing the document ---------------------------------------------------------

  /* A REMOTE SIGNER CAN SIMPLY NOT ANSWER, and that has to be survivable.
   *
   * With Amber (NIP-55) or nsec.app (NIP-46) the decryption is not local: it is a request to another
   * app, or to a relay the signer may not be listening on. A declined prompt rejects and a timed-out
   * relay round trip rejects, but a signer that is asleep, unpaired, or waiting on a notification
   * nobody has tapped yet does neither — the promise stays pending, for ever.
   *
   * That is exactly what "Tablet says: still loading your desktop layout after a long time" is,
   * while the same account behaves on a laptop with a local key: `await nip44dec(...)` never settles,
   * so loadLayout never settles, so the write gate is never decided and every drag repeats the same
   * message. Bounding it turns a hang into an ANSWER — an unreadable document, which the caller
   * already knows how to talk about and retry from. 25 seconds is long enough to pick up a phone and
   * approve a prompt, and short enough that a desktop is not stuck for a session over it. */
  function _bounded(p, ms){
    return new Promise((resolve, reject) => {
      let done = false;
      const t = setTimeout(() => { if(!done){ done = true; reject(new Error('signer timeout')); } }, ms);
      Promise.resolve(p).then(
        (v) => { if(!done){ done = true; clearTimeout(t); resolve(v); } },
        (e) => { if(!done){ done = true; clearTimeout(t); reject(e); } });
    });
  }
  async function _decode(ev){
    if(!ev || !ev.content) return null;
    try{ return _normDoc(JSON.parse(await _bounded(PC().nip44dec(me().pubkey, ev.content), 25000))); }
    catch(_){ return null; }   // not ours, or not decryptable here — never read as "no layout"
  }

  function loadLayout(){
    if(!me() || !PC().nip44dec || !RelayOf()) return Promise.resolve(null);
    const pk = me().pubkey;
    /* Shared per IDENTITY, not globally. A read that finds nothing retries for over a second (a
     * first REQ at a still-warming socket EOSEs empty, and treating that as "no layout" is the
     * whole reason `_wr` exists), which is long enough for the account switcher to run inside it.
     * Handing the new account that in-flight read means it finishes, sees the pubkey has moved,
     * discards its result — and nothing ever reads the layout again for the rest of the session.
     * The desktop then draws the default order for an account that has one, which is exactly the
     * failure that looks like the layout was lost. */
    if(_layLoading && _layLoadingPk === pk) return _layLoading;
    _layLoadingPk = pk;
    _layLoading = (async () => {
      // The local cache first, so a desktop this device arranged draws right away rather than
      // flashing the default order for as long as the network takes.
      try{
        const cached = (StoreOf() && StoreOf().query([LFILTER()])) || [];
        const c = cached.sort((a, b) => (b.created_at || 0) - (a.created_at || 0))[0];
        if(c && c.created_at > _docAt){
          const d = await _decode(c);
          if(d && pk === ((me() || {}).pubkey || '')){ _doc = d; _docPk = pk; _docAt = c.created_at; refreshIcons(); }
        }
      }catch(_){}
      /* A query fired at a socket that is still CONNECTING is silently dropped, so waiting for the
       * pool first is half of not mistaking "nobody answered" for "you have no layout". */
      try{ if(RelayOf().ready) await RelayOf().ready(); }catch(_){}
      let ev = null, answered = false;
      for(let a = 0; a < 3 && !ev; a++){
        if(a) await new Promise(r => setTimeout(r, 450 * a));
        let got = [], threw = false;
        try{ got = await RelayOf().query([LFILTER()]) || []; }catch(_){ threw = true; }
        /* `complete`, NOT the absence of a throw — the other half, and the one that made the
         * identical guard in vault.js DEAD CODE until it was found. Relay.query() has no reject
         * path at all: when no relay EOSEs it RESOLVES with [] and marks the array
         * `complete:false`. Catching an exception here proves nothing, so a zombie socket after a
         * resume would read as "this account has never arranged its desktop", arm the writer, and
         * the first icon dragged would publish the DEFAULTS over the real document — on every
         * device, since the event is addressable. An empty array from the catch carries no
         * `complete` marker, which is why the throw is tracked separately. */
        if(!threw && got.complete !== false) answered = true;
        ev = got.sort((x, y) => (y.created_at || 0) - (x.created_at || 0))[0] || null;
      }
      if(pk !== ((me() || {}).pubkey || '')) return _doc;      // signed out / switched mid-read
      let unreadable = false;
      if(ev && ev.created_at >= _docAt){
        const d = await _decode(ev);
        if(d){ _doc = d; _docAt = ev.created_at; }
        // A document that ARRIVED and would not decrypt is the one case that must never reach the
        // line below: an Amber/NIP-46 prompt that timed out or was dismissed leaves a perfectly
        // good layout on the relay, and treating it as "no layout" would publish the defaults over
        // it at the first drag. `_decode` says as much; this is what makes that true of the caller.
        else unreadable = true;
      }
      // (1) Only a relay that ANSWERED makes this safe to write. Until then the desktop draws, and
      // refuses to save — the alternative publishes the defaults over the layout it could not read.
      /* WHY it is not writable, kept for the message. All three of these arrive as "still loading
       * your desktop layout", for ever, and they are three different problems with three different
       * answers: a relay that never EOSEs is a connection, a document that will not decrypt is the
       * SIGNER (an Amber prompt that timed out is the common one, and it is not going to fix itself
       * by waiting), and no key at all is a guest. Reported from a tablet that sat on the first
       * message indefinitely while the same account arranged itself fine on two other devices —
       * which is a fact about that device, and the message has to be able to say which one. */
      _layWhy = !answered ? 'relay' : unreadable ? 'signer' : '';
      if(answered && !unreadable){ _wr = true; if(!_doc){ _doc = BLANK(); _docPk = pk; } }
      /* KEEP TRYING while the signer is the problem, on a ladder — it used to be ONE retry at 20s.
       *
       * The usual cause was assumed to be a prompt not approved YET (a phone face down, a
       * notification tapped a minute later), and one retry covers that. It does not cover the case
       * that actually happened: a NIP-46 transport that was down for the whole morning, because the
       * page's relay socket died when the machine slept and nothing redialled it. Both retries fell
       * inside that window, so the desktop drew the DEFAULT for the rest of the session and it was
       * reported as "all my desktop widgets disappeared, on all devices" — with the document sitting
       * intact on the relay the entire time.
       *
       * Bounded, and only while `_wr` is still false: a signer that is genuinely unreachable must
       * not become a prompt loop, and the moment one read succeeds the ladder stops. */
      if(_layWhy === 'signer' && _signerRetried < _SIGNER_RETRY_AT.length){
        const at = _SIGNER_RETRY_AT[_signerRetried++];
        setTimeout(() => { if(!_wr && me()) loadLayout().catch(() => {}); }, at);
      }
      refreshIcons(); watchLayout(); arrangeHint(); layoutNotice();
      return _doc;
    })();
    const p = _layLoading;
    // Cleared when it settles, not awaited here: callers must be able to fire this and carry on
    // drawing, and a second call while it is in flight has to share the one read.
    p.catch(() => {}).then(() => { if(_layLoading === p) _layLoading = null; });
    return p;
  }

  /* AN EMPTY DESKTOP IS INDISTINGUISHABLE FROM A LOST ONE, AND THAT SILENCE IS THE WHOLE BUG.
   *
   * When the layout cannot be read the desktop draws the defaults: no widgets, icons back in their
   * original order. Nothing said so. Reported as "all my desktop widgets disappeared, on ALL
   * devices" — which is the reasonable reading of what is on screen, and completely wrong: the
   * document was on the relay the whole time, and `_wr` had already refused every write so nothing
   * could have overwritten it. The user spent that time believing their arrangement was gone.
   *
   * So say which of the two it is, and say that nothing has been changed — the second half matters
   * more than the first. Once per reason per account: the retry ladder must not repeat it. */
  function layoutNotice(){
    try{
      if(!on || _wr || !_layWhy) return;
      if(_noticeSaid === _layWhy) return;
      _noticeSaid = _layWhy;
      PC().toast && PC().toast(_layWhy === 'signer'
        ? 'Showing the default desktop — your signer did not answer, so your arrangement could not '
          + 'be decrypted. It is still saved; nothing has been changed.'
        : 'Showing the default desktop — no relay answered, so your arrangement could not be '
          + 'loaded. It is still saved; nothing has been changed.');
    }catch(_){}
  }

  /* Said once, to people who have never arranged anything. A desktop that CAN be rearranged and
   * never says so is a desktop nobody rearranges — dragging an icon is not something anyone tries on
   * a web page unbidden. Deliberately the ordinary toast rather than the desktop's own notification
   * card: this is a tip that disappears, not an event, and osToast plays the arrival sound. */
  function arrangeHint(){
    try{
      if(!on || !_wr || !_doc) return;
      if(_doc.order.length || _doc.folders.length || _doc.hidden.length) return;
      if(settings().get('osArrangeHintSeen', false)) return;
      settings().set('osArrangeHintSeen', true);
      PC().toast && PC().toast('Drag the icons to arrange your desktop — drop one on another to '
                             + 'make a folder. Right-click for the rest.');
    }catch(_){}
  }

  // Live, so rearranging the desktop on the laptop rearranges it on the tablet without a reload.
  // `since` only: the full filter would replay the document as the opening batch and decrypt it a
  // second time straight after loadLayout has just done it.
  function watchLayout(){
    if(_laySub || !me() || !RelayOf() || !RelayOf().subscribe) return;
    try{
      const f = Object.assign(LFILTER(), { since: Math.floor(Date.now() / 1000) - 120 });
      delete f.limit;
      _laySub = RelayOf().subscribe([f], { live: true, onEvent: async (ev) => {
        if(!ev || ev.created_at <= _docAt) return;
        const d = await _decode(ev);
        if(!d) return;
        _doc = d; _docAt = ev.created_at;
        refreshIcons();
      }});
    }catch(_){ _laySub = null; }
  }
  function unwatchLayout(){ if(_laySub){ try{ RelayOf().close(_laySub); }catch(_){} _laySub = null; } }

  function saveLayout(next){
    const prev = _doc, prevAt = _docAt;
    // WHOSE layout this is, captured now. The publish runs at the back of a queue, and reading the
    // identity again when it finally does would encrypt one account's arrangement to whoever is
    // signed in by then — and write it to THEIR `pcai:desktop`, replacing theirs.
    const pk = (me() || {}).pubkey || '';
    _doc = next;                       // optimistic: the icon lands where it was dropped…
    refreshIcons();
    const done = _layChain.catch(() => {}).then(async () => {
      if(!pk || pk !== ((me() || {}).pubkey || '')) throw new Error('the account changed');
      const ct = await PC().nip44enc(pk,
        JSON.stringify(Object.assign({}, next, { updated: Math.floor(Date.now() / 1000) })));
      // noQueue: publish()'s Outbox refuses replaceable kinds on purpose (blind replay is what
      // caused the follows wipe), and saying so here keeps this from depending on that.
      const r = await PC().publish(LAY_KIND, ct, [['d', D_LAY]], { quiet: true, noQueue: true });
      if(!(r && r.ok)) throw new Error('relay rejected the layout');
      if(r.ev) _docAt = r.ev.created_at;
    });
    _layChain = done.catch(() => {});   // one failure must not poison every later write
    done.catch(() => {                  // …and goes back if the write did not land, out loud.
      /* Unless something newer has been applied since. A second drag builds its document FROM this
       * one, so if that write landed the relay already holds this change — rolling back here would
       * undo, on screen only, something the relay has. */
      if(_doc !== next) return;
      _doc = prev; _docAt = prevAt; refreshIcons();
      try{ PC().toast('couldn’t save the desktop layout — that change is not stored'); }catch(_){}
    });
    return done;
  }

  // ---- the arrangement itself -------------------------------------------------------------------

  // Every mutation is expressed against the layout ON SCREEN, so what gets written is what the user
  // is looking at — the document is sparse (it holds decisions, not the app list), and a reorder has
  // to materialise the visible order or it would be describing a desktop nobody has seen.
  const _orderNow = lay => lay.items.map(a => a.view);
  const _pluck = (doc, view) => { for(const f of doc.folders) f.views = f.views.filter(v => v !== view); };

  // A built-in folder exists only as a default until you change it; the moment you do, it becomes a
  // real entry in the document with the members it had on screen.
  function _materialise(doc, lay, key){
    let f = doc.folders.find(x => x.key === key);
    if(f) return f;
    const c = lay.folders.find(x => x.key === key);
    if(!c) return null;
    f = { key, label: c.label, icon: c.icon, views: c.members.map(m => m.view) };
    doc.folders.push(f);
    return f;
  }

  /* A folder holding one app is not a folder. The phone home screens this borrows its gestures from
   * dissolve one, and the alternative here is a tile you have to open to reach a single icon. The
   * survivor takes the folder's OWN place in the order rather than being appended — otherwise
   * dragging the second-to-last app out of a folder flings the last one to the end of the desktop,
   * which reads as an icon that moved on its own. */
  function _collapse(doc){
    for(const f of doc.folders.slice()){
      if(f.views.length > 1) continue;
      doc.folders = doc.folders.filter(x => x !== f);
      const tag = 'folder:' + f.key;
      const at = doc.order.indexOf(tag);
      const rest = doc.order.filter(k => k !== tag);
      if(f.views.length) rest.splice(at < 0 ? rest.length : at, 0, f.views[0]);
      doc.order = rest;
    }
  }

  function _apply(fn){
    if(!me()) return Promise.resolve(false);
    /* layout() FIRST, then the gate. It is layout() that notices the account has changed and drops
     * the previous one's document — checking `_wr` before it means the write gate belongs to the
     * account you have just switched away from, and the first drag after a switch would publish a
     * defaults-derived layout over the NEW account's real one. */
    const lay = layout();
    if(!_wr){
      /* Not "it didn't work" — the layout has not been READ yet, and saying so is the difference
       * between a desktop that looks broken and one that is still waking up. But "in a moment" is a
       * promise, and two of the three reasons never come good on their own: name them, so somebody
       * on the device it is happening to can act instead of waiting. */
      try{
        PC().toast(_layWhy === 'signer'
          ? 'your desktop layout is on the relay but this device could not decrypt it — approve the '
            + 'request in your signer (Amber/nsec.app), then try again'
          : _layWhy === 'relay'
            ? 'no relay has answered with your desktop layout yet — check Settings → Relays; nothing '
              + 'will be saved until one does'
            : 'still loading your desktop layout — try that again in a moment');
      }catch(_){}
      loadLayout().catch(() => {});
      return Promise.resolve(false);
    }
    const doc = _clone(_doc || BLANK());
    if(fn(doc, lay) === false) return Promise.resolve(false);
    _collapse(doc);
    return saveLayout(_normDoc(doc)).then(() => true, () => false);
  }

  // Onto the desktop, at a position — which is also how a member LEAVES a folder.
  function toDesk(dragKey, targetKey, after){
    return _apply((doc, lay) => {
      const order = _orderNow(lay).filter(k => k !== dragKey);
      _pluck(doc, dragKey);
      let i = targetKey ? order.indexOf(targetKey) : -1;
      if(i < 0) order.push(dragKey);
      else order.splice(after ? i + 1 : i, 0, dragKey);
      doc.order = order;
    });
  }

  // Into a folder, at a position — same-folder reordering and moving between folders are this.
  function toFolder(destKey, dragKey, targetKey, after){
    return _apply((doc, lay) => {
      if(!dragKey || dragKey.indexOf('folder:') === 0) return false;   // no folders inside folders
      const f = _materialise(doc, lay, destKey);
      if(!f) return false;
      _pluck(doc, dragKey);
      const views = f.views;
      const i = targetKey ? views.indexOf(targetKey) : -1;
      if(i < 0) views.push(dragKey);
      else views.splice(after ? i + 1 : i, 0, dragKey);
      doc.order = _orderNow(lay).filter(k => k !== dragKey);
    });
  }

  /* Dropped one icon on another: a new folder in the target's place, holding both. Returns the new
   * folder's key so the caller can offer to name it — the folder is created FIRST, with a default
   * name, because a drop that waits on a dialog is a drop that a cancelled dialog throws away. */
  function mergeInto(dragKey, targetKey){
    if(targetKey.indexOf('folder:') === 0) return toFolder(targetKey.slice(7), dragKey, null, false).then(() => '');
    let made = '';
    return _apply((doc, lay) => {
      if(!dragKey || dragKey === targetKey || dragKey.indexOf('folder:') === 0) return false;
      made = 'u' + Math.random().toString(36).slice(2, 8) + Date.now().toString(36).slice(-4);
      _pluck(doc, dragKey); _pluck(doc, targetKey);
      doc.folders.push({ key: made, label: 'New folder', icon: '#i-folder', views: [targetKey, dragKey] });
      const order = _orderNow(lay).filter(k => k !== dragKey);
      const at = Math.max(0, order.indexOf(targetKey));
      doc.order = order.filter(k => k !== targetKey);
      doc.order.splice(at, 0, 'folder:' + made);
    }).then(ok => ok ? made : '');
  }

  function renameFolder(key, label){
    const name = String(label || '').trim();
    if(!name) return Promise.resolve(false);
    return _apply((doc, lay) => {
      const f = _materialise(doc, lay, key);
      if(!f) return false;
      f.label = name.slice(0, 60);
    });
  }

  // Take a folder apart: its members go back on the desktop where the folder was. They are listed
  // EXPLICITLY, because a built-in folder would otherwise re-form from the same unplaced members.
  function ungroup(key){
    return _apply((doc, lay) => {
      const f = lay.folders.find(x => x.key === key);
      if(!f) return false;
      const order = _orderNow(lay);
      const at = order.indexOf('folder:' + key);
      const views = f.members.map(m => m.view);
      doc.folders = doc.folders.filter(x => x.key !== key);
      const rest = order.filter(k => k !== 'folder:' + key);
      rest.splice(at < 0 ? rest.length : at, 0, ...views);
      doc.order = rest;
    });
  }

  // Hidden from the DESKTOP, not from the app: it stays in the start menu, which is where every
  // desktop puts the things that are not on the desktop, and is the way back.
  function hideItem(view){
    return _apply((doc, lay) => {
      if(!view || view.indexOf('folder:') === 0) return false;
      _pluck(doc, view);
      doc.order = _orderNow(lay).filter(k => k !== view);
      if(doc.hidden.indexOf(view) < 0) doc.hidden.push(view);
    });
  }
  function showItem(view){
    return _apply((doc, lay) => {
      if(doc.hidden.indexOf(view) < 0) return false;
      doc.hidden = doc.hidden.filter(v => v !== view);
      doc.order = _orderNow(lay).concat([view]);
    });
  }

  /* PUT AN ICON WHERE IT WAS DROPPED.
   *
   * The first free move SEEDS every other icon with the position it already has on screen. Without
   * that, moving one icon would switch the desktop from grid to free layout and everything else
   * would jump to wherever the free layout happened to put it — the user moved one icon and the
   * whole desktop rearranged itself. Seeding from the measured grid means nothing else moves at all.
   */
  function placeIcon(view, x, y, seed){
    return _apply((doc, lay) => {
      if(!view) return false;
      doc.pos = Object.assign({}, doc.pos);
      if(!Object.keys(doc.pos).length && seed) for(const k in seed) doc.pos[k] = seed[k];
      _pluck(doc, view);                                   // out of any folder it was dragged from
      if(doc.order.indexOf(view) < 0 && lay.items.every(a => a.view !== view)) doc.order = _orderNow(lay).concat([view]);
      // Snapped to 8px. Free placement people actually want is "roughly here, tidily" — pixel-exact
      // means two icons never quite line up with each other.
      doc.pos[view] = [Math.max(0, Math.round(x / 8) * 8), Math.max(0, Math.round(y / 8) * 8)];
    });
  }
  // …and back to a grid. The opposite of the above, and the way out of a desktop somebody has made a
  // mess of without losing their folders or their hidden icons.
  function lineUp(){
    return _apply((doc) => {
      if(!Object.keys(doc.pos || {}).length) return false;
      doc.pos = {};
    });
  }

  // The wallpaper: a picture from the drive's `Backgrounds` folder, or '' for the default emblem.
  function setWallpaper(sha){
    return _apply((doc) => { doc.bg = /^[0-9a-f]{64}$/i.test(String(sha || '')) ? String(sha).toLowerCase() : ''; });
  }

  function resetLayout(){
    return _apply((doc) => { doc.folders = []; doc.order = []; doc.hidden = []; doc.pos = {}; });
  }

  // Who is signed in. NOT window.ME — the client keeps ME inside its IIFE, so window.ME is undefined
  // for every module out here whoever is logged in. Gating on it hid all of this from everyone.
  const me = () => { try{ return (PC().me && PC().me()) || null; }catch(_){ return null; } };

  const EXTRAS = [
    { view: '__profile', label: 'My Profile', icon: '#i-user', act: () => PC().openProfile && PC().openProfile(),
      when: () => !!(me() && PC().openProfile) },
    /* Music opens as a WINDOW, not as a bare "start playing" action — it is a library you browse,
     * and the floating player bar is the transport, not the app. The window renders the same Files →
     * 🎵 Music view the classic UI uses, so there is one music library and not two. */
    { view: '__music', label: 'Music', icon: '#i-music',
      act: () => openDoc('music', 'Music', 'i-music',
                         () => { try{ PC().renderMusicApp && PC().renderMusicApp(); }catch(_){} }),
      when: () => !!(me() && PC().renderMusicApp) },
    { view: '__golive', label: 'Go Live', icon: '#i-live', act: () => PC().goLive && PC().goLive(),
      when: () => !!(me() && PC().goLive) },
  ];

  /* The start button wears the instance's own logo — the same image the sidebar brand shows, read
   * live rather than hardcoded, so a deployment that set a custom logo (Admin → Site) gets ITS mark
   * on the start button instead of PosterChan's. */
  function brandLogo(){
    const img = document.querySelector('.brand-logo');
    const src = img && (img.getAttribute('src') || img.src);
    return src || '/static/posterchan-relay.png';
  }

  /* The sidebar's own <use href> values carry the '#', but every hand-written call site here passes
   * a bare id — and `<use href="i-wot">` resolves to NOTHING and draws nothing, with no error. That
   * is why the start-menu stat icons and the Post/Profile/Search window-title icons were blank. Take
   * either form. */
  const iconSvg = (href) => {
    const h = String(href || '').trim();
    const id = !h ? '#i-app' : (h.charAt(0) === '#' ? h : '#' + h);
    return `<svg class="ic" aria-hidden="true"><use href="${enc(id)}"></use></svg>`;
  };

  // ---- the feed handoff -----------------------------------------------------------------------

  /* MOVE THE ELEMENT, not the id.
   *
   * The first version of this gave each window its own body and handed `id="feed"` to whichever was
   * focused, so `$('#feed')` resolved there. That is only half of it: this client also binds
   * DELEGATED listeners once, at boot, to the feed ELEMENT — a click handler (app.js), the
   * infinite-scroll handler, the pull-to-refresh touch handlers. Those stay on the node they were
   * attached to. Renaming ids left every feature rendering into a window whose clicks nothing was
   * listening for, which is exactly "none of the app buttons work".
   *
   * So the real feed element is RELOCATED into the focused window. appendChild preserves listeners,
   * so every delegated handler comes with it and each feature behaves exactly as it does in the
   * classic client. Only one window can hold it — which was always true, since only one feature is
   * live at a time — so the window losing it keeps a static snapshot of what it was showing, and
   * refocusing moves the real element back and re-renders.
   */
  /* PARK this window's content — MOVE the real nodes into its own slot, never a copy of their HTML.
   *
   * This used to be `slot.innerHTML = realFeed.innerHTML`, and that one line is where the desktop's
   * three worst papercuts came from. A serialised copy is DEAD: no event handlers, no scroll
   * position, and no memory of where you had navigated inside the view. So a background window
   * showed a picture of itself, the first click on it did nothing at all, and focusing it re-rendered
   * the view from scratch — landing you at the top of the default screen. "Click a post and it just
   * loads the window again", "you lose your spot", and "open a thread, come back, it's the top of the
   * catalog again" are all the same line.
   *
   * appendChild MOVES a node and keeps its listeners, so parking and unparking is lossless and the
   * window keeps showing exactly what it showed. Nothing has to be re-rendered to bring it back. */
  function snapshot(w){
    if(!w || !realFeed || realFeed.parentElement !== w.body) return;
    const slot = w.slot;
    if(!slot) return;
    w.scrollTop = realFeed.scrollTop || 0;
    w.feedClass = realFeed.className;      // .feed-ai/.feed-chat etc. decide how it scrolls
    /* …and WHICH VIEW the client believes it is showing, so refocusing can hand that back.
     *
     * The client keeps ONE `VIEW` global and every painter keys on it — flushLive, _drawTimeline
     * and onFeedScroll all ask "am I the timeline?" and then paint into `$('#feed')`, wherever that
     * element currently lives. So a window holding the feed while VIEW names a DIFFERENT window's
     * feature is not a cosmetic mismatch: the timeline prepends live posts, redraws on EOSE and
     * paginates on scroll straight into a Profile window. That is the reported "opened a profile in
     * a new window and timeline posts started filling it in".
     *
     * Read at PARK time rather than mapped from w.view, because the two genuinely differ: a doc
     * window is a thread/profile/music library with no view name of its own, and a feature window
     * navigated INSIDE itself (a hashtag or a search opened from the timeline) is no longer showing
     * the view it was opened as. */
    try{ const v = PC().VIEW; if(v) w.appView = v; }catch(_){}
    slot.innerHTML = '';
    while(realFeed.firstChild) slot.appendChild(realFeed.firstChild);
    slot.scrollTop = w.scrollTop;
    w.parked = true;
  }

  /* The slot of a PARKED window that is currently showing `view`.
   *
   * app.js needs this to deliver live timeline posts to the RIGHT window. Parking MOVES a window's
   * nodes into its slot, so with both Home and Nostrverse open there are two `id="tl-notes"` in one
   * document — and `getElementById` answers with whichever was opened first, which would prepend the
   * firehose into the following-feed (or the reverse) while the window they belong to never updates.
   * Only this module knows which window is which, so the question is answered here rather than
   * guessed from the DOM.
   *
   * Matched on `appView` — what the client reported the window was showing when it was parked —
   * falling back to the view it was OPENED as, because a window navigated inside itself (a hashtag
   * or a search opened from the timeline) is no longer the latter. */
  function parkedSlot(view){
    if(!on || !view) return null;
    const w = wins.find(x => x.parked && x.slot && ((x.appView || x.view) === view));
    return w ? w.slot : null;
  }

  /* Put the feed back where this window left it.
   *
   * Retried rather than set once: a view may await the relay before it has any content, and
   * scrollTop against a zero-height element silently does nothing — which is the difference between
   * "restores your spot" and "looks like it tried". Bounded at ~1s, and abandoned the moment the
   * feed belongs to some other window, so a slow view cannot scroll a window you have left. */
  /* A parked window's slot is LIVE, not a frozen picture — app.js prepends new timeline posts into
   * it while it sits there, correcting the slot's own scrollTop so the reading position holds. The
   * offset captured at park time knows nothing about that, and replaying it on restore would land
   * several posts away from where the user actually was. So whoever moves content in says so. */
  function noteScroll(slot){
    if(!slot) return;
    const w = wins.find(x => x.slot === slot);
    if(w && w.parked) w.scrollTop = slot.scrollTop || 0;
  }

  function restoreScroll(w){
    // The slot's own offset wins while it is parked: it is the one that has been kept up to date.
    const want = (w && w.parked && w.slot ? w.slot.scrollTop : 0) || (w && w.scrollTop) || 0;
    if(!want || !realFeed) return;
    let tries = 0;
    const put = () => {
      if(++tries > 40 || !realFeed || realFeed.parentElement !== w.body) return;
      if(realFeed.scrollHeight > realFeed.clientHeight){
        realFeed.scrollTop = want;
        if(Math.abs(realFeed.scrollTop - want) <= 2) return;   // landed (or clamped to the bottom)
      }
      setTimeout(put, 25);
    };
    put();
  }

  /* WHOEVER TAKES THE FEED DECIDES WHETHER IT IS VISIBLE.
   *
   * The admin panel does not render INTO the feed — it hides it (`feed.style.display='none'` in
   * app.js's _adminFrame) and shows a persistent iframe host beside it. That inline style lives on
   * the element, and on the desktop there is exactly ONE #feed that every window borrows in turn.
   * app.js clears it only when it RENDERS a non-admin view, and a window restored from its park
   * deliberately skips the repaint (its nodes are still live — that is the whole point of parking).
   *
   * So closing the Admin window handed the next window a display:none feed full of its own restored
   * content and never drew it: "closing Admin Panel makes the Social window black". Nothing was lost
   * and nothing threw — the element was simply invisible. Decide it here, at the one place the feed
   * changes hands, rather than relying on a repaint that is allowed not to happen. */
  function _feedVisibleFor(view){
    if(!realFeed) return;
    const admin = (view === 'admin');
    realFeed.style.display = admin ? 'none' : '';
    const ah = document.getElementById('admin-host');
    if(!ah) return;
    if(!admin){ ah.style.display = 'none'; return; }
    /* AND THE WAY BACK IN. The first version of this only did the leaving half — hide the host, show
     * the feed — which fixed the window that came after Admin and left Admin itself broken:
     *
     *   focus another window  → this runs for that view, hides #admin-host
     *   focus Admin again     → this hides the FEED (correct) and never re-shows the host
     *   ⇒ both halves hidden, so the Admin window is black. Every time. Reported as "moving the
     *     window makes it black; changing windows makes it not black; going back makes it black".
     *
     * The host is re-parented only when it is genuinely in the wrong place, and that condition is
     * load-bearing rather than defensive: MOVING AN IFRAME IN THE DOM RELOADS IT. This panel is
     * created once and kept alive on purpose — "reloading on every enter made the panel slow +
     * flickery and re-ran all its fetches" — so an unconditional appendChild here would reload the
     * whole admin app on every focus, which on screen is a black frame over a spinner until it
     * finishes. In the ordinary two-window case nothing moves: the host is a sibling of the feed
     * INSIDE the window body, so it stays there while the feed visits another window and is already
     * in place when the feed comes back. */
    const home = realFeed.parentElement;
    if(home && ah.parentElement !== home) home.appendChild(ah);
    ah.style.display = 'block';
  }

  function claimFeed(w){
    if(!realFeed || realFeed.parentElement === w.body){ _feedVisibleFor(w.appView || w.view); return; }
    const holder = wins.find(x => realFeed.parentElement === x.body);
    if(holder) snapshot(holder);
    w.body.appendChild(realFeed);
    _feedVisibleFor(w.appView || w.view);
    if(w.parked && w.slot){
      // Move this window's own nodes back into the live feed, exactly as they were.
      realFeed.innerHTML = '';
      while(w.slot.firstChild) realFeed.appendChild(w.slot.firstChild);
      if(w.feedClass) realFeed.className = w.feedClass;
      w.parked = false;
      w.restored = true;                            // → focusWin skips the repaint entirely
    } else if(w.slot) w.slot.innerHTML = '';
  }

  /* Send the live feed back to the CLASSIC container.
   *
   * `park` decides whether the holder keeps a copy of what it was showing, and the default is NO —
   * which matters because parking MOVES the nodes out of the feed. Leaving the desktop with parking
   * on would hand classic an EMPTY #feed and rely on the repaint at the end of exit() to refill it;
   * that repaint does run, but "classic renders nothing if anything above it throws" is not a
   * guarantee worth trading away. Only minimise wants the content kept, because that window is
   * coming back. */
  function releaseFeed(park){
    if(!realFeed || !realHome) return;
    const holder = wins.find(x => realFeed.parentElement === x.body);
    if(holder && park) snapshot(holder);
    realHome.appendChild(realFeed);
    // …and the same for the way back to classic: the feed can arrive there hidden by the admin panel
    // (see _feedVisibleFor), which would be a blank main column with nothing to explain it.
    try{ _feedVisibleFor(PC().VIEW); }catch(_){ _feedVisibleFor(''); }
    try{ PC().syncPlayer && PC().syncPlayer(); }catch(_){}   // the music app may have just been unmounted
    /* The admin panel's iframe host is a sibling of the feed and follows it (see _adminFrame). Send
     * it home too, or leaving the desktop strands it in a window that is about to be destroyed —
     * which throws away a loaded panel and forces a reload on the next open.
     *
     * NOT WHEN PARKING. `park` means the window is coming back (a minimise), and moving the host is
     * not free: an iframe that changes parent RELOADS, so doing it on every release re-ran the whole
     * admin app — the black frame and spinner you get while it comes back. Left where it is, the
     * window it belongs to still has it when it returns. */
    if(park) return;
    const ah = document.getElementById('admin-host');
    if(ah && ah.parentElement !== realHome) realHome.appendChild(ah);
  }

  // ---- windows -------------------------------------------------------------------------------

  let repainting = 0;      // >0 while a window is repainting itself; see focusWin

  function focusWin(w, render){
    if(!w) return;
    /* CAPTURE WHAT THIS WINDOW IS SHOWING, BEFORE ANYTHING MOVES.
     *
     * If it already holds the live feed then the client's current VIEW *is* this window's view —
     * and this is the only moment that is true, because claimFeed below may hand the feed elsewhere
     * and the repaint at the end reads `appView`. Doing it here rather than waiting to be told
     * covers the case that produced the bug: a window nobody ever parked, dragged where it sits.
     * (noteView keeps it current while the client navigates; this is the belt to that's braces.) */
    try{
      if(realFeed && realFeed.parentElement === w.body){
        const v = PC().VIEW; if(v) w.appView = v;
      }
    }catch(_){}
    wins.forEach(x => x.el.classList.toggle('focused', x === w));
    w.el.style.zIndex = String(nextZ());
    if(w.min){ w.min = false; w.el.classList.remove('minimised'); }
    if(!w.noFeed) claimFeed(w);   // a folder owns its own contents and must never take the feed
    drawBar();
    // Re-render the feature into ITS window. Cheap for these modules — they hold their own state
    // and repaint from it, which is exactly what leaving and returning to a view already does.
    if(render !== false){
      // A DOCUMENT window (a single post) repaints itself; a FEATURE window repaints by switching
      // the client back to its view. Both are needed because the one live #feed moves between
      // windows on every focus, so whatever it holds must be redrawn on arrival.
      /* A repaint is NOT a navigation. Both paths below end in code that pushes a history entry
       * (_navUrl), so without this every click between two windows added one — and the back button
       * then walked window focus instead of navigation, landing on the profile again and again
       * instead of returning to the timeline. */
      if(w.restored){
        /* The window's REAL DOM is back — handlers, scroll offset, and wherever you had navigated
         * inside it. Nothing needs painting; only the app's bookkeeping has to agree with what is
         * already on screen (nav highlight, view title, the VIEW global that later renders key on).
         *
         * That last one is not merely cosmetic, and it is why a DOC window has to adopt a view too.
         * VIEW is what every painter tests before writing into `$('#feed')` — and `#feed` is this
         * window now. Left naming the window you came FROM, a Profile or Post window quietly became
         * the timeline's canvas: live posts prepended into it, the EOSE redraw rebuilt the composer
         * and tabs over the profile, and scrolling to the bottom paged in older timeline posts.
         *
         * `appView` is what the client actually reported when this window was parked (see snapshot),
         * so it is right for a document, and right for a feature window that was navigated inside
         * itself. Falling back to w.view covers a feature window parked before any of that. */
        w.restored = false;
        const adopt = w.appView || (w.view && w.view.indexOf('doc:') !== 0 ? w.view : null);
        if(adopt){
          repainting++;
          try{ PC().switchView && PC().switchView(adopt, true); }catch(err){ /* bookkeeping only */ }
          finally{ repainting--; }
        }
        restoreScroll(w);
        return;
      }
      repainting++;
      try{
        if(w.render){ try{ w.render(); }catch(err){ /* a stale document is not fatal */ } }
        // `appView` first, exactly as the restored branch above — repaint what the window IS
        // showing, not what it was opened as. See noteView.
        else try{ const v = w.appView || w.view;
                  PC().switchView ? PC().switchView(v) : null; }catch(err){ /* a view that refuses is not fatal */ }
      }finally{ repainting--; }
    }
    restoreScroll(w);   // …and land back where this window was, once its content exists
  }

  let iconSpan = 318;               // width the icon grid actually took; windows open clear of it
  // Mirrors `.osw{min-width;min-height}` in the stylesheet. If they disagree the CSS wins and a
  // window opens LARGER than the area place() clamped it into — i.e. off the edge.
  const MIN_W = 420, MIN_H = 260;

  /* HOW BIG A WINDOW OPENS, and where.
   *
   * The old answer was one size for everything — 72% of the free width and 78% of the height,
   * capped at a hardcoded 1100x760 — which is wrong at both ends of the range it has to serve. On a
   * 2560px display every app opened as a small box in the corner of a large screen; on a 1024x600
   * tablet the cascade (up to six steps of 38px) pushed the sixth window's bottom edge under the
   * taskbar. Either way the first thing you did with a window was resize it.
   *
   * So: measure the free area, then pick a SHAPE for the app that is opening. What a window wants is
   * a property of the app, not of the screen — a mail client and a message list are columns and get
   * taller and narrower; the Meme Builder, the admin panel and Live Translate are workbenches and
   * take the width; a game board is square and looks silly stretched. Everything else is a reading
   * column, which is what a timeline is.
   *
   * The result is always CLAMPED into the free area, cascade included, so a window cannot open
   * partly off-screen — the case that had people dragging a window back before they could use it.
   */
  const WIN_SHAPE = {
    // Workbenches: as wide as they can get.
    // 'terminal' is here because COLUMNS are what a shell needs: 80 of them is the width every
    // piece of unix output was laid out for, and a narrow window wraps `ls -l` and every log line.
    wide: ['meme', 'admin', 'translate', 'stats', 'ai', 'websearch', 'calendar', 'markets', 'sync',
           'terminal'],
    // Columns: a list beside a pane. Width past a point is empty space.
    column: ['messages', 'mail', 'notifications', 'notes', 'drafts', 'bookmarks', 'vault', 'contacts',
             'budget', 'news', 'articles'],
    // Boards. A square-ish window, because the board is square.
    square: ['chess', 'ttt', 'hangman', 'connect4', 'blackjack', 'holdem'],
    /* Games from a .xdc — a 3D viewport, not a workbench and not a board. They want the largest
     * 16:9 rectangle that fits: Half-Life in a column-shaped panel is letterboxed on two sides and
     * unplayable, which is exactly how another client's presentation of the same app was described
     * ("a rectangle and terrible"). `doc:webxdc:<id>` reduces to `webxdc` here. */
    game: ['webxdc'],
  };
  function _shapeOf(view){
    const v = String(view || '').replace(/^doc:/, '').split(':')[0];
    for(const k in WIN_SHAPE) if(WIN_SHAPE[k].indexOf(v) >= 0) return k;
    return 'default';
  }

  function place(i, view){
    /* MEASURE the desktop, do not derive it. `vhL() - TASKBAR` assumes the taskbar is exactly the
     * 48px constant, and it is not — measured at 1280x800, the real desk is 16 LAYOUT px shorter,
     * so a window sized to "the whole area" opened 3px under the taskbar. One getBoundingClientRect
     * per window OPEN is nothing (this is not a per-frame path), and it cannot drift when the bar's
     * padding changes. */
    const k = zf();
    const dr = desk ? desk.getBoundingClientRect() : null;
    const vw = dr && dr.width ? dr.width / k : vwL();
    const vh = dr && dr.height ? dr.height / k : (vhL() - TASKBAR);
    const GAP = 12;
    // The free area: right of the icon column, above the taskbar. Everything below is inside it.
    const ax = iconSpan + GAP, ay = Math.round(vh * 0.04);
    const aw = Math.max(360, vw - ax - GAP), ah = Math.max(260, vh - ay - GAP);
    const shape = _shapeOf(view);
    let w, h;
    if(aw < 900 || ah < 560){
      // A small desktop (a laptop at 1024, a tablet in landscape) has no room to be clever: fill it,
      // and let the user cascade from there. A "nicely sized" window here is just a smaller one.
      w = aw; h = ah;
    }else if(shape === 'wide'){
      w = Math.round(aw * 0.92); h = Math.round(ah * 0.92);
    }else if(shape === 'column'){
      // Wide enough for a list AND its pane, and no wider: a mail client at 1800px is two columns
      // of content and a field of empty panel.
      w = Math.min(Math.round(aw * 0.78), 1080); h = Math.round(ah * 0.94);
    }else if(shape === 'game'){
      /* The biggest 16:9 that fits, capped so it does not become a billboard on a 4K display. The
       * aspect is chosen rather than inherited because the app inside cannot ask for one — a webxdc
       * gets the box it is given and letterboxes whatever is left. */
      const maxW = Math.min(aw * 0.94, 1600), maxH = Math.min(ah * 0.94, 900);
      w = Math.round(Math.min(maxW, maxH * 16 / 9));
      h = Math.round(w * 9 / 16);
      if(h > maxH){ h = Math.round(maxH); w = Math.round(h * 16 / 9); }
    }else if(shape === 'square'){
      const side = Math.min(Math.round(aw * 0.62), Math.round(ah * 0.96), 900);
      w = side; h = side;
    }else{
      // A reading column, scaled to the screen rather than pinned to a number: comfortable line
      // length on a laptop, and on a big display it grows without becoming a billboard.
      w = Math.min(Math.round(aw * 0.66), 1280); h = Math.round(ah * 0.9);
    }
    w = Math.max(MIN_W, Math.min(w, aw));
    h = Math.max(MIN_H, Math.min(h, ah));
    // Cascade — but never off the edge. The step shrinks to whatever room is actually left, and it
    // wraps once there is none, so the sixth window is as usable as the first.
    const step = 34, n = i % 6;
    const roomX = Math.max(0, aw - w), roomY = Math.max(0, ah - h);
    const x = ax + Math.min(n * step, roomX);
    const y = ay + Math.min(n * step, roomY);
    return { x, y, w, h };
  }

  function openApp(view, label, icon, render, noFeed){
    if(view && view.indexOf('folder:') === 0 && !_inFolder){
      const f = layout().folders.find(x => 'folder:' + x.key === view);
      return f ? openFolder(f) : null;
    }
    // Opening EMAIL is what acknowledges unread mail on the tray clock — it used to be Messages,
    // back when the mailbox was a tab inside it. Opening Messages now shows DMs and no mail at all,
    // so acknowledging there would clear a count for something you had not looked at.
    if(view === 'mail'){
      try{ mailAck = (PC().mailUnread && PC().mailUnread()) || 0; }catch(_){}
    }
    const extra = EXTRAS.find(x => x.view === view);
    if(extra){ try{ extra.act(); }catch(err){ try{ PC().toast('could not open ' + extra.label); }catch(_){} } return null; }
    const existing = wins.find(w => w.view === view);
    if(existing){ focusWin(existing); return existing; }
    const app = apps().find(a => a.view === view) || {};
    label = label || app.label || view;
    icon = icon || app.icon || '';
    const r = place(wins.length, view);
    const el = document.createElement('div');
    el.className = 'osw';
    el.style.cssText = `left:${r.x}px;top:${r.y}px;width:${r.w}px;height:${r.h}px;z-index:${nextZ()}`;
    el.innerHTML =
      `<div class="osw-bar">
         <span class="osw-ico">${iconSvg(icon)}</span>
         <span class="osw-title">${enc(label)}</span>
         <span class="osw-btns">
           <button class="osw-b" data-w="min" title="Minimise" aria-label="Minimise">–</button>
           <button class="osw-b" data-w="max" title="Maximise" aria-label="Maximise">▢</button>
           <button class="osw-b osw-x" data-w="close" title="Close" aria-label="Close">✕</button>
         </span>
       </div>
       <div class="osw-body"><div class="osw-slot"></div></div>
       <span class="osw-grip" aria-hidden="true"></span>`;
    desk.appendChild(el);
    // noFeed is set BEFORE the focusWin at the end of this function, or that call claims the one live
    // #feed into a window with no use for it — blanking whichever window was actually showing something.
    const w = { id: ++seq, view, title: label, icon, el, body: $('.osw-body', el),
                slot: $('.osw-slot', el), min: false, max: false, rect: r,
                render: render || null, noFeed: !!noFeed };
    wins.push(w);

    $('.osw-bar', el).addEventListener('pointerdown', e => {
      if(e.target.closest('.osw-b')) return;
      focusWin(w, false); startDrag(w, e);
    });
    $('.osw-bar', el).addEventListener('dblclick', e => {
      if(!e.target.closest('.osw-b')) toggleMax(w);
    });
    $$('.osw-b', el).forEach(b => b.onclick = (e) => {
      e.stopPropagation();
      const a = b.dataset.w;
      if(a === 'close') closeWin(w);
      else if(a === 'max') toggleMax(w);
      else minimise(w);
    });
    const maxBtn = $('.osw-b[data-w="max"]', el);
    maxBtn.addEventListener('pointerenter', () => {
      clearTimeout(layoutT);
      layoutT = setTimeout(() => showLayouts(w, maxBtn), 380);
    });
    maxBtn.addEventListener('pointerleave', () => {
      clearTimeout(layoutT);
      layoutT = setTimeout(hideLayouts, 260);
    });
    $('.osw-grip', el).addEventListener('pointerdown', e => { focusWin(w, false); startResize(w, e); });
    /* Just focus. The content of a background window is LIVE now (see snapshot/claimFeed), so the
     * card under the cursor carries its own handler and the click does what it says — no replaying
     * the intent across a re-render, and deliberately no synthetic second click, which would double
     * every action now that the real one lands. */
    el.addEventListener('pointerdown', () => {
      if(!el.classList.contains('focused')) focusWin(w);
    }, true);

    focusWin(w);
    return w;
  }

  // A post opens in its OWN window on the desktop, instead of replacing the timeline underneath it
  // — that is the whole point of having windows. Keyed by id, so clicking the same post twice
  // focuses the window it is already in rather than stacking duplicates.
  /* `noFeed` is forwarded, and it matters for anything that owns its own contents rather than
   * borrowing the timeline. A window without it JOINS THE FEED HAND-OFF: focusing it claims the
   * shared #feed, and focusing anything else takes it away and repaints — which for a running mini
   * app means its iframe is disturbed and the game goes black or starts over, every time you click
   * another window. A folder already opts out for the same reason. */
  /* `rerun` — THE WINDOW IS THIS DOCUMENT NOW, not the one it was opened with.
   *
   * Without it a second search did nothing, which is exactly how it was reported: type a query, get
   * results, type a different query, and the window comes forward still showing the FIRST one. The
   * cause is that `render` is a closure over the query, captured when the window was created: the
   * existing-window branch focused it and `focusWin` re-ran that ORIGINAL closure. So it was not
   * "the search didn't run" — the old search ran again, which looks identical.
   *
   * Every doc window whose key identifies its content (a post is `doc:<id>`) is right as it was: the
   * same key means the same document, so re-running is a repaint. SEARCH is the odd one — one window
   * that shows a succession of different documents — so it, and anything else like it, says so.
   *
   * The new render is called AFTER `focusWin(…, false)` rather than through it, because focusWin has
   * a second path: a window whose nodes were parked is RESTORED from them and the repaint skipped
   * entirely, which would put the old results back on top of the new ones. `scrollTop` is cleared
   * for the same reason — `restoreScroll` spends a second trying to put a fresh result list back at
   * the previous query's offset. */
  function openDoc(key, label, icon, render, noFeed, rerun){
    const view = 'doc:' + key;
    const existing = wins.find(w => w.view === view);
    if(existing){
      if(rerun && render){
        existing.render = render;
        existing.scrollTop = 0;
        focusWin(existing, false);          // forward + claim the feed, WITHOUT repainting the old
        try{ render(); }catch(_){ }
        return existing;
      }
      focusWin(existing);
      return existing;
    }
    return openApp(view, label, icon, render, noFeed);
  }

  /* Route a view switch to that feature's OWN window. Returns true when it has taken over (a window
   * was created and has already repainted itself), false when the caller should paint where it is —
   * which covers both "that window already exists, the feed has been moved into it" and "this view
   * is not something the launcher knows about", where a window would be a surprise. */
  /* WHAT THIS WINDOW IS ACTUALLY SHOWING, kept current as the client navigates inside it.
   *
   * `w.view` is what the window was OPENED as and never changes. That is wrong for any window
   * navigated in place — and the Admin panel is exactly that: it is reached from the SETTINGS view's
   * "Admin panel" button (`switchView('admin')`), and `admin` is not a sidebar app, so routeView
   * declines and the settings window simply paints Admin into itself. Its `w.view` stays 'settings'.
   *
   * `snapshot()` has always captured this at PARK time, which covers a window that lost focus. A
   * window that never lost focus had nothing — so dragging the Admin window repainted it by
   * `switchView('settings')` and dropped you back in User Settings, and `_feedVisibleFor` read
   * 'settings' and hid the admin host, leaving the window black. Both were the same missing fact.
   *
   * Called by renderView on every view change; it lands on whichever window holds the live feed. */
  function noteView(v){
    if(!on || !v || !realFeed) return;
    const w = wins.find(x => realFeed.parentElement === x.body);
    if(w) w.appView = v;
  }

  function routeView(view, focusOnly){
    if(!on || !view) return false;
    if(!apps().some(a => a.view === view)) return false;
    const w = wins.find(x => x.view === view);
    if(w){ focusWin(w, false); return false; }   // already open: claim the feed, let the caller paint
    // Back/forward passes focusOnly: it may bring a window forward, never conjure one.
    if(focusOnly) return false;
    return !!openApp(view);                       // creates it AND repaints through focusWin
  }

  // Focus the window already showing a document, without creating one. Back/forward must never
  // conjure a window: the history entry is a URL, not a user asking for a new frame.
  function focusDoc(key){
    const w = wins.find(x => x.view === 'doc:' + key);
    if(!w) return false;
    focusWin(w, false);            // claim the feed; the caller is about to paint into it
    return true;
  }

  /* A folder window: an icon grid of its members and nothing else. It never holds the feed, so the
   * window you opened it from keeps showing live content behind it. */
  let _inFolder = false;
  function openFolder(f){
    const view = 'folder:' + f.key;
    const existing = wins.find(w => w.view === view);
    if(existing){ focusWin(existing); return existing; }
    let w;
    _inFolder = true;                       // …or openApp would bounce straight back into here
    try{ w = openApp(view, f.label, f.icon, () => {}, true); }
    finally{ _inFolder = false; }
    if(!w) return null;
    w.slot.classList.add('os-folder');
    paintFolder(w, f);
    return w;
  }

  /* The contents of a folder window, repainted from the layout — by openFolder when it opens, and
   * by refreshIcons whenever the document changes, including from another device. The TITLE is
   * repainted too: a folder renamed on the laptop that keeps its old name in the tablet's title bar
   * and taskbar is the same document disagreeing with itself. */
  function paintFolder(w, f){
    if(w.title !== f.label){
      w.title = f.label;
      const t = $('.osw-title', w.el);
      if(t) t.textContent = f.label;
      drawBar();
    }
    w.slot.innerHTML = f.members.length ? f.members.map(iconHtml).join('')
                                        : '<div class="empty">Nothing in here.</div>';
    wireIcons(w.slot, f);
  }

  function closeWin(w){
    const i = wins.indexOf(w);
    if(i < 0) return;
    wins.splice(i, 1);
    // If this window held the id, hand it back BEFORE removing the element, or `$('#feed')` briefly
    // resolves to nothing and whatever renders next paints into a detached node.
    const wasMusic = (w.view === 'doc:music');
    // Closing the Terminal window is what ends its SSH session — renderView deliberately does not,
    // because on the desktop a background window is parked and still running (see the note there).
    if(w.view === 'terminal'){ try{ if(window.PCTerm) PCTerm.unmount(); }catch(_){} }
    /* A window may own something that has to be let go of — a sandboxed mini app holds an iframe and
     * a live relay subscription, and closing the window is the only thing that ends them. Generic on
     * purpose: the Terminal above is the same need answered by name, and a second hardcoded view
     * would make it a pattern of exceptions rather than a hook. */
    if(typeof w.onClose === 'function'){ try{ w.onClose(); }catch(e){ console.warn('window onClose', e); } }
    if(realFeed && realFeed.parentElement === w.body) releaseFeed();
    w.el.remove();
    if(wasMusic){
      // Closing the Music window closes the PLAYER. Anything else and shutting the app just replaces
      // it with the smaller floating one, still playing — which is not what "close" means.
      try{ PC().stopMusic && PC().stopMusic(); }catch(_){}
    }else{
      // Some other window closed; if the music app happened to be inside it the transport comes back.
      try{ PC().syncPlayer && PC().syncPlayer(); }catch(_){}
    }
    const next = wins.filter(x => !x.min).pop();
    if(next) focusWin(next); else drawBar();
  }

  function minimise(w){
    w.min = true;
    w.el.classList.add('minimised');
    if(realFeed && realFeed.parentElement === w.body) releaseFeed(true);   // it is coming back
    const next = wins.filter(x => !x.min).pop();
    if(next) focusWin(next); else drawBar();
  }

  // ---- snapping (Windows 11 style) ------------------------------------------------------------
  // Drag a window against a screen edge and it snaps: the sides give halves, the top maximises, the
  // corners give quarters. A GHOST previews the zone before the pointer is released — a window that
  // jumps somewhere unannounced reads as a bug, not a feature. Hovering Maximise opens the same
  // zones as a menu, which is how Win11 offers them without a drag (and the only way to reach them
  // with a finger, since a touch drag never hovers an edge long enough to be sure of the intent).
  const EDGE = 26;                 // how close to an edge counts as being AT it (in pointer px)

  // The client scales the whole document with body{zoom} on desktop (see --zf), which leaves TWO
  // coordinate spaces in play: pointer events and getBoundingClientRect report ZOOMED css pixels,
  // while style.left/width and offsetWidth are LAYOUT pixels. Mixing them is not cosmetic — at
  // zoom .72 a window drags at 0.72x the speed of the cursor and a "half of the screen" snap covers
  // a third of it (measured: 784px of a 2222px desktop). Everything below works in LAYOUT pixels
  // and converts the viewport and the pointer deltas on the way in.
  function zf(){
    const z = parseFloat(getComputedStyle(document.body).zoom || '1');
    return (z > 0 && isFinite(z)) ? z : 1;
  }
  const vwL = () => window.innerWidth / zf();
  const vhL = () => window.innerHeight / zf();
  let ghost = null, layoutFor = null, layoutT = 0;

  function zones(){
    const vw = vwL(), vh = vhL() - TASKBAR;
    const hw = Math.round(vw / 2), hh = Math.round(vh / 2);
    return { max:  { x: 0,       y: 0,       w: vw, h: vh },
             left: { x: 0,       y: 0,       w: hw, h: vh },
             right:{ x: vw - hw, y: 0,       w: hw, h: vh },
             tl:   { x: 0,       y: 0,       w: hw, h: hh },
             tr:   { x: vw - hw, y: 0,       w: hw, h: hh },
             bl:   { x: 0,       y: vh - hh, w: hw, h: hh },
             br:   { x: vw - hw, y: vh - hh, w: hw, h: hh } };
  }

  function zoneAt(x, y){
    // x/y come straight from a pointer event, so this one comparison stays in pointer pixels.
    const vw = window.innerWidth, vh = window.innerHeight - TASKBAR * zf();
    const L = x <= EDGE, R = x >= vw - EDGE, T = y <= EDGE, B = y >= vh - EDGE;
    if(T && L) return 'tl';
    if(T && R) return 'tr';
    if(B && L) return 'bl';
    if(B && R) return 'br';
    if(T) return 'max';
    if(L) return 'left';
    if(R) return 'right';
    return '';
  }

  function rectOf(z){
    const r = zones()[z];
    return r && { left: (r.x + SNAP) + 'px', top: (r.y + SNAP) + 'px',
                  width: (r.w - SNAP * 2) + 'px', height: (r.h - SNAP * 2) + 'px' };
  }

  function showGhost(z){
    const css = z && rectOf(z);
    if(!css){ hideGhost(); return; }
    if(!ghost){ ghost = document.createElement('div'); ghost.className = 'os-ghost'; desk.appendChild(ghost); }
    Object.assign(ghost.style, css, { display: 'block' });
  }
  function hideGhost(){ if(ghost) ghost.style.display = 'none'; }

  // Remember the floating geometry BEFORE the first snap, so every later restore has something real
  // to go back to — snapping an already-snapped window must not overwrite it with half the screen.
  function keepRect(w){
    if(w.snap || w.max) return;
    w.rect = { x: parseInt(w.el.style.left, 10), y: parseInt(w.el.style.top, 10),
               w: w.el.offsetWidth, h: w.el.offsetHeight };
  }

  function snapTo(w, z){
    const css = rectOf(z);
    if(!css) return;
    keepRect(w);
    w.snap = z;
    w.max = (z === 'max');
    w.el.classList.toggle('maximised', w.max);
    w.el.classList.add('snapped');
    Object.assign(w.el.style, css);
    focusWin(w);
  }

  function unsnap(w){
    if(!w.snap && !w.max) return;
    w.snap = null; w.max = false;
    w.el.classList.remove('maximised', 'snapped');
    Object.assign(w.el.style, { left: w.rect.x + 'px', top: w.rect.y + 'px',
                                width: w.rect.w + 'px', height: w.rect.h + 'px' });
  }

  function toggleMax(w){
    if(w.max || w.snap) unsnap(w); else snapTo(w, 'max');
    focusWin(w);
  }

  // The Snap Layouts flyout: hover (or tap) Maximise to place the window without dragging at all.
  const LAYOUTS = [['left','Left half'], ['right','Right half'], ['max','Full screen'],
                   ['tl','Top left'], ['tr','Top right'], ['bl','Bottom left'], ['br','Bottom right']];

  function hideLayouts(){
    clearTimeout(layoutT);
    desk.querySelectorAll('.os-layouts').forEach(n => n.remove());
    layoutFor = null;
  }

  function showLayouts(w, btn){
    if(layoutFor === w) return;
    hideLayouts();
    layoutFor = w;
    const m = document.createElement('div');
    m.className = 'os-layouts';
    m.innerHTML = LAYOUTS.map(([z, t]) =>
      `<button class="os-lay os-lay-${z}" data-z="${z}" title="${t}" aria-label="${t}"><i></i></button>`).join('');
    desk.appendChild(m);
    const r = btn.getBoundingClientRect(), dr = desk.getBoundingClientRect();
    m.style.left = Math.max(8, Math.min(dr.width - 200, r.left - dr.left - 78)) + 'px';
    m.style.top = (r.bottom - dr.top + 6) + 'px';
    $$('.os-lay', m).forEach(b => b.onclick = (e) => {
      e.stopPropagation();
      snapTo(w, b.dataset.z);
      hideLayouts();
    });
    m.addEventListener('pointerenter', () => clearTimeout(layoutT));
    m.addEventListener('pointerleave', () => { layoutT = setTimeout(hideLayouts, 260); });
  }

  function startDrag(w, ev){
    // Dragging used to write style.left/top on every pointermove. The window CONTAINS the live feed
    // — thousands of nodes — so each move forced a full layout of it, plus a getComputedStyle() for
    // the zoom, on a device with a fraction of a laptop's budget. That is the tablet sluggishness.
    // The gesture now runs entirely on the compositor: a transform per animation frame, committed
    // to left/top once on release. The zoom is read once, since it cannot change mid-drag.
    const k = zf();
    let sx = ev.clientX, sy = ev.clientY;
    let ox = parseInt(w.el.style.left, 10), oy = parseInt(w.el.style.top, 10);
    let curX = ox, curY = oy, zone = '', raf = 0;
    hideLayouts();
    /* A drag must not be able to outlive the button. Three ways it could, all of which end with the
     * window glued to the cursor because `up` never ran:
     *   - the browser starts its OWN drag of the title text/icon, which stops sending pointer events
     *     (preventDefault below, plus user-select:none in the stylesheet);
     *   - the pointer is cancelled rather than released — a gesture the OS claims, a lost capture
     *     (pointercancel and lostpointercapture are treated as a release);
     *   - the pointerup lands somewhere that never reaches us at all (a released MOUSE reports
     *     buttons === 0 on its next move, which is checked below).
     * Only armed for a real button press, so synthetic events — which carry buttons: 0 — still work. */
    try{ ev.preventDefault(); }catch(_){}
    const hadButtons = (ev.buttons || 0) > 0;
    w.el.classList.add('dragging');
    const paint = () => { raf = 0; w.el.style.transform = `translate(${curX - ox}px, ${curY - oy}px)`; };
    const move = (e) => {
      // A released mouse reports buttons === 0 on its next move. Checked FIRST: doing it at the end
      // of the handler still applied one more move, which is the whole symptom.
      if(hadButtons && e.pointerType !== 'touch' && (e.buttons || 0) === 0){ up(); return; }
      // Win11: dragging a snapped or maximised window RESTORES its floating size and picks it up
      // under the cursor, keeping the grab point roughly where it was along the title bar. Dragging
      // a full-width pane around by its corner is the thing that feels broken.
      if((w.snap || w.max) && (Math.abs(e.clientX - sx) > 6 || Math.abs(e.clientY - sy) > 6)){
        const frac = Math.min(0.9, Math.max(0.1,
                       (e.clientX - w.el.getBoundingClientRect().left) / w.el.offsetWidth));
        unsnap(w);
        ox = curX = Math.round(e.clientX / k - w.el.offsetWidth * frac);
        oy = curY = Math.max(0, e.clientY / k - 18);
        sx = e.clientX; sy = e.clientY;
        w.el.style.transform = '';
        w.el.style.left = ox + 'px'; w.el.style.top = oy + 'px';
      }
      // Clamped so a window can never be dragged somewhere it cannot be dragged back from: the title
      // bar stays on screen and above the taskbar.
      curX = Math.max(-w.el.offsetWidth + 120, Math.min(vwL() - 120, ox + (e.clientX - sx) / k));
      curY = Math.max(0, Math.min(vhL() - TASKBAR - 34, oy + (e.clientY - sy) / k));
      if(!raf) raf = requestAnimationFrame(paint);
      const z = zoneAt(e.clientX, e.clientY);
      if(z !== zone){ zone = z; showGhost(zone); }     // only when it CHANGES — not 120 times a second
    };
    let ended = false;
    const up = () => {
      if(ended) return;
      ended = true;
      document.removeEventListener('pointermove', move);
      document.removeEventListener('pointerup', up);
      document.removeEventListener('pointercancel', up);
      window.removeEventListener('blur', up);
      w.el.removeEventListener('lostpointercapture', up);
      if(raf) cancelAnimationFrame(raf);
      w.el.classList.remove('dragging');
      w.el.style.transform = '';
      w.el.style.left = Math.round(curX) + 'px';
      w.el.style.top = Math.round(curY) + 'px';
      hideGhost();
      if(zone) snapTo(w, zone);
    };
    document.addEventListener('pointermove', move);
    document.addEventListener('pointerup', up);
    document.addEventListener('pointercancel', up);
    window.addEventListener('blur', up);          // alt-tabbed away mid-drag
    w.el.addEventListener('lostpointercapture', up);
  }

  function startResize(w, ev){
    if(w.max) return;
    // Resizing by hand means the window is no longer "the left half" — drop the snap, or a later
    // restore yanks it back to a size the user has just replaced by hand.
    if(w.snap){ w.snap = null; w.el.classList.remove('snapped'); }
    ev.preventDefault();
    const sx = ev.clientX, sy = ev.clientY, ow = w.el.offsetWidth, oh = w.el.offsetHeight;
    // A resize really does have to relayout the contents, so the saving here is doing it ONCE per
    // animation frame instead of once per pointer event — a touchscreen fires far more of those.
    const k = zf();
    let nw = ow, nh = oh, raf = 0;
    const paint = () => { raf = 0; w.el.style.width = nw + 'px'; w.el.style.height = nh + 'px'; };
    const move = (e) => {
      nw = Math.max(420, ow + (e.clientX - sx) / k);
      nh = Math.max(260, oh + (e.clientY - sy) / k);
      if(!raf) raf = requestAnimationFrame(paint);
    };
    let ended = false;
    const up = () => { if(ended) return; ended = true;
                       document.removeEventListener('pointermove', move);
                       document.removeEventListener('pointerup', up);
                       document.removeEventListener('pointercancel', up);
                       window.removeEventListener('blur', up);
                       if(raf){ cancelAnimationFrame(raf); paint(); } };
    document.addEventListener('pointermove', move);
    document.addEventListener('pointerup', up);
    document.addEventListener('pointercancel', up);
    window.addEventListener('blur', up);
  }

  // ---- desktop, taskbar, start menu -----------------------------------------------------------

  // Icon tile geometry, in LAYOUT pixels — the same space style.left/width live in. Kept here
  // rather than only in the stylesheet because the column count is computed, not authored.
  const ICON_W = 96, ICON_H = 80, ICON_GAP = 4, ICON_PAD = 14;   // matches .os-icon in the stylesheet

  // How many columns it takes to fit EVERY app in the height available. A fixed three columns fits
  // a 900px laptop and cuts the last rows off a tablet in landscape, where the desktop is short —
  // and an icon you cannot see is an app you cannot open. Width is the other bound: never take more
  // than a third of the desktop, or the launcher starts competing with the windows.
  function iconCols(n){
    const availH = (vhL() - TASKBAR) - ICON_PAD * 2;
    const availW = vwL() / 3;
    const perCol = Math.max(1, Math.floor((availH + ICON_GAP) / (ICON_H + ICON_GAP)));
    const maxCols = Math.max(1, Math.floor((availW + ICON_GAP) / (ICON_W + ICON_GAP)));
    return Math.max(1, Math.min(maxCols, Math.ceil(n / perCol)));
  }

  /* A folder wears its contents, four at a time — the one glyph that says "there are apps in here"
   * without a label having to. Its own <use> ids come from the sidebar, so they are always symbols
   * that exist (the check harness fails on one that does not). */
  const folderGlyph = f =>
    `<span class="os-fold">${f.members.slice(0, 4).map(m => iconSvg(m.icon)).join('')}</span>`;

  const iconHtml = a =>
    `<button class="os-icon${a.folder ? ' is-folder' : ''}" data-view="${enc(a.view)}" title="${enc(a.label)}">
       ${a.folder ? folderGlyph(a.folder) : iconSvg(a.icon)}<span>${enc(a.label)}</span></button>`;

  function drawDesktop(){
    desk.querySelectorAll('.os-icons').forEach(n => n.remove());
    const lay = layout();
    const items = lay.items;
    const grid = document.createElement('div');
    grid.className = 'os-icons';
    grid.innerHTML = items.map(iconHtml).join('');
    const free = Object.keys(lay.pos || {}).length > 0;
    if(free){
      /* FREE PLACEMENT. Once an icon has been moved, the desktop is a canvas rather than a grid —
       * every icon carries its own left/top and the flow layout is out of the way. Positions are in
       * LAYOUT pixels (the same space style.left lives in for windows), and are CLAMPED into the
       * area on every draw: a desktop arranged on a 2560px monitor must not put half its icons off
       * the edge of a laptop that opens the same account. */
      grid.classList.add('os-free');
      const k = zf();
      const dr = desk.getBoundingClientRect();
      const maxX = Math.max(0, (dr.width / k) - ICON_W - 4), maxY = Math.max(0, (dr.height / k) - ICON_H - 4);
      let auto = 0, span = 0;
      $$('.os-icon', grid).forEach(b => {
        const p = lay.pos[b.dataset.view];
        // No saved position (a feature that shipped after the desktop was arranged): drop it into
        // the next free column slot rather than at 0,0 on top of something else.
        const x = p ? Math.min(p[0], maxX) : ICON_PAD;
        const y = p ? Math.min(p[1], maxY) : Math.min(ICON_PAD + (auto++) * (ICON_H + ICON_GAP), maxY);
        b.style.left = x + 'px'; b.style.top = y + 'px';
        span = Math.max(span, x + ICON_W);
      });
      // Windows open clear of the icons — of where they ACTUALLY are now, bounded so a stray icon
      // dropped in the middle of the screen doesn't push every window into the corner.
      iconSpan = Math.min(span + ICON_PAD, Math.round((dr.width / k) / 3));
    }else{
      const cols = iconCols(items.length);
      grid.style.gridTemplateColumns = `repeat(${cols}, ${ICON_W}px)`;
      // Windows open clear of whatever the launcher actually takes, not a hardcoded guess.
      iconSpan = ICON_PAD * 2 + cols * ICON_W + (cols - 1) * ICON_GAP;
    }
    desk.appendChild(grid);
    applyWallpaper(lay.bg);
    drawWidgets();
    wireIcons(grid, null);
    /* The desktop's own menu is bound to the DESK, not to the icon grid: the grid is only as big as
     * the icons in it, and "right-click the empty wallpaper" is where anyone would look for it.
     * Everything with a menu of its own — an icon, a folder window, the menu itself — is left alone,
     * and so is every ordinary window, where the BROWSER's menu (copy, open in new tab, spell
     * check) is the right one and taking it away would be a regression in every feature at once. */
    desk.oncontextmenu = (e) => {
      const t = e.target;
      if(!t || !t.closest || t.closest('.os-icon') || t.closest('.osw') || t.closest('.os-ctx')) return;
      e.preventDefault();
      deskMenu(e.clientX, e.clientY);
    };
  }

  /* Redraw everything the layout decides — the desktop AND any folder window that is open, because
   * they are two views of one document and a change made in either has to show in both. A folder
   * whose last member was just dragged out has nothing left to show, so its window goes with it. */
  function refreshIcons(){
    if(!on || !desk) return;
    drawDesktop();
    for(const w of wins.slice()){
      if(!w.view || w.view.indexOf('folder:') !== 0) continue;
      const f = (_lay || layout()).folders.find(x => 'folder:' + x.key === w.view);
      if(!f || !f.members.length){ closeWin(w); continue; }
      paintFolder(w, f);
    }
  }

  /* Where every desktop icon is at this moment, in layout pixels — the seed for the first free move.
   * Measured from the DOM rather than recomputed from the grid arithmetic, because the grid is CSS
   * and the arithmetic is a guess at what CSS did. */
  function gridPositions(){
    const out = {}, k = zf();
    const g = desk && desk.querySelector('.os-icons');
    if(!g) return out;
    const gr = desk.getBoundingClientRect();
    for(const b of $$('.os-icon', g)){
      const r = b.getBoundingClientRect();
      out[b.dataset.view] = [Math.max(0, Math.round((r.left - gr.left) / k)),
                             Math.max(0, Math.round((r.top - gr.top) / k))];
    }
    return out;
  }

  /* ===== DESKTOP WIDGETS =========================================================================
   *
   * Panels that live ON the desktop rather than in a window. Right-click → Add a widget.
   *
   * PERFORMANCE IS THE DESIGN CONSTRAINT, not a polish pass. Everything on this desktop shares one
   * document with a live timeline in it, and this file has now paid for that lesson three times: the
   * icon drag forcing three layouts per pointer move, the music visualiser measuring `clientWidth`
   * sixty times a second, and a Music library putting 17,000 nodes on screen. A widget is a thing
   * that updates FOREVER, so it is the easiest way yet to reintroduce all three. Hence:
   *
   *   ONE timer for every widget, not one each. `_wgtTick` walks the mounted set and refreshes only
   *   what is due; a desktop with five widgets has one interval, not five.
   *
   *   NOTHING RUNS WHEN NOTHING IS WATCHING. The timer stops when the desktop is left and when the
   *   tab is hidden, and catches up on return. A background tab must cost zero.
   *
   *   ONE FETCH PER SOURCE, shared. Two ticker widgets are one request; `_wgtFeed` holds the
   *   in-flight promise as well as the value, so a burst on wake does not become a burst of requests.
   *
   *   NO LAYOUT READS IN A TICK. Sizes come from the size NAME and the desk rect measured once per
   *   draw, never from `clientWidth` per frame. Updates write text into existing nodes; only a
   *   config change rebuilds a widget's body.
   *
   * The DOCUMENT holds decisions (which widgets, where, what size, their settings) and never their
   * data — the same rule the icon layout follows. A ticker's prices are not part of your desktop.
   * ============================================================================================= */

  const WGT_MAX = 12;
  /* How much text a widget may keep in the LAYOUT document. 400 was chosen for a city name and
   * silently truncated the sticky note to a sentence and a half — on the next load, not on the
   * screen you typed it on, which is the worst place to find out. 4000 is generous for the copy
   * that fills the paper in before Notes has loaded, while leaving the whole document well inside
   * NIP-44's 65535-byte ceiling with a dozen widgets and a full icon arrangement beside it. The
   * authoritative text is in Notes, which has no such bound. */
  const WGT_TEXT_MAX = 4000;
  // Named sizes in layout px, and the FLOOR they collapse to on a small desk. A widget must never be
  // wider than the desktop it is on: this is the whole of "it should resize going from a tablet to a
  // desktop and back".
  const WGT_SIZES = {
    s: { w: 210, h: 118 },
    m: { w: 290, h: 176 },
    l: { w: 380, h: 250 },
  };
  const WGT_GAP = 10;

  // What a widget of this size actually gets, on THIS desk. Never more than 46% of the width or 40%
  // of the height, so a phone-sized desktop cannot be covered by one panel.
  function wgtBox(size, deskW, deskH, def){
    /* A BAR is a shape, not a size. A search box wants to be wide and one line tall — the height of
     * a panel is dead space around a single input, and the width is what makes it usable. So the size
     * name still chooses how wide, and the height comes from the shape. Same clamp: never wider than
     * the desk. */
    if(def && def.bar){
      const wide = { s: 300, m: 420, l: 560 }[size] || 420;
      /* 54, not the 96 the panels floor at: the control inside is 30px and the body padding is 5px,
       * so anything more is a frame of empty widget around one input — which is what it looked like
       * ("reduce the border width around the text input so it's thinner and leaner"). The height of a
       * bar is the height of what it holds. */
      return { w: Math.max(200, Math.min(wide, Math.round(deskW * 0.72))), h: 54 };
    }
    const s = WGT_SIZES[size] || WGT_SIZES.m;
    return { w: Math.max(150, Math.min(s.w, Math.round(deskW * 0.46))),
             h: Math.max(96,  Math.min(s.h, Math.round(deskH * 0.40))) };
  }

  /* A shared, cached read. `ttl` is how long a value stays good; while a fetch is in flight every
   * caller gets THAT promise rather than starting another. Errors are cached briefly too — a
   * failing endpoint must not be retried once per widget per tick. */
  const _wgtFeeds = new Map();
  function _wgtFeed(key, ttl, fetcher){
    const now = Date.now();
    let f = _wgtFeeds.get(key);
    if(f && f.p) return f.p;
    if(f && (now - f.at) < ttl) return Promise.resolve(f.val);
    const p = Promise.resolve().then(fetcher).then(
      val => { _wgtFeeds.set(key, { at: Date.now(), val, p: null }); return val; },
      err => { _wgtFeeds.set(key, { at: Date.now() - Math.max(0, ttl - 15000), val: null, p: null });
               throw err; });
    _wgtFeeds.set(key, { at: (f && f.at) || 0, val: (f && f.val) || null, p });
    return p;
  }

  const _api = (p) => { try{ return (PC().apiBase ? PC().apiBase() : '') + p; }catch(_){ return p; } };
  async function _wgtJson(path){
    const r = await fetch(_api(path));
    if(!r.ok) throw new Error('HTTP ' + r.status);
    return await r.json();
  }

  const _wgtNum = (n, d) => (typeof n === 'number' && isFinite(n)) ? n.toFixed(d) : '–';
  const _mmss = (s) => { s = Math.max(0, Math.floor(s || 0));
    return Math.floor(s / 60) + ':' + String(s % 60).padStart(2, '0'); };

  /* WMO weather codes → a glyph and a word. Open-Meteo reports the code; everything human about it
   * is ours. Grouped rather than enumerated: the distinctions between "slight" and "moderate" drizzle
   * do not survive being drawn at 13px. */
  function _wxDesc(code, day){
    const c = Number(code);
    if(c === 0) return [day ? '☀️' : '🌙', 'Clear'];
    if(c <= 2) return [day ? '🌤️' : '☁️', 'Mostly clear'];
    if(c === 3) return ['☁️', 'Overcast'];
    if(c <= 48) return ['🌫️', 'Fog'];
    if(c <= 57) return ['🌦️', 'Drizzle'];
    if(c <= 67) return ['🌧️', 'Rain'];
    if(c <= 77) return ['🌨️', 'Snow'];
    if(c <= 82) return ['🌧️', 'Showers'];
    if(c <= 86) return ['🌨️', 'Snow showers'];
    return ['⛈️', 'Thunderstorm'];
  }

  /* THE REGISTRY. Each widget is `{label, icon, blurb, every, mount(el, w), refresh(el, w)}`:
   *   mount   — build the body ONCE (and wire its controls).
   *   refresh — update it in place. Called on mount and every `every` ms while the desktop is up.
   *             No `every` means it never polls: the music panel is driven by the player's own
   *             events, and a search box has nothing to poll for.
   * `w` is the stored widget (`cfg` included); `save(w)` persists a cfg change. */
  const WIDGETS = {
    crypto: {
      label: 'Crypto ticker', icon: '#i-chart', blurb: 'Live prices for the coins Markets follows',
      every: 90000,
      mount(el){ el.innerHTML = '<div class="wgt-rows"><div class="wgt-dim">loading…</div></div>'; },
      async refresh(el){
        const box = $('.wgt-rows', el); if(!box) return;
        let d = null;
        try{ d = await _wgtFeed('prices', 75000, () => _wgtJson('/api/markets/prices')); }
        catch(_){ if(!box.dataset.filled) box.innerHTML = '<div class="wgt-dim">prices unavailable</div>'; return; }
        const prices = (d && d.prices) || {};
        const syms = Object.keys(prices);
        if(!syms.length){ box.innerHTML = '<div class="wgt-dim">no prices yet</div>'; return; }
        box.dataset.filled = '1';
        box.innerHTML = syms.map(s => {
          const p = prices[s] || {}, chg = Number(p.chg24h) || 0;
          const usd = Number(p.usd) || 0;
          const shown = usd >= 1 ? usd.toLocaleString(undefined, { maximumFractionDigits: 2 })
                                 : String(usd.toFixed(6)).replace(/0+$/, '').replace(/\.$/, '');
          return `<div class="wgt-row"><span class="wgt-sym">${enc(s)}</span>
            <span class="wgt-val">$${enc(shown)}</span>
            <span class="wgt-chg ${chg >= 0 ? 'up' : 'down'}">${chg >= 0 ? '▲' : '▼'}${enc(Math.abs(chg).toFixed(2))}%</span></div>`;
        }).join('') + (d && d.stale ? '<div class="wgt-dim">last known — upstream is not answering</div>' : '');
      },
    },

    weather: {
      label: 'Weather', icon: '#i-globe', blurb: 'Conditions where you are, refreshed every 10 minutes',
      every: 600000,
      mount(el, w){ el.innerHTML = (w.cfg && w.cfg.lat != null)
        ? '<div class="wgt-wx"><div class="wgt-dim">loading…</div></div>'
        : _wxPickerHtml(); },
      async refresh(el, w, save){
        if(w.cfg.lat == null){ if(!$('.wgt-wxpick', el)) el.innerHTML = _wxPickerHtml(); _wxWire(el, w, save); return; }
        const box = $('.wgt-wx', el); if(!box) return;
        const u = _wxUnits(w);
        let d = null;
        try{ d = await _wgtFeed('wx:' + w.cfg.lat + ',' + w.cfg.lon + ':' + u, 540000,
                                () => _wgtJson('/api/weather?lat=' + encodeURIComponent(w.cfg.lat)
                                                       + '&lon=' + encodeURIComponent(w.cfg.lon)
                                                       + '&units=' + u)); }
        catch(_){ if(!box.dataset.filled) box.innerHTML = '<div class="wgt-dim">weather unavailable</div>'; return; }
        if(!d || !d.ok || !d.now){ box.innerHTML = '<div class="wgt-dim">no reading</div>'; return; }
        box.dataset.filled = '1';
        const unit = (d.units && d.units.temp) || (u === 'imperial' ? '°F' : '°C');
        const [g, word] = _wxDesc(d.now.code, d.now.day);
        const days = (d.days || []).slice(1, 3).map(x => {
          const [dg] = _wxDesc(x.code, true);
          let nm = '';
          try{ nm = new Date(x.date + 'T12:00:00').toLocaleDateString(undefined, { weekday: 'short' }); }catch(_){ nm = ''; }
          return `<span class="wgt-wxday">${enc(nm)} ${dg} ${enc(_wgtNum(x.max, 0))}°</span>`;
        }).join('');
        box.innerHTML = `<div class="wgt-wxnow"><span class="wgt-wxglyph">${g}</span>
            <span class="wgt-wxt">${enc(_wgtNum(d.now.temp, 0))}<sup>${enc(unit)}</sup></span>
            <button class="wgt-wxu" data-wxu aria-label="Switch units"
              title="Switch to ${u === 'imperial' ? '°C' : '°F'}">${u === 'imperial' ? '°C' : '°F'}</button></div>
          <div class="wgt-wxwhere">${enc(w.cfg.place || '')}</div>
          <div class="wgt-dim">${enc(word)} · feels ${enc(_wgtNum(d.now.feels, 0))}° · ${enc(_wgtNum(d.now.wind, 0))} ${enc((d.units && d.units.wind) || (u === 'imperial' ? 'mph' : 'km/h'))}</div>
          <div class="wgt-wxdays">${days}</div>`;
        const ub = $('[data-wxu]', box);
        if(ub) ub.onclick = async (ev) => {
          ev.stopPropagation();                       // the panel is draggable; this is a button
          const next = _wxUnits(w) === 'imperial' ? 'metric' : 'imperial';
          w.cfg.units = next;
          await save((row) => { row.cfg = Object.assign({}, row.cfg, { units: next }); });
          _wgtRefreshOne(el.closest('.os-wgt'));
        };
      },
    },

    music: {
      label: 'Now playing', icon: '#i-music', blurb: 'What the player is on, with the transport',
      // No `every`: the player tells us. A widget that polls an element it could have listened to is
      // the same mistake the games made.
      mount(el){
        el.innerHTML = `<div class="wgt-music">
          <div class="wgt-mtop">
            <div class="wgt-mart"><svg class="ic" aria-hidden="true"><use href="#i-music"></use></svg></div>
            <div class="wgt-mmeta">
              <div class="wgt-mtitle wgt-dim">Nothing playing</div>
              <div class="wgt-mnext wgt-dim"></div>
            </div>
          </div>
          <div class="wgt-mseek" data-seek role="slider" tabindex="0" aria-label="Seek">
            <div class="wgt-mseekfill"></div>
          </div>
          <div class="wgt-mtimes"><span class="wgt-mt0">0:00</span><span class="wgt-mt1">0:00</span></div>
          <div class="wgt-mctl">
            <button class="wgt-b wgt-bsh" data-m="shuffle" aria-label="Shuffle everything"
                    title="Shuffle your whole library">
              <svg class="ic" aria-hidden="true"><use href="#i-shuffle"></use></svg></button>
            <button class="wgt-b" data-m="prev" aria-label="Previous">⏮</button>
            <button class="wgt-b wgt-bmain" data-m="toggle" aria-label="Play or pause">▶</button>
            <button class="wgt-b" data-m="next" aria-label="Next">⏭</button>
            <button class="wgt-b" data-m="open" aria-label="Open Music">☰</button>
          </div></div>`;
        // Press anywhere on the bar to move: the widget reports a FRACTION, since it knows where you
        // pressed and not how long the track is.
        const bar = $('[data-seek]', el);
        if(bar){
          bar.onpointerdown = (ev) => {
            ev.stopPropagation();                       // the panel is draggable; the bar is not a grip
            const P = (PC().music && PC().music()) || null; if(!P || !P.seek) return;
            const r = bar.getBoundingClientRect();
            if(r.width) P.seek((ev.clientX - r.left) / r.width);
          };
          bar.onkeydown = (ev) => {
            const P = (PC().music && PC().music()) || null; if(!P || !P.seek || !P.now) return;
            const n = P.now(); if(!n || !n.d) return;
            const step = ev.key === 'ArrowRight' ? 5 : ev.key === 'ArrowLeft' ? -5 : 0;
            if(!step) return;
            ev.preventDefault();
            P.seek(Math.max(0, Math.min(1, (n.t + step) / n.d)));
          };
        }
        el.onclick = (ev) => {
          const b = ev.target.closest && ev.target.closest('[data-m]'); if(!b) return;
          ev.stopPropagation();
          const P = (PC().music && PC().music()) || null;
          try{
            if(b.dataset.m === 'open'){ openApp('__music'); return; }
            /* A MISSING BRIDGE MUST NOT LOOK LIKE A DEAD BUTTON.
             *
             * `if(!P) return` was silence, and silence is indistinguishable from a broken widget —
             * which is precisely how it was reported ("music widget doing nothing"). The realistic
             * cause is version skew, not a bug: the service worker serves app code
             * stale-while-revalidate, so the first load after a deploy runs the PREVIOUS app.js while
             * this file is already the new one, and `PC().music` does not exist yet. That resolves
             * itself on the next load — but only if the widget says so instead of shrugging. */
            if(!P){
              const t = $('.wgt-mtitle', el);
              if(t){ t.textContent = 'reload to finish updating'; t.classList.remove('wgt-dim'); }
              return;
            }
            // Starting from cold pulls the drive index first — a network round trip and a decrypt.
            // Say so, or the wait is indistinguishable from the dead button this used to be.
            const t = $('.wgt-mtitle', el);
            let slow = null;
            if(b.dataset.m !== 'prev' && b.dataset.m !== 'next' && t && /nothing playing/i.test(t.textContent || ''))
              slow = setTimeout(() => { t.textContent = 'loading your library…'; }, 350);
            const done = () => { if(slow) clearTimeout(slow); _wgtRefreshOne(el.closest('.os-wgt')); };
            let r;
            if(b.dataset.m === 'prev') r = P.prev();
            else if(b.dataset.m === 'next') r = P.next();
            else if(b.dataset.m === 'shuffle') r = P.shuffle && P.shuffle();
            else r = P.toggle();
            if(r && r.then) r.then(done, done); else done();
          }catch(e){
            const t = $('.wgt-mtitle', el);
            if(t) t.textContent = 'that control failed — ' + ((e && e.message) || 'unknown');
          }
        };
      },
      refresh(el){
        const P = (PC().music && PC().music()) || null;
        const t = $('.wgt-mtitle', el), main = $('[data-m="toggle"]', el);
        if(!t) return;
        const now = P && P.now ? P.now() : null;
        t.textContent = (now && now.title) || 'Nothing playing';
        t.classList.toggle('wgt-dim', !(now && now.title));
        /* The panel was a title and a row of buttons with a hole between them. What fills it is the
         * thing a now-playing panel is actually for — the art, and WHAT IS COMING — rather than a
         * decoration: the queue is already in memory, so this costs a lookup, not a request. */
        const nx = $('.wgt-mnext', el);
        if(nx) nx.textContent = (now && now.next) ? ('next · ' + now.next)
                              : (now && now.total > 1 ? (now.pos + ' of ' + now.total) : '');
        const art = $('.wgt-mart', el);
        if(art) art.classList.toggle('spin', !!(now && now.playing));
        /* THE MIDDLE OF THE PANEL, which was a hole between the title and the transport. A player's
         * missing piece is where you ARE in the track — and it is what makes the space earn itself.
         * Updated from the player's existing once-a-second hook, so there is no new timer and nothing
         * ticks while nothing plays. */
        const fill = $('.wgt-mseekfill', el), t0 = $('.wgt-mt0', el), t1 = $('.wgt-mt1', el);
        const dur = (now && now.d) || 0, at = (now && now.t) || 0;
        if(fill) fill.style.width = (dur > 0 ? Math.max(0, Math.min(100, at / dur * 100)) : 0) + '%';
        if(t0) t0.textContent = _mmss(at);
        if(t1) t1.textContent = dur > 0 ? _mmss(dur) : '--:--';
        if(main) main.textContent = (now && now.playing) ? '⏸' : '▶';
        // Shuffle is a MODE, not an action — show whether it is on, or pressing it twice looks like
        // nothing happened the second time.
        const sh = $('[data-m="shuffle"]', el);
        if(sh) sh.classList.toggle('on', !!(P && P.shuffling && P.shuffling()));
      },
    },

    note: {
      label: 'Sticky note', icon: '#i-note', blurb: 'A note on the desktop — saved in your Notes',
      // It has to POLL, because the note it shows can change anywhere: in the Notes app on this
      // screen, on a phone, on a laptop. 20s is a local lookup and a string compare.
      every: 20000,
      mount(el, w, save){
        el.innerHTML = `<textarea class="wgt-note" placeholder="Write something…" spellcheck="false"></textarea>
                        <div class="wgt-notest wgt-dim"></div>`;
        const ta = $('.wgt-note', el), st = $('.wgt-notest', el);
        ta.value = w.cfg.text || '';
        // Typing must not publish. The note is written after a pause, and again on blur, so closing
        // the lid mid-sentence does not lose the sentence.
        let t = null;
        const flush = async () => {
          t = null;
          const text = ta.value;
          // Cleared here rather than at the end: from this point the text is being written, and a
          // refresh that arrives now would be writing the SAME string.
          delete ta.dataset.typing;
          if(text === (w.cfg.text || '')) return;
          st.textContent = 'saving…';
          /* Notes FIRST, and its answer decides what this says.
           *
           * The desktop document keeps a copy so the paper is filled in before Notes has loaded
           * anything, but Notes is where the note actually lives — it is the copy that reaches your
           * phone and the one that is not bounded by what fits in a layout document. So a failure
           * here is reported rather than swallowed: "saved to Notes" over a note that is not in
           * Notes is the exact thing that made this feature look like it worked. */
          let r = null;
          try{ r = await _noteSync(w.cfg.noteId || '', text); }
          catch(e){ r = { id: w.cfg.noteId || '', ok: false, why: (e && e.message) || 'write failed' }; }
          // save() takes a MUTATOR, not a copy of the row — this closure is created once at mount and
          // the row beneath it is replaced on every save, so writing a captured object back would
          // undo whatever another device changed in the meantime.
          try{
            await save((row) => {
              row.cfg = Object.assign({}, row.cfg, { text: text.slice(0, WGT_TEXT_MAX) });
              if(r && r.id) row.cfg.noteId = r.id;
            });
            st.textContent = r && r.ok ? (r.queued ? 'saved — will sync' : 'saved to Notes')
                                       : ('kept here only — ' + ((r && r.why) || 'Notes did not take it'));
          }catch(_){ st.textContent = 'not saved'; }
          // The paper holds a copy, and a copy has to fit in the layout document. Say so rather than
          // letting the tail vanish on the next load — Notes has the whole thing.
          if(text.length > WGT_TEXT_MAX && r && r.ok) st.textContent = 'saved to Notes (long notes live there)';
        };
        ta.oninput = () => { st.textContent = ''; ta.dataset.typing = '1';
                             if(t) clearTimeout(t); t = setTimeout(flush, 1200); };
        ta.onblur = () => { if(t){ clearTimeout(t); flush(); } };
        // A textarea inside a draggable panel: the pointer belongs to the text, not to the drag.
        ta.onpointerdown = (ev) => ev.stopPropagation();
      },
      /* SHOW WHAT THE NOTE ACTUALLY SAYS NOW.
       *
       * This was an empty function, and `mount` reads the text exactly once — so the paper showed
       * whatever it said when the widget was drawn, for ever. Edit the note in the Notes app, or on
       * another device, and the desktop kept the old text: "windows app not updating note contents on
       * the desktop widget. same for tablet and laptop", which is every device, because it was never
       * a platform bug.
       *
       * Two sources, in order: the NOTES library when this session has it loaded (the real copy, and
       * the one another device's edit reaches), else the desktop document's own copy — which also
       * syncs, being a replaceable document, and is what fills the paper in before Notes has loaded
       * anything.
       *
       * IT MUST NEVER CLOBBER TYPING. A refresh landing mid-sentence that replaced the textarea with
       * the last SAVED text would eat whatever is inside the 1.2s debounce — the same rule notes.js
       * follows for its own repaint (`if(VIEW==='notes' && !_dirty)`). Focused or dirty means leave
       * it alone; the flush is about to write anyway. */
      refresh(el, w){
        const ta = $('.wgt-note', el); if(!ta) return;
        if(document.activeElement === ta) return;
        if(ta.dataset.typing === '1') return;
        let text = w.cfg.text || '';
        try{
          const N = window.PCNotes;
          if(N && N.get && w.cfg.noteId){
            const n = N.get(w.cfg.noteId);
            if(n && typeof n.body === 'string') text = n.body;
          }
        }catch(_){}
        if(ta.value !== text) ta.value = text;
      },
    },

    search: {
      label: 'Web search', icon: '#i-search', blurb: 'Search from the desktop',
      bar: true,                     // wide and one line tall — see wgtBox
      mount(el){
        el.innerHTML = `<form class="wgt-search"><input class="wgt-sinput" type="search"
            placeholder="Search the web…" aria-label="Search the web" spellcheck="false">
          <button class="wgt-sgo" type="submit" aria-label="Search">➜</button></form>
          <div class="wgt-dim wgt-shint">Results open in Web Search</div>`;
        const f = $('.wgt-search', el), i = $('.wgt-sinput', el);
        i.onpointerdown = (ev) => ev.stopPropagation();
        f.onsubmit = (ev) => {
          ev.preventDefault();
          const q = String(i.value || '').trim(); if(!q) return;
          try{
            if(window.PCWebSearch && PCWebSearch.search){ openApp('websearch'); PCWebSearch.search(q); }
            else openApp('websearch');
          }catch(_){}
        };
      },
      refresh(){},
    },

    mempool: {
      label: 'Bitcoin blocks', icon: '#i-chart',
      blurb: 'Pending and confirmed blocks, from mempool.space',
      // A block is ten minutes. 30s is frequent enough that a new one appears while you are looking,
      // and the node's own cache means several of these cost one upstream request.
      every: 30000,
      mount(el){ el.innerHTML = '<div class="wgt-mp"><div class="wgt-dim">loading…</div></div>'; },
      async refresh(el){
        const box = $('.wgt-mp', el); if(!box) return;
        let d = null;
        try{ d = await _wgtFeed('mempool', 25000, () => _wgtJson('/api/mempool/blocks')); }
        catch(_){ if(!box.dataset.filled) box.innerHTML = '<div class="wgt-dim">blocks unavailable</div>'; return; }
        if(!d || !d.ok){ if(!box.dataset.filled) box.innerHTML = '<div class="wgt-dim">blocks unavailable</div>'; return; }
        box.dataset.filled = '1';
        /* HOW MANY FIT, measured — not a fixed three and three. A widget is resizable and lives at
         * four named sizes, and a row that is always clipped at the right edge looks broken rather
         * than scrollable. 71px is a tile plus its gap; 18px is the seam and its margins. The tip is
         * the interesting end, so an odd number of slots goes to the confirmed side. */
        const w = Math.max(120, box.clientWidth || el.clientWidth || 300);
        const slots = Math.max(2, Math.min(6, Math.floor((w - 18) / 71)));
        const nConf = Math.min(3, Math.ceil(slots / 2));
        const nPend = Math.min(3, slots - nConf);
        const pend = (d.pending || []).slice(0, nPend);
        const conf = (d.blocks || []).slice(0, nConf);
        /* PENDING ON THE LEFT, CONFIRMED ON THE RIGHT, meeting in the middle at the chain tip — the
         * arrangement mempool.space uses, and it is not decoration: it is what makes "my transaction
         * is two blocks away" readable at a glance. The pending stack is drawn newest-furthest so it
         * reads outward from the tip, which is why it is reversed here. */
        const tile = (o, kind) => `<div class="mp-b ${kind}" style="--mp-h:${_mpHue(o.median)}">
            <div class="mp-b-top">${kind === 'p' ? '~' + _mpFee(o.median) : '#' + enc(String(o.height))}</div>
            <div class="mp-b-mid">${kind === 'p' ? enc(_mpFee(o.lo) + '–' + _mpFee(o.hi))
                                                 : '~' + _mpFee(o.median) + ' sat/vB'}</div>
            <div class="mp-b-bot">${kind === 'p' ? enc(_mpCount(o.tx)) + ' tx'
                                                 : enc(_mpAgo(o.ts))}</div>
          </div>`;
        box.innerHTML = `<div class="mp-row">
            <div class="mp-side pending">${pend.slice().reverse().map(o => tile(o, 'p')).join('')}</div>
            <div class="mp-tip" aria-hidden="true"></div>
            <div class="mp-side done">${conf.map(o => tile(o, 'c')).join('')}</div>
          </div>
          <div class="mp-foot">${pend.length ? enc(_mpFee(pend[0].median)) + ' sat/vB next block' : 'mempool empty'}${
            d.stale ? ' · last known' : ''}</div>`;
        box.onclick = (ev) => { if(ev.target.closest('.mp-b, .mp-foot')) openExternal('https://mempool.space'); };
      },
    },

    calendar: {
      label: 'Today', icon: '#i-clock', blurb: "What is on today, from your encrypted calendars",
      // Five minutes. A calendar changes on human timescales, and the read is N+1 requests.
      every: 300000,
      mount(el){ el.innerHTML = '<div class="wgt-cal"><div class="wgt-dim">loading…</div></div>'; },
      async refresh(el){
        const box = $('.wgt-cal', el); if(!box) return;
        let d = null;
        try{ d = await _calFeed(); }
        catch(_){
          // dataset.filled: keep the last good list rather than replacing a real day with an error.
          // A calendar that failed to refresh at 09:04 still tells you what is on at 09:00.
          if(!box.dataset.filled) box.innerHTML = '<div class="wgt-dim">calendar unavailable</div>';
          return;
        }
        if(d && d.off){ box.innerHTML = '<div class="wgt-dim">Calendar is off on this node</div>'; return; }

        const now = new Date();
        const day0 = new Date(now.getFullYear(), now.getMonth(), now.getDate());
        // Fourteen days, not one: an empty day is the COMMON case, and "nothing today" alone is less
        // use than the thing that is actually coming. The extra window costs one expansion, not one
        // request — the items are already here.
        const soon = new Date(now.getFullYear(), now.getMonth(), now.getDate() + 14);
        const all = _calOccurrences(d.items, day0, soon);
        box.dataset.filled = '1';

        const { today, later } = _calSplit(all, now);
        const head = `<div class="wgt-calhead"><b>${enc(String(now.getDate()))}</b>
          <span>${enc(now.toLocaleDateString(undefined, { weekday: 'long', month: 'long' }))}</span></div>`;

        const row = (o) => {
          return `<div class="wgt-calrow${o.gone ? ' gone' : ''}">
            <span class="wgt-calt">${enc(_calTime(o))}</span>
            <span class="wgt-calx" style="background:${enc(_calHue(o.cal))}"></span>
            <span class="wgt-caln">${enc(o.title || '(no title)')}</span></div>`;
        };

        /* TODAY AND WHAT IS COMING, always — not "today, or the next thing if today happens to be
         * empty". Asked for as "Desktop widget only showing today, can it show today and tomorrow
         * events?", and it is the more useful shape anyway: what you want from a glance at a calendar
         * is whether the next thing is in an hour or on Thursday. A later row carries its DAY so it
         * can never be read as one of today's, and the list scrolls, so a busy week does not push the
         * panel out of shape. */
        const laterRows = later.map(o => `<div class="wgt-calrow">
            <span class="wgt-calt">${enc(_calDayLabel(o.start, now))}</span>
            <span class="wgt-calx" style="background:${enc(_calHue(o.cal))}"></span>
            <span class="wgt-caln">${enc(o.title || '(no title)')}</span></div>`).join('');
        let body;
        if(today.length || later.length){
          body = (today.length ? today.map(row).join('')
                               : '<div class="wgt-dim">Nothing today.</div>')
               + (later.length ? '<div class="wgt-calnext">Coming up</div>' + laterRows : '');
        }else{
          body = '<div class="wgt-dim">Nothing today, and nothing in the next two weeks.</div>';
        }
        box.innerHTML = head + '<div class="wgt-calrows">' + body + '</div>';
        // The whole panel opens the Calendar. `pointerdown` is what DRAGS a widget, so the click is
        // taken on the row and the drag is left alone.
        box.onclick = (ev) => { if(ev.target.closest('.wgt-calrow, .wgt-calhead')) openApp('calendar'); };
      },
    },

    clock: {
      label: 'Clock', icon: '#i-clock', blurb: 'The time here, and in the cities you follow',
      /* ONE SECOND, and the shared timer takes the fastest widget on the desk as its period (see
       * _wgtPeriod). A clock is the one panel whose whole job is to be right: at the 15s tick a
       * minute flips up to fifteen seconds late, which is exactly the error someone notices when
       * they look from this to their phone. It stays one timer, it still stops when the desktop is
       * left or the tab is hidden, and a tick writes text into nodes that already exist. */
      every: 1000,
      mount(el){
        el.innerHTML = `<div class="wgt-clk">
            <div class="wgt-clkt"><b class="wgt-clkh"></b><i class="wgt-clkap"></i>
              <button class="wgt-clkadd" type="button" title="Add a city" aria-label="Add a city">＋</button></div>
            <div class="wgt-clkd"></div>
            <div class="wgt-clkz"></div>
          </div>`;
      },
      refresh(el, w, save){
        const box = $('.wgt-clk', el); if(!box) return;
        const cfg = w.cfg || {};
        const now = new Date();
        const main = _clockFace(now, cfg.tz || '', cfg);
        // textContent, and only when it CHANGED: this runs every second for the life of the desktop.
        const t = $('.wgt-clkh', box), ap = $('.wgt-clkap', box), d = $('.wgt-clkd', box);
        if(t && t.textContent !== main.time) t.textContent = main.time;
        if(ap && ap.textContent !== main.ampm) ap.textContent = main.ampm;
        if(d && d.textContent !== main.date) d.textContent = main.date;

        const zbox = $('.wgt-clkz', box); if(!zbox) return;
        { const b = $('.wgt-clkadd', box);
          if(b && !b.dataset.wired){ b.dataset.wired = '1';
            b.onpointerdown = (ev) => ev.stopPropagation();     // the panel is the drag handle
            b.onclick = (ev) => { ev.stopPropagation(); _clockPicker(el, w, save); }; } }
        // The city search is open and being typed in — leave the list alone until it closes.
        if(zbox.dataset.pick) return;
        const zones = _clockZones(cfg);
        /* Rebuild the ROWS only when the list of cities changes; a tick updates their times. Rebuilding
         * three rows of innerHTML every second is not a layout read, but it is a second of DOM churn
         * every second, for ever, on a panel nobody is interacting with. */
        const sig = zones.join('|');
        if(zbox.dataset.sig !== sig){
          zbox.dataset.sig = sig;
          zbox.innerHTML = zones.map(z => `<div class="wgt-clkrow" data-z="${enc(z)}">
              <span class="wgt-clkzn">${enc(_clockCity(z))}</span>
              <span class="wgt-clkzt"></span></div>`).join('');
          zbox.onclick = (ev) => {
            const row = ev.target.closest('.wgt-clkrow'); if(!row) return;
            ev.stopPropagation();
            _clockZoneMenu(row.dataset.z, ev.clientX, ev.clientY, w, save);
          };
        }
        for(const row of zbox.querySelectorAll('.wgt-clkrow')){
          const f = _clockFace(now, row.dataset.z, cfg);
          const txt = f.time + (f.ampm ? ' ' + f.ampm : '') + (f.dayNote ? ' · ' + f.dayNote : '');
          const n = row.querySelector('.wgt-clkzt');
          if(n && n.textContent !== txt) n.textContent = txt;
          if(row.classList.contains('bad') !== !f.ok) row.classList.toggle('bad', !f.ok);
        }
      },
    },

    news: {
      label: 'Headlines', icon: '#i-news', blurb: 'The latest from your News feeds, one after another',
      /* The ROTATION interval, not a fetch interval — this widget never fetches. News already keeps a
       * background snapshot of every feed for its unread badge (one shared read of the server's own
       * cache, ten-minutely); the panel reads that. Two of these on a desk are still zero requests. */
      every: 7000,
      mount(el){
        el.innerHTML = '<div class="wgt-news"><div class="wgt-dim">loading…</div></div>';
        /* PAUSE WHILE IT IS BEING READ. A headline that slides away mid-sentence is the reason
         * scrolling tickers are turned off, and the pointer is a perfectly good statement of intent. */
        const box = $('.wgt-news', el);
        el.addEventListener('pointerenter', () => { if(box) box.dataset.hold = '1'; });
        el.addEventListener('pointerleave', () => { if(box) delete box.dataset.hold; });
      },
      refresh(el){
        const box = $('.wgt-news', el); if(!box) return;
        const N = window.PCNews;
        if(!N || !N.latest){ box.innerHTML = '<div class="wgt-dim">News is not available here</div>'; return; }
        let items = [];
        try{ items = N.latest(80) || []; }catch(_){ items = []; }
        if(!items.length){
          /* THREE different answers, and only one of them is actionable. "No feeds yet" is a
           * statement about the ACCOUNT, so it must not be made on a device that has simply never
           * opened News and does not know yet — feedCount() returns -1 for that, and saying "no
           * feeds" there was a confident lie to somebody who has plenty. */
          let has = -1; try{ has = N.feedCount ? N.feedCount() : -1; }catch(_){ has = -1; }
          box.innerHTML = has < 0 ? '<div class="wgt-dim">loading…</div>'
                        : has     ? '<div class="wgt-news-done">No more News</div>'
                                  : '<div class="wgt-dim">No feeds yet — add them in News.</div>';
          box.onclick = () => openApp('news');
          delete box.dataset.filled;
          return;
        }
        /* HOLD MEANS HOLD. Freezing the offset was not enough: the rebuild below replaced the rows
         * with identical ones every 7 seconds, which replays the slide-in animation under the
         * pointer the hold exists to stop — and a rebuild landing between mousedown and mouseup
         * throws away the <a> being clicked, so the article never opens. */
        if(box.dataset.hold && box.dataset.filled) return;
        // How many fit, measured — same discipline as the block tiles. 33px is a title line and its
        // source/age line at 12.5px; 4px is the gap.
        const h = Math.max(40, box.clientHeight || el.clientHeight || 120);
        const rows = Math.max(1, Math.min(6, Math.floor(h / 33)));
        let off = Number(box.dataset.off) || 0;
        if(box.dataset.filled) off = (off + 1) % items.length;
        box.dataset.off = String(off);
        box.dataset.filled = '1';
        box.onclick = null;      // the empty state binds one; a populated panel must not carry it
        const shown = _newsWindow(items, off, rows);
        box.innerHTML = shown.map(it => {
          const meta = `<span class="wgt-nt">${enc(it.title || '(untitled)')}</span>
            <span class="wgt-nm">${enc(it.feedName || '')}${it.ts ? ' · ' + enc(_wgtAgo(it.ts)) : ''}</span>`;
          /* NO LINK, NO ANCHOR. A feed item without a usable http(s) link used to render
           * `href="#" target="_blank"`, which resolves to the CURRENT document — so clicking it
           * opened a second full copy of the client in a new tab, with its own relay sockets and
           * subscriptions. A row that cannot go anywhere is a row, not a link. */
          const href = _safeHttp(it.link);
          const id = it.id ? ` data-nid="${enc(String(it.id))}"` : '';
          return href ? `<a class="wgt-nrow" href="${enc(href)}" target="_blank"
                            rel="noopener noreferrer"${id}>${meta}</a>`
                      : `<div class="wgt-nrow nolink"${id}>${meta}</div>`;
        }).join('');
        for(const a of box.querySelectorAll('.wgt-nrow')){
          // A link is a link — but the drag handle is the whole panel, so the row must not start one.
          a.onpointerdown = (ev) => ev.stopPropagation();
          /* OPENING IT IS READING IT. The panel only carries unread items, so without this the same
           * article comes round for ever — the News screen marks on scroll and nobody scrolls a
           * widget. It marks on the way out, so the row is gone by the next rotation. */
          a.onclick = () => { try{ if(a.dataset.nid && N.markRead) N.markRead(a.dataset.nid); }catch(_){} };
        }
      },
    },

    stats: {
      label: 'Community', icon: '#i-wot', blurb: 'Web of trust, who is online, live streams and calls',
      // The app polls /client/stats once every 15s for the sidebar; this reads what that already
      // fetched, so it matches the tick it depends on and costs nothing of its own.
      every: 15000,
      mount(el){ el.innerHTML = '<div class="wgt-st"></div>'; },
      refresh(el){
        const box = $('.wgt-st', el); if(!box) return;
        let st = null;
        try{ st = (PC().communityStats && PC().communityStats()) || null; }catch(_){ st = null; }
        /* `fetched`, not truthiness. communityStats() is defined unconditionally and answers with a
         * zeroed object, so a standalone build with no instance to ask would render five
         * authoritative-looking zeroes — "0 WoT, 0 online" reads as a dead network rather than as no
         * server. The desktop's tray panel learned this the same way. */
        if(!st || !st.fetched){
          box.innerHTML = '<div class="wgt-dim">No instance to ask — this client is running on its own.</div>';
          return;
        }
        box.innerHTML = _statCells(st).map(c => `<span class="wgt-stc${c.live ? ' live' : ''}">
            ${iconSvg(c.icon)}<b>${enc(_wgtCount(c.n))}</b><i>${enc(c.label)}</i></span>`).join('');
        box.onclick = (ev) => { if(ev.target.closest('.wgt-stc')) openApp('stats'); };
      },
    },
  };

  /* ---- the clock ------------------------------------------------------------------------------
   *
   * The taskbar already carries HH:MM, so this panel is not "the time" — it is the time READ AT A
   * GLANCE (a numeral you can see from across the room, with the date under it) and the cities you
   * keep track of. The whole of it is Intl: no offset table, no DST rules, no list of cities to go
   * stale, and "is it tomorrow there yet" comes out of the same formatter as the time.
   *
   * DateTimeFormat objects are CACHED. Constructing one is the expensive half of Intl — this runs
   * once a second times (one + a city per row), for the life of the desktop, and building four
   * formatters a second to print four times was the obvious way to make a clock cost real CPU. */
  const _dtfs = new Map();
  function _dtf(opts){
    const key = JSON.stringify(opts);
    let f = _dtfs.get(key);
    if(f === undefined){
      try{ f = new Intl.DateTimeFormat(opts.locale || undefined, opts.o); }
      catch(_){ f = null; }                     // an unknown zone throws — remembered as "cannot"
      if(_dtfs.size > 60) _dtfs.clear();        // bounded; the key space is cfg × zones, both small
      _dtfs.set(key, f);
    }
    return f;
  }
  const _clockCity = (tz) => String(tz || '').split('/').pop().replace(/_/g, ' ');
  /* Is this clock showing 12 hours? A stored choice, else whatever the reader's own locale does —
   * asking Intl rather than guessing from the language, because en-GB is 24-hour and en-US is not. */
  function _clockIs12(cfg){
    const c = cfg || {};
    if(c.h12 === 1 || c.h12 === true) return true;
    if(c.h12 === 0 || c.h12 === false) return false;
    try{ return !!_dtf({ o: { hour: 'numeric' } }).resolvedOptions().hour12; }catch(_){ return false; }
  }
  /* The stored list, bounded. Four is what fits a large panel; it is also the point past which a
   * "world clock" is a timezone table, which is a screen and not a widget. */
  function _clockZones(cfg){
    return String((cfg && cfg.zones) || '').split(',').map(s => s.trim()).filter(Boolean).slice(0, 4);
  }
  // The calendar day in a zone, as YYYY-MM-DD. 'en-CA' is not a display choice — it is the one common
  // locale that formats ISO-order, so two of these can be COMPARED. Never shown to anybody.
  function _clockDay(now, tz){
    const f = _dtf({ locale: 'en-CA', o: Object.assign({ year: 'numeric', month: '2-digit', day: '2-digit' },
                                                       tz ? { timeZone: tz } : {}) });
    try{ return f ? f.format(now) : ''; }catch(_){ return ''; }
  }
  /* What the panel draws for one zone (or for here, with tz='') — kept DOM-free so
   * tests/test_desktop_widgets.py can run the shipped code against real zones and real DST dates.
   * Nothing on screen says when a clock is wrong; it just says the wrong time, confidently. */
  function _clockFace(now, tz, cfg){
    const c = cfg || {};
    const o = { hour: '2-digit', minute: '2-digit' };
    if(c.sec) o.second = '2-digit';
    // Unset follows the reader's locale, which is right far more often than either fixed answer. A
    // stored choice is a choice and wins for ever — the same rule the weather's units follow.
    if(c.h12 === 1 || c.h12 === true) o.hour12 = true;
    else if(c.h12 === 0 || c.h12 === false) o.hour12 = false;
    if(tz) o.timeZone = tz;
    const f = _dtf({ o });
    if(!f) return { time: '--:--', ampm: '', date: '', dayNote: '', ok: false };
    let parts;
    try{ parts = f.formatToParts(now); }
    catch(_){ return { time: '--:--', ampm: '', date: '', dayNote: '', ok: false }; }
    /* The am/pm marker is SPLIT OUT rather than formatted into the string: it is set small beside a
     * 34px numeral, and "10:45 PM" all at one size is a clock that reads as text. Everything that is
     * not the marker (including the separators) is the time, so this holds for locales that place it
     * first or use their own words for it. */
    let time = '', ampm = '';
    for(const p of parts){
      if(p.type === 'dayPeriod') ampm = String(p.value || '');
      else if(p.type !== 'literal' || time) time += p.value;   // drop a leading separator/space
    }
    time = time.trim().replace(/[\s  ]+$/, '');
    const df = _dtf({ o: Object.assign({ weekday: 'long', day: 'numeric', month: 'long' },
                                       tz ? { timeZone: tz } : {}) });
    let date = '';
    try{ date = df ? df.format(now) : ''; }catch(_){ date = ''; }
    /* "It is 07:10 there" is half an answer — the useful half is that it is 07:10 TOMORROW. Computed
     * by comparing calendar days rather than by arithmetic on offsets, which is what makes it right
     * across DST, the date line, and the half-hour zones. */
    let dayNote = '';
    if(tz){
      const here = _clockDay(now, ''), there = _clockDay(now, tz);
      if(here && there && here !== there) dayNote = there > here ? 'tomorrow' : 'yesterday';
    }
    return { time, ampm, date, dayNote, ok: true };
  }
  /* Cities offered before anything is typed. A search box with nothing in it is a search box you have
   * to already know the answer for — and "what is Europe/Kyiv called in the tz database" is exactly
   * the thing somebody adding a clock does not know. */
  const _TZ_COMMON = ['America/Los_Angeles', 'America/Denver', 'America/Chicago', 'America/New_York',
                      'America/Sao_Paulo', 'Europe/London', 'Europe/Berlin', 'Europe/Kyiv',
                      'Africa/Johannesburg', 'Asia/Dubai', 'Asia/Kolkata', 'Asia/Shanghai',
                      'Asia/Tokyo', 'Australia/Sydney'];
  function _tzList(){
    try{
      if(typeof Intl.supportedValuesOf === 'function'){
        const v = Intl.supportedValuesOf('timeZone');
        if(v && v.length) return v;
      }
    }catch(_){}
    return _TZ_COMMON;                 // older Safari/WebView: the shortlist is still a working picker
  }
  /* The city search, drawn INTO the clock's own zone list. `pick` on that node is what stops the
   * one-second refresh from repainting the list out from under the search you are halfway through —
   * the same guard the weather picker needs, for the same reason. */
  function _clockPicker(el, w, save){
    const zbox = $('.wgt-clkz', el); if(!zbox) return;
    zbox.dataset.pick = '1';
    zbox.innerHTML = `<div class="wgt-clkpick">
        <div class="wgt-clkprow">
          <input class="wgt-wxq wgt-clkq" type="search" placeholder="City or time zone…"
                 aria-label="Find a city" spellcheck="false" autocomplete="off">
          <button class="wgt-clkx" type="button" title="Cancel" aria-label="Cancel">✕</button>
        </div>
        <div class="wgt-wxres wgt-clkres"></div></div>`;
    const q = $('.wgt-clkq', zbox), res = $('.wgt-clkres', zbox);
    /* `delete`, not `sig = ''`: an empty city list HAS the signature '', so assigning it left the
     * rebuild guard seeing no change — on a clock with no cities yet, which is every clock somebody
     * opens this picker on for the first time. A missing attribute differs from every signature. */
    let closed = false;
    const close = () => {
      if(closed) return;
      closed = true;
      delete zbox.dataset.pick;
      delete zbox.dataset.sig;                      // next tick rebuilds the rows
      document.removeEventListener('pointerdown', away, true);
    };
    /* AND IT MUST BE DISMISSABLE BY GIVING UP. Escape only reaches a focused input, and picking a
     * city is the one other way out — so clicking ＋ and changing your mind replaced the world
     * clocks with an empty search box for the rest of the session. Outside-click closes it, and
     * there is a ✕ for touch, where "click outside" is not something anyone thinks to try. */
    function away(ev){ if(!zbox.contains(ev.target)) close(); }
    document.addEventListener('pointerdown', away, true);
    if(!q || !res) return close();
    { const x = $('.wgt-clkx', zbox);
      if(x){ x.onpointerdown = (ev) => ev.stopPropagation();
             x.onclick = (ev) => { ev.stopPropagation(); close(); }; } }
    q.onpointerdown = (ev) => ev.stopPropagation();
    q.onkeydown = (ev) => { if(ev.key === 'Escape'){ ev.stopPropagation(); close(); } };
    const all = _tzList();
    const paint = () => {
      const s = String(q.value || '').trim().toLowerCase().replace(/\s+/g, ' ');
      const hits = (s ? all.filter(z => z.toLowerCase().replace(/_/g, ' ').indexOf(s) >= 0)
                      : _TZ_COMMON.filter(z => all.indexOf(z) >= 0 || all === _TZ_COMMON)).slice(0, 14);
      res.innerHTML = hits.length
        ? hits.map(z => `<button class="wgt-wxhit" data-z="${enc(z)}"><b>${enc(_clockCity(z))}</b>
            <span class="wgt-dim">${enc(z)}</span></button>`).join('')
        : '<div class="wgt-dim">No zone by that name.</div>';
      for(const b of res.querySelectorAll('.wgt-wxhit')) b.onclick = (ev) => {
        ev.stopPropagation();
        const z = b.dataset.z;
        close();
        save((row) => {
          const cur = _clockZones(row.cfg || {});
          if(cur.indexOf(z) >= 0) return false;                       // already there — no write
          if(cur.length >= 4){ try{ PC().toast('four cities is as many as the clock takes'); }catch(_){} return false; }
          row.cfg = Object.assign({}, row.cfg, { zones: cur.concat([z]).join(',') });
        });
      };
    };
    q.oninput = paint;
    paint();
    try{ q.focus(); }catch(_){}
  }
  function _clockZoneMenu(z, x, y, w, save){
    showCtx(x, y, [{ label: 'Remove ' + _clockCity(z), run: () => save((row) => {
      const cur = _clockZones(row.cfg || {}).filter(v => v !== z);
      row.cfg = Object.assign({}, row.cfg, { zones: cur.join(',') });
    }) }]);
  }

  /* ---- headlines ------------------------------------------------------------------------------
   * The window of items on screen, wrapping — kept pure so the rotation can be tested without a
   * clock and without a DOM. Wrapping is the whole of it: a ticker that runs off the end of a short
   * feed and shows nothing is how one with three headlines behaves. */
  function _newsWindow(items, off, n){
    const out = [], L = (items || []).length;
    if(!L) return out;
    const start = ((Number(off) || 0) % L + L) % L;
    for(let i = 0; i < Math.min(Math.max(1, n | 0), L); i++) out.push(items[(start + i) % L]);
    return out;
  }
  // '' for anything that is not a real http(s) link — never '#', which in a target=_blank anchor
  // resolves to the current document and opens a second copy of the whole client.
  const _safeHttp = (u) => /^https?:\/\//i.test(String(u || '')) ? String(u) : '';
  function _wgtAgo(ts){
    const s = Math.max(0, Math.floor(Date.now() / 1000) - (Number(ts) || 0));
    if(s < 90) return 'just now';
    if(s < 3600) return Math.round(s / 60) + 'm';
    if(s < 86400) return Math.round(s / 3600) + 'h';
    return Math.round(s / 86400) + 'd';
  }

  /* ---- the community counters -------------------------------------------------------------------
   * ALL FIVE, ALWAYS, INCLUDING THE ZEROES. "0 live" is an answer; a cell that disappears when it is
   * zero reads as a feature that is missing, which is what happened the last time these were made
   * conditional (see netStatsHtml, which shows the same five for the same reason). Pure, so the
   * decision can be tested without a relay. */
  function _statCells(st){
    const n = (v) => Math.max(0, Number(v) || 0);
    const live = n(st && st.streams);
    return [{ icon: 'i-wot',       n: n(st && st.users),  label: 'WoT' },
            { icon: 'i-livedot',   n: n(st && st.online), label: 'online' },
            { icon: 'i-relay-dot', n: n(st && st.relay),  label: 'on relay' },
            { icon: 'i-stream',    n: live,               label: 'live', live: live > 0 },
            { icon: 'i-call',      n: n(st && st.calls),  label: 'in call' }];
  }
  const _wgtCount = (n) => { const v = Math.max(0, Number(n) || 0);
    try{ return v.toLocaleString(); }catch(_){ return String(v); } };

  /* Fee → hue, the way an explorer colours a block: cheap is green, busy is amber, a fee spike is
   * red. Bucketed rather than a continuous ramp, because the point is "is it cheap right now" and a
   * gradient answers that less clearly than four steps do. */
  function _mpHue(fee){
    const f = Number(fee) || 0;
    if(f < 3) return '150 70% 55%';
    if(f < 10) return '95 65% 55%';
    if(f < 30) return '45 90% 58%';
    if(f < 100) return '25 90% 58%';
    return '355 80% 62%';
  }
  // Fees are quoted to one decimal below ten and rounded above it: "1.5" matters, "127.4" does not.
  const _mpFee = (n) => { const f = Number(n) || 0; return f < 10 ? String(Math.round(f * 10) / 10) : String(Math.round(f)); };
  const _mpCount = (n) => { n = Number(n) || 0; return n >= 1000 ? (n / 1000).toFixed(1).replace(/\.0$/, '') + 'k' : String(n); };
  function _mpAgo(ts){
    const s = Math.max(0, Math.floor(Date.now() / 1000) - (Number(ts) || 0));
    if(s < 90) return 'just now';
    if(s < 5400) return Math.round(s / 60) + 'm ago';
    if(s < 172800) return Math.round(s / 3600) + 'h ago';
    return Math.round(s / 86400) + 'd ago';
  }

  /* A stable colour per calendar, from its id. The real palette lives in calendar.js keyed on the
   * calendar LIST's order, which this widget deliberately does not fetch a second time — and a hue
   * that changes when a calendar is added would be worse than one that merely differs from the
   * Calendar screen's. */
  function _calHue(cal){
    let h = 0;
    for(const ch of String(cal || '')) h = (h * 31 + ch.charCodeAt(0)) % 360;
    return `hsl(${h} 70% 60%)`;
  }

  /* The sticky note is a NOTE — the same encrypted per-note document the Notes app owns, so what you
   * jot on the desktop is there on your phone. The widget stores the note's id and its own copy of
   * the text (it has to draw before Notes has loaded anything); Notes owns the storage. If the Notes
   * module is not present the text still persists in the desktop document, which is a worse note but
   * not a lost one. */
  /* `PCNotes.save`, and it has to be called by its REAL name.
   *
   * The first version of this called `PCNotes.saveExternal`, which does not exist. Nothing threw and
   * nothing was logged: the guard right above it — `if(!N || !N.saveExternal) return` — read a
   * missing function as "Notes is not loaded here", took the graceful path, and the note went
   * nowhere. That is the same mistake as the mail sender card that called three functions that were
   * not there, and it fails the same way: silently, as "why isn't this saving".
   *
   * So the guard now names what it actually needs, and a Notes module that is present but wrong is a
   * REPORTED failure rather than a quiet no-op — the text is safe either way (it is in the desktop
   * document), but "saved to Notes" must not appear over a note that is not in Notes.
   *
   * Returns the note id; `save` answers `{id, queued}`, and queued is a success — the offline
   * notebook publishes it on reconnect, which is the whole point of having one. */
  async function _noteSync(noteId, text){
    const N = window.PCNotes;
    if(!N || typeof N.save !== 'function') return { id: noteId, ok: false, why: 'Notes is not loaded' };
    const r = await N.save({ id: noteId, title: 'Desktop note', body: text });
    // `save` answers {id, queued} — an OBJECT, not an id. Storing it whole would put an object into
    // cfg, which _normDoc drops (strings, numbers and booleans only), so every session would lose the
    // link and start a fresh note beside the last one.
    return { id: (r && r.id) || noteId, ok: true, queued: !!(r && r.queued) };
  }

  /* TODAY'S EVENTS, read the way the Calendar screen reads them and expanded the same way.
   *
   * The widget does its OWN fetching rather than borrowing calendar.js's module state, for two
   * reasons: that state only exists once the screen has been opened (a desktop is often the first
   * thing you see), and its `load()` ends in `paint()`, which writes into #feed — the element every
   * other view is drawn into. A widget must never repaint somebody else's screen.
   *
   * `authFetch`, not the plain `_wgtJson` the ticker and the weather use: those endpoints are open
   * and these are not, and the bundled apps authenticate with a BEARER rather than a cookie, so a
   * bare fetch is a 401 on exactly the clients that have no other way in.
   *
   * ONE read for every calendar widget on the desk, cached for five minutes (`_wgtFeed`), because
   * this is N+1 requests by construction — one for the calendar list and one per calendar.
   */
  async function _calFeed(){
    return _wgtFeed('cal:items', 300000, async () => {
      const A = PC().authFetch;
      const get = async (path) => {
        const r = await A(_api(path));
        if(!r.ok) throw new Error('HTTP ' + r.status);
        return await r.json();
      };
      const cfg = await get('/api/calendar/config');
      if(!cfg || !cfg.enabled) return { off: true, items: [] };
      const cals = ((await get('/api/calendar/calendars')) || {}).calendars || [];
      const out = [];
      for(const c of cals){
        // A calendar that will not load must not blank the ones that will.
        try{
          const r = await get('/api/calendar/items?cal=' + encodeURIComponent(c.id));
          for(const rec of (r.items || [])) out.push(Object.assign({ cal: c.id }, rec));
        }catch(_){}
      }
      return { off: false, items: out };
    });
  }

  /* Occurrences between two dates, sorted. Recurrence is PCIcal's job — the same DOM-free module the
   * Calendar screen uses and `tests/test_ical_recurrence.py` runs under node — because a widget that
   * only placed DTSTART would show an empty day to somebody whose every appointment repeats, which
   * is precisely the bug the month grid had. */
  function _calOccurrences(items, from, to){
    const I = window.PCIcal;
    if(!I) return [];
    const out = [];
    for(const rec of items || []){
      // One malformed item must not empty the whole widget.
      try{ out.push(...I.occurrences(I.parseResource(rec), from, to)); }catch(_){}
    }
    out.sort((a, b) => (a.start - b.start) || String(a.title || '').localeCompare(String(b.title || '')));
    return out;
  }

  /* "Tomorrow" beats "Thu" for the day everybody actually asks about, and a weekday beats a date
   * inside the week you can picture. Past that a weekday alone is ambiguous ("Thu" — this one or the
   * next?), so it becomes a date. */
  function _calDayLabel(d, now){
    const day0 = (x) => new Date(x.getFullYear(), x.getMonth(), x.getDate());
    const n = Math.round((day0(d) - day0(now)) / 86400000);
    if(n <= 1) return 'Tomorrow';
    if(n < 7) return d.toLocaleDateString(undefined, { weekday: 'short' });
    return d.toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
  }

  const _calTime = (o) => o.allDay ? 'all day'
    : `${String(o.start.getHours()).padStart(2, '0')}:${String(o.start.getMinutes()).padStart(2, '0')}`;

  /* WHAT THE PANEL SHOWS, decided away from the DOM so tests/test_desktop_widgets.py can run it under
   * node against real recurring events.
   *
   * `today` is everything in the calendar day `now` falls in — which is NOT "the next 24 hours", and
   * the difference is the whole point of a day view: at 23:50 you want tomorrow's 09:00 under
   * "later", not mixed into today. `later` is the next two things after that, because an empty day
   * is the common case and "nothing today" on its own is less use than the thing actually coming.
   * `gone` marks a finished appointment, which is DIMMED rather than dropped — a day whose entries
   * disappear as it goes on reads as a calendar losing things. An all-day item is never gone. */
  const _CAL_LATER_MAX = 6;
  function _calSplit(occ, now){
    const day1 = new Date(now.getFullYear(), now.getMonth(), now.getDate() + 1);
    const today = [], later = [];
    for(const o of occ || []){
      if(!o || !o.start) continue;
      if(o.start < day1) today.push(Object.assign({}, o, { gone: !o.allDay && o.start < now }));
      else if(later.length < _CAL_LATER_MAX) later.push(o);
    }
    return { today, later };
  }

  /* WHICH UNITS, defaulted from the BROWSER rather than from the server.
   *
   * A node in one country serves readers in another, so a server-side default is wrong for somebody
   * by construction — and "°C on a widget in America" is exactly that failure. `resolvedOptions()`
   * reports what this browser's locale actually uses; Intl exposes it directly on modern engines and
   * the en-US/liberia/… set is the documented fallback list for the rest. Once the reader presses the
   * toggle, their choice is stored per widget and neither of these is consulted again. */
  const _F_LOCALES = ['US', 'LR', 'MM', 'BS', 'BZ', 'KY', 'PW', 'FM', 'MH'];
  function _wxUnits(w){
    if(w && w.cfg && (w.cfg.units === 'imperial' || w.cfg.units === 'metric')) return w.cfg.units;
    try{
      const r = Intl.DateTimeFormat().resolvedOptions();
      if(r && r.measurementSystem) return r.measurementSystem === 'us' ? 'imperial' : 'metric';
      const loc = (r && r.locale) || navigator.language || '';
      const reg = (loc.split('-')[1] || '').toUpperCase();
      if(reg && _F_LOCALES.indexOf(reg) >= 0) return 'imperial';
    }catch(_){}
    return 'metric';
  }

  function _wxPickerHtml(){
    return `<div class="wgt-wxpick"><input class="wgt-wxq" type="search" placeholder="Your town or city…"
        aria-label="Search for a place" spellcheck="false"><div class="wgt-wxres"></div></div>`;
  }
  function _wxWire(el, w, save){
    const q = $('.wgt-wxq', el), res = $('.wgt-wxres', el);
    if(!q || q.dataset.wired) return;
    q.dataset.wired = '1';
    q.onpointerdown = (ev) => ev.stopPropagation();
    let t = null;
    q.oninput = () => {
      if(t) clearTimeout(t);
      const term = String(q.value || '').trim();
      if(term.length < 2){ res.innerHTML = ''; return; }
      // Typed a character at a time, so this is debounced AND the server caches geocodes for a day.
      t = setTimeout(async () => {
        let d = null;
        try{ d = await _wgtJson('/api/weather/geocode?q=' + encodeURIComponent(term)); }catch(_){}
        const rows = (d && d.results) || [];
        res.innerHTML = rows.length
          ? rows.map((r, i) => `<button class="wgt-wxhit" data-i="${i}">${enc(r.name)}<span class="wgt-dim">${
              enc([r.admin, r.country].filter(Boolean).join(', '))}</span></button>`).join('')
          : '<div class="wgt-dim">no match</div>';
        res.onclick = async (ev) => {
          const b = ev.target.closest && ev.target.closest('.wgt-wxhit'); if(!b) return;
          ev.stopPropagation();
          const r = rows[+b.dataset.i]; if(!r) return;
          const place = [r.name, r.country].filter(Boolean).join(', ');
          w.cfg.lat = r.lat; w.cfg.lon = r.lon; w.cfg.place = place;   // so this paint has it
          el.innerHTML = '<div class="wgt-wx"><div class="wgt-dim">loading…</div></div>';
          await save((row) => { row.cfg = Object.assign({}, row.cfg,
                                  { lat: r.lat, lon: r.lon, place }); });
          _wgtRefreshOne(el.closest('.os-wgt'));
        };
      }, 350);
    };
  }

  // ---- the wallpaper ----------------------------------------------------------------------------

  /* A picture from the drive's `Backgrounds` folder, decrypted here.
   *
   * The document stores the SHA, never a URL: drive files are encrypted with the user's master key,
   * so what an <img> can use is an object URL that exists for one session on one device. Anything
   * else — a /blossom/<sha> link, a data: URI in the doc — either shows ciphertext or puts the
   * picture in the document in the clear. Failure is silent BY DESIGN here: a wallpaper that cannot
   * be fetched leaves the default emblem, which is a desktop, rather than an error where a picture
   * should be. */
  let _bgSha = '', _bgUrl = '';
  function isBackground(m){
    return !!(m && m.folder === 'Backgrounds' && /^image\//i.test(String(m.mime || '')));
  }
  function backgrounds(){
    const out = [];
    try{
      const idx = PC().filesIdx && PC().filesIdx();
      const files = (idx && idx._norm && idx._norm().files) || {};
      for(const sha in files) if(isBackground(files[sha]))
        out.push({ sha, name: String(files[sha].name || sha.slice(0, 8)), mime: files[sha].mime });
    }catch(_){}
    return out.sort((a, b) => a.name.localeCompare(b.name));
  }
  /* Returns '' on success, else why it failed.
   *
   * Silence is right on HYDRATE — a wallpaper that cannot be fetched should leave a desktop, not an
   * error where a picture should be — and wrong when someone just PRESSED the picture: a click that
   * does nothing, next to a thumbnail that never appeared, is unactionable by the only person who can
   * see it. Same lesson as the note card that prints the reason on itself. The caller decides which
   * of the two it is; this only reports. */
  async function applyWallpaper(sha){
    if(!desk) return 'the desktop is not up';
    const want = /^[0-9a-f]{64}$/i.test(String(sha || '')) ? String(sha).toLowerCase() : '';
    if(want === _bgSha && (!want || _bgUrl)){ _paintWallpaper(); return ''; }
    _bgSha = want;
    if(!want){ _bgUrl = ''; _paintWallpaper(); return ''; }
    let why = '';
    try{
      const u = await PC().encFileUrl(want);
      if(_bgSha !== want) return '';           // changed again while this was decrypting
      _bgUrl = u || '';
      if(!_bgUrl) why = 'that picture decrypted to nothing';
    }catch(e){ _bgUrl = ''; why = (e && (e.message || e.name)) ? String(e.message || e.name).slice(0, 120) : 'could not read it'; }
    _paintWallpaper();
    return why;
  }
  function _paintWallpaper(){
    if(!desk) return;
    desk.classList.toggle('has-bg', !!_bgUrl);
    desk.style.backgroundImage = _bgUrl ? `url("${_bgUrl}")` : '';
  }

  // ---- drawing, scheduling and moving widgets ---------------------------------------------------

  const _mounted = new Map();          // element -> { w, def, due }
  let _wgtTimer = null, _wgtVis = false, _wgtMs = 0;   // _wgtMs: the period the live timer is running at

  /* WHERE EACH WIDGET ACTUALLY GOES — fractions in, pixels out, and NOTHING OVERLAPPING.
   *
   * A fraction preserves the edge you put a panel against, which is the whole reason positions are
   * stored that way. What it cannot preserve is the SPACE BETWEEN two panels: the widgets keep a
   * minimum readable size while the desk shrinks, so a tablet has proportionally less free area, and
   * an arrangement that is comfortably spread on a monitor lands on top of itself there. The
   * new-widget positions make it worse by design — they step down the right-hand edge in 0.22
   * increments, which is four clear panels on a desktop and a stack on a short desk.
   *
   * So the fractions are the INTENT and this resolves it: place in reading order, and if a panel
   * would land on one already placed, push it down; if that runs out of desk, start a new column to
   * the left. On a screen where nothing overlaps this changes nothing at all — every position is
   * exactly the fraction asked for — which is what keeps a deliberate arrangement deliberate.
   *
   * Kept DOM-free so tests/test_desktop_widgets.py can run it at real tablet sizes; every way it can
   * be wrong is silent (a panel under another one, or shoved off the edge). */
  function placeWidgets(list, deskW, deskH){
    const out = [];
    const hits = (a, b) => a.x < b.x + b.w + WGT_GAP && a.x + a.w + WGT_GAP > b.x
                        && a.y < b.y + b.h + WGT_GAP && a.y + a.h + WGT_GAP > b.y;
    const wanted = (list || []).map((w, i) => {
      const box = wgtBox(w.size, deskW, deskH, WIDGETS[w.type]);
      const maxX = Math.max(0, deskW - box.w - WGT_GAP), maxY = Math.max(0, deskH - box.h - WGT_GAP);
      return { w, i, h: box.h, wd: box.w, maxX, maxY,
               x: Math.round(maxX * w.x + WGT_GAP / 2), y: Math.round(maxY * w.y + WGT_GAP / 2) };
    });
    // Reading order, so the resolution is stable: the same document always lays out the same way,
    // rather than depending on which widget happened to be added first.
    wanted.sort((a, b) => (a.y - b.y) || (a.x - b.x) || (a.i - b.i));
    for(const p of wanted){
      let { x, y } = p;
      let guard = 0;
      while(guard++ < 64){
        const clash = out.find(o => hits({ x, y, w: p.wd, h: p.h }, o));
        if(!clash) break;
        y = clash.y + clash.h + WGT_GAP;                    // straight down, under what is in the way
        if(y > p.maxY){                                     // out of desk → next column, from the top
          x = Math.max(0, Math.min(p.maxX, (clash.x - p.wd - WGT_GAP)));
          y = Math.round(WGT_GAP / 2);
          if(out.some(o => hits({ x, y, w: p.wd, h: p.h }, o)) && x <= 0) break;   // nowhere left
        }
      }
      out.push({ id: p.w.id, x: Math.max(0, Math.min(p.maxX, x)),
                 y: Math.max(0, Math.min(p.maxY, y)), w: p.wd, h: p.h });
    }
    return out;
  }

  /* RECONCILE, never rebuild wholesale.
   *
   * This runs on every desktop redraw, and a redraw happens on every SAVE — `_apply` updates the
   * document optimistically and calls refreshIcons. So tearing the widgets down and remounting them
   * would destroy the textarea you are typing in 1.2 seconds after you stop typing, and again on
   * blur: the sticky note would eat your cursor, your selection and your scroll position, and the
   * weather picker would drop the search you were halfway through. A widget is only rebuilt when
   * something STRUCTURAL changed (it is new, its type changed, its size changed); otherwise it is
   * repositioned and left alone, which is also far less work on a desktop that redraws often. */
  function drawWidgets(){
    if(!desk) return;
    const lay = layout();
    const list = (lay.widgets || []);
    const keep = new Set();
    // ONE measurement for the whole pass — see the header. Every position below is arithmetic on it.
    const k = zf();
    const dr = desk.getBoundingClientRect();
    const deskW = dr.width / k, deskH = dr.height / k;
    // Resolved once for the whole set — see placeWidgets. Positions are the fractions asked for
    // wherever they fit, and pushed apart only where they would land on each other.
    const spots = {};
    for(const p of placeWidgets(list, deskW, deskH)) spots[p.id] = p;
    for(const w of list){
      const def = WIDGETS[w.type]; if(!def) continue;
      const box = wgtBox(w.size, deskW, deskH, def);
      const at = spots[w.id] || { x: 0, y: 0 };
      const prev = desk.querySelector('.os-wgt[data-id="' + (window.CSS && CSS.escape ? CSS.escape(w.id) : w.id) + '"]');
      if(prev && prev.dataset.type === w.type && prev.dataset.size === w.size){
        const m = _mounted.get(prev);
        if(m) m.w = w;                       // the row is a fresh object on every save
        prev.style.width = box.w + 'px';
        prev.style.height = box.h + 'px';
        prev.style.left = at.x + 'px';
        prev.style.top  = at.y + 'px';
        keep.add(prev);
        continue;
      }
      if(prev){ _mounted.delete(prev); prev.remove(); }
      const el = document.createElement('section');
      el.className = 'os-wgt';
      el.dataset.id = w.id;
      el.dataset.type = w.type;
      el.dataset.size = w.size;
      el.style.width = box.w + 'px';
      el.style.height = box.h + 'px';
      el.style.left = at.x + 'px';
      el.style.top  = at.y + 'px';
      /* NO TITLE BAR. A widget is a piece of the desktop, not a little window — an icon, a label and
       * a close button on top of a four-line panel is more chrome than content, and it made them read
       * as boxes sitting ON the desktop rather than part of it. What the bar carried is still here:
       * the label lives in the widget's own body where each one already says what it is, dragging was
       * never the bar's job (the whole panel is the handle), and removal is on the right-click menu
       * plus a ✕ that fades in on hover. `title` keeps the name reachable for anyone who wants it. */
      el.title = def.label;
      el.innerHTML = `<button class="os-wgt-x" aria-label="Remove the ${enc(def.label)} widget"
                              title="Remove">✕</button>
        <div class="os-wgt-body"></div>`;
      desk.appendChild(el);
      const body = el.querySelector('.os-wgt-body');
      /* save(mutator) — the widget describes the CHANGE, never hands back a copy of its row.
       *
       * A mount closure is created once and lives as long as the element, while `drawWidgets` replaces
       * the row object beneath it on every save (and on every layout that arrives from another
       * device). Writing a captured object back would therefore undo whatever changed in between —
       * including a `noteId` another device had just set, which orphans the note being edited. */
      const save = (mut) => _apply((doc) => {
        const row = (doc.widgets || []).find(x => x.id === w.id);
        if(!row) return false;
        if(mut(row) === false) return false;
      });
      try{ def.mount(body, w, save); }
      catch(e){ body.innerHTML = '<div class="wgt-dim">this widget failed to start</div>'; console.warn('widget', w.type, e); }
      _mounted.set(el, { w, def, body, due: 0, save, fresh: true });
      keep.add(el);
      /* The handlers read the CURRENT row out of _mounted rather than closing over `w`.
       * A kept element keeps its listeners while the document object beneath it is replaced on every
       * save, so a captured `w` goes stale: the weather widget's own menu decides whether to offer
       * "Change the place…" from `cfg.lat`, and with a stale copy it would still think no place had
       * been chosen — the one entry that only exists once you have chosen one. */
      const cur = () => (_mounted.get(el) || {}).w || w;
      el.querySelector('.os-wgt-x').onclick = (ev) => { ev.stopPropagation(); removeWidget(cur().id); };
      el.oncontextmenu = (ev) => { ev.preventDefault(); ev.stopPropagation(); wgtMenu(cur(), ev.clientX, ev.clientY); };
      el.addEventListener('pointerdown', (ev) => {
        if(ev.button !== 0) return;
        if(ev.target.closest('button, input, textarea, a, select')) return;
        startWgtDrag(el, cur(), ev);
      });
    }
    // Anything left over is a widget that was removed, or one whose type this client cannot draw.
    for(const n of desk.querySelectorAll('.os-wgt')) if(!keep.has(n)){ _mounted.delete(n); n.remove(); }
    if(!_mounted.size){ _wgtStop(); return; }
    // Only the NEW ones paint now. A redraw happens on every save, and refreshing everything here
    // would turn a keystroke's worth of note-saving into a round of every widget's work — and reset
    // their schedules, so a 10-minute forecast would refetch whenever anything else was touched.
    for(const [el, m] of _mounted) if(m.fresh){ m.fresh = false; _wgtRefreshOne(el); }
    _wgtStart();
  }

  /* THE DEADLINE IS SET SHORT, and that is what makes a widget whose `every` EQUALS the timer's
   * period refresh on every tick instead of on every other one.
   *
   * setInterval fires at ideal+jitter, and the deadline was `Date.now() + every` read at the moment
   * of the refresh — i.e. that same jitter baked in. The next tick then had to be later by more
   * jitter than the last one, which is a coin flip: the clock skipped roughly every other second
   * (…:01 → :03, a minute rollover up to two seconds late), and the Community panel refreshed every
   * ~30s against its declared 15. Subtracting a slack smaller than the tick absorbs the jitter
   * without letting a slow widget run early — a 90s ticker still fires on the tick after 90s. */
  const _wgtSlack = (every) => Math.min(250, Math.max(40, Math.round(every / 8)));
  function _wgtRefreshOne(el, now){
    const m = _mounted.get(el); if(!m) return;
    const every = m.def.every || 0;
    m.due = (now || Date.now()) + (every ? every - _wgtSlack(every) : 0);
    try{ const r = m.def.refresh(m.body, m.w, m.save); if(r && r.catch) r.catch(()=>{}); }
    catch(e){ console.warn('widget refresh', m.w.type, e); }
  }
  function _wgtRefreshDue(all){
    const now = Date.now();
    for(const [el, m] of _mounted){
      if(!all && !(m.def.every && now >= m.due)) continue;
      _wgtRefreshOne(el, now);
    }
  }
  /* How often the ONE timer fires: the fastest thing mounted, never below a second and never above
   * fifteen. It used to be a flat 15s, which is right for everything that reads a network — and
   * makes a CLOCK wrong, by up to fifteen seconds, on the one panel whose entire job is to be right.
   * Taking the minimum keeps the property that matters (one timer for the whole desktop, stopped
   * when nothing is watching) and pays the 1s cost only on a desk that actually has a clock on it. */
  function _wgtPeriodOf(everies){
    let ms = 15000;
    for(const v of (everies || [])){
      const e = Number(v) || 0;
      if(e > 0 && e < ms) ms = e;
    }
    // Never below a second. `every` is a widget's own declaration and a typo in one (100, 10) would
    // otherwise become the whole desktop's timer for as long as it is on screen.
    return Math.max(1000, ms);
  }
  function _wgtPeriod(){
    const out = [];
    for(const [, m] of _mounted) out.push(m.def && m.def.every);
    return _wgtPeriodOf(out);
  }
  /* ONE interval for every widget, and none at all when nothing is watching.
   *
   * Each widget refreshes only when ITS interval is due, so a ticker on 90s and the weather on 10
   * minutes share one timer and neither runs early. Stopped when the desktop is left and when the tab
   * is hidden — a widget must cost nothing in a background tab, which is where a page spends most of
   * its life. */
  function _wgtStart(){
    /* The visibility listener is installed FIRST, before either early return.
     *
     * It used to sit after them, so a desktop that mounted in a HIDDEN tab — a restored session, a
     * cmd-clicked link — bailed on `document.hidden` having attached nothing, and there was then no
     * event that could ever start the timer. Switching to that tab left the ticker and the weather
     * frozen for the life of the page: the one case the hidden-tab guard exists for is the one it
     * broke. */
    if(!_wgtVis && typeof document !== 'undefined'){
      _wgtVis = true;
      document.addEventListener('visibilitychange', () => {
        if(document.hidden){ _wgtStop(); return; }
        if(!on || !_mounted.size) return;
        _wgtRefreshDue(false);      // catch up on what fell due while we were away
        _wgtStart();
      });
    }
    if(!_mounted.size) return;
    if(typeof document !== 'undefined' && document.hidden) return;
    const ms = _wgtPeriod();
    // A running timer at the WRONG period is not "already started": adding a clock to a desk that
    // held only a ticker must speed the timer up, and removing it must let it slow back down.
    if(_wgtTimer && _wgtMs === ms) return;
    _wgtStop();
    _wgtMs = ms;
    _wgtTimer = setInterval(() => {
      if(!on || !_mounted.size){ _wgtStop(); return; }
      if(typeof document !== 'undefined' && document.hidden){ _wgtStop(); return; }
      _wgtRefreshDue(false);
    }, ms);
  }
  function _wgtStop(){ if(_wgtTimer){ clearInterval(_wgtTimer); _wgtTimer = null; } _wgtMs = 0; }

  // The player has no event to subscribe to, so app.js calls this when its state changes — the same
  // shape as `syncPlayer`. Cheap: it touches two nodes of one widget.
  function musicChanged(){
    for(const [el, m] of _mounted) if(m.w.type === 'music') _wgtRefreshOne(el);
  }

  /* Dragging a widget. Transform while moving, committed once on release — the same discipline as
   * windows and icons, and for the same reason: this desktop can hold a live timeline, so writing
   * left/top per pointer move lays the whole document out at pointer rate. */
  function startWgtDrag(el, w, ev){
    const k = zf();
    const dr = desk.getBoundingClientRect();
    const deskW = dr.width / k, deskH = dr.height / k;
    const ox = parseInt(el.style.left, 10) || 0, oy = parseInt(el.style.top, 10) || 0;
    const bw = el.offsetWidth, bh = el.offsetHeight;
    const maxX = Math.max(0, deskW - bw - WGT_GAP), maxY = Math.max(0, deskH - bh - WGT_GAP);
    let sx = ev.clientX, sy = ev.clientY, cx = ox, cy = oy, raf = 0, moved = false;
    const id = ev.pointerId;
    try{ ev.preventDefault(); }catch(_){}
    el.classList.add('dragging');
    const paint = () => { raf = 0; el.style.transform = `translate(${cx - ox}px, ${cy - oy}px)`; };
    const move = (e) => {
      // ONE finger owns the drag. These listen on `document`, so on a touch screen a second finger
      // put down anywhere would otherwise drive the widget from wherever it happens to move.
      if(e.pointerId !== id) return;
      if(e.pointerType !== 'touch' && (e.buttons || 0) === 0){ up(); return; }
      // Clamped in DRAWN coordinates (which carry the half-gap), matching where the widget can sit.
      cx = Math.max(WGT_GAP / 2, Math.min(maxX + WGT_GAP / 2, ox + (e.clientX - sx) / k));
      cy = Math.max(WGT_GAP / 2, Math.min(maxY + WGT_GAP / 2, oy + (e.clientY - sy) / k));
      if(Math.abs(cx - ox) > 3 || Math.abs(cy - oy) > 3) moved = true;
      if(!raf) raf = requestAnimationFrame(paint);
    };
    let ended = false;
    const up = (e) => {
      // …and only that finger ends it. A window `blur` carries no pointerId, and `move` calls this
      // with nothing at all when a mouse button was released where we could not see it.
      if(e && e.pointerId != null && e.pointerId !== id) return;
      if(ended) return;
      ended = true;
      document.removeEventListener('pointermove', move);
      document.removeEventListener('pointerup', up);
      document.removeEventListener('pointercancel', up);
      window.removeEventListener('blur', up);
      if(raf) cancelAnimationFrame(raf);
      el.classList.remove('dragging');
      el.style.transform = '';
      el.style.left = Math.round(cx) + 'px';
      el.style.top = Math.round(cy) + 'px';
      if(!moved) return;
      /* SWALLOW THE CLICK THIS DRAG IS ABOUT TO PRODUCE. preventDefault on pointerdown does not stop
       * it: the browser still synthesises a click on release, and every panel whose body opens
       * something (Today → Calendar, the blocks → mempool.space, Community → Server Stats) took it.
       * So dragging the Community widget across the desktop ended by opening the Stats window on top
       * of the desktop being arranged. Capture phase, once, and only after a real move — a drag that
       * never left the 3px threshold is a click and must stay one. */
      const eat = (e) => { e.stopPropagation(); e.preventDefault(); };
      el.addEventListener('click', eat, { capture: true, once: true });
      // …and take it back off if no click follows. A drag ended by pointercancel or by the window
      // losing focus produces none, and a listener left armed would eat the NEXT real one instead.
      setTimeout(() => el.removeEventListener('click', eat, { capture: true }), 350);
      // Back to fractions — that is what the document stores, so the panel keeps this edge on every
      // other screen. Guard the divisions: a desk smaller than the widget has no room to be a
      // fraction OF, and 0/0 would write NaN into the document.
      /* The fraction must be expressed in the space drawWidgets READS it in. That maps x to
       * `maxX * x + WGT_GAP/2`, so the committed value has to have the half-gap taken back off —
       * otherwise every drop re-drew the widget 5px right and 5px down of where it was released, and
       * because the next drag starts from that shifted position the error COMPOUNDS: ten small
       * adjustments walk a panel 50px and eventually pin it against the edge. */
      const fx = maxX > 0 ? (cx - WGT_GAP / 2) / maxX : 0, fy = maxY > 0 ? (cy - WGT_GAP / 2) / maxY : 0;
      _apply((doc) => {
        const row = (doc.widgets || []).find(x => x.id === w.id);
        if(!row) return false;
        row.x = Math.max(0, Math.min(1, fx)); row.y = Math.max(0, Math.min(1, fy));
      }).then(ok => { if(ok){ w.x = fx; w.y = fy; } });
    };
    document.addEventListener('pointermove', move);
    document.addEventListener('pointerup', up);
    document.addEventListener('pointercancel', up);
    window.addEventListener('blur', up);
  }

  function addWidget(type){
    if(!WIDGETS[type]) return Promise.resolve(false);
    return _apply((doc) => {
      if(!Array.isArray(doc.widgets)) doc.widgets = [];
      if(doc.widgets.length >= WGT_MAX){
        try{ PC().toast('that is as many widgets as the desktop takes'); }catch(_){}
        return false;
      }
      // Down the right-hand side, out of the icons' way, stepping so a second widget does not land
      // exactly on the first.
      const n = doc.widgets.length;
      doc.widgets.push({ id: type + '-' + Math.random().toString(36).slice(2, 8), type,
                         x: 1, y: Math.min(1, n * 0.22), size: 'm', cfg: {} });
    }).then(ok => { if(ok) drawWidgets(); return ok; });
  }
  function removeWidget(id){
    return _apply((doc) => { doc.widgets = (doc.widgets || []).filter(x => x.id !== id); })
      .then(ok => { if(ok) drawWidgets(); return ok; });
  }
  // The mounted element for a widget row, for the few menu entries that need to reach INTO a panel
  // (the clock's city search draws in its own zone list rather than in a modal).
  function _wgtEl(id){
    if(!desk) return null;
    return desk.querySelector('.os-wgt[data-id="' + (window.CSS && CSS.escape ? CSS.escape(id) : id) + '"]');
  }
  /* A cfg change, applied to the row in the DOCUMENT and never to a captured copy — the same rule
   * the mount closures' `save` follows, and for the same reason: the row object is replaced on every
   * save and on every layout that arrives from another device, so writing one back would undo
   * whatever changed in between. No drawWidgets(): a cfg change is not structural, and rebuilding
   * would throw away the widget's own state (a half-typed search, the note you are in). */
  function _wgtCfg(id, mut){
    return _apply((doc) => {
      const row = (doc.widgets || []).find(x => x.id === id);
      if(!row) return false;
      const cfg = Object.assign({}, row.cfg || {});
      if(mut(cfg) === false) return false;
      row.cfg = cfg;
    });
  }
  function sizeWidget(id, size){
    if(!WGT_SIZES[size]) return Promise.resolve(false);
    return _apply((doc) => {
      const row = (doc.widgets || []).find(x => x.id === id);
      if(!row || row.size === size) return false;
      row.size = size;
    }).then(ok => { if(ok) drawWidgets(); return ok; });
  }

  function wgtMenu(w, x, y){
    const rows = [];
    for(const s of ['s', 'm', 'l']){
      rows.push({ label: (w.size === s ? '• ' : '') + { s: 'Small', m: 'Medium', l: 'Large' }[s],
                  run: () => sizeWidget(w.id, s) });
    }
    if(w.type === 'weather' && w.cfg.lat != null){
      rows.push({ sep: true });
      rows.push({ label: 'Change the place…', run: () => _apply((doc) => {
        const row = (doc.widgets || []).find(x => x.id === w.id);
        if(!row) return false;
        row.cfg = {};
      }).then(ok => { if(ok) drawWidgets(); }) });
    }
    /* The clock's preferences. They live on the menu rather than as controls in the panel because a
     * clock is read from across the room: every pixel spent on a switch is a pixel off the numeral,
     * and these are each set once. `＋` is the exception — adding a city is a thing somebody sets out
     * to do, so it also has a button where they will look for it. */
    if(w.type === 'clock'){
      rows.push({ sep: true });
      rows.push({ label: 'Add a city…', run: () => {
        const el = _wgtEl(w.id), m = el && _mounted.get(el);
        if(m) _clockPicker(el, m.w, m.save);
      } });
      rows.push({ label: w.cfg.sec ? 'Hide seconds' : 'Show seconds',
                  run: () => _wgtCfg(w.id, (cfg) => { cfg.sec = w.cfg.sec ? 0 : 1; }) });
      const h12 = _clockIs12(w.cfg);
      rows.push({ label: h12 ? 'Use a 24-hour clock' : 'Use a 12-hour clock',
                  run: () => _wgtCfg(w.id, (cfg) => { cfg.h12 = h12 ? 0 : 1; }) });
    }
    rows.push({ sep: true });
    rows.push({ label: 'Remove this widget', run: () => removeWidget(w.id) });
    showCtx(x, y, rows);
  }

  /* The picker. Its own panel, like the wallpaper one — this is desktop chrome and the client's modal
   * belongs to the app underneath. Everything already on the desktop is marked rather than hidden: a
   * second ticker or a second note is a reasonable thing to want. */
  function widgetPicker(){
    hideCtx();
    if(!root) return;
    root.querySelectorAll('.os-wgtpick').forEach(n => n.remove());
    const have = new Set((layout().widgets || []).map(w => w.type));
    const m = document.createElement('div');
    m.className = 'os-bgpick os-wgtpick';
    m.innerHTML = `<div class="os-bg-head"><b>Add a widget</b>
        <button class="os-bg-x" id="os-wgt-x" aria-label="Close">✕</button></div>
      <div class="os-wgt-grid">${Object.keys(WIDGETS).map(k => {
        const d = WIDGETS[k];
        return `<button class="os-wgt-pick" data-t="${enc(k)}">
          <svg class="ic" aria-hidden="true"><use href="${enc(d.icon)}"></use></svg>
          <span class="os-wgt-pl">${enc(d.label)}${have.has(k) ? ' <i>· on the desktop</i>' : ''}</span>
          <span class="os-wgt-pb">${enc(d.blurb)}</span></button>`;
      }).join('')}</div>`;
    root.appendChild(m);
    { const x = $('#os-wgt-x', m); if(x) x.onclick = () => m.remove(); }
    $$('.os-wgt-pick', m).forEach(b => b.onclick = () => { m.remove(); addWidget(b.dataset.t); });
  }

  // ---- dragging icons ---------------------------------------------------------------------------

  /* Where a drop would land — arithmetic over rectangles MEASURED ONCE, at the start of the drag.
   *
   * The first version asked the DOM on every pointermove: elementFromPoint, then
   * querySelectorAll('.os-icon') and getBoundingClientRect on the target. Each of those forces the
   * browser to flush style and layout, and a desktop window holds a LIVE timeline — thousands of
   * nodes — so that is three synchronous layouts of a very large document per pointer event, at
   * whatever rate the mouse reports. This file already learned that lesson once, in startDrag:
   * "Dragging used to write style.left/top on every pointermove … that is the tablet sluggishness."
   * I repeated it in the icon drag; this is the same discipline applied here.
   *
   * Nothing MOVES during an icon drag — the source tile stays in place at reduced opacity and the
   * ghost is a separate absolutely-positioned element — so the rectangles cannot go stale. (The one
   * exception is scrolling a folder window mid-drag, which needs a second pointer.)
   *
   *   into   — the middle of another icon: make a folder of the two, or add to the folder that is
   *            already there. The edges stay reorder, so the common gesture cannot be swallowed by
   *            the rarer one (this is how a phone home screen splits the same drop).
   *   before/after — beside that icon, in whichever container it lives in.
   *   end    — empty space: the end of the desktop, or of the folder window it was dropped in. */
  function dragTargets(){
    const out = [];
    for(const n of $$('.os-icon', desk)){
      const slot = n.closest('.osw-slot.os-folder');
      const w = slot && wins.find(x => x.slot === slot);
      const dest = (w && w.view && w.view.indexOf('folder:') === 0) ? w.view.slice(7) : null;
      const r = n.getBoundingClientRect();
      if(!r.width || !r.height) continue;                 // a minimised window's icons
      out.push({ view: n.dataset.view, el: n, dest,
                 l: r.left, t: r.top, w: r.width, h: r.height,
                 slot: slot ? slot.getBoundingClientRect() : null });
    }
    return out;
  }

  function hitTest(targets, cx, cy, dragKey){
    for(const t of targets){
      if(cx < t.l || cx > t.l + t.w || cy < t.t || cy > t.t + t.h) continue;
      if(t.view === dragKey) return null;
      const mid = cx > t.l + t.w * 0.3 && cx < t.l + t.w * 0.7;
      // "Into" is a DESKTOP gesture. Inside a folder window every drop is a reorder — there is
      // nothing sensible for a folder within a folder to mean.
      if(!t.dest && mid && dragKey.indexOf('folder:') !== 0) return { mode: 'into', key: t.view, dest: null, t };
      return { mode: cx < t.l + t.w / 2 ? 'before' : 'after', key: t.view, dest: t.dest, t };
    }
    // Empty space INSIDE an open folder window: the end of that folder.
    for(const t of targets){
      const s = t.slot;
      if(s && cx >= s.left && cx <= s.right && cy >= s.top && cy <= s.bottom)
        return { mode: 'end', key: null, dest: t.dest, t: null };
    }
    // …or the desktop itself. Anything else (a window, the taskbar) is not a drop target at all.
    const d = _deskRect || desk.getBoundingClientRect();
    if(cx >= d.left && cx <= d.right && cy >= d.top && cy <= d.bottom &&
       !wins.some(w => !w.min && _inRect(w.el.getBoundingClientRect(), cx, cy)))
      return { mode: 'end', key: null, dest: null, t: null };
    return null;
  }
  const _inRect = (r, x, y) => x >= r.left && x <= r.right && y >= r.top && y <= r.bottom;
  let _deskRect = null;

  /* Paint the drop indicator ONLY when it CHANGES — not 120 times a second, which is the same rule
   * the window drag's snap ghost follows. The rectangle comes from the cached target, so this costs
   * no layout either. */
  let _dropKey = '', _mark = null, _intoEl = null;
  function paintDrop(hit){
    const key = hit ? hit.mode + ':' + (hit.key || '') + ':' + (hit.dest || '') : '';
    if(key === _dropKey) return;
    _dropKey = key;
    if(_intoEl){ _intoEl.classList.remove('os-into'); _intoEl = null; }
    if(!hit || hit.mode === 'end' || !hit.t){ if(_mark) _mark.style.display = 'none'; return; }
    if(hit.mode === 'into'){
      if(_mark) _mark.style.display = 'none';
      _intoEl = hit.t.el; _intoEl.classList.add('os-into');
      return;
    }
    if(!_mark){ _mark = document.createElement('div'); _mark.className = 'os-ins'; desk.appendChild(_mark); }
    const k = zf();
    _mark.style.display = 'block';
    _mark.style.left = ((hit.mode === 'before' ? hit.t.l : hit.t.l + hit.t.w) / k - 1) + 'px';
    _mark.style.top = (hit.t.t / k) + 'px';
    _mark.style.height = (hit.t.h / k) + 'px';
  }
  function clearDrop(){
    _dropKey = '';
    if(_intoEl){ _intoEl.classList.remove('os-into'); _intoEl = null; }
    if(_mark){ _mark.remove(); _mark = null; }
  }

  let _dragEnd = 0;             // a drag ends with a click on the icon it started from — see wireIcons

  function startIconDrag(icon, ev, srcFolder){
    if(!me()) return;                                   // nothing to save it to
    if(ev.pointerType !== 'touch' && ev.button) return;  // left button (and any touch/pen) only
    const key = icon.dataset.view;
    const k = zf();
    const sx = ev.clientX, sy = ev.clientY;
    let moved = false, gh = null, hit = null, ended = false, longT = 0, targets = [];
    /* Touch has no right button, so a press-and-hold is the context menu. Cancelled by the first
     * movement, which is a drag rather than a hold.
     *
     * `_dragEnd` is stamped BY HAND here, and it is load-bearing: up() only stamps it for a gesture
     * that MOVED, and a long press by definition has not. Without it the finger lifting off fires an
     * ordinary click, which opens the app on top of the menu that was just summoned — and on touch
     * that menu is the only way to hide an icon or take one out of a folder, so the whole of it
     * would be unreachable with a finger. */
    if(ev.pointerType === 'touch'){
      longT = setTimeout(() => {
        if(moved) return;
        up(true);
        _dragEnd = Date.now();
        iconMenu(icon, srcFolder, sx, sy);
      }, 500);
    }
    /* The gesture runs on the COMPOSITOR, exactly as the window drag does: the ghost is placed once
     * and then moved with a transform inside one requestAnimationFrame per frame, rather than
     * written to style.left/top on every pointer event. A mouse can report far more moves than the
     * screen has frames, and each write is a layout of a document holding a live timeline. */
    let cx = 0, cy = 0, raf = 0;
    const paint = () => {
      raf = 0;
      gh.style.transform = `translate(${(cx - sx) / k}px, ${(cy - sy) / k}px)`;
      hit = hitTest(targets, cx, cy, key);
      paintDrop(hit);
    };
    const move = (e) => {
      // A released mouse reports buttons === 0 on its next move. Same guard the window drag needs,
      // for the same reason: the pointerup can be lost entirely.
      if(e.pointerType !== 'touch' && (e.buttons || 0) === 0){ up(); return; }
      if(!moved){
        if(Math.abs(e.clientX - sx) < 6 && Math.abs(e.clientY - sy) < 6) return;
        moved = true;
        clearTimeout(longT);
        icon.classList.add('os-dragging');
        // Measured ONCE, here: every drop target's rectangle, and the desk's. Nothing moves during
        // the drag, so asking the DOM again per pointermove only bought three forced layouts.
        targets = dragTargets();
        _deskRect = desk.getBoundingClientRect();
        gh = document.createElement('div');
        gh.className = 'os-drag';
        gh.innerHTML = icon.innerHTML;
        gh.style.left = (sx / k - ICON_W / 2) + 'px';
        gh.style.top = (sy / k - ICON_H / 2) + 'px';
        desk.appendChild(gh);
      }
      cx = e.clientX; cy = e.clientY;
      if(!raf) raf = requestAnimationFrame(paint);
    };
    function up(cancel){
      if(ended) return;
      ended = true;
      clearTimeout(longT);
      document.removeEventListener('pointermove', move);
      document.removeEventListener('pointerup', end);
      document.removeEventListener('pointercancel', abort);
      window.removeEventListener('blur', abort);
      if(raf){ cancelAnimationFrame(raf); raf = 0; }
      if(gh) gh.remove();
      _deskRect = null;
      clearDrop();
      icon.classList.remove('os-dragging');
      if(!moved) return;
      _dragEnd = Date.now();
      if(cancel) return;
      // Decided from the LAST pointer position, not from whatever the last animation frame got to:
      // a release can land between frames, and dropping on a stale hit files the icon somewhere the
      // user was no longer pointing.
      hit = hitTest(targets, cx, cy, key);
      if(!hit) return;
      // Where the GHOST is, in layout px relative to the desk — not where the pointer is. Dropping
      // should leave the icon where it looked like it was going to land.
      const k2 = zf(), dr2 = desk.getBoundingClientRect();
      const at = hit.dest ? null
        : { x: (cx - dr2.left) / k2 - ICON_W / 2, y: (cy - dr2.top) / k2 - ICON_H / 2 };
      drop(key, srcFolder, hit, at);
    }
    /* A RELEASE commits the drop; a CANCELLED gesture must not. They were the same handler, and the
     * difference is a rearrangement the user never asked for and that is then written to the relay:
     * a folder window's list keeps its touch-action so it can scroll, so a finger scrolling one
     * moves past the 6px threshold, the browser claims the gesture and fires pointercancel — and
     * committing there files the icon wherever the finger happened to be. Alt-tabbing mid-drag is
     * the same shape with a mouse. */
    const end = () => up(false);
    const abort = () => up(true);
    document.addEventListener('pointermove', move);
    document.addEventListener('pointerup', end);
    document.addEventListener('pointercancel', abort);
    window.addEventListener('blur', abort);            // alt-tabbed away mid-drag
  }

  function drop(key, srcFolder, hit, at){
    if(hit.mode === 'into'){
      mergeInto(key, hit.key).then(made => {
        if(!made) return;
        // Named right away, because "New folder" is not a name and nothing else prompts for one.
        // Cancelling keeps the folder — the drop already happened, and undoing it silently would be
        // a second surprise.
        const ask = PC().uiPrompt;
        if(!ask) return;
        ask('Name this folder', { value: 'New folder', placeholder: 'Folder name' })
          .then(n => { if(n && n.trim()) renameFolder(made, n); })
          .catch(() => {});
      });
      return;
    }
    const after = hit.mode === 'after';
    if(hit.dest){ toFolder(hit.dest, key, hit.key, after); return; }
    /* Dropped on the DESKTOP. Which of the two things that means depends on what the desktop IS:
     *
     *   a GRID — dropping beside another icon reorders (the grid decides where things sit, so a
     *            position would be meaningless); dropping on empty space places it there, and that
     *            is the move that turns the desktop into a canvas.
     *   a CANVAS — every drop is a placement. Reordering here would change a list nothing draws:
     *            the icon would stay exactly where it was while the document said it had moved,
     *            which is the worst of both.
     */
    const free = Object.keys((_lay && _lay.pos) || {}).length > 0;
    /* Turning the grid into a canvas is a decision, so it takes a deliberate move: dragging an icon
     * that is ALREADY on the desktop. Dragging one OUT OF A FOLDER onto empty space is a different
     * gesture with a different meaning — "put this back on the desktop" — and having it silently
     * switch the whole desktop to free placement (and freeze every other icon where it happened to
     * be) is a side effect nobody asked for. Once the desktop IS a canvas, every drop is a
     * placement, including that one: there is no grid left to append to. */
    if(at && (free || (hit.mode === 'end' && !srcFolder))) placeIcon(key, at.x, at.y, gridPositions());
    else toDesk(key, hit.key, after);
    // srcFolder is not consulted: toDesk/toFolder both pluck the view out of whatever folder held
    // it, so "out of a folder" and "between two folders" are the same write.
  }

  // ---- the right-click menus --------------------------------------------------------------------

  function hideCtx(){
    if(!root) return;
    root.querySelectorAll('.os-ctx').forEach(n => n.remove());
    document.removeEventListener('pointerdown', _ctxAway, true);
  }
  function _ctxAway(e){ if(!e.target.closest || !e.target.closest('.os-ctx')) hideCtx(); }

  function showCtx(x, y, rows){
    hideCtx();
    if(!rows.length) return;
    const m = document.createElement('div');
    m.className = 'os-ctx';
    m.innerHTML = rows.map((r, i) => r.sep ? '<hr>'
      : `<button class="os-ctx-b" data-i="${i}">${enc(r.label)}</button>`).join('');
    desk.appendChild(m);
    const k = zf();
    // Clamped so the menu is never opened off the edge it was summoned at — a right-click in the
    // bottom-right corner is exactly where this happens.
    const w = m.offsetWidth || 200, h = m.offsetHeight || 80;
    m.style.left = Math.max(4, Math.min(vwL() - w - 4, x / k)) + 'px';
    m.style.top = Math.max(4, Math.min(vhL() - TASKBAR - h - 4, y / k)) + 'px';
    $$('.os-ctx-b', m).forEach(b => b.onclick = () => {
      const r = rows[b.dataset.i | 0];
      hideCtx();
      if(r && r.run) try{ r.run(); }catch(_){}
    });
    document.addEventListener('pointerdown', _ctxAway, true);
  }

  function iconMenu(icon, srcFolder, x, y){
    const key = icon.dataset.view;
    const lay = layout();
    const rows = [{ label: 'Open', run: () => openApp(key) }];
    if(key.indexOf('folder:') === 0){
      const fk = key.slice(7);
      rows.push({ sep: true });
      rows.push({ label: 'Rename folder…', run: () => {
        const f = lay.folders.find(x => x.key === fk);
        const ask = PC().uiPrompt;
        if(!ask) return;
        ask('Rename folder', { value: (f && f.label) || '' })
          .then(n => { if(n && n.trim()) renameFolder(fk, n); }).catch(() => {});
      } });
      rows.push({ label: 'Take folder apart', run: () => ungroup(fk) });
    }else{
      rows.push({ sep: true });
      if(srcFolder) rows.push({ label: 'Move to desktop', run: () => toDesk(key, null, false) });
      else rows.push({ label: 'Hide from desktop', run: () => hideItem(key) });
    }
    showCtx(x, y, rows);
  }

  /* THE WALLPAPER PICKER. Its own panel rather than the app's modal: this is desktop chrome, the
   * modal belongs to the client underneath, and everything here is already drawn this way (the
   * notification centre, the network flyout, the context menus).
   *
   * The pictures come from ONE place — a folder called `Backgrounds` in your encrypted drive — and
   * that is the whole configuration: put a picture there from Files and it is offered here. Nothing
   * is uploaded from this screen, because a wallpaper picker that can upload is a file manager, and
   * there is one of those already. */
  function wallpaperPicker(){
    hideCtx();
    if(!root) return;
    root.querySelectorAll('.os-bgpick').forEach(n => n.remove());
    const pics = backgrounds();
    const m = document.createElement('div');
    m.className = 'os-bgpick';
    m.innerHTML =
      `<div class="os-bg-head"><b>Desktop background</b>
         <button class="os-bg-x" id="os-bg-x" aria-label="Close">✕</button></div>
       ${pics.length ? `<div class="os-bg-grid">
           <button class="os-bg-item${_bgSha ? '' : ' on'}" data-sha=""><span class="os-bg-none">Default</span></button>
           ${pics.map(p => `<button class="os-bg-item${p.sha === _bgSha ? ' on' : ''}" data-sha="${enc(p.sha)}"
                title="${enc(p.name)}"><img alt="" data-lazy="${enc(p.sha)}"><span>${enc(p.name)}</span></button>`).join('')}
         </div>`
        : `<div class="os-bg-empty">
             <p>No pictures yet.</p>
             <p class="muted small">Make a folder called <b>Backgrounds</b> in Files → Blossom and put
                some images in it. They stay encrypted — the desktop decrypts them here.</p>
             <button class="btn btn-cyan small" id="os-bg-files">Open Files</button></div>`}`;
    root.appendChild(m);
    { const x = $('#os-bg-x', m); if(x) x.onclick = () => m.remove(); }
    { const f = $('#os-bg-files', m); if(f) f.onclick = () => { m.remove(); openApp('blossom'); }; }
    // Thumbnails are decrypted one at a time, after the panel is up: a folder of 4K wallpapers is
    // tens of megabytes, and doing it before showing anything is a picker that takes ten seconds to
    // appear. A tile that fails SAYS SO — it used to leave the <img> blank, which is indistinguishable
    // from one still decrypting and from a picker that simply doesn't work, and the same fetch is what
    // the choice below runs, so a blank grid is a preview of a click that will also do nothing.
    (async () => {
      for(const img of $$('img[data-lazy]', m)){
        if(!img.isConnected) return;
        try{
          const u = await PC().encFileUrl(img.dataset.lazy);
          if(!img.isConnected) return;
          if(u) img.src = u; else _bgTileFailed(img, 'decrypted to nothing');
        }catch(e){ _bgTileFailed(img, (e && (e.message || e.name)) || 'could not read it'); }
      }
    })();
    $$('.os-bg-item', m).forEach(b => b.onclick = async () => {
      const sha = b.dataset.sha || '';
      m.remove();
      const why = await applyWallpaper(sha);   // instant, so the choice is visible before the relay answers
      // A refused picture must not be written to the document as the one in force: every other device
      // would then try to paint a wallpaper this one already knows it cannot read.
      if(why){ try{ PC().toast && PC().toast('couldn’t use that picture — ' + why); }catch(_){} return; }
      setWallpaper(sha);
    });
  }

  // Put the reason ON the tile. `title` too, because the grid cell is 84px and a decrypt error is not.
  function _bgTileFailed(img, why){
    const b = img.closest('.os-bg-item'); if(!b) return;
    const n = document.createElement('span');
    n.className = 'os-bg-none os-bg-err';
    n.textContent = '⚠ ' + String(why).slice(0, 60);
    b.title = (b.title ? b.title + ' — ' : '') + String(why).slice(0, 160);
    img.replaceWith(n);
  }

  function deskMenu(x, y){
    const lay = layout();
    const rows = [];
    for(const a of lay.hidden.slice(0, 12)) rows.push({ label: 'Show ' + a.label, run: () => showItem(a.view) });
    if(rows.length) rows.push({ sep: true });
    if(Object.keys(lay.pos || {}).length)
      rows.push({ label: 'Line the icons up', run: () => lineUp() });
    rows.push({ label: 'Add a widget…', run: () => widgetPicker() });
    rows.push({ label: 'Change background…', run: () => wallpaperPicker() });
    rows.push({ sep: true });
    rows.push({ label: 'Restore the default layout', run: async () => {
      const ask = PC().uiConfirm;
      if(ask && !await ask('Put every icon back where it started? Your folders and hidden icons go with it.')) return;
      resetLayout();
    } });
    showCtx(x, y, rows);
  }

  /* Click to open, drag to arrange, right-click (or press and hold) for the rest. Wired on the
   * desktop grid AND on each folder window's grid, which is what lets an icon be dragged out of a
   * folder, into another one, or back. */
  function wireIcons(box, folder){
    $$('.os-icon', box).forEach(b => {
      // Single click opens. A desktop double-click is the convention, but this is a web app people
      // arrive at from a single-click UI, and a double-click that does nothing the first time reads
      // as broken. A click that is the tail of a drag is not a click, and the ONE thing that must
      // not happen is a rearranged icon also opening its app.
      b.onclick = () => { if(Date.now() - _dragEnd < 250) return; openApp(b.dataset.view); };
      b.oncontextmenu = (e) => { e.preventDefault(); e.stopPropagation(); iconMenu(b, folder, e.clientX, e.clientY); };
      b.addEventListener('pointerdown', (e) => startIconDrag(b, e, folder));
    });
    // Inside a folder WINDOW the gaps belong to the folder itself — its own rename / take-apart.
    // (The desktop grid's gaps are handled on the desk, which is bigger than the grid.)
    if(folder) box.oncontextmenu = (e) => {
      if(e.target.closest && e.target.closest('.os-icon')) return;
      e.preventDefault();
      e.stopPropagation();
      iconMenu({ dataset: { view: 'folder:' + folder.key } }, null, e.clientX, e.clientY);
    };
  }

  /* Your account, at the foot of the start menu — where Windows 11 puts it. The classic UI reaches
   * your profile through #me-card in the sidebar, which the desktop hides, so without this there is
   * no route to your own profile or to the account switcher at all. The picture and the name are
   * read from that card rather than re-fetched, so they can never disagree with it. */
  /* Full screen — the desktop with nothing else around it, which is the whole point of a desktop
   * inside a browser tab. The request MUST come from a user gesture (a click on the start-menu
   * entry, or the key handler below), and browsers can still refuse it — iOS Safari has no element
   * fullscreen at all — so the failure is reported rather than swallowed. Prefixed calls are kept
   * for the older WebView the APK can ship on. */
  const isFull = () => !!(document.fullscreenElement || document.webkitFullscreenElement);
  async function toggleFull(){
    try{
      if(isFull()){
        if(document.exitFullscreen) await document.exitFullscreen();
        else if(document.webkitExitFullscreen) document.webkitExitFullscreen();
        return;
      }
      const el = document.documentElement;
      if(el.requestFullscreen) await el.requestFullscreen({ navigationUI: 'hide' });
      else if(el.webkitRequestFullscreen) el.webkitRequestFullscreen();
      else throw new Error('unsupported');
    }catch(err){
      try{ PC().toast('this browser would not go full screen'); }catch(_){}
    }
  }

  function meChip(){
    if(!me()) return '';
    let src = '', name = '';
    try{
      const img = document.querySelector('#me-card img'); if(img) src = img.getAttribute('src') || '';
      const mn = document.querySelector('#me-card .mn'); if(mn) name = (mn.textContent || '').trim();
    }catch(_){}
    const pic = src ? `<img src="${enc(src)}" alt="">`
                    : '<svg class="ic" aria-hidden="true"><use href="#i-user"></use></svg>';
    return `<button class="os-acct" id="os-acct" title="Accounts">${pic}
              <span>${enc(name || 'My account')}</span>
              <i aria-hidden="true">⌃</i></button>`;
  }

  function drawBar(){
    if(!bar) return;
    // Remember whether the search box had the caret BEFORE the rebuild throws the element away.
    try{ barFocused = barFocused || (document.activeElement && document.activeElement.id === 'os-q-bar'); }catch(_){}
    const t = new Date();
    const clock = t.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
    const date = t.toLocaleDateString([], { day: 'numeric', month: 'short' });
    const netNow = netState();
    bar.innerHTML =
      `<button class="os-start${startOpen ? ' on' : ''}" id="os-start" title="Start">
         <img src="${enc(brandLogo())}" alt="Start"></button>
       <div class="os-qbox">
         <svg class="ic" aria-hidden="true"><use href="#i-search"></use></svg>
         <input id="os-q-bar" class="os-qin" type="search" autocomplete="off"
                value="${enc(barQuery)}" placeholder="Search Nostr" aria-label="Search Nostr"></div>
       <div class="os-tasks">${wins.map(w =>
         `<button class="os-task${w.el.classList.contains('focused') && !w.min ? ' on' : ''}"
                  data-id="${w.id}" title="${enc(w.title)}">
            ${iconSvg(w.icon)}<span>${enc(w.title)}</span></button>`).join('')}</div>
       <div class="os-tray">
         <button class="os-net net-${netNow.level}${netOpen ? ' on' : ''}" id="os-net"
                 title="${enc(NET_LABEL[netNow.level] + ' — ' + netSummary(netNow))}"
                 aria-label="${enc('Nostr connection: ' + netSummary(netNow))}">
           <svg class="ic" aria-hidden="true"><use href="#i-relay"></use></svg></button>
         <button class="os-bell${notiOpen ? ' on' : ''}" id="os-bell"
                 title="${enc(notiTitle())}" aria-label="${enc(notiTitle())}">
           <svg class="ic" aria-hidden="true"><use href="#i-bell"></use></svg>${notiDot()}</button>
         <button class="os-clock${notiOpen ? ' on' : ''}" id="os-clock" title="Notifications">
           <b>${enc(clock)}</b><span>${enc(date)}</span></button>
       </div>`;
    $('#os-start', bar).onclick = (e) => { e.stopPropagation(); toggleStart(); };
    /* There is deliberately no New post button here. One was added when posting appeared impossible
     * on the desktop — but the real cause was the compose MODAL rendering behind it (see the
     * z-index block in client.css). With that fixed, the timeline's own composer works inside a
     * window, and a second entry point on the taskbar is just clutter. */
    /* The taskbar box searches NOSTR, not the app list — the start menu already filters apps, and a
     * second app filter next to it would be the least useful thing that box could do. Results land
     * in their own window, so searching does not throw away whatever the focused app was showing. */
    { const qb = $('#os-q-bar', bar);
      if(qb){
        /* drawBar() rebuilds the whole bar — on every window focus, and on the clock tick — so the
         * text has to live outside the DOM or it is wiped mid-sentence. Kept here, restored above,
         * and the caret put back if the box was the thing you were typing in. */
        qb.addEventListener('input', () => { barQuery = qb.value; });
        if(barFocused){ qb.focus(); try{ qb.setSelectionRange(barQuery.length, barQuery.length); }catch(_){} barFocused = false; }
        qb.addEventListener('blur', () => { barFocused = false; });
        qb.addEventListener('keydown', (e) => {
          if(e.key === 'Escape'){ qb.value = ''; barQuery = ''; qb.blur(); return; }
          if(e.key !== 'Enter') return;
          const q = qb.value.trim();
          if(!q) return;
          // Submitting empties the box. It is a search bar, not a location bar — leaving the query
          // sitting there just means the next search starts by clearing it.
          qb.value = ''; barQuery = ''; qb.blur();
          const run = () => { try{ PC().runSearch && PC().runSearch(q); }
                              catch(err){ PC().toast && PC().toast('search is unavailable here'); } };
          // `rerun`: one Search window shows a succession of queries, so a second search has to
          // REPLACE what it is rendering. Without it the window came forward re-running the first
          // query — see openDoc.
          openDoc('search', 'Search', 'i-search', run, false, true);
        });
      } }
    /* The BELL is the notification button; the clock still opens the same panel because it always
     * has and people have learned it. A count sitting on a clock is not a notification indicator —
     * it reads as part of the time, there is nothing to tell you it can be pressed, and every other
     * desktop puts a bell there. The badge moved with it, so the clock is a clock again. */
    { const bb = $('#os-bell', bar); if(bb) bb.onclick = (e) => { e.stopPropagation(); toggleNoti(); }; }
    { const cb = $('#os-clock', bar); if(cb) cb.onclick = (e) => { e.stopPropagation(); toggleNoti(); }; }
    { const nb = $('#os-net', bar); if(nb) nb.onclick = (e) => { e.stopPropagation(); toggleNet(); }; }
    $$('.os-task', bar).forEach(b => b.onclick = () => {
      const w = wins.find(x => String(x.id) === b.dataset.id);
      if(!w) return;
      // Clicking the focused window's own task button minimises it, the way a taskbar does.
      if(w.el.classList.contains('focused') && !w.min) minimise(w); else focusWin(w);
    });
  }

  // ---- notification centre ---------------------------------------------------------------------
  // Windows opens this from the clock, and so does this. The rows are the client's OWN notification
  // rows (PC.notifHtml), not a second rendering of the same data: notifList is the single gate that
  // decides what counts as a notification, and a re-implementation here would drift from it silently
  // — exactly how kinds 1621/1617/42/1111 once got fetched, toasted and then never shown.
  let notiOpen = false, mailAck = 0;
  let barQuery = '', barFocused = false;   // the taskbar search box survives a drawBar() rebuild

  /* UNREAD IS ASKED FOR, NOT RECONSTRUCTED, and that is the whole of "the bell lights when you log in
   * and never again". This used to be `notifItems(60).length` minus a `notiSeen` snapshot taken when
   * the centre was last opened — but that list is SLICED TO 60, so on any account past 60
   * notifications its length is the constant 60: the first login (notiSeen still 0) showed 60 and
   * every open thereafter pinned notiSeen at 60, leaving 60-60=0 for the rest of the session however
   * much arrived. The bell was measuring the length of a capped list, which is not a quantity that
   * can grow. PC().notifUnread() is the same computation the sidebar badge is painted from — one
   * definition of unread, four surfaces — and the centre clears it by MARKING READ (notifsRead),
   * which is a thing the whole app agrees about rather than a number this file remembered. */
  function notiCount(){
    try{ return (PC().notifUnread && PC().notifUnread()) || 0; }catch(_){ return 0; }
  }

  /* What the bell is for, in words — the tooltip and the screen-reader label. "Notifications" on a
   * bell showing 3 says nothing the icon did not. */
  function notiTitle(){
    let n = 0, mail = 0;
    try{ n = Math.max(0, notiCount()); }catch(_){}
    try{ mail = Math.max(0, ((PC().mailUnread && PC().mailUnread()) || 0) - mailAck); }catch(_){}
    if(!n && !mail) return 'Notifications';
    const bits = [];
    if(n) bits.push(n + ' new notification' + (n === 1 ? '' : 's'));
    if(mail) bits.push(mail + ' new email' + (mail === 1 ? '' : 's'));
    return bits.join(' · ');
  }

  function notiDot(){
    let n = 0, mail = 0;
    try{ n = notiCount(); }catch(_){}
    try{ mail = (PC().mailUnread && PC().mailUnread()) || 0; }catch(_){}
    // The tray count means "new since you last looked", not "unread forever" — opening Messages or
    // the centre acknowledges what is there. Without this the number sat on the clock after you had
    // read the mail, because Mail.unread only falls when a message is actually marked read.
    if(mail < mailAck) mailAck = mail;          // mail really was read → let the ack fall with it
    const total = Math.max(0, n) + Math.max(0, mail - mailAck);
    return total > 0 ? `<i class="os-dot">${enc(total > 99 ? '99+' : String(total))}</i>` : '';
  }

  function hideNoti(){
    notiOpen = false;
    const p = $('#os-noti', root);
    if(p) p.remove();
  }

  function toggleNoti(force){
    notiOpen = (force === undefined) ? !notiOpen : !!force;
    const old = $('#os-noti', root);
    if(old) old.remove();
    if(!notiOpen){ drawBar(); return; }
    toggleStart(false); toggleNet(false);
    const panel = document.createElement('div');
    panel.id = 'os-noti';
    panel.className = 'os-noti';
    let items = [];
    try{ items = (PC().notifItems && PC().notifItems(60)) || []; }catch(_){}
    let mail = 0;
    try{ mail = (PC().mailUnread && PC().mailUnread()) || 0; }catch(_){}
    /* System alerts. A desktop's notifications are only useful when the window is behind something
     * else, and that needs OS permission — which can only be asked from a click, so it has to be
     * offered somewhere rather than requested at boot (asking on load pops the browser's chrome and
     * gets refused by reflex). Shown only while it is not granted; 'denied' cannot be re-asked, so
     * it says where to change it instead of offering a button that does nothing. */
    let perm = 'unsupported';
    try{ perm = (PC().osNotifyState && PC().osNotifyState()) || 'unsupported'; }catch(_){}
    const permRow = perm === 'granted' || perm === 'unsupported' ? ''
      : (perm === 'denied'
          ? `<div class="os-noti-perm muted small">System alerts are blocked for this site — turn them
               back on in your browser's site settings.</div>`
          : `<button class="os-noti-perm as-btn" id="os-noti-perm">
               <svg class="ic" aria-hidden="true"><use href="#i-bell"></use></svg>
               <span><b>Turn on system alerts</b><i>Reminders and messages reach you while this window
                 is behind another one.</i></span></button>`);
    const mailRow = mail > 0
      ? `<button class="os-noti-mail" id="os-noti-mail">
           <svg class="ic" aria-hidden="true"><use href="#i-mail"></use></svg>
           <b>${enc(String(mail))}</b><span>unread email</span></button>` : '';
    let rows = '';
    try{ rows = items.map(e => `<div class="os-noti-row">${PC().notifHtml(e)}</div>`).join(''); }catch(_){}
    panel.innerHTML =
      `<div class="os-noti-head"><b>Notifications</b>
         <span class="os-noti-hb">
           <button class="os-noti-x" id="os-noti-ding"
                   title="${settings().get('osDing', true) ? 'Mute the arrival sound' : 'Unmute the arrival sound'}"
                   aria-label="Notification sound">${settings().get('osDing', true) ? '🔔' : '🔕'}</button>
           <button class="os-noti-x" id="os-noti-all" title="Open the Notifications app">Open all</button>
         </span></div>
       ${permRow}${mailRow}
       <div class="os-noti-list">${rows || '<div class="empty">Nothing new.</div>'}</div>`;
    root.appendChild(panel);

    { const pb = $('#os-noti-perm', panel);
      if(pb && pb.tagName === 'BUTTON') pb.onclick = async (e) => {
        e.stopPropagation();
        try{ await PC().askOsNotify(); }catch(_){}
        toggleNoti(true);                 // repaint: the row goes away once it is granted
      }; }
    if(mail > 0) $('#os-noti-mail', panel).onclick = () => { hideNoti(); openApp('mail'); };
    mailAck = mail;                              // looking at the centre counts as looking
    $('#os-noti-all', panel).onclick = () => { hideNoti(); openApp('notifications'); };
    $('#os-noti-ding', panel).onclick = (e) => {
      e.stopPropagation();
      const nowOn = !settings().get('osDing', true);
      settings().set('osDing', nowOn);
      if(nowOn) ding();                    // hear what you just switched on
      toggleNoti(true);                    // repaint the header
    };

    // Same wiring the Notifications view uses: the row opens the post, the avatar opens the sender.
    // Both land in their own window, which is where reply and react already live in full.
    panel.querySelectorAll('.notif:not(.upd-notif)').forEach(n => {
      n.onclick = (ev) => {
        if(ev.target.closest('.os-noti-act')) return;
        hideNoti();
        try{
          if(n.dataset.prof) PC().openProfile(n.dataset.prof);
          else if(n.dataset.open) PC().openThread(n.dataset.open);
        }catch(_){}
      };
      // …plus reply and react right here, so acknowledging something never costs a window.
      const id = n.dataset.open, pk = n.dataset.pk || '';
      if(!id) return;
      const acts = document.createElement('div');
      acts.className = 'os-noti-act';
      acts.innerHTML =
        `<button class="os-na" data-a="reply" title="Reply"><svg class="ic" aria-hidden="true"><use href="#i-reply"></use></svg></button>
         <button class="os-na" data-a="react" title="React"><svg class="ic" aria-hidden="true"><use href="#i-heart"></use></svg></button>`;
      n.appendChild(acts);
      $$('.os-na', acts).forEach(b => b.onclick = (ev) => {
        ev.stopPropagation();
        try{
          if(b.dataset.a === 'reply'){ hideNoti(); PC().compose({ reply: id, replyPk: pk }); }
          else PC().reactTo(id, pk, b);       // the emoji picker anchors itself to this button
        }catch(_){ try{ PC().toast('could not do that here'); }catch(__){} }
      });
    });

    // Looking at the centre IS reading them — and it is recorded where the whole app can see it
    // (seenNotif.last), never as a private count in this file.
    try{ PC().notifsRead && PC().notifsRead(); }catch(_){}
    drawBar();
  }

  /* Repaint the BELL IN PLACE. Same reason as paintNetButton: drawBar() rebuilds bar.innerHTML,
   * which destroys and recreates the Search Nostr input — and a notification arriving is not
   * something the user did, so it must not eat the caret of whoever is mid-word. */
  function paintBell(){
    const b = bar && $('#os-bell', bar);
    if(!b) return;                       // no bar/button yet — the next drawBar() paints it
    const t = notiTitle();
    b.className = 'os-bell' + (notiOpen ? ' on' : '');
    b.title = t;
    b.setAttribute('aria-label', t);
    b.innerHTML = '<svg class="ic" aria-hidden="true"><use href="#i-bell"></use></svg>' + notiDot();
  }

  /* app.js calls this whenever the unread count can have moved (an arrival, or marking them read).
   * DEBOUNCED because it is called once per event, and the opening flood is hundreds — one repaint
   * after the burst says exactly what a repaint per event would. */
  let _bellT = null;
  function notifChanged(){
    if(!on) return;
    clearTimeout(_bellT);
    _bellT = setTimeout(() => { _bellT = null; if(on) paintBell(); }, 200);
  }

  // ---- network status --------------------------------------------------------------------------
  /* The taskbar's networking widget, in the shape Windows put it: state of the connection in the
   * tray, and a flyout naming what you are connected to with a way to fix it.
   *
   * The desktop had NO connection indicator at all. The classic client's is `#conn-status` in the
   * topbar — and the topbar is one of the things entering the desktop replaces, so the single most
   * useful piece of ambient state in a Nostr client disappeared exactly when the app started looking
   * like an OS. A dead relay then presents as a timeline that has simply stopped, which is
   * indistinguishable from a quiet one.
   *
   * Per-RELAY, not one word for the pool, because that is the question a Nostr user actually has: a
   * pool reads 'ok' while three of its five relays are down, and "online" over a half-dead pool is
   * how you spend ten minutes wondering why your posts are not showing up on someone else's client.
   */
  let netOpen = false, _netOff = null, _netT = null;

  /* Past this, a socket is not talking. The heartbeat answers its own ping every 25s and a trusted
   * socket that reaches 40s idle tears itself down and reconnects, so 45s means the reconnect is
   * already due. Deliberately NOT treated as "down": it is shown as its own quieter state, because
   * the heartbeat is skipped while the tab is in the background, so a desktop returning to the
   * foreground legitimately has stale sockets for a moment. */
  const NET_STALE_MS = 45000;
  const NET_LABEL = { ok: 'Connected', partial: 'Partly connected',
                      connecting: 'Connecting…', off: 'No connection' };

  function netConns(){
    try{ return (window.Relay && Relay.conns && Relay.conns()) || []; }catch(_){ return []; }
  }

  function netState(){
    const conns = netConns();
    const up = conns.filter(netLive).length;
    const total = conns.length;
    let level;
    if(!total) level = 'off';
    else if(up === 0) level = conns.some(c => c.status === 'connecting') ? 'connecting' : 'off';
    else if(up < total) level = 'partial';
    else level = 'ok';
    /* The radio being off outranks whatever the pool believes: a WebSocket can read OPEN for a while
     * after the network goes, so a widget that trusted the pool alone would sit on "Connected" while
     * the machine was plainly offline — the one moment its answer has to be right. */
    let online = true;
    try{ online = navigator.onLine !== false; }catch(_){}
    if(!online) level = 'off';
    return { level, up, total, online, conns };
  }

  // "3 of 5 relays" — the summary the tray icon is standing in for.
  function netSummary(s){
    if(!s.online) return 'This device is offline';
    if(!s.total) return 'No relays configured';
    if(s.level === 'ok') return s.total === 1 ? 'Connected to 1 relay'
                                             : `Connected to all ${s.total} relays`;
    if(s.level === 'connecting') return 'Connecting…';
    if(s.level === 'off') return s.total === 1 ? 'Relay unreachable'
                                              : `None of ${s.total} relays reachable`;
    return `${s.up} of ${s.total} relays connected`;
  }

  // wss://relay.example.com/ → relay.example.com. The scheme is noise in a list of relays, and the
  // full URL wraps; the row's title carries the whole thing for anyone who needs it.
  function netHost(url){
    try{ return new URL(url).host || url; }catch(_){ return String(url || '').replace(/^wss?:\/\//, ''); }
  }

  // A bare DURATION, no "ago" — the only caller prefixes "No data for ", and appending it here read
  // as "No data for 46s ago".
  function netDur(ms){
    if(ms == null) return '';
    const s = Math.round(ms / 1000);
    if(s < 60) return s + 's';
    if(s < 5400) return Math.round(s / 60) + 'm';
    return Math.round(s / 3600) + 'h';
  }
  /* Is this socket actually carrying traffic? OPEN is not the same as alive: a proxy idle-closes a
   * WebSocket and the browser still reports readyState 1 (the zombie relay.js's heartbeat exists to
   * catch). Counting one of those as up let the summary say "Connected to all 3 relays" directly
   * above a row reading "No data for 5m" — the header contradicting the list it heads, on the one
   * screen whose whole job is answering whether the connection is healthy. */
  function netLive(c){
    return c.status === 'ok' && c.open && !(c.idle != null && c.idle > NET_STALE_MS);
  }

  function netRow(c){
    const open = c.status === 'ok' && c.open;
    const cls = open ? (netLive(c) ? 'ok' : 'quiet')
              : c.status === 'connecting' ? 'connecting' : 'off';
    const what = cls === 'ok' ? 'Connected'
               : cls === 'quiet' ? 'No data for ' + netDur(c.idle)
               : cls === 'connecting' ? 'Connecting…' : 'Unreachable';
    // "This server's relay" earns its label: it is the one the instance runs, the only TRUSTED
    // socket, and the only one whose events skip signature verification — so which one it is is
    // worth being able to see when the others are misbehaving.
    const tag = c.trusted ? '<i class="os-net-tag">this server</i>' : '';
    return `<div class="os-net-row" title="${enc(c.url)}">
              <span class="os-net-led ${cls}" aria-hidden="true"></span>
              <span class="os-net-n">${enc(netHost(c.url))}${tag}</span>
              <span class="os-net-s">${enc(what)}</span>
            </div>`;
  }

  /* The community counters, under the relays they are counted on. Read from the client's OWN cache
   * (PC.communityStats) rather than polling /client/stats from here: that endpoint counts its caller
   * as a viewer, so a second poll would inflate "online" by one — and this panel repaints every five
   * seconds while it is open, which would have made the number climb as you watched it.
   *
   * All five, always, INCLUDING the zeroes. "0 live" is an answer; a row that disappears when it is
   * zero just looks like the feature is missing, which is exactly what happened the last time these
   * were made conditional. A standalone build has no instance to ask, and there the whole strip is
   * dropped — a row of five zeroes there would be a lie rather than an answer. */
  function netStatsHtml(){
    let st = null;
    try{ st = (PC().communityStats && PC().communityStats()) || null; }catch(_){ st = null; }
    /* An instance-less build has no counters to show, and the test for that is whether the numbers
     * were ever FETCHED — not whether communityStats() returned something. It is defined
     * unconditionally in app.js and answers with a zeroed object, so `if(!st)` never fired and a
     * standalone desktop rendered five authoritative-looking zeroes: "0 WoT, 0 online" reads as a
     * dead network rather than as no server to ask. */
    if(!st || !st.fetched) return '';
    /* The CELLS come from _statCells — the same list the desktop's Community widget draws. They were
     * written out twice, in the same order with the same icons and labels and the same "all five,
     * always" rule stated in both comments, which is two places to rename a counter and one place to
     * forget. Only the markup differs: a tray row is a line of text, the widget is a grid of tiles. */
    return `<div class="os-stats os-net-stats">${_statCells(st).map(c =>
              `<span class="os-stat${c.n > 0 ? ' on' : ''}" title="${enc(c.label)}">${iconSvg(c.icon)}<b>${
                enc(String(c.n || 0))}</b><i>${enc(c.label)}</i></span>`).join('')}</div>`;
  }

  function hideNet(){
    netOpen = false;
    clearInterval(_netT); _netT = null;
    const p = $('#os-net-panel', root);
    if(p) p.remove();
  }

  function paintNet(){
    const panel = $('#os-net-panel', root);
    if(!panel) return;
    const s = netState();
    panel.innerHTML =
      `<div class="os-noti-head"><b>Nostr</b>
         <span class="os-noti-hb">
           <button class="os-noti-x" id="os-net-relays">Relays…</button>
           <button class="os-noti-x" id="os-net-again">Reconnect</button>
         </span></div>
       <div class="os-net-sum net-${s.level}">
         <svg class="ic" aria-hidden="true"><use href="#i-relay"></use></svg>
         <span><b>${enc(NET_LABEL[s.level] || '')}</b><i>${enc(netSummary(s))}</i></span></div>
       <div class="os-net-list">${
         s.conns.length ? s.conns.map(netRow).join('')
                        : '<div class="os-net-empty muted small">This client has no relays to talk to. Add one in Settings → Relays.</div>'}</div>
       ${netStatsHtml()}`;
    { const b = $('#os-net-again', panel); if(b) b.onclick = (e) => {
        e.stopPropagation();
        /* wake(), not reviveStale(): this is someone telling the machine the connection is wrong,
         * and reviveStale deliberately spares sockets that merely LOOK fine — which is every socket
         * in the case that makes a person reach for this button (a zombie reads OPEN). */
        try{ window.Relay && Relay.wake && Relay.wake(); }catch(_){}
        paintNet();
      }; }
    { const b = $('#os-net-relays', panel); if(b) b.onclick = (e) => {
        e.stopPropagation(); hideNet(); openApp('settings', 'Settings', '#i-gear');
      }; }
  }

  /* ONE click-away handler for BOTH tray panels, and that is the fix rather than a tidy-up.
   *
   * Two independent handlers raced. Each closes its own panel by calling drawBar(), which rebuilds
   * bar.innerHTML — so whichever fired first DETACHED the very button the pointerdown was aimed at,
   * and that button's click listener then never ran. Clicking the network icon while the
   * notification centre happened to be open did nothing at all: the notifications closed, the
   * network flyout never opened, and the icon read as dead.
   *
   * So every tray TRIGGER is excluded here, not just this panel's own — a click on a sibling button
   * must reach that button, which cross-closes on its open path.
   *
   * The BELL was missing from this list, and that is the same bug with a newer button: it was added
   * to the tray two days after this handler was written, so clicking it while the network flyout was
   * open closed the flyout, rebuilt the bar, and swallowed the click that was supposed to open the
   * notification centre. The bell read as dead. Anything new in the tray goes in here. */
  const _TRAY_KEEP = '#os-noti,#os-net-panel,#os-clock,#os-bell,#os-net,.modal-bg';
  function _trayAway(e){
    if(!notiOpen && !netOpen) return;
    if(e.target.closest(_TRAY_KEEP)) return;
    if(notiOpen) toggleNoti(false);
    if(netOpen) toggleNet(false);
  }

  /* Repaint the BUTTON IN PLACE, never the whole taskbar. drawBar() destroys and recreates the
   * Search Nostr input, and relay churn is not something the user did — a reconnect landing while
   * they were mid-word rebuilt the box under the caret, which barQuery/barFocused restore only to
   * the END of the text, dropping any selection. The tray icon is the one thing that has to change
   * here, so change only that. */
  function paintNetButton(){
    const b = bar && $('#os-net', bar);
    if(!b){ drawBar(); return; }        // no bar/button yet — a full paint is the only option
    const s = netState();
    b.className = 'os-net net-' + s.level + (netOpen ? ' on' : '');
    b.title = NET_LABEL[s.level] + ' — ' + netSummary(s);
    b.setAttribute('aria-label', 'Nostr connection: ' + netSummary(s));
  }

  /* DEBOUNCED, because this fires per socket: reconnecting a five-relay pool walks every one of them
   * through connecting→ok, and repainting on each is work nobody can see. One repaint after the
   * burst settles says the same thing. */
  let _netPaintT = null;
  function onNetChange(){
    if(!on) return;
    clearTimeout(_netPaintT);
    _netPaintT = setTimeout(() => {
      _netPaintT = null;
      if(!on) return;
      paintNetButton();
      if(netOpen) paintNet();
    }, 250);
  }

  function toggleNet(force){
    netOpen = (force === undefined) ? !netOpen : !!force;
    const old = $('#os-net-panel', root);
    if(old) old.remove();
    clearInterval(_netT); _netT = null;
    if(!netOpen){ drawBar(); return; }
    toggleStart(false); toggleNoti(false);
    const panel = document.createElement('div');
    panel.id = 'os-net-panel';
    panel.className = 'os-noti os-net-panel';
    root.appendChild(panel);
    paintNet();
    /* A relay change repaints this through the pool watcher below, but "no data for 38s" is a clock,
     * and nothing fires an event when a quiet socket crosses into stale. Only while the panel is
     * open, and cleared the moment it closes. */
    _netT = setInterval(() => {
      /* Never repaint out from under the keyboard. paintNet replaces the panel's innerHTML, so a
       * user who has tabbed to Reconnect loses the focused element every five seconds and lands back
       * on <body> — Enter does nothing and the tab order restarts. On the one panel whose job is
       * recovering a broken connection, that makes it mouse-only. The ages are cosmetic; whoever is
       * mid-keystroke wins, and the next tick repaints. */
      if(panel.contains(document.activeElement) && document.activeElement !== panel) return;
      paintNet();
    }, 5000);
    drawBar();
  }

  // Arrival toasts, bottom-right. app.js's notifToast routes here while the desktop is up, so this
  // fires for exactly the things the classic UI toasts and nothing else.
  let toastHost = null, mailSeen = null, mailT = 0;

  /* The arrival chime. Synthesised rather than shipped as a file: no asset in the bundle, no fetch
   * (so it works offline and in the desktop app), no decode. A soft chime, not a beep — C5 with a
   * quiet fifth above it, triangle waves rolled off by a lowpass, a gentle 40ms attack and a long
   * tail. Sharp attacks and high sines are what make notification sounds grating; this is meant to
   * be liveable at forty a day. Browsers refuse audio until the page has been interacted with, and a
   * desktop restored from the remembered toggle has had no click yet, so a blocked play is swallowed
   * rather than thrown — the toast is the notification, the sound is the courtesy. */
  let _ac = null;
  function ding(){
    if(!settings().get('osDing', true)) return;
    try{
      const AC = window.AudioContext || window.webkitAudioContext;
      if(!AC) return;
      _ac = _ac || new AC();
      if(_ac.state === 'suspended') _ac.resume().catch(() => {});
      const t0 = _ac.currentTime;
      const lp = _ac.createBiquadFilter();
      lp.type = 'lowpass'; lp.frequency.value = 1800;
      const out = _ac.createGain();
      out.gain.setValueAtTime(0.0001, t0);
      out.gain.exponentialRampToValueAtTime(0.075, t0 + 0.04);
      out.gain.exponentialRampToValueAtTime(0.0001, t0 + 1.1);
      lp.connect(out); out.connect(_ac.destination);
      [[523.25, 1], [783.99, 0.45]].forEach(([f, lvl]) => {
        const o = _ac.createOscillator(), g = _ac.createGain();
        o.type = 'triangle'; o.frequency.value = f;
        g.gain.value = lvl;
        o.connect(g); g.connect(lp);
        o.start(t0); o.stop(t0 + 1.2);
      });
    }catch(_){ /* no audio here — the toast still is the notification */ }
  }

  function osToast(html, pic, onClick){
    if(!on) return;
    ding();
    if(!toastHost || !toastHost.isConnected){
      toastHost = document.createElement('div');
      toastHost.className = 'os-toasts';
      root.appendChild(toastHost);
    }
    const t = document.createElement('div');
    t.className = 'os-toast';
    t.innerHTML = (pic ? `<img src="${enc(pic)}" alt="" style="width:26px;height:26px;border-radius:50%;object-fit:cover;flex:0 0 auto">`
                       : '<svg class="ic" aria-hidden="true"><use href="#i-bell"></use></svg>')
                + `<div><span>${html}</span></div>`;
    // Clicking the card goes where the notification points — the notification centre by default,
    // but Messages for email, which is not in that centre's list.
    t.onclick = () => { t.remove(); if(onClick){ try{ onClick(); return; }catch(_){} } toggleNoti(true); };
    toastHost.appendChild(t);
    setTimeout(() => t.remove(), 7000);       // a desktop toast can afford to linger past the app's 5s
    drawBar();                                 // refresh the tray count
  }

  // Email has no live arrival event in the client — the unread number is updated by the mail poll —
  // so it is watched instead. Cheap: it reads one integer, and only while the desktop is up.
  function watchMail(){
    clearInterval(mailT);
    mailT = setInterval(() => {
      if(!on) return;
      let n = 0;
      try{ n = (PC().mailUnread && PC().mailUnread()) || 0; }catch(_){ return; }
      if(mailSeen === null){ mailSeen = n; mailAck = n; return; }   // baseline, not an arrival
      if(n > mailSeen) osToast(`✉ <b>${n - mailSeen} new email</b>`, '',
                               () => { try{ openApp('mail'); }catch(_){} });
      if(n !== mailSeen){ mailSeen = n; drawBar(); }
    }, 20000);
  }

  function toggleStart(force){
    startOpen = (force === undefined) ? !startOpen : !!force;
    let menu = $('#os-startmenu', root);
    if(!startOpen){ if(menu) menu.remove(); drawBar(); return; }
    if(notiOpen){ notiOpen = false; const np = $('#os-noti', root); if(np) np.remove(); }
    if(menu) menu.remove();
    menu = document.createElement('div');
    menu.id = 'os-startmenu';
    menu.className = 'os-startmenu';
    menu.innerHTML =
      `<input class="input os-search" id="os-q" placeholder="Search apps" autocomplete="off">
       <div class="os-applist" id="os-applist"></div>
       <div class="os-foot">${meChip()}<span class="spacer"></span>
         <button class="os-exit" id="os-full" title="Full screen (F11)">${isFull()?'⛶ Windowed':'⛶ Full screen'}</button>
         <button class="os-exit" id="os-exit" title="Leave the desktop">⤢ Classic</button></div>`;
    root.appendChild(menu);
    const searchNostr = (q) => {
      toggleStart(false);
      // `rerun` — searching again from the start menu must show the NEW query, not re-run the one
      // the Search window was opened with. See openDoc.
      openDoc('search', 'Search', 'i-search', () => {
        try{ PC().runSearch && PC().runSearch(q); }
        catch(_){ try{ PC().toast('search is unavailable here'); }catch(__){} }
      }, false, true);
    };
    const paint = (q) => {
      // Folded when idle; FLAT while searching, so typing "chess" finds Chess rather than requiring
      // you to know it lives in a folder.
      /* Idle, the start menu is the DESKTOP plus whatever the desktop is hiding — that is what makes
       * "Hide from desktop" safe to offer at all, and it is where every desktop keeps the apps that
       * are not on its desktop. The right-click menu out there is the way to put one back. */
      const lay = layout();
      // Search searches the LAUNCHER, not every view that exists: typing "torrents" after switching
      // Torrents off must not hand back the icon the switch just removed.
      const list = q ? launchApps().filter(a => a.label.toLowerCase().includes(q.toLowerCase()))
                     : lay.items.concat(lay.hidden);
      /* Typing here searches NOSTR — that row is FIRST, so it is what Enter runs, and it opens in
       * its own window like every other result on this desktop. The app list stays underneath
       * because the start menu is also how you find an app, and Windows puts both in one box. */
      const nrow = q ? `<button class="os-app os-app-find" data-find="1">
             <svg class="ic" aria-hidden="true"><use href="#i-search"></use></svg>
             <span>Search Nostr for “${enc(q)}”</span></button>` : '';
      $('#os-applist', menu).innerHTML = nrow + (list.length
        ? list.map(a => `<button class="os-app" data-view="${enc(a.view)}">
             ${iconSvg(a.icon)}<span>${enc(a.label)}</span></button>`).join('')
        : (q ? '' : '<div class="muted small" style="padding:10px">Nothing matches that.</div>'));
      $$('.os-app', menu).forEach(b => b.onclick = () => {
        if(b.dataset.find) return searchNostr(q);
        toggleStart(false); openApp(b.dataset.view);
      });
    };
    paint('');
    /* The community counters USED to hang off the bottom of this menu — they live in the sidebar,
     * which the desktop hides, and the start menu was the only surface that existed at the time.
     * They have moved to the network flyout (netStatsHtml), where they belong: every one of them is
     * a fact about the network — who is in the web of trust, who is online, how many of them this
     * relay is holding, what is live — so they read as detail under "Connected to N relays" instead
     * of as a footer under a list of apps. */
    /* The account chip opens the switcher, anchored to itself. Its first row is the identity you are
     * signed in as and opens your profile, so nothing that used to be reachable has moved further
     * away. */
    { const ab = $('#os-acct', menu);
      if(ab) ab.onclick = (e) => {
        e.stopPropagation();
        /* Open the flyout BEFORE closing the start menu. The other order looks harmless and is not:
         * closing the menu removes this button from the document, so the anchor it is positioned
         * against measures 0x0 at 0,0 and the flyout lands in the TOP-LEFT corner of the screen. */
        try{
          if(PC().accountMenu) PC().accountMenu(ab);
          else if(PC().openProfile) PC().openProfile();
        }catch(err){ PC().toast && PC().toast('could not open your accounts'); }
        toggleStart(false);
      }; }
    { const fb = $('#os-full', menu); if(fb) fb.onclick = () => { toggleStart(false); toggleFull(); }; }
    { const xb = $('#os-exit', menu); if(xb) xb.onclick = () => { toggleStart(false); exit(); }; }
    const q = $('#os-q', menu);
    q.oninput = () => paint(q.value.trim());
    q.onkeydown = (e) => {
      if(e.key === 'Escape'){ e.stopPropagation(); toggleStart(false); }
      if(e.key === 'Enter'){ const first = $('.os-app', menu); if(first) first.click(); }
    };
    q.focus();
    drawBar();
  }

  // ---- enter / leave ---------------------------------------------------------------------------

  function enter(){
    if(on) return;
    if(!fits()){
      // A tablet held upright is the common case here, and "needs a wider screen" is useless advice
      // when turning the device sideways is the answer.
      const rotatable = Math.max(window.innerWidth, window.innerHeight) >= MIN_WIDTH;
      const msg = rotatable ? 'Turn the device sideways for the desktop'
                            : 'The desktop needs a wider screen';
      try{ PC().toast && PC().toast(msg); }catch(_){}
      return;
    }
    on = true;
    realFeed = document.getElementById('feed');
    realHome = realFeed ? realFeed.parentElement : null;
    root = document.createElement('div');
    root.id = 'os-root';
    root.className = 'os-root';
    root.innerHTML = '<div class="os-desk" id="os-desk"></div><div class="os-bar" id="os-bar"></div>';
    document.body.appendChild(root);
    document.body.classList.add('os-on');
    // …and on <html>, because the overlays that matter here — the music player, the upload badge —
    // are appended to documentElement, i.e. SIBLINGS of <body>. A `body.os-on #music-player` rule
    // can never match one, which is why the player kept its z-index:120 and played on, invisible,
    // underneath the z-index:300 desktop.
    document.documentElement.classList.add('os-on');
    /* Getting here ANSWERS the "Click me for Desktop Mode" bubble, however you arrived. The logo's
     * own click dismisses it, but the keyboard shortcut, the remembered preference and a plain
     * PCOS.toggle() do not go through that — and the bubble is fixed-position on <html>, so it
     * survives into the desktop and floats over it pointing at a sidebar that is no longer on
     * screen, offering something you are already looking at. */
    try{
      settings().set('osHintSeen', true);
      const h = document.getElementById('os-hint');
      if(h){ try{ h._osUnplace(); }catch(_){} h.remove(); }
    }catch(_){}
    desk = $('#os-desk', root);
    bar = $('#os-bar', root);
    desk.addEventListener('pointerdown', (e) => {
      if(e.target === desk || e.target.closest('.os-icons') === e.target){
        toggleStart(false); toggleNoti(false); toggleNet(false);
      }
    });
    // …and so does clicking anywhere that is not the panel or the clock itself. Without this the
    // only way to close it is the clock, which is not where anyone's hand is by then.
    document.addEventListener('pointerdown', _trayAway, true);
    /* Repaint on every connection change — the tray icon is the point of the widget, so it cannot
     * wait for the 30s clock tick to notice the relay went. Kept OFF Relay.onStatus: that is a
     * single slot app.js owns for the offline banner and the outbox flush, and assigning it here
     * would silently take all of it over (see Relay.watch). */
    try{ _netOff = window.Relay && Relay.watch ? Relay.watch(onNetChange) : null; }catch(_){ _netOff = null; }
    // navigator.onLine outranks the pool in netState(), and nothing in the pool fires when the radio
    // goes — so without these the icon stays green on a machine that is plainly offline.
    window.addEventListener('online', onNetChange);
    window.addEventListener('offline', onNetChange);
    drawDesktop();
    drawBar();
    // …and then YOUR arrangement of it, which lives on the relays. Drawn first from the defaults so
    // the desktop is never blank while the network thinks, repainted by refreshIcons when it lands.
    loadLayout().catch(() => {});
    /* No `!netOpen` here. The flyout lives on `root`, NOT inside `#os-bar`, so drawBar() cannot
     * disturb it — adding it to this guard protected nothing and only stopped the taskbar CLOCK
     * while the panel was open, which is the one thing on the bar that has to keep moving. */
    // The desktop taskbar clock: nobody is reading it behind another window, and it repaints the
    // whole bar. 30s of DOM work every 30s, forever, for a clock nobody can see.
    _clock = setInterval(() => { if(document.hidden) return; if(on && !startOpen && !notiOpen) drawBar(); }, 30000);
    // Leaving full screen by pressing Escape never goes through our button, so the label has to
    // follow the browser rather than our own last action.
    document.addEventListener('fullscreenchange', onFullChange);
    watchMail();
    document.addEventListener('keydown', onKey, true);
    window.addEventListener('resize', onResize);
    settings().set(KEY, true);
  }

  function exit(){
    if(!on) return;
    on = false;
    clearInterval(_clock); _clock = null;
    clearInterval(mailT); mailT = 0; mailSeen = null;
    // The pool outlives the desktop — leaving this subscribed keeps a watcher calling drawBar()
    // against a torn-down taskbar for the rest of the session, and re-entering would add a second.
    clearInterval(_netT); _netT = null;
    clearTimeout(_netPaintT); _netPaintT = null;
    clearTimeout(_bellT); _bellT = null;
    // The layout subscription outlives the desktop otherwise — it would go on calling refreshIcons
    // against a torn-down desk, and re-entering would add a second one.
    unwatchLayout(); hideCtx();
    /* The widgets go with the desktop, and their BOOKKEEPING has to go too.
     *
     * `root.remove()` below detaches the elements but leaves `_mounted` pointing at every one of
     * them. The interval only notices on its next 15s tick, and re-entering reconciles against the
     * NEW desk — which never finds the previous cycle's detached nodes, so they are never evicted.
     * A few toggles of desktop mode and `_mounted.size` is permanently non-zero: the map grows, the
     * "nothing runs when nothing is watching" guard stops being true, and every tick walks widgets
     * that are not on screen. */
    _wgtStop(); _mounted.clear();
    // The wallpaper is an object URL for THIS session's decrypt; drop it so re-entering (or another
    // account) re-derives it rather than painting the previous one.
    _bgSha = ''; _bgUrl = '';
    try{ _netOff && _netOff(); }catch(_){} _netOff = null;
    document.removeEventListener('pointerdown', _trayAway, true);
    window.removeEventListener('online', onNetChange);
    window.removeEventListener('offline', onNetChange);
    toastHost = null; notiOpen = false; netOpen = false;
    document.removeEventListener('keydown', onKey, true);
    document.removeEventListener('fullscreenchange', onFullChange);
    // Leaving the desktop leaves full screen with it: a full-screen CLASSIC client with no way back
    // to the desktop is a trap, and nothing else in the app asks for the whole screen.
    if(isFull()){ try{ document.exitFullscreen && document.exitFullscreen(); }catch(_){} }
    window.removeEventListener('resize', onResize);
    // Hand the id back BEFORE the windows go, then repaint the classic view into it.
    releaseFeed();
    wins = [];
    if(root) root.remove();
    root = bar = desk = null;
    startOpen = false;
    document.body.classList.remove('os-on');
    document.documentElement.classList.remove('os-on');
    settings().set(KEY, false);
    /* Land the classic UI on a view it actually HAS. Windows can leave VIEW on something only the
     * desktop knows — 'music' is the Music window's own screen, reachable from the launcher and from
     * nowhere in the sidebar — and switching back to it drops the classic client on a dead view
     * showing the leftover player markup, with no nav entry to leave by. Anything the sidebar does
     * not list falls back to the timeline. Playback is untouched: classic DOES show the floating
     * widget, so the music carries on with controls, which is the one thing the desktop suppresses. */
    let back = 'global';
    try{
      const v = PC().VIEW;
      const known = !!document.querySelector(`.sidebar .nav .nav-item[data-view="${v}"]`);
      if(v && known) back = v;
    }catch(_){}
    try{ PC().switchView && PC().switchView(back); }catch(_){}
  }

  let _clock = null;

  /* Rotating a tablet into portrait leaves the desktop in a width it was refused at. Rather than
   * strand somebody in a layout that cannot work, step back to the classic client — and because the
   * preference is remembered, turning it back to landscape restores the desktop.
   *
   * The remembered flag is deliberately NOT cleared here: this is the screen being too narrow, not
   * the user choosing to leave. */
  function onFullChange(){ if(on && startOpen) toggleStart(true); }

  function onResize(){
    if(!on) return;
    if(!fits()){
      const remember = settings().get(KEY, false);
      exit();
      if(remember) settings().set(KEY, true);
      try{ PC().toast && PC().toast('Turn the device sideways for the desktop'); }catch(_){}
      return;
    }
    // A rotation or a resized browser changes how many icon columns fit and where the snap zones
    // are. Without this a tablet turned sideways keeps the portrait column count and the last rows
    // sit under the taskbar — visible only by scrolling, which is how they got "cut off".
    drawDesktop();
    wins.forEach(w => {
      if(!w.snap) return;
      const css = rectOf(w.snap);
      if(css) Object.assign(w.el.style, css);
    });
  }

  function onKey(e){
    if(!on) return;
    /* F11, the way every desktop spells it. A normal browser tab takes F11 for its OWN fullscreen
     * before this ever runs, which is fine — the result is the same. Where it does reach us (the
     * desktop app, a kiosk WebView) it does the job. */
    if(e.key === 'F11'){ e.preventDefault(); toggleFull(); return; }
    if(e.key === 'Escape' && root && root.querySelector('.os-bgpick')){
      e.stopPropagation(); root.querySelectorAll('.os-bgpick').forEach(n => n.remove()); return; }
    if(e.key === 'Escape' && root && root.querySelector('.os-ctx')){ e.stopPropagation(); hideCtx(); return; }
    if(e.key === 'Escape' && netOpen){ e.stopPropagation(); toggleNet(false); return; }
    if(e.key === 'Escape' && notiOpen){ e.stopPropagation(); toggleNoti(false); return; }
    if(e.key === 'Escape' && startOpen){ e.stopPropagation(); toggleStart(false); return; }
    // Alt+W closes the focused window — Ctrl+W is the browser's tab and must not be taken.
    if(e.altKey && (e.key === 'w' || e.key === 'W')){
      const f = wins.find(w => w.el.classList.contains('focused'));
      if(f){ e.preventDefault(); closeWin(f); }
    }
  }

  function toggle(){ on ? exit() : enter(); }

  /* Restore on load when the screen is wide enough. A remembered desktop on a window that has since
   * been made narrow must not strand somebody in a UI they cannot use, so the size check applies to
   * the restore as well as to the click. */
  function restore(){
    /* A POPOUT IS NOT A DESKTOP, and the size check cannot tell the difference.
     *
     * "🗔 Open in a window" opens ONE view — a stream — drawn without sidebar, nav or rightbar, at
     * `max(900, availWidth*0.7)`. That is over MIN_WIDTH on any ordinary monitor, and the window is
     * the SAME ORIGIN, so it reads the same remembered `osMode` and the desktop claims it: the
     * stream never draws, and what is left is the shell with its chrome already hidden by
     * `body.popout`. Reported as "the Streams window button launches a new window with an empty
     * desktop". Screen-dependent, which is what makes it look intermittent — at 1280 wide the
     * popout is 900px, under MIN_WIDTH, and none of this happens.
     *
     * Deliberately a RETURN and not a `settings().set(KEY,false)`: the flag is shared with the tab
     * that opened this window, so turning it off here would exit desktop mode over there too. */
    try{ if(new URLSearchParams(location.search).get('popout') === '1') return; }catch(_){}
    try{ if(settings().get(KEY, false) && fits()) enter(); }catch(_){}
  }

  // Win+Arrow. Meta, not Ctrl: Ctrl+Arrow is caret navigation inside every text box on this desktop.
  document.addEventListener('keydown', (e) => {
    if(!on || !e.metaKey || e.altKey || e.ctrlKey) return;
    if(!/^Arrow(Left|Right|Up|Down)$/.test(e.key)) return;
    const w = wins.find(x => x.el.classList.contains('focused'));
    if(!w) return;
    e.preventDefault();
    const k = e.key.slice(5);
    if(k === 'Up')        snapTo(w, w.snap === 'left' ? 'tl' : w.snap === 'right' ? 'tr' : 'max');
    else if(k === 'Down'){
      if(w.snap === 'left')       snapTo(w, 'bl');
      else if(w.snap === 'right') snapTo(w, 'br');
      else if(w.snap || w.max){ unsnap(w); focusWin(w); }
      else minimise(w);
    }
    else snapTo(w, k === 'Left' ? 'left' : 'right');
  });

  /* Re-read the launcher and repaint the chrome. The desktop icons are built ONCE at enter(), and
   * the entries gated on being signed in — My Profile, Music, Go Live, and the tray avatar — are
   * decided at that moment. A remembered desktop opens during boot, BEFORE the identity resolves, so
   * those were simply absent for the rest of the session; the start menu is built on each open and
   * therefore had them, which is what "it is in the start menu but not on the desktop" was. */
  function refresh(){
    if(!on) return;
    drawDesktop();
    drawBar();
    // Signing in is also when the layout becomes readable at all — a remembered desktop opens during
    // boot, before the identity resolves, and loadLayout returns nothing when there is nobody to
    // decrypt for. Without this the account's own arrangement never appears for the whole session.
    loadLayout().catch(() => {});
  }

  /* Step OUT of the desktop without FORGETTING it — the distinction exit() cannot make on its own,
   * because leaving deliberately and being pushed out are the same call with opposite meanings for
   * the remembered preference.
   *
   * The sign-in screen is why this exists. `.auth-gate` is z-index 50 and `#os-root` is 300, so the
   * gate rendered UNDERNEATH the desktop: clicking "Log in" on the guest card put a full, correct
   * login form on screen with the desktop icons drawn on top of it, and every button — including
   * "Browser extension (NIP-07)" — belonged to whatever was above it. Measured, not guessed:
   * elementFromPoint at the centre of #btn-nip07 returned a #os-root child. It reads as "the login
   * doesn't detect my extension" because the click never reaches the button that would.
   *
   * Same shape on LOGOUT from inside the desktop. So the gate suspends the desktop, and signing in
   * restores it (`restore()` reads the preference this deliberately leaves set). */
  function suspend(){
    if(!on) return false;
    const remember = settings().get(KEY, false);
    exit();
    if(remember) settings().set(KEY, true);
    return true;
  }

  window.PCOS = { enter, exit, suspend, toggle, restore, refresh, isOn: () => on, openDoc, focusDoc, routeView, snapTo, osToast,
                  // app.js calls this when the player's state changes — the Now-playing widget has
                  // nothing to subscribe to, and polling an element we could be told about is the
                  // mistake the games were just fixed for.
                  musicChanged, noteView,
                  /* …and this one when the unread count moves. Same reason: the tray bell has
                   * nothing to subscribe to, and the taskbar otherwise repaints only on a window
                   * focus, the 30s clock tick and an arrival toast — none of which a follow, or
                   * anything landing before the notification sub reaches EOSE, produces. */
                  notifChanged,
                  /* app.js calls this when Settings → 🧭 Sidebar changes. refreshIcons, not refresh:
                   * the LIST changed, the document did not, so re-reading the layout off the relay
                   * would cost a round trip per switch flip. It also closes a folder window whose
                   * members have all just gone, which is the visible half of hiding a group. */
                  navChanged: refreshIcons,
                  isRepainting: () => repainting > 0, parkedSlot, noteScroll,
                  windows: () => wins.map(w => ({ view: w.view, title: w.title, min: w.min })),
                  /* The layout arithmetic, exposed so tests/test_desktop_layout.py can run the
                   * SHIPPED code against a list of apps and a document. Everything it decides fails
                   * silently on screen — an app that stops appearing, a folder that swallows an icon
                   * twice, a feature added next month that never shows up — so it is tested directly
                   * rather than inferred from a rendered desktop. */
                  __layout: (list, doc) => computeLayout(list, doc), __normDoc: (d) => _normDoc(d),
                  /* The launcher's own list, for the same reason: "a row switched off in Settings →
                   * Sidebar is gone from the desktop too" is invisible when it is wrong — you get a
                   * desktop, just one still carrying the app you removed. tests/client/test_nav_hide.py
                   * drives it against a stub sidebar. */
                  __launchApps: () => launchApps(),
                  // The size arithmetic, for the same reason: 'a widget fits the screen it is on'
                  // is the whole of the tablet↔desktop requirement and nothing on screen says
                  // when it is wrong — the panel is just too big, or too small to read.
                  __wgtBox: (size, w, h, def) => wgtBox(size, w, h, def),
                  __wxUnits: (w) => _wxUnits(w),
                  __calSplit: (occ, now) => _calSplit(occ, now),
                  __calDayLabel: (d, now) => _calDayLabel(d, now),
                  __calOccurrences: (items, a, b) => _calOccurrences(items, a, b),
                  __placeWidgets: (l, w, h) => placeWidgets(l, w, h),
                  /* The three newest panels' decisions, DOM-free for the same reason as the rest: a
                   * clock that is an hour out in a zone with a half-hour offset, a ticker that runs
                   * off the end of a three-item feed, a counter row that drops a zero — each one is
                   * wrong on screen in a way that looks deliberate. */
                  __clockFace: (d, tz, cfg) => _clockFace(d, tz, cfg),
                  __clockZones: (cfg) => _clockZones(cfg),
                  __newsWindow: (items, off, n) => _newsWindow(items, off, n),
                  __statCells: (st) => _statCells(st),
                  __wgtPeriodOf: (everies) => _wgtPeriodOf(everies),
                  // How long after a refresh a widget becomes due again. Shorter than `every` on
                  // purpose — see _wgtRefreshOne. Exported because "the clock skips a second" is the
                  // kind of wrongness people notice and nothing reports.
                  __wgtDueIn: (every) => every - _wgtSlack(every),
                  __safeHttp: (u) => _safeHttp(u) };
})();
