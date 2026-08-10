/* Two devices, one manifest — the situation folder sync has never actually been in.
 *
 * Every test this feature has runs ONE engine: test_folder_sync.py drives diff() with a hand-written
 * "remote" snapshot, and test_sync_run.py drives the executor with an injected store. Both pass with
 * the manifest keyed on the PLATFORM's directory handle — which is device-local, so every device
 * wrote and read a different document and cross-device sync could not work at all. A hand-written
 * remote snapshot is the one input that cannot expose that: the test supplies the very thing the
 * bug prevents two devices from sharing.
 *
 * So this file runs TWO devices for real. Each gets its own in-memory filesystem, its own `base`, and
 * its own platform id (a random hex handle like the desktop's, a SAF tree URI like Android's) — the
 * only thing they share is the manifest store, addressed exactly the way the server addresses it
 * (`_sync_folder_key` in app/routers/client.py), and a Blossom blob store. Both run the SHIPPED
 * foldersync.js and syncrun.js. Nothing about the pairing is handed to them.
 *
 * This is not a substitute for a real desktop → phone round trip: the platform adapters, the signing
 * and the encryption are all stubbed here. What it does do is take the ENGINE, the ORDER and the
 * KEYING off the list of suspects, so what remains to be proven on real hardware is the adapters.
 *
 * Usage: node two_device_sim.js   → one JSON line per scenario, non-zero exit if any failed.
 */
'use strict';
const crypto = require('crypto');
const path = require('path');

const S = require(path.join(__dirname, '..', '..', 'static', 'js', 'client', 'foldersync.js'));
const RUN = require(path.join(__dirname, '..', '..', 'static', 'js', 'client', 'syncrun.js'));

const sha256 = (bytes) => crypto.createHash('sha256').update(Buffer.from(bytes)).digest('hex');
const bytesOf = (s) => new Uint8Array(Buffer.from(String(s), 'utf8'));
const textOf = (b) => Buffer.from(b).toString('utf8');

/* The d-tag, mirrored from the server's _sync_folder_key. It is deliberately duplicated rather than
 * imported: the whole question this file answers is whether two clients that never talk to each
 * other land on the SAME document, and that is decided by what the server makes of the name each one
 * sends. A shared helper would make them agree by construction. tests/client/test_two_device_sync.py
 * cross-checks this against the real Python. */
function dtag(key){
  const f = String(key || '').replace(/[^A-Za-z0-9_-]/g, '');
  if(f.length < 4 || f.length > 64) throw new Error('the server would reject this folder key: ' + key);
  return 'pcai:sync:' + f;
}

/* ---- the shared world: one manifest store, one blob store ------------------------------------ */
function makeWorld(){
  const docs = new Map();     // d-tag -> {path: entry}   (the manifest, sealed in real life)
  const blobs = new Map();    // sha -> bytes             (encrypted Blossom, plaintext here)
  return {
    docs, blobs,
    reads: 0, writes: 0,
    manifestOf(key){ return JSON.parse(JSON.stringify(docs.get(dtag(key)) || {})); },
  };
}

/* ---- one device ------------------------------------------------------------------------------ */
/* `id` is what the platform calls this directory and is deliberately DIFFERENT on each device, the
 * way it is in life. `key` is the name the user typed on both. */
