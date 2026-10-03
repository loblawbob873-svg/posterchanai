/* PosterChan, dancing on the desktop ("i want a live dancing posterchan character on the desktop mode that
 * you can interact with").
 *
 * EIGHT GENERATED FRAMES, ONE CHARACTER. static/mascot/dance/dance-1..8.webp were made with pose-guided
 * image generation (an OpenPose skeleton per move, one shared description), cut out, checked for halos,
 * fringes, crops and anatomy, and aligned so the feet share a baseline and the torso a centre line -- the
 * limbs move, the body does not jitter.
 *
 * LIVES ON THE DESKTOP, NEVER OVER WORK. Mounted by os.js inside #os-desk, above the wallpaper and the
 * icons, below widgets and windows; never inside an app window (os.js never draws a desktop there) and
 * only on the monitor that owns the background. Dropped somewhere that covers icons, she steps aside.
 *
 * EASY TO TURN OFF ("make sure users can disable the dancing posterchan somehow sometimes they may hate
 * it"): right-click / long-press -> Hide PosterChan; the desktop's own menu offers her back; Settings ->
 * Timeline has the switch. Off means NOTHING loads -- no frames fetched, no timer.
 *
 * FOLLOWS THE ACCOUNT. `desktopBuddy` {on, x, y} goes in the synced client prefs (pcai:client-prefs);
 * x/y are fractions of the desktop so she keeps her corner on any screen size.
 */
