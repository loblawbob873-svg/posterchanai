/* THE PER-FILE SYNC, END TO END — real devices, real bytes, every failure that has been reported.
 *
 * This drives the shipped syncstate.js + syncexec.js through a fake network and a fake filesystem
 * that behave like the real ones: content-addressed blobs, ONE VERSIONED RECORD PER FILE behind the
 * server's compare-and-swap (the sim's store enforces the same CAS and era rules the real endpoint
 * does), a journal in "IndexedDB", and files whose bytes are checked at every step. A wrong offset,
 * a dropped chunk or a truncation changes a hash and fails the run.
 *
 * The scenarios are named after the reports that produced them.
 *
 * Usage: node exec_sim.js [files]
 */
'use strict';
const crypto = require('crypto');
const fs_ = require('fs');
const path = require('path');
require(path.join(__dirname, '..', '..', 'static', 'js', 'client', 'foldersync.js'));
require(path.join(__dirname, '..', '..', 'static', 'js', 'client', 'syncstate.js'));
const EXEC = require(path.join(__dirname, '..', '..', 'static', 'js', 'client', 'syncexec.js'));

const MB = 1024 * 1024;
const sha = (b) => crypto.createHash('sha256').update(b).digest('hex');

/* ---- THE REAL CHUNKER, LIFTED OUT OF app.js -----------------------------------------------------
 *
 * NOT a copy. A hand-written putParts/getParts in a simulator is the blind spot this codebase has
 * already paid for once: the sim's copy kept a pre-fix "empty read" check while the shipped one had
 * moved to "short read", so every chunk scenario passed throughout the weeks videos were coming back
 * unplayable. It was agreeing with itself.
 *
 * The encryption below is a keyed transform rather than AES — but it is NOT identity, and that
 * matters: with identity a chunk written at the wrong offset still reassembles into something, which
 * is the failure being hunted. The IV is derived from the content exactly as the real one is, so the
 * addressing scheme under test is the shipped scheme. */
const APP = fs_.readFileSync(path.join(__dirname, '..', '..', 'static', 'js', 'client', 'app.js'), 'utf8');
function lift(name, re){
  const m = APP.match(re);
  if(!m) throw new Error(name + ' moved in app.js — re-point exec_sim.js');
  return m[0];
}
const SHIPPED = [
  lift('_exactPart', /async _exactPart\([^)]*\)\{[\s\S]*?\n      \},/),
  lift('putParts', /async putParts\([^)]*\)\{[\s\S]*?\n      \},/),
  lift('getParts', /async getParts\([^)]*\)\{[\s\S]*?\n      \},/),
].join('\n');

function realChunker(sky){
  const sha256hex = (b) => sha(Buffer.from(b));
  const _contentIV = async (p) => new Uint8Array(
    crypto.createHash('sha256').update(Buffer.from(p)).digest().subarray(0, 12));
  const _masterEncrypt = async (mk, plain, iv) => {
    const out = new Uint8Array(12 + plain.length);
    out.set(iv, 0);
    for(let i = 0; i < plain.length; i++) out[12 + i] = plain[i] ^ iv[i % 12] ^ 0x5A;
    return out;
  };
  const _masterDecrypt = (ct) => {
    const iv = ct.subarray(0, 12), body = ct.subarray(12);
    const out = new Uint8Array(body.length);
    for(let i = 0; i < body.length; i++) out[i] = body[i] ^ iv[i % 12] ^ 0x5A;
    return out;
  };
  const FilesIdx = { _ensureMK: async () => 'mk' };
  const _blobAlreadyStored = async (h) => sky.hasCipher(h);
  const _shaFromUrl = (u) => String(u).split('/').pop();
  const uploadBlob = async (file) => {
    const bytes = new Uint8Array(await file.arrayBuffer());
    const h = sha256hex(bytes);
    sky.putCipher(h, bytes);
    return 'https://blossom.example/' + h;
  };
  const _syncBlobBytes = async (h) => {
    const b = sky.getCipher(h);
    if(!b) throw new Error('blob ' + String(h).slice(0, 8) + ' unavailable (404)');
    return _masterDecrypt(b);
  };
  // eslint-disable-next-line no-new-func
  return new Function('crypto', 'sha256hex', '_contentIV', '_masterEncrypt', '_masterDecrypt',
                      'FilesIdx', '_blobAlreadyStored', '_shaFromUrl', 'uploadBlob',
                      '_syncBlobBytes', 'File', 'Blob',
                      'return { ' + SHIPPED + ' };')(
    crypto, sha256hex, _contentIV, _masterEncrypt, _masterDecrypt, FilesIdx, _blobAlreadyStored,
    _shaFromUrl, uploadBlob, _syncBlobBytes, File, Blob);
}

/* ---- the world -------------------------------------------------------------------------------- */

function cloud(){
  const blobs = new Map();               // plaintext blobs (the whole-file path)
  const ciphers = new Map();             // what the SHIPPED chunker stores: encrypted chunks
  const recs = {};                       // path -> {v, by, era, t, bad, entry} — the server's store
  let era = 0;
  const sky = {
    recs,
    era: () => era,
    bumpEra(){ era++; },                 // what forgetAll does: one write, the old world dies
    /* The record set the CURRENT era can see, as a folder — what io.state hands the executor. */
    folder(){
      const out = {};
      for(const p in recs){
        const r = recs[p];
        if(r.era !== era) continue;
        const e = JSON.parse(JSON.stringify(r.entry));
        e.v = r.v; e.by = r.by;
        if(r.t && !e.deletedAt) e.deletedAt = 1;
        out[p] = e;
      }
      return out;
    },
    entry(p){ const f = sky.folder(); return f[p]; },
    liveCount(){ const f = sky.folder(); let n = 0;
                 for(const p in f) if(!f[p].deletedAt) n++; return n; },
    injectRec(p, entry){ recs[p] = { v: entry.v || 1, by: entry.by || 'x', era,
                                     t: entry.deletedAt ? 1 : 0,
                                     entry: JSON.parse(JSON.stringify(entry)) }; },
    dropRec(p){ delete recs[p]; },
    /* THE CAS, exactly as the server enforces it: strictly newer or refused. */
    putRec(p, entry){
      const cur = recs[p];
      if(cur && cur.era === era && cur.v >= (entry.v || 0)) return 'stale';
      recs[p] = { v: entry.v || 1, by: entry.by || '?', era, t: entry.deletedAt ? 1 : 0,
                  entry: JSON.parse(JSON.stringify(entry)) };
      return 'ok';
    },
    flagRec(p, id){ const r = recs[p]; if(r && r.era === era && !r.t) r.bad = String(id); },
    putCipher(h, b){ ciphers.set(h, Buffer.from(b)); },
    getCipher(h){ return ciphers.get(h); },
    hasCipher(h){ return ciphers.has(h); },
    corruptCipher(h){ ciphers.set(h, Buffer.from('rubbish that is long enough to look like a chunk')); },
    put(b){ const h = sha(b); blobs.set(h, Buffer.from(b)); return h; },
    putRaw(h, b){ blobs.set(h, Buffer.from(b)); },
    get(h){ return blobs.get(h); },
    has(h){ return blobs.has(h); },
    corrupt(h){ blobs.set(h, Buffer.from('rubbish')); },
    forget(h){ blobs.delete(h); },
    wipe(){ blobs.clear(); for(const k in recs) delete recs[k]; },
  };
  return sky;
}

