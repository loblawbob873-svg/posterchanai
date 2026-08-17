/* SETTLING A CONFLICT MUST NOT PULL THE FILE INTO THE RENDERER.
 *
 * Reported from the device, and it is the most precise thing anybody said all day:
 *
 *     "i see conflict 1/1927 then crash"
 *
 * A folder whose agreement is empty but whose manifest is full conflicts on EVERY path at once —
 * both sides look changed — and settling each one means asking "are these the same bytes". The only
 * way the engine had was `fs.read`: the whole file into the plugin, base64 across the bridge (four
 * characters per three bytes, held as UTF-16), then a hash pass in the renderer. Tens of megabytes
 * per photo. The bound on it was `_VERIFY_MAX`, two GIGABYTES, which is generous on a desktop and
 * meaningless on a phone — so the app died on conflict number one, of one thousand nine hundred and
 * twenty-seven.
 *
 * Skipping the verify is not the alternative: it is what settles the conflict, and skipping 1,927 of
 * them duplicates the entire folder on every device. So the file is hashed WHERE IT LIVES —
 * `fs.hashFile`, streamed natively, nothing crossing the bridge — and the whole-file read survives
 * only as the fallback for a platform that cannot, bounded by what that platform says it can hold.
 *
 * Usage: node conflict_verify_sim.js [conflicts]
 */
'use strict';
const crypto = require('crypto');
const path = require('path');

const RUN = require(path.join(__dirname, '..', '..', 'static', 'js', 'client', 'syncrun.js'));

const N = parseInt(process.argv[2] || '1927', 10);
const MB = 1024 * 1024;
const CHUNK = 4 * MB;
const sha = (s) => crypto.createHash('sha256').update(String(s)).digest('hex');

const fail = [];
const check = (c, what) => { if(!c) fail.push(what); };

/* Every file is a photo whose content is IDENTICAL on both sides — the manifest knows its checksum,
 * the local scan does not (an ordinary sweep does not hash), and on Android the mtime cannot match
 * because SAF gives a downloaded file whatever last-modified it likes. That is a conflict per path,
 * and every one of them is settleable without moving a byte. */
function world(n, opts){
  const o = opts || {};
  const disk = {}, manifest = {};
  for(let i = 0; i < n; i++){
    const rel = 'DCIM/IMG_' + String(i).padStart(5, '0') + '.jpg';
    const size = (i % 50 === 0) ? 20 * MB : 3 * MB;
    /* The manifest's SIZE differs from the file on disk, which is what makes this a conflict rather
     * than a match: identical sizes are (correctly) read as the same content when there is no base
     * to compare against. The CHECKSUM is what settles it, and getting that checksum is the step
     * that was killing the app. */
    disk[rel] = { size, mtime: 2000 + i };                       // no csum: an ordinary scan
    manifest[rel] = { size: size + 7, mtime: 1000 + i, sha: sha('blob' + rel),
                      csum: sha('content' + rel) };
  }
  const state = { reads: [], hashed: [], moved: [], written: [], manifest, base: {} };
  const fs = {
    chunkBytes: CHUNK,
    scan: async () => ({ files: JSON.parse(JSON.stringify(disk)), skipped: [] }),
    read: async (id, rel) => {
      state.reads.push(rel);                                     // THE CALL THAT KILLED THE APP
      return new Uint8Array(Math.min(disk[rel].size, 64));
    },
    readPart: async (id, rel, off, len) => new Uint8Array(len),
    hashPart: async () => sha('part'),
    partSize: async () => 0, discardPart: async () => {},
    writePart: async (id, rel) => { state.written.push(rel); },
    writeCommit: async () => ({ mtime: Date.now() }),
    write: async (id, rel) => { state.written.push(rel); return { mtime: Date.now() }; },
    move: async (id, from, to) => { state.moved.push(from + ' -> ' + to); },
    trash: async () => {},
    sweepParts: async () => ({ removed: 0 }),
  };
  // The whole point: the adapter can hash a file itself, streamed, without it crossing the bridge.
  if(!o.noHashFile) fs.hashFile = async (id, rel) => { state.hashed.push(rel); return sha('content' + rel); };

  const store = {
    manifest: async () => JSON.parse(JSON.stringify(state.manifest)),
    base: async () => JSON.parse(JSON.stringify(state.base)),
    saveBase: async (k, b) => { state.base = JSON.parse(JSON.stringify(b || {})); },
    save: async (k, m) => { if(m && m.base) state.base = JSON.parse(JSON.stringify(m.base)); },
    putBlob: async () => ({ sha: sha('put') }),
    putParts: async () => ({ sha: sha('put'), parts: [sha('put')], cs: CHUNK }),
    getBlob: async () => new Uint8Array(16),
    getParts: async (c, wp) => { await wp(0, new Uint8Array(16)); },
    // Whatever bytes it is handed, this is the identity the renderer would compute.
    hashBytes: async () => sha('read-in-the-renderer'),
    blobSha: async () => sha('read-in-the-renderer'),
    chunkShas: async () => [],
  };
  return { fs, store, state };
}

