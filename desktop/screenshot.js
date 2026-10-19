/* Screenshots for the PosterChanOS shell.
 *
 * WHY THIS IS A SUBPROCESS AND NOT `webContents.capturePage()`. The shell IS the desktop, so the
 * obvious thing — ask Electron for a picture of its own window — looks right and is wrong the
 * moment anything native is on screen. A Linux app in a PosterChan window is a real compositor
 * surface held over a HOLE in our page (see `.osw-native .osw-body`), so it is not in our window's
 * pixels at all: capturePage() returns the desktop with a black rectangle where Firefox was. The
 * one thing a person screenshots most is the app they are looking at.
 *
 * `desktopCapturer` is the other candidate and is worse here: on wlroots it goes through the
 * xdg-desktop-portal screencast path, which puts a "share your screen?" dialog in front of every
 * single screenshot. That is a permission prompt answering a question nobody asked.
 *
 * `grim` reads the compositor's own output — everything on the screen, native surfaces included,
 * with no dialog. `slurp` is how a region is chosen. Both are wlroots tools, which is exactly the
 * compositor this OS ships.
 *
 * IT SAYS SO WHEN IT CANNOT. A screenshot key that does nothing is the worst version of this
 * feature, because the failure is invisible: you press it, nothing appears to happen, and you have
 * no way to tell a missing tool from a missing file from a picture that saved silently somewhere
 * you did not look. Every path here returns `{ ok, path, why }` and the caller shows `why`.
 */
'use strict';
const { execFile } = require('child_process');
const fs = require('fs');
const os = require('os');
const path = require('path');

const GRIM = process.env.PC_GRIM || 'grim';
const SLURP = process.env.PC_SLURP || 'slurp';

/* grim 1.5's ext-image-copy path waits for the next damaged compositor frame, and a completely
 * static Sway output (two full-output shell surfaces) could fail to produce one -- so this nudged
 * the cursor plane with `swaymsg` to force a frame. That command does not exist any more, and
 * measured on the real Wayfire desktop it is not needed: a capture of a completely idle two-monitor
 * session returns in ~0.5s. Kept as a no-op function so the call sites and their ordering comments
 * still read correctly, and so re-adding a wake is one place rather than three. */
function wakeCompositor() {
  return;
}

function run(bin, args, opts) {
  const o = opts || {};
  return new Promise((resolve, reject) => {
    const child = execFile(bin, args, { timeout: o.timeout || 15000, maxBuffer: 4 * 1024 * 1024 },
      (err, stdout, stderr) => {
        if (err) {
          /* ENOENT is the one failure worth naming differently: "grim is not installed on this
           * machine" is an instruction, where "Command failed" is a shrug. */
          if (err.code === 'ENOENT') return reject(Object.assign(new Error(bin + ' is not installed'),
                                                                 { missing: bin }));
          return reject(Object.assign(new Error(String(stderr || err.message || err).trim().split('\n').pop()),
                                      { stderr: String(stderr || '').trim() }));
        }
        resolve(String(stdout || ''));
      });
    /* CLOSE STDIN. slurp, given a stdin that is not a terminal, first reads a list of predefined
     * boxes from it — and execFile hands every child an open pipe, so the region picker sat reading
     * a pipe nobody would close, never connected to the compositor, and drew nothing: "Select" did
     * nothing, and each press left another slurp waiting. None of these tools take input on stdin. */
    try { if (child && child.stdin) child.stdin.end(); } catch (_) { }
  });
}

const has = (bin) => run(bin, ['--help'], { timeout: 4000 })
  .then(() => true, (e) => !e.missing);   // a tool that ran and complained is still installed

/** Can this machine take a screenshot at all, and can it take a REGION one? */
async function available() {
  const [grim, slurp] = await Promise.all([has(GRIM), has(SLURP)]);
  return { ok: !!grim, region: !!(grim && slurp),
           why: grim ? '' : 'screenshots need grim (gui-apps/grim), which is not installed' };
}

/* WHERE A SCREENSHOT GOES. `~/Pictures/Screenshots`, which is where every other desktop puts them
 * and — the part that matters here — is inside the folder a PosterChan drive sync is most likely
 * to already be watching. The name sorts chronologically, because a folder of screenshots is only
 * ever read in date order. */