function makeDevice(world, opts){
  const name = opts.name, id = opts.id, key = opts.key;
  const files = new Map();               // relpath -> {bytes, mtime}
  const parts = new Map();               // relpath -> [{offset, bytes}] mid-download, like a .part file
  // A real wall-clock start, not a counter from zero: `.pc-trash/<date>/` and the "(conflict from
  // laptop, <date>)" suffix are both formatted from it, and a 1970 date in a report is a detail that
  // reads as a bug when someone eyeballs the output.
  let clock = opts.now || Date.UTC(2026, 7, 1, 9, 30);

  // Mirrors desktop/fsbridge.js: the trash lives INSIDE the root, and is never scanned — scanning it
  // would re-upload everything the last sweep deleted, under a new path, forever.
  const IGNORED = (p) => p.split('/')[0] === '.pc-trash';

  const fs = {
    async scan(_id, o){
      const out = {}, skipped = [];
      for(const [p, f] of files){
        if(IGNORED(p)) continue;
        if(o && o.maxBytes && f.bytes.length > o.maxBytes){ skipped.push({ path:p, why:'too big', size:f.bytes.length }); continue; }
        const e = { size: f.bytes.length, mtime: f.mtime };
        // A full sweep hashes; an incremental one trusts size+mtime and leaves `sha` undefined. Both
        // are real code paths, so both are exercised.
        if(o && o.hash) e.sha = sha256(f.bytes);
        out[p] = e;
      }
      return { files: out, skipped };
    },
    async read(_id, p){
      const f = files.get(p); if(!f) throw new Error('ENOENT ' + p);
      return f.bytes;
    },
    async write(_id, p, bytes, mtime){
      const t = mtime || ++clock;
      files.set(p, { bytes: new Uint8Array(bytes), mtime: t });
      return { size: bytes.length, mtime: t };
    },
    async move(_id, from, to){
      const f = files.get(from); if(!f) throw new Error('ENOENT ' + from);
      files.delete(from); files.set(to, f); return true;
    },
    // Slice I/O — what lets a file bigger than the renderer's heap (and bigger than a proxy's
    // request-body cap) move at all. Same shape as the desktop adapter's.
    async readPart(_id, p, offset, len){
      const f = files.get(p); if(!f) throw new Error('ENOENT ' + p);
      return f.bytes.subarray(offset, offset + len);
    },
    async writePart(_id, p, offset, bytes){
      const cur = parts.get(p) || [];
      cur.push({ offset, bytes: new Uint8Array(bytes) });
      parts.set(p, cur);
      return true;
    },
    async writeCommit(_id, p, mtime){
      const cur = parts.get(p) || [];
      const total = cur.reduce((n, c) => Math.max(n, c.offset + c.bytes.length), 0);
      const out = new Uint8Array(total);
      for(const c of cur) out.set(c.bytes, c.offset);
      parts.delete(p);
      const t = mtime || ++clock;
      files.set(p, { bytes: out, mtime: t });
      return { size: out.length, mtime: t };
    },
    async trash(_id, p, now){
      const f = files.get(p); if(!f) throw new Error('ENOENT ' + p);
      const to = S.trashPath(p, now || clock);
      files.delete(p); files.set(to, f); return to;
    },
  };

  /* The store as sync.js builds it: the manifest is SHARED and addressed by the pair key, `base` is
   * this device's own and never leaves it. Whether those two are keyed the same way is the entire
   * question — so `base` is deliberately kept in a per-device map here, exactly as localStorage is
   * per-device in life. */
  const bases = new Map();
  const store = {
    async manifest(k){ world.reads++; return world.manifestOf(k); },
    async base(k){ return JSON.parse(JSON.stringify(bases.get(k) || {})); },
    async save(k, s){
      world.writes++;
      // Mirrors sync.js: merge the touched paths onto what the document holds NOW, so a concurrent
      // sweep on another device cannot be erased by this one's stale snapshot.
      let paths = JSON.parse(JSON.stringify(s.manifest || {}));
      if(Array.isArray(s.touched) && s.touched.length){
        const fresh = world.manifestOf(k);
        const merged = Object.assign({}, fresh);
        for(const p of s.touched) if(paths[p] !== undefined) merged[p] = paths[p];
        paths = merged;
      }
      world.docs.set(dtag(k), paths);
      bases.set(k, JSON.parse(JSON.stringify(s.base || {})));
    },
    async putBlob(bytes){ const sha = sha256(bytes); world.blobs.set(sha, new Uint8Array(bytes)); return sha; },
    /* The chunked pair, with the SAME contract the client's syncBlobs has: one chunk in memory at a
     * time, each content-addressed and skipped when the store already holds it, and an identity for
     * the whole file that is the hash of its parts in order — so the engine's same() keeps working
     * without knowing chunking exists. */
    CHUNK: 4096,
    async putParts(readPart, size){
      const chunks = []; let existed = true;
      for(let off = 0; off < size; off += 4096){
        const want = Math.min(4096, size - off);
        const plain = await readPart(off, want);
        if(!plain || !plain.length) throw new Error('short read at ' + off);
        const sha = sha256(plain);
        if(!world.blobs.has(sha)){ world.blobs.set(sha, new Uint8Array(plain)); existed = false; }
        world.maxBody = Math.max(world.maxBody || 0, plain.length);   // nothing may exceed one chunk
        chunks.push(sha);
      }
      return { sha: sha256(bytesOf(chunks.join(''))), chunks, existed };
    },
    async getParts(chunks, writePart){
      let off = 0;
      for(const sha of chunks){
        const b = world.blobs.get(sha);
        if(!b) throw new Error('chunk ' + sha.slice(0,8) + ' unavailable (404)');
        await writePart(off, b); off += b.length;
      }
      return off;
    },
    async getBlob(sha){
      const b = world.blobs.get(sha);
      if(!b) throw new Error('blob ' + String(sha).slice(0,8) + ' unavailable (404)');
      return b;
    },
  };

  return {
    name, id, key, files, bases, fs, store,
    put(p, text){ files.set(p, { bytes: bytesOf(text), mtime: ++clock }); },
    rm(p){ files.delete(p); },
    read(p){ const f = files.get(p); return f ? textOf(f.bytes) : null; },
    live(){ return [...files.keys()].filter(p => !IGNORED(p)).sort(); },
    trashed(){ return [...files.keys()].filter(IGNORED).sort(); },
    // A sweep, as sync.js calls it: the PLATFORM id for the filesystem, the PAIR key for the store.
    sweep(o){
      return RUN.sweep(fs, store, Object.assign({
        id, key, device: name, now: ++clock, hash: true, excludes: [], chunkAbove: 8192,
        maxBytes: 16384,   // deliberately BELOW the chunk test's file: a scan that caps drops it
      }, o || {}));
    },
  };
}

