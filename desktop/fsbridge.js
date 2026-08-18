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

const TRASH_DIR = '.pc-trash';
const IGNORE = new Set([TRASH_DIR, '.git', 'node_modules', '.DS_Store', 'Thumbs.db',
                        '.Trash', '$RECYCLE.BIN', 'System Volume Information',
                        // Another sync engine's bookkeeping is not content — same rule as our own
                        // .pc-trash. A synced .stfolder marker breaks Syncthing on the machines it
                        // lands on, and one marker file was published without an address and then
                        // failed every fetch on every other device.
                        '.stfolder', '.stversions', '.stignore']);
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
/* POSITIVE PROOF OF A DELETION, or nothing. The engine used to infer "the user deleted this"
 * from "the scan did not list it" — and every way a scan fails to SEE (unmounted drive, permission
 * loss, a flaky listing) became a published deletion. A tombstone now requires this answer:
 * the exact path is ENOENT while its parent directory stats healthy. Anything else is UNKNOWN,
 * which deletes nothing anywhere. */
async function confirmGone(id, rel){
  try{
    const abs = await resolveIn(id, rel);
    const rootAbs = await resolveIn(id, '');
    try{ await fsp.stat(abs); return { gone: false, parentAlive: true }; }
    catch(e){
      if(!e || e.code !== 'ENOENT') return { gone: false, parentAlive: false };   // EACCES/EIO
      /* WALK UP. A deleted FOLDER takes its children's parents with it, so "is the parent
       * healthy" answered no for a genuine whole-directory deletion and the tombstones were held
       * for ever ("i wanted to simulate a restore event" — deleting .ssh outright parked six
       * deletions as unconfirmable). The proof for a subtree: climb until an ancestor stats,
       * and require the topmost MISSING segment to be ENOENT under that live ancestor. Reaching
       * the folder root without a live ancestor is the unmount case, and stays unprovable. */
      let cur = abs;
      while(true){
        const up = path.dirname(cur);
        if(cur === up || !up.startsWith(rootAbs)) return { gone: false, parentAlive: false };
        try{
          await fsp.stat(up);                              // live ancestor found
          try{ await fsp.stat(cur); return { gone: false, parentAlive: false }; }  // raced back?
          catch(e2){
            return (e2 && e2.code === 'ENOENT') ? { gone: true, parentAlive: true }
                                                : { gone: false, parentAlive: false };
          }
        }catch(e3){
          if(!e3 || e3.code !== 'ENOENT') return { gone: false, parentAlive: false };
          cur = up;                                        // parent missing too — keep climbing
        }
      }
    }
  }catch(_){ return { gone: false, parentAlive: false }; }
}

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
/* WHICH PART FILES ARE OURS.
 *
 * Kept inside `.pc-trash`, which this adapter already owns and the scan already ignores, so it
 * survives a restart (the whole point of resuming a download) without putting anything new in the
 * user's way. Best-effort throughout: a failure to record costs a leaked temp file, never a file. */
const PARTS_LIST = '.parts.json';
function partsFile(root){ return path.join(root.dir, '.pc-trash', PARTS_LIST); }
async function readParts(root){
  try{ return JSON.parse(await fsp.readFile(partsFile(root), 'utf8')) || {}; }
  catch(_){ return {}; }
}
async function notePart(root, abs){
  try{
    const list = await readParts(root);
    if(list[abs]) return;
    list[abs] = Date.now();
    await fsp.mkdir(path.dirname(partsFile(root)), { recursive: true });
    await fsp.writeFile(partsFile(root), JSON.stringify(list));
  }catch(_){ }
}
async function forgetPart(root, abs){
  try{
    const list = await readParts(root);
    if(!(abs in list)) return;
    delete list[abs];
    await fsp.writeFile(partsFile(root), JSON.stringify(list));
  }catch(_){ }
}
async function knownPart(root, abs){
  const list = await readParts(root);
  return Object.prototype.hasOwnProperty.call(list, abs);
}

