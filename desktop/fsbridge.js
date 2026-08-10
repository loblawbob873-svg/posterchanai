/* Folder sync — the desktop filesystem adapter.
 *
 * The engine (static/js/client/foldersync.js) decides WHAT should happen; this module is the only
 * thing that may touch a real file, and it is deliberately dumb: list, read, write, move, trash.
 * Every decision lives in the pure module where it can be tested; every irreversible act lives here
 * where it can be confined.
 *
 * CONFINEMENT IS THE WHOLE POINT OF THIS FILE. The renderer loads the instance's own JavaScript over
 * the network — that is the trust boundary the rest of the app is built around, and handing it
 * `fs.writeFile(path)` would tear it down completely. So:
 *
 *   * a path is only reachable inside a ROOT the user chose in a native folder picker, this session
 *     or a previous one (roots persist in the app's own config, not in anything the page can write);
 *   * every path from the page is resolved and re-checked against those roots with a real prefix
 *     test on the RESOLVED path, so `..`, an absolute path, or a symlink pointing out of the tree
 *     cannot escape (realpath, not string maths — a symlink is the interesting case and string
 *     comparison misses it entirely);
 *   * nothing here can create a root. Only a native dialog can, which means only a human can.
 *
 * ATOMIC WRITES, ALWAYS. A sync that half-writes a file during a crash or a power cut has corrupted
 * data that looks like data — worse than no sync. Write to `<name>.pcpart`, fsync, rename over the
 * target: rename is atomic on every filesystem this ships to, so a reader sees the old file or the
 * new one and never a torn one.
 *
 * NOTHING IS DELETED. `trash()` moves into `.pc-trash/<date>/` inside the same root, which is also
 * why it is one rename rather than a copy+unlink: same filesystem, instant, and no window in which
 * the file exists nowhere.
 */
'use strict';

const fs = require('fs');
const fsp = fs.promises;
const path = require('path');
const crypto = require('crypto');

const IGNORE = new Set(['.pc-trash', '.git', 'node_modules', '.DS_Store', 'Thumbs.db',
                        '.Trash', '$RECYCLE.BIN', 'System Volume Information']);
/* Half-written files, by the names the tools that make them use.
 *
 * This is the substitute for file locking, and it has to be, because there is nothing to lock
 * AGAINST: flock on Linux and macOS is advisory and no ordinary application takes it, so locking
 * would exclude us and nobody else. Word, LibreOffice, browsers and rsync all announce a file in
 * flight by its NAME instead, and that is a signal we can actually read. */
const TEMP_RX = /^(~\$|\.~lock\.)|\.(crdownload|part|partial|tmp|temp|swp|swx|download)$/i;
const PART = '.pcpart';

let roots = [];          // [{id, dir}] — absolute, real, user-chosen
let saveRoots = () => {};

function init(opts){
  roots = ((opts && opts.roots) || []).filter(r => r && r.id && r.dir);
  saveRoots = (opts && opts.save) || (() => {});
}
function list(){ return roots.map(r => ({ id: r.id, dir: r.dir })); }

/* Resolve `rel` inside root `id`, or throw. The realpath dance is the load-bearing part: a symlink
 * inside a synced folder that points at ~/.ssh is a normal thing for a filesystem to contain and a
 * catastrophic thing for a sync to follow. For a path that does not exist yet (every download) the
 * nearest existing ANCESTOR is what gets realpath'd, since the file itself cannot be resolved. */
async function resolveIn(id, rel){
  const root = roots.find(r => r.id === id);
  if(!root) throw new Error('unknown sync folder');
  const base = await fsp.realpath(root.dir);
  const want = path.resolve(base, String(rel || '').replace(/^[/\\]+/, ''));

  let probe = want, missing = 0;
  for(;;){
    try{ probe = await fsp.realpath(probe); break; }
    catch(_){
      const up = path.dirname(probe);
      if(up === probe || ++missing > 64) throw new Error('path does not resolve');
      probe = up;
    }
  }
  const inside = probe === base || probe.startsWith(base + path.sep);
  if(!inside) throw new Error('path escapes the sync folder');
  return want;
}

