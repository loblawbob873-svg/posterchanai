/* A FIRST SWEEP OF A REAL PICTURES FOLDER, RUN AT THE SIZE THAT KILLED THE APP.
 *
 * The engine assembles the whole folder before it moves a byte — metadata, plan, manifest snapshot
 * and agreement, all live at once. At 15,790 files that kills a WebView's render process the moment
 * a sweep starts: the app disappears and stays in the recents list, because the process never died,
 * only its renderer, so nothing is thrown and nothing reaches any log. Confirmed on the device by
 * pausing the folders — stable paused, dead within seconds of the sweep starting.
 *
 * syncrun now runs a FIRST sweep in bounded batches (see firstSweepInBatches). This drives the
 * SHIPPED foldersync.js and syncrun.js against a stub phone at that size, and asserts the properties
 * that make batching safe rather than merely smaller:
 *
 *   1. every file is uploaded, exactly once
 *   2. nothing is deleted — locally or remotely — by a partial view of the folder
 *   3. no batch ever sees more than its page of files
 *   4. a file that exists only on another device is downloaded, and NOT before the whole folder has
 *      been seen (downloading it early would overwrite a local file sitting in a later page)
 *   5. an interrupted first sweep RESUMES: the second run finishes the rest and does not re-upload
 *      what the first already agreed
 *
 * Usage: node first_sweep_sim.js [files] [pageSize]
 */
'use strict';
const crypto = require('crypto');
const path = require('path');

const RUN = require(path.join(__dirname, '..', '..', 'static', 'js', 'client', 'syncrun.js'));

const N = parseInt(process.argv[2] || '15790', 10);
const PAGE = parseInt(process.argv[3] || '750', 10);
const MB = 1024 * 1024;
const CHUNK = 4 * MB;

const sha = (s) => crypto.createHash('sha256').update(String(s)).digest('hex');
const fail = [];
const check = (cond, what) => { if(!cond) fail.push(what); };

// ---- a phone's camera roll -----------------------------------------------------------------------
function makeFolder(n){
  const files = {};
  for(let i = 0; i < n; i++){
    const rel = 'DCIM/2026/IMG_' + String(i).padStart(5, '0') + '.jpg';
    const size = (i % 50 === 0) ? 20 * MB : (i % 5 === 0) ? 6 * MB : 2 * MB;
    files[rel] = { size, mtime: 1700000000000 + i * 1000, sha: sha(rel) };
  }
  return files;
}

// A file only another device has: it must be fetched, but only once the whole folder has been seen.
const REMOTE_ONLY = 'DCIM/from-the-laptop.jpg';

