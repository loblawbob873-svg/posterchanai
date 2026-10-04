'use strict';
/* THE DESKTOP POSTERCHAN ON POSTERCHANOS: HER OWN WINDOW, OVER EVERY OTHER ONE.
 *
 * "posterchan should be going over windows right? should never be hidden unless you kill it". On the
 * web desktop she is an element above the in-page windows (client.css z-index). On PosterChanOS every
 * app is a real compositor toplevel and the desktop surface is UNDER all of them, so nothing drawn on
 * it can ever cover them. She therefore gets a small transparent window of her own (buddy.html), which
 * the compositor keeps ALWAYS ON TOP and on every workspace (sticky), and which is shown INACTIVE and
 * never focused -- she must not take the keyboard from whatever the person is typing in.
 *
 * The desktop renderer that owns the background (buddy.js) stays the owner of her state: whether she
 * is on, and where (fractions of the desk, synced with the account). It asks for her here in its own
 * viewport pixels; this converts to the compositor's pixels on that renderer's output, exactly as the
 * popups do. A drag happens in her window, moves it through the compositor (a Wayland client cannot
 * position itself), and the final spot goes back to the owner to be saved. "Hide PosterChan" from her
 * own menu goes back the same way, so the setting is written in one place.
 *
 * Dependencies are injected so tests/test_desktop_buddy_window.py runs this file under node. */
const TITLE = 'PosterChan Buddy';

