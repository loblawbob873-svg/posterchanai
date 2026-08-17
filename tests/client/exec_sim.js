/* THE NEW SYNC, END TO END — real devices, real bytes, every failure that has been reported.
 *
 * This drives the shipped syncengine.js + syncexec.js through a fake network and a fake filesystem
 * that behave like the real ones: content-addressed blobs, one document per device that only that
 * device may write, a journal in "IndexedDB", and files whose bytes are checked at every step. A
 * wrong offset, a dropped chunk or a truncation changes a hash and fails the run.
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
require(path.join(__dirname, '..', '..', 'static', 'js', 'client', 'syncengine.js'));
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
  const docs = {};                       // device -> {path: entry}
  return {
    docs,
    putCipher(h, b){ ciphers.set(h, Buffer.from(b)); },
    getCipher(h){ return ciphers.get(h); },
    hasCipher(h){ return ciphers.has(h); },
    corruptCipher(h){ ciphers.set(h, Buffer.from('rubbish that is long enough to look like a chunk')); },
    down: new Set(),                     // devices whose document cannot be read right now
    put(b){ const h = sha(b); blobs.set(h, Buffer.from(b)); return h; },
    get(h){ return blobs.get(h); },
    has(h){ return blobs.has(h); },
    corrupt(h){ blobs.set(h, Buffer.from('rubbish')); },
    forget(h){ blobs.delete(h); },
    wipe(){ blobs.clear(); for(const k in docs) delete docs[k]; },
  };
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
        files[k[i]] = Object.assign({}, stat(k[i]), o.hashed ? { csum: sha(disk[k[i]]) } : {});
      }
      return { files, skipped: [], total: k.length, done: end >= k.length };
    },
    read: async (id, r) => new Uint8Array(disk[r]),
    readPart: async (id, r, off, len) => new Uint8Array(disk[r].subarray(off, off + len)),
    hashFile: async (id, r) => sha(disk[r] || Buffer.alloc(0)),
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
    async views(){
      if(o.viewsFail) throw new Error('the server did not answer');
      const views = {}; let missing = 0;
      for(const dev in sky.docs){
        if(sky.down.has(dev)){ missing++; continue; }
        views[dev] = JSON.parse(JSON.stringify(sky.docs[dev]));
      }
      missing += o.extraMissing || 0;
      return { views, missing };
    },
    async publish(key, entries){
      st.publishes++;
      sky.docs[name] = JSON.parse(JSON.stringify(entries));
    },
    index: async () => { if(o.indexFails) throw new Error('IndexedDB: UnknownError');
                         return JSON.parse(JSON.stringify(st.index)); },
    saveIndex: async (k, idx) => { st.saves++; st.index = JSON.parse(JSON.stringify(idx)); },
    hashBytes: async (b) => sha(Buffer.from(b)),
    putBlob: async (b) => {
      // What the real path holds at once: the plaintext, the ciphertext and the upload body.
      hold(b.length * 3);
      await new Promise(r => setTimeout(r, 2));
      const out = { sha: sky.put(Buffer.from(b)) };
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

scenario('a device that cannot be read deletes nothing — "SENDING EVERYTHING TO TRASH"', async (t) => {
  const sky = cloud();
  const A = device('laptop', sky, { disk: photos(200) });
  await A.sweep();
  const B = device('phone', sky, {});
  await B.sweep();
  // The laptop's document goes unreadable. Its 200 files must not vanish from the phone.
  sky.down.add('laptop');
  const B2 = device('phone', sky, { disk: B.disk, index: B.st.index, extraMissing: 0 });
  const r = await B2.sweep();
  t.eq(B2.st.trashed.length, 0, 'it trashed ' + B2.st.trashed.length + ' files');
  t.eq(Object.keys(B2.disk).length, 200, 'files went missing');
  t.ok(r.missingViews >= 1, 'the unreadable device was not counted');
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
  const half = {}; let i = 0;
  for(const p in A.disk){ if(i++ < 100) half[p] = A.disk[p]; }
  const B = device('phone', sky, { disk: half });            // no index at all
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
  const victim = Object.keys(sky.docs.laptop)[0];
  sky.corrupt(sky.docs.laptop[victim].sha);
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
  const chunks = sky.docs.laptop['DCIM/clip.mp4'].chunks;
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
  sky.forget(sky.docs.laptop[names[3]].sha);                      // the store lost the bytes
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
  const live = Object.keys(sky.docs.laptop).filter(p => !sky.docs.laptop[p].deletedAt).length;
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
  const victim = Object.keys(sky.docs.laptop)[0];
  const id = sky.docs.laptop[victim].csum;
  sky.corrupt(sky.docs.laptop[victim].sha);
  const B = device('phone', sky, {});
  const r1 = await B.sweep();
  t.ok(!!(r1.badFetch && r1.badFetch[victim]), 'the failed copy was not remembered');
  t.eq(r1.badFetch[victim], id, 'it was remembered by something other than the copy\u2019s identity');
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

scenario('a device whose own document is lost puts it back from its journal', async (t) => {
  const sky = cloud();
  const A = device('laptop', sky, { disk: photos(30) });
  await A.sweep();
  const B = device('phone', sky, {});
  await B.sweep();
  // The laptop's document is wiped — a bad write, a cleared record, anything. Its journal is intact.
  delete sky.docs.laptop;
  const A2 = device('laptop', sky, { disk: A.disk, index: A.st.index });
  const r = await A2.sweep();
  t.ok(!!sky.docs.laptop, 'the lost document was not restored');
  t.eq(Object.keys(sky.docs.laptop).length, 30, 'it restored ' + Object.keys(sky.docs.laptop || {}).length + ' of 30');
  t.eq(r.uploaded.length, 0, 'it re-uploaded ' + r.uploaded.length + ' files to do it');
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
  t.ok(!sky.docs.laptop[victim], 'the truncated file was published anyway');
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