function device(name, sky, opts){
  const o = opts || {};
  const CH = o.chunk || 4 * MB;
  const disk = o.disk || {};
  const st = { index: o.index || {}, trashed: [], parts: {}, moved: [],
               live: { small: 0, big: 0 }, peak: { small: 0, big: 0 }, saves: 0, publishes: 0 };
  const stat = (r) => ({ size: disk[r].length, mtime: (o.mtimes && o.mtimes[r]) || 1000 });

  const fs = {
    chunkBytes: CH,
    scanPage: async (id, so, off, lim) => {
      if(o.scanFails) throw new Error('the folder is no longer readable');
      const k = o.scanEmpty ? [] : Object.keys(disk).sort();
      const end = Math.min(k.length, off + lim), files = {};
      for(let i = off; i < end; i++){
        // Honours the SCAN's request, as both real adapters do: `so.hash` is how the sweep asks for
        // content identity, and a fake that ignored it could not show the difference between a
        // hashing sweep and one that trusts size and mtime.
        const wantHash = !!(so && so.hash) || !!o.hashed;
        files[k[i]] = Object.assign({}, stat(k[i]), wantHash ? { csum: sha(disk[k[i]]) } : {});
      }
      return { files, skipped: [], total: k.length, done: end >= k.length };
    },
    read: async (id, r) => new Uint8Array(disk[r]),
    readPart: async (id, r, off, len) => new Uint8Array(disk[r].subarray(off, off + len)),
    hashFile: async (id, r) => sha(disk[r] || Buffer.alloc(0)),
    /* Honest, like both real adapters: a path removed from `disk` is provably gone under a healthy
     * parent; a folder that cannot be read (scanFails) can prove nothing; and a LYING listing
     * (scanEmpty with files still on disk) answers "still there" — which is exactly the case the
     * probe exists to catch. */
    confirmGone: async (id, r) => o.scanFails ? { gone: false, parentAlive: false }
                                              : { gone: !(r in disk), parentAlive: true },
    writePart: async (id, r, off, bytes) => {
      const b = Buffer.from(bytes), cur = st.parts[r] || Buffer.alloc(0);
      const next = Buffer.alloc(Math.max(cur.length, off + b.length));
      cur.copy(next); b.copy(next, off); st.parts[r] = next;
    },
    partSize: async (id, r) => (st.parts[r] ? st.parts[r].length : 0),
    discardPart: async (id, r) => { delete st.parts[r]; },
    hashPart: async (id, r) => sha(st.parts[r] || Buffer.alloc(0)),
    writeCommit: async (id, r) => { disk[r] = st.parts[r] || Buffer.alloc(0); delete st.parts[r];
                                    return { size: disk[r].length, mtime: 2000 }; },
    write: async (id, r, bytes) => { disk[r] = Buffer.from(bytes);
                                     return { size: disk[r].length, mtime: 2000 }; },
    move: async (id, from, to) => { st.moved.push(from + ' -> ' + to);
                                    disk[to] = disk[from]; delete disk[from]; },
    trash: async (id, r) => { st.trashed.push(r); delete disk[r]; return '.pc-trash/x/' + r; },
    /* Abandoned part files, collected by age. The real adapters delete them off the disk; this
     * records that it was ASKED, which is the half the executor is responsible for. */
    sweepParts: async (id, olderThan) => { st.swept = (st.swept || 0) + 1; st.sweptAge = olderThan;
                                           return { removed: 0 }; },
  };

  const mark = (k, d) => { st.live[k] += d; st.peak[k] = Math.max(st.peak[k], st.live[k]); };
  /* BYTES held in flight, not just how many transfers overlap. The Windows app ran out of memory on
   * a sweep that "worked": four 16 MB files at once, each held as plaintext, ciphertext and a
   * request body. Counting transfers would have called that healthy. */
  st.bytes = 0; st.peakBytes = 0;
  const hold = (n) => { st.bytes += n; st.peakBytes = Math.max(st.peakBytes, st.bytes); };
  const chunker = realChunker(sky);
  chunker.CHUNK = CH;
  const io = {
    /* The transport contract, as sync.js implements it: throws when the server cannot be asked; on
     * an era shift it voids this device's journal BEFORE the executor reads it (the journal answers
     * for a dead world). `st.era` plays the state cache's role. */
    async state(){
      if(o.stateFail) throw new Error('the server did not answer');
      if(st.era !== undefined && st.era !== sky.era()) st.index = {};
      st.era = sky.era();
      const state = sky.folder(), flagged = {};
      for(const p in sky.recs){
        const r = sky.recs[p];
        if(r.era === sky.era() && r.bad && !r.t) flagged[p] = r.bad;
      }
      return { state, flagged, undecryptable: 0 };
    },
    async putState(key, batch, o2){
      st.publishes++;
      const out = { ok: [], stale: [], failed: [] };
      let tombs = 0;
      for(const r of batch) if(r.entry && r.entry.deletedAt) tombs++;
      if(tombs > 100 && !(o2 && o2.confirmed))
        throw new Error('refused: ' + tombs + ' tombstones need the deliberate delete flow');
      for(const r of batch){
        const res = sky.putRec(r.path, Object.assign({ by: name }, r.entry));
        (res === 'ok' ? out.ok : out.stale).push(r.path);
      }
      return out;
    },
    async flagBad(key, items){ for(const it of items || []) sky.flagRec(it.path, it.id); },
    index: async () => { if(o.indexFails) throw new Error('IndexedDB: UnknownError');
                         return JSON.parse(JSON.stringify(st.index)); },
    saveIndex: async (k, idx) => { st.saves++; st.index = JSON.parse(JSON.stringify(idx)); },
    hashBytes: async (b) => sha(Buffer.from(b)),
    putBlob: async (b) => {
      // What the real path holds at once: the plaintext, the ciphertext and the upload body.
      hold(b.length * 3);
      await new Promise(r => setTimeout(r, 2));
      const existed = sky.has(sha(Buffer.from(b)));
      const out = { sha: sky.put(Buffer.from(b)) };
      if(existed) out.existed = true;
      hold(-b.length * 3);
      return out;
    },
    getBlob: async (h) => {
      mark('small', 1);
      const size = (sky.get(h) || Buffer.alloc(0)).length;
      hold(size * 2);
      await new Promise(r => setTimeout(r, 2));
      hold(-size * 2);
      mark('small', -1);
      const b = sky.get(h);
      if(!b) throw new Error('blob ' + String(h).slice(0, 8) + ' unavailable (404)');
      return new Uint8Array(b);
    },
    /* THE SHIPPED PAIR, not a copy of it — see realChunker above. `CHUNK` is this device's size,
     * which is the arrangement that exists (16 MB desktop, 4 MB Android) and the one where
     * reassembly can go wrong. */
    putParts: (readPart, size, onProg, cs) => chunker.putParts(readPart, size, onProg, cs || CH),
    getParts: async (chunks, write, size, have, cs) => {
      mark('big', 1);
      try{
        // A connection that dies mid-file: the part file keeps whatever landed, exactly as on disk.
        let n = 0;
        return await chunker.getParts(chunks, async (off, bytes) => {
          if(o.dieAfter != null && n++ >= o.dieAfter) throw new Error('the connection went away');
          return write(off, bytes);
        }, size, have, cs);
      }
      finally { mark('big', -1); }
    },
    hasBlob: async (h) => sky.has(h),
  };

  return { name, fs, io, disk, st,
    sweep: (extra) => EXEC.sweep(fs, io, Object.assign(
      { id: name, key: 'Pictures', device: name, now: Date.now(),
        chunkAbove: CH, maxBytes: 8 * 1024 * MB }, extra || {})),
    verify: (extra) => EXEC.verify(fs, io, Object.assign(
      { id: name, key: 'Pictures', deep: true }, extra || {})) };
}

function photos(n, pre){
  const d = {};
  for(let i = 0; i < n; i++){
    const b = Buffer.alloc(600 + (i % 200));
    for(let o = 0; o + 4 <= b.length; o += 4) b.writeUInt32BE(((i * 2654435761) ^ o) >>> 0, o);
    d[(pre || 'DCIM/') + 'img' + i + '.jpg'] = b;
  }
  return d;
}
function video(mb, seed){
  const b = Buffer.alloc(Math.round(mb * MB));
  for(let o = 0; o + 4 <= b.length; o += 4) b.writeUInt32BE(((seed * 40503) ^ o) >>> 0, o);
  return b;
}
const identical = (a, b) => {
  const ka = Object.keys(a).sort(), kb = Object.keys(b).sort();
  if(ka.join('|') !== kb.join('|'))
    return 'different file lists (' + ka.length + ' vs ' + kb.length + '): '
           + ka.filter(x => !b[x]).slice(0,3).join(',') + ' / ' + kb.filter(x => !a[x]).slice(0,3).join(',');
  for(const k of ka) if(!a[k].equals(b[k])) return 'different bytes for ' + k;
  return null;
};

/* ---- the scenarios ---------------------------------------------------------------------------- */

const runs = [];
const scenario = (name, fn) => runs.push({ name, fn });

scenario('a fresh pair: everything goes up, the other device gets every byte', async (t) => {
  const sky = cloud();
  const A = device('laptop', sky, { disk: Object.assign(photos(300), { 'DCIM/clip.mp4': video(9, 7) }),
                                    chunk: 16 * MB });
  const r = await A.sweep();
  t.eq(r.failed.length, 0, 'the first sweep failed: ' + JSON.stringify(r.failed.slice(0, 2)));
  t.eq(r.uploaded.length, 301, 'uploaded ' + r.uploaded.length + ' of 301');
  const B = device('phone', sky, { chunk: 4 * MB });
  const r2 = await B.sweep();
  t.eq(r2.failed.length, 0, 'the joining device failed: ' + JSON.stringify(r2.failed.slice(0, 2)));
  t.eq(identical(A.disk, B.disk), null, 'the two devices do not hold the same files');
  t.eq(B.st.trashed.length, 0, 'the joining device trashed something');
});

scenario('two hosts updating at the same time — neither loses the other\'s work', async (t) => {
  const sky = cloud();
  const A = device('laptop', sky, { disk: photos(40) });
  await A.sweep();
  const B = device('phone', sky, {});
  await B.sweep();
  // Both add files without either seeing the other's sweep, and both publish.
  Object.assign(A.disk, photos(10, 'fromA/'));
  Object.assign(B.disk, photos(10, 'fromB/'));
  const A2 = device('laptop', sky, { disk: A.disk, index: A.st.index });
  const B2 = device('phone', sky, { disk: B.disk, index: B.st.index });
  await Promise.all([A2.sweep(), B2.sweep()]);
  // …and then each catches up with the other.
  const A3 = device('laptop', sky, { disk: A2.disk, index: A2.st.index });
  const B3 = device('phone', sky, { disk: B2.disk, index: B2.st.index });
  await A3.sweep(); await B3.sweep(); await A3.sweep();
  t.eq(Object.keys(A3.disk).length, 60, 'the laptop holds ' + Object.keys(A3.disk).length + ' of 60');
  t.eq(identical(A3.disk, B3.disk), null, 'the two devices did not converge');
});