async function writePart(id, rel, offset, bytes){
  const abs = await resolveIn(id, rel);
  await fsp.mkdir(path.dirname(abs), { recursive: true });
  const tmp = abs + PART;
  { const root = roots.find(r => r.id === id); if(root) await notePart(root, tmp); }
  const off = Number(offset) || 0;
  const fh = await fsp.open(tmp, off === 0 ? 'w' : 'r+').catch(async (e) => {
    if(off === 0) throw e;
    return await fsp.open(tmp, 'w+');        // a resumed write whose part file went missing
  });
  try{ await fh.write(Buffer.from(bytes), 0, bytes.length, off); }
  finally { await fh.close(); }
  return true;
}

/* VERIFY BEFORE YOU OVERWRITE, which is why this hashes the `.part` file and not the target.
 *
 * A download used to be recorded as agreed carrying the REMOTE's csum, without anyone ever asking
 * what actually landed on disk — so a file with the right length and the wrong bytes was written
 * over a good copy and then asserted correct by `base` for ever. Nothing would look at it again
 * short of a Deep check.
 *
 * Hashing the part file is the only ordering that can refuse: writeCommit has already renamed the
 * new file into place and trashed the old one, so a check after it is a report, not a defence. */
function hashPart(id, rel){
  return resolveIn(id, rel).then(abs => sha256(abs + PART));
}

/* THE CONTENT IDENTITY OF A WHOLE FILE, so what this device uploads can be VERIFIED by whatever
 * receives it.
 *
 * `csum` is what the far side checks a download against — verifyPart returns early without one — and
 * a chunked upload only gets one if the adapter can produce it. Android can (SafFs.sha256Of); the
 * desktop could not, so every large file it sent arrived somewhere unverifiable, and a truncated or
 * mis-assembled video was written and played. That is not hypothetical: it is what happened.
 *
 * Streamed, like sha256 above — a 4 GB video is never in memory. */
function hashFile(id, rel){
  return resolveIn(id, rel).then(abs => sha256(abs));
}

/* How much of an interrupted download is already on disk, so the next attempt can carry on.
 *
 * A download used to restart at byte zero every time: getParts walks the chunk list from the
 * beginning and re-fetches all of it. Uploads have always resumed — a chunk is content-addressed and
 * skipped if the server already holds it — but the receiving side had no equivalent, so a network
 * drop at 95%% of an 8 GB video cost the whole 8 GB, again, and on a link that drops it may never
 * finish at all.
 *
 * 0 when there is nothing to resume, which is also the answer for a missing file. */
async function partSize(id, rel){
  try{
    const abs = await resolveIn(id, rel);
    const st = await fsp.stat(abs + PART);
    return st.isFile() ? st.size : 0;
  }catch(_){ return 0; }
}

/* Remove abandoned `.part` files — the ones no download is coming back for.
 *
 * They are invisible to everything: `ignored()` keeps them out of the scan (rightly — a half-written
 * file must never be uploaded), so nothing ever looked at them again and an interrupted download
 * left its bytes on the disk for good. On a folder of videos that is real money.
 *
 * `olderThanMs` is what makes it safe: a sweep in flight has just touched its own part file, so only
 * ones untouched for far longer than any sweep can be running are taken. */
