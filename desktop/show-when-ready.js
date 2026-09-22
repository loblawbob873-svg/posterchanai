'use strict';
/* A HIDDEN WINDOW'S `ready-to-show` IS NOT A PROMISE, IT IS A RACE — and on a slow machine it is lost.
 *
 * Every flyout (Start, notifications, tray, network) is created `show: false` and used to be shown
 * ONLY from `ready-to-show`. MEASURED in the PosterChanOS VM (QEMU bochs-drm, no GPU, Wayland): a
 * hidden BrowserWindow gets `ready-to-show` only if its page paints within roughly 100ms of the
 * window being created. A data: page whose <head> busy-waits 30ms or 60ms got it at 49/83ms; 120ms or
 * 250ms never got it at all, and neither did the real client page (~130ms to first paint) — with or
 * without the preload, transparent or opaque. After that the renderer produces no frames for a
 * surface that was never mapped (requestAnimationFrame inside the live popup did not fire in 2s), so
 * nothing ever paints, so `ready-to-show` never comes, so `show()` is never called and placement
 * never runs. The menu page loaded, drew its whole app list into a window that did not exist, and
 * preload's floor "revealed" it 1.5s later into nothing. Reported as "in VM, start menu not working";
 * the same build worked on a laptop that simply paints faster.
 *
 * So `ready-to-show` stays the FAST path, and the load finishing is the floor: once the document
 * has loaded, the window is shown after a short grace whether or not the renderer managed its first
 * frame, and an absolute ceiling covers a load that is itself slow. Showing a popup before its first
 * paint is safe — it is `transparent` and preload holds the page at opacity 0 until it is placed —
 * and it is what lets the renderer produce that first frame at all.
 *
 * `show` is called at most once, and never for a destroyed window. `stillWanted()` lets the caller
 * decline a window it has already replaced. */
function showWhenReady(win, show, opts = {}){
  const graceMs = Number.isFinite(opts.graceMs) ? opts.graceMs : 150;
  const ceilingMs = Number.isFinite(opts.ceilingMs) ? opts.ceilingMs : 1000;
  const stillWanted = typeof opts.stillWanted === 'function' ? opts.stillWanted : () => true;
  const setT = opts.setTimeout || setTimeout, clearT = opts.clearTimeout || clearTimeout;
  let done = false, grace = null, ceiling = null;
  const fire = (why) => {
    if(done) return;
    done = true;
    if(grace) clearT(grace);
    if(ceiling) clearT(ceiling);
    let alive = false;
    try{ alive = !win.isDestroyed() && stillWanted(); }catch(_){ alive = false; }
    if(!alive) return;
    try{ show(why); }catch(_){ /* a show that throws must not take the open path with it */ }
  };
  win.once('ready-to-show', () => fire('ready-to-show'));
  try{
    win.webContents.once('did-finish-load', () => {
      if(!done) grace = setT(() => fire('loaded without a first paint'), graceMs);
    });
  }catch(_){ /* no webContents: the ceiling still stands */ }
  ceiling = setT(() => fire('no first paint within ' + ceilingMs + 'ms'), ceilingMs);
  win.once('closed', () => { done = true; if(grace) clearT(grace); if(ceiling) clearT(ceiling); });
  return () => fire('asked');
}

module.exports = { showWhenReady };
