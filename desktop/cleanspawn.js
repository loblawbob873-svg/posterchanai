'use strict';
/* AN APP STARTED FROM THE DESKTOP MUST NOT INHERIT THE DESKTOP.
 *
 * Chromium does not mark its descriptors close-on-exec, and Node's spawn() hands a child everything that
 * is not -- so every program launched from PosterChanOS (the start menu, a .desktop file, xdg-open, a
 * VM viewer) started holding the desktop's GPU buffers, files and SOCKETS. Measured 2026-10-16 on .102:
 * Steam, launched from the desktop hours earlier, owned the desktop's remote-debugging LISTENER on 9222;
 * after a shell restart the new desktop could not bind its own port, and the port queued connections
 * nobody would ever answer. Any listener or open file of the desktop could end up held by a game.
 *
 * `cleanArgv` wraps a command so a shell closes every descriptor above stderr and then EXECs the real
 * program: same PID (the window is matched by PID -- see wm.js launch), nothing inherited. A program
 * that cannot be found exits 127 instead of raising ENOENT; `notFound(code)` says which that was. */
const CLOSE_AND_EXEC =
  'for f in /proc/$$/fd/*; do n=${f##*/}; [ "$n" -gt 2 ] 2>/dev/null && eval "exec $n>&-" 2>/dev/null; done; exec "$@"';

function cleanArgv(argv) {
  return ['/bin/sh', '-c', CLOSE_AND_EXEC, 'pc-launch'].concat(argv);
}

function cleanSpawn(spawn, argv, opts) {
  const a = cleanArgv(argv);
  return spawn(a[0], a.slice(1), opts);
}

const notFound = (code) => code === 127;

module.exports = { cleanArgv, cleanSpawn, notFound, CLOSE_AND_EXEC };