async function sweepParts(id, olderThanMs){
  const root = roots.find(r => r.id === id);
  if(!root) throw new Error('unknown sync folder');
  const base = await fsp.realpath(root.dir);
  const cutoff = Date.now() - (olderThanMs || 24 * 3600000);
  /* Read ONCE, pruned at the end. `knownPart` re-read and re-parsed the whole register for every
   * `.pcpart` the walk met, and nothing ever shrank it — so a folder that had synced for months
   * carried a map of every part file it had ever made, re-parsed per candidate. */
  const reg = await readParts(root);
  const gone = [];
  let removed = 0, bytes = 0;
  async function walk(dir){
    let ents;
    try{ ents = await fsp.readdir(dir, { withFileTypes: true }); }catch(_){ return; }
    for(const e of ents){
      const p = path.join(dir, e.name);
      if(e.isDirectory()){ if(!IGNORE.has(e.name)) await walk(p); continue; }
      if(!e.isFile() || !e.name.endsWith(PART)) continue;
      /* OURS, NOT ANYTHING THAT HAPPENS TO END IN `.pcpart`.
       *
       * This walks somebody's Documents folder deleting files, so matching on the extension alone is
       * not good enough: a user's own `notes.pcpart` is invisible to the scan (part files are
       * ignored, exactly so a half-written download is never uploaded), which means sync has never
       * touched it — and this would delete it a day later, with no report and no copy in .pc-trash.
       *
       * So the sweep only removes part files this adapter recorded creating. One it did not — from a
       * build before the register existed, or a crash between creating and recording — is left
       * alone. Leaking a temp file is a cost; deleting somebody's file is not a cost, it is the
       * thing that must not happen. */
      if(!Object.prototype.hasOwnProperty.call(reg, p)) continue;
      let st; try{ st = await fsp.stat(p); }catch(_){ continue; }
      if(st.mtimeMs >= cutoff) continue;                 // something may still be writing it
      try{ await fsp.rm(p, { force: true, maxRetries: 3, retryDelay: 100 }); removed++; bytes += st.size; gone.push(p); }
      catch(_){}
    }
  }
  await walk(base);
  /* Forget what was collected, and anything the register names that is no longer on disk — a part
   * file that was committed by a build before the register existed, or one removed by hand. A
   * register that only grows is the leak this exists to stop, one level up. */
  try{
    let changed = gone.length > 0;
    for(const q of gone) delete reg[q];
    for(const q of Object.keys(reg)){
      try{ await fsp.stat(q); }
      catch(_){ delete reg[q]; changed = true; }
    }
    if(changed) await fsp.writeFile(partsFile(root), JSON.stringify(reg));
  }catch(_){ }
  return { removed, bytes };
}

/* Throw away a download that did not verify. Not into `.pc-trash` — this is not somebody's file, it
 * is bytes we could not confirm, and putting them in the safety net makes the net less trustworthy. */
async function discardPart(id, rel){
  const abs = await resolveIn(id, rel);
  try{ await fsp.rm(abs + PART, { force: true, maxRetries: 3, retryDelay: 100 }); }catch(_){}
  { const root = roots.find(r => r.id === id); if(root) await forgetPart(root, abs + PART); }
  return true;
}

async function writeCommit(id, rel, mtime){
  const abs = await resolveIn(id, rel);
  const tmp = abs + PART;
  const fh = await fsp.open(tmp, 'r+');
  try{ await fh.sync(); }                    // the rename is only atomic if the BYTES landed first
  finally { await fh.close(); }
  await fsp.rename(tmp, abs);
  { const root = roots.find(r => r.id === id); if(root) await forgetPart(root, tmp); }
  if(mtime) { try{ const t = new Date(mtime); await fsp.utimes(abs, t, t); }catch(_){} }
  const st = await fsp.stat(abs);
  return { size: st.size, mtime: Math.floor(st.mtimeMs) };
}

async function write(id, rel, bytes, mtime){
  const abs = await resolveIn(id, rel);
  await fsp.mkdir(path.dirname(abs), { recursive: true });
  const tmp = abs + PART;
  // Registered like the chunked path's: this is the majority of receives, and without it the sweep's
  // ownership gate skips every part file a crash between the open and the rename leaves behind.
  { const root = roots.find(r => r.id === id); if(root) await notePart(root, tmp); }
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
  const landed = path.relative(await fsp.realpath(roots.find(r => r.id === id).dir), abs)
                     .split(path.sep).join('/');
  // The directory the file just left may now be empty, and so may its parents. See pruneEmptyDirs.
  await pruneEmptyDirs(id, path.dirname(src));
  return landed;
}