(function(){
  'use strict';
  /* 700ms a frame (~1.4 moves a second), and a little quicker after a click: at 340ms she read as frantic
   * ("posterchan is moving too fast"). tests/client/test_desktop_buddy_full_app.py measures the pace. */
  const FRAMES = 8, STEP_MS = 700, HAPPY_MS = 450, KEY = 'desktopBuddy';
  const SRC = i => '/static/mascot/dance/dance-' + i + '.webp';
  const LINES = ['hi!', '♪ ♫', 'dance with me!', 'PosterChan!', 'hehe', 'nostr!', '✨'];
  let el = null, desk = null, opts = {}, timer = 0, frame = 1, loaded = false, drag = null, pressT = 0;

  const CS = () => window.ClientSettings;
  const clamp = (v, d) => { const n = Number(v); return Number.isFinite(n) ? Math.min(1, Math.max(0, n)) : d; };
  function pref(){
    let v = null; try{ v = CS() && CS().get(KEY, null); }catch(_){ v = null; }
    v = (v && typeof v === 'object') ? v : {};
    return { on: v.on !== false, x: clamp(v.x, 0.86), y: clamp(v.y, 1) };
  }
  function setPref(v){
    try{ CS() && CS().set(KEY, v); }catch(_){ }
    try{ const pc = window.__PC; if(pc && pc.saveDesktopBuddy) pc.saveDesktopBuddy(v); }catch(_){ }
  }
  const reduced = () => { try{ return !!(window.matchMedia && matchMedia('(prefers-reduced-motion: reduce)').matches); }catch(_){ return false; } };
  const idle = () => document.hidden || document.documentElement.classList.contains('os-idle');

  function preload(){
    if(loaded) return; loaded = true;
    for(let i = 1; i <= FRAMES; i++){ const im = new Image(); im.decoding = 'async'; im.src = SRC(i); }
  }
  function show(i){ frame = i; const im = el && el.querySelector('img'); if(im && im.getAttribute('src') !== SRC(i)) im.setAttribute('src', SRC(i)); }
  function tick(){
    timer = 0;
    if(!el || !el.isConnected) return;
    if(!idle() && !reduced() && !drag) show(frame % FRAMES + 1);
    timer = setTimeout(tick, el.classList.contains('happy') ? HAPPY_MS : STEP_MS);
  }
  function start(){ if(!timer && !reduced()) timer = setTimeout(tick, STEP_MS); }
  function stop(){ if(timer){ clearTimeout(timer); timer = 0; } }

  /* Where she stands, in desk layout px (the desk is zoomed: divide client rects by the zoom). */
  function zf(){ try{ return parseFloat(getComputedStyle(document.body).zoom) || 1; }catch(_){ return 1; } }
  function place(p){
    if(!el || !desk) return;
    const dw = desk.clientWidth, dh = desk.clientHeight, w = el.offsetWidth, h = el.offsetHeight;
    if(!dw || !dh || !w) return;
    let left = Math.round((dw - w) * p.x), top = Math.round((dh - h) * p.y);
    // Never on the icons: step sideways until her box is clear (or stay put if the desk is full).
    const z = zf(), dr = desk.getBoundingClientRect();
    const icons = [...desk.querySelectorAll('.os-icon')].map(n => { const r = n.getBoundingClientRect();
      return { l: (r.left - dr.left) / z, t: (r.top - dr.top) / z, r: (r.right - dr.left) / z, b: (r.bottom - dr.top) / z }; });
    const hits = (L, T) => icons.some(r => L < r.r - 4 && L + w > r.l + 4 && T < r.b - 4 && T + h > r.t + 4);
    if(hits(left, top)){
      for(let d = 8; d < dw; d += 8){
        if(left + d + w <= dw && !hits(left + d, top)){ left += d; break; }
        if(left - d >= 0 && !hits(left - d, top)){ left -= d; break; }
      }
    }
    el.style.left = Math.max(0, Math.min(dw - w, left)) + 'px';
    el.style.top = Math.max(0, Math.min(dh - h, top)) + 'px';
  }
  function say(text){
    if(!el) return;
    let b = el.querySelector('.os-buddy-say');
    if(!b){ b = document.createElement('span'); b.className = 'os-buddy-say'; el.appendChild(b); }
    b.textContent = text; b.classList.add('on');
    clearTimeout(b._t); b._t = setTimeout(() => b.classList.remove('on'), 1600);
  }
  function cheer(){
    if(!el) return;
    el.classList.remove('hop'); void el.offsetWidth; el.classList.add('hop', 'happy');
    say(LINES[Math.floor(Math.random() * LINES.length)]);
    clearTimeout(el._happy); el._happy = setTimeout(() => el && el.classList.remove('happy'), 2400);
    if(reduced()) show(frame % FRAMES + 1);
  }
  function menu(x, y){
    const rows = [{ label: 'Hide PosterChan', run: hide }, { label: 'Dance!', run: cheer }];
    if(opts.menu) opts.menu(x, y, rows);
  }

  function build(){
    el = document.createElement('div');
    el.className = 'os-buddy';
    el.setAttribute('role', 'img');
    el.setAttribute('aria-label', 'PosterChan dancing. Drag to move her; right-click or long-press to hide her.');
    el.title = 'PosterChan — click me, drag me, right-click to hide';
    const im = document.createElement('img'); im.alt = ''; im.draggable = false; im.src = SRC(frame);
    el.appendChild(im);
    el.addEventListener('pointerenter', () => { if(!drag && !el.classList.contains('happy')) show(2); });
    el.addEventListener('contextmenu', ev => { ev.preventDefault(); ev.stopPropagation(); menu(ev.clientX, ev.clientY); });
    el.addEventListener('pointerdown', ev => {
      if(ev.button !== 0) return;
      ev.stopPropagation();
      const z = zf(), r = el.getBoundingClientRect();
      drag = { id: ev.pointerId, sx: ev.clientX, sy: ev.clientY, ox: (ev.clientX - r.left) / z, oy: (ev.clientY - r.top) / z, moved: false };
      try{ el.setPointerCapture(ev.pointerId); }catch(_){ }
      // Touch: a long press is the right-click.
      clearTimeout(pressT);
      if(ev.pointerType !== 'mouse') pressT = setTimeout(() => { if(drag && !drag.moved){ const x = drag.sx, y = drag.sy; drag = null; menu(x, y); } }, 550);
    });
    el.addEventListener('pointermove', ev => {
      if(!drag || ev.pointerId !== drag.id) return;
      if(!drag.moved && Math.hypot(ev.clientX - drag.sx, ev.clientY - drag.sy) < 4) return;
      drag.moved = true; clearTimeout(pressT); el.classList.add('dragging');
      const z = zf(), dr = desk.getBoundingClientRect();
      const L = (ev.clientX - dr.left) / z - drag.ox, T = (ev.clientY - dr.top) / z - drag.oy;
      el.style.left = Math.max(0, Math.min(desk.clientWidth - el.offsetWidth, L)) + 'px';
      el.style.top = Math.max(0, Math.min(desk.clientHeight - el.offsetHeight, T)) + 'px';
    });
    const end = ev => {
      if(!drag || (ev && ev.pointerId !== drag.id)) return;
      clearTimeout(pressT);
      const moved = drag.moved; drag = null; el.classList.remove('dragging');
      if(!moved){ cheer(); return; }
      const dw = desk.clientWidth - el.offsetWidth, dh = desk.clientHeight - el.offsetHeight;
      const p = pref();
      p.x = dw > 0 ? (parseFloat(el.style.left) || 0) / dw : p.x;
      p.y = dh > 0 ? (parseFloat(el.style.top) || 0) / dh : p.y;
      p.x = clamp(p.x, .86); p.y = clamp(p.y, 1);
      setPref(p); place(p);
    };
    el.addEventListener('pointerup', end);
    el.addEventListener('pointercancel', end);
    return el;
  }

  /* Called by os.js whenever it draws the desktop. Idempotent. */
  function mount(deskEl, o){
    desk = deskEl || desk; opts = o || opts;
    const p = pref();
    if(!p.on || !desk){ unmount(); return; }
    preload();
    if(!el || !el.isConnected || el.parentNode !== desk){ if(el) el.remove(); desk.appendChild(build()); }
    // Wait for a laid-out size before placing (the first frame may not have decoded yet).
    const go = () => { place(p); start(); };
    const im = el.querySelector('img');
    if(im.complete && im.naturalWidth) go(); else im.addEventListener('load', go, { once: true });
  }
  function unmount(){ stop(); if(el){ el.remove(); el = null; } }
  function hide(){
    const p = pref(); p.on = false; setPref(p); unmount();
    try{ const pc = window.__PC; pc && pc.toast && pc.toast('PosterChan hidden. Right-click the desktop (or Settings, Timeline) to bring her back.'); }catch(_){ }
  }
  function reveal(){ const p = pref(); p.on = true; setPref(p); mount(desk, opts); }
  /* A synced preference arrived or Settings changed it: re-read and re-apply. */
  function refresh(){ if(desk) mount(desk, opts); }

  document.addEventListener('visibilitychange', () => { if(el && !document.hidden) start(); });
  window.PCBuddy = { mount, unmount, hide, show: reveal, refresh, isOn: () => pref().on, _frame: () => frame };
})();
