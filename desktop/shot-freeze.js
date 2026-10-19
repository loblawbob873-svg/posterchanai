'use strict';
/* THE FROZEN SCREEN A REGION SCREENSHOT IS CHOSEN ON.
 *
 * "Screen Shot on PosterChanOS -> take the screenshot then let the user choose the area ... It's hard
 * to take a selection when the screen is moving." screenshot.js now captures the whole screen FIRST;
 * this lays that still picture over every monitor, so `slurp` (a layer-shell overlay, drawn above
 * every window including these) is dragged over a screen that has stopped moving.
 *
 * One window per output, each showing ITS part of grim's whole-layout picture. Placed by the
 * compositor (Wayland gives a client no say in where its window goes) at the output's logical
 * rectangle, kept on top, never focusable. The image is positioned in VIEWPORT units, so it lines up
 * whatever device scale Chromium picked for that output -- the window is exactly one output, so
 * 100vw IS that output's logical width.
 *
 * A window maps where Wayfire likes (the middle of the screen) before it can be moved, so the page
 * starts fully transparent and is revealed only once it sits on its output: a frozen picture flashing
 * up in the wrong place would be worse than none.
 *
 * `hide()` destroys every window and deletes the pages it wrote, and it is safe to call at any point,
 * including while windows are still opening -- a cancel must leave nothing on the screen.
 *
 * Dependencies are injected so tests/test_screenshot_freeze_first.py runs this under node against a
 * fake compositor. Our app_id keeps these off the taskbar and out of window adoption (osshell.js
 * taskbarRows only lists "PosterChan Window" titles); the title is how the compositor finds each one. */
const fs = require('fs');
const path = require('path');
const { pathToFileURL } = require('url');

const TITLE = 'PosterChan Screenshot Freeze';

function _box(layout) {
  const outs = layout.filter(r => r && Number(r.width) > 0 && Number(r.height) > 0);
  if (!outs.length) return null;
  const x0 = Math.min(...outs.map(r => Number(r.x) || 0)), y0 = Math.min(...outs.map(r => Number(r.y) || 0));
  return { x: x0, y: y0,
           width: Math.max(...outs.map(r => (Number(r.x) || 0) + Number(r.width))) - x0,
           height: Math.max(...outs.map(r => (Number(r.y) || 0) + Number(r.height))) - y0 };
}

/** The page for ONE output: the whole picture, offset so this output's part fills the viewport. */
function pageHtml(src, out, box, title) {
  const pct = (v) => (Math.round(v * 1e6) / 1e6) + '';
  const left = -((Number(out.x) || 0) - box.x) / out.width * 100, top = -((Number(out.y) || 0) - box.y) / out.height * 100;
  const w = box.width / out.width * 100, h = box.height / out.height * 100;
  const url = pathToFileURL(src).href.replace(/"/g, '%22');
  const t = String(title).replace(/[<&>]/g, '');
  return '<!doctype html><html><head><meta charset="utf-8"><title>' + t + '</title><style>'
    + 'html,body{margin:0;width:100%;height:100%;overflow:hidden;background:transparent;cursor:crosshair}'
    + 'img{position:fixed;left:' + pct(left) + 'vw;top:' + pct(top) + 'vh;width:' + pct(w) + 'vw;height:' + pct(h) + 'vh;'
    + 'opacity:0;pointer-events:none;user-select:none;image-rendering:auto}'
    + 'html.on body{background:#000}html.on img{opacity:1}'
    + '</style></head><body><img src="' + url + '" alt=""></body></html>';
}

function createFreezeHost(deps) {
  const { wm } = deps;
  const createWindow = deps.createWindow || (o => new deps.BrowserWindow(o));
  const sleep = deps.sleep || (ms => new Promise(r => setTimeout(r, ms)));
  const settleMs = deps.settleMs == null ? 90 : deps.settleMs;

  async function findRow(title, alive) {
    for (let i = 0; i < 40; i++) {
      if (!alive()) return null;
      try {
        const row = (await wm().windows()).find(x => String(x.title || '') === title);
        if (row) return row;
      } catch (_) { }
      await sleep(40);
    }
    return null;
  }

  /** Show `src` (grim's whole-layout PNG) over every output in `layout`; resolves to hide(). */
  async function show(src, layout) {
    const outs = (Array.isArray(layout) ? layout : []).filter(r => r && Number(r.width) > 0 && Number(r.height) > 0);
    const box = outs.length ? _box(outs) : null;
    const wins = [], pages = [];
    let gone = false;
    const hide = async () => {
      gone = true;
      for (const w of wins.splice(0)) { try { if (!w.isDestroyed()) w.destroy(); } catch (_) { } }
      for (const f of pages.splice(0)) { try { fs.unlinkSync(f); } catch (_) { } }
    };
    if (!box || !src) return hide;
    const stem = path.join(path.dirname(src), path.basename(src, '.png'));
    await Promise.all(outs.map(async (out, i) => {
      const title = TITLE + ' ' + (i + 1);
      const page = stem + '-freeze-' + (i + 1) + '.html';
      try {
        fs.writeFileSync(page, pageHtml(src, out, box, title), { mode: 0o600 });
        pages.push(page);
        if (gone) return;
        const win = createWindow({
          show: false, frame: false, transparent: true, backgroundColor: '#00000000', hasShadow: false,
          resizable: true, skipTaskbar: true, focusable: false, alwaysOnTop: true, title,
          width: Math.round(out.width), height: Math.round(out.height),
          webPreferences: { contextIsolation: true, nodeIntegration: false, sandbox: true },
        });
        wins.push(win);
        const alive = () => !gone && !win.isDestroyed();
        win.on('page-title-updated', e => e.preventDefault());
        await win.loadFile(page);
        if (!alive()) return;
        win.showInactive();
        const row = await findRow(title, alive);
        if (!row || !alive()) return;          // never placed: it stays transparent, i.e. invisible
        await wm().place(Number(row.id), Number(out.x) || 0, Number(out.y) || 0, Number(out.width), Number(out.height));
        try { await wm().alwaysOnTop(Number(row.id), true); } catch (_) { }
        if (!alive()) return;
        await win.webContents.executeJavaScript("document.documentElement.classList.add('on')");
      } catch (_) { /* one output without its still picture is a live one, not a failure */ }
    }));
    if (!gone) await sleep(settleMs);      // let the revealed frames reach the screen before slurp draws
    return hide;
  }

  return { show, TITLE };
}

module.exports = { createFreezeHost, pageHtml, TITLE };