function createBuddyHost(deps){
  const { wm, scopeOf, pagePath, preloadPath } = deps;
  /* A FACTORY, NOT THE CLASS. main.js only gets Electron's BrowserWindow once the app is ready, after
   * this host is created -- a class captured here was `undefined` for good, and every show() on the
   * real machine threw "BrowserWindow is not a constructor" while every test (which passed a class in)
   * passed. `createWindow` is called at show() time. */
  const createWindow = deps.createWindow || (o => new deps.BrowserWindow(o));
  const sleep = deps.sleep || (ms => new Promise(r => setTimeout(r, ms)));
  let win = null, owner = null, id = null, at = null, out = null, k = 1, opening = null, who = 'posterchan', rebuilt = false;
  const WHO = /^[a-z]{1,20}$/;   // a dancer's id, passed to her page as #who (validated there too)

  const alive = () => !!(win && !win.isDestroyed());
  const tell = ev => { try{ if(owner && !owner.isDestroyed()) owner.send('pc:buddy:event', ev); }catch(_){ } };

  let rects = [];                  // every output's rectangle: she may be dragged onto any of them
  let named = [];                  // the same, with each output's name: where she was left is SAVED by name
  async function outputOf(sender){
    const scope = scopeOf(sender.id);
    const outs = await wm().outputs();
    named = outs.filter(x => x && x.rect && x.rect.width > 0 && x.rect.height > 0).map(x => ({ name: String(x.name || ''), rect: x.rect }));
    rects = named.map(x => x.rect);
    const o = (scope && outs.find(x => x && x.name === scope.output)) || outs[0];
    return o && o.rect;
  }
  /* The output a box belongs on: the one under its centre, else the nearest. Clamping to the OWNER's
   * output stopped every drag at that monitor's edge ("when you drag it to the other monitor, it goes
   * about 15% in"). */
  function homeOf(b){
    const cx = b.x + b.w / 2, cy = b.y + b.h / 2;
    const list = rects.length ? rects : (out ? [out.rect] : []);
    let best = null, bd = Infinity;
    for(const r of list){
      if(cx >= r.x && cx < r.x + r.width && cy >= r.y && cy < r.y + r.height) return r;
      const dx = Math.max(r.x - cx, 0, cx - (r.x + r.width)), dy = Math.max(r.y - cy, 0, cy - (r.y + r.height));
      if(dx * dx + dy * dy < bd){ bd = dx * dx + dy * dy; best = r; }
    }
    return best;
  }
  const keepIn = (b, r) => ({ x: Math.round(Math.min(Math.max(r.x, b.x), r.x + Math.max(0, r.width - b.w))),
                              y: Math.round(Math.min(Math.max(r.y, b.y), r.y + Math.max(0, r.height - b.h))) });
  async function findRow(){
    for(let i = 0; i < 40; i++){
      if(!alive()) return null;
      try{
        const row = (await wm().windows()).find(x => String(x.title || '') === TITLE);
        if(row) return row;
      }catch(_){ }
      await sleep(50);
    }
    return null;
  }
  /* SHE NEVER KEEPS THE KEYBOARD ("fix the focus issue too"). `focusable: false` is not honoured on
   * Wayland, so a click on her made the compositor activate her window and the person's typing went
   * nowhere until they clicked back. Whenever her window is focused, focus goes straight back to the
   * window that had it before (the compositor's own last-focused order). Everything she does -- click,
   * drag, her menu -- works by pointer, which focus does not affect. */
  let giving = false;
  async function giveBackFocus(){
    if(giving || id == null) return;
    giving = true;
    try{
      const rows = (await wm().windows()).filter(r => r && Number(r.id) !== id && !r.stashed
                                                   && String(r.title || '') !== TITLE);
      rows.sort((a, b) => (Number(b.focusTime) || 0) - (Number(a.focusTime) || 0));
      if(rows[0]) await wm().focus(Number(rows[0].id));
    }catch(_){ }
    finally{ giving = false; }
  }
  async function open(box, w){
    win = createWindow({
      show: false, frame: false, transparent: true, backgroundColor: '#00000000', hasShadow: false,
      /* RESIZABLE, though nobody drags her edges: on Wayland a non-resizable window's min and max size
       * are pinned to its FIRST size, and every later setSize/placement is ignored. Measured on the
       * laptop: created while the desktop surface was still 500x540 she came up 668x568 (min = max =
       * 668x568) and stayed that size -- off the right edge, unable to reach it, the drag stopping short. */
      resizable: true, skipTaskbar: true, focusable: false, alwaysOnTop: true, title: TITLE,
      width: box.w, height: box.h,
      webPreferences: { contextIsolation: true, nodeIntegration: false, sandbox: true, preload: preloadPath },
    });
    const mine = win;
    win.on('page-title-updated', e => e.preventDefault());   // the title is how the compositor finds her
    win.on('closed', () => { if(win === mine){ win = null; id = null; at = null; } });
    win.on('focus', () => { if(win === mine) giveBackFocus(); });
    who = w;
    await win.loadFile(pagePath, { hash: w });
    if(!alive() || win !== mine) return false;
    win.showInactive();
    const row = await findRow();
    if(!row || win !== mine) return false;
    id = Number(row.id);
    // Electron sizes are DIP, the compositor's are its own pixels: measure the ratio once.
    try{ const b = win.getBounds(); if(b.width > 0 && row.rect && row.rect.width > 0) k = row.rect.width / b.width; }catch(_){ }
    try{ await wm().alwaysOnTop(id, true); }catch(_){ }
    try{ await wm().sticky(id, true); }catch(_){ }
    // Wayfire activates every window it maps, showInactive or not (measured: activated=True the moment
    // she appeared). Whoever had the keyboard gets it straight back.
    await giveBackFocus();
    return true;
  }

  /* want = { vx, vy, bw, bh, vw, vh } in the owner's viewport pixels: her top-left, her box, and the
   * viewport it is measured in. */
  async function show(sender, want){
    const w = want && typeof want === 'object' ? want : {};
    const n = (v, d) => { const x = Number(v); return Number.isFinite(x) ? x : d; };
    owner = sender;
    let rect = null;
    try{ rect = await outputOf(sender); }catch(_){ rect = null; }
    if(!rect) return false;
    const sx = rect.width / Math.max(1, n(w.vw, rect.width)), sy = rect.height / Math.max(1, n(w.vh, rect.height));
    const box = { w: Math.max(40, Math.round(n(w.bw, 174) * sx)), h: Math.max(40, Math.round(n(w.bh, 284) * sy)) };
    box.x = Math.round(rect.x + Math.min(Math.max(0, n(w.vx, 0) * sx), rect.width - box.w));
    box.y = Math.round(rect.y + Math.min(Math.max(0, n(w.vy, 0) * sy), rect.height - box.h));
    /* PERSISTENT: the monitor she was left on, by name, and her spot on it as fractions. A monitor that
     * is not connected now leaves her on the desktop's own (the spot above). */
    const saved = named.find(x => x.name && x.name === String(w.out || ''));
    const fx = Number(w.fx), fy = Number(w.fy);
    if(saved && w.fx != null && w.fy != null && Number.isFinite(fx) && Number.isFinite(fy)){
      const r = saved.rect;
      box.x = Math.round(r.x + Math.min(1, Math.max(0, fx)) * Math.max(0, r.width - box.w));
      box.y = Math.round(r.y + Math.min(1, Math.max(0, fy)) * Math.max(0, r.height - box.h));
    }
    out = { rect, sx, sy };
    const nextWho = WHO.test(String(w.who || '')) ? String(w.who) : 'posterchan';
    if(!alive()){
      if(!opening) opening = open(box, nextWho).finally(() => { opening = null; });
      if(!(await opening)) return false;
    }else if(nextWho !== who){
      // Switched dancer: same window, same spot, her page reloaded for the other one.
      who = nextWho;
      try{ await win.loadFile(pagePath, { hash: nextWho }); }catch(_){ }
    }
    if(id == null) return false;
    try{ win.setSize(Math.max(1, Math.round(box.w / k)), Math.max(1, Math.round(box.h / k))); }catch(_){ }
    try{ await wm().place(id, box.x, box.y, box.w, box.h); }catch(_){ return false; }
    at = box;
    /* SHE IS AS BIG AS THE COMPOSITOR SAYS, NOT AS BIG AS ASKED. A window that refused the size (on
     * Wayland a min size can stick at the first size) was clamped with the size asked for, so the
     * rest of her hung off the edge: 668x568 at x=1786 on a 1920-wide laptop, 1337x1347 on a 4K
     * desk. Measured, then: a window more than a quarter too big is rebuilt once at the right size,
     * and the edges are kept with the size she really has. */
    let real = null;
    try{ const row = (await wm().windows()).find(x => Number(x.id) === id); real = row && row.rect; }catch(_){ }
    if(real && real.width > 0 && real.height > 0){
      const tooBig = real.width > box.w * 1.25 || real.height > box.h * 1.25;
      if(tooBig && !rebuilt){ rebuilt = true; hide(); return show(sender, want); }
      at = { x: box.x, y: box.y, w: real.width, h: real.height };
      const fit = keepIn(at, homeOf(at) || rect);
      if(fit.x !== at.x || fit.y !== at.y){
        at.x = fit.x; at.y = fit.y;
        try{ await wm().place(id, at.x, at.y, at.w, at.h); }catch(_){ }
      }
    }
    return true;
  }
  function hide(){
    const w = win; win = null; id = null; at = null;
    try{ if(w && !w.isDestroyed()) w.destroy(); }catch(_){ }
    return true;
  }
  const fromHer = sender => alive() && sender === win.webContents;
  /* A drag step, in her page's CSS pixels. */
  function drag(sender, dx, dy){
    if(!fromHer(sender) || id == null || !at || !out) return false;
    const next = { x: at.x + (Number(dx) || 0) * k, y: at.y + (Number(dy) || 0) * k, w: at.w, h: at.h };
    const fit = keepIn(next, homeOf(next) || out.rect);
    at.x = fit.x; at.y = fit.y;
    try{ const m = wm().move ? wm().move(id, at.x, at.y) : wm().place(id, at.x, at.y, at.w, at.h); if(m && m.catch) m.catch(() => {}); }catch(_){ }
    return true;
  }
  /* The drag ended: hand the spot back to the owner, in ITS viewport pixels, to be saved. */
  function drop(sender){
    if(!fromHer(sender) || !at || !out) return false;
    const r = homeOf(at) || out.rect, o = named.find(x => x.rect === r);
    tell({ type: 'moved', vx: (at.x - out.rect.x) / out.sx, vy: (at.y - out.rect.y) / out.sy,
           out: o ? o.name : '', fx: r.width > at.w ? (at.x - r.x) / (r.width - at.w) : 0,
           fy: r.height > at.h ? (at.y - r.y) / (r.height - at.h) : 0 });
    giveBackFocus();
    return true;
  }
  function menu(sender, action){
    if(!fromHer(sender)) return false;
    if(String(action) === 'hide'){ tell({ type: 'hide' }); hide(); return true; }
    // The desktop owns who dances: it saves the choice and shows her again as the other one.
    if(String(action) === 'switch'){ tell({ type: 'switch' }); giveBackFocus(); return true; }
    // Bigger / Smaller: the desktop owns her size too (saved with the account) and shows her again.
    if(String(action) === 'bigger' || String(action) === 'smaller'){ tell({ type: 'size', step: action === 'bigger' ? 1 : -1 }); giveBackFocus(); return true; }
    /* SHE NEVER KEEPS THE KEYBOARD. Electron's 'focus' event does not fire for her on Wayland, so the
     * compositor left her activated after every click or drag (measured on the laptop: activated=True
     * after a drag). Her page says when a press ends, and focus goes straight back. */
    if(String(action) === 'release'){ giveBackFocus(); return true; }
    return false;
  }
  /* The owner is gone (its renderer reloaded or closed): she goes with it, and comes back when it
   * mounts her again. */
  function ownerGone(contentsId){ if(owner && owner.id === contentsId){ owner = null; hide(); } }

  return { show, hide, drag, drop, menu, ownerGone, giveBackFocus, TITLE, _state: () => ({ id, at, k, open: alive(), who }) };
}

module.exports = { createBuddyHost, TITLE };
