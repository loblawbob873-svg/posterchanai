/* "Play in VLC" from the Preview (a video this browser cannot decode -- a phone's HEVC, an .mkv). The page hands over the
 * bytes it already decrypted; they are written where only this user can read them ($XDG_RUNTIME_DIR, else the temp
 * dir, in a 0700 directory of their own) and VLC is started on that file. The copy is removed when VLC exits, and any
 * a crash left behind is swept the next time. PosterChanOS only (shell mode): VLC is part of its package set. */
'use strict';
const fs = require('fs');
const os = require('os');
const path = require('path');
const crypto = require('crypto');
const { spawn } = require('child_process');

const PREFIX = 'pc-play-';

function base() { return process.env.XDG_RUNTIME_DIR || os.tmpdir(); }

/** A file name safe to put on disk: no directories, no leading dots, something sensible when nothing is left. */
function safeName(name) {
  const n = String(name || '').split(/[\\/]/).pop().replace(/[\0-\x1f]/g, '').replace(/^\.+/, '').slice(0, 120);
  return n || 'video';
}

/** Remove copies whose VLC is gone (a crash, a restart of the desktop). */
function sweep(root = base()) {
  let names = [];
  try { names = fs.readdirSync(root); } catch (_) { return 0; }
  let n = 0;
  for (const d of names) {
    if (!d.startsWith(PREFIX)) continue;
    const full = path.join(root, d);
    let pid = 0;
    try { pid = Number(fs.readFileSync(path.join(full, '.pid'), 'utf8')) || 0; } catch (_) { pid = 0; }
    let alive = false;
    if (pid) { try { process.kill(pid, 0); alive = true; } catch (_) { alive = false; } }
    if (!alive) { try { fs.rmSync(full, { recursive: true, force: true }); n++; } catch (_) {} }
  }
  return n;
}

/** Write `bytes` and start the player on them. Resolves 'opened' once the player process has started. */
function open(bytes, name, { root = base(), player = 'vlc', spawnFn = spawn } = {}) {
  sweep(root);
  const buf = Buffer.from(bytes instanceof ArrayBuffer ? new Uint8Array(bytes) : bytes);
  if (!buf.length) return Promise.reject(new Error('there are no bytes to play'));
  const dir = path.join(root, PREFIX + crypto.randomBytes(8).toString('hex'));
  fs.mkdirSync(dir, { mode: 0o700 });
  const file = path.join(dir, safeName(name));
  fs.writeFileSync(file, buf, { mode: 0o600 });
  return new Promise((resolve, reject) => {
    let child;
    try { child = spawnFn(player, ['--', file], { detached: true, stdio: 'ignore' }); }
    catch (e) { fs.rmSync(dir, { recursive: true, force: true }); return reject(e); }
    const gone = () => { try { fs.rmSync(dir, { recursive: true, force: true }); } catch (_) {} };
    child.once('error', (e) => { gone(); reject(e.code === 'ENOENT' ? new Error('VLC is not installed') : e); });
    child.once('exit', gone);
    child.once('spawn', () => {
      try { fs.writeFileSync(path.join(dir, '.pid'), String(child.pid)); } catch (_) {}
      try { child.unref(); } catch (_) {}
      resolve('opened');
    });
  });
}

module.exports = { open, sweep, safeName, PREFIX };
