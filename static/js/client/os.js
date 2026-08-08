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
   * there is no second list to forget to update. */
  function apps(){
    const seen = new Set();
    return $$('.sidebar .nav .nav-item[data-view]').map(btn => {
      const view = btn.dataset.view;
      if(!view || seen.has(view)) return null;
      seen.add(view);
      const use = btn.querySelector('svg use');
      const label = (btn.querySelector('span') || {}).textContent || view;
      return { view, label: String(label).replace(/\d+$/, '').trim(),
               icon: use ? (use.getAttribute('href') || use.getAttribute('xlink:href') || '') : '' };
    }).filter(Boolean);
  }

  /* The start button wears the instance's own logo — the same image the sidebar brand shows, read
   * live rather than hardcoded, so a deployment that set a custom logo (Admin → Site) gets ITS mark
   * on the start button instead of PosterChan's. */
  function brandLogo(){
    const img = document.querySelector('.brand-logo');
    const src = img && (img.getAttribute('src') || img.src);
    return src || '/static/posterchan-relay.png';
  }

  const iconSvg = (href) => href
    ? `<svg class="ic" aria-hidden="true"><use href="${enc(href)}"></use></svg>`
    : '<svg class="ic" aria-hidden="true"><use href="#i-app"></use></svg>';

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
  function snapshot(w){
    if(!w || !realFeed || realFeed.parentElement !== w.body) return;
    const slot = w.slot;
    if(slot) slot.innerHTML = realFeed.innerHTML;   // a picture of it, for the unfocused window
  }

  function claimFeed(w){
    if(!realFeed || realFeed.parentElement === w.body) return;
    const holder = wins.find(x => realFeed.parentElement === x.body);
    if(holder) snapshot(holder);
    w.body.appendChild(realFeed);
    if(w.slot) w.slot.innerHTML = '';               // the live element replaces the snapshot
  }

  function releaseFeed(){
    if(!realFeed || !realHome) return;
    const holder = wins.find(x => realFeed.parentElement === x.body);
    if(holder) snapshot(holder);
    realHome.appendChild(realFeed);
  }

  // ---- windows -------------------------------------------------------------------------------

  function focusWin(w, render){
    if(!w) return;
    wins.forEach(x => x.el.classList.toggle('focused', x === w));
    w.el.style.zIndex = String(++zTop);
    if(w.min){ w.min = false; w.el.classList.remove('minimised'); }
    claimFeed(w);
    drawBar();
    // Re-render the feature into ITS window. Cheap for these modules — they hold their own state
    // and repaint from it, which is exactly what leaving and returning to a view already does.
    if(render !== false){
      // A DOCUMENT window (a single post) repaints itself; a FEATURE window repaints by switching
      // the client back to its view. Both are needed because the one live #feed moves between
      // windows on every focus, so whatever it holds must be redrawn on arrival.
      if(w.render){ try{ w.render(); }catch(err){ /* a stale document is not fatal */ } }
      else try{ PC().switchView ? PC().switchView(w.view) : null; }catch(err){ /* a view that refuses is not fatal */ }
    }
  }

  let iconSpan = 318;               // width the icon grid actually took; windows open clear of it

  function place(i){
    // Cascade, then wrap, so opening several windows does not land exactly on top of one another
    // (which reads as "only one opened") — and start to the RIGHT of the icon column, or the first
    // window covers the icons you just clicked.
    const vw = vwL(), vh = vhL() - TASKBAR;
    const w = Math.min(1100, Math.round((vw - iconSpan) * 0.72));
    const h = Math.min(760, Math.round(vh * 0.78));
    const step = 38, n = i % 6;
    return { x: iconSpan + 16 + n * step, y: Math.round(vh * 0.05) + n * step, w, h };
  }

  function openApp(view, label, icon, render){
    const existing = wins.find(w => w.view === view);
    if(existing){ focusWin(existing); return existing; }
    const app = apps().find(a => a.view === view) || {};
    label = label || app.label || view;
    icon = icon || app.icon || '';
    const r = place(wins.length);
    const el = document.createElement('div');
    el.className = 'osw';
    el.style.cssText = `left:${r.x}px;top:${r.y}px;width:${r.w}px;height:${r.h}px;z-index:${++zTop}`;
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
    const w = { id: ++seq, view, title: label, icon, el, body: $('.osw-body', el),
                slot: $('.osw-slot', el), min: false, max: false, rect: r, render: render || null };
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
    el.addEventListener('pointerdown', () => { if(!el.classList.contains('focused')) focusWin(w); }, true);

    focusWin(w);
    return w;
  }

  // A post opens in its OWN window on the desktop, instead of replacing the timeline underneath it
  // — that is the whole point of having windows. Keyed by id, so clicking the same post twice
  // focuses the window it is already in rather than stacking duplicates.
  function openDoc(key, label, icon, render){
    const view = 'doc:' + key;
    const existing = wins.find(w => w.view === view);
    if(existing){ focusWin(existing); return existing; }
    return openApp(view, label, icon, render);
  }

  // Focus the window already showing a document, without creating one. Back/forward must never
  // conjure a window: the history entry is a URL, not a user asking for a new frame.
  function focusDoc(key){
    const w = wins.find(x => x.view === 'doc:' + key);
    if(!w) return false;
    focusWin(w, false);            // claim the feed; the caller is about to paint into it
    return true;
  }

  function closeWin(w){
    const i = wins.indexOf(w);
    if(i < 0) return;
    wins.splice(i, 1);
    // If this window held the id, hand it back BEFORE removing the element, or `$('#feed')` briefly
    // resolves to nothing and whatever renders next paints into a detached node.
    if(realFeed && realFeed.parentElement === w.body) releaseFeed();
    w.el.remove();
    const next = wins.filter(x => !x.min).pop();
    if(next) focusWin(next); else drawBar();
  }

  function minimise(w){
    w.min = true;
    w.el.classList.add('minimised');
    if(realFeed && realFeed.parentElement === w.body) releaseFeed();
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
    w.el.classList.add('dragging');
    const paint = () => { raf = 0; w.el.style.transform = `translate(${curX - ox}px, ${curY - oy}px)`; };
    const move = (e) => {
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
    const up = () => {
      document.removeEventListener('pointermove', move);
      document.removeEventListener('pointerup', up);
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
    const up = () => { document.removeEventListener('pointermove', move);
                       document.removeEventListener('pointerup', up);
                       if(raf){ cancelAnimationFrame(raf); paint(); } };
    document.addEventListener('pointermove', move);
    document.addEventListener('pointerup', up);
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

  function drawDesktop(){
    desk.querySelectorAll('.os-icons').forEach(n => n.remove());
    const grid = document.createElement('div');
    grid.className = 'os-icons';
    grid.innerHTML = apps().map(a =>
      `<button class="os-icon" data-view="${enc(a.view)}" title="${enc(a.label)}">
         ${iconSvg(a.icon)}<span>${enc(a.label)}</span></button>`).join('');
    const cols = iconCols(apps().length);
    grid.style.gridTemplateColumns = `repeat(${cols}, ${ICON_W}px)`;
    // Windows open clear of whatever the launcher actually takes, not a hardcoded guess.
    iconSpan = ICON_PAD * 2 + cols * ICON_W + (cols - 1) * ICON_GAP;
    desk.appendChild(grid);
    $$('.os-icon', grid).forEach(b => {
      // Single click opens. A desktop double-click is the convention, but this is a web app people
      // arrive at from a single-click UI, and a double-click that does nothing the first time reads
      // as broken.
      b.onclick = () => openApp(b.dataset.view);
    });
  }

  // Your own account, in the tray. The classic UI reaches your profile through #me-card in the
  // sidebar, which the desktop hides — so without this there is no way to open your own profile at
  // all. The picture is read from that card rather than re-fetched, so it can never disagree with it.
  function meAvatar(){
    if(!(window.ME && ME.pubkey)) return '';
    let src = '';
    try{ const img = document.querySelector('#me-card img'); if(img) src = img.getAttribute('src') || ''; }catch(_){}
    const inner = src ? `<img src="${enc(src)}" alt="">`
                      : '<svg class="ic" aria-hidden="true"><use href="#i-user"></use></svg>';
    return `<button class="os-me" id="os-me" title="My profile" aria-label="My profile">${inner}</button>`;
  }

  function drawBar(){
    if(!bar) return;
    const t = new Date();
    const clock = t.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
    const date = t.toLocaleDateString([], { day: 'numeric', month: 'short' });
    bar.innerHTML =
      `<button class="os-start${startOpen ? ' on' : ''}" id="os-start" title="Start">
         <img src="${enc(brandLogo())}" alt="Start"></button>
       <button class="os-new" id="os-new" title="New post">
         <svg class="ic" aria-hidden="true"><use href="#i-plus"></use></svg><span>Post</span></button>
       <div class="os-qbox">
         <svg class="ic" aria-hidden="true"><use href="#i-search"></use></svg>
         <input id="os-q-bar" class="os-qin" type="search" autocomplete="off"
                placeholder="Search Nostr" aria-label="Search Nostr"></div>
       <div class="os-tasks">${wins.map(w =>
         `<button class="os-task${w.el.classList.contains('focused') && !w.min ? ' on' : ''}"
                  data-id="${w.id}" title="${enc(w.title)}">
            ${iconSvg(w.icon)}<span>${enc(w.title)}</span></button>`).join('')}</div>
       <div class="os-tray">
         ${meAvatar()}
         <button class="os-exit" id="os-exit" title="Leave the desktop">⤢ Classic</button>
         <div class="os-clock"><b>${enc(clock)}</b><span>${enc(date)}</span></div>
       </div>`;
    $('#os-start', bar).onclick = (e) => { e.stopPropagation(); toggleStart(); };
    /* Posting needs a home here. In the classic UI it is a small ＋ floating inside the timeline,
     * which in the desktop ends up tucked in the corner of whichever window happens to hold the
     * feed — findable only if you already know it exists. On a desktop, "new" belongs on the
     * taskbar. */
    { const nb = $('#os-new', bar);
      if(nb) nb.onclick = () => { try{ PC().compose && PC().compose(); }
                                  catch(err){ PC().toast && PC().toast('could not open the composer'); } }; }
    /* The taskbar box searches NOSTR, not the app list — the start menu already filters apps, and a
     * second app filter next to it would be the least useful thing that box could do. Results land
     * in their own window, so searching does not throw away whatever the focused app was showing. */
    { const qb = $('#os-q-bar', bar);
      if(qb) qb.addEventListener('keydown', (e) => {
        if(e.key !== 'Enter') return;
        const q = qb.value.trim();
        if(!q) return;
        const run = () => { try{ PC().runSearch && PC().runSearch(q); }
                            catch(err){ PC().toast && PC().toast('search is unavailable here'); } };
        openDoc('search', 'Search', 'i-search', run);
      }); }
    { const mb = $('#os-me', bar);
      if(mb) mb.onclick = () => { try{ PC().openProfile && PC().openProfile(); }
                                  catch(err){ PC().toast && PC().toast('could not open your profile'); } }; }
    $('#os-exit', bar).onclick = () => exit();
    $$('.os-task', bar).forEach(b => b.onclick = () => {
      const w = wins.find(x => String(x.id) === b.dataset.id);
      if(!w) return;
      // Clicking the focused window's own task button minimises it, the way a taskbar does.
      if(w.el.classList.contains('focused') && !w.min) minimise(w); else focusWin(w);
    });
  }

  function toggleStart(force){
    startOpen = (force === undefined) ? !startOpen : !!force;
    let menu = $('#os-startmenu', root);
    if(!startOpen){ if(menu) menu.remove(); drawBar(); return; }
    if(menu) menu.remove();
    menu = document.createElement('div');
    menu.id = 'os-startmenu';
    menu.className = 'os-startmenu';
    menu.innerHTML =
      `<input class="input os-search" id="os-q" placeholder="Search apps" autocomplete="off">
       <div class="os-applist" id="os-applist"></div>
       <div class="os-stats" id="os-stats"></div>`;
    root.appendChild(menu);
    const paint = (q) => {
      const list = apps().filter(a => !q || a.label.toLowerCase().includes(q.toLowerCase()));
      $('#os-applist', menu).innerHTML = list.length
        ? list.map(a => `<button class="os-app" data-view="${enc(a.view)}">
             ${iconSvg(a.icon)}<span>${enc(a.label)}</span></button>`).join('')
        : '<div class="muted small" style="padding:10px">Nothing matches that.</div>';
      $$('.os-app', menu).forEach(b => b.onclick = () => { toggleStart(false); openApp(b.dataset.view); });
    };
    paint('');
    // The community counters live in the sidebar, which the desktop hides — so the start menu is
    // where they go. Read from the client's own cache (PC.communityStats): /client/stats counts the
    // caller as a viewer, and polling it again from here would inflate "online now" by one.
    try{
      const st = (PC().communityStats && PC().communityStats()) || {};
      const row = (icon, n, label) => (n > 0 || label === 'WoT')
        ? `<span class="os-stat" title="${enc(label)}">${iconSvg(icon)}<b>${enc(String(n || 0))}</b>
             <i>${enc(label)}</i></span>` : '';
      $('#os-stats', menu).innerHTML =
        row('i-wot', st.users, 'WoT') + row('i-livedot', st.online, 'online') +
        row('i-relay-dot', st.relay, 'on relay') + row('i-stream', st.streams, 'live') +
        row('i-call', st.calls, 'in call');
    }catch(_){ /* no instance (standalone build) → no community counters, which is correct */ }
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
    desk = $('#os-desk', root);
    bar = $('#os-bar', root);
    desk.addEventListener('pointerdown', (e) => { if(e.target === desk || e.target.closest('.os-icons') === e.target) toggleStart(false); });
    drawDesktop();
    drawBar();
    _clock = setInterval(() => { if(on && !startOpen) drawBar(); }, 30000);
    document.addEventListener('keydown', onKey, true);
    window.addEventListener('resize', onResize);
    settings().set(KEY, true);
  }

  function exit(){
    if(!on) return;
    on = false;
    clearInterval(_clock); _clock = null;
    document.removeEventListener('keydown', onKey, true);
    window.removeEventListener('resize', onResize);
    // Hand the id back BEFORE the windows go, then repaint the classic view into it.
    releaseFeed();
    wins = [];
    if(root) root.remove();
    root = bar = desk = null;
    startOpen = false;
    document.body.classList.remove('os-on');
    settings().set(KEY, false);
    try{ PC().switchView && PC().switchView(PC().VIEW || 'global'); }catch(_){}
  }

  let _clock = null;

  /* Rotating a tablet into portrait leaves the desktop in a width it was refused at. Rather than
   * strand somebody in a layout that cannot work, step back to the classic client — and because the
   * preference is remembered, turning it back to landscape restores the desktop.
   *
   * The remembered flag is deliberately NOT cleared here: this is the screen being too narrow, not
   * the user choosing to leave. */
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

  window.PCOS = { enter, exit, toggle, restore, isOn: () => on, openDoc, focusDoc, snapTo,
                  windows: () => wins.map(w => ({ view: w.view, title: w.title, min: w.min })) };
})();