scenario('three hosts, all writing, converge on the same folder', async (t) => {
  const sky = cloud();
  const names = ['laptop', 'phone', 'tablet'];
  const devs = names.map(n => device(n, sky, { disk: photos(5, n + '/') }));
  await Promise.all(devs.map(d => d.sweep()));
  const round2 = names.map((n, i) => device(n, sky, { disk: devs[i].disk, index: devs[i].st.index }));
  await Promise.all(round2.map(d => d.sweep()));
  const round3 = names.map((n, i) => device(n, sky, { disk: round2[i].disk, index: round2[i].st.index }));
  for(const d of round3) await d.sweep();
  for(const d of round3) t.eq(Object.keys(d.disk).length, 15,
                              d.name + ' holds ' + Object.keys(d.disk).length + ' of 15');
  t.eq(identical(round3[0].disk, round3[2].disk), null, 'the laptop and the tablet disagree');
});

scenario('a server that cannot be asked changes NOTHING — "SENDING EVERYTHING TO TRASH"', async (t) => {
  const sky = cloud();
  const A = device('laptop', sky, { disk: photos(200) });
  await A.sweep();
  const B = device('phone', sky, {});
  await B.sweep();
  /* The record set cannot be read. Under per-file records there is no partial view to misread —
   * the transport throws, and the sweep must change nothing at all. */
  const B2 = device('phone', sky, { disk: B.disk, index: B.st.index, stateFail: true });
  let err = null;
  try{ await B2.sweep(); }catch(e){ err = e; }
  t.ok(!!err, 'a sweep with no record set reported success');
  t.ok(/nothing has been changed/.test(String(err && err.message)), 'it does not say it was safe');
  t.eq(B2.st.trashed.length, 0, 'it trashed ' + B2.st.trashed.length + ' files');
  t.eq(Object.keys(B2.disk).length, 200, 'files went missing');
});

scenario('the store was emptied by hand — nothing is trashed without a person', async (t) => {
  const sky = cloud();
  const A = device('laptop', sky, { disk: photos(200) });
  await A.sweep();
  const kept = Object.assign({}, A.disk);
  sky.wipe();                                       // "i cleared out the Pictures in blossom"
  const A2 = device('laptop', sky, { disk: A.disk, index: A.st.index });
  const r = await A2.sweep();
  t.eq(A2.st.trashed.length, 0, 'an emptied store trashed ' + A2.st.trashed.length + ' files');
  t.eq(identical(A2.disk, kept), null, 'the files on disk changed');
  /* …and the holder PUTS THE FOLDER BACK: a record the store lost, for a file this journal knows
   * it applied and this disk still holds, is re-published — bytes included, since the wipe took
   * those too. Absence used to be carefully ignored; per-file, restoring one record risks nothing. */
  t.eq(r.uploaded.length, 200, 'the holder restored ' + r.uploaded.length + ' of 200 lost records');
  const C = device('tablet', sky, {});
  await C.sweep();
  t.eq(identical(A2.disk, C.disk), null, 'a joining device could not fetch the restored folder');
});

scenario('the folder handle is gone — nobody is told the files were deleted', async (t) => {
  const sky = cloud();
  const A = device('laptop', sky, { disk: photos(200) });
  await A.sweep();
  const B = device('phone', sky, { disk: photos(200), index: JSON.parse(JSON.stringify(A.st.index)),
                                   scanEmpty: true });
  await B.sweep();
  const C = device('laptop2', sky, {});
  await C.sweep();
  t.eq(Object.keys(C.disk).length, 200, 'a fresh device got ' + Object.keys(C.disk).length + ' of 200');
});

scenario('a reinstall lost the journal — it fetches what it lacks and duplicates nothing', async (t) => {
  const sky = cloud();
  const A = device('laptop', sky, { disk: photos(200) });
  await A.sweep();
  const half = {}; const mt = {}; let i = 0;
  for(const p in A.disk){ if(i++ < 100){ half[p] = A.disk[p]; mt[p] = 424242; } }
  /* AND ITS TIMESTAMPS ARE ITS OWN. This fixture used to give the phone the laptop's mtimes, so
   * size+mtime matched and the folder settled — which is not what a phone looks like: SAF stamps
   * its own last-modified on everything it writes, so nothing it holds can match by timestamp. The
   * test passed for a reason that does not hold on the device it is about. */
  const B = device('phone', sky, { disk: half, mtimes: mt });   // no index at all
  const r = await B.sweep();
  t.eq(identical(A.disk, B.disk), null, 'the reinstalled device did not converge');
  t.eq(B.st.trashed.length, 0, 'a reinstall trashed files');
  t.eq(r.conflicted.length, 0, 'a reinstall made ' + r.conflicted.length + ' conflict copies');
  t.eq(r.downloaded.length, 100, 'it fetched ' + r.downloaded.length + ' instead of the 100 it lacked');
});

scenario('an interrupted sweep resumes where it stopped', async (t) => {
  const sky = cloud();
  const A = device('laptop', sky, { disk: photos(300) });
  await A.sweep();
  const B = device('phone', sky, {});
  // Stopped part-way through the transfers: `stopping()` is asked once per file, so this lands in
  // the middle of the folder rather than after it.
  let n = 0;
  const first = await B.sweep({ shouldStop: () => (++n > 100) });
  t.ok(first.stopped === true, 'an interrupted sweep did not report itself stopped');
  const got = Object.keys(B.disk).length;
  t.ok(got > 0 && got < 300, 'it moved ' + got + ' of 300 — expected a partial folder');
  const B2 = device('phone', sky, { disk: B.disk, index: B.st.index });
  const second = await B2.sweep();
  t.eq(identical(A.disk, B2.disk), null, 'the resumed sweep did not finish the folder');
  t.ok(second.downloaded.length <= (300 - got) + EXEC.LANES * 2,
       'it re-fetched files it already had (' + second.downloaded.length + ' for '
       + (300 - got) + ' missing)');
});

scenario('corrupt bytes are refused, never written over a good file', async (t) => {
  const sky = cloud();
  const A = device('laptop', sky, { disk: photos(20) });
  await A.sweep();
  const victim = Object.keys(sky.folder())[0];
  sky.corrupt(sky.entry(victim).sha);
  const B = device('phone', sky, {});
  const r = await B.sweep();
  t.ok(!B.disk[victim], 'the corrupt file was written to disk');
  t.eq(r.failed.length, 1, 'the corruption was not reported (' + r.failed.length + ' failures)');
  t.ok(/checksum/.test(r.failed[0].error), 'reported as something other than a checksum failure: '
                                            + r.failed[0].error);
  t.eq(r.downloaded.length, 19, 'the other 19 files did not download');
});

scenario('a corrupt large file is refused too — the chunked path', async (t) => {
  const sky = cloud();
  const A = device('laptop', sky, { disk: { 'DCIM/clip.mp4': video(9, 3) }, chunk: 4 * MB });
  await A.sweep();
  const chunks = sky.entry('DCIM/clip.mp4').chunks;
  sky.corruptCipher(chunks[1]);
  const B = device('phone', sky, { chunk: 4 * MB });
  const r = await B.sweep();
  t.ok(!B.disk['DCIM/clip.mp4'], 'a corrupt video was committed');
  t.eq(r.failed.length, 1, 'the corruption was not reported');
});

scenario('the consistency check finds corruption, gaps and strays without changing anything', async (t) => {
  const sky = cloud();
  const A = device('laptop', sky, { disk: photos(30) });
  await A.sweep();
  const names = Object.keys(A.disk);
  A.disk[names[0]] = Buffer.from('tampered with');       // wrong bytes, wrong size
  A.disk[names[1]] = Buffer.alloc(A.disk[names[1]].length, 7);   // right size, wrong bytes
  delete A.disk[names[2]];                                        // missing here
  A.disk['DCIM/stray.jpg'] = Buffer.from('never synced');
  sky.forget(sky.entry(names[3]).sha);                            // the store lost the bytes
  const before = Object.keys(A.disk).length;
  const v = await A.verify();
  t.eq(v.corrupt.length, 2, 'found ' + v.corrupt.length + ' corrupt files, expected 2');
  t.eq(v.missingHere.length, 1, 'found ' + v.missingHere.length + ' missing, expected 1');
  t.eq(v.extra.length, 1, 'found ' + v.extra.length + ' strays, expected 1');
  t.eq(v.missingBytes.length, 1, 'found ' + v.missingBytes.length + ' lost by the store, expected 1');
  t.eq(Object.keys(A.disk).length, before, 'the check changed the folder');
  t.eq(A.st.trashed.length, 0, 'the check trashed something');
});

scenario('a delete on one device reaches the other, and only that', async (t) => {
  const sky = cloud();
  const A = device('laptop', sky, { disk: photos(200) });
  await A.sweep();
  const B = device('phone', sky, {});
  await B.sweep();
  const gone = 'DCIM/img7.jpg';
  delete A.disk[gone];
  const A2 = device('laptop', sky, { disk: A.disk, index: A.st.index });
  await A2.sweep();
  const B2 = device('phone', sky, { disk: B.disk, index: B.st.index });
  await B2.sweep();
  t.eq(B2.st.trashed.join(','), gone, 'the phone trashed ' + B2.st.trashed.length + ' files for one deletion');
  t.eq(Object.keys(B2.disk).length, 199, 'the phone holds ' + Object.keys(B2.disk).length + ', expected 199');
  // …and it stays deleted: a third sweep must not bring it back.
  const B3 = device('phone', sky, { disk: B2.disk, index: B2.st.index });
  await B3.sweep();
  t.ok(!B3.disk[gone], 'the deleted file came back');
});