const ignored = name => IGNORE.has(name) || name.endsWith(PART) || TEMP_RX.test(name);

/* Walk a tree into the shape the engine wants: {relPath: {size, mtime}}.
 *
 * `hash` is opt-in per call because it is the difference between a background task and a space
 * heater — see shouldSync's 'full' vs 'incremental'. On an incremental pass the caller hashes only
 * the handful of paths whose size or mtime moved.
 *
 * Symlinks are SKIPPED rather than followed: following them duplicates data at best and walks out
 * of the tree (or into a cycle) at worst, and a sync that silently uploads whatever ~/Documents/link
 * points at is a data leak, not a feature. */
async function scan(id, opts){
  const root = roots.find(r => r.id === id);
  if(!root) throw new Error('unknown sync folder');
  const base = await fsp.realpath(root.dir);
  const withHash = !!(opts && opts.hash);
  const maxBytes = (opts && opts.maxBytes) || 0;
  /* User exclusions ("all of Pictures except Old"). Applied during the WALK, not after it, so an
   * excluded folder is never read, never stat'd and never hashed — on a Pictures/Old holding a
   * decade of photos that is the difference between a sweep and a chore. The engine applies the
   * same patterns to the manifest; this is only the cheap half. */
  const excl = (opts && opts.excludes) || [];
  const isExcluded = excl.length ? require('./excludes').excluder(excl) : () => false;
  const out = {}, skipped = [];

  async function walk(dir){
    let entries;
    try{ entries = await fsp.readdir(dir, { withFileTypes: true }); }
    catch(e){ skipped.push({ path: path.relative(base, dir), why: e.code || 'unreadable' }); return; }
    for(const ent of entries){
      if(ignored(ent.name)) continue;
      const abs = path.join(dir, ent.name);
      if(ent.isSymbolicLink()){ skipped.push({ path: path.relative(base, abs), why: 'symlink' }); continue; }
      const relDir = path.relative(base, abs).split(path.sep).join('/');
      if(ent.isDirectory()){ if(!isExcluded(relDir)) await walk(abs); continue; }
      if(!ent.isFile()) continue;
      let st;
      try{ st = await fsp.stat(abs); }
      catch(e){ skipped.push({ path: path.relative(base, abs), why: e.code || 'unreadable' }); continue; }
      const rel = relDir;
      if(isExcluded(rel)) continue;
      if(maxBytes && st.size > maxBytes){ skipped.push({ path: rel, why: 'too big', size: st.size }); continue; }
      const e = { size: st.size, mtime: Math.floor(st.mtimeMs) };
      if(withHash){
        /* HASH, THEN LOOK AGAIN. A file being written while we read it hashes to bytes that were
         * never a whole file, and uploading that is worse than skipping it — the other devices get a
         * corrupt copy with a perfectly good checksum. Re-stat afterwards: if size or mtime moved,
         * something else owns this file right now, so leave it and take it on the next sweep. A
         * delay is recoverable; a torn upload is not. */
        e.sha = await sha256(abs);
        let after;
        try{ after = await fsp.stat(abs); }catch(_){ skipped.push({ path: rel, why: 'vanished' }); continue; }
        if(after.size !== st.size || Math.floor(after.mtimeMs) !== Math.floor(st.mtimeMs)){
          skipped.push({ path: rel, why: 'in use — will try again' });
          continue;
        }
      }
      out[rel] = e;
    }
  }
  await walk(base);
  return { files: out, skipped };
}

// Streamed, so hashing a 4GB video does not put 4GB in the main process's heap.
function sha256(abs){
  return new Promise((res, rej) => {
    const h = crypto.createHash('sha256');
    fs.createReadStream(abs).on('error', rej).on('data', d => h.update(d))
      .on('end', () => res(h.digest('hex')));
  });
}