/* THE DIRECTORIES A DELETE LEAVES BEHIND, which is what "the folder is still there" was.
 *
 * A manifest holds PATHS, never directories — a folder in the Blossom view is only the common
 * prefix of the files under it. So deleting one tombstones every file it contains, each device
 * trashes those files, and the directory tree they lived in is left standing: empty, on disk,
 * exactly where the user just deleted it. `PDF Project/1/venv` with nothing in it.
 *
 * `rmdir`, NEVER `rm -r`, AND THAT IS THE WHOLE SAFETY ARGUMENT. rmdir refuses a directory that is
 * not empty, so this physically cannot remove a file — not one this sweep missed, not one another
 * program wrote a moment ago, not one that was never ours. The worst case is a directory that stays.
 * A recursive delete here would be the only unguarded destructive path in the whole feature.
 *
 * It walks UP, because deleting `a/b/c/x.txt` empties `c`, which may empty `b`, which may empty `a`.
 * It stops at the first directory that will not go (non-empty, in use, permission), and it stops at
 * the sync ROOT, which is never removed however empty it gets: the root is the pairing itself, and
 * a device that deleted it would have to re-pick the folder to sync again.
 */
async function pruneEmptyDirs(id, dir){
  const root = roots.find(r => r.id === id);
  if(!root) return 0;
  let base;
  try{ base = await fsp.realpath(root.dir); }catch(_){ return 0; }
  let cur;
  try{ cur = await fsp.realpath(dir); }catch(_){ return 0; }
  let gone = 0;
  // Bounded: a path cannot have more components than this, and a symlinked parent must not turn the
  // walk into a loop.
  for(let i = 0; i < 64; i++){
    if(cur === base) break;                             // never the root
    const inside = cur.startsWith(base + path.sep);
    if(!inside) break;                                  // outside the pairing — not ours to touch
    if(path.basename(cur) === TRASH_DIR) break;         // the safety net is not swept by the sweep
    try{ await fsp.rmdir(cur); }catch(_){ break; }      // not empty, or held open: stop here
    gone++;
    cur = path.dirname(cur);
  }
  return gone;
}

/* Empty trash older than N days. Deliberately explicit and NOT automatic: the whole value of the
 * trash is that it outlives the mistake, and "safe deletes" that quietly become an unbounded second
 * copy of everything you ever removed is the other failure. The caller decides. */
/* `olderThanDays === 0` MEANS EVERYTHING, and it has to mean something.
 *
 * The trash lives INSIDE the synced root, so it is counted by every "how big is this folder?" anyone
 * asks — Explorer, a disk usage tool, a quota. Every layer used to hardcode 30 and there was no
 * automatic sweep for that floor to serve, so the only caller was a button labelled "Empty trash"
 * that could not empty trash: delete 40 GB of pictures, press it, and it removes nothing and reports
 * success. The space is unreclaimable from inside the app for a month, and the only way out is
 * deleting `.pc-trash` by hand in a file manager — which is the app telling the user to go round it.
 *
 * `|| 30` is what did that, because it cannot tell 0 from absent. An explicit 0 is now honoured and
 * only a MISSING value falls back to the safety window.
 *
 * Reports bytes as well as days, because "emptied 0 day(s)" is what made the old one look broken and
 * "freed 40.2 GB" is the only answer to the question actually being asked. Sized before removal:
 * a trash directory is small in file count next to the folder it belongs to, and the walk is what
 * lets the caller state the cost before it is paid. */
async function trashSize(base){
  let files = 0, bytes = 0;
  const walk = async (dir) => {
    let ents;
    try{ ents = await fsp.readdir(dir, { withFileTypes: true }); }catch(_){ return; }
    for(const e of ents){
      const p = path.join(dir, e.name);
      if(e.isDirectory()) await walk(p);
      else { files++; try{ bytes += (await fsp.stat(p)).size; }catch(_){} }
    }
  };
  await walk(base);
  return { files, bytes };
}