/* ---- scenarios -------------------------------------------------------------------------------- */
const scenarios = [];
const scenario = (name, fn) => scenarios.push({ name, fn });

/* THE ONE THIS FILE EXISTS FOR. Two devices, two different platform handles, the same name typed by
 * the user on each. A file written on one must arrive on the other, byte for byte. */
scenario('pairs-by-name', async () => {
  const w = makeWorld();
  const laptop = makeDevice(w, { name:'laptop', id:'a91f3c0e77',                       key:'Documents' });
  const phone  = makeDevice(w, { name:'phone',  id:'content://com.android.externalstorage.documents/tree/primary%3ADocuments', key:'Documents' });

  laptop.put('report.txt', 'the quarterly numbers');
  laptop.put('notes/todo.md', '- ship it');
  const up = await laptop.sweep();
  const down = await phone.sweep();

  return {
    ok: phone.read('report.txt') === 'the quarterly numbers'
        && phone.read('notes/todo.md') === '- ship it'
        && up.uploaded.length === 2 && down.downloaded.length === 2,
    detail: { uploaded: up.uploaded, downloaded: down.downloaded, onPhone: phone.live(),
              ids: [laptop.id.slice(0,12), phone.id.slice(0,12)] },
  };
});

/* THE CONTROL, and the reason to trust the one above. Keyed on the platform handle — what the code
 * did before — the identical scenario transfers NOTHING, and each device happily reports success.
 * If this scenario ever starts passing files across, the sim has stopped being able to see the bug
 * it was written for. */
scenario('keyed-by-platform-id-cannot-pair', async () => {
  const w = makeWorld();
  const laptop = makeDevice(w, { name:'laptop', id:'a91f3c0e77', key:'a91f3c0e77' });
  const phone  = makeDevice(w, { name:'phone',  id:'b40d92ff18', key:'b40d92ff18' });

  laptop.put('report.txt', 'the quarterly numbers');
  const up = await laptop.sweep();
  const down = await phone.sweep();

  return {
    ok: up.uploaded.length === 1 && down.downloaded.length === 0 && phone.read('report.txt') === null
        && w.docs.size === 1,   // one document per device is the whole disease
    detail: { uploaded: up.uploaded, downloaded: down.downloaded, docs: [...w.docs.keys()] },
  };
});

/* An edit made on the second device comes back to the first. Same file, new bytes — the case that
 * needs `base` to tell "changed elsewhere" from "new here". */
scenario('edit-comes-back', async () => {
  const w = makeWorld();
  const laptop = makeDevice(w, { name:'laptop', id:'aaa1', key:'Documents' });
  const phone  = makeDevice(w, { name:'phone',  id:'bbb2', key:'Documents' });

  laptop.put('report.txt', 'draft');
  await laptop.sweep(); await phone.sweep();

  phone.put('report.txt', 'final');
  const up = await phone.sweep();
  const down = await laptop.sweep();

  return {
    ok: laptop.read('report.txt') === 'final' && up.uploaded.length === 1
        && down.downloaded.length === 1 && down.plan.download[0].why === 'changed elsewhere',
    detail: { why: down.plan.download.map(d => d.why), onLaptop: laptop.read('report.txt') },
  };
});