scenario('a restored backup does not refill everybody else\'s folder', async (t) => {
  const sky = cloud();
  const A = device('laptop', sky, { disk: photos(200) });
  await A.sweep();
  const B = device('phone', sky, {});
  await B.sweep();
  for(const p of Object.keys(A.disk).slice(0, 60)) delete A.disk[p];
  const A2 = device('laptop', sky, { disk: A.disk, index: A.st.index });
  await A2.sweep({ manual: true, confirm: async () => true });
  // The phone's timestamps moved under it (an rsync without -t) and it has never hashed.
  const mt = {}; for(const p in B.disk) mt[p] = 999999;
  const B2 = device('phone', sky, { disk: B.disk, index: B.st.index, mtimes: mt });
  const r = await B2.sweep();
  t.ok(!!r.refused.find(x => x.kind === 'massResurrect'), 'the resurrection was not refused');
  const live = sky.liveCount();
  t.eq(live, 140, 'the folder came back to life: ' + live + ' live, expected 140');
});

scenario('big files go one at a time, small ones several — and every byte survives', async (t) => {
  const sky = cloud();
  const big = {};
  for(let i = 0; i < 4; i++) big['DCIM/vid' + i + '.mp4'] = video(9, i + 1);
  const A = device('laptop', sky, { disk: Object.assign(photos(60), big), chunk: 4 * MB });
  await A.sweep();
  const B = device('phone', sky, { chunk: 4 * MB });
  await B.sweep();
  t.ok(B.st.peak.small > 1, 'downloads still run one at a time (peak ' + B.st.peak.small + ')');
  t.eq(B.st.peak.big, 1, 'two large downloads overlapped (peak ' + B.st.peak.big + ')');
  t.eq(identical(A.disk, B.disk), null, 'parallel downloads did not reproduce the files exactly');
});

scenario('a settled folder is quiet — no re-upload, no republish, no writes', async (t) => {
  const sky = cloud();
  const A = device('laptop', sky, { disk: photos(200) });
  await A.sweep();
  const A2 = device('laptop', sky, { disk: A.disk, index: A.st.index });
  const r = await A2.sweep();
  t.eq(r.uploaded.length, 0, 'a settled folder re-uploaded ' + r.uploaded.length + ' files');
  t.eq(r.downloaded.length, 0, 'a settled folder downloaded ' + r.downloaded.length + ' files');
  t.eq(r.unchanged, 200, 'it checked ' + r.unchanged + ' of 200');
  t.eq(A2.st.trashed.length, 0, 'a settled folder trashed something');
});

scenario('a journal that cannot be read stops the sweep instead of guessing', async (t) => {
  const sky = cloud();
  const A = device('laptop', sky, { disk: photos(50), indexFails: true });
  let err = null;
  try{ await A.sweep(); }catch(e){ err = e; }
  t.ok(!!err, 'a sweep with an unreadable journal reported success');
  t.eq(A.st.trashed.length, 0, 'it trashed files');
  t.ok(/sync record/.test(String(err && err.message)), 'the failure does not say what it could not read');
});

scenario('a copy that fails its checksum is not fetched again for ever', async (t) => {
  const sky = cloud();
  const A = device('laptop', sky, { disk: photos(10) });
  await A.sweep();
  const victim = Object.keys(sky.folder())[0];
  /* Remembered by STORAGE ADDRESS, never csum: the holder's repair re-sends the same bytes under a
   * NEW address (fresh ciphertext), and a csum key would refuse the repair for ever. */
  const id = sky.entry(victim).sha;
  sky.corrupt(sky.entry(victim).sha);
  const B = device('phone', sky, {});
  const r1 = await B.sweep();
  t.ok(!!(r1.badFetch && r1.badFetch[victim]), 'the failed copy was not remembered');
  t.eq(r1.badFetch[victim].id, id, 'it was remembered by something other than the copy\u2019s identity');
  t.eq(r1.badFetch[victim].why, 'checksum', 'a checksum failure was recorded as something that expires');
  // The next sweep is told what failed, exactly as the client persists it — and must not spend the
  // bytes again. Reported as two videos failing on every sweep, all evening.
  const B2 = device('phone', sky, { disk: B.disk, index: B.st.index });
  const r2 = await B2.sweep({ skipFetch: r1.badFetch });
  t.eq(r2.failed.length, 0, 'it tried the known-bad copy again');
  t.ok((r2.unfetchable || []).length === 1, 'it did not report the file as unfetchable');
  // …and the block lifts by itself the moment the holder publishes a different copy.
  A.disk[victim] = Buffer.from('a repaired file');
  const A2 = device('laptop', sky, { disk: A.disk, index: A.st.index });
  await A2.sweep();
  const B3 = device('phone', sky, { disk: B2.disk, index: B2.st.index });
  const r3 = await B3.sweep({ skipFetch: r1.badFetch });
  t.eq(r3.downloaded.length, 1, 'a repaired copy was still blocked');
});

scenario('the preview and the sweep agree about what is outstanding', async (t) => {
  const sky = cloud();
  const A = device('laptop', sky, { disk: photos(50) });
  await A.sweep();
  const B = device('phone', sky, {});
  const dry = await B.sweep({ dryRun: true });
  t.eq(dry.plan.fetch.length, 50, 'the preview planned ' + dry.plan.fetch.length + ' of 50');
  t.eq(dry.downloaded.length, 0, 'a preview moved files');
  t.eq(Object.keys(B.disk).length, 0, 'a preview wrote to the disk');
  const real = await B.sweep();
  t.eq(real.downloaded.length, dry.plan.fetch.length,
       'Check offered ' + dry.plan.fetch.length + ' and the sweep did ' + real.downloaded.length);
});

scenario('records the folder lost are put back by whoever holds the files', async (t) => {
  const sky = cloud();
  const A = device('laptop', sky, { disk: photos(30) });
  await A.sweep();
  const B = device('phone', sky, {});
  await B.sweep();
  // Ten records vanish — a bad write, a cleared row, anything. The bytes and the journal are intact.
  const gone = Object.keys(sky.folder()).slice(0, 10);
  for(const p of gone) sky.dropRec(p);
  const A2 = device('laptop', sky, { disk: A.disk, index: A.st.index });
  const r = await A2.sweep();
  t.eq(Object.keys(sky.folder()).length, 30, 'it restored ' + Object.keys(sky.folder()).length + ' of 30 records');
  // The bytes were still in the store, so the uploads dedup to record writes.
  t.eq(r.uploaded.length, 10, 'it re-published ' + r.uploaded.length + ' of the 10 lost records');
  t.eq(r.alreadyStored, 10, 'it moved bytes the store already had');
  t.eq(A2.st.trashed.length, 0, 'it trashed something');
  // …and a joining device can still get everything.
  const C = device('tablet', sky, {});
  await C.sweep();
  t.eq(identical(A2.disk, C.disk), null, 'a new device could not fetch the restored folder');
});

scenario('a short read is refused, not stored under a checksum that certifies it', async (t) => {
  const sky = cloud();
  const A = device('laptop', sky, { disk: photos(6) });
  const victim = Object.keys(A.disk)[0];
  // The adapter hands back less than the file. THIS is the dangerous shape: the short buffer is what
  // gets hashed, so the entry's checksum would match the truncation and every later check agrees.
  const realRead = A.fs.read;
  A.fs.read = async (id, r) => (r === victim ? new Uint8Array(A.disk[r].subarray(0, 20))
                                             : realRead(id, r));
  const rep = await A.sweep();
  t.eq(rep.failed.length, 1, 'a short read was accepted (' + rep.failed.length + ' failures)');
  t.ok(/bytes of/.test(rep.failed[0].error), 'reported as something else: ' + rep.failed[0].error);
  t.ok(!sky.entry(victim), 'the truncated file was published anyway');
  t.eq(rep.uploaded.length, 5, 'the other five files did not upload');
});

scenario('a download interrupted mid-file resumes and is still byte-perfect', async (t) => {
  const sky = cloud();
  const A = device('laptop', sky, { disk: { 'DCIM/clip.mp4': video(9, 11) }, chunk: 4 * MB });
  await A.sweep();
  // The phone loses the connection after the first chunk lands.
  const B = device('phone', sky, { chunk: 4 * MB, dieAfter: 1 });
  const r1 = await B.sweep();
  t.eq(r1.failed.length, 1, 'the interrupted download was not reported as a failure');
  t.ok(!B.disk['DCIM/clip.mp4'], 'a half-written file was committed under the real name');
  t.ok((B.st.parts['DCIM/clip.mp4'] || Buffer.alloc(0)).length > 0, 'nothing was kept to resume from');
  // …and the next sweep finishes it rather than starting over.
  const B2 = device('phone', sky, { chunk: 4 * MB, disk: B.disk, index: B.st.index });
  B2.st.parts['DCIM/clip.mp4'] = B.st.parts['DCIM/clip.mp4'];
  const r2 = await B2.sweep();
  t.eq(r2.failed.length, 0, 'the resumed download failed: ' + JSON.stringify(r2.failed));
  t.eq(identical(A.disk, B2.disk), null, 'the resumed file is not byte-identical');
});