function opts(extra){
  return Object.assign({
    id: 'tree://pictures', key: 'Pictures', device: 'phone', now: Date.now(),
    excludes: [], maxBytes: 8 * 1024 * MB, chunkBytes: CHUNK, chunkAbove: CHUNK,
  }, extra || {});
}

(async () => {
  // ---- with a native hash: nothing is read, and every conflict settles ---------------------------
  const a = world(N);
  const rep = await RUN.sweep(a.fs, a.store, opts());

  check(a.state.reads.length === 0,
        'the conflict path pulled ' + a.state.reads.length + ' whole files into the renderer — this '
        + 'is the crash, at conflict 1 of ' + N);
  check(a.state.hashed.length >= N * 0.9,
        'the adapter was not asked to hash: ' + a.state.hashed.length + ' of ' + N);
  check((rep.conflicted || []).length === 0,
        (rep.conflicted || []).length + ' conflicts were not settled — that is a duplicate of the '
        + 'whole folder on every device');
  check(a.state.moved.length === 0, 'it made ' + a.state.moved.length + ' conflict copies');
  check(rep.unchanged >= N * 0.9, 'only ' + rep.unchanged + ' of ' + N + ' were recognised as identical');

  // ---- without one: the read is bounded by what the platform can hold ----------------------------
  const b = world(200, { noHashFile: true });
  await RUN.sweep(b.fs, b.store, opts());
  const bigOnes = Object.keys(b.state.manifest).filter((_, i) => i % 50 === 0).length;
  check(b.state.reads.length > 0, 'the fallback never read anything, so it cannot settle at all');
  // Nothing above one chunk may be read whole on a platform that reports a chunk size.
  const tooBig = b.state.reads.filter(r => (b.state.manifest[r] || {}).size > CHUNK);
  check(tooBig.length === 0,
        tooBig.length + ' files larger than one chunk were still read whole (' + bigOnes + ' exist)');

  // ---- the up-front hash, and why pause appeared to hang -----------------------------------------
  const c = world(400);
  let askedToHashEverything = false;
  const innerScan = c.fs.scan;
  c.fs.scan = async (id, so) => { if(so && so.hash) askedToHashEverything = true; return innerScan(id, so); };
  c.state.base = {};
  await RUN.sweep(c.fs, c.store, opts());
  check(!askedToHashEverything,
        'a first sweep still hashes the WHOLE folder up front even though it can hash on demand — '
        + 'that is tens of gigabytes before anything moves, and it is why pause sat on "stopping…"');

  // ...and a platform that CANNOT hash on demand must still do it, or a joining device duplicates
  // every file it already has.
  const d = world(50, { noHashFile: true });
  let hashedUpFront = false;
  const innerScanD = d.fs.scan;
  d.fs.scan = async (id, so) => { if(so && so.hash) hashedUpFront = true; return innerScanD(id, so); };
  await RUN.sweep(d.fs, d.store, opts());
  check(hashedUpFront,
        'a platform with no native hash stopped hashing up front, so it has no way to settle a join');

  console.log(JSON.stringify({
    conflicts: N,
    upFrontHash: { withNativeHash: askedToHashEverything, without: hashedUpFront },
    withNativeHash: { wholeFileReads: a.state.reads.length, nativeHashes: a.state.hashed.length,
                      unsettled: (rep.conflicted || []).length, copies: a.state.moved.length,
                      settled: rep.unchanged },
    fallback: { reads: b.state.reads.length, oversizedReads: tooBig.length },
    failures: fail,
  }, null, 1));
  process.exit(fail.length ? 1 : 0);
})().catch(e => { console.error('FAILED: ' + (e && e.stack || e)); process.exit(1); });