async function read(id, rel){
  const abs = await resolveIn(id, rel);
  return await fsp.readFile(abs);          // Buffer → the preload hands the page a Uint8Array
}

/* ---- PART I/O: a file too big to hold in one piece ------------------------------------------
 *
 * Uploading a file used to mean reading all of it, encrypting all of it, and handing the result to
 * fetch — the plaintext, the ciphertext and the Blob alive at once, three to four times the file's
 * size in the renderer. A 2 GB document asked for ~7 GB and Chromium killed the window instead
 * (which in the app is a black screen, and on the way down its in-flight requests fail in the way
 * the console calls a CORS error).
 *
 * These read and write SLICES, so the renderer only ever holds one chunk. Deliberately stateless:
 * each call carries its own offset, because the alternative is an open file handle living across
 * IPC calls, and then a renderer that dies mid-transfer leaks it.
 */
async function readPart(id, rel, offset, len){
  const abs = await resolveIn(id, rel);
  const fh = await fsp.open(abs, 'r');
  try{
    const buf = Buffer.allocUnsafe(Math.max(0, len | 0));
    const { bytesRead } = await fh.read(buf, 0, buf.length, Number(offset) || 0);
    return bytesRead === buf.length ? buf : buf.subarray(0, bytesRead);
  } finally { await fh.close(); }
}

/* Writes land in the SAME `.part` file `write()` uses, at an offset, and are only renamed into place
 * by writeCommit. So an interrupted download leaves a partial `.part` and never a half-written file
 * under the real name — the rule the whole-file path already follows, kept for the chunked one. */
async function writePart(id, rel, offset, bytes){
  const abs = await resolveIn(id, rel);
  await fsp.mkdir(path.dirname(abs), { recursive: true });
  const tmp = abs + PART;
  const off = Number(offset) || 0;
  const fh = await fsp.open(tmp, off === 0 ? 'w' : 'r+').catch(async (e) => {
    if(off === 0) throw e;
    return await fsp.open(tmp, 'w+');        // a resumed write whose part file went missing
  });
  try{ await fh.write(Buffer.from(bytes), 0, bytes.length, off); }
  finally { await fh.close(); }
  return true;
}

async function writeCommit(id, rel, mtime){
  const abs = await resolveIn(id, rel);
  const tmp = abs + PART;
  const fh = await fsp.open(tmp, 'r+');
  try{ await fh.sync(); }                    // the rename is only atomic if the BYTES landed first
  finally { await fh.close(); }
  await fsp.rename(tmp, abs);
  if(mtime) { try{ const t = new Date(mtime); await fsp.utimes(abs, t, t); }catch(_){} }
  const st = await fsp.stat(abs);
  return { size: st.size, mtime: Math.floor(st.mtimeMs) };
}

async function write(id, rel, bytes, mtime){
  const abs = await resolveIn(id, rel);
  await fsp.mkdir(path.dirname(abs), { recursive: true });
  const tmp = abs + PART;
  const fh = await fsp.open(tmp, 'w');
  try{
    await fh.write(Buffer.from(bytes));
    await fh.sync();                        // the rename is only atomic if the BYTES landed first
  } finally { await fh.close(); }
  await fsp.rename(tmp, abs);
  // Carry the source mtime so the next scan does not see our own download as a local edit and push
  // it straight back — the loop that makes a sync never settle.
  if(mtime) { try{ const t = new Date(mtime); await fsp.utimes(abs, t, t); }catch(_){} }
  const st = await fsp.stat(abs);
  return { size: st.size, mtime: Math.floor(st.mtimeMs) };
}

async function move(id, from, to){
  const a = await resolveIn(id, from), b = await resolveIn(id, to);
  await fsp.mkdir(path.dirname(b), { recursive: true });
  await fsp.rename(a, b);
  return true;
}

/* Delete = move into .pc-trash/<date>/, inside the same root so it is one atomic rename. A name
 * already in today's trash gets a counter rather than clobbering — the trash is the safety net, and
 * a safety net that overwrites itself is not one. */
