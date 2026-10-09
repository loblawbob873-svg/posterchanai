'use strict';
/* POSTERCHAN OFFICE ON THIS MACHINE (PosterChanOS) — and with no network.
 *
 * "for posterchanOS, let's make office work offline too … include the office suite that loads only
 * when office documents are loaded". The instance's Office is Collabora CODE behind a small WOPI host
 * (app/routers/office.py); with no network there is no instance, so a document could not be opened at
 * all. PosterChanOS ships the SAME CODE build (app-office/posterchan-office, unpacked under
 * /opt/posterchan-office) and this module is the WOPI host for it, in the desktop's main process:
 *
 *   - CODE is NOT a service. Nothing runs until a document is opened; the first open starts coolwsd on
 *     a free loopback port, and it is stopped again after the last document has been closed for
 *     IDLE_MS. A 1 GB editor idling on every desktop for the one day a week somebody opens a .docx
 *     would be the wrong trade.
 *   - The same contract as the router, so files.js drives both through one code path: a session is
 *     {id, token, editor_url, expires}; contents/export/close by id. The bytes live in a private
 *     per-session directory and never leave the machine.
 *   - Loopback only, both halves (coolwsd and the WOPI listener), and every WOPI call carries the
 *     session's random token — another local user's process cannot read a document by guessing a port.
 */
const fs = require('fs');
const os = require('os');
const net = require('net');
const http = require('http');
const path = require('path');
const crypto = require('crypto');
const { spawn } = require('child_process');

const ROOT = process.env.PC_OFFICE_ROOT || '/opt/posterchan-office';
const IDLE_MS = 10 * 60 * 1000;
const START_MS = 120 * 1000;
const MAX = 128 * 1024 * 1024;
const TTL_S = 6 * 3600;
// Same allowlist as the router's export, for the same reason: not a general conversion service.
const EXPORT = {
  pdf: 'application/pdf',
  docx: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
  odt: 'application/vnd.oasis.opendocument.text',
  xlsx: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
  ods: 'application/vnd.oasis.opendocument.spreadsheet',
  pptx: 'application/vnd.openxmlformats-officedocument.presentationml.presentation',
  odp: 'application/vnd.oasis.opendocument.presentation',
};

function available(root = ROOT) {
  try { return fs.statSync(path.join(root, 'usr', 'bin', 'coolwsd')).isFile(); } catch (_) { return false; }
}

function freePort() {
  return new Promise((resolve, reject) => {
    const s = net.createServer();
    s.on('error', reject);
    s.listen(0, '127.0.0.1', () => { const p = s.address().port; s.close(() => resolve(p)); });
  });
}

function _lib(root) {
  return ['usr/lib', 'usr/lib/x86_64-linux-gnu', 'usr/lib64', 'lib', 'lib/x86_64-linux-gnu', 'lib64']
    .map(d => path.join(root, d)).join(':');
}

class OfficeLocal {
  constructor(opts = {}) {
    this.root = opts.root || ROOT;
    this.origin = opts.origin || 'app://posterchan';
    this.idleMs = opts.idleMs || IDLE_MS;
    this.base = opts.workDir || path.join(os.tmpdir(), 'posterchan-office-' + process.getuid());
    this.sessions = new Map();
    this.code = null;          // {proc, port, ready:Promise, dir}
    this.wopi = null;          // {server, port}
    this._idle = null;
    this.log = opts.log || (() => {});
  }

  available() { return available(this.root); }