/* A delete propagates, and lands in .pc-trash rather than being destroyed. */
scenario('delete-propagates-to-trash', async () => {
  const w = makeWorld();
  const laptop = makeDevice(w, { name:'laptop', id:'aaa1', key:'Documents' });
  const phone  = makeDevice(w, { name:'phone',  id:'bbb2', key:'Documents' });

  laptop.put('old.txt', 'bye');
  await laptop.sweep(); await phone.sweep();

  laptop.rm('old.txt');
  await laptop.sweep();
  const rep = await phone.sweep();

  return {
    ok: rep.trashed.length === 1 && phone.live().indexOf('old.txt') === -1
        && phone.trashed().length === 1 && textOf(phone.files.get(phone.trashed()[0]).bytes) === 'bye',
    detail: { trashed: rep.trashed, live: phone.live(), inTrash: phone.trashed() },
  };
});

/* Both devices edit the same file while apart. Nothing may be lost and nothing may be silently
 * chosen: the incoming copy takes the name, the local one is renamed, and both sets of bytes are
 * still on disk afterwards. */
scenario('conflict-keeps-both', async () => {
  const w = makeWorld();
  const laptop = makeDevice(w, { name:'laptop', id:'aaa1', key:'Documents' });
  const phone  = makeDevice(w, { name:'phone',  id:'bbb2', key:'Documents' });

  laptop.put('report.txt', 'shared start');
  await laptop.sweep(); await phone.sweep();

  laptop.put('report.txt', 'the laptop version');
  phone.put('report.txt', 'the phone version');
  await phone.sweep();                       // phone gets there first
  const rep = await laptop.sweep();          // laptop finds divergent bytes

  const kept = laptop.live().filter(p => p !== 'report.txt');
  const texts = laptop.live().map(p => laptop.read(p)).sort();
  return {
    ok: rep.conflicted.length === 1 && kept.length === 1 && /conflict from phone/.test(kept[0])
        && texts.join('|') === ['the laptop version','the phone version'].sort().join('|'),
    detail: { conflicted: rep.conflicted, onLaptop: laptop.live(), texts },
  };
});

/* Excluding a folder on ONE device must not delete it from the others — the regression that landed
 * in 53d0dd40, now checked with two real devices instead of a synthetic remote snapshot. */
scenario('exclude-here-does-not-delete-there', async () => {
  const w = makeWorld();
  const laptop = makeDevice(w, { name:'laptop', id:'aaa1', key:'Pictures' });
  const phone  = makeDevice(w, { name:'phone',  id:'bbb2', key:'Pictures' });

  laptop.put('trip.jpg', 'JPEGDATA');
  laptop.put('Old/2019.jpg', 'OLDDATA');
  await laptop.sweep(); await phone.sweep();

  const rep = await laptop.sweep({ excludes: ['Old'] });   // the laptop stops looking at Old
  const after = await phone.sweep();                        // the phone must be untouched

  return {
    ok: rep.removedRemote.length === 0 && rep.trashed.length === 0
        && after.trashed.length === 0 && phone.read('Old/2019.jpg') === 'OLDDATA'
        && !!w.manifestOf('Pictures')['Old/2019.jpg'],
    detail: { removedRemote: rep.removedRemote, phoneTrashed: after.trashed,
              stillInManifest: Object.keys(w.manifestOf('Pictures')).sort() },
  };
});

/* Sweeping repeatedly must do nothing. A sync that re-decides settled files is one that re-uploads
 * what it just downloaded, forever — the classic loop, and the reason plan.notes has to be recorded
 * in `base` rather than treated as "nothing happened". */
scenario('idempotent', async () => {
  const w = makeWorld();
  const laptop = makeDevice(w, { name:'laptop', id:'aaa1', key:'Documents' });
  const phone  = makeDevice(w, { name:'phone',  id:'bbb2', key:'Documents' });

  laptop.put('a.txt', 'A'); laptop.put('b/c.txt', 'C');
  await laptop.sweep(); await phone.sweep();

  const moved = [];
  for(let i = 0; i < 5; i++){
    for(const d of [laptop, phone]){
      const r = await d.sweep();
      if(r.uploaded.length || r.downloaded.length || r.trashed.length || r.conflicted.length)
        moved.push({ device: d.name, pass: i, r: { up:r.uploaded, down:r.downloaded, trash:r.trashed } });
    }
  }
  return { ok: moved.length === 0, detail: { moved } };
});