async function trash(id, rel, when){
  const src = await resolveIn(id, rel);
  const d = new Date(when || Date.now());
  const day = d.getUTCFullYear() + '-' + String(d.getUTCMonth() + 1).padStart(2, '0')
            + '-' + String(d.getUTCDate()).padStart(2, '0');
  let dest = path.posix.join('.pc-trash', day, String(rel).split(path.sep).join('/'));
  let abs = await resolveIn(id, dest);
  for(let n = 2; n < 1000; n++){
    try{ await fsp.access(abs); }catch(_){ break; }      // free
    const ext = path.extname(dest), stem = dest.slice(0, dest.length - ext.length);
    abs = await resolveIn(id, stem + ' (' + n + ')' + ext);
  }
  await fsp.mkdir(path.dirname(abs), { recursive: true });
  await fsp.rename(src, abs);
  return path.relative(await fsp.realpath(roots.find(r => r.id === id).dir), abs)
             .split(path.sep).join('/');
}

/* Empty trash older than N days. Deliberately explicit and NOT automatic: the whole value of the
 * trash is that it outlives the mistake, and "safe deletes" that quietly become an unbounded second
 * copy of everything you ever removed is the other failure. The caller decides. */
async function emptyTrash(id, olderThanDays){
  const base = await resolveIn(id, '.pc-trash');
  const cutoff = Date.now() - (olderThanDays || 30) * 86400000;
  let removed = 0;
  let days;
  try{ days = await fsp.readdir(base, { withFileTypes: true }); }catch(_){ return { removed: 0 }; }
  for(const d of days){
    if(!d.isDirectory()) continue;
    const when = Date.parse(d.name + 'T00:00:00Z');
    if(!isFinite(when) || when >= cutoff) continue;
    await fsp.rm(path.join(base, d.name), { recursive: true, force: true });
    removed++;
  }
  return { removed };
}

/* A change notifier, NOT a change list. fs.watch is famously inconsistent across platforms (it
 * fires per-file on Linux, per-directory on macOS, coalesces differently on Windows, and misses
 * events under load), so nothing here tries to build an event stream out of it. It says "something
 * under this root moved, look again when your policy next allows" — and the scan, which is
 * authoritative, does the rest. That also means a missed event costs a delay, never a lost file. */
const watchers = new Map();
function watch(id, onChange, debounceMs){
  const root = roots.find(r => r.id === id);
  if(!root) throw new Error('unknown sync folder');
  unwatch(id);
  let t = null;
  const w = fs.watch(root.dir, { recursive: true, persistent: false }, (_ev, name) => {
    if(name && ignored(path.basename(String(name)))) return;
    clearTimeout(t);
    t = setTimeout(() => { try{ onChange(id); }catch(_){} }, debounceMs || 4000);
  });
  w.on('error', () => {});   // a watch dying must never take the process with it; the sweep still runs
  watchers.set(id, { w, clear: () => clearTimeout(t) });
  return true;
}
function unwatch(id){
  const cur = watchers.get(id);
  if(!cur) return false;
  try{ cur.clear(); cur.w.close(); }catch(_){}
  watchers.delete(id);
  return true;
}

function addRoot(dir){
  const abs = path.resolve(dir);
  const found = roots.find(r => r.dir === abs);
  if(found) return { id: found.id, dir: abs };
  const id = crypto.randomBytes(8).toString('hex');
  roots.push({ id, dir: abs });
  saveRoots(roots);
  return { id, dir: abs };
}
function removeRoot(id){
  unwatch(id);
  const n = roots.length;
  roots = roots.filter(r => r.id !== id);
  if(roots.length !== n) saveRoots(roots);
  return roots.length !== n;
}

module.exports = { init, list, addRoot, removeRoot, resolveIn, scan, sha256,
                   readPart, writePart, writeCommit,
                   read, write, move, trash, emptyTrash, watch, unwatch, IGNORE };
