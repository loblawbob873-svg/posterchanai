/* The desktop app's filesystem bridge, for a test browser: the SHIPPED desktop/fsbridge.js over HTTP.
 *
 * In the desktop app the page's `window.pcFs` is preload.js forwarding each call over IPC to main.js,
 * which hands it to fsbridge.js against a real directory. A headless Chrome has no preload, so this
 * stands in for the two Electron halves only -- the IPC hop and main.js's three handlers that are not
 * fsbridge's (pick: the native folder dialog, which here "chooses" PC_FS_DIR; power: powerMonitor,
 * here a plugged-in desktop; the watcher's push, here a long poll). Every file operation is the real
 * module on a real directory: atomic writes, confinement, scan, hashing, removal.
 *
 *   node fsbridge_rpc.cjs <dir>        prints the port it listens on, then serves
 *
 * POST /call {m, a:[...]}  -> {ok, r} | {ok:false, error}. Bytes travel as {$b64:"..."} both ways.
 * GET  /changed            -> long poll: [ids] whose folder changed (fsbridge.watch's callback).
 */
'use strict';
const http = require('http');
const path = require('path');
const fsbridge = require(path.join(__dirname, '..', '..', 'desktop', 'fsbridge.js'));

const DIR = process.argv[2];
fsbridge.init({ roots: [], save: () => {} });

const changed = [];
const waiters = [];
function flush(){
  if(!changed.length) return;
  const ids = changed.splice(0);
  for(const w of waiters.splice(0)) w(ids);
}

const dec = (v) => (v && typeof v === 'object' && typeof v.$b64 === 'string') ? new Uint8Array(Buffer.from(v.$b64, 'base64')) : v;
const enc = (v) => (v instanceof Uint8Array || Buffer.isBuffer(v)) ? { $b64: Buffer.from(v).toString('base64') } : v;

const MAIN = {
  // main.js pc:fs:pick -- the dialog "returns" DIR; a config that saved
  pick: () => Object.assign({}, fsbridge.addRoot(DIR), { persisted: true }),
  forget: (id) => fsbridge.removeRoot(String(id || '')),
  power: () => ({ charging: true, metered: false, online: true }),
  watch: (id, ms) => fsbridge.watch(String(id || ''), (which) => { changed.push(which); flush(); }, ms),
};

http.createServer((req, res) => {
  res.setHeader('Access-Control-Allow-Origin', '*');
  res.setHeader('Access-Control-Allow-Headers', 'Content-Type');
  if(req.method === 'OPTIONS'){ res.writeHead(204); return res.end(); }
  if(req.method === 'GET' && req.url === '/changed'){
    const t = setTimeout(() => { const i = waiters.indexOf(done); if(i >= 0) waiters.splice(i, 1); done([]); }, 20000);
    const done = (ids) => { clearTimeout(t); res.writeHead(200, { 'Content-Type': 'application/json' }); res.end(JSON.stringify(ids)); };
    waiters.push(done);
    return;
  }
  let body = '';
  req.on('data', (c) => { body += c; });
  req.on('end', async () => {
    let out;
    try{
      const { m, a } = JSON.parse(body || '{}');
      const fn = MAIN[m] || fsbridge[m];
      if(typeof fn !== 'function') throw new Error('no such bridge call: ' + m);
      out = { ok: true, r: enc(await fn.apply(fsbridge, (a || []).map(dec))) };
    }catch(e){ out = { ok: false, error: String((e && e.message) || e) }; }
    res.writeHead(200, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify(out));
  });
}).listen(0, '127.0.0.1', function(){ process.stdout.write(this.address().port + '\n'); });