function shotDir() {
  const home = os.homedir();
  return path.join(home, 'Pictures', 'Screenshots');
}

function shotName(now) {
  const d = now || new Date();
  const p = (n) => String(n).padStart(2, '0');
  return 'PosterChan-' + d.getFullYear() + '-' + p(d.getMonth() + 1) + '-' + p(d.getDate())
       + '-' + p(d.getHours()) + p(d.getMinutes()) + p(d.getSeconds()) + '.png';
}

/** `slurp`'s answer, validated. An empty selection means the person pressed Escape — not an error. */
function parseGeometry(out) {
  const s = String(out || '').trim();
  if (!s) return null;
  return /^-?\d+,-?\d+ \d+x\d+$/.test(s) ? s : null;
}

/**
 * Take one.
 *   mode  'screen' (default) · 'region' (drag a box) · 'area' with an explicit `geometry`
 *   copy  ignored here — see the note at the end of this function; main.js does the clipboard
 *
 * Returns { ok, path, copied, why, cancelled }.
 */
async function capture(opts) {
  const o = opts || {};
  const mode = o.mode === 'region' || o.mode === 'area' ? o.mode : 'screen';
  /* A REGION IS CHOSEN ON A STILL PICTURE when the caller can show one and crop one (main.js passes
   * both) -- see captureFrozenRegion. Without them it is the live picker below, as it always was. */
  if (mode === 'region' && Array.isArray(o.layout) && o.layout.length && typeof o.crop === 'function')
    return captureFrozenRegion(o);

  let geometry = null;
  if (mode === 'region') {
    /* THE PICKER RUNS FIRST AND CAN BE CANCELLED, and a cancel is not a failure. `slurp` exits
     * nonzero with nothing on stdout when the user presses Escape; reported as an error that is a
     * toast saying something went wrong every time somebody changes their mind. */
    try { geometry = parseGeometry(await run(SLURP, [], { timeout: 120000 })); }
    catch (e) {
      if (e.missing) return { ok: false, why: 'selecting a region needs slurp (gui-apps/slurp), which is not installed' };
      /* ONLY slurp's own "selection cancelled" is a change of mind. Every other failure (it could not
       * reach the compositor, the output went away, it was killed) used to be read as Escape and
       * swallowed, which on screen is a button that does nothing. */
      const why = String((e && e.stderr) || '').trim().split('\n').pop();   // slurp's own words, never execFile's
      if (!why || /cancel/i.test(why)) return { ok: false, cancelled: true, why: '' };
      return { ok: false, why: 'could not select a region: ' + why };
    }
    if (!geometry) return { ok: false, cancelled: true, why: '' };
  } else if (mode === 'area') {
    geometry = parseGeometry(o.geometry);
    if (!geometry) return { ok: false, why: 'that is not a screen area' };
  }

  const dir = o.stage ? stageDir() : shotDir();
  const file = path.join(dir, shotName());
  try { fs.mkdirSync(dir, { recursive: true }); }
  catch (e) { return { ok: false, why: 'could not make ' + dir + ': ' + ((e && e.message) || e) }; }

  const args = [];
  if (geometry) args.push('-g', geometry);
  args.push(file);
  try {
    const shot = run(GRIM, args, { timeout: 20000 });
    wakeCompositor();
    await shot;
  }
  catch (e) {
    if (e.missing) return { ok: false, why: 'screenshots need grim (gui-apps/grim), which is not installed' };
    return { ok: false, why: (e && e.message) || String(e) };
  }

  /* THE FILE IS THE RESULT, so it is checked. grim can exit 0 having written nothing when the
   * compositor refuses the capture, and a toast reading "saved to ~/Pictures/Screenshots/…" that
   * names a file which is not there is worse than an error. */
  let size = 0;
  try { size = fs.statSync(file).size; } catch (_) { size = 0; }
  if (!size) return { ok: false, why: 'the screenshot came back empty' };

  /* THE CLIPBOARD IS DELIBERATELY NOT DONE HERE, AND `wl-copy` COST A WHOLE AFTERNOON TO LEARN WHY.
   *
   * `wl-copy` does not exit. It forks a daemon that stays resident serving the clipboard offer
   * until something else takes the selection — and that daemon INHERITS ITS PARENT'S OPEN FILE
   * DESCRIPTORS. The parent here is the Electron shell, i.e. the whole desktop. Measured on the
   * test machine, one screenshot left a `wl-copy -t image/png` holding NINETY-FIVE descriptors,
   * THIRTEEN of them sockets, including the shell's own listening socket.
   *
   * The visible symptom was a port that could never be bound again: the shell was restarted, its
   * listener was still held open by a clipboard process from a screenshot taken twenty minutes
   * earlier, and every connection to it queued for ever against a socket nothing was accepting on.
   * `ss -ltnp` names the owner and that is the only reason it was found — from the outside it looks
   * exactly like a hung app.
   *
   * So the copy is Electron's own `clipboard.writeImage`, done by the caller in main.js: same
   * clipboard, no subprocess, nothing to inherit. It also keeps this module free of an electron
   * import, which is what lets tests/test_desktop_screenshot.py run it under plain node.
   */
  return { ok: true, path: file, bytes: size, copied: false, dir, staged: !!o.stage };
}

