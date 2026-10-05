'use strict';
/* NOTIFICATION CARDS ON POSTERCHANOS: THEIR OWN WINDOW, OVER EVERY OTHER ONE.
 *
 * "desktop missed another social notification" / "i am not seeing any notifications for telegram
 * messages". The desktop's toast cards were drawn INSIDE the desktop surface, and on PosterChanOS that
 * surface is kept under every application window (sinkShellSurfaces) -- so with windows covering the
 * bottom-right corner, every card popped up underneath them and was gone before anyone saw it. And
 * osNotify (Telegram, Texts, reminders) went to Electron's native notifications, which need a
 * freedesktop notification server that this OS deliberately does not run (gentoo.sh: "NO NOTIFICATION
 * DAEMON, DELIBERATELY").
 *
 * This is NOT that daemon. It is the same PosterChan card the desktop always drew, in a small
 * transparent window of its own that the compositor keeps ALWAYS ON TOP (exactly like the desktop
 * PosterChan, buddy-host.js), shown inactive and never keeping the keyboard. The bell and the
 * notification centre stay the surface where notifications live; this is only the brief card.
 *
 * The desktop renderer that raised a card owns what clicking it does: the card carries an id, a click
 * comes back here, and this tells that renderer which id. Dependencies are injected so
 * tests/test_desktop_toast_window.py runs this file under node against a fake compositor. */
const TITLE = 'PosterChan Notifications';
const W = 380, CARD = 84, GAP = 10, MAX = 4, MARGIN = 14, TTL = 7000, TASKBAR = 72;

