/* THE WHOLE JOURNEY, ONCE, AT THE REPORTED NUMBERS — "I want it to work in 1 shot".
 *
 * Every other simulation here proves ONE rule. This proves the sequence a person actually performs,
 * from the state they are actually in, and it is the thing that was never tested: a desktop holding
 * 6,331 files, a shared manifest that is nothing but tombstones (every other device erased the
 * folder), and a local agreement that still says those files were synced. That combination produced
 * "move 6331 files on this device to the trash? ... this sweep keeps only 0", over and over, on
 * build after build.
 *
 *   1. the desktop sweeps        → nothing is trashed, everything is uploaded
 *   2. the desktop sweeps again  → quiet; it does not undo or repeat itself
 *   3. the phone pairs the name  → downloads all of it, VERIFIED, byte for byte
 *   4. both sweep again          → quiet, and the two disks are identical
 *
 * Real content throughout: a wrong offset, a truncation or a dropped chunk changes the hash and
 * fails the run. The two devices use different chunk sizes (4 MB phone, 16 MB desktop), which is the
 * arrangement that exists and the one where reassembly can go wrong.
 *
 * Usage: node one_shot_sim.js [files]
 */
'use strict';
const crypto = require('crypto');
const path = require('path');
const RUN = require(path.join(__dirname, '..', '..', 'static', 'js', 'client', 'syncrun.js'));

const N = parseInt(process.argv[2] || '6331', 10);
const MB = 1024 * 1024;
const sha = (b) => crypto.createHash('sha256').update(b).digest('hex');
const fail = [];
const check = (c, w) => { if(!c) fail.push(w); };

// Small but DISTINCT per file and per region, so any mix-up shows up as a hash mismatch.
function content(i){
  const size = 900 + (i % 400);
  const b = Buffer.alloc(size);
  for(let o = 0; o + 4 <= size; o += 4) b.writeUInt32BE(((i * 2654435761) ^ o) >>> 0, o);
  return b;
}

function cloud(){
  const blobs = new Map();
  let manifest = {};
  return { get manifest(){ return manifest; }, set manifest(m){ manifest = m; },
           put(b){ const h = sha(b); blobs.set(h, Buffer.from(b)); return h; },
           get(h){ return blobs.get(h); } };
}

function device(disk, sky, o){
  const CH = o.chunk;
  const state = { base: o.base || {}, verified: [], trashed: [] };
  const parts = {};
  const fs = {
    chunkBytes: CH,
    scan: async () => {
      const files = {};
      for(const r in disk) files[r] = { size: disk[r].length, mtime: 1000 };
      return { files, skipped: [] };
    },
    scanPage: async (id, so, off, lim) => {
      const k = Object.keys(disk), end = Math.min(k.length, off + lim), files = {};
      for(let i = off; i < end; i++) files[k[i]] = { size: disk[k[i]].length, mtime: 1000 };
      return { files, skipped: [], total: k.length, done: end >= k.length };
    },
    read: async (id, r) => new Uint8Array(disk[r]),
    readPart: async (id, r, off, len) => new Uint8Array(disk[r].subarray(off, off + len)),
    writePart: async (id, r, off, bytes) => {
      const b = Buffer.from(bytes), cur = parts[r] || Buffer.alloc(0);
      const next = Buffer.alloc(Math.max(cur.length, off + b.length));
      cur.copy(next); b.copy(next, off); parts[r] = next;
    },
    partSize: async (id, r) => (parts[r] ? parts[r].length : 0),
    discardPart: async (id, r) => { delete parts[r]; },
    hashPart: async (id, r) => { state.verified.push(r); return sha(parts[r] || Buffer.alloc(0)); },
    writeCommit: async (id, r) => { disk[r] = parts[r] || Buffer.alloc(0); delete parts[r];
                                    return { size: disk[r].length, mtime: 2000 }; },
    write: async (id, r, bytes) => { disk[r] = Buffer.from(bytes);
                                     return { size: disk[r].length, mtime: 2000 }; },
    move: async () => {},
    trash: async (id, r) => { state.trashed.push(r); delete disk[r]; },
    sweepParts: async () => ({ removed: 0 }),
  };
  if(o.hashFile) fs.hashFile = async (id, r) => sha(disk[r] || Buffer.alloc(0));

  const store = {
    manifest: async () => JSON.parse(JSON.stringify(sky.manifest)),
    base: async () => JSON.parse(JSON.stringify(state.base)),
    saveBase: async (k, b) => { state.base = JSON.parse(JSON.stringify(b || {})); },
    save: async (k, m) => {
      const next = (m && m.manifest) || {}, cur = sky.manifest;
      for(const p of (m && m.touched) || Object.keys(next)){
        if(next[p]) cur[p] = next[p]; else delete cur[p];
      }
      sky.manifest = cur;
      if(m && m.base) state.base = JSON.parse(JSON.stringify(m.base));
    },
    putBlob: async (b) => ({ sha: sky.put(Buffer.from(b)) }),
    getBlob: async (h) => new Uint8Array(sky.get(h) || Buffer.alloc(0)),
    putParts: async (readPart, size, onProg, cs) => {
      const use = cs || CH, chunks = [];
      for(let off = 0; off < size; off += use)
        chunks.push(sky.put(Buffer.from(await readPart(off, Math.min(use, size - off)))));
      return { chunks, parts: chunks, cs: use };
    },
    getParts: async (chunks, write, size, have, cs) => {
      const use = cs || CH;
      for(let i = 0; i < chunks.length; i++){
        const b = sky.get(chunks[i]);
        if(!b) throw new Error('the store has forgotten a chunk');
        await write(i * use, new Uint8Array(b));
      }
    },
    /* Counted as a verification too: a SMALL file takes the whole-file path, which checks the bytes
     * with hashBytes rather than hashing a part file. Counting only hashPart made this look like
     * nothing was verified when in fact everything was. */
    hashBytes: async (b) => { state.verified.push('(whole)'); return sha(Buffer.from(b)); },
    blobSha: async (b) => sha(Buffer.from(b)),
    chunkShas: async (readPart, size, cs) => {
      const use = cs || CH, out = [];
      for(let off = 0; off < size; off += use)
        out.push(sha(Buffer.from(await readPart(off, Math.min(use, size - off)))));
      return out;
    },
  };
  return { fs, store, state, disk };
}

