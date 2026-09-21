/* System Settings → PosterChan Server: the bundled server (app-misc/posterchan-server), OFF until enabled.
 *
 * Everything privileged goes through ONE root-owned helper, /usr/local/bin/pc-server, which runs the
 * project's own ./install.sh for the heavy lifting. This bridge only ever asks it for one of the verbs
 * its sudoers rule names — never builds a command line out of what the renderer sent — so the grant
 * stays "these few things" even if the page asking were not ours.
 *
 * Slow verbs (enable, install-ai) return as soon as the helper has started a transient systemd unit;
 * progress is read back with job()/jobLog(), which is also what survives this process restarting in
 * the middle of an hour-long install. */
'use strict';
const { execFile } = require('child_process');
const fs = require('fs');
const http = require('http');
const os = require('os');

const SUDO = process.env.PC_SUDO || 'sudo';
const HELPER = process.env.PC_SERVER_HELPER || '/usr/local/bin/pc-server';
const FEATURES = ['ai', 'music', 'video', 'voice', 'searxng'];
const VERBS = ['status', 'enable', 'disable', 'restart', 'logs', 'job', 'job-log'];

function helper(args, ms) {
  return new Promise((resolve, reject) => execFile(SUDO, ['-n', HELPER, ...args],
    { timeout: ms || 20000, maxBuffer: 4 * 1024 * 1024, env: { ...process.env, LC_ALL: 'C' } },
    (err, stdout, stderr) => {
      if (!err) return resolve(String(stdout || ''));
      const text = String(stderr || err.message || err).trim();
      /* sudo's own refusals, in a sentence a person can act on. "a password is required" is what an
       * account outside wheel gets: its Unix password does not exist, so it could never type one. */
      if (/password is required|not in the sudoers|not allowed to execute|may not run sudo/i.test(text))
        return reject(new Error('Only an administrator of this computer can change the server.'));
      reject(new Error(text.split('\n').filter(Boolean).pop() || 'the server helper failed'));
    }));
}

function available() {
  return process.platform === 'linux' && fs.existsSync(HELPER);
}

/* Is anything actually ANSWERING on the port — a separate fact from systemd calling the unit active,
 * which it does from the moment the process starts, a minute before uvicorn is listening. */
function reachable(port, ms) {
  return new Promise(resolve => {
    const req = http.get({ host: '127.0.0.1', port, path: '/client/config', timeout: ms || 2500 }, res => {
      res.resume(); resolve(res.statusCode > 0 && res.statusCode < 500);
    });
    req.on('timeout', () => { req.destroy(); resolve(false); });
    req.on('error', () => resolve(false));
  });
}

function lanAddresses() {
  const out = [];
  for (const list of Object.values(os.networkInterfaces() || {}))
    for (const a of list || [])
      if (a && a.family === 'IPv4' && !a.internal) out.push(a.address);
  return out;
}

async function status() {
  if (!available()) return { available: false, reason: 'The PosterChan server is not installed on this computer.' };
  const s = JSON.parse(await helper(['status']));
  const port = Number(s.port) || 3051, relayPort = Number(s.relayPort) || 3052;
  const up = s.active === 'active' ? await reachable(port) : false;
  const lan = lanAddresses();
  return Object.assign(s, {
    available: true, reachable: up,
    localUrl: 'http://127.0.0.1:' + port,
    adminUrl: 'http://127.0.0.1:' + port + '/admin',
    relayUrl: 'ws://127.0.0.1:' + relayPort,
    lanUrls: lan.map(ip => 'http://' + ip + ':' + port),
    lanRelayUrls: lan.map(ip => 'ws://' + ip + ':' + relayPort),
  });
}

async function verb(name) {
  if (!VERBS.includes(name)) throw new Error('unknown server action');
  if (!available()) throw new Error('The PosterChan server is not installed on this computer.');
  return helper([name], name === 'logs' ? 30000 : 60000);
}

async function job() { return JSON.parse(await verb('job')); }
function installAi(feature) {
  const f = String(feature || '');
  if (!FEATURES.includes(f)) return Promise.reject(new Error('unknown AI feature'));
  if (!available()) return Promise.reject(new Error('The PosterChan server is not installed on this computer.'));
  return helper(['install-ai', f], 60000);
}

module.exports = {
  FEATURES, available, status, job,
  enable: () => verb('enable'), disable: () => verb('disable'), restart: () => verb('restart'),
  logs: () => verb('logs'), jobLog: () => verb('job-log'), installAi,
  _reachable: reachable,
};