function makeStubs(disk, opts){
  const o = opts || {};
  const state = {
    manifest: Object.assign({}, o.manifest || {}),
    base: Object.assign({}, o.base || {}),
    pagesSeen: [], downloadsAt: [], written: {}, batches: 0,
  };
  const fs = {
    chunkBytes: CHUNK,
    scanPage: async (id, so, offset, limit) => {
      const keys = Object.keys(disk);
      const end = Math.min(keys.length, offset + limit);
      const out = {};
      for(let i = offset; i < end; i++) out[keys[i]] = disk[keys[i]];
      state.pagesSeen.push(end - offset);
      return { files: out, skipped: [], total: keys.length, done: end >= keys.length };
    },
    scan: async () => ({ files: disk, skipped: [] }),
    read: async (id, rel) => new Uint8Array(Math.min(disk[rel] ? disk[rel].size : 0, 1024)),
    readPart: async (id, rel, off, len) => new Uint8Array(len),
    hashPart: async () => sha('part'),
    partSize: async () => 0,
    discardPart: async () => {},
    writePart: async (id, rel) => { state.written[rel] = true; },
    writeCommit: async () => ({ mtime: Date.now() }),
    write: async (id, rel) => { state.written[rel] = true;
                                state.downloadsAt.push(state.batches);
                                return { mtime: Date.now() }; },
    trash: async (id, rel) => { state.trashed = (state.trashed || []).concat(rel); },
    move: async () => {},
    sweepParts: async () => ({ removed: 0 }),
    wakeBegin: () => {}, wakeEnd: () => {},
  };
  const store = {
    manifest: async () => JSON.parse(JSON.stringify(state.manifest)),
    base: async () => JSON.parse(JSON.stringify(state.base)),
    saveBase: async (k, b) => { state.base = JSON.parse(JSON.stringify(b || {})); },
    save: async (k, m) => {
      // The real one re-reads and merges the touched paths; this keeps that shape.
      const next = (m && m.manifest) || {};
      for(const p of (m && m.touched) || Object.keys(next)){
        if(next[p]) state.manifest[p] = next[p]; else delete state.manifest[p];
      }
      if(m && m.base) state.base = JSON.parse(JSON.stringify(m.base));
    },
    putBlob: async (bytes) => ({ sha: sha('blob' + (bytes && bytes.length)) }),
    putParts: async (readPart, size, onProg, cs) => {
      const parts = [];
      for(let off = 0; off < size; off += (cs || CHUNK)) parts.push(sha('c' + off));
      return { sha: parts[0], parts, cs: cs || CHUNK };
    },
    getBlob: async () => new Uint8Array(16),
    getParts: async (chunks, writePart) => { await writePart(0, new Uint8Array(16)); },
    hashBytes: async (b) => sha('bytes' + (b && b.length)),
    blobSha: async (b) => sha('blob' + (b && b.length)),
    chunkShas: async () => [],
  };
  return { fs, store, state };
}

function options(extra){
  return Object.assign({
    id: 'tree://pictures', key: 'Pictures', device: 'phone', now: Date.now(),
    excludes: [], maxBytes: 8 * 1024 * MB,
    chunkBytes: CHUNK, chunkAbove: CHUNK,
    batchFiles: PAGE,
  }, extra || {});
}