/* The same file created independently on both devices — a photo library seeded from one camera, a
 * document restored from the same backup. Identical bytes are not a conflict, and treating them as
 * one produces thousands of "(conflict from …)" copies on a first sync. */
scenario('same-bytes-both-sides-is-not-a-conflict', async () => {
  const w = makeWorld();
  const laptop = makeDevice(w, { name:'laptop', id:'aaa1', key:'Pictures' });
  const phone  = makeDevice(w, { name:'phone',  id:'bbb2', key:'Pictures' });

  laptop.put('DSC_0001.jpg', 'IDENTICAL BYTES');
  phone.put('DSC_0001.jpg', 'IDENTICAL BYTES');
  await laptop.sweep();
  const rep = await phone.sweep();

  return {
    ok: rep.conflicted.length === 0 && phone.live().length === 1 && laptop.live().length === 1,
    detail: { conflicted: rep.conflicted, onPhone: phone.live(), notes: (rep.plan||{}).notes },
  };
});

/* An INCREMENTAL sweep — the ordinary one on battery — never hashes, so every local entry reaches
 * the engine with no `sha` at all and size+mtime is the only comparison available. A round trip has
 * to survive that too, and must not re-upload files it already agreed about. */
scenario('incremental-sweep-round-trips', async () => {
  const w = makeWorld();
  const laptop = makeDevice(w, { name:'laptop', id:'aaa1', key:'Documents' });
  const phone  = makeDevice(w, { name:'phone',  id:'bbb2', key:'Documents' });

  laptop.put('memo.txt', 'hello');
  await laptop.sweep({ hash: false });
  const down = await phone.sweep({ hash: false });
  const again = await laptop.sweep({ hash: false });

  return {
    ok: phone.read('memo.txt') === 'hello' && down.downloaded.length === 1
        && again.uploaded.length === 0 && again.downloaded.length === 0,
    detail: { downloaded: down.downloaded, secondPass: { up: again.uploaded, down: again.downloaded } },
  };
});

/* WHY `base` MUST BE CLEARED WITH THE SAME KEY IT WAS WRITTEN UNDER.
 *
 * "Stop syncing" deletes this device's agreement. If it deletes the wrong one — the base survives
 * while the mapping goes — then re-adding the folder later starts from a base that claims files are
 * synced which are no longer on disk, and the engine correctly reads that as "deleted here" and
 * removes them from every other device. That is not a hypothetical: base moved from the platform id
 * to the pair key and the removeItem() did not move with it.
 *
 * This asserts the SHAPE of the damage, so the cost of getting that key wrong is visible in a test
 * rather than in somebody's Documents folder. */
scenario('stale-base-is-what-deletes-everything', async () => {
  const w = makeWorld();
  const laptop = makeDevice(w, { name:'laptop', id:'aaa1', key:'Documents' });
  const phone  = makeDevice(w, { name:'phone',  id:'bbb2', key:'Documents' });

  laptop.put('keep.txt', 'important');
  await laptop.sweep(); await phone.sweep();

  // "Stop syncing", then re-add the folder pointing at a fresh empty directory — with the base left
  // behind because it was cleared under the wrong key.
  laptop.rm('keep.txt');
  const withStale = await laptop.sweep();

  // Now the same thing with the agreement actually cleared, which is what the fix does.
  const w2 = makeWorld();
  const laptop2 = makeDevice(w2, { name:'laptop', id:'aaa1', key:'Documents' });
  const phone2  = makeDevice(w2, { name:'phone',  id:'bbb2', key:'Documents' });
  laptop2.put('keep.txt', 'important');
  await laptop2.sweep(); await phone2.sweep();
  laptop2.rm('keep.txt');
  laptop2.bases.clear();                       // ← what "Stop syncing" must do
  const withCleared = await laptop2.sweep();
  const phoneAfter = await phone2.sweep();

  return {
    ok: withStale.removedRemote.length === 1            // stale base ⇒ the delete propagates
        && withCleared.removedRemote.length === 0       // cleared base ⇒ nothing is deleted anywhere
        && withCleared.downloaded.length === 1          // it is treated as a new device and refills
        && phoneAfter.trashed.length === 0
        && phone2.read('keep.txt') === 'important',
    detail: { stale: withStale.removedRemote, cleared: { removed: withCleared.removedRemote,
              downloaded: withCleared.downloaded }, phoneStillHas: phone2.read('keep.txt') },
  };
});