scenario('a stale part file is thrown away, not spliced — and does not blacklist a good copy', async (t) => {
  const sky = cloud();
  const A = device('laptop', sky, { disk: { 'DCIM/clip.mp4': video(9, 21) }, chunk: 4 * MB });
  await A.sweep();
  const B = device('phone', sky, { chunk: 4 * MB });
  // Left behind by an EARLIER version of the same path: the right length to look resumable, the
  // wrong bytes. Splicing onto it produces a file that is half one video and half another.
  B.st.parts['DCIM/clip.mp4'] = Buffer.alloc(4 * MB, 0x5A);
  const r = await B.sweep();
  t.eq(r.failed.length, 0, 'a stale part file failed the download: ' + JSON.stringify(r.failed));
  t.ok(!r.badFetch, 'a stale part file of OURS got the stored copy blacklisted');
  t.eq(identical(A.disk, B.disk), null, 'the file was spliced rather than re-fetched');
});

scenario('abandoned part files are collected, and only ones old enough', async (t) => {
  const sky = cloud();
  const A = device('laptop', sky, { disk: photos(3) });
  await A.sweep();
  t.ok(A.st.swept > 0, 'nothing ever collects abandoned .part files — an interrupted download leaks');
  t.eq(A.st.sweptAge, 24 * 3600000,
       'the age bound is ' + A.st.sweptAge + ' — a part file this sweep is about to resume from must survive');
});

scenario('a folder of large files does not hold them all at once — the Windows OOM', async (t) => {
  const sky = cloud();
  const big = {};
  // Eight files just under the chunking threshold: the size that is held WHOLE, which is exactly the
  // band that ran a desktop out of memory when four of them overlapped.
  for(let i = 0; i < 8; i++) big['DCIM/raw' + i + '.tif'] = video(12, i + 40);
  const A = device('laptop', sky, { disk: big, chunk: 16 * MB });
  const r = await A.sweep();
  t.eq(r.uploaded.length, 8, 'uploaded ' + r.uploaded.length + ' of 8');
  const ceiling = 12 * MB * 3 + MB;              // one file's worth of holding, plus slack
  t.ok(A.st.peakBytes <= ceiling,
       'held ' + Math.round(A.st.peakBytes / MB) + ' MB at once, ceiling '
       + Math.round(ceiling / MB) + ' MB — large uploads are overlapping again');

  // …and the same on the way down.
  const B = device('phone', sky, { chunk: 16 * MB });
  await B.sweep();
  t.ok(B.st.peakBytes <= 12 * MB * 2 + MB,
       'held ' + Math.round(B.st.peakBytes / MB) + ' MB at once on download');
  t.eq(identical(A.disk, B.disk), null, 'the files did not survive');
});

scenario('small files still overlap — the whole point of the lanes', async (t) => {
  const sky = cloud();
  const A = device('laptop', sky, { disk: photos(80) });
  await A.sweep();
  const B = device('phone', sky, {});
  await B.sweep();
  t.ok(B.st.peak.small > 1, 'downloads run one at a time again (peak ' + B.st.peak.small + ')');
});

scenario('a conflict whose incoming copy never arrives leaves the local file where it is', async (t) => {
  const sky = cloud();
  const A = device('laptop', sky, { disk: photos(4) });
  await A.sweep();
  const B = device('phone', sky, {});
  await B.sweep();
  // Both edit the same file without seeing each other: a genuine conflict.
  const p = 'DCIM/img0.jpg';
  A.disk[p] = Buffer.from('the laptop version, which is longer');
  B.disk[p] = Buffer.from('the phone version');
  const A2 = device('laptop', sky, { disk: A.disk, index: A.st.index, mtimes: { [p]: 5000 } });
  await A2.sweep();
  // …and the store loses the incoming copy before the phone can fetch it.
  sky.forget(sky.entry(p).sha);
  const B2 = device('phone', sky, { disk: B.disk, index: B.st.index, mtimes: { [p]: 5000 } });
  const r = await B2.sweep();
  /* "Can't be fetched", not "failed": a 404 is a fact about the STORE, and labelling it a failure
   * of this sweep invited pressing Sync now again — which retried every missing copy and printed
   * the same alarm again, for ever. The safety half is unchanged: the local file stays exactly
   * where it is, nothing is renamed, and the sweep does not claim a clean pass. */
  t.eq(r.failed.length, 0, 'a missing stored copy was labelled a sweep failure');
  t.eq((r.unfetchable || []).length, 1, 'a conflict whose copy is missing was not reported at all');
  t.ok(r.ok === false, 'a sweep with an unresolved conflict claimed a clean pass');
  t.ok(!!B2.disk[p], 'the local file was renamed away and nothing replaced it');
  t.eq(B2.disk[p].toString(), 'the phone version', 'the local copy was not left intact');
  t.eq(B2.st.moved.length, 0, 'it renamed the local copy before it had anything to put in its place');
});

scenario('a record that names no bytes is reported, never chased for ever', async (t) => {
  const sky = cloud();
  const A = device('laptop', sky, { disk: photos(3) });
  await A.sweep();
  // A live entry with neither a sha nor a chunk list: it says the file exists and not where it is.
  // Reported from a real folder — "Files → Blossom says this file has no stored copy" — and every
  // device planned a download, fetched nothing and failed, on every sweep.
  sky.injectRec('DCIM/orphan.mp4', { v: 1, by: 'laptop', size: 400, mtime: 1000 });
  const B = device('phone', sky, {});
  const r = await B.sweep();
  t.eq(r.failed.length, 0, 'it tried to fetch a file with no address: ' + JSON.stringify(r.failed));
  t.eq((r.unfetchable || []).length, 1, 'the unfetchable entry was not reported');
  t.ok(/does not say where/.test(r.unfetchable[0].why), r.unfetchable[0].why);
  t.eq(r.downloaded.length, 3, 'the other three files did not arrive');
  // …and the check names it too, which is where somebody would go looking.
  const v = await B.verify();
  t.eq((v.unaddressed || []).length, 1, 'the consistency check did not name it');
});

scenario('an upload that finishes without an address is refused, not published', async (t) => {
  const sky = cloud();
  const A = device('laptop', sky, { disk: photos(2) });
  // A store that accepts the bytes and answers with nothing at all.
  A.io.putBlob = async () => ({ sha: '' });
  const r = await A.sweep();
  t.eq(r.uploaded.length, 0, 'it recorded a file nothing can fetch');
  t.eq(r.failed.length, 2, 'the failure was not reported: ' + JSON.stringify(r.failed));
  t.ok(/without an address/.test(r.failed[0].error), r.failed[0].error);
  t.ok(!Object.keys(sky.folder()).length, 'an addressless entry was published anyway');
});

scenario('a sweep that loses the network resumes rather than starting over', async (t) => {
  const sky = cloud();
  const A = device('laptop', sky, { disk: photos(60) });
  await A.sweep();
  // The phone gets part of the way and the connection goes.
  const B = device('phone', sky, {});
  let n = 0;
  const realGet = B.io.getBlob;
  B.io.getBlob = async (h) => {
    if(++n > 20) throw new Error('network error: the connection went away');
    return realGet(h);
  };
  const r1 = await B.sweep();
  t.ok(r1.downloaded.length >= 15, 'it moved almost nothing before the break: ' + r1.downloaded.length);
  t.ok(r1.failed.length > 0, 'the break was not reported as a failure');
  t.ok(!r1.ok, 'a sweep with failures reported itself as clean — the scheduler then waits it out');

  // The network comes back. What it already moved is journalled, so the next sweep does the REST.
  const B2 = device('phone', sky, { disk: B.disk, index: B.st.index });
  const r2 = await B2.sweep();
  t.eq(r2.failed.length, 0, 'the retry failed: ' + JSON.stringify(r2.failed.slice(0, 2)));
  t.eq(identical(A.disk, B2.disk), null, 'the folder did not catch up');
  t.ok(r2.downloaded.length <= 60 - r1.downloaded.length + EXEC.LANES,
       're-fetched files it already had: ' + r2.downloaded.length + ' for '
       + (60 - r1.downloaded.length) + ' outstanding');
});

scenario('a phone that already holds the files does not conflict every one of them', async (t) => {
  const sky = cloud();
  const A = device('laptop', sky, { disk: photos(40) });
  await A.sweep();
  /* The phone HAS the folder — it synced before — but its journal is gone: a reinstall, cleared
   * storage, or simply a device joining a pair whose files it already has. And on Android the file
   * timestamps are whatever SAF assigned when it wrote them, which no other device has ever seen.
   *
   * Both sides then look changed for every path at once, and if the answer comes from size+mtime it
   * is "edited on both" — a conflict copy of the entire folder. Reported as "phone is now
   * downloading 1803 conflicts". */
  const mt = {};
  for(const p in A.disk) mt[p] = 777777;                    // SAF's own idea of last-modified
  const B = device('phone', sky, { disk: Object.assign({}, A.disk), mtimes: mt });
  const r = await B.sweep();
  t.eq(r.conflicted.length, 0, 'it made ' + r.conflicted.length + ' conflict copies of files it already had');
  t.eq(r.downloaded.length, 0, 're-downloaded ' + r.downloaded.length + ' files it already had');
  t.eq(r.uploaded.length, 0, 're-uploaded ' + r.uploaded.length + ' files that were already stored');
  t.ok(r.hashed === true, 'it settled the folder without hashing — the next device will conflict');
  t.eq(B.st.moved.length, 0, 'it renamed files aside');
});