async function emptyTrash(id, olderThanDays){
  const base = await resolveIn(id, '.pc-trash');
  const days = (olderThanDays === 0 || olderThanDays === '0') ? 0 : (olderThanDays || 30);
  const cutoff = Date.now() - days * 86400000;
  let removed = 0, files = 0, bytes = 0;
  const failed = [];
  let entries;
  try{ entries = await fsp.readdir(base, { withFileTypes: true }); }catch(_){ return { removed: 0, files: 0, bytes: 0, failed: [] }; }
  for(const d of entries){
    if(!d.isDirectory()) continue;
    /* EVERYTHING means everything — the name is not consulted at all when days is 0.
     *
     * Two directories survive a date comparison forever and both are real: one dated in the FUTURE
     * (a device whose clock was wrong when it trashed something), and one whose name does not parse
     * as a date at all, which `!isFinite(when)` skips on every run. Either is a permanent leak in
     * the one place a user goes to reclaim space, and neither is visible — the folder is simply
     * still there and still counted. Asking the name a question is only meaningful for the
     * retention window. */
    if(days > 0){
      const when = Date.parse(d.name + 'T00:00:00Z');
      if(!isFinite(when) || when >= cutoff) continue;
    }
    const dir = path.join(base, d.name);
    const sz = await trashSize(dir);
    /* ONE LOCKED FILE MUST NOT COST THE WHOLE EMPTY, and on Windows it did.
     *
     * `fsp.rm` was unguarded, so the first EBUSY/EPERM threw straight out of the loop: every
     * remaining day was skipped and the caller reported "action failed" having deleted an arbitrary
     * part of one directory. That is the likeliest single failure here, not an unlikely one — a
     * folder of pictures is exactly what Explorer's preview pane, the Windows Search indexer,
     * OneDrive and every antivirus hold handles on, and the user is looking at the folder while
     * pressing the button.
     *
     * `maxRetries` exists in Node for precisely this and was not being used. Past that the day is
     * recorded as failed and the sweep CARRIES ON, because emptying nine of ten days is worth far
     * more than a clean error — and what actually came off is measured by re-walking the directory
     * rather than assumed, so a partial removal reports the space it really freed. */
    let err = null;
    try{ await fsp.rm(dir, { recursive: true, force: true, maxRetries: 5, retryDelay: 150 }); }
    catch(e){ err = e; }
    if(err){
      const left = await trashSize(dir);              // what survived, if anything
      files += Math.max(0, sz.files - left.files);
      bytes += Math.max(0, sz.bytes - left.bytes);
      failed.push({ day: d.name, why: err.code || String(err.message || err), files: left.files });
      continue;
    }
    removed++; files += sz.files; bytes += sz.bytes;
  }
  return { removed, files, bytes, failed };
}

/* What is sitting in the trash right now, so the confirmation can state the cost rather than guess
 * it. Read-only; the caller decides. */
/* Everything currently in the trash, as [{at, to}] — `at` the path inside .pc-trash, `to` where it
 * came from (the day segment stripped). The RESTORE lives in the client over the ordinary move(),
 * so both platforms share one restore and the bridge only enumerates. */
async function listTrash(id){
  const base = await resolveIn(id, '.pc-trash');
  const out = [];
  async function walk(abs, rel){
    let ents = [];
    try{ ents = await fsp.readdir(abs, { withFileTypes: true }); }catch(_){ return; }
    for(const e of ents){
      if(e.name === PARTS_LIST) continue;
      const r = rel ? rel + '/' + e.name : e.name;
      if(e.isDirectory()) await walk(path.join(abs, e.name), r);
      else if(e.isFile() && !e.name.endsWith(PART)){
        const cut = r.indexOf('/');
        if(cut > 0) out.push({ at: '.pc-trash/' + r, to: r.slice(cut + 1) });
      }
    }
  }
  await walk(base, '');
  return out;
}

async function trashStat(id){
  const base = await resolveIn(id, '.pc-trash');
  const out = await trashSize(base);
  let days = 0;
  try{ days = (await fsp.readdir(base, { withFileTypes: true })).filter(d => d.isDirectory()).length; }catch(_){}
  return Object.assign(out, { days });
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
                   readPart, writePart, writeCommit, confirmGone, listTrash,
                   read, write, move, trash, emptyTrash, trashStat, hashPart, hashFile, discardPart, partSize, sweepParts, watch, unwatch, IGNORE };
