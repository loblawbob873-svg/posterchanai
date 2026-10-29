/* POSTERFOX'S PASSWORDS ADD-ON IS PAIRED BY THE DESKTOP, with nothing to type ("we need to make sure that the passwords
 * extension automatically connects to posterchanOS without any config", 2026-10-29).
 *
 * The client builds this account's pairing (the same code Passwords -> Pair a device shows) and hands it here; this writes
 * it into Firefox's per-user MANAGED STORAGE for that add-on, which the add-on reads with storage.managed and pairs from.
 * The file is this user's alone (0600 in a 0700 directory) -- the same home that already holds the desktop's own copy of
 * the key -- and it is removed at sign-out, which the add-on reads as "unpair". PosterChanOS only (shell mode). */
'use strict';
const fs = require('fs');
const path = require('path');

const ADDON_ID = 'passwords@poster.place';

function file(home) { return path.join(home, '.mozilla', 'managed-storage', ADDON_ID + '.json'); }

/** A pairing code is base64 of a `pcvault` JSON payload; anything else is refused rather than written. */
function valid(code) {
  if (typeof code !== 'string' || !code || code.length > 65536) return false;
  try {
    const p = JSON.parse(Buffer.from(code, 'base64').toString('utf8'));
    return !!(p && p.t === 'pcvault' && /^[0-9a-f]{64}$/.test(p.pubkey || '') && p.key);
  } catch (_) { return false; }
}

/** Write (code) or remove (null/'') the add-on's managed pairing. Returns 'written', 'removed', 'unchanged' or throws. */
function set(home, code) {
  const f = file(home);
  if (!code) {
    try { fs.unlinkSync(f); return 'removed'; } catch (e) { if (e.code === 'ENOENT') return 'unchanged'; throw e; }
  }
  if (!valid(code)) throw new Error('not a PosterChan pairing code');
  const body = JSON.stringify({ name: ADDON_ID, description: 'PosterChanOS: this account\'s Passwords pairing',
                                type: 'storage', data: { pairing: code } });
  try { if (fs.readFileSync(f, 'utf8') === body) return 'unchanged'; } catch (_) { /* not there yet */ }
  const dir = path.dirname(f);
  fs.mkdirSync(dir, { recursive: true, mode: 0o700 });
  fs.chmodSync(dir, 0o700);
  const tmp = f + '.' + process.pid + '.tmp';
  fs.writeFileSync(tmp, body, { mode: 0o600 });
  fs.renameSync(tmp, f);
  return 'written';
}

module.exports = { ADDON_ID, file, valid, set };