scenario('a conflict against bytes that do not exist does not multiply every sweep', async (t) => {
  const sky = cloud();
  const A = device('laptop', sky, { disk: photos(30) });
  await A.sweep();
  const B = device('phone', sky, {});
  await B.sweep();

  /* Both sides change the same files, so every one of them is a genuine conflict — and then the
   * store loses the incoming bytes. THE LOOP: resolving a conflict renames the local file out of
   * the way and writes the incoming copy in its place, so a fetch that fails leaves the path empty;
   * the next sweep reads it as new elsewhere, fails again, and the sweep after that makes ANOTHER
   * conflict copy. Measured on a real folder: 1,803 conflicts, then 2,322, climbing every sweep,
   * with eleven thousand failed fetches in ten minutes. */
  const paths = Object.keys(A.disk).slice(0, 10);
  const mtA = {}, mtB = {};
  for(const p of paths){
    A.disk[p] = Buffer.from('laptop version of ' + p);
    B.disk[p] = Buffer.from('phone version of ' + p);
    mtA[p] = 5000; mtB[p] = 6000;
  }
  const A2 = device('laptop', sky, { disk: A.disk, index: A.st.index, mtimes: mtA });
  await A2.sweep();
  for(const p of paths) sky.forget(sky.entry(p).sha);           // the store loses the incoming bytes

  const B2 = device('phone', sky, { disk: B.disk, index: B.st.index, mtimes: mtB });
  const r1 = await B2.sweep();
  t.eq(B2.st.moved.length, 0, 'it renamed ' + B2.st.moved.length + ' files out of the way with '
       + 'nothing to put in their place');
  for(const p of paths) t.ok(!!B2.disk[p], 'the local copy of ' + p + ' disappeared');
  t.ok(!!r1.badFetch && Object.keys(r1.badFetch).length === 10, 'the dead copies were not remembered');
  for(const p of Object.keys(r1.badFetch)){
    t.eq(r1.badFetch[p].why, 'gone', 'a missing blob was remembered as permanent rather than expiring');
    t.ok(!!r1.badFetch[p].at, 'a missing blob was remembered without a clock, so it can never lift');
  }

  // The next sweep must not try again, and must not make a second copy of anything.
  const B3 = device('phone', sky, { disk: B2.disk, index: B2.st.index, mtimes: mtB });
  const r2 = await B3.sweep({ skipFetch: r1.badFetch });
  t.eq(r2.failed.length, 0, 'it chased the missing bytes again: ' + JSON.stringify(r2.failed.slice(0,2)));
  t.eq(B3.st.moved.length, 0, 'it renamed files aside on the retry');
  t.eq((r2.unfetchable || []).length, 10, 'it did not report why those paths cannot settle');
  t.eq(Object.keys(B3.disk).length, 30, 'the folder grew or shrank: ' + Object.keys(B3.disk).length);
});

scenario('a 404 is forgotten in time, and by a pressed button', async (t) => {
  const sky = cloud();
  const A = device('laptop', sky, { disk: photos(4) });
  await A.sweep();
  const victim = Object.keys(A.disk)[0];
  const sha = sky.entry(victim).sha;
  const bytes = sky.get(sha);
  sky.forget(sha);

  const B = device('phone', sky, {});
  const r1 = await B.sweep();
  t.ok(!!r1.badFetch[victim], 'the missing copy was not remembered');

  /* THE BYTES COME BACK — and blobs are content-addressed, so they come back under the SAME
   * identity. A block keyed on identity alone could never lift: one bad minute from a media server
   * would strand that path for ever, and neither a re-upload nor a restore could rescue it. */
  sky.putRaw(sha, bytes);
  const B2 = device('phone', sky, { disk: B.disk, index: B.st.index });
  const r2 = await B2.sweep({ manual: true, skipFetch: r1.badFetch });
  t.eq(r2.downloaded.length, 1, 'a pressed button did not retry a copy that is back');
  t.eq((r2.unfetchable || []).length, 0, 'it still refused to look');

  // …and an automatic sweep forgets it once the record is old enough.
  const B3 = device('phone', sky, {});
  const old = {}; old[victim] = { id: r1.badFetch[victim].id, why: 'gone', at: 1 };
  const r3 = await B3.sweep({ skipFetch: old });
  t.eq(r3.downloaded.length, 4, 'a stale 404 record still blocks an automatic sweep');
});

scenario('a checksum failure is NOT forgotten with time', async (t) => {
  const sky = cloud();
  const A = device('laptop', sky, { disk: photos(3) });
  await A.sweep();
  const victim = Object.keys(A.disk)[0];
  sky.corrupt(sky.entry(victim).sha);
  const B = device('phone', sky, {});
  const r1 = await B.sweep();
  t.eq(r1.badFetch[victim].why, 'checksum', 'a corrupt copy was recorded as something that expires');
  const stale = {}; stale[victim] = { id: r1.badFetch[victim].id, why: 'checksum', at: 1 };
  const B2 = device('phone', sky, { disk: B.disk, index: B.st.index });
  const r2 = await B2.sweep({ skipFetch: stale });
  t.eq(r2.failed.length, 0, 'it fetched the known-corrupt copy again after time passed');
});

scenario('an interrupted first sweep does not bring the conflict storm back', async (t) => {
  const sky = cloud();
  const A = device('laptop', sky, { disk: photos(30) });
  await A.sweep();
  /* The phone holds the folder with SAF timestamps and its journal covers almost none of it —
   * exactly what an interrupted first sweep leaves behind, because the settle entries are recorded
   * at the very end. "Empty journal" was too narrow a test: this is the back door the storm came
   * through. */
  const mt = {}; for(const p in A.disk) mt[p] = 777777;
  const partial = {};
  const first = Object.keys(A.disk)[0];
  partial[first] = Object.assign({}, sky.entry(first),
                                 { local: { size: A.disk[first].length, mtime: 777777 } });
  const B = device('phone', sky, { disk: Object.assign({}, A.disk), mtimes: mt, index: partial });
  const r = await B.sweep();
  t.eq(r.conflicted.length, 0, 'it made ' + r.conflicted.length + ' conflict copies');
  t.ok(r.hashed === true, 'it did not hash, so the folder cannot settle by content');
});

scenario('"send them again" actually sends them', async (t) => {
  const sky = cloud();
  const A = device('laptop', sky, { disk: photos(5) });
  await A.sweep();
  const paths = Object.keys(A.disk).slice(0, 3);
  for(const p of paths) sky.forget(sky.entry(p).sha);         // the store loses the bytes

  /* Clearing the journal does NOT make the next sweep upload: both sides then read as changed, the
   * reconciler asks whether they are the same anyway, the checksums match — because it IS the same
   * file — and it settles. The first version of this repair reported "3 queued to send again" and
   * sent nothing at all. */
  const A2 = device('laptop', sky, { disk: A.disk, index: A.st.index });
  const r = await A2.sweep({ manual: true, resend: paths });
  t.eq(r.uploaded.length, 3, 'it uploaded ' + r.uploaded.length + ' of the 3 asked for');
  t.eq(r.resent, 3, 'the resend was not recorded');
  for(const p of paths){
    const e = sky.entry(p);
    t.ok(!!e && sky.has(e.sha), 'the bytes for ' + p + ' are still not in the store');
  }
  // …and another device can now fetch them.
  const C = device('tablet', sky, {});
  const r2 = await C.sweep();
  t.eq(r2.failed.length, 0, 'a joining device still cannot fetch them: ' + JSON.stringify(r2.failed));
  t.eq(identical(A2.disk, C.disk), null, 'the folders do not match');
});

scenario('a sweep that could not settle a path does not call itself clean', async (t) => {
  const sky = cloud();
  const A = device('laptop', sky, { disk: photos(6) });
  await A.sweep();
  const victim = Object.keys(A.disk)[0];
  sky.forget(sky.entry(victim).sha);
  const B = device('phone', sky, {});
  const r1 = await B.sweep();
  const B2 = device('phone', sky, { disk: B.disk, index: B.st.index });
  const r2 = await B2.sweep({ skipFetch: r1.badFetch });
  t.eq((r2.unfetchable || []).length, 1, 'the unresolved path was not reported');
  t.ok(!r2.ok, 'it reported a clean sweep while a path could not be settled — the card then says '
       + '"in step" and the scheduler stamps the clock');
});

