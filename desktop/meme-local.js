'use strict';
/* MEME BUILDER RENDERS ON THIS MACHINE (PosterChanOS).
 *
 * "for PosterChanOS, most of memebuilder should process on that machine (minus the AI features that
 * need network)". Every PosterChanOS machine already carries the server's code (app-misc/posterchan-
 * server → /opt/posterchan-server) and the system's python3, Pillow and ffmpeg — measured on the
 * laptop: the SAME meme_builder_service.render the server runs made a PNG in 0.15s and an MP4 in
 * 0.56s with nothing else installed and no server enabled. So the render is not reimplemented: the
 * edit list and the layer pictures (bytes the page already holds, so this works offline) are handed to
 * that function in a child python, and the finished file comes back.
 *
 * Nothing here trusts the renderer's input more than the server does: the edit goes through the same
 * render() validation, sources are written into a private temp dir under names this module chooses
 * (the page never names a path), and the child is killed past a deadline.
 */
const fs = require('fs');
const os = require('os');
const path = require('path');
const crypto = require('crypto');
const { spawn } = require('child_process');

const APP_DIR = process.env.PC_SERVER_APP || '/opt/posterchan-server';
const PYTHON = process.env.PC_MEME_PYTHON || 'python3';
const MAX_SOURCE_BYTES = 512 * 1024 * 1024;
const DEADLINE_MS = 15 * 60 * 1000;

// The runner. Given as `-c` text: python cannot import from inside the asar.
const RUNNER = String.raw`
import json, sys
job = json.load(open(sys.argv[1]))
sys.path.insert(0, job["app"])
from app.services import meme_builder_service as m
out, ct = m.render(job["edit"], job["sources"])
if isinstance(out, (bytes, bytearray)):
    open(job["out"], "wb").write(out)
else:
    import shutil; shutil.copyfile(out, job["out"])
json.dump({"ct": ct}, sys.stdout)
`;

/* OFFLINE, A LAYER CANNOT BE UPLOADED, SO IT IS KEPT HERE.
 *
 * Every picture, clip, drawing and erase mask the builder adds becomes a Blossom URL first — that is
 * how the server renderer reaches it. With no network that upload fails, and the builder was dead the
 * moment you added anything. On this machine the bytes are stored under userData by their sha256 and
 * given an address the page can draw (`app://posterchan/__memelocal/<sha>.<ext>`) and this renderer
 * can resolve straight to the file. Content-addressed, so the same picture twice is one file. */
const LOCAL_PREFIX = '/__memelocal/';
const _EXT_OK = /^\.[a-z0-9]{1,6}$/;
const _TYPES = {
  '.png': 'image/png', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.gif': 'image/gif', '.webp': 'image/webp',
  '.mp4': 'video/mp4', '.webm': 'video/webm', '.mov': 'video/quicktime', '.mp3': 'audio/mpeg',
  '.wav': 'audio/wav', '.ogg': 'audio/ogg', '.m4a': 'audio/mp4', '.opus': 'audio/ogg',
};
const _EXT_OF = { 'image/png': '.png', 'image/jpeg': '.jpg', 'image/gif': '.gif', 'image/webp': '.webp',
  'video/mp4': '.mp4', 'video/webm': '.webm', 'video/quicktime': '.mov', 'audio/mpeg': '.mp3',
  'audio/wav': '.wav', 'audio/x-wav': '.wav', 'audio/ogg': '.ogg', 'audio/mp4': '.m4a' };

function _toBuf(b) {
  if (Buffer.isBuffer(b)) return b;
  if (b instanceof ArrayBuffer) return Buffer.from(new Uint8Array(b));
  if (b && b.buffer instanceof ArrayBuffer) return Buffer.from(b.buffer, b.byteOffset || 0, b.byteLength);
  return Buffer.alloc(0);
}

function _extFor(name, type) {
  const e = path.extname(String(name || '')).toLowerCase();
  if (_EXT_OK.test(e) && _TYPES[e]) return e === '.jpeg' ? '.jpg' : e;
  return _EXT_OF[String(type || '').split(';')[0].trim().toLowerCase()] || '.bin';
}

/** store(dir, bytes, name, type) → the path part of the address ('/__memelocal/<sha>.<ext>'). */
function store(dir, bytes, name, type) {
  const buf = _toBuf(bytes);
  if (!buf.length) throw new Error('empty file');
  if (buf.length > MAX_SOURCE_BYTES) throw new Error('that file is too large');
  const file = crypto.createHash('sha256').update(buf).digest('hex') + _extFor(name, type);
  fs.mkdirSync(dir, { recursive: true, mode: 0o700 });
  const full = path.join(dir, file);
  if (!fs.existsSync(full)) {
    const tmp = full + '.part' + process.pid;
    fs.writeFileSync(tmp, buf, { mode: 0o600 });
    fs.renameSync(tmp, full);
  }
  return LOCAL_PREFIX + file;
}