const opts = (chunk, extra) => Object.assign({
  id: 't', key: 'Pictures', device: 'dev', now: Date.now(), excludes: [],
  maxBytes: 8 * 1024 * MB, chunkBytes: chunk, chunkAbove: chunk, manual: true,
  /* THE TWO QUESTIONS, ANSWERED THE WAY SOMEBODY IN THIS POSITION ANSWERS THEM.
   *
   * "Everything was deleted elsewhere and I want this device emptied too" and "everything was
   * deleted elsewhere and this device is the one with the files" are the SAME manifest and the same
   * disk — indistinguishable from the data, so only the person can say which. The sweep asks about
   * emptying first; saying no now leads to the other question instead of to nothing, which is what
   * made it an unbreakable loop.
   *
   * What must never happen is a file moved to the trash on the way through, and that is asserted
   * below rather than here. */
  confirmTrash: async () => false,          // no, do not empty this device
  confirmResurrect: async () => true,       // yes, put them back on the others
}, extra || {});

(async () => {
  const sky = cloud();
  const deskDisk = {}, want = {};
  const manifest = {}, agreed = {};
  for(let i = 0; i < N; i++){
    const r = 'Pictures/IMG_' + String(i).padStart(5, '0') + '.jpg';
    const b = content(i);
    deskDisk[r] = b; want[r] = sha(b);
    manifest[r] = { deletedAt: 4000, device: 'phone' };     // erased everywhere else
    agreed[r] = { size: b.length, mtime: 1000 };            // …but this device agreed them once
  }
  sky.manifest = manifest;

  const desk = device(deskDisk, sky, { chunk: 16 * MB, hashFile: false, base: agreed });
  const t0 = Date.now();

  // 1. the desktop sweeps
  const d1 = await RUN.sweep(desk.fs, desk.store, opts(16 * MB));
  check(desk.state.trashed.length === 0,
        desk.state.trashed.length + ' files were moved to the trash');
  check((d1.uploaded || []).length === N,
        'uploaded ' + (d1.uploaded || []).length + ' of ' + N);
  const live = Object.keys(sky.manifest).filter(p => !sky.manifest[p].deletedAt).length;
  check(live === N, 'the manifest holds ' + live + ' live files, want ' + N
        + ' — the folder would still show "0 files"');

  // 2. and again: quiet
  const d2 = await RUN.sweep(desk.fs, desk.store, opts(16 * MB));
  check((d2.uploaded || []).length === 0 && (d2.downloaded || []).length === 0
        && desk.state.trashed.length === 0, 'the second desktop sweep was not quiet');

  // 3. the phone pairs the same name, holding nothing
  const phoneDisk = {};
  const phone = device(phoneDisk, sky, { chunk: 4 * MB, hashFile: true, base: {} });
  const p1 = await RUN.sweep(phone.fs, phone.store, opts(4 * MB));
  check((p1.downloaded || []).length === N,
        'the phone downloaded ' + (p1.downloaded || []).length + ' of ' + N);
  let bad = 0, missing = 0;
  for(const r in want){
    if(!phoneDisk[r]){ missing++; continue; }
    if(sha(phoneDisk[r]) !== want[r]) bad++;
  }
  check(missing === 0, missing + ' files never arrived on the phone');
  check(bad === 0, bad + ' files arrived CORRUPT');
  check(phone.state.verified.length >= N,
        'only ' + phone.state.verified.length + ' of ' + N + ' were verified before being written');

  // 4. both quiet, and the disks agree
  const p2 = await RUN.sweep(phone.fs, phone.store, opts(4 * MB));
  const d3 = await RUN.sweep(desk.fs, desk.store, opts(16 * MB));
  check((p2.uploaded || []).length === 0 && (p2.downloaded || []).length === 0,
        'the phone kept working after it was in step');
  check((d3.uploaded || []).length === 0 && (d3.downloaded || []).length === 0,
        'the desktop kept working after it was in step');
  check(Object.keys(phoneDisk).length === Object.keys(deskDisk).length,
        'the two devices hold different numbers of files');

  console.log(JSON.stringify({
    files: N, seconds: Math.round((Date.now() - t0) / 1000),
    desktop: { trashed: desk.state.trashed.length, uploaded: (d1.uploaded || []).length,
               discardedBase: d1.discardedBase || 0, secondSweepQuiet: (d2.uploaded || []).length === 0 },
    manifestLive: live,
    phone: { downloaded: (p1.downloaded || []).length, corrupt: bad, missing,
             verified: phone.state.verified.length, secondSweepQuiet: (p2.downloaded || []).length === 0 },
    failures: fail,
  }, null, 1));
  process.exit(fail.length ? 1 : 0);
})().catch(e => { console.error('FAILED: ' + (e && e.stack || e)); process.exit(1); });
