/* A COPY IN THE STORE THAT FAILS ITS CHECKSUM MUST NOT BE FETCHED FOR EVER.
 *
 * Reported, after the checksum was restored and started doing its job:
 *
 *     downloading 2/2 20260508_152428 (1).mp4
 *     Failed — checksum mismatch after download — refusing to write it
 *     ...and again, and again, every sweep
 *
 * Refusing the file was right; retrying it was not. The chunks are content-addressed, so
 * reassembling them is deterministic — the same stored copy yields the same wrong hash every time,
 * for ever, moving real bytes over somebody's connection on every sweep. I shipped the guard without
 * ever testing what happens on the second attempt, which is this.
 *
 * The block is keyed on the IDENTITY of the copy rather than the path, so it lifts by itself when
 * the holder publishes a different one — no state to clear, no user action, and a genuinely repaired
 * file arrives on the next sweep.
 *
 * WHAT THIS FILE ALSO GUARDS AGAINST is the obvious repair that would be a catastrophe: dropping the
 * manifest entry so the holder re-uploads. The device that HAS the file holds it in its own `base`,
 * so an entry that vanishes reads as "deleted elsewhere" and it trashes its only good copy.
 *
 * Usage: node bad_copy_sim.js
 */
'use strict';
const crypto = require('crypto');
const path = require('path');

const RUN = require(path.join(__dirname, '..', '..', 'static', 'js', 'client', 'syncrun.js'));

const MB = 1024 * 1024, CHUNK = 4 * MB;
const sha = (b) => crypto.createHash('sha256').update(b).digest('hex');
const fail = [];
const check = (c, what) => { if(!c) fail.push(what); };

const GOOD = Buffer.alloc(8 * MB, 7);
const ROT = Buffer.alloc(8 * MB, 9);          // what the store actually holds: the wrong bytes

function world(){
  const disk = { 'Camera/ok.jpg': Buffer.alloc(1024, 1) };
  // The manifest advertises a video with GOOD's checksum, but the blobs reassemble to ROT.
  const manifest = {
    'Camera/bad.mp4': { size: GOOD.length, mtime: 10, csum: sha(GOOD),
                        chunks: [sha('c0'), sha('c1')], cs: CHUNK },
  };
  const state = { manifest, base: {}, fetched: 0, discarded: 0, committed: [] };
  const parts = {};
  const fs = {
    chunkBytes: CHUNK,
    scan: async () => {
      const files = {};
      for(const rel in disk) files[rel] = { size: disk[rel].length, mtime: 1 };
      return { files, skipped: [] };
    },
    scanPage: async (id, so, off, lim) => {
      const k = Object.keys(disk), end = Math.min(k.length, off + lim), files = {};
      for(let i = off; i < end; i++) files[k[i]] = { size: disk[k[i]].length, mtime: 1 };
      return { files, skipped: [], total: k.length, done: end >= k.length };
    },
    hashFile: async (id, rel) => sha(disk[rel] || Buffer.alloc(0)),
    read: async (id, rel) => new Uint8Array(disk[rel] || Buffer.alloc(0)),
    readPart: async (id, rel, off, len) => new Uint8Array((disk[rel] || Buffer.alloc(0)).subarray(off, off + len)),
    writePart: async (id, rel, off, bytes) => {
      const b = Buffer.from(bytes), cur = parts[rel] || Buffer.alloc(0);
      const next = Buffer.alloc(Math.max(cur.length, off + b.length));
      cur.copy(next); b.copy(next, off); parts[rel] = next;
    },
    partSize: async (id, rel) => (parts[rel] ? parts[rel].length : 0),
    discardPart: async (id, rel) => { state.discarded++; delete parts[rel]; },
    hashPart: async (id, rel) => sha(parts[rel] || Buffer.alloc(0)),
    writeCommit: async (id, rel) => {
      disk[rel] = parts[rel]; delete parts[rel]; state.committed.push(rel);
      return { size: disk[rel].length, mtime: 20 };
    },
    write: async (id, rel, bytes) => {
      disk[rel] = Buffer.from(bytes); state.committed.push(rel);
      return { size: disk[rel].length, mtime: 20 };
    },
    move: async () => {}, trash: async () => {}, sweepParts: async () => ({ removed: 0 }),
  };
  const store = {
    manifest: async () => JSON.parse(JSON.stringify(state.manifest)),
    base: async () => JSON.parse(JSON.stringify(state.base)),
    saveBase: async (k, b) => { state.base = JSON.parse(JSON.stringify(b || {})); },
    save: async (k, m) => {
      const next = (m && m.manifest) || {};
      for(const p of (m && m.touched) || Object.keys(next)){
        if(next[p]) state.manifest[p] = next[p]; else delete state.manifest[p];
      }
      if(m && m.base) state.base = JSON.parse(JSON.stringify(m.base));
    },
    putBlob: async (b) => ({ sha: sha(Buffer.from(b)) }),
    putParts: async () => ({ chunks: [sha('x')], parts: [sha('x')], cs: CHUNK }),
    getBlob: async () => new Uint8Array(ROT),
    getParts: async (chunks, write) => {           // hands back the WRONG bytes, as a bad copy does
      state.fetched++;
      await write(0, new Uint8Array(ROT));
    },
    hashBytes: async (b) => sha(Buffer.from(b)),
    blobSha: async (b) => sha(Buffer.from(b)),
    chunkShas: async () => [],
  };
  return { fs, store, state, disk };
}

