/* `pc-open` — OPEN A FILE ON THIS COMPUTER IN ONE OF THE DESKTOP'S OWN APPS, FROM A TERMINAL.
 *
 * "Need way in CLI to open files in office, PosterChan Code, PosterChan Preview, etc." Office, Code
 * and Preview are windows inside this process — there is no program on disk to exec for them — so a
 * command line can only ASK the running shell. This is that door: a Unix socket in the user's
 * runtime directory, one JSON request per connection, one JSON answer back.
 *
 *   → {"v":1, "app":"auto|preview|office|code|files", "paths":["/abs/one", …]}
 *   ← {"ok":true|false, "results":[{"path", "ok", "app", "why"}], "why"}
 *
 * WHAT LIMITS IT IS THE UNIX ACCOUNT, exactly as for hostfs.js: the socket lives in
 * $XDG_RUNTIME_DIR (0700, this user's) and is chmod 0600, so only this account can connect, and it
 * can only name files this account can already read in its own terminal. It grants nothing the
 * Files app does not.
 *
 * The files are CHECKED HERE, before the renderer sees them, so a typo is answered as "No such file"
 * with a nonzero exit rather than as an empty editor window. The pure half (parse, inspect) is
 * exported for tests/test_pc_open.py, which runs it under node against a real directory.
 */
'use strict';
const fs = require('fs');
const net = require('net');
const path = require('path');

const SOCKET_NAME = 'posterchan-open.sock';
const APPS = ['auto', 'preview', 'office', 'code', 'files'];
const MAX_PATHS = 64;
const MAX_REQUEST = 256 * 1024;

/** A request line → {app, paths} or throws with the reason. Paths must already be ABSOLUTE: the
 *  CLI resolves them against ITS working directory, which this process cannot know. */
function parseRequest(text) {
  let req;
  try { req = JSON.parse(String(text || '')); } catch (_) { throw new Error('not a pc-open request'); }
  if (!req || typeof req !== 'object' || req.v !== 1) throw new Error('unsupported request version');
  const app = req.app == null ? 'auto' : String(req.app);
  if (!APPS.includes(app)) throw new Error('unknown app: ' + app);
  if (!Array.isArray(req.paths) || !req.paths.length) throw new Error('no files named');
  if (req.paths.length > MAX_PATHS) throw new Error('too many files (at most ' + MAX_PATHS + ')');
  const paths = req.paths.map((p) => {
    const s = String(p == null ? '' : p);
    if (!s || s.length > 4096 || s.includes('\0') || !path.isAbsolute(s))
      throw new Error('not an absolute path: ' + s.slice(0, 200));
    return path.normalize(s);
  });
  return { app, paths };
}

/** What each path IS, measured: {path, kind:'file'|'dir', size, mtime} or {path, ok:false, why}.
 *  The same words a shell uses, so `pc-open typo.txt` reads like `cat typo.txt`. */
function inspect(p, fsx) {
  const f = fsx || fs;
  let st;
  try { st = f.statSync(p); }
  catch (e) {
    const why = e && e.code === 'ENOENT' ? 'No such file or directory'
      : e && e.code === 'EACCES' ? 'Permission denied'
      : e && e.code === 'ENOTDIR' ? 'Not a directory'
      : String((e && e.message) || e);
    return { path: p, ok: false, why };
  }
  const kind = st.isDirectory() ? 'dir' : st.isFile() ? 'file' : '';
  if (!kind) return { path: p, ok: false, why: 'Not a regular file' };
  try { f.accessSync(p, fs.constants.R_OK); }
  catch (_) { return { path: p, ok: false, why: 'Permission denied' }; }
  return { path: p, ok: true, kind, size: Number(st.size) || 0, mtime: Math.round(Number(st.mtimeMs) || 0) };
}

/** A directory can only be shown by Files; everything else is the renderer's call. */
function precheck(app, item) {
  if (!item.ok) return item;
  if (item.kind === 'dir' && app !== 'files' && app !== 'auto')
    return { path: item.path, ok: false, why: 'Is a directory (use --files)' };
  return item;
}

/* One request per connection. A client that never finishes (or sends a megabyte) is dropped rather
 * than held: this listens inside the desktop process, and nothing on the command line is worth a
 * stalled shell. */
function listen(socketPath, handle, opts) {
  const o = opts || {};
  /* A socket left by the previous shell (a restart, a crash) is replaced; anything else at that
   * path is somebody's file and is refused rather than deleted. */
  let st = null;
  try { st = fs.lstatSync(socketPath); } catch (e) { if (!e || e.code !== 'ENOENT') throw e; }
  if (st) {
    if (!st.isSocket()) throw new Error(socketPath + ' exists and is not a socket');
    fs.unlinkSync(socketPath);
  }
  const server = net.createServer((c) => {
    let buf = '';
    let done = false;
    c.setEncoding('utf8');
    const timer = setTimeout(() => { if (!done) { done = true; c.destroy(); } }, o.idleMs || 60000);
    const answer = (obj) => {
      if (done) return; done = true; clearTimeout(timer);
      try { c.end(JSON.stringify(obj) + '\n'); } catch (_) {}
    };
    c.on('error', () => {});
    c.on('data', (x) => {
      buf += x;
      if (buf.length > MAX_REQUEST) { answer({ ok: false, why: 'request too large' }); return; }
      const nl = buf.indexOf('\n');
      if (nl < 0) return;
      const line = buf.slice(0, nl);
      let req;
      try { req = parseRequest(line); }
      catch (e) { answer({ ok: false, why: String((e && e.message) || e) }); return; }
      Promise.resolve().then(() => handle(req)).then(answer,
        (e) => answer({ ok: false, why: String((e && e.message) || e) }));
    });
  });
  server.on('error', (e) => { if (o.onError) o.onError(e); });
  server.listen(socketPath, () => { try { fs.chmodSync(socketPath, 0o600); } catch (_) {} if (o.onListening) o.onListening(); });
  return server;
}

/* THE WHOLE REQUEST, given something that can reach the desktop's renderer. `deliver(app, items)`
 * resolves the renderer's per-file answers; files that failed the check never reach it. */
async function handleWith(deliver, req, fsx) {
  const checked = req.paths.map((p) => precheck(req.app, inspect(p, fsx)));
  const good = checked.filter((x) => x.ok);
  let answered = [];
  if (good.length) {
    answered = await deliver(req.app, good.map(({ path: p, kind, size, mtime }) => ({ path: p, kind, size, mtime })));
    if (!Array.isArray(answered)) answered = [];
  }
  const byPath = new Map(answered.map((r) => [r && r.path, r]));
  const results = checked.map((x) => {
    if (!x.ok) return { path: x.path, ok: false, why: x.why };
    const r = byPath.get(x.path);
    if (!r) return { path: x.path, ok: false, why: 'the desktop did not answer for this file' };
    return { path: x.path, ok: !!r.ok, app: String(r.app || ''), why: r.ok ? '' : String(r.why || 'could not open it') };
  });
  return { ok: results.every((r) => r.ok), results };
}

module.exports = { SOCKET_NAME, APPS, parseRequest, inspect, precheck, listen, handleWith };