/* STAGING: PRINT SCREEN ASKS WHAT TO DO, AND THE PICTURE IS TAKEN BEFORE IT ASKS.
 *
 * The key now opens a prompt (Save to Pictures · Select region · Copy · Cancel). The capture has to
 * happen FIRST -- a prompt drawn on screen and then photographed is a screenshot of the prompt --
 * so the whole screen is written here, to a private staging folder, and only a Save moves it into
 * ~/Pictures/Screenshots. Cancel (or a region instead) deletes it: nothing the person turned down
 * is left lying in their Pictures.
 *
 * `commit` and `discard` take a path from the RENDERER, so each accepts only a file that is
 * directly inside the staging folder under our own naming. Anything else -- `../`, a symlink out,
 * somebody's document -- is refused, never moved or deleted. */
function stageDir() {
  return path.join(os.tmpdir(), 'posterchan-shots-' + (process.getuid ? process.getuid() : 'u'));
}
function _staged(p) {
  const f = path.resolve(String(p || ''));
  if (path.dirname(f) !== path.resolve(stageDir())) return null;
  if (!/^PosterChan-\d{4}-\d{2}-\d{2}-\d{6}\.png$/.test(path.basename(f))) return null;
  try { if (!fs.lstatSync(f).isFile()) return null; } catch (_) { return null; }
  return f;
}
function commit(p) {
  const f = _staged(p);
  if (!f) return { ok: false, why: 'that screenshot is gone — take it again' };
  const dir = shotDir();
  try { fs.mkdirSync(dir, { recursive: true }); }
  catch (e) { return { ok: false, why: 'could not make ' + dir + ': ' + ((e && e.message) || e) }; }
  let dest = path.join(dir, path.basename(f));
  for (let n = 2; fs.existsSync(dest); n++) dest = path.join(dir, path.basename(f, '.png') + '-' + n + '.png');
  try { fs.renameSync(f, dest); }
  catch (_) {
    // A tmpfs /tmp and a disk /home are different filesystems: rename cannot cross them.
    try { fs.copyFileSync(f, dest); fs.unlinkSync(f); }
    catch (e) { return { ok: false, why: (e && e.message) || String(e) }; }
  }
  let size = 0; try { size = fs.statSync(dest).size; } catch (_) { size = 0; }
  if (!size) return { ok: false, why: 'the screenshot came back empty' };
  return { ok: true, path: dest, bytes: size, dir, copied: false };
}
function discard(p) {
  const f = _staged(p);
  if (!f) return { ok: false };
  try { fs.unlinkSync(f); return { ok: true }; } catch (_) { return { ok: false }; }
}