  /* ---- CODE, started on demand --------------------------------------------------------------- */
  async _startCode() {
    if (this.code) return this.code.ready;
    const port = await freePort();
    fs.mkdirSync(this.base, { recursive: true, mode: 0o700 });
    const dir = fs.mkdtempSync(path.join(this.base, 'code-'));
    const root = this.root;
    const env = Object.assign({}, process.env, {
      PATH: path.join(root, 'usr/bin') + ':' + path.join(root, 'usr/sbin') + ':' + (process.env.PATH || ''),
      // REPLACES the inherited value: an AppImage-launched desktop exports its own lib dir, which
      // would shadow CODE's bundled libraries (the bundled-tor lesson, desktop/tor.js).
      LD_LIBRARY_PATH: _lib(root),
      LC_ALL: 'C.UTF-8', SAL_LOG: '-INFO-WARN', HOME: dir, TMPDIR: dir,
      COOLKITCONFIG_XCU: path.join(root, 'etc/coolwsd/coolkitconfig.xcu'),
    });
    const tpl = path.join(dir, 'systemplate');
    await new Promise(res => {
      const p = spawn(path.join(root, 'usr/bin/coolwsd-systemplate-setup'), [tpl, path.join(root, 'opt/collaboraoffice')],
        { env, stdio: 'ignore' });
      p.on('error', () => res()); p.on('close', () => res());
    });
    // AppRun's arguments, minus what this host does differently: a private port, loopback only, no
    // welcome dialog, no update check, and the desktop page as the only frame ancestor.
    const args = [
      '--config-file=' + path.join(root, 'etc/coolwsd/coolwsd.xml'),
      '--disable-cool-user-checking',
      '--port=' + port,
      '--lo-template-path=' + path.join(root, 'opt/collaboraoffice'),
      '--o:sys_template_path=' + tpl + '/',
      '--o:security.capabilities=false',
      '--o:security.seccomp=false',
      '--o:child_root_path=' + path.join(dir, 'jails'),
      '--o:file_server_root_path=' + path.join(root, 'usr/share/coolwsd'),
      '--o:ssl.enable=false',
      '--o:ssl.termination=false',
      '--o:net.proto=IPv4',
      '--o:net.listen=loopback',
      '--o:net.frame_ancestors=' + this.origin,
      '--o:memproportion=25',
      '--o:welcome.enable=false',
      '--o:fetch_update_check=0',
      '--o:allow_update_popup=false',
      '--o:logging.level=warning',
      '--o:admin_console.enable=false',
      '--o:storage.wopi.host[0]=127.0.0.1',
      '--o:storage.wopi.host[0][@allow]=true',
    ];
    const proc = spawn(path.join(root, 'usr/bin/coolwsd'), args, { env, stdio: ['ignore', 'ignore', 'pipe'], detached: false });
    let tail = '';
    proc.stderr.on('data', d => { tail = (tail + d).slice(-4000); });
    const code = { proc, port, dir, exited: false };
    proc.on('exit', (c, sig) => {
      code.exited = true;
      this.log('[office-local] coolwsd exited ' + (sig || c));
      if (this.code === code) this.code = null;
      try { fs.rmSync(dir, { recursive: true, force: true }); } catch (_) {}
    });
    proc.on('error', e => { code.exited = true; tail += String(e && e.message || e); if (this.code === code) this.code = null; });
    code.ready = (async () => {
      const until = Date.now() + START_MS;
      while (Date.now() < until) {
        if (code.exited) throw new Error('the office editor stopped while starting: ' + (tail.trim().split('\n').pop() || 'no output'));
        try {
          const r = await fetch('http://127.0.0.1:' + port + '/hosting/discovery');
          if (r.ok) { code.discovery = await r.text(); return code; }
        } catch (_) {}
        await new Promise(r => setTimeout(r, 300));
      }
      this._stopCode();
      throw new Error('the office editor did not start in time');
    })();
    this.code = code;
    return code.ready;
  }

  _stopCode() {
    const c = this.code; this.code = null;
    if (c && !c.exited) { try { c.proc.kill('SIGTERM'); } catch (_) {} setTimeout(() => { if (!c.exited) try { c.proc.kill('SIGKILL'); } catch (_) {} }, 15000).unref(); }
  }

  _touch() {
    if (this._idle) { clearTimeout(this._idle); this._idle = null; }
    if (!this.sessions.size && this.code) {
      this._idle = setTimeout(() => { if (!this.sessions.size) this._stopCode(); }, this.idleMs);
      if (this._idle.unref) this._idle.unref();
    }
  }

