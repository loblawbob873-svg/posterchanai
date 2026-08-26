/* Native clipboard interop for PosterChanOS.
 *
 * Electron's Wayland clipboard can accept writeText/readText while never publishing or observing
 * the compositor selection.  That makes copy/paste appear to work inside PosterChan and fail in
 * Firefox, Telegram, and every other native client.  wl-clipboard is part of the OS image, so use
 * it as the compositor-facing bridge on Wayland and keep Electron as the portable fallback.
 *
 * wl-copy normally forks a selection-owning daemon.  Spawn is given ONLY stdin/out/err below;
 * Node marks every other descriptor close-on-exec, so the daemon cannot inherit Electron's HTTP,
 * CDP, or instance-lock sockets.  This is deliberately unlike the old screenshot implementation
 * that leaked the desktop's listening descriptors into a long-lived wl-copy process.
 */
'use strict';

const { execFile, spawn } = require('child_process');

const isWayland = () => process.platform === 'linux' && !!process.env.WAYLAND_DISPLAY;

function writeWaylandText(text, deps) {
  if (!isWayland()) return Promise.resolve(false);
  const start = (deps && deps.spawn) || spawn;
  const bin = process.env.PC_WLCOPY || 'wl-copy';
  return new Promise((resolve) => {
    let settled = false;
    let child;
    try {
      child = start(bin, ['--type', 'text/plain;charset=utf-8'], {
        stdio: ['pipe', 'ignore', 'ignore'],
        windowsHide: true,
      });
    } catch (_) { return resolve(false); }
    const done = (ok) => { if (!settled) { settled = true; resolve(!!ok); } };
    const timer = setTimeout(() => { try { child.kill(); } catch (_) {} done(false); }, 4000);
    if (timer.unref) timer.unref();
    child.once('error', () => { clearTimeout(timer); done(false); });
    child.once('exit', (code) => { clearTimeout(timer); done(code === 0); });
    try { child.stdin.end(String(text)); }
    catch (_) { clearTimeout(timer); done(false); }
  });
}

function readWaylandText(deps) {
  if (!isWayland()) return Promise.resolve(null);
  const run = (deps && deps.execFile) || execFile;
  const bin = process.env.PC_WLPASTE || 'wl-paste';
  return new Promise((resolve) => {
    run(bin, ['--no-newline', '--type', 'text/plain'], {
      timeout: 3000,
      maxBuffer: 65536,
      windowsHide: true,
    }, (err, stdout) => resolve(err ? null : String(stdout || '').slice(0, 65536)));
  });
}

module.exports = { isWayland, writeWaylandText, readWaylandText };
