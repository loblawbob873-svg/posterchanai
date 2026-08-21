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
const E = globalThis.PCSyncState;      // the engine, for the decisions a scenario has to make itself

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
    flagRec(p, id, got){ const r = recs[p];
      // `<address>` from an older device, `<address>|<hash the downloader measured>` now.
      if(r && r.era === era && !r.t) r.bad = String(id) + (got ? '|' + String(got) : ''); },
    putCipher(h, b){ ciphers.set(h, Buffer.from(b)); },
    getCipher(h){ return ciphers.get(h); },
    hasCipher(h){ return ciphers.has(h); },
    corruptCipher(h){ ciphers.set(h, Buffer.from('rubbish that is long enough to look like a chunk')); },
    put(b){ const h = sha(b); blobs.set(h, Buffer.from(b)); return h; },
    putRaw(h, b){ blobs.set(h, Buffer.from(b)); },
    get(h){ return blobs.get(h); },
    has(h){ return blobs.has(h); },
    corrupt(h){ blobs.set(h, Buffer.from('rubbish')); },
    /* Take the ADDRESS off a record — a tombstone for a file whose bytes were never stored, or one
     * written by an older build. There is then nothing to confirm and nothing to restore from. */
    strip(path){
      const r = recs[path];
      if(!r) return;
      for(const k of ['sha', 'chunks', 'cs', 'ps']) delete r.entry[k];
    },
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
      /* `.pc-trash` IS NOT PART OF THE FOLDER, and both real adapters already know it: the desktop
       * bridge's walk ignores the directory outright and SafFs skips it plus the `.parts.json`
       * resume registry inside it. A fake that listed them would let the sweep sync its own
       * bookkeeping to every device — and, worse, would hide the fact that it does. */
      const k = o.scanEmpty ? []
        : Object.keys(disk).filter(p => p.indexOf('.pc-trash/') !== 0
                                     && p.indexOf('/.pc-trash/') < 0).sort();
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
    /* REMOVE, not "move somewhere else". The trash is one place now and it is on the server, so a
     * deletion here really deletes — which is only safe because the executor has already asked the
     * store whether it still holds the bytes. `st.trashed` keeps its name: what it records is
     * "files this sweep deleted locally", which is what every scenario below asserts about. */
    remove: async (id, r) => {
      if(!(r in disk)) return true;               // already gone is where it was going
      st.trashed.push(r); delete disk[r]; return true;
    },
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
  /* Bytes AND count, because they answer different questions. Bytes is the memory guarantee (the
   * Windows OOM); the count is whether anything overlaps at all — and a folder of photographs going
   * strictly one at a time is bounded by round trips rather than by bandwidth, which no byte
   * measurement would ever show. */
  st.inflight = 0; st.peakInflight = 0;
  const hold = (n) => { st.bytes += n; st.peakBytes = Math.max(st.peakBytes, st.bytes);
                        // hold() RELEASES with a negative value; the count follows the sign.
                        st.inflight += (n >= 0 ? 1 : -1);
                        st.peakInflight = Math.max(st.peakInflight, st.inflight); };
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
      /* MIRRORS THE SHIPPED TRANSPORT, which is the only reason this fake is worth anything. The
       * server answers 409 + {backstop}; `stateS.put` turns that into every path in the batch
       * STRUCK (`stale`) plus a count, rather than the throw it used to be — a throw killed the
       * whole flush, left the journal intact, and had the device propose the same deletions on
       * every sweep for ever. A sim that kept throwing would agree with that bug. */
      if(tombs > 100 && !(o2 && o2.confirmed)){
        for(const r of batch) out.stale.push(r.path);
        out.backstop = batch.length;
        return out;
      }
      for(const r of batch){
        const res = sky.putRec(r.path, Object.assign({ by: name }, r.entry));
        (res === 'ok' ? out.ok : out.stale).push(r.path);
      }
      return out;
    },
    async flagBad(key, items){ for(const it of items || []) sky.flagRec(it.path, it.id, it.got); },
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
/* `.pc-trash` is DELIBERATELY per-device — one device's trash and its resume bookkeeping are not
 * part of what the folder holds, and neither adapter scans them. Comparing two devices' CONTENT
 * therefore means comparing what the folder agreed on, not what each machine keeps beside it. */
const _content = (m) => { const o = {};
  for(const k in m) if(k.indexOf('.pc-trash/') !== 0 && k.indexOf('/.pc-trash/') < 0) o[k] = m[k];
  return o; };