scenario('the scale that killed the desktop: 6,000 files, checked and quiet the second time', async (t) => {
  const N = parseInt(process.argv[2] || '6000', 10);
  const sky = cloud();
  const A = device('laptop', sky, { disk: photos(N) });
  const r = await A.sweep();
  t.eq(r.uploaded.length, N, 'uploaded ' + r.uploaded.length + ' of ' + N);
  t.ok(A.st.publishes >= 2, 'a long first sync never told the other devices until the end');
  const B = device('phone', sky, {});
  const r2 = await B.sweep();
  t.eq(r2.downloaded.length, N, 'downloaded ' + r2.downloaded.length + ' of ' + N);
  t.eq(identical(A.disk, B.disk), null, 'the folders differ at ' + N + ' files');
  const B2 = device('phone', sky, { disk: B.disk, index: B.st.index });
  const r3 = await B2.sweep();
  t.eq(r3.unchanged, N, 'the second sweep re-examined ' + (N - r3.unchanged) + ' files');
});

scenario('the CAS race: two devices edit one file at once — both copies survive, nothing silent', async (t) => {
  const sky = cloud();
  const A = device('laptop', sky, { disk: photos(6) });
  await A.sweep();
  const B = device('phone', sky, {});
  await B.sweep();
  const p = 'DCIM/img2.jpg';
  A.disk[p] = Buffer.from('the laptop edit');
  B.disk[p] = Buffer.from('the phone edit, which is different');
  const A2 = device('laptop', sky, { disk: A.disk, index: A.st.index, mtimes: { [p]: 5000 } });
  const B2 = device('phone', sky, { disk: B.disk, index: B.st.index, mtimes: { [p]: 6000 } });
  /* Both sweep against the SAME starting record, so both publish v2 — the server accepts exactly
   * one and refuses the other. The loser is struck from its own journal on the spot. */
  await Promise.all([A2.sweep(), B2.sweep()]);
  const winner = sky.entry(p);
  t.ok(!!winner && !winner.deletedAt, 'the record vanished in the race');
  /* Everyone sweeps again: the loser resolves the divergence as a conflict — both versions on both
   * disks, the winner under the real name. */
  let dA = A2.disk, iA = A2.st.index, dB = B2.disk, iB = B2.st.index;
  for(let round = 0; round < 3; round++){
    const A3 = device('laptop', sky, { disk: dA, index: iA });
    const B3 = device('phone', sky, { disk: dB, index: iB });
    await A3.sweep(); await B3.sweep();
    dA = A3.disk; iA = A3.st.index; dB = B3.disk; iB = B3.st.index;
  }
  t.eq(identical(dA, dB), null, 'the devices did not converge after the race');
  const names = Object.keys(dA).filter(x => x.indexOf('img2') !== -1);
  t.eq(names.length, 2, 'a copy was silently lost — ' + names.length + ' of 2 versions survive');
});

scenario('remove-and-re-add cannot haunt: the era kills the ghosts', async (t) => {
  const sky = cloud();
  const A = device('laptop', sky, { disk: photos(50) });
  await A.sweep();
  const B = device('phone', sky, {});
  await B.sweep();
  /* The pair is retired (Stop syncing everywhere) and re-added: one era bump. The phone comes back
   * with its old journal AND the old records still physically present — the exact state that
   * minted 373 conflicts, once per file, on the real phone. */
  sky.bumpEra();
  const A2 = device('laptop', sky, { disk: A.disk, index: A.st.index });
  const r1 = await A2.sweep();
  t.eq(r1.conflicted.length, 0, 'the re-seeding device conflicted with its own past life');
  t.eq(r1.uploaded.length, 50, 'the folder was not re-seeded into the new era');
  const B2 = device('phone', sky, { disk: B.disk, index: B.st.index });
  const r2 = await B2.sweep();
  t.eq(r2.conflicted.length, 0, 'the re-joining device minted ' + r2.conflicted.length + ' ghost conflicts');
  t.eq(r2.downloaded.length, 0, 're-downloaded ' + r2.downloaded.length + ' files it already had');
  t.eq(B2.st.trashed.length, 0, 'the era change trashed files');
  t.eq(identical(A2.disk, B2.disk), null, 'the pair did not converge in the new era');
});

scenario('the receipts: a torn store copy heals itself, end to end', async (t) => {
  const sky = cloud();
  const A = device('desktop', sky, { disk: photos(8) });
  await A.sweep();
  const victim = Object.keys(sky.folder())[0];
  /* THE TORN UPLOAD, faithfully: the file changed mid-upload, so the STORED bytes are a version
   * that never matched the record's checksum — a perfectly valid blob of the wrong content. (The
   * shipped uploader now hashes on both sides of the read, so this can no longer be produced; the
   * store still holds copies from the era when it could.) */
  const torn = Buffer.from('the half-written version the upload actually captured');
  const rec = sky.entry(victim);
  const tornSha = sky.put(torn);
  sky.injectRec(victim, Object.assign({}, rec, { sha: tornSha }));
  /* The holder's own journal recorded the torn address too — it is what it uploaded — with the
   * clean checksum beside it. That is exactly why the holder "settles clean" while every other
   * device fails the download. */
  A.st.index[victim] = Object.assign({}, A.st.index[victim], { sha: tornSha });
  /* The tablet pulls, refuses the poison, and FLAGS the record — exactly what the client does
   * after a sweep with a checksum badFetch. */
  const B = device('tablet', sky, {});
  const r1 = await B.sweep();
  t.eq(r1.badFetch[victim].why, 'checksum', 'the poison was not remembered as a checksum failure');
  await B.io.flagBad('Pictures', [{ path: victim, id: r1.badFetch[victim].id }]);
  /* The desktop's next ordinary sweep sees the flag, verifies its local copy is good, and re-sends
   * — a NEW storage address, so every refusal expires by itself. No buttons, no人 asking. */
  const A2 = device('desktop', sky, { disk: A.disk, index: A.st.index });
  const r2 = await A2.sweep();
  t.eq((r2.reseeding || []).length, 1, 'the holder did not re-seed the flagged file');
  t.ok(sky.entry(victim).sha !== r1.badFetch[victim].id, 'the re-send kept the poisoned address');
  /* And the tablet — still carrying its refusal — fetches clean, because the refusal is keyed on
   * the storage address the repair just replaced. */
  const B2 = device('tablet', sky, { disk: B.disk, index: B.st.index });
  const r3 = await B2.sweep({ skipFetch: r1.badFetch });
  t.eq(r3.failed.length, 0, 'the healed copy still failed: ' + JSON.stringify(r3.failed));
  t.eq(r3.downloaded.length, 1, 'the tablet did not fetch the healed copy');
  t.eq(identical(A2.disk, B2.disk), null, 'the folders do not match after the heal');
});

scenario('pause cuts into a big upload and the resume is nearly free', async (t) => {
  const sky = cloud();
  // One big chunked file plus photos. Pause lands DURING the big upload — the shape that used to
  // keep uploading a multi-GB ISO to 100% while the card said paused, wedging everything behind it.
  const A = device('laptop', sky, { disk: Object.assign(photos(5), { 'iso/win.iso': video(12, 99) }),
                                    chunk: 4 * MB });
  let reads = 0;
  const realRead = A.fs.readPart;
  A.fs.readPart = async (id, r, off, len) => { reads++; return realRead(id, r, off, len); };
  const r1 = await A.sweep({ shouldStop: () => reads >= 2 });   // stop mid-ISO, chunk 2 of 3
  t.ok(r1.stopped === true, 'a pause during a chunked upload did not report itself stopped');
  t.eq(r1.failed.length, 0, 'a user pause was recorded as a failure: ' + JSON.stringify(r1.failed));
  t.ok(!sky.entry('iso/win.iso'), 'a half-uploaded file was published anyway');
  const uploadedChunks = reads;
  // The next sweep finishes it — and the chunks already stored are skipped, not re-sent.
  const A2 = device('laptop', sky, { disk: A.disk, index: A.st.index, chunk: 4 * MB });
  const r2 = await A2.sweep();
  t.eq(r2.failed.length, 0, 'the resume failed: ' + JSON.stringify(r2.failed));
  const e = sky.entry('iso/win.iso');
  t.ok(!!e && e.chunks && e.chunks.length === 3, 'the ISO never finished: ' + JSON.stringify(e && e.chunks));
  const B = device('phone', sky, { chunk: 4 * MB });
  await B.sweep();
  t.eq(identical(A2.disk, B.disk), null, 'the resumed ISO is not byte-identical');
  void uploadedChunks;
});