/* A blob the server has forgotten must fail THAT path and leave everything else alone — and must not
 * record agreement about a file it never wrote, or the next sweep reads the missing file as a local
 * delete and removes it everywhere. */
scenario('a-missing-blob-does-not-poison-the-sweep', async () => {
  const w = makeWorld();
  const laptop = makeDevice(w, { name:'laptop', id:'aaa1', key:'Documents' });
  const phone  = makeDevice(w, { name:'phone',  id:'bbb2', key:'Documents' });

  laptop.put('gone.txt', 'vanished upstream');
  laptop.put('fine.txt', 'still here');
  await laptop.sweep();
  w.blobs.delete(sha256(bytesOf('vanished upstream')));   // the TTL sweep got it

  const rep = await phone.sweep();
  const again = await phone.sweep();

  return {
    ok: rep.downloaded.join() === 'fine.txt' && rep.failed.length === 1
        && rep.failed[0].path === 'gone.txt' && rep.ok === false
        // and the next sweep tries again rather than concluding the file was deleted
        && again.failed.length === 1 && again.removedRemote.length === 0,
    detail: { downloaded: rep.downloaded, failed: rep.failed, second: { failed: again.failed, removed: again.removedRemote } },
  };
});

/* AN INTERRUPTED SWEEP MUST RESUME, NOT RESTART.
 *
 * `base` advanced per file in memory and was written once, at the very end — so a sweep that never
 * reached the end (the laptop closed, the app killed mid-upload) recorded nothing at all and the
 * next one began at the first file. On a 15790-file folder that is not a slow resume, it is a folder
 * that can never finish on a machine anyone ever closes.
 *
 * A kill is modelled the way it actually lands: the world stops at the last checkpoint. So this runs
 * a sweep, takes the state as of the FIRST checkpoint, and starts a fresh device from exactly that —
 * which is what a restarted app reads.
 */
scenario('an-interrupted-sweep-resumes', async () => {
  const w = makeWorld();
  const laptop = makeDevice(w, { name:'laptop', id:'aaa1', key:'Pictures' });
  const N = 450;                                  // more than one checkpoint's worth
  for(let i = 0; i < N; i++) laptop.put('DSC_' + String(i).padStart(4,'0') + '.jpg', 'PHOTO ' + i);

  const checkpoints = [];
  const realSave = laptop.store.save.bind(laptop.store);
  laptop.store.save = async (k, s) => {
    checkpoints.push({ base: JSON.parse(JSON.stringify(s.base)), manifest: JSON.parse(JSON.stringify(s.manifest)) });
    return realSave(k, s);
  };
  const first = await laptop.sweep();
  if(checkpoints.length < 2) return { ok:false, detail:{ why:'no checkpoint before the final save', checkpoints:checkpoints.length } };

  // The world as it was when the app died: the first checkpoint's manifest, and its agreement.
  const killed = checkpoints[0];
  const w2 = makeWorld();
  w2.docs.set(dtag('Pictures'), killed.manifest);
  for(const [sha, bytes] of w.blobs) w2.blobs.set(sha, bytes);   // the uploads really happened
  const restarted = makeDevice(w2, { name:'laptop', id:'aaa1', key:'Pictures' });
  for(const [p, f] of laptop.files) restarted.files.set(p, f);
  restarted.bases.set('Pictures', killed.base);

  const resumed = await restarted.sweep();
  const done = Object.keys(killed.base).length;

  return {
    ok: first.uploaded.length === N && checkpoints.length > 1
        && resumed.uploaded.length === N - done      // only the remainder
        && resumed.conflicted.length === 0,
    detail: { files: N, checkpoints: checkpoints.length, agreedAtKill: done,
              reuploaded: resumed.uploaded.length, conflicts: resumed.conflicted.length },
  };
});

/* AN EMPTY `base` AGAINST A FULL FOLDER MUST NOT CONFLICT EVERY FILE.
 *
 * Re-adding a folder that is already synced, or any device whose local agreement was cleared, gives
 * base={} with both sides full. Both then look "changed", and a convergence test that required
 * `L.sha === R.sha` could never fire on an ORDINARY sweep — an incremental one does not hash, so
 * there is no local sha — and every single file became a conflict copy. On a real folder that is the
 * whole library duplicated as "(conflict from …)" on every device.
 */