(async () => {
  // ---- 1-4: a whole first sweep ----------------------------------------------------------------
  const disk = makeFolder(N);
  const { fs, store, state } = makeStubs(disk, {
    /* The checksum has to be the one the executor will actually compute for the bytes this stub
     * hands back, or the download is correctly REJECTED as corrupt and the test measures its own
     * fake rather than the engine. */
    manifest: { [REMOTE_ONLY]: { size: 16, mtime: 1, sha: sha('remote-only'), csum: sha('bytes16') } },
  });
  const t0 = Date.now();
  const rep = await RUN.sweep(fs, store, options({
    onProgress: (p) => { if(p && p.batch) state.batches = p.batch; },
  }));
  const secs = Math.round((Date.now() - t0) / 1000);

  // A folder smaller than one page is a single batch, correctly — the properties below still hold.
  if(N > PAGE) check(rep.batches > 1, 'it did not batch at all (batches=' + rep.batches + ')');
  check(rep.uploaded.length === N,
        'uploaded ' + rep.uploaded.length + ' of ' + N);
  check(new Set(rep.uploaded).size === rep.uploaded.length, 'a file was uploaded twice');
  check(!(rep.trashed || []).length && !(rep.removedRemote || []).length,
        'a partial view of the folder DELETED something: trashed=' + (rep.trashed || []).length
        + ' removedRemote=' + (rep.removedRemote || []).length);
  check(Math.max.apply(null, state.pagesSeen) <= PAGE,
        'a batch saw more than one page: ' + Math.max.apply(null, state.pagesSeen));
  check(Object.keys(state.base).length >= N, 'the agreement did not cover the folder: '
        + Object.keys(state.base).length);
  // The remote-only file is fetched, and only on the last batch — before that, a path missing from
  // the page is simply a path this batch has not seen.
  check(!!state.written[REMOTE_ONLY], 'the file that only exists on another device was never fetched');
  check(state.downloadsAt.every(b => b === 0 || b >= rep.batches - 1),
        'a remote-only file was downloaded before the whole folder had been seen');

  // ---- 5: interrupt and resume ------------------------------------------------------------------
  const disk2 = makeFolder(N);
  let seen = 0;
  const a = makeStubs(disk2, {});
  const rep1 = await RUN.sweep(a.fs, a.store, options({
    shouldStop: () => { return seen++ > PAGE * 2; },
  }));
  const agreedAfterStop = Object.keys(a.state.base).length;
  if(N > PAGE * 3)
    check(agreedAfterStop > 0 && agreedAfterStop < N,
          'the interrupted sweep agreed ' + agreedAfterStop + ' of ' + N + ' — expected a partial');

  const rep2 = await RUN.sweep(a.fs, a.store, options({}));
  const totalUploaded = rep1.uploaded.length + rep2.uploaded.length;
  check(Object.keys(a.state.base).length >= N,
        'the resumed sweep did not finish the folder: ' + Object.keys(a.state.base).length);
  check(totalUploaded <= N * 1.05,
        're-uploaded work the first run had already agreed: ' + totalUploaded + ' for ' + N + ' files');

  // ---- 6: the final pass may conclude "deleted", and only when it is true ----------------------
  /* Batching every sweep means no single batch can see the whole folder, so the pass that decides a
   * file is gone runs last and alone. Two ways that goes wrong, both data loss on every device:
   * concluding a deletion from a partial view, and concluding one from a file whose upload failed. */
  {
    const disk3 = makeFolder(600);
    const gone = ['DCIM/2026/IMG_00003.jpg', 'DCIM/2026/IMG_00004.jpg'];
    const c = makeStubs(disk3, {});
    // Agree the whole folder first, the way a completed sweep would.
    await RUN.sweep(c.fs, c.store, options({}));
    const agreedBefore = Object.keys(c.state.base).length;
    // …then two files are deleted on this disk, and one that remains fails to upload its change.
    for(const g of gone) delete disk3[g];
    const edited = 'DCIM/2026/IMG_00009.jpg';
    disk3[edited] = { size: disk3[edited].size + 1, mtime: Date.now() };
    c.store.putParts = async () => { throw new Error('upload refused'); };
    c.store.putBlob = async () => { throw new Error('upload refused'); };

    const rep3 = await RUN.sweep(c.fs, c.store, options({}));
    const removed = (rep3.removedRemote || []).map(x => (x && x.path) || x);
    check(agreedBefore >= 600, 'the setup sweep did not agree the folder: ' + agreedBefore);
    check(removed.length === gone.length,
          'the final pass removed ' + removed.length + ' paths, expected ' + gone.length
          + ' — a partial view concluded a deletion');
    for(const g of gone)
      check(removed.indexOf(g) >= 0, 'a genuinely deleted file was not propagated: ' + g);
    check(removed.indexOf(edited) < 0,
          'a file whose UPLOAD FAILED was reported as deleted — that removes it from every other '
          + 'device because this one could not send it');
    var deletionArm = { agreedBefore, removed: removed.length, editedRemoved: removed.indexOf(edited) >= 0 };
  }

  console.log(JSON.stringify({
    files: N, page: PAGE, seconds: secs,
    deletions: deletionArm,
    batches: rep.batches, uploaded: rep.uploaded.length,
    downloaded: rep.downloaded.length, conflicted: rep.conflicted.length,
    biggestPage: Math.max.apply(null, state.pagesSeen),
    agreed: Object.keys(state.base).length,
    remoteOnlyFetched: !!state.written[REMOTE_ONLY],
    resume: { firstRun: rep1.uploaded.length, secondRun: rep2.uploaded.length, total: totalUploaded },
    failures: fail,
  }, null, 1));
  process.exit(fail.length ? 1 : 0);
})().catch(e => { console.error('FAILED: ' + (e && e.stack || e)); process.exit(1); });