  /* ---- the WOPI host --------------------------------------------------------------------------- */
  async _startWopi() {
    if (this.wopi) return this.wopi;
    const server = http.createServer((req, res) => this._wopiRequest(req, res).catch(e => {
      try { res.writeHead(500); res.end(String(e && e.message || e)); } catch (_) {}
    }));
    await new Promise((resolve, reject) => { server.on('error', reject); server.listen(0, '127.0.0.1', resolve); });
    this.wopi = { server, port: server.address().port };
    return this.wopi;
  }

  _auth(id, token) {
    const s = this.sessions.get(id);
    if (!s || typeof token !== 'string' || token.length !== s.token.length) return null;
    if (!crypto.timingSafeEqual(Buffer.from(token), Buffer.from(s.token))) return null;
    return s;
  }

  async _wopiRequest(req, res) {
    const u = new URL(req.url, 'http://127.0.0.1');
    const m = /^\/wopi\/files\/([a-f0-9]{32})(\/contents)?$/.exec(u.pathname);
    const send = (code, body, headers) => { res.writeHead(code, Object.assign({ 'Content-Type': 'application/json' }, headers || {})); res.end(body == null ? '{}' : body); };
    if (!m) return send(404);
    const s = this._auth(m[1], u.searchParams.get('access_token') || '');
    if (!s) return send(401);
    const lockNow = () => (s.lock && s.lock.until > Date.now()) ? s.lock.id : '';
    if (!m[2] && req.method === 'GET') {
      return send(200, JSON.stringify({
        BaseFileName: s.name, Size: fs.statSync(s.file).size, Version: String(s.version),
        OwnerId: 'posterchan', UserId: 'posterchan', UserFriendlyName: 'PosterChan user',
        UserCanWrite: !s.readonly, SupportsLocks: true, SupportsUpdate: !s.readonly,
        PostMessageOrigin: this.origin,
      }));
    }
    if (m[2] && req.method === 'GET') {
      res.writeHead(200, { 'Content-Type': 'application/octet-stream', 'X-WOPI-ItemVersion': String(s.version) });
      return fs.createReadStream(s.file).pipe(res);
    }
    if (m[2] && req.method === 'POST') {
      if (s.readonly) return send(403);
      const lock = lockNow();
      if (lock && lock !== (req.headers['x-wopi-lock'] || '')) return send(409, null, { 'X-WOPI-Lock': lock });
      const chunks = []; let n = 0;
      for await (const c of req) { n += c.length; if (n > MAX) return send(413); chunks.push(c); }
      const tmp = s.file + '.save';
      fs.writeFileSync(tmp, Buffer.concat(chunks), { mode: 0o600 });
      fs.renameSync(tmp, s.file);
      s.version++;
      return send(200, null, { 'X-WOPI-ItemVersion': String(s.version) });
    }
    if (!m[2] && req.method === 'POST') {
      const op = String(req.headers['x-wopi-override'] || '').toUpperCase();
      const want = String(req.headers['x-wopi-lock'] || ''), old = String(req.headers['x-wopi-oldlock'] || '');
      const cur = lockNow();
      if (op === 'LOCK') {
        if (cur && cur !== want && cur !== old) return send(409, null, { 'X-WOPI-Lock': cur });
        s.lock = { id: want, until: Date.now() + 1800e3 };
      } else if (op === 'REFRESH_LOCK') {
        if (cur !== want) return send(409, null, { 'X-WOPI-Lock': cur });
        s.lock = { id: want, until: Date.now() + 1800e3 };
      } else if (op === 'UNLOCK') {
        if (cur !== want) return send(409, null, { 'X-WOPI-Lock': cur });
        s.lock = null;
      } else if (op === 'GET_LOCK') {
        return send(200, null, { 'X-WOPI-Lock': cur });
      } else return send(501);
      return send(200);
    }
    return send(405);
  }