scenario('an-empty-base-does-not-conflict-the-whole-folder', async () => {
  const w = makeWorld();
  const laptop = makeDevice(w, { name:'laptop', id:'aaa1', key:'Documents' });
  for(let i = 0; i < 40; i++) laptop.put('doc' + i + '.txt', 'contents of ' + i);
  await laptop.sweep();

  laptop.bases.clear();                        // "Stop syncing", then add it back
  const rep = await laptop.sweep({ hash: false });   // the ordinary sweep: no hashing

  return {
    ok: rep.conflicted.length === 0 && rep.uploaded.length === 0 && rep.trashed.length === 0
        && laptop.live().length === 40,
    detail: { conflicted: rep.conflicted.length, uploaded: rep.uploaded.length,
              onDisk: laptop.live().length, notes: (rep.plan||{}).notes && (rep.plan.notes||[]).length },
  };
});

/* A FILE TOO BIG TO HOLD IN ONE PIECE still crosses, and no single request carries more than a chunk.
 *
 * The whole-file path holds the plaintext, the ciphertext and the upload body at once — three to four
 * times the file — so a 2 GB document asked for ~7 GB and Chromium killed the renderer instead, which
 * in the desktop app is a black window. The same ceiling is a proxy's: a request body over ~95 MB is
 * refused by Cloudflare whatever the app allows.
 *
 * Chunking answers both, and this asserts both: the bytes arrive identical on the other device, and
 * the largest single request was one chunk. */
scenario('a-file-too-big-to-hold-crosses-in-chunks', async () => {
  const w = makeWorld();
  const laptop = makeDevice(w, { name:'laptop', id:'aaa1', key:'Documents' });
  const phone  = makeDevice(w, { name:'phone',  id:'bbb2', key:'Documents' });

  // 40 KB against an 8 KB chunk-above and a 4 KB chunk: ten pieces, and well over the threshold.
  const big = new Uint8Array(40 * 1024);
  for(let i = 0; i < big.length; i++) big[i] = (i * 7 + (i >> 8)) & 0xff;   // not compressible to one value
  laptop.files.set('video.mp4', { bytes: big, mtime: Date.UTC(2026, 7, 1) });

  const up = await laptop.sweep();
  const down = await phone.sweep();

  const got = phone.files.get('video.mp4');
  const same = !!got && got.bytes.length === big.length && Buffer.compare(Buffer.from(got.bytes), Buffer.from(big)) === 0;
  const entry = w.manifestOf('Documents')['video.mp4'] || {};
  return {
    ok: same && up.uploaded.join() === 'video.mp4' && down.downloaded.join() === 'video.mp4'
        && Array.isArray(entry.chunks) && entry.chunks.length === 10
        && !!entry.sha && w.maxBody === 4096,
    detail: { bytesMatch: same, chunks: (entry.chunks || []).length, maxRequestBytes: w.maxBody,
              uploaded: up.uploaded, downloaded: down.downloaded },
  };
});

/* ...and the second device does not re-send what it already agreed: a chunked file settles like any
 * other, or a big file would re-upload on every sweep for ever. */
scenario('a-chunked-file-settles', async () => {
  const w = makeWorld();
  const laptop = makeDevice(w, { name:'laptop', id:'aaa1', key:'Documents' });
  const phone  = makeDevice(w, { name:'phone',  id:'bbb2', key:'Documents' });
  const big = new Uint8Array(20 * 1024).map((_, i) => (i * 13) & 0xff);
  laptop.files.set('big.bin', { bytes: big, mtime: Date.UTC(2026, 7, 1) });
  await laptop.sweep(); await phone.sweep();

  const moved = [];
  for(let i = 0; i < 3; i++){
    for(const d of [laptop, phone]){
      const r = await d.sweep();
      if(r.uploaded.length || r.downloaded.length || r.conflicted.length)
        moved.push({ device: d.name, up: r.uploaded, down: r.downloaded, conflicts: r.conflicted.length });
    }
  }
  return { ok: moved.length === 0, detail: { moved } };
});

/* A DEVICE JOINING A FOLDER THAT ALREADY EXISTS, with the files already on its disk and mtimes that
 * do NOT match the manifest — which is every Android device, because SAF ignores the last-modified
 * you ask for and the provider decides. Without hashing that first sweep, every identical file looks
 * divergent and the whole folder is duplicated as conflict copies. */