scenario('a blob deleted out from under a seed puts itself back — no buttons', async (t) => {
  const sky = cloud();
  const A = device('desktop', sky, { disk: photos(6) });
  await A.sweep();
  /* Somebody reclaimed mid-seed: the bytes are gone from the store, the records and the
   * desktop's files are fine. Nothing about this repair may require a person. */
  const victims = Object.keys(sky.folder()).slice(0, 3);
  for(const p of victims) sky.forget(sky.entry(p).sha);
  // A joining device fails the fetches, remembers them, and FLAGS the records — as the client does.
  const B = device('phone', sky, {});
  const r1 = await B.sweep();
  t.eq((r1.unfetchable || []).length, 3, 'the missing bytes were not reported');
  const fl = [];
  for(const p in (r1.badFetch || {})){ const r = r1.badFetch[p];
    if(r && r.id && (r.why === 'checksum' || r.why === 'gone')) fl.push({ path: p, id: r.id }); }
  t.eq(fl.length, 3, 'gone blobs were not flagged for the holder');
  await B.io.flagBad('Pictures', fl);
  // The desktop's next ORDINARY sweep re-seeds them — same bytes, same address, bytes restored.
  const A2 = device('desktop', sky, { disk: A.disk, index: A.st.index });
  const r2 = await A2.sweep();
  t.eq((r2.reseeding || []).length, 3, 'the holder did not re-seed: ' + JSON.stringify(r2.reseeding));
  for(const p of victims) t.ok(sky.has(sky.entry(p).sha), 'the bytes for ' + p + ' are still missing');
  /* And the phone's next AUTOMATIC sweep retries at once — the record's version moved past the
   * memory, so the six-hour clock does not apply. */
  const B2 = device('phone', sky, { disk: B.disk, index: B.st.index });
  const r3 = await B2.sweep({ skipFetch: r1.badFetch });
  t.eq(r3.failed.length, 0, 'the retry failed: ' + JSON.stringify(r3.failed));
  t.eq(r3.downloaded.length, 3, 'the healed files were not fetched (' + r3.downloaded.length + ' of 3)');
  t.eq(identical(A2.disk, B2.disk), null, 'the folders do not match after the self-heal');
});

scenario('a holder whose own copy is ALSO bad re-seeds nothing and says so', async (t) => {
  const sky = cloud();
  const A = device('desktop', sky, { disk: photos(4) });
  await A.sweep();
  const victim = Object.keys(sky.folder())[0];
  sky.corrupt(sky.entry(victim).sha);
  sky.flagRec(victim, sky.entry(victim).sha);
  A.disk[victim] = Buffer.from('rotted on this disk too');       // bit-rot on the holder
  const A2 = device('desktop', sky, { disk: A.disk, index: A.st.index });
  const r = await A2.sweep();
  t.eq((r.reseeding || []).length, 0, 'it re-seeded a copy it could not verify');
  t.ok((r.badHere || []).indexOf(victim) !== -1, 'the rot on this device was not named');
});

scenario('pause cuts into a big DOWNLOAD too, and the part file resumes it', async (t) => {
  const sky = cloud();
  const A = device('laptop', sky, { disk: { 'iso/big.iso': video(12, 55) }, chunk: 4 * MB });
  await A.sweep();
  let writes = 0;
  const B = device('phone', sky, { chunk: 4 * MB });
  const realWrite = B.fs.writePart;
  B.fs.writePart = async (id, r, off, bytes) => { writes++; return realWrite(id, r, off, bytes); };
  const r1 = await B.sweep({ shouldStop: () => writes >= 2 });
  t.ok(r1.stopped === true, 'a pause during a chunked download did not report itself stopped');
  t.eq(r1.failed.length, 0, 'a user pause was recorded as a failure: ' + JSON.stringify(r1.failed));
  t.ok(!B.disk['iso/big.iso'], 'a half-fetched file was committed under the real name');
  t.ok((B.st.parts['iso/big.iso'] || Buffer.alloc(0)).length > 0, 'the part file was thrown away');
  const B2 = device('phone', sky, { chunk: 4 * MB, disk: B.disk, index: B.st.index });
  B2.st.parts['iso/big.iso'] = B.st.parts['iso/big.iso'];
  const r2 = await B2.sweep();
  t.eq(r2.failed.length, 0, 'the resumed download failed: ' + JSON.stringify(r2.failed));
  t.eq(identical(A.disk, B2.disk), null, 'the resumed file is not byte-identical');
});

scenario('a crash that lost the journal checkpoint settles by content — no re-upload, no conflicts', async (t) => {
  const sky = cloud();
  const A = device('laptop', sky, { disk: photos(60) });
  await A.sweep();
  /* The renderer died between publishing records and saving the journal — the records run AHEAD
   * of the journal, which is the safe direction and must stay free: half the journal is gone, the
   * records and the disk are complete. */
  const partial = {};
  const keys = Object.keys(A.st.index);
  for(let i = 0; i < keys.length / 2; i++) partial[keys[i]] = A.st.index[keys[i]];
  const A2 = device('laptop', sky, { disk: A.disk, index: partial });
  const r = await A2.sweep();
  t.eq(r.uploaded.length, 0, 'a lost checkpoint re-uploaded ' + r.uploaded.length + ' files');
  t.eq(r.conflicted.length, 0, 'a lost checkpoint minted ' + r.conflicted.length + ' conflicts');
  t.eq(A2.st.trashed.length, 0, 'a lost checkpoint trashed files');
  t.eq(r.downloaded.length, 0, 'a lost checkpoint re-downloaded its own files');
});

scenario('zero-byte files are files', async (t) => {
  const sky = cloud();
  const disk = photos(3);
  disk['empty/placeholder.txt'] = Buffer.alloc(0);
  const A = device('laptop', sky, { disk });
  const r1 = await A.sweep();
  t.eq(r1.failed.length, 0, 'an empty file failed to upload: ' + JSON.stringify(r1.failed));
  t.eq(r1.uploaded.length, 4, 'the empty file was skipped');
  const B = device('phone', sky, {});
  const r2 = await B.sweep();
  t.eq(r2.failed.length, 0, 'an empty file failed to download: ' + JSON.stringify(r2.failed));
  t.eq(identical(A.disk, B.disk), null, 'the folders differ');
  const B2 = device('phone', sky, { disk: B.disk, index: B.st.index });
  const r3 = await B2.sweep();
  t.eq(r3.unchanged, 4, 'an empty file cannot settle — it re-decides every sweep');
});

scenario('the era changing mid-sweep fails that sweep cleanly and the next one rejoins fresh', async (t) => {
  const sky = cloud();
  const A = device('laptop', sky, { disk: photos(30) });
  await A.sweep();
  // Another device retires the pair while this one is mid-sweep: its next publish is refused with
  // the new era. The sweep must end as a FAILURE (nothing corrupted), and the next sweep — reading
  // the new era — must void its journal and re-seed into the new world without conflicts.
  Object.assign(A.disk, photos(5, 'new/'));
  const A2 = device('laptop', sky, { disk: A.disk, index: A.st.index });
  const realPut = A2.io.putState.bind(A2.io);
  A2.io.putState = async (k, recs, o2) => { sky.bumpEra(); throw new Error('the folder was retired or re-added elsewhere'); };
  /* The sweep may end by THROWING here — a failed final publish is a failed sweep, said loudly —
   * and what matters is what it did NOT do: corrupt the journal or touch a file. */
  let r1 = null, threw = '';
  try{ r1 = await A2.sweep(); }catch(e){ threw = String((e && e.message) || e); }
  t.ok(threw || (r1 && (r1.checkpointError || r1.failed.length > 0 || !r1.ok)),
       'an era change mid-sweep claimed a clean pass');
  t.eq(A2.st.trashed.length, 0, 'an era change trashed files');
  const A3 = device('laptop', sky, { disk: A2.disk, index: A2.st.index });
  const r2 = await A3.sweep();
  t.eq(r2.failed.length, 0, 'the rejoin failed: ' + JSON.stringify(r2.failed.slice(0, 2)));
  t.eq(r2.conflicted.length, 0, 'the rejoin minted ' + r2.conflicted.length + ' conflicts');
  t.eq(Object.keys(sky.folder()).length, 35, 'the new era holds ' + Object.keys(sky.folder()).length + ' of 35');
  void realPut;
});

scenario('a name Windows cannot hold is refused with the fix, not failed for ever', async (t) => {
  const sky = cloud();
  const A = device('linux', sky, { disk: { 'notes:v2.txt': Buffer.from('colon'), 'ok.txt': Buffer.from('fine'),
                                           'aux.cfg': Buffer.from('reserved'), 'trail. ': Buffer.from('dot') } });
  await A.sweep();
  const B = device('windows', sky, {});
  B.fs.platform = 'win32';
  const r = await B.sweep();
  t.eq(r.failed.length, 0, 'impossible names were retried as failures: ' + JSON.stringify(r.failed));
  t.eq((r.unfetchable || []).filter(u => /cannot exist on Windows/.test(u.why)).length, 3,
       'the impossible names were not refused by name: ' + JSON.stringify(r.unfetchable));
  t.ok(!!B.disk['ok.txt'], 'the ordinary file did not arrive');
});

/* ---- runner ----------------------------------------------------------------------------------- */
(async () => {
  let bad = 0;
  for(const s of runs){
    const errs = [];
    const t = { ok: (c, w) => { if(!c) errs.push(w); },
                eq: (a, b, w) => { if(a !== b) errs.push(w + ' [' + JSON.stringify(a) + ' != '
                                                    + JSON.stringify(b) + ']'); } };
    const t0 = Date.now();
    try{ await s.fn(t); }catch(e){ errs.push('threw: ' + ((e && e.stack) || e)); }
    const ms = Date.now() - t0;
    if(errs.length){ bad++; console.log('FAIL  ' + s.name); for(const e of errs) console.log('        ' + e); }
    else console.log('ok    ' + s.name + '  (' + ms + 'ms)');
  }
  if(bad){ console.log('\n' + bad + ' of ' + runs.length + ' scenarios FAILED'); process.exit(1); }
  console.log('\nOK  all ' + runs.length + ' scenarios');
})();