/** The file behind a stored address (a full URL or its path), or '' — never anything outside `dir`. */
function localFile(dir, ref) {
  let p = String(ref || '');
  try { if (/^[a-z]+:/i.test(p)) p = new URL(p).pathname; } catch (_) { return ''; }
  if (!p.startsWith(LOCAL_PREFIX)) return '';
  const name = p.slice(LOCAL_PREFIX.length);
  if (!/^[0-9a-f]{64}\.[a-z0-9]{1,6}$/.test(name)) return '';
  const full = path.join(dir, name);
  return fs.existsSync(full) ? full : '';
}

function contentType(file) { return _TYPES[path.extname(file).toLowerCase()] || 'application/octet-stream'; }

function available(appDir = APP_DIR) {
  try {
    return fs.existsSync(path.join(appDir, 'app', 'services', 'meme_builder_service.py'));
  } catch (_) { return false; }
}

function _run(args, opts) {
  return new Promise((resolve) => {
    let out = '', err = '', done = false;
    const child = spawn(opts.python, args, { stdio: ['ignore', 'pipe', 'pipe'], env: Object.assign({}, process.env, { PYTHONDONTWRITEBYTECODE: '1' }) });
    const timer = setTimeout(() => { if (!done) { try { child.kill('SIGKILL'); } catch (_) {} } }, opts.deadline);
    child.stdout.on('data', d => { out += d; if (out.length > 65536) out = out.slice(-65536); });
    child.stderr.on('data', d => { err += d; if (err.length > 65536) err = err.slice(-65536); });
    child.on('error', e => { done = true; clearTimeout(timer); resolve({ code: -1, out, err: String(e && e.message || e) }); });
    child.on('close', code => { done = true; clearTimeout(timer); resolve({ code, out, err }); });
  });
}

/**
 * render({edit, sources}) → {ok, mime, bytes} | {ok:false, error}
 *   edit     the Meme Builder edit list (what POST /client/meme/render receives)
 *   sources  [{key, bytes}] — key is the exact string a layer's `src`/`mask` holds; bytes a Buffer,
 *            Uint8Array or ArrayBuffer of that picture/clip
 */
async function render(job, opts = {}) {
  const appDir = opts.appDir || APP_DIR;
  const storeDir = opts.storeDir || '';
  if (!available(appDir)) return { ok: false, error: 'the renderer is not installed on this machine' };
  const edit = job && job.edit;
  if (!edit || typeof edit !== 'object' || !Array.isArray(edit.layers)) return { ok: false, error: 'no edit' };
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'pc-meme-'));
  try {
    const sources = {};
    let total = 0, n = 0;
    // A layer kept on this machine resolves to its file; nothing is copied.
    for (const l of edit.layers) {
      for (const k of [l && l.src, l && l.mask]) {
        if (typeof k === 'string' && k && storeDir) { const f = localFile(storeDir, k); if (f) sources[k] = f; }
      }
    }
    for (const s of (job.sources || [])) {
      if (!s || typeof s.key !== 'string' || !s.key || sources[s.key]) continue;
      const buf = _toBuf(s.bytes);
      total += buf.length;
      if (total > MAX_SOURCE_BYTES) return { ok: false, error: 'the pictures are too large to render here' };
      // KEEP the extension, as the server does: a VP9-alpha .webm decodes by it, or its alpha is lost.
      let ext = '';
      try { ext = path.extname(new URL(s.key, 'app://x/').pathname).toLowerCase(); } catch (_) {}
      const file = path.join(dir, 'src' + (n++) + (_EXT_OK.test(ext) ? ext : ''));
      fs.writeFileSync(file, buf);
      sources[s.key] = file;
    }
    const out = path.join(dir, 'out');
    const jobFile = path.join(dir, 'job.json');
    fs.writeFileSync(jobFile, JSON.stringify({ app: appDir, edit, sources, out }));
    const r = await _run(['-c', RUNNER, jobFile], { python: opts.python || PYTHON, deadline: opts.deadline || DEADLINE_MS });
    if (r.code !== 0 || !fs.existsSync(out)) {
      const last = String(r.err || '').trim().split('\n').filter(Boolean).pop() || ('renderer exited ' + r.code);
      return { ok: false, error: last.replace(/^\w*Error:\s*/, '').slice(0, 300) };
    }
    let mime = 'application/octet-stream';
    try { mime = JSON.parse(r.out.trim().split('\n').pop()).ct || mime; } catch (_) {}
    return { ok: true, mime, bytes: fs.readFileSync(out) };
  } finally {
    try { fs.rmSync(dir, { recursive: true, force: true }); } catch (_) {}
  }
}

module.exports = { available, render, store, localFile, contentType, LOCAL_PREFIX, RUNNER };
