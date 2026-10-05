/* PosterChan, dancing on the desktop ("i want a live dancing posterchan character on the desktop mode that
 * you can interact with").
 *
 * EIGHT GENERATED FRAMES, ONE CHARACTER. static/mascot/dance/dance-1..8.webp were made with pose-guided
 * image generation (an OpenPose skeleton per move, one shared description), cut out, checked for halos,
 * fringes, crops and anatomy, and aligned so the feet share a baseline and the torso a centre line -- the
 * limbs move, the body does not jitter.
 *
 * OVER EVERY WINDOW ("posterchan should be going over windows right? should never be hidden unless you
 * kill it"). On the web desktop she is mounted in #os-desk above the in-page windows (client.css: over
 * the window band, under the start menu, flyouts and menus). On PosterChanOS the apps are real
 * compositor windows the desktop surface sits UNDER, so there she is drawn by a small window of her own
 * that the compositor keeps always on top (desktop/buddy-host.js + buddy.html); this file still decides
 * whether she is on and where. Only on the monitor that owns the background; dropped somewhere that
 * covers icons, she steps aside.
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
  /* 900ms a frame (~1.1 moves a second, chosen side by side against the old pace), and a little quicker after a click: at 340ms she read as frantic
   * ("posterchan is moving too fast"). tests/client/test_desktop_buddy_full_app.py measures the pace. */
  const BOX_W = 174, BOX_H = 240, BUBBLE = 44;   // her box (client.css .os-buddy); room above it for a line
  const FRAMES = 8, STEP_MS = 900, HAPPY_MS = 550, KEY = 'desktopBuddy';
  /* WHO DANCES ("make an alternative to posterchan that users can choose, a dancing axolotl. posterchan is
   * default of course"). Same 8 moves, same box and baseline; each has her own frames and lines. The
   * choice is `who` in the same synced preference, so it follows the account. */
  const CHARS = {
    posterchan: { name: 'PosterChan', dir: 'dance', lines: ['hi!', '♪ ♫', 'dance with me!', 'PosterChan!', 'hehe', 'nostr!', '✨'] },
    axolotl:    { name: 'Axolotl', dir: 'axolotl', lines: ['blub!', '♪ ♫', 'wanna dance?', 'axolotl!', 'hehe', 'splish!', '✨'] },
  };
  const who = () => pref().who;
  const SRC = (i, w) => '/static/mascot/' + CHARS[w || who()].dir + '/dance-' + i + '.webp';
  let el = null, desk = null, opts = {}, timer = 0, frame = 1, loaded = false, drag = null, pressT = 0, nativeUp = false, wired = false;
  /* PosterChanOS with real app windows: her own always-on-top window draws her. */
  const native = () => { try{ return !!(window.pcBuddy && window.PCOSWin && PCOSWin.enabled()); }catch(_){ return false; } };

  const CS = () => window.ClientSettings;
  const clamp = (v, d) => { const n = Number(v); return (v !== null && v !== '' && Number.isFinite(n)) ? Math.min(1, Math.max(0, n)) : d; };
  /* RESIZABLE AND PERSISTENT ("you need to make the axolotl, posterchan resizeable and persistent").
   * `size` scales her box (50%..250%, in quarter steps); `out` + `fx`/`fy` are the MONITOR she was left on
   * and her spot on it, so a restart puts her back exactly there -- even on another monitor than the one
   * that owns the desktop. All of it is in the same synced preference, so it follows the account. */
  const SIZE_MIN = 0.5, SIZE_MAX = 2.5;
  const sizeOf = v => { const n = Number(v); return Number.isFinite(n) && n > 0 ? Math.min(SIZE_MAX, Math.max(SIZE_MIN, Math.round(n * 4) / 4)) : 1; };
  function pref(){
    let v = null; try{ v = CS() && CS().get(KEY, null); }catch(_){ v = null; }
    v = (v && typeof v === 'object') ? v : {};
    return { on: v.on !== false, x: clamp(v.x, 0.86), y: clamp(v.y, 1), who: CHARS[v.who] ? v.who : 'posterchan',
             size: sizeOf(v.size), out: /^[A-Za-z0-9._-]{1,32}$/.test(String(v.out || '')) ? String(v.out) : '',
             fx: clamp(v.fx, null), fy: clamp(v.fy, null), clickThrough: through(v) };
  }
  /* LET CLICKS THROUGH IS THIS DEVICE'S, NOT THE ACCOUNT'S ("on webui, posterchan is not moveable, can't
   * change to axolotl, and not resizeable"). It used to ride the synced preference with her size, so
   * turning it on for PosterChanOS -- where her window cannot pass only its empty parts -- made her
   * untouchable in every browser too: no drag, and no menu to switch or resize her from. Whether she is
   * in the way is a fact about one screen. A value synced from elsewhere is ignored, except once on
   * PosterChanOS, where the switch was first set, so an existing choice there survives this move. */
  const THROUGH_KEY = 'pc.buddy.clickThrough.v1';
  function through(synced){
    let v = null; try{ v = localStorage.getItem(THROUGH_KEY); }catch(_){ v = null; }
    if(v === '1' || v === '0') return v === '1';
    if(native() && synced.clickThrough === true){ try{ localStorage.setItem(THROUGH_KEY, '1'); }catch(_){ } return true; }
    return false;
  }
  const boxW = p => Math.round(BOX_W * p.size), boxH = p => Math.round(BOX_H * p.size);
  function setPref(v){
    v = Object.assign({}, v);
    try{ localStorage.setItem(THROUGH_KEY, v.clickThrough ? '1' : '0'); }catch(_){ }
    delete v.clickThrough;                         // never synced: see through()
    try{ CS() && CS().set(KEY, v); }catch(_){ }
    try{ const pc = window.__PC; if(pc && pc.saveDesktopBuddy) pc.saveDesktopBuddy(v); }catch(_){ }
  }
  const reduced = () => { try{ return !!(window.matchMedia && matchMedia('(prefers-reduced-motion: reduce)').matches); }catch(_){ return false; } };
  const idle = () => document.hidden || document.documentElement.classList.contains('os-idle');

  function preload(){
    const w = who();
    if(loaded === w) return; loaded = w;     // only the chosen one's frames are ever fetched
    for(let i = 1; i <= FRAMES; i++){ const im = new Image(); im.decoding = 'async'; im.src = SRC(i, w); }
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
    const at = spot(p, el.offsetWidth, el.offsetHeight);
    if(!at) return;
    el.style.left = at.left + 'px';
    el.style.top = at.top + 'px';
  }
  /* Where she stands for preference p, in desk layout px, for a box of w x h. */
  function spot(p, w, h){
    if(!desk) return null;
    const dw = desk.clientWidth, dh = desk.clientHeight;
    if(!dw || !dh || !w) return null;
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
    return { left: Math.max(0, Math.min(dw - w, left)), top: Math.max(0, Math.min(dh - h, top)) };
  }
  /* PosterChanOS: ask for her window at the spot she would stand on the desk, in viewport px. */
  /* WHERE SHE IS ON SCREEN, in viewport px -- her own window's rectangle as last placed (PosterChanOS), or
   * her element in the page. The desktop's right-click menu asks: with clicks passing through her, a
   * right-click ON her reaches the desktop, and a menu opened at the pointer lands under her window. */
  let _nativeBox = null;
  function box(){
    if(nativeUp && _nativeBox) return Object.assign({}, _nativeBox);
    if(el && el.isConnected){ const r = el.getBoundingClientRect(); if(r.width > 0) return { left: r.left, top: r.top, width: r.width, height: r.height }; }
    return null;
  }
  function nativeShow(p){
    if(!desk) return;
    /* NOT WHILE THE DESKTOP IS STILL ITS STARTUP SIZE. The surface starts at its 500x540 minimum before it
     * is made full screen; sized against that she came up 3.8x too wide. Wait: the resize to the real
     * screen places her (see the resize listener below). */
    if((window.innerWidth || 0) < 640 || (window.innerHeight || 0) < 480) return;
    const at = spot(p, boxW(p), boxH(p));
    if(!at) return;
    const z = zf(), dr = desk.getBoundingClientRect();
    wire();
    nativeUp = true;
    _nativeBox = { left: dr.left + at.left * z, top: dr.top + (at.top - BUBBLE) * z, width: boxW(p) * z, height: (boxH(p) + BUBBLE) * z };
    try{ Promise.resolve(window.pcBuddy.show({ vx: dr.left + at.left * z, vy: dr.top + (at.top - BUBBLE) * z,
                                               bw: boxW(p) * z, bh: (boxH(p) + BUBBLE) * z, who: p.who,
                                               out: p.out, fx: p.fx, fy: p.fy, clickThrough: p.clickThrough })).catch(() => {}); }catch(_){ }
  }
  function nativeHide(){
    if(!nativeUp) return;
    nativeUp = false; _nativeBox = null;
    try{ Promise.resolve(window.pcBuddy.hide()).catch(() => {}); }catch(_){ }
  }
  /* Her window reports a drag's end (to be saved) and her own "Hide PosterChan". */
  function wire(){
    if(wired || !window.pcBuddy || !window.pcBuddy.onEvent) return;
    wired = true;
    window.pcBuddy.onEvent(ev => {
      if(!ev || !desk) return;
      if(ev.type === 'hide'){ nativeUp = false; hide(); return; }
      if(ev.type === 'switch'){ choose(who() === 'axolotl' ? 'posterchan' : 'axolotl'); return; }
      if(ev.type === 'size'){ resize(Number(ev.step) || 0); return; }
      if(ev.type === 'through'){ setClickThrough(true); return; }
      if(ev.type !== 'moved') return;
      if(_nativeBox && Number.isFinite(Number(ev.vx)) && Number.isFinite(Number(ev.vy)))
        _nativeBox = Object.assign({}, _nativeBox, { left: Number(ev.vx), top: Number(ev.vy) });
      const p = pref();
      const z = zf(), dr = desk.getBoundingClientRect();
      const dw = desk.clientWidth - boxW(p), dh = desk.clientHeight - boxH(p);
      const left = (Number(ev.vx) - dr.left) / z, top = (Number(ev.vy) - dr.top) / z + BUBBLE;
      if(dw > 0 && Number.isFinite(left)) p.x = clamp(left / dw, .86);
      if(dh > 0 && Number.isFinite(top)) p.y = clamp(top / dh, 1);
      // The monitor she was left on, and her spot on it -- what puts her back there after a restart.
      if(/^[A-Za-z0-9._-]{1,32}$/.test(String(ev.out || ''))){ p.out = String(ev.out); p.fx = clamp(ev.fx, null); p.fy = clamp(ev.fy, null); }
      setPref(p);
    });
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
    const lines = CHARS[who()].lines;
    say(lines[Math.floor(Math.random() * lines.length)]);
    clearTimeout(el._happy); el._happy = setTimeout(() => el && el.classList.remove('happy'), 2400);
    if(reduced()) show(frame % FRAMES + 1);
  }
  function menu(x, y){
    const other = who() === 'axolotl' ? 'posterchan' : 'axolotl';
    const rows = [{ label: 'Hide ' + CHARS[who()].name, run: hide }, { label: 'Dance!', run: cheer },
                  { label: 'Switch to ' + CHARS[other].name, run: () => choose(other) },
                  { label: 'Bigger', run: () => resize(1) }, { label: 'Smaller', run: () => resize(-1) },
                  { label: 'Let clicks through ' + CHARS[who()].name, run: () => setClickThrough(true) }];
    if(opts.menu) opts.menu(x, y, rows);
  }

  /* CLICKS GO THROUGH HER TRANSPARENT PARTS ("make posterchan/axolotl so it don't interfere with
   * clicking widgets/app elements behind it"). Her box is a rectangle and most of it is empty air
   * around a dancing figure; it used to swallow every click in it. Now each frame's alpha is read once
   * (same-origin images, an offscreen canvas) and, as the mouse moves, the box only catches the pointer
   * where she is actually drawn -- anywhere else it lets the press fall through to whatever is under her.
   * The image is drawn with object-fit: contain, so a point maps through that letterboxing first. */
  const alphaMaps = new Map();
  function alphaMap(im){
    const key = im.currentSrc || im.src;
    if(alphaMaps.has(key)) return alphaMaps.get(key);
    let map = null;
    try{
      const w = im.naturalWidth, h = im.naturalHeight, step = 4;
      const c = document.createElement('canvas'); c.width = Math.ceil(w / step); c.height = Math.ceil(h / step);
      const g = c.getContext('2d', { willReadFrequently: true }); g.drawImage(im, 0, 0, c.width, c.height);
      map = { w, h, cw: c.width, ch: c.height, data: g.getImageData(0, 0, c.width, c.height).data };
    }catch(_){ map = null; }
    if(map) alphaMaps.set(key, map);
    return map;
  }
  /* Is she drawn at this point of the viewport? Unknown (frame not decoded yet) answers yes, so her
   * box never goes dead to a click before her picture has loaded. */
  function opaqueAt(x, y){
    const im = el && el.querySelector('img');
    if(!im) return false;
    const r = im.getBoundingClientRect();
    if(x < r.left || x > r.right || y < r.top || y > r.bottom) return false;
    if(!im.complete || !im.naturalWidth) return true;
    const m = alphaMap(im); if(!m) return true;
    const k = Math.min(r.width / m.w, r.height / m.h), dw = m.w * k, dh = m.h * k;
    const ox = r.left + (r.width - dw) / 2, oy = r.top + (r.height - dh) / 2;
    const u = (x - ox) / dw, v = (y - oy) / dh;
    if(u < 0 || u > 1 || v < 0 || v > 1) return false;
    const cx = Math.min(m.cw - 1, Math.floor(u * m.cw)), cy = Math.min(m.ch - 1, Math.floor(v * m.ch));
    // A little slack around her outline: a pixel's neighbours count, so a thin arm is still grabbable.
    for(let dy = -1; dy <= 1; dy++) for(let dx = -1; dx <= 1; dx++){
      const px = cx + dx, py = cy + dy;
      if(px >= 0 && py >= 0 && px < m.cw && py < m.ch && m.data[(py * m.cw + px) * 4 + 3] > 40) return true;
    }
    return false;
  }
  function hitTest(e){
    if(!el || drag || e.pointerType === 'touch') return;
    if(pref().clickThrough){ el.style.pointerEvents = 'none'; return; }
    const on = opaqueAt(e.clientX, e.clientY);
    el.style.pointerEvents = on ? '' : 'none';
  }
  document.addEventListener('pointermove', hitTest, { capture: true, passive: true });
  function build(){
    const me = CHARS[who()];
    el = document.createElement('div');
    el.className = 'os-buddy';
    el.dataset.who = who();
    el.setAttribute('role', 'img');
    el.setAttribute('aria-label', me.name + ' dancing. Drag to move; right-click or long-press to hide or switch.');
    el.title = me.name + ' — click me, drag me, right-click to hide or switch';
    const im = document.createElement('img'); im.alt = ''; im.draggable = false; im.src = SRC(frame);
    el.appendChild(im);
    el.addEventListener('pointerenter', ev => { if(ev.pointerType !== 'touch' && !opaqueAt(ev.clientX, ev.clientY)){ el.style.pointerEvents = 'none'; return; } if(!drag && !el.classList.contains('happy')) show(2); });
    el.addEventListener('contextmenu', ev => { ev.preventDefault(); ev.stopPropagation(); menu(ev.clientX, ev.clientY); });
    el.addEventListener('wheel', ev => { if(!ev.ctrlKey) return; ev.preventDefault(); resize(ev.deltaY < 0 ? 1 : -1); }, { passive: false });
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
      p.out = ''; p.fx = null; p.fy = null;          // the web desk is one surface: no monitor to remember
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
    if(native()){
      if(el){ stop(); el.remove(); el = null; }
      nativeShow(p);
      return;
    }
    preload();
    if(!el || !el.isConnected || el.parentNode !== desk || el.dataset.who !== p.who){ if(el) el.remove(); frame = 1; desk.appendChild(build()); }
    // Wait for a laid-out size before placing (the first frame may not have decoded yet).
    el.style.width = boxW(p) + 'px'; el.style.height = boxH(p) + 'px';
    el.style.pointerEvents = p.clickThrough ? 'none' : '';
    const go = () => { place(p); start(); };
    const im = el.querySelector('img');
    if(im.complete && im.naturalWidth) go(); else im.addEventListener('load', go, { once: true });
  }
  function unmount(){ stop(); if(el){ el.remove(); el = null; } nativeHide(); }
  function hide(){
    const p = pref(); p.on = false; setPref(p); unmount();
    try{ const pc = window.__PC; pc && pc.toast && pc.toast(CHARS[p.who].name + ' hidden. Right-click the desktop (or Settings, Timeline) to bring her back.'); }catch(_){ }
  }
  /* Switch who dances; she keeps her spot. */
  function choose(w){
    if(!CHARS[w]) return;
    const p = pref(); p.who = w; p.on = true; setPref(p);
    if(desk) mount(desk, opts);
  }
  /* Bigger / Smaller: a quarter step, kept within 50%..250%, saved with the account. */
  function resize(step){
    if(!step) return;
    const p = pref(), next = sizeOf(p.size + 0.25 * Math.sign(step));
    if(next === p.size) return;
    p.size = next; setPref(p);
    if(desk) mount(desk, opts);
  }
  /* LET CLICKS THROUGH HER: she is decoration, every click reaches what is behind her. On PosterChanOS
   * her own window cannot let only its empty parts through -- measured on Wayland: Electron's setShape
   * changes what is drawn, not what is clicked, while setIgnoreMouseEvents passes every click -- so
   * this is a switch. Her own menu cannot be reached while it is on; the desktop's right-click menu and
   * Settings turn it back off. Kept on this device only -- see through(). */
  function setClickThrough(on){
    const p = pref(); p.clickThrough = !!on; setPref(p);
    if(desk) mount(desk, opts);
    try{ const pc = window.__PC; if(on && pc && pc.toast) pc.toast(CHARS[p.who].name + ' lets clicks through now. Right-click the desktop (or Settings) to grab her again.'); }catch(_){ }
  }
  function setSize(v){ const p = pref(); p.size = sizeOf(v); setPref(p); if(desk) mount(desk, opts); }
  function reveal(){ const p = pref(); p.on = true; setPref(p); mount(desk, opts); }
  /* A synced preference arrived or Settings changed it: re-read and re-apply. */
  function refresh(){ if(desk) mount(desk, opts); }

  document.addEventListener('visibilitychange', () => { if(el && !document.hidden) start(); });
  /* HER WINDOW IS SIZED FROM THIS PAGE, SO A PAGE THAT CHANGES SIZE MOVES HER. Measured on the laptop:
   * the desktop surface starts at about 500x540 before it is made full screen, and she was sized and
   * placed in that instant -- four times too big (668x568 instead of 174x284), and on the 4K desk off the
   * edge of the monitor, where she could not be dragged to the edge because her box was the problem.
   * Re-place on every resize (a monitor change, the surface going full screen, the UI scale). */
  let resizeT = 0;
  window.addEventListener('resize', () => {
    if(!nativeUp) return;
    clearTimeout(resizeT);
    resizeT = setTimeout(() => { const p = pref(); if(p.on && nativeUp) nativeShow(p); }, 150);
  });
  window.PCBuddy = { mount, unmount, hide, show: reveal, refresh, choose, isOn: () => pref().on, who,
                    resize, setSize, size: () => pref().size, setClickThrough, clickThrough: () => pref().clickThrough, box,
                    name: () => CHARS[who()].name, choices: () => Object.keys(CHARS).map(k => ({ id: k, name: CHARS[k].name })),
                    _frame: () => frame };
})();