scenario('a-joining-device-with-different-mtimes-does-not-duplicate', async () => {
  const w = makeWorld();
  const laptop = makeDevice(w, { name:'laptop', id:'aaa1', key:'Pictures' });
  for(let i = 0; i < 12; i++) laptop.put('img' + i + '.jpg', 'PHOTO ' + i);
  await laptop.sweep();

  // The tablet already holds the same bytes, written at a different time entirely.
  const tablet = makeDevice(w, { name:'tablet', id:'bbb2', key:'Pictures' });
  for(let i = 0; i < 12; i++){
    tablet.files.set('img' + i + '.jpg', { bytes: bytesOf('PHOTO ' + i), mtime: Date.UTC(2019, 0, 1) });
  }
  // An ORDINARY sweep — the caller asks for no hashing at all. The executor must hash anyway,
  // because this device has never agreed about this folder.
  const rep = await tablet.sweep({ hash: false });

  return {
    ok: rep.conflicted.length === 0 && rep.uploaded.length === 0 && rep.downloaded.length === 0
        && tablet.live().length === 12,
    detail: { conflicted: rep.conflicted.length, uploaded: rep.uploaded.length,
              downloaded: rep.downloaded.length, onDisk: tablet.live().length },
  };
});

/* TWO DEVICES SYNCING THE SAME FOLDER AT ONCE must not erase each other.
 *
 * Each sweep holds a snapshot of the manifest taken when it started, so writing that snapshot whole
 * is last-writer-wins: the later save drops every path the other device added. The blobs survive —
 * they are uploaded and content-addressed — but the ENTRIES go, so the files are missing from the
 * folder everywhere else, and the device that uploaded them never adds them again because its own
 * `base` says they are agreed. Silent, and permanent until something else changes those files. */
scenario('concurrent-sweeps-do-not-erase-each-other', async () => {
  const w = makeWorld();
  const a = makeDevice(w, { name:'laptop', id:'aaa1', key:'Pictures' });
  const b = makeDevice(w, { name:'tablet', id:'bbb2', key:'Pictures' });
  for(let i = 0; i < 6; i++) a.put('from-laptop-' + i + '.jpg', 'L' + i);
  for(let i = 0; i < 6; i++) b.put('from-tablet-' + i + '.jpg', 'T' + i);

  // Interleaved: both read an empty manifest, then both save. Whoever writes second used to win
  // outright and take the other's six paths with it.
  await Promise.all([a.sweep(), b.sweep()]);

  const man = w.manifestOf('Pictures');
  const fromA = Object.keys(man).filter(p => p.startsWith('from-laptop-')).length;
  const fromB = Object.keys(man).filter(p => p.startsWith('from-tablet-')).length;
  return { ok: fromA === 6 && fromB === 6,
           detail: { laptopPaths: fromA, tabletPaths: fromB, total: Object.keys(man).length } };
});

/* THREE devices, because "the other device" is not always the same one. A file added on the third
 * has to reach both of the others, and a delete on the first has to reach both of the others. */
scenario('three-devices-converge', async () => {
  const w = makeWorld();
  const ds = [
    makeDevice(w, { name:'laptop',  id:'aaa1', key:'Documents' }),
    makeDevice(w, { name:'phone',   id:'bbb2', key:'Documents' }),
    makeDevice(w, { name:'desktop', id:'ccc3', key:'Documents' }),
  ];
  const settle = async () => { for(let i = 0; i < 3; i++) for(const d of ds) await d.sweep(); };

  ds[2].put('shared.txt', 'from the desktop');
  await settle();
  const everyoneHasIt = ds.every(d => d.read('shared.txt') === 'from the desktop');

  ds[0].rm('shared.txt');
  await settle();
  const everyoneLostIt = ds.every(d => d.live().indexOf('shared.txt') === -1);
  const everyoneKeptACopy = ds.slice(1).every(d => d.trashed().length === 1);

  return {
    ok: everyoneHasIt && everyoneLostIt && everyoneKeptACopy,
    detail: { everyoneHasIt, everyoneLostIt, everyoneKeptACopy, live: ds.map(d => d.live()) },
  };
});

/* ---- run -------------------------------------------------------------------------------------- */
(async () => {
  const rows = [];
  for(const s of scenarios){
    try{ const r = await s.fn(); rows.push({ name: s.name, ok: !!r.ok, detail: r.detail }); }
    catch(e){ rows.push({ name: s.name, ok: false, detail: { threw: (e && e.stack) || String(e) } }); }
  }
  process.stdout.write(JSON.stringify(rows, null, 1));
  process.exit(rows.every(r => r.ok) ? 0 : 1);
})();
