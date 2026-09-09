/* Native clipboard interop for PosterChanOS.
 *
 * Electron's Wayland clipboard can accept writeText/readText while never publishing or observing
 * the compositor selection.  That makes copy/paste appear to work inside PosterChan and fail in
 * Firefox, Telegram, and every other native client.  wl-clipboard is part of the OS image, so use
 * it as the compositor-facing bridge on Wayland and keep Electron as the portable fallback.
 *
 * The same is true of IMAGES, and there it was not a degraded copy but no copy at all: the lightbox's
 * "Copy image" had only navigator.clipboard.write, which the app:// origin refuses, so on this desktop
 * every attempt ended in a toast telling a mouse user to long-press. clipboard.writeImage would not
 * have fixed it either — it fills Chromium's own cache and never takes the selection, i.e. the exact
 * inside-works/outside-fails split the text path exists to avoid.
 *
 * wl-copy forks a selection-owning daemon. Chromium can leave descriptors inheritable despite
 * Node's stdio options, so close unrelated descriptors BEFORE exec wl-copy. Its daemon must not
 * keep a dead desktop's CDP/HTTP listening sockets or instance locks alive.
 */
'use strict';

const { execFile, spawn } = require('child_process');

const isWayland = () => process.platform === 'linux' && !!process.env.WAYLAND_DISPLAY;

/* ONE wl-copy invocation for every MIME type we publish, so a new payload kind cannot arrive with
 * its own spawn() and quietly skip the descriptor-closing wrapper below. `payload` is whatever
 * stdin should carry: a string for text, a Buffer for bytes. */
function writeWaylandSelection(mime, payload, deps) {
  if (!isWayland()) return Promise.resolve(false);
  const start = (deps && deps.spawn) || spawn;
  const bin = process.env.PC_WLCOPY || 'wl-copy';
  return new Promise((resolve) => {
    let settled = false;
    let child;
    try {
      // Bash supports high-numbered descriptors; /bin/sh may be dash, which does not.
      // No redirection on the loop: saving stderr at a high FD would get that copy closed too.
      const closeInherited =
        'for __fd in /proc/$$/fd/[0-9]*; do __n=${__fd##*/}; ' +
        'case "$__n" in \'\'|*[!0-9]*) continue;; esac; ' +
        '[ "$__n" -le 2 ] || eval "exec $__n>&-"; done; unset __fd __n';
      child = start('/bin/bash', ['-c', `${closeInherited}; exec "$@"`,
        'posterchan-clipboard', bin, '--type', mime], {
        stdio: ['pipe', 'ignore', 'ignore'],
        windowsHide: true,
      });
    } catch (_) { return resolve(false); }
    const done = (ok) => { if (!settled) { settled = true; resolve(!!ok); } };
    const timer = setTimeout(() => { try { child.kill(); } catch (_) {} done(false); }, 4000);
    if (timer.unref) timer.unref();
    child.once('error', () => { clearTimeout(timer); done(false); });
    child.once('exit', (code) => { clearTimeout(timer); done(code === 0); });
    /* spawn() succeeding does not mean wl-copy stayed alive long enough to consume stdin. A missing
     * compositor or rejected MIME offer can close the pipe first; that EPIPE is emitted on stdin,
     * not on the child process, and without a listener it is an uncaught exception in Electron's
     * main process. Report the copy as failed through the existing fallback contract. */
    if(child.stdin&&child.stdin.once)child.stdin.once('error',()=>{clearTimeout(timer);done(false);});
    try { child.stdin.end(payload); }
    catch (_) { clearTimeout(timer); done(false); }
  });
}

/* Offer the baseline type understood by both GTK Firefox and Qt Telegram.  Wayland MIME
 * offers are exact strings: advertising only text/plain;charset=utf-8 leaves clients that
 * enumerate text/plain with no compatible-looking target, even though the bytes are UTF-8. */
function writeWaylandText(text, deps) {
  return writeWaylandSelection('text/plain', String(text), deps);
}

/* image/png for the same reason text/plain is the text offer: it is the one image target every
 * Wayland paste target enumerates (GTK, Qt, Chromium, Firefox all accept it), and the caller has
 * already converted whatever the post held into PNG. An empty buffer is refused here rather than
 * handed to wl-copy, which would take the selection and then offer nothing — a copy that reports
 * success and pastes an empty image is worse than one that says it failed. */
function writeWaylandImage(png, deps) {
  const bytes = Buffer.isBuffer(png) ? png : Buffer.from(png || []);
  if (!bytes.length) return Promise.resolve(false);
  return writeWaylandSelection('image/png', bytes, deps);
}

function readWaylandText(deps) {
  if (!isWayland()) return Promise.resolve(null);
  const run = (deps && deps.execFile) || execFile;
  const bin = process.env.PC_WLPASTE || 'wl-paste';
  return new Promise((resolve) => {
    /* `text` is wl-paste's generic selector.  Native clients variously advertise text/plain,
     * text/plain;charset=utf-8, UTF8_STRING, or several of them; requesting exact text/plain made
     * Firefox/Telegram copies disappear whenever that exact spelling was absent. */
    run(bin, ['--no-newline', '--type', 'text'], {
      timeout: 3000,
      maxBuffer: 65536,
      windowsHide: true,
    }, (err, stdout) => resolve(err ? null : String(stdout || '').slice(0, 65536)));
  });
}

module.exports = { isWayland, writeWaylandText, writeWaylandImage, readWaylandText };
