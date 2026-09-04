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
    execFile(bin, args, { timeout: o.timeout || 15000, maxBuffer: 4 * 1024 * 1024 },
      (err, stdout, stderr) => {
        if (err) {
          /* ENOENT is the one failure worth naming differently: "grim is not installed on this
           * machine" is an instruction, where "Command failed" is a shrug. */
          if (err.code === 'ENOENT') return reject(Object.assign(new Error(bin + ' is not installed'),
                                                                 { missing: bin }));
          return reject(new Error(String(stderr || err.message || err).trim().split('\n').pop()));
        }
        resolve(String(stdout || ''));
      });
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

  let geometry = null;
  if (mode === 'region') {
    /* THE PICKER RUNS FIRST AND CAN BE CANCELLED, and a cancel is not a failure. `slurp` exits
     * nonzero with nothing on stdout when the user presses Escape; reported as an error that is a
     * toast saying something went wrong every time somebody changes their mind. */
    try { geometry = parseGeometry(await run(SLURP, [], { timeout: 120000 })); }
    catch (e) {
      if (e.missing) return { ok: false, why: 'selecting a region needs slurp (gui-apps/slurp), which is not installed' };
      return { ok: false, cancelled: true, why: '' };
    }
    if (!geometry) return { ok: false, cancelled: true, why: '' };
  } else if (mode === 'area') {
    geometry = parseGeometry(o.geometry);
    if (!geometry) return { ok: false, why: 'that is not a screen area' };
  }

  const dir = shotDir();
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
  return { ok: true, path: file, bytes: size, copied: false, dir };
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

module.exports = { available, capture, clipboardHasImage, shotDir, shotName, parseGeometry, has };
