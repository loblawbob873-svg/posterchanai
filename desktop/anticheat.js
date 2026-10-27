/* Secure Boot ("anti-cheat") for System Settings: a thin bridge to the root helper pc-anticheat.
 *
 * The helper does the work and keeps the state (/var/lib/posterchan/anticheat.json); this only asks it. Anyone
 * signed in may read the state (a sudoers grant for `pc-anticheat status` alone); setting it up or undoing it
 * changes how the machine boots, so it goes through the administrator's own sudo and an ordinary account is told
 * so in words, instead of a sudo error. Asked for 2026-10-27 together with the installer's checkbox.
 */
'use strict';
const fs = require('fs');
const { execFile } = require('child_process');

const SUDO = process.env.PC_SUDO || 'sudo';
const HELPER = process.env.PC_ANTICHEAT_HELPER || '/usr/local/bin/pc-anticheat';

function run(args, ms) {
  return new Promise((resolve, reject) => {
    execFile(SUDO, ['-n', HELPER, ...args], { timeout: ms || 20000 }, (err, stdout, stderr) => {
      if (err) {
        const msg = String(stderr || err.message || err).trim();
        if (/password is required|not allowed|may not run/i.test(msg))
          return reject(new Error('Only the administrator of this computer can change Secure Boot.'));
        return reject(new Error(msg.split('\n').pop() || 'pc-anticheat failed'));
      }
      resolve(String(stdout || ''));
    });
  });
}

async function status() {
  const out = await run(['status']);
  try { return JSON.parse(out); } catch (_) { throw new Error('pc-anticheat answered something that is not its status'); }
}
async function setup() { await run(['setup']); return status(); }
async function undo() { await run(['undo']); return status(); }

function available() {
  try { return process.platform === 'linux' && fs.existsSync(HELPER); } catch (_) { return false; }
}

module.exports = { available, status, setup, undo };
