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
  let win = null, owner = null, id = null, at = null, out = null, k = 1, opening = null, who = 'posterchan';
  const WHO = /^[a-z]{1,20}$/;   // a dancer's id, passed to her page as #who (validated there too)

  const alive = () => !!(win && !win.isDestroyed());
  const tell = ev => { try{ if(owner && !owner.isDestroyed()) owner.send('pc:buddy:event', ev); }catch(_){ } };

  async function outputOf(sender){
    const scope = scopeOf(sender.id);
    const outs = await wm().outputs();
    const o = (scope && outs.find(x => x && x.name === scope.output)) || outs[0];
    return o && o.rect;
  }
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
  async function open(box, w){
    win = createWindow({
      show: false, frame: false, transparent: true, backgroundColor: '#00000000', hasShadow: false,
      resizable: false, skipTaskbar: true, focusable: false, alwaysOnTop: true, title: TITLE,
      width: box.w, height: box.h,
      webPreferences: { contextIsolation: true, nodeIntegration: false, sandbox: true, preload: preloadPath },
    });
    const mine = win;
    win.on('page-title-updated', e => e.preventDefault());   // the title is how the compositor finds her
    win.on('closed', () => { if(win === mine){ win = null; id = null; at = null; } });
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
    const r = out.rect;
    at.x = Math.round(Math.min(Math.max(r.x, at.x + (Number(dx) || 0) * k), r.x + r.width - at.w));
    at.y = Math.round(Math.min(Math.max(r.y, at.y + (Number(dy) || 0) * k), r.y + r.height - at.h));
    try{ const m = wm().move ? wm().move(id, at.x, at.y) : wm().place(id, at.x, at.y, at.w, at.h); if(m && m.catch) m.catch(() => {}); }catch(_){ }
    return true;
  }
  /* The drag ended: hand the spot back to the owner, in ITS viewport pixels, to be saved. */
  function drop(sender){
    if(!fromHer(sender) || !at || !out) return false;
    tell({ type: 'moved', vx: (at.x - out.rect.x) / out.sx, vy: (at.y - out.rect.y) / out.sy });
    return true;
  }
  function menu(sender, action){
    if(!fromHer(sender)) return false;
    if(String(action) === 'hide'){ tell({ type: 'hide' }); hide(); return true; }
    // The desktop owns who dances: it saves the choice and shows her again as the other one.
    if(String(action) === 'switch'){ tell({ type: 'switch' }); return true; }
    return false;
  }
  /* The owner is gone (its renderer reloaded or closed): she goes with it, and comes back when it
   * mounts her again. */
  function ownerGone(contentsId){ if(owner && owner.id === contentsId){ owner = null; hide(); } }

  return { show, hide, drag, drop, menu, ownerGone, TITLE, _state: () => ({ id, at, k, open: alive(), who }) };
}

module.exports = { createBuddyHost, TITLE };
