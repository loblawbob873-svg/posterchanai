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
  let realFeed = null;             // the client's own #feed, parked while the desktop owns the id

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

  // ---- the #feed handoff ---------------------------------------------------------------------

  /* Give `id="feed"` to this window's body and to nothing else, then let the app render into it.
   *
   * Duplicate ids are invalid and `querySelector` would answer with whichever came first in the
   * document, so exactly one element may hold it at a time — that single rule is what keeps every
   * untouched feature pointing at the right window. */
  function claimFeed(w){
    const cur = document.getElementById('feed');
    if(cur && cur !== w.body) cur.removeAttribute('id');
    if(w.body.id !== 'feed') w.body.id = 'feed';
  }

  function releaseFeed(){
    const cur = document.getElementById('feed');
    if(cur && cur !== realFeed) cur.removeAttribute('id');
    if(realFeed && realFeed.id !== 'feed') realFeed.id = 'feed';
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
      try{ PC().switchView ? PC().switchView(w.view) : null; }catch(err){ /* a view that refuses is not fatal */ }
    }
  }

  const ICON_COL = 120;             // the desktop-icon column; windows open clear of it

  function place(i){
    // Cascade, then wrap, so opening several windows does not land exactly on top of one another
    // (which reads as "only one opened") — and start to the RIGHT of the icon column, or the first
    // window covers the icons you just clicked.
    const vw = window.innerWidth, vh = window.innerHeight - TASKBAR;
    const w = Math.min(1100, Math.round((vw - ICON_COL) * 0.72));
    const h = Math.min(760, Math.round(vh * 0.78));
    const step = 38, n = i % 6;
    return { x: ICON_COL + 16 + n * step, y: Math.round(vh * 0.05) + n * step, w, h };
  }

  function openApp(view, label, icon){
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
       <div class="osw-body feed"></div>
       <span class="osw-grip" aria-hidden="true"></span>`;
    desk.appendChild(el);
    const w = { id: ++seq, view, title: label, icon, el, body: $('.osw-body', el),
                min: false, max: false, rect: r };
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
    $('.osw-grip', el).addEventListener('pointerdown', e => { focusWin(w, false); startResize(w, e); });
    el.addEventListener('pointerdown', () => { if(!el.classList.contains('focused')) focusWin(w); }, true);

    focusWin(w);
    return w;
  }

  function closeWin(w){
    const i = wins.indexOf(w);
    if(i < 0) return;
    wins.splice(i, 1);
    // If this window held the id, hand it back BEFORE removing the element, or `$('#feed')` briefly
    // resolves to nothing and whatever renders next paints into a detached node.
    if(w.body.id === 'feed') releaseFeed();
    w.el.remove();
    const next = wins.filter(x => !x.min).pop();
    if(next) focusWin(next); else drawBar();
  }

  function minimise(w){
    w.min = true;
    w.el.classList.add('minimised');
    if(w.body.id === 'feed') releaseFeed();
    const next = wins.filter(x => !x.min).pop();
    if(next) focusWin(next); else drawBar();
  }

  function toggleMax(w){
    w.max = !w.max;
    w.el.classList.toggle('maximised', w.max);
    if(w.max){
      w.rect = { x: parseInt(w.el.style.left, 10), y: parseInt(w.el.style.top, 10),
                 w: w.el.offsetWidth, h: w.el.offsetHeight };
      Object.assign(w.el.style, { left: SNAP + 'px', top: SNAP + 'px',
        width: (window.innerWidth - SNAP * 2) + 'px',
        height: (window.innerHeight - TASKBAR - SNAP * 2) + 'px' });
    }else{
      Object.assign(w.el.style, { left: w.rect.x + 'px', top: w.rect.y + 'px',
        width: w.rect.w + 'px', height: w.rect.h + 'px' });
    }
    focusWin(w);
  }

  function startDrag(w, ev){
    if(w.max) return;
    const sx = ev.clientX, sy = ev.clientY;
    const ox = parseInt(w.el.style.left, 10), oy = parseInt(w.el.style.top, 10);
    const move = (e) => {
      // Clamped so a window can never be dragged somewhere it cannot be dragged back from: the title
      // bar stays on screen and above the taskbar.
      const x = Math.max(-w.el.offsetWidth + 120, Math.min(window.innerWidth - 120, ox + e.clientX - sx));
      const y = Math.max(0, Math.min(window.innerHeight - TASKBAR - 34, oy + e.clientY - sy));
      w.el.style.left = x + 'px'; w.el.style.top = y + 'px';
    };
    const up = () => { document.removeEventListener('pointermove', move);
                       document.removeEventListener('pointerup', up); };
    document.addEventListener('pointermove', move);
    document.addEventListener('pointerup', up);
  }

  function startResize(w, ev){
    if(w.max) return;
    ev.preventDefault();
    const sx = ev.clientX, sy = ev.clientY, ow = w.el.offsetWidth, oh = w.el.offsetHeight;
    const move = (e) => {
      w.el.style.width = Math.max(420, ow + e.clientX - sx) + 'px';
      w.el.style.height = Math.max(260, oh + e.clientY - sy) + 'px';
    };
    const up = () => { document.removeEventListener('pointermove', move);
                       document.removeEventListener('pointerup', up); };
    document.addEventListener('pointermove', move);
    document.addEventListener('pointerup', up);
  }

  // ---- desktop, taskbar, start menu -----------------------------------------------------------

  function drawDesktop(){
    desk.querySelectorAll('.os-icon').forEach(n => n.remove());
    const grid = document.createElement('div');
    grid.className = 'os-icons';
    grid.innerHTML = apps().map(a =>
      `<button class="os-icon" data-view="${enc(a.view)}" title="${enc(a.label)}">
         ${iconSvg(a.icon)}<span>${enc(a.label)}</span></button>`).join('');
    desk.appendChild(grid);
    $$('.os-icon', grid).forEach(b => {
      // Single click opens. A desktop double-click is the convention, but this is a web app people
      // arrive at from a single-click UI, and a double-click that does nothing the first time reads
      // as broken.
      b.onclick = () => openApp(b.dataset.view);
    });
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
       <div class="os-tasks">${wins.map(w =>
         `<button class="os-task${w.el.classList.contains('focused') && !w.min ? ' on' : ''}"
                  data-id="${w.id}" title="${enc(w.title)}">
            ${iconSvg(w.icon)}<span>${enc(w.title)}</span></button>`).join('')}</div>
       <div class="os-tray">
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
       <div class="os-applist" id="os-applist"></div>`;
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
    if(!on || fits()) return;
    const remember = settings().get(KEY, false);
    exit();
    if(remember) settings().set(KEY, true);
    try{ PC().toast && PC().toast('Turn the device sideways for the desktop'); }catch(_){}
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

  window.PCOS = { enter, exit, toggle, restore, isOn: () => on,
                  windows: () => wins.map(w => ({ view: w.view, title: w.title, min: w.min })) };
})();