/* IS THERE ACTUALLY AN IMAGE ON THE CLIPBOARD? Asked of a real Wayland client, because nothing
 * else here can answer it honestly.
 *
 * Electron's `clipboard.writeImage()` is the natural way to do the copy and on this compositor it
 * DOES NOT TAKE THE SELECTION — measured: the call returned without error, `clipboard.readImage()`
 * came back non-empty (Chromium caches its own write, so the readback agrees with itself and proves
 * nothing), and `wl-paste --list-types` in another client answered "Nothing is copied". A toast
 * saying "· copied" on the strength of that sends somebody to paste a screenshot into a chat and
 * post whatever was on their clipboard before.
 *
 * `wl-paste` EXITS, which is the whole reason it is allowed here where `wl-copy` is not: a child of
 * this process inherits Chromium's non-CLOEXEC descriptors, so a short-lived one is harmless and a
 * daemon is a leak that outlives the app (see the note in capture()).
 *
 * A machine with no wl-paste answers "I do not know", which is reported as NOT copied — the safe
 * direction, because the cost of the two mistakes is not symmetric. */
async function clipboardHasImage() {
  try {
    const out = await run(process.env.PC_WLPASTE || 'wl-paste', ['--list-types'], { timeout: 6000 });
    return /image\/(png|bmp)/i.test(out);
  } catch (_) { return false; }
}

/* FREEZE FIRST, THEN CHOOSE. "Screen Shot on PosterChanOS -> take the screenshot then let the user
 * choose the area ... It's hard to take a selection when the screen is moving."
 *
 * The region used to be picked with `slurp` over the LIVE screen and photographed afterwards with
 * `grim -g`: with a video or an animation playing, the thing being framed moved under the mouse, and
 * the picture was of the moment the button was RELEASED, not the moment the key was pressed. Now the
 * whole screen (every output) is captured FIRST, that still picture is laid over each monitor
 * (`o.showFrozen`, desktop/shot-freeze.js), slurp draws its rectangle over the still, and the
 * rectangle is cut out of the picture already taken. There is no second capture -- a second capture
 * would photograph the overlay, or the screen as it is now, which is the bug.
 *
 * `o.frozen` reuses a frame the Print Screen prompt already staged (the moment of the keypress).
 *
 * THE ARITHMETIC (frozenCrop). slurp answers in LOGICAL layout coordinates; grim's whole-layout
 * picture is the layout's bounding box at ONE scale (the highest output's), with its origin at the
 * box's top-left -- which can be negative (a monitor left of the primary). The scale is MEASURED as
 * picture size / logical box size rather than read from the outputs, because what grim chose for a
 * fractional scale is grim's business; if the two axes disagree, the layout we were handed is not the
 * one grim photographed, and guessing would cut out the wrong part of somebody's screen -- so that
 * case falls back to the live `grim -g` capture of the chosen rectangle, which is at least the
 * rectangle they drew. */
function pngSize(file) {
  try {
    const fd = fs.openSync(file, 'r');
    const b = Buffer.alloc(24);
    try { fs.readSync(fd, b, 0, 24, 0); } finally { fs.closeSync(fd); }
    if (b.readUInt32BE(0) !== 0x89504e47 || b.toString('latin1', 12, 16) !== 'IHDR') return null;
    const w = b.readUInt32BE(16), h = b.readUInt32BE(20);
    return w > 0 && h > 0 ? { width: w, height: h } : null;
  } catch (_) { return null; }
}

function layoutBox(layout) {
  const outs = (Array.isArray(layout) ? layout : []).filter(r => r && Number(r.width) > 0 && Number(r.height) > 0);
  if (!outs.length) return null;
  const x0 = Math.min(...outs.map(r => Number(r.x) || 0)), y0 = Math.min(...outs.map(r => Number(r.y) || 0));
  const x1 = Math.max(...outs.map(r => (Number(r.x) || 0) + Number(r.width)));
  const y1 = Math.max(...outs.map(r => (Number(r.y) || 0) + Number(r.height)));
  return { x: x0, y: y0, width: x1 - x0, height: y1 - y0 };
}

/** slurp's logical "x,y wxh" -> the pixel rectangle inside grim's whole-layout picture, or null. */
function frozenCrop(geometry, layout, img) {
  const m = /^(-?\d+),(-?\d+) (\d+)x(\d+)$/.exec(String(geometry || '').trim());
  const box = layoutBox(layout);
  if (!m || !box || !img || !(img.width > 0) || !(img.height > 0)) return null;
  const sx = img.width / box.width, sy = img.height / box.height;
  if (Math.abs(sx - sy) > 0.02 * Math.max(sx, sy)) return null;
  const gx = Number(m[1]), gy = Number(m[2]), gw = Number(m[3]), gh = Number(m[4]);
  const clamp = (v, hi) => Math.max(0, Math.min(hi, v));
  const x0 = clamp(Math.round((gx - box.x) * sx), img.width), y0 = clamp(Math.round((gy - box.y) * sy), img.height);
  const x1 = clamp(Math.round((gx + gw - box.x) * sx), img.width), y1 = clamp(Math.round((gy + gh - box.y) * sy), img.height);
  if (x1 <= x0 || y1 <= y0) return null;
  return { x: x0, y: y0, width: x1 - x0, height: y1 - y0 };
}