const identical = (a0, b0) => {
  const a = _content(a0), b = _content(b0);
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

scenario('a device that rewrites what it downloads does not start a round trip', async (t) => {
  /* THE REPORT: two Android devices added to a folder that had been clean for days, and the same
   * receipts climbing to version SIX and EIGHT within hours. Android's media scanner rewrites JPEG
   * metadata in place, so the phone's copy of a file it just downloaded really does hash
   * differently — every sweep in the loop is individually correct, which is why nothing caught it. */
  const sky = cloud();
  const A = device('desktop', sky, { disk: photos(12) });
  await A.sweep();

  // The phone joins, fetches everything, and holds the same bytes.
  const B = device('phone', sky, {});
  const r1 = await B.sweep();
  t.eq(r1.failed.length, 0, 'the join failed: ' + JSON.stringify(r1.failed.slice(0, 2)));
  t.eq(identical(A.disk, B.disk), null, 'the phone did not end up with the desktop\'s files');

  // …then the phone rewrites what it received, exactly as the media scanner does: same file, new
  // bytes, new timestamp. Not an edit anybody made.
  const victim = Object.keys(B.disk).sort()[0];
  B.disk[victim] = Buffer.concat([B.disk[victim], Buffer.from('EXIF')]);

  const B2 = device('phone', sky, { disk: B.disk, index: B.st.index,
                                    mtimes: { [victim]: 987654 } });
  const r2 = await B2.sweep();
  t.eq(r2.uploaded.length, 0, 'the phone published ' + r2.uploaded.length
       + ' file(s) it had rewritten after downloading — that is the round trip');
  t.eq((r2.rewrittenAfterDownload || []).length, 1,
       'the sweep did not NAME the path it held back');
  t.eq(sky.recs[victim].v, 1, 'the record moved to v' + sky.recs[victim].v
       + ' — every other device now has a version to react to');

  // The rewrite is a one-time finding, not the permanent state of this folder. Its measured local
  // checksum becomes the new device baseline, while the shared checksum stays untouched.
  const B3 = device('phone', sky, { disk: B2.disk, index: B2.st.index,
                                    mtimes: { [victim]: 987654 } });
  const rQuiet = await B3.sweep();
  t.eq((rQuiet.rewrittenAfterDownload || []).length, 0,
       'the same Android rewrite was reported again on the next fresh sweep');
  t.eq(rQuiet.uploaded.length, 0, 'the quiet follow-up published the held-back rewrite');

  // And the desktop, sweeping after it, has nothing to do: no fetch, no publish, no round trip.
  const A2 = device('desktop', sky, { disk: A.disk, index: A.st.index });
  const r3 = await A2.sweep();
  t.eq(r3.uploaded.length, 0, 'the desktop republished after the phone was held back');
  t.eq(r3.downloaded.length, 0, 'the desktop fetched the rewrite');
});

scenario('a fresh phone settles Android rewrites quietly and finishes its first sync', async (t) => {
  const sky = cloud();
  const A = device('desktop', sky, { disk: photos(300) });
  await A.sweep();

  // The first join is interrupted after some files have landed and been journalled.
  const B = device('phone', sky, {});
  let checks = 0;
  const partial = await B.sweep({ shouldStop: () => ++checks > 100 });
  t.ok(partial.stopped === true, 'the setup did not interrupt the first sync');
  const victim = Object.keys(B.disk).sort()[0];
  t.ok(!!victim, 'nothing landed before the interruption');
  B.disk[victim] = Buffer.concat([B.disk[victim], Buffer.from('EXIF')]);

  // This is still a thin journal: resume the join, keep the provider rewrite local, and do not put
  // a giant warning in front of somebody setting up their phone.
  const B2 = device('phone', sky, { disk: B.disk, index: B.st.index,
                                    mtimes: { [victim]: 987654 } });
  const resumed = await B2.sweep();
  t.eq(resumed.uploaded.length, 0, 'the fresh phone published its provider rewrite');
  t.eq((resumed.rewrittenAfterDownload || []).length, 0,
       'the first sync displayed the established-device rewrite warning');
  t.eq(resumed.localRewritesSettled || 0, 1, 'the local rewrite was not accounted for');
  t.eq(Object.keys(B2.disk).length, 300, 'the resumed first sync did not finish the folder');

  const B3 = device('phone', sky, { disk: B2.disk, index: B2.st.index,
                                    mtimes: { [victim]: 987654 } });
  const quiet = await B3.sweep();
  t.eq((quiet.rewrittenAfterDownload || []).length, 0, 'the settled rewrite came back again');
  t.eq(quiet.uploaded.length, 0, 'the quiet follow-up published the provider rewrite');
});

scenario('a refused mass delete is not proposed again — the files come back instead', async (t) => {
  /* THE REPORT: "REFUSED 400 tombstones for Documents (backstop)" at 10:40 and 400 more for
   * Pictures at 10:46, on a phone whose folder had been emptied. The server was right to refuse.
   * The client then threw, kept every journal entry, and proposed exactly the same deletions on the
   * next sweep — a device with no way forward and a person watching sync do nothing. */
  const sky = cloud();
  const A = device('desktop', sky, { disk: photos(200) });
  await A.sweep();

  const B = device('phone', sky, {});
  await B.sweep();                                   // joins and fetches all 200
  t.eq(Object.keys(B.disk).length, 200, 'the phone did not fetch the folder');

  /* MOST of the folder is wiped off the phone — not all of it, which is the case that reaches the
   * server at all. A device that can see NONE of what it knows about is caught here by
   * `emptyDevice`, FATAL and unconfirmable, and never proposes anything. A device that can still
   * see SOME files is treated as authoritative about the rest, and the SERVER's backstop is the
   * only thing left between it and everybody else's copies — which is exactly the shape that was
   * reported (400 refused, twice, on a phone that still held a few thousand of ~12,000 files). */
  const keep = Object.keys(B.disk).sort().slice(0, 20);
  for(const p of Object.keys(B.disk)) if(keep.indexOf(p) === -1) delete B.disk[p];
  const B2 = device('phone', sky, { disk: B.disk, index: B.st.index });
  const r2 = await B2.sweep();
  t.eq(r2.refusedMassDelete > 0, true, 'the refusal was not reported: ' + r2.refusedMassDelete);
  const alive = Object.values(sky.recs).filter(r => !r.t).length;
  t.eq(alive, 200, 'the folder lost records — only ' + alive + ' of 200 are still alive');

  // THE POINT: the next sweep must not propose the same deletions. With the journal struck, a
  // record that lives and a file that is absent is a DOWNLOAD.
  const B3 = device('phone', sky, { disk: B2.disk, index: B2.st.index });
  const r3 = await B3.sweep();
  t.eq(r3.refusedMassDelete || 0, 0, 'the phone proposed the mass delete a second time');
  t.eq(r3.downloaded.length, 180, 'the phone fetched ' + r3.downloaded.length
       + ' of 180 back — a struck journal must resolve as "I do not have this yet"');
  t.eq(Object.values(sky.recs).filter(r => r.t).length, 0, 'something was tombstoned after all');
  t.eq(identical(A.disk, B3.disk), null, 'the phone did not end up holding the folder again');
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

/* ---- THE ONE RULE THAT REPLACED EVERY FLOOR ----------------------------------------------------
 *
 * Deletion is automatic now: no floor, no ratio, no dialog, and the local copy is REMOVED rather
 * than moved into a per-device .pc-trash that nobody could see the whole of. What makes that
 * defensible is a single line in the executor — a device never removes its copy until the STORE IS
 * CONFIRMED TO HOLD THOSE BYTES — plus one account-wide trash on the server holding every deleted
 * file with the address it can be restored from.
 *
 * It is a better guard than the numbers it replaced because it is a MEASUREMENT rather than a guess
 * about intent. "Twenty files is a lot" cannot tell a deliberate bulk delete from a folder about to
 * be lost. "The store answered 200 for these exact bytes" is a fact about whether this can be
 * undone, and it is the only fact that matters.
 */
scenario('a build with no delete of its own keeps the files and says so once', async (t) => {
  /* The Android half needs a plugin method that ships with the APK, so a phone on an older build
   * has no `remove` at all. Without a check that is one failure logged per deleted path — 300 of
   * them, reading as the sync being broken rather than as one build being behind. */
  const sky = cloud();
  const A = device('laptop', sky, { disk: photos(8) });
  await A.sweep();
  const B = device('phone', sky, {});
  await B.sweep();
  for(const p of Object.keys(A.disk).slice(0, 5)) delete A.disk[p];
  await device('laptop', sky, { disk: A.disk, index: A.st.index }).sweep();

  const B2 = device('phone', sky, { disk: B.disk, index: B.st.index });
  delete B2.fs.remove;                       // an older build
  const r = await B2.sweep();
  t.eq(r.failed.length, 0, 'it logged ' + r.failed.length + ' failures for a build limitation');
  t.eq(r.cannotDelete, 5, 'it did not say how many deletions it could not carry out');
  t.eq(Object.keys(B2.disk).length, 8, 'files went missing on a build that cannot delete');
});

scenario('a deletion waits for the store to confirm it still has the bytes', async (t) => {
  const sky = cloud();
  const A = device('laptop', sky, { disk: photos(6) });
  await A.sweep();
  const B = device('phone', sky, {});
  await B.sweep();
  const victim = Object.keys(A.disk)[0];
  delete A.disk[victim];
  await device('laptop', sky, { disk: A.disk, index: A.st.index }).sweep();

  // The store cannot be asked — a rate limiter, a proxy, a dead socket. The file STAYS.
  const B2 = device('phone', sky, { disk: B.disk, index: B.st.index });
  B2.io.hasBlob = async () => null;
  const r = await B2.sweep();
  t.eq(r.trashed.length, 0, 'it deleted a file without being able to check the store');
  t.ok(!!B2.disk[victim], 'the file is gone from a device that could not verify it was safe');
  t.eq((r.keptUnconfirmed || []).length, 1, 'it kept the file and said nothing about why');
  t.ok(/could not be asked/.test(r.keptUnconfirmed[0].why),
       'it blamed the file rather than the moment: ' + JSON.stringify(r.keptUnconfirmed[0]));

  // The store answers NO — the bytes are not there, so deleting this copy would be losing it.
  const B3 = device('phone', sky, { disk: B.disk, index: B.st.index });
  B3.io.hasBlob = async () => false;
  const r3 = await B3.sweep();
  t.eq(r3.trashed.length, 0, 'it deleted the last copy of a file the store does not have');
  t.ok(/does not have these bytes/.test((r3.keptUnconfirmed || [{}])[0].why || ''),
       'the two reasons for keeping a file are not told apart');

  // And when the store says yes, the deletion simply happens — no dialog, no floor.
  const B4 = device('phone', sky, { disk: B.disk, index: B.st.index });
  const r4 = await B4.sweep();
  t.eq(r4.trashed.length, 1, 'a confirmed deletion did not apply');
  t.ok(!B4.disk[victim], 'the file is still here after a confirmed deletion');
});

scenario('a thousand deletions apply without asking, because every one of them was checked', async (t) => {
  /* The old floor stopped at twenty and asked. It could not tell this from a folder about to be
   * lost, so it asked about both — and a dialog that fires often enough is a dialog people confirm,
   * which is how "Mirror this Device" took 122 files off every machine. Now the question is asked
   * of the STORE, once per file, and it is not a matter of opinion. */
  const sky = cloud();
  const A = device('laptop', sky, { disk: photos(60) });
  await A.sweep();
  const B = device('phone', sky, {});
  await B.sweep();
  for(const p of Object.keys(A.disk).slice(0, 40)) delete A.disk[p];
  await device('laptop', sky, { disk: A.disk, index: A.st.index }).sweep();
  const B2 = device('phone', sky, { disk: B.disk, index: B.st.index });
  let asked = 0;
  const r = await B2.sweep({ confirm: () => { asked++; return true; } });
  t.eq(asked, 0, 'it asked ' + asked + ' times about deletions it had already verified were safe');
  t.eq(r.trashed.length, 40, 'it applied ' + r.trashed.length + ' of 40 verified deletions');
  t.eq(Object.keys(B2.disk).length, 20, 'the phone holds ' + Object.keys(B2.disk).length + ' of 20');
});

scenario('a tombstone with no address never deletes anything', async (t) => {
  /* The bytes were never stored — a file deleted before it ever finished uploading. There is
   * nothing to confirm and nothing to restore from, so the record is the only trace of the intent
   * and the local copy is the only copy of the file. Keep it, and say so. */
  const sky = cloud();
  const A = device('laptop', sky, { disk: photos(3) });
  await A.sweep();
  const B = device('phone', sky, {});
  await B.sweep();
  const victim = Object.keys(A.disk)[0];
  delete A.disk[victim];
  await device('laptop', sky, { disk: A.disk, index: A.st.index }).sweep();
  sky.strip(victim);                       // the tombstone loses its address
  const B2 = device('phone', sky, { disk: B.disk, index: B.st.index });
  const r = await B2.sweep();
  t.eq(r.trashed.length, 0, 'it deleted a file it could never have restored');
  t.eq((r.keptUnstored || []).length, 1, 'it kept the file silently');
  t.ok(!!B2.disk[victim], 'the only copy of the file is gone');
});

scenario('MIRROR PUBLISHES AND NEVER DELETES — the button that lost the receipts', async (t) => {
  /* WHAT HAPPENED, on a real account, to real business receipts and photographs.
   *
   * "Mirror this Device" tells you in its own dialog: "Nothing here is deleted and nothing is
   * overwritten." What it ran was an ORDINARY sweep with resends added — and an ordinary sweep
   * publishes a tombstone for every file the folder has a record of that this device does not have.
   * True locally, where nothing is trashed. False everywhere else, where every absence on the
   * mirroring device became a deletion on all the others.
   *
   * The device most likely to be mirroring is somebody restoring from a backup, which is exactly
   * the device most likely to be MISSING files. So the promise was broken in the one situation the
   * button exists for: rsync from a NAS, press Mirror, and 32 then 88 then 122 deletions go out.
   *
   * A publishing sweep has no deletion in it. This asserts that structurally, both directions. */
  const sky = cloud();
  const A = device('laptop', sky, { disk: photos(40) });
  await A.sweep();
  const B = device('desktop', sky, { disk: photos(40) });
  await B.sweep();

  /* The desktop's copy comes back from a backup INCOMPLETE — the whole situation somebody presses
   * Mirror in. TEN missing, deliberately UNDER the twenty-file floor: the mass-delete guard would
   * otherwise catch this and hide what Mirror itself does. Under the floor there is no guard, no
   * dialog and no record of it having happened — which is the quieter half of what went wrong. */
  const partial = {};
  const names = Object.keys(B.disk);
  for(const p of names.slice(0, 30)) partial[p] = B.disk[p];
  const M = device('desktop', sky, { disk: partial, index: B.st.index });

  const r = await M.sweep({ manual: true, resendAll: true, noDelete: true });
  t.eq(r.removedRemote.length, 0,
       'Mirror told the other devices to delete ' + r.removedRemote.length + ' files');
  t.eq(r.trashed.length, 0, 'Mirror trashed files on the device being mirrored FROM');
  t.ok(!!r.deletionsHeld, 'it deleted nothing but did not say that it had held anything back');
  t.eq(r.deletionsHeld.remote, 10, 'it held back ' + r.deletionsHeld.remote + ' of the 10 absences');

  // …and the other device still has all 40: nothing was taken from it.
  const C = device('laptop', sky, { disk: A.disk, index: A.st.index });
  const r2 = await C.sweep();
  t.eq(r2.trashed.length, 0, 'the other device trashed ' + r2.trashed.length + ' files after a mirror');
  t.eq(Object.keys(C.disk).length, 40, 'the other device is down to ' + Object.keys(C.disk).length + ' of 40');

  /* AND IT MUST NOT EVEN ASK, however many are missing. A person who has just read "nothing is
   * deleted" and is then asked "delete 122 files?" is being asked to arbitrate between two things
   * the app said in the same breath — and on the real account, after four days of dialogs, the
   * answer was yes. So the guard is not the safety here; not planning the deletion is. */
  const few = {};
  for(const p of names.slice(0, 5)) few[p] = B.disk[p];
  const M2 = device('desktop', sky, { disk: few, index: B.st.index });
  let asked = 0;
  const r3 = await M2.sweep({ manual: true, resendAll: true, noDelete: true,
                              confirm: () => { asked++; return true; } });
  t.eq(asked, 0, 'Mirror asked ' + asked + ' times whether to delete, having just promised it would not');
  t.eq(r3.removedRemote.length, 0, 'Mirror deleted ' + r3.removedRemote.length + ' files when confirmed');
});

scenario('and an ordinary sweep still settles a real deletion', async (t) => {
  // The fix must not turn deletion off in general — only in the sweep whose meaning is "publish".
  const sky = cloud();
  const A = device('laptop', sky, { disk: photos(6) });
  await A.sweep();
  const victim = Object.keys(A.disk)[0];
  delete A.disk[victim];
  const A2 = device('laptop', sky, { disk: A.disk, index: A.st.index });
  const r = await A2.sweep();
  t.eq(r.removedRemote.length, 1, 'an ordinary sweep stopped publishing deletions altogether');
});

scenario('a repair that has failed three separate copies is stopped, and names the sender', async (t) => {
  /* THE LOOP THIS ENDS, measured in production: a device whose hash of its OWN file was wrong
   * (`read(buf) > 0` treating a pipe's legal zero-length read as end of file) agreed with itself.
   * Asked to verify its copy it found no fault, re-sent the same bytes, and the fresh storage
   * address cleared every other device's memory of the last one. Download, fail, flag, re-send,
   * download: sixteen rounds in ninety minutes on one multi-gigabyte .jex, 1.14 GB re-fetched, and
   * nothing in the design could have ended it — the refusal is keyed on the ADDRESS precisely so
   * that a repair lifts it. So the ROUNDS are what stops this, and they must survive the address
   * changing every time, which is the one thing an address-keyed memory cannot notice. */
  const sky = cloud();
  const A = device('laptop', sky, { disk: photos(3) });
  await A.sweep();
  const victim = Object.keys(sky.folder())[0];
  const B = device('phone', sky, {});
  // Three distinct copies have already each failed the same checksum — the state the client
  // persists, built by the engine's own counter rather than by hand.
  let bad = {};
  for(const id of ['address-one', 'address-two', 'address-three'])
    bad = E.mergeBadFetch(bad, { [victim]: { id, why: 'checksum', v: 0 } });
  t.eq(bad[victim].rounds, 3, 'three failed copies were counted as ' + bad[victim].rounds);
  const r = await B.sweep({ skipFetch: bad });
  t.eq((r.abandoned || []).length, 1, 'it went back for a fourth copy: the loop is still unbounded');
  t.eq(r.downloaded.length, 2, 'it stopped fetching the other files too (' + r.downloaded.length + ' of 2)');
  const said = (r.unfetchable || []).find(u => u.path === victim);
  t.ok(!!said && /device sending it/.test(said.why),
       'it gave up without naming the sender as the fault: ' + JSON.stringify(said));
});

scenario('a person pressing Sync now is still allowed to try', async (t) => {
  // The count bounds a loop that runs BY ITSELF. Somebody who has just gone and fixed the other
  // device is answering the very question it exists to ask.
  const sky = cloud();
  const A = device('laptop', sky, { disk: photos(3) });
  await A.sweep();
  const victim = Object.keys(sky.folder())[0];
  const B = device('phone', sky, {});
  const spent = { [victim]: { id: 'some-old-address', why: 'checksum', rounds: 9 } };
  const r = await B.sweep({ skipFetch: spent, manual: true });
  t.eq((r.abandoned || []).length, 0, 'a manual sweep was refused by the automatic loop guard');
  t.eq(r.downloaded.length, 3, 'the manual sweep fetched ' + r.downloaded.length + ' of 3');
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

/* PHOTOGRAPHS ARE "BIG", AND BIG WAS STRICTLY ONE AT A TIME.
 *
 * The threshold is 2 MB, which is every photograph a camera has produced this decade — so the serial
 * path was not the exception it was written as, it was the whole folder. Measured against a real
 * store: one blob per second, sequential, because the wait is a round trip and nothing else was
 * allowed in flight during it. Sixty files a minute is the ceiling then, however fast the link is,
 * and 64 photos really is "about 3 h left".
 *
 * The rule was protecting the heap and that reason is real — see the Windows OOM scenario above,
 * which still passes. What was wrong was the instrument: a FILE COUNT cannot tell three 6 MB photos
 * from three 100 MB videos, and one-at-a-time is what you get when you set it for the videos.
 */
scenario('photographs overlap — a byte budget, not one file at a time', async (t) => {
  const sky = cloud();
  const disk = {};
  for(let i = 0; i < 10; i++) disk['DCIM/p' + i + '.jpg'] = video(5, i + 90);   // 5 MB each: "big"
  const A = device('laptop', sky, { disk, chunk: 16 * MB });
  const r = await A.sweep();
  t.eq(r.uploaded.length, 10, 'uploaded ' + r.uploaded.length + ' of 10');
  t.ok(A.st.peakInflight >= 2,
       'photographs still went one at a time (peak ' + A.st.peakInflight + ' in flight) — a folder '
       + 'of them is bounded by round trips, not by bandwidth');
  // ...and the budget is still honoured: 5 MB * 3 = 15 MB each, so at most two fit in 36 MB.
  t.ok(A.st.peakBytes <= 36 * MB + MB,
       'held ' + Math.round(A.st.peakBytes / MB) + ' MB at once — over the budget');
  const B = device('phone', sky, { chunk: 16 * MB });
  await B.sweep();
  t.eq(identical(A.disk, B.disk), null, 'the photos did not survive the overlap');
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

/* THE CHECKSUM WAS WRONG, NOT THE FILE — and refusing to re-send made it permanent.
 *
 * Android's digest looped `while (read(buf) > 0)`, and a DocumentsProvider serves a file over a
 * pipe where a zero-length read is ordinary and is not the end. It published the hash of a PREFIX.
 * Every other device then fetched the file perfectly, checked it against that number, and refused
 * it. Reported as four receipts that would not download, with the app insisting the stored copy was
 * damaged — while every chunk had verified against its own content address on the way in.
 *
 * Fixing the digest is not enough on its own, and that is what this scenario is really about: the
 * holder now hashes its file CORRECTLY, finds it disagrees with the number it published, and under
 * the old rule declares its own perfectly good file bad and re-seeds nothing. The fix would have
 * made the symptom permanent. The flag carries the hash the downloader measured, so the holder can
 * see that the store's bytes and its own bytes agree with each other and only the record's checksum
 * is the odd one out.
 */
scenario('a checksum published wrong is corrected by the holder, not treated as rot', async (t) => {
  const sky = cloud();
  const A = device('phone', sky, { disk: photos(4) });
  await A.sweep();
  const victim = Object.keys(sky.folder())[0];
  const real = require('crypto').createHash('sha256').update(A.disk[victim]).digest('hex');
  // What a truncated digest published: a hash of the first few bytes, recorded as the whole file's.
  const prefix = require('crypto').createHash('sha256')
                   .update(A.disk[victim].subarray(0, 8)).digest('hex');
  sky.entry(victim).csum = prefix;
  // A downloader fetched it, every chunk verified against its address, and the file hashed to
  // `real` — which is not what the record says. That is the flag, and it carries what it measured.
  sky.flagRec(victim, sky.entry(victim).sha, real);
  const A2 = device('phone', sky, { disk: A.disk, index: A.st.index });
  A2.st.index[victim].csum = prefix;                 // the journal remembers the wrong number too
  const r = await A2.sweep();
  t.ok((r.reseeding || []).indexOf(victim) !== -1,
       'the holder refused to re-send a file whose bytes it and the store agree about — those four '
       + 'receipts stay unfetchable on every device for ever');
  t.ok((r.staleChecksum || []).indexOf(victim) !== -1, 'it did not say WHY it re-sent');
  t.eq(sky.entry(victim).csum, real, 'the record still carries the wrong checksum');
  const B = device('laptop', sky);
  const rb = await B.sweep();
  t.eq(rb.failed.length, 0, 'the file still will not download: ' + JSON.stringify(rb.failed));
  t.eq(identical(A2.disk, B.disk), null, 'the corrected file did not reach the other device');
});

/* And the other reading of the same evidence, which must still be refused: the holder's bytes hash
 * to something DIFFERENT from what the downloader measured, so the two copies really are different
 * and this one is not evidence of anything. */
/* THE ONE THE APP HAD NO ANSWER FOR, and the reason "already re-sent" had to stop being an address
 * comparison.
 *
 * A device holds good bytes and is BEHIND the record: it never applied the newer version, because
 * the newer version cannot be applied — its checksum describes nothing, so every device that fetches
 * it fails verification and refuses to overwrite the good copy it already has. Correct behaviour,
 * and a permanent standoff. The heal path then skipped this device for the worst possible reason:
 * its journal names different bytes from the flagged record, which was read as "it must have re-sent
 * already" when it actually means "it never caught up".
 *
 * Measured on a real folder: four receipts, a good copy restored to the desktop from a NAS backup, a
 * record one version ahead carrying a checksum computed by a digest that stopped early. Mirror this
 * Device walks past a LIVE record. Nothing put them right by itself.
 */
/* AN RSYNC IS NOT A CHANGE, AND THE DOWNLOAD SIDE COULD NOT TELL.
 *
 * `same(L, R)` compares content only when BOTH sides carry a checksum, and a paged scan does not
 * hash — it cannot afford to on a folder of 12,000 files. So the local side has no checksum, the
 * comparison falls back to size and mtime, and anything that rewrites a timestamp without changing
 * a byte looks like a different file: a restore from backup, an rsync, a touch.
 *
 * Measured on a real folder: a desktop restored from a NAS backup and told to Mirror fetched 223
 * blobs in twelve minutes, every one a file it already held byte-for-byte, while its actual uploads
 * sat at eleven in half an hour. The bandwidth was real and the work was not.
 */
scenario('a republished file this device already holds is not downloaded again', async (t) => {
  const sky = cloud();
  const A = device('laptop', sky, { disk: photos(6) });
  await A.sweep();
  const B = device('desktop', sky);
  await B.sweep();                       // B now holds every file AND a journal covering them
  // Somebody republishes the same content — the churn a rewritten timestamp produces. Same bytes,
  // same address, a new version, and the uploader's OWN mtime, which is not B's.
  for(const p in sky.folder()){
    const was = sky.entry(p);
    sky.injectRec(p, Object.assign({}, was, { v: (was.v || 1) + 1, mtime: (was.mtime || 0) + 999000 }));
  }
  let fetched = 0;
  const B2 = device('desktop', sky, { disk: B.disk, index: B.st.index });
  const realGet = B2.io.getBlob;
  B2.io.getBlob = async (sha) => { fetched++; return realGet(sha); };
  const r = await B2.sweep();
  t.eq(fetched, 0, 'it downloaded ' + fetched + ' blobs it already held byte-for-byte');
  t.eq((r.heldAlready || []).length, 6, 'the files it already had were not settled by content');
  t.eq(r.failed.length, 0, JSON.stringify(r.failed));
  t.eq(identical(A.disk, B2.disk), null, 'the folder no longer matches');
  // ...and it STAYS settled: the journal must carry the record's version, or every sweep re-hashes
  // the whole folder for ever.
  const B3 = device('desktop', sky, { disk: B2.disk, index: B2.st.index });
  const r3 = await B3.sweep();
  t.eq(r3.downloaded.length, 0, 'the next sweep fetched them anyway');
  t.eq((r3.heldAlready || []).length, 0, 'the settle did not stick — every sweep re-hashes');
});

/* And the other direction, which must NOT be shortcut: bytes that really are different still come
 * down. The check can only ever remove work. */
scenario('a file that really did change elsewhere is still downloaded', async (t) => {
  const sky = cloud();
  const A = device('laptop', sky, { disk: photos(3) });
  await A.sweep();
  const B = device('desktop', sky);
  await B.sweep();
  const victim = Object.keys(A.disk)[0];
  A.disk[victim] = Buffer.from('genuinely different content now');
  const A2 = device('laptop', sky, { disk: A.disk, index: A.st.index });
  await A2.sweep();
  const B2 = device('desktop', sky, { disk: B.disk, index: B.st.index });
  const r = await B2.sweep();
  t.eq(r.downloaded.length, 1, 'a real edit was mistaken for a file already held');
  t.eq(identical(A2.disk, B2.disk), null, 'the edit did not arrive');
});

scenario('a device holding good bytes BEHIND a broken record re-sends them', async (t) => {
  const sky = cloud();
  const A = device('desktop', sky, { disk: photos(4) });
  await A.sweep();
  const victim = Object.keys(sky.folder())[0];
  const good = require('crypto').createHash('sha256').update(A.disk[victim]).digest('hex');
  // Somebody else published a NEWER version whose checksum describes nothing — the shape a
  // truncated digest produces. Its address is new, so it differs from what this device's journal
  // holds, and the journal stays where it was because the download can never succeed.
  const was = sky.entry(victim);
  const broken = Object.assign({}, was, { v: (was.v || 1) + 1,
                                          sha: 'a-newer-address-nobody-can-verify',
                                          csum: 'a-checksum-that-describes-nothing' });
  sky.injectRec(victim, broken);
  sky.putCipher('a-newer-address-nobody-can-verify', Buffer.from('whatever'));
  sky.flagRec(victim, broken.sha, good);       // a downloader tried it, failed, and said what it saw
  const A2 = device('desktop', sky, { disk: A.disk, index: A.st.index });
  const r = await A2.sweep();
  t.ok((r.reseeding || []).indexOf(victim) !== -1,
       'the one device still holding a usable copy was skipped for looking like it had already '
       + 'helped — the file stays unfetchable on every device for ever');
  t.eq(sky.entry(victim).csum, good, 'the record still describes bytes nobody can produce');
  const B = device('laptop', sky);
  const rb = await B.sweep();
  t.eq(rb.failed.length, 0, 'the file still will not download: ' + JSON.stringify(rb.failed));
  t.eq(identical(A2.disk, B.disk), null, 'the recovered file did not reach the other device');
});

scenario('a holder whose bytes disagree with the store as well is still refused', async (t) => {

  const sky = cloud();
  const A = device('desktop', sky, { disk: photos(4) });
  await A.sweep();
  const victim = Object.keys(sky.folder())[0];
  sky.flagRec(victim, sky.entry(victim).sha, 'what-the-downloader-saw');
  A.disk[victim] = Buffer.from('something else entirely');
  const A2 = device('desktop', sky, { disk: A.disk, index: A.st.index });
  const r = await A2.sweep();
  t.eq((r.reseeding || []).length, 0, 'it re-seeded a copy nothing agrees with');
  t.ok((r.badHere || []).indexOf(victim) !== -1, 'the divergence on this device was not named');
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

/* THE FILE THAT COULD NEVER LAND. A chunked record with NO whole-file checksum — which is what
 * Files → Synced folders publishes for a big upload, deliberately, because computing one means
 * holding the whole file — used to have its part file DISCARDED at the start of every attempt,
 * since without a csum nothing could prove the part belonged to this record. So the one class of
 * file that cannot afford to restart was the only one that always did: reported as a 2 GB .jex
 * reaching 100%, going back to the beginning, forever. The identity is a note beside the part file
 * now, not a hash of the content. */
scenario('a chunked file with NO checksum resumes instead of starting over', async (t) => {
  const sky = cloud();
  const A = device('laptop', sky, { disk: { 'big/no-csum.jex': video(12, 71) }, chunk: 4 * MB });
  await A.sweep();
  // Strip the whole-file checksum from the published record, exactly as the Files upload path does.
  for(const k of Object.keys(sky.recs || {})){
    const r = sky.recs[k];
    if(r && r.entry && r.entry.chunks && r.entry.chunks.length) delete r.entry.csum;
  }
  let writes = 0;
  const B = device('phone', sky, { chunk: 4 * MB });
  const realWrite = B.fs.writePart;
  B.fs.writePart = async (id, r, off, bytes) => { writes++; return realWrite(id, r, off, bytes); };
  const r1 = await B.sweep({ shouldStop: () => writes >= 2 });
  t.ok(r1.stopped === true, 'the interrupted download did not report itself stopped');
  const held = (B.st.parts['big/no-csum.jex'] || Buffer.alloc(0)).length;
  t.ok(held > 0, 'the part file was thrown away even though the transfer was only paused');

  // Second attempt: same device, same part file. It must NOT start from zero.
  const B2 = device('phone', sky, { chunk: 4 * MB, disk: B.disk, index: B.st.index });
  B2.st.parts['big/no-csum.jex'] = B.st.parts['big/no-csum.jex'];
  B2.st.files = B.st.files;                       // the sidecar lives on the same disk
  let from = null;
  const realWrite2 = B2.fs.writePart;
  B2.fs.writePart = async (id, r, off, bytes) => {
    if(from === null) from = off;
    return realWrite2(id, r, off, bytes);
  };
  const r2 = await B2.sweep();
  t.eq(r2.failed.length, 0, 'the resumed download failed: ' + JSON.stringify(r2.failed));
  t.ok(from > 0, 'it restarted at byte 0 — every retry throws the whole file away');
  t.eq(identical(A.disk, B2.disk), null, 'the resumed file is not byte-identical');
});

/* THE ONE THAT DECIDES WHETHER ANY OF THIS IS WORTH HAVING.
 *
 * "if you cant do an initial sync and sync to the clients, this will never be trusted." Every other
 * scenario here tests a recovery from something already wrong; this is the plain case, at a size
 * that is not a toy: one device holding a real folder — thousands of files across nested
 * directories, a chunked video among them — and TWO empty clients that must end up holding exactly
 * it. Byte for byte, no conflict copies, nothing trashed, nobody told to delete anything.
 *
 * The second client matters as much as the first: two devices joining the same fresh pair is where
 * a mint race or a double-publish shows up, and neither is visible with one. */
scenario('the plain case: one full folder, two empty clients, everybody converges', async (t) => {
  const sky = cloud();
  const disk = Object.assign({}, photos(1200), photos(900, 'Docs/2025/'),
                             photos(400, 'Docs/2024/scans/'));
  disk['Video/clip.mp4'] = video(9, 31);          // chunked, so the whole chunk path is exercised
  const N = Object.keys(disk).length;

  const A = device('desktop', sky, { disk, chunk: 4 * MB });
  const r1 = await A.sweep();
  t.eq(r1.failed.length, 0, 'the first sweep failed: ' + JSON.stringify(r1.failed.slice(0, 3)));
  t.eq(r1.uploaded.length, N, 'published ' + r1.uploaded.length + ' of ' + N + ' files');
  t.eq(r1.removedRemote.length, 0, 'a first sweep told the world to delete something');

  for(const name of ['laptop', 'phone']){
    const B = device(name, sky, { chunk: 4 * MB });
    const r = await B.sweep();
    t.eq(r.failed.length, 0, name + ' failed: ' + JSON.stringify(r.failed.slice(0, 3)));
    t.eq(r.downloaded.length, N, name + ' got ' + r.downloaded.length + ' of ' + N);
    t.eq(r.conflicted.length, 0, name + ' minted ' + r.conflicted.length + ' conflict copies');
    t.eq(B.st.trashed.length, 0, name + ' trashed ' + B.st.trashed.length + ' files on arrival');
    t.eq(r.removedRemote.length, 0, name + ' told the world to delete something');
    t.eq(identical(A.disk, B.disk), null, name + ' is not byte-identical to the source');

    // …and the sweep after it is silent. A client that re-downloads or republishes on its second
    // pass never settles, which is indistinguishable from broken however correct pass one was.
    const r2 = await B.sweep();
    t.eq(r2.downloaded.length, 0, name + ' re-downloaded ' + r2.downloaded.length + ' files');
    t.eq(r2.uploaded.length, 0, name + ' republished ' + r2.uploaded.length + ' files');
    t.eq(r2.conflicted.length, 0, name + ' minted conflicts on its second pass');
    t.eq(B.st.trashed.length, 0, name + ' trashed files on its second pass');
  }

  // And the source is undisturbed by either of them arriving.
  const r3 = await A.sweep();
  t.eq(r3.uploaded.length, 0, 'the source re-uploaded ' + r3.uploaded.length + ' files');
  t.eq(r3.downloaded.length, 0, 'the source downloaded its own files back');
  t.eq(A.st.trashed.length, 0, 'the source trashed ' + A.st.trashed.length + ' of its own files');
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

scenario('old-sealed records are re-published by whoever holds them in plaintext', async (t) => {
  const sky = cloud();
  const A = device('laptop', sky, { disk: photos(30) });
  await A.sweep();
  /* The transport reports which paths still wear the pre-a1 seal; the sweep must re-publish them
   * from its journal — no decrypting, no transfers, one version bump each. */
  const legacy = Object.keys(sky.folder()).slice(0, 10);
  const A2 = device('laptop', sky, { disk: A.disk, index: A.st.index });
  const realState = A2.io.state.bind(A2.io);
  A2.io.state = async () => Object.assign(await realState(), { oldSeal: legacy.slice() });
  const before = {};
  for(const p of legacy) before[p] = sky.entry(p).v;
  const r = await A2.sweep();
  t.eq(r.resealed, 10, 'the holder re-sealed ' + (r.resealed || 0) + ' of 10');
  t.eq(r.uploaded.length, 0, 're-sealing moved bytes for no reason');
  for(const p of legacy){
    t.eq(sky.entry(p).v, before[p] + 1, p + ' was not republished at a bumped version');
  }
  // …and the sweep after that is quiet: the journal moved with the records.
  const A3 = device('laptop', sky, { disk: A2.disk, index: A2.st.index });
  const r2 = await A3.sweep();
  t.eq(r2.uploaded.length + r2.downloaded.length + (r2.conflicted || []).length, 0,
       'the re-seal left the folder unsettled');
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