const opts = (skipFetch) => ({
  id: 't', key: 'Pictures', device: 'phone', now: Date.now(), excludes: [],
  maxBytes: 8 * 1024 * MB, chunkBytes: CHUNK, chunkAbove: CHUNK, skipFetch: skipFetch || {},
});

(async () => {
  const w = world();
  // Sweep one: it tries, the checksum refuses it, and the part file is thrown away.
  const r1 = await RUN.sweep(w.fs, w.store, opts());
  const bad1 = (r1.failed || []).filter(f => /checksum mismatch/.test(f.error || ''));
  check(bad1.length === 1, 'the bad copy was not refused: ' + JSON.stringify(r1.failed || []));
  check(w.state.committed.indexOf('Camera/bad.mp4') < 0, 'a file failing its checksum was WRITTEN');
  check(!!(r1.badFetch && r1.badFetch['Camera/bad.mp4']),
        'the sweep did not report which copy failed, so the next one cannot know');

  // Sweep two, carrying what sweep one learned: it must not fetch that copy again.
  const before = w.state.fetched;
  const r2 = await RUN.sweep(w.fs, w.store, opts(r1.badFetch));
  check(w.state.fetched === before,
        'it fetched the same broken copy again — that is the loop, ' + (w.state.fetched - before)
        + ' more transfer(s)');
  const fetchedBySweepTwo = w.state.fetched;
  check((r2.unfetchable || []).length === 1,
        'it went quiet about the file instead of saying it needs re-sending');

  // …and the entry must still be there. Dropping it would read as "deleted elsewhere" on the device
  // that holds the only good copy, and it would trash it.
  check(!!w.state.manifest['Camera/bad.mp4'],
        'the manifest entry was dropped — the device holding the file would now DELETE it');

  // A repaired copy (a different identity) downloads with no action from anyone.
  w.state.manifest['Camera/bad.mp4'] = { size: ROT.length, mtime: 30, csum: sha(ROT),
                                         chunks: [sha('c0')], cs: CHUNK };
  const r3 = await RUN.sweep(w.fs, w.store, opts(r1.badFetch));
  check((r3.downloaded || []).indexOf('Camera/bad.mp4') >= 0,
        'a re-uploaded copy was still blocked — the block did not lift with the identity');

  console.log(JSON.stringify({
    refusedFirstTime: bad1.length, fetchesBeforeSweepTwo: before,
    fetchesAfterSweepTwo: fetchedBySweepTwo, fetchesAfterRepair: w.state.fetched,
    saidSo: (r2.unfetchable || []).length, entryKept: !!w.state.manifest['Camera/bad.mp4'],
    repairedDownloaded: (r3.downloaded || []).indexOf('Camera/bad.mp4') >= 0,
    failures: fail,
  }, null, 1));
  process.exit(fail.length ? 1 : 0);
})().catch(e => { console.error('FAILED: ' + (e && e.stack || e)); process.exit(1); });
