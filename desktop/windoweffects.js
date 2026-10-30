/* Window effects for System Settings: a thin bridge to the user helper pc-window-effects.
 *
 * The helper chooses among Wayfire's own effects (wobbly windows; how a window opens, closes and minimizes -- zoom,
 * fade, fire, shatter, ...), keeps the person's choice in ~/.config/posterchanos/window-effects.json and applies it
 * to the running compositor; this only asks it. No sudo: it is the person's own session. Asked for 2026-10-29
 * ("wobbly windows, exploding windows, etc. its own settings menu").
 */
'use strict';
const fs = require('fs');
const { execFile } = require('child_process');

const HELPER = process.env.PC_WINDOW_EFFECTS_HELPER || '/usr/local/bin/pc-window-effects';
const KEYS = ['open', 'close', 'minimize', 'speed', 'wobbly', 'wobbliness', 'fire_color', 'fire_rainbow'];
const BOOLS = new Set(['wobbly', 'fire_rainbow']);

function run(args) {
  return new Promise((resolve, reject) => {
    execFile(HELPER, args, { timeout: 15000 }, (err, stdout, stderr) => {
      if (err && !String(stdout || '').trim()) return reject(new Error(String(stderr || err.message || err).trim().split('\n').pop() || 'pc-window-effects failed'));
      try { resolve(JSON.parse(String(stdout))); } catch (_) { reject(new Error('pc-window-effects answered something that is not its status')); }
    });
  });
}

const status = () => run(['status']);
/* Only the known keys cross into the helper, and only as JSON in one argument -- never a shell. The helper checks
 * every value against what this machine offers again; this is the first of two filters, not the only one. */
function set(choice) {
  const c = {};
  for (const k of KEYS) if (choice && Object.prototype.hasOwnProperty.call(choice, k)) c[k] = BOOLS.has(k) ? choice[k] === true : String(choice[k]).slice(0, 32);
  return run(['set', JSON.stringify(c)]);
}
function available() {
  try { return process.platform === 'linux' && fs.existsSync(HELPER); } catch (_) { return false; }
}

module.exports = { available, status, set };