async function captureFrozenRegion(o) {
  const unlink = (f) => { try { fs.unlinkSync(f); } catch (_) { } };
  /* 1. THE FRAME, before anything is drawn and before anything is chosen. */
  let src = o.frozen ? _staged(o.frozen) : null;
  if (!src) {
    const sdir = stageDir();
    try { fs.mkdirSync(sdir, { recursive: true }); }
    catch (e) { return { ok: false, why: 'could not make ' + sdir + ': ' + ((e && e.message) || e) }; }
    src = path.join(sdir, 'PosterChan-freeze-' + Date.now() + '-' + process.pid + '.png');
    try { await run(GRIM, [src], { timeout: 20000 }); }
    catch (e) {
      unlink(src);
      if (e.missing) return { ok: false, why: 'screenshots need grim (gui-apps/grim), which is not installed' };
      return { ok: false, why: (e && e.message) || String(e) };
    }
  }
  const img = pngSize(src);
  if (!img) { unlink(src); return { ok: false, why: 'the screenshot came back empty' }; }

  /* 2. THE STILL PICTURE OVER EVERY MONITOR, then the picker over it. The overlay is taken down
   * whatever slurp says -- a cancel must leave nothing on the screen. An overlay that could not be
   * shown is not a reason to refuse: the rectangle still comes out of the frozen frame. */
  let hide = null, geometry = null, failed = null;
  try { if (typeof o.showFrozen === 'function') hide = await o.showFrozen(src, o.layout); }
  catch (_) { hide = null; }
  try { geometry = parseGeometry(await run(SLURP, [], { timeout: 120000 })); }
  catch (e) { failed = e; }
  finally { if (typeof hide === 'function') { try { await hide(); } catch (_) { } } }
  if (failed) {
    unlink(src);
    if (failed.missing) return { ok: false, why: 'selecting a region needs slurp (gui-apps/slurp), which is not installed' };
    const why = String((failed && failed.stderr) || '').trim().split('\n').pop();
    if (!why || /cancel/i.test(why)) return { ok: false, cancelled: true, why: '' };
    return { ok: false, why: 'could not select a region: ' + why };
  }
  if (!geometry) { unlink(src); return { ok: false, cancelled: true, why: '' }; }

  /* 3. CUT IT OUT of the frame already taken. */
  const dir = o.stage ? stageDir() : shotDir();
  const file = path.join(dir, shotName());
  try { fs.mkdirSync(dir, { recursive: true }); }
  catch (e) { unlink(src); return { ok: false, why: 'could not make ' + dir + ': ' + ((e && e.message) || e) }; }
  const rect = frozenCrop(geometry, o.layout, img);
  let frozen = false;
  if (rect) {
    try { await o.crop(src, rect, file); frozen = true; }
    catch (_) { unlink(file); frozen = false; }
  }
  unlink(src);
  if (!frozen) {
    try { await run(GRIM, ['-g', geometry, file], { timeout: 20000 }); }
    catch (e) {
      if (e.missing) return { ok: false, why: 'screenshots need grim (gui-apps/grim), which is not installed' };
      return { ok: false, why: (e && e.message) || String(e) };
    }
  }
  let size = 0;
  try { size = fs.statSync(file).size; } catch (_) { size = 0; }
  if (!size) { unlink(file); return { ok: false, why: 'the screenshot came back empty' }; }
  return { ok: true, path: file, bytes: size, copied: false, dir, staged: !!o.stage, frozen };
}

module.exports = { available, capture, commit, discard, stageDir, clipboardHasImage, shotDir, shotName, parseGeometry, has,
                   frozenCrop, layoutBox, pngSize };
