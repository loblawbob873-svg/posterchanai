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
    }).filter(Boolean).concat(EXTRAS.filter(x => x.when()));
  }

  /* Entries that are not sidebar nav items. Go Live lives in the mobile "More" sheet and in the
   * rightbar, neither of which the desktop shows, so the launcher — which reads .nav-item[data-view]
   * — could never have found it. `act` marks it as something to RUN rather than a view to open in a
   * window: it is a dialog that ends in a live stream, and the stream itself opens the Streams app. */
  // Who is signed in. NOT window.ME — the client keeps ME inside its IIFE, so window.ME is undefined
  // for every module out here whoever is logged in. Gating on it hid all of this from everyone.
  const me = () => { try{ return (PC().me && PC().me()) || null; }catch(_){ return null; } };

  const EXTRAS = [
    { view: '__profile', label: 'My Profile', icon: '#i-user', act: () => PC().openProfile && PC().openProfile(),
      when: () => !!(me() && PC().openProfile) },
    { view: '__music', label: 'Music', icon: '#i-music', act: () => PC().openMusic && PC().openMusic(),
      when: () => !!(me() && PC().openMusic) },
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
    // The admin panel's iframe host is a sibling of the feed and follows it (see _adminFrame). Send
    // it home too, or leaving the desktop strands it in a window that is about to be destroyed —
    // which throws away a loaded panel and forces a reload on the next open.
    const ah = document.getElementById('admin-host');
    if(ah && ah.parentElement !== realHome) realHome.appendChild(ah);
  }

  // ---- windows -------------------------------------------------------------------------------

  let repainting = 0;      // >0 while a window is repainting itself; see focusWin

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
      /* A repaint is NOT a navigation. Both paths below end in code that pushes a history entry
       * (_navUrl), so without this every click between two windows added one — and the back button
       * then walked window focus instead of navigation, landing on the profile again and again
       * instead of returning to the timeline. */
      repainting++;
      try{
        if(w.render){ try{ w.render(); }catch(err){ /* a stale document is not fatal */ } }
        else try{ PC().switchView ? PC().switchView(w.view) : null; }catch(err){ /* a view that refuses is not fatal */ }
      }finally{ repainting--; }
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
    if(view === 'messages'){
      try{ mailAck = (PC().mailUnread && PC().mailUnread()) || 0; }catch(_){}
    }
    const extra = EXTRAS.find(x => x.view === view);
    if(extra){ try{ extra.act(); }catch(err){ try{ PC().toast('could not open ' + extra.label); }catch(_){} } return null; }
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

  /* Route a view switch to that feature's OWN window. Returns true when it has taken over (a window
   * was created and has already repainted itself), false when the caller should paint where it is —
   * which covers both "that window already exists, the feed has been moved into it" and "this view
   * is not something the launcher knows about", where a window would be a surprise. */
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
    if(!me()) return '';
    let src = '';
    try{ const img = document.querySelector('#me-card img'); if(img) src = img.getAttribute('src') || ''; }catch(_){}
    const inner = src ? `<img src="${enc(src)}" alt="">`
                      : '<svg class="ic" aria-hidden="true"><use href="#i-user"></use></svg>';
    return `<button class="os-me" id="os-me" title="My profile" aria-label="My profile">${inner}</button>`;
  }

  function drawBar(){
    if(!bar) return;
    // Remember whether the search box had the caret BEFORE the rebuild throws the element away.
    try{ barFocused = barFocused || (document.activeElement && document.activeElement.id === 'os-q-bar'); }catch(_){}
    const t = new Date();
    const clock = t.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
    const date = t.toLocaleDateString([], { day: 'numeric', month: 'short' });
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
         ${meAvatar()}
         <button class="os-exit" id="os-exit" title="Leave the desktop">⤢ Classic</button>
         <button class="os-clock${notiOpen ? ' on' : ''}" id="os-clock" title="Notifications">
           <b>${enc(clock)}</b><span>${enc(date)}</span>${notiDot()}</button>
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
          openDoc('search', 'Search', 'i-search', run);
        });
      } }
    /* The tray avatar opens the ACCOUNTS sheet, not the profile directly — the sheet's first row is
     * the identity you are signed in as and opens the profile, so nothing is lost, and switching
     * between a few keys gets the place a desktop expects it: the corner with your face in it. */
    { const mb = $('#os-me', bar);
      if(mb) mb.onclick = () => {
        try{
          if(PC().accountMenu) PC().accountMenu(mb);   // anchored to the avatar → one click to switch
          else if(PC().openProfile) PC().openProfile();
        }catch(err){ PC().toast && PC().toast('could not open your accounts'); }
      }; }
    { const cb = $('#os-clock', bar); if(cb) cb.onclick = (e) => { e.stopPropagation(); toggleNoti(); }; }
    $('#os-exit', bar).onclick = () => exit();
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
  let notiOpen = false, notiSeen = 0, mailAck = 0;
  let barQuery = '', barFocused = false;   // the taskbar search box survives a drawBar() rebuild

  function notiCount(){
    try{ return (PC().notifItems && PC().notifItems(60).length) || 0; }catch(_){ return 0; }
  }

  function notiDot(){
    let n = 0, mail = 0;
    try{ n = notiCount() - notiSeen; }catch(_){}
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
    toggleStart(false);
    const panel = document.createElement('div');
    panel.id = 'os-noti';
    panel.className = 'os-noti';
    let items = [];
    try{ items = (PC().notifItems && PC().notifItems(60)) || []; }catch(_){}
    let mail = 0;
    try{ mail = (PC().mailUnread && PC().mailUnread()) || 0; }catch(_){}
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
       ${mailRow}
       <div class="os-noti-list">${rows || '<div class="empty">Nothing new.</div>'}</div>`;
    root.appendChild(panel);

    if(mail > 0) $('#os-noti-mail', panel).onclick = () => { hideNoti(); openApp('messages'); };
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

    notiSeen = notiCount();
    try{ PC().notifsRead && PC().notifsRead(); }catch(_){}
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

  function osToast(html, pic){
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
    t.onclick = () => { t.remove(); toggleNoti(true); };
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
      if(n > mailSeen) osToast(`✉ <b>${n - mailSeen} new email</b>`, '');
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
       <div class="os-stats" id="os-stats"></div>`;
    root.appendChild(menu);
    const searchNostr = (q) => {
      toggleStart(false);
      openDoc('search', 'Search', 'i-search', () => {
        try{ PC().runSearch && PC().runSearch(q); }
        catch(_){ try{ PC().toast('search is unavailable here'); }catch(__){} }
      });
    };
    const paint = (q) => {
      const list = apps().filter(a => !q || a.label.toLowerCase().includes(q.toLowerCase()));
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
    // The community counters live in the sidebar, which the desktop hides — so the start menu is
    // where they go. Read from the client's own cache (PC.communityStats): /client/stats counts the
    // caller as a viewer, and polling it again from here would inflate "online now" by one.
    try{
      const st = (PC().communityStats && PC().communityStats()) || {};
      /* All five, always — including the zeroes. "0 in call" is information; a row that vanishes
       * when it is zero just looks like the feature is missing, which is what happened here. */
      const row = (icon, n, label) =>
        `<span class="os-stat${(n > 0) ? ' on' : ''}" title="${enc(label)}">${iconSvg(icon)}<b>${enc(String(n || 0))}</b><i>${enc(label)}</i></span>`;
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
    desk.addEventListener('pointerdown', (e) => {
      if(e.target === desk || e.target.closest('.os-icons') === e.target){ toggleStart(false); toggleNoti(false); }
    });
    // …and so does clicking anywhere that is not the panel or the clock itself. Without this the
    // only way to close it is the clock, which is not where anyone's hand is by then.
    document.addEventListener('pointerdown', (e) => {
      if(!notiOpen) return;
      if(e.target.closest('#os-noti') || e.target.closest('#os-clock') || e.target.closest('.modal-bg')) return;
      toggleNoti(false);
    }, true);
    drawDesktop();
    drawBar();
    _clock = setInterval(() => { if(on && !startOpen && !notiOpen) drawBar(); }, 30000);
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
    toastHost = null; notiOpen = false;
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

  window.PCOS = { enter, exit, toggle, restore, isOn: () => on, openDoc, focusDoc, routeView, snapTo, osToast,
                  isRepainting: () => repainting > 0,
                  windows: () => wins.map(w => ({ view: w.view, title: w.title, min: w.min })) };
})();