function createToastHost(deps){
  const { wm, scopeOf, pagePath, preloadPath } = deps;
  const createWindow = deps.createWindow || (o => new deps.BrowserWindow(o));
  const sleep = deps.sleep || (ms => new Promise(r => setTimeout(r, ms)));
  const now = deps.now || (() => Date.now());
  const later = deps.setTimeout || setTimeout;
  let win = null, id = null, owner = null, k = 1, opening = null, cards = [], timer = null;

  const alive = () => !!(win && !win.isDestroyed());
  const send = (ch, v) => { try{ if(alive()) win.webContents.send(ch, v); }catch(_){ } };

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
  /* A CARD NEVER TAKES THE KEYBOARD. Wayfire activates every window it maps, shown inactive or not, so
   * whoever had the keyboard gets it straight back -- a notification must not eat the person's typing. */
  async function giveBackFocus(){
    if(id == null) return;
    try{
      const rows = (await wm().windows()).filter(r => r && Number(r.id) !== id && !r.stashed
                                                   && String(r.title || '') !== TITLE);
      rows.sort((a, b) => (Number(b.focusTime) || 0) - (Number(a.focusTime) || 0));
      if(rows[0]) await wm().focus(Number(rows[0].id));
    }catch(_){ }
  }
  async function open(){
    win = createWindow({
      show: false, frame: false, transparent: true, backgroundColor: '#00000000', hasShadow: false,
      resizable: true,   // Wayland pins a non-resizable window to its FIRST size (see buddy-host.js)
      skipTaskbar: true, focusable: false, alwaysOnTop: true, title: TITLE, width: W, height: CARD + 2 * GAP,
      webPreferences: { contextIsolation: true, nodeIntegration: false, sandbox: true, preload: preloadPath },
    });
    const mine = win;
    win.on('page-title-updated', e => e.preventDefault());   // the title is how the compositor finds it
    win.on('closed', () => { if(win === mine){ win = null; id = null; } });
    win.on('focus', () => { if(win === mine) giveBackFocus(); });
    await win.loadFile(pagePath);
    if(!alive() || win !== mine) return false;
    win.showInactive();
    const row = await findRow();
    if(!row || win !== mine) return false;
    id = Number(row.id);
    try{ const b = win.getBounds(); if(b.width > 0 && row.rect && row.rect.width > 0) k = row.rect.width / b.width; }catch(_){ }
    try{ await wm().alwaysOnTop(id, true); }catch(_){ }
    try{ await wm().sticky(id, true); }catch(_){ }
    await giveBackFocus();
    return true;
  }
  async function ensure(){
    if(alive() && id != null){ if(win.isVisible && !win.isVisible()){ win.showInactive(); await giveBackFocus(); } return true; }
    if(!opening) opening = open().finally(() => { opening = null; });
    return opening;
  }
  /* Bottom-right of the monitor the raising desktop is on, ABOVE the taskbar: the work area the desktop
   * publishes when the compositor has it, otherwise the taskbar's usual height. */
  async function place(){
    if(id == null) return;
    const scope = owner ? scopeOf(owner.id) : null;
    const outs = await wm().outputs();
    const o = (scope && outs.find(x => x && x.name === scope.output)) || outs[0];
    if(!o || !o.rect) return;
    const area = o.work ? { x: o.work.x, y: o.work.y, w: o.work.w, h: o.work.h }
                        : { x: o.rect.x, y: o.rect.y, w: o.rect.width, h: Math.max(1, o.rect.height - TASKBAR * k) };
    const n = Math.max(1, cards.length);
    const w = Math.round(W * k), h = Math.round((n * (CARD + GAP) + GAP) * k), m = Math.round(MARGIN * k);
    try{ win.setSize(W, n * (CARD + GAP) + GAP); }catch(_){ }
    await wm().place(id, area.x + area.w - w - m, area.y + area.h - h - m, w, h);
  }
  function arm(){
    if(timer){ try{ clearTimeout(timer); }catch(_){ } timer = null; }
    if(!cards.length) return;
    const next = Math.min(...cards.map(c => c.until)) - now();
    timer = later(() => { timer = null; expire(); }, Math.max(50, next));
  }
  function expire(){
    const t = now();
    const kept = cards.filter(c => c.until > t);
    if(kept.length !== cards.length){ cards = kept; refresh(); }
    else arm();
  }
  async function refresh(){
    if(!cards.length){ send('pc:toast:cards', []); try{ if(alive()) win.hide(); }catch(_){ } arm(); return; }
    send('pc:toast:cards', cards.map(c => ({ id: c.id, html: c.html, pic: c.pic })));
    try{ await place(); }catch(_){ }
    arm();
  }

  /* card = { id, html, pic } from a desktop renderer. Newest at the bottom, at most MAX on screen. */
  async function show(sender, card){
    const c = card && typeof card === 'object' ? card : {};
    const cid = String(c.id || '').slice(0, 64);
    if(!cid) return false;
    owner = sender;
    cards = cards.filter(x => x.id !== cid).concat([{ id: cid, html: String(c.html || '').slice(0, 4000),
                                                     pic: String(c.pic || '').slice(0, 2000), until: now() + TTL, owner: sender }]);
    if(cards.length > MAX) cards = cards.slice(-MAX);
    if(!await ensure()) return false;
    await refresh();
    return true;
  }
  /* From the notification window only: which card was clicked (or dismissed with its ×). */
  function click(sender, cardId, dismissed){
    if(!alive() || !sender || sender.id !== win.webContents.id) return false;
    const c = cards.find(x => x.id === String(cardId));
    if(!c) return false;
    cards = cards.filter(x => x !== c);
    if(!dismissed){ try{ if(c.owner && !c.owner.isDestroyed()) c.owner.send('pc:toast:clicked', c.id); }catch(_){ } }
    refresh();
    giveBackFocus();
    return true;
  }
  function ownerGone(contentsId){
    const before = cards.length;
    cards = cards.filter(x => !(x.owner && x.owner.id === contentsId));
    if(cards.length !== before) refresh();
  }
  return { show, click, ownerGone, TITLE, _state: () => ({ id, cards: cards.map(c => c.id), open: alive() }) };
}

module.exports = { createToastHost, TITLE };