  /* ---- what the page asks for ------------------------------------------------------------------ */
  _actionUrl(discovery, ext, mode) {
    const order = mode === 'edit' ? ['edit', 'view_comment', 'view'] : ['view', 'view_comment', 'edit'];
    const found = {};
    const re = /<action\b([^>]*)>/g; let a;
    while ((a = re.exec(discovery))) {
      const attr = k => { const x = new RegExp('\\b' + k + '="([^"]*)"').exec(a[1]); return x ? x[1].replace(/&amp;/g, '&') : ''; };
      if (attr('ext').toLowerCase() !== ext) continue;
      const name = attr('name'), url = attr('urlsrc');
      if (name && url && !found[name]) found[name] = url;
    }
    for (const n of order) if (found[n]) return found[n];
    const any = Object.values(found)[0];
    if (any) return any;
    throw new Error('the office editor does not open .' + ext + ' files');
  }

  async open(bytes, name, mode) {
    if (!this.available()) throw new Error('the office editor is not installed on this machine');
    const buf = Buffer.isBuffer(bytes) ? bytes : Buffer.from(bytes instanceof ArrayBuffer ? new Uint8Array(bytes) : (bytes || []));
    if (buf.length > MAX) throw new Error('office document is too large');
    const base = path.basename(String(name || 'document')).slice(0, 240) || 'document';
    const ext = base.includes('.') ? base.split('.').pop().toLowerCase() : '';
    const [code, wopi] = await Promise.all([this._startCode(), this._startWopi()]);
    const urlsrc = this._actionUrl(code.discovery, ext, mode === 'view' ? 'view' : 'edit');
    const id = crypto.randomBytes(16).toString('hex');
    const dir = path.join(this.base, 'doc-' + id);
    fs.mkdirSync(dir, { recursive: true, mode: 0o700 });
    const file = path.join(dir, 'document');
    fs.writeFileSync(file, buf, { mode: 0o600 });
    const s = { id, token: crypto.randomBytes(24).toString('hex'), name: base, file, dir, version: 1,
                readonly: mode === 'view', lock: null, expires: Math.floor(Date.now() / 1000) + TTL_S };
    this.sessions.set(id, s);
    this._touch();
    // Discovery names the editor by whatever host coolwsd thinks it has; the page reaches it on loopback.
    const p = new URL(urlsrc);
    const editor = 'http://127.0.0.1:' + code.port + p.pathname + p.search + (p.search ? '&' : '?')
      + 'WOPISrc=' + encodeURIComponent('http://127.0.0.1:' + wopi.port + '/wopi/files/' + id);
    return { id, token: s.token, editor_url: editor, expires: s.expires, readonly: s.readonly };
  }

  contents(id, token) {
    const s = this._auth(id, token);
    if (!s) throw new Error('this document is no longer open');
    return fs.readFileSync(s.file);
  }

  async export(id, token, fmt) {
    const s = this._auth(id, token);
    if (!s) throw new Error('this document is no longer open');
    fmt = String(fmt || '').toLowerCase();
    if (!EXPORT[fmt]) throw new Error('cannot export to .' + fmt);
    const code = await this._startCode();
    const fd = new FormData();
    fd.append('data', new Blob([fs.readFileSync(s.file)]), s.name);
    const r = await fetch('http://127.0.0.1:' + code.port + '/cool/convert-to/' + fmt, { method: 'POST', body: fd });
    if (!r.ok) throw new Error('could not convert this document (' + r.status + ')');
    const out = Buffer.from(await r.arrayBuffer());
    if (!out.length) throw new Error('the converter returned an empty document');
    return { mime: EXPORT[fmt], bytes: out };
  }

  close(id, token) {
    const s = this._auth(id, token);
    if (!s) return false;
    this.sessions.delete(id);
    try { fs.rmSync(s.dir, { recursive: true, force: true }); } catch (_) {}
    this._touch();
    return true;
  }

  stop() {
    for (const s of this.sessions.values()) { try { fs.rmSync(s.dir, { recursive: true, force: true }); } catch (_) {} }
    this.sessions.clear();
    if (this._idle) clearTimeout(this._idle);
    this._stopCode();
    if (this.wopi) { try { this.wopi.server.close(); } catch (_) {} this.wopi = null; }
  }

  running() { return !!(this.code && !this.code.exited); }
}

module.exports = { OfficeLocal, available, EXPORT };
