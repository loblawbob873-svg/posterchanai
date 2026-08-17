/* A FILE THAT ARRIVES MUST BE THE FILE THAT WAS SENT — with real bytes, end to end.
 *
 * WHY THIS EXISTS, and it is an indictment of every other test in this directory. Videos synced from
 * a phone to a desktop and would not play, and 584 green tests had nothing to say about it, because
 * every one of them asserted the thing its author had just fixed: counts, phases, plans, guards. Not
 * one moved a byte and compared it. So removing the up-front hash — which was also the only thing
 * putting a checksum on a large file — went through the whole suite without a murmur, and the far
 * side stopped verifying anything it received.
 *
 * So this one carries CONTENT. Device A holds real buffers; the store is a real content-addressed
 * blob map; device B reassembles from chunks and the result is compared byte for byte. The chunk
 * sizes deliberately DIFFER between the two devices (4 MB on the phone, 16 MB on the desktop), which
 * is the arrangement that actually exists and the one that makes a wrong reassembly possible.
 *
 * It asserts two things, and the second is the one that was missing:
 *   1. what lands on B is byte-identical to what left A;
 *   2. B VERIFIED it — the entry carried a content identity and B checked the file against it before
 *      putting it in place. A correct file that nobody could have checked is a bug waiting to be
 *      silent, which is exactly what shipped today.
 *
 * Usage: node roundtrip_integrity_sim.js
 */
'use strict';
const crypto = require('crypto');
const path = require('path');

const RUN = require(path.join(__dirname, '..', '..', 'static', 'js', 'client', 'syncrun.js'));

const MB = 1024 * 1024;
const sha = (b) => crypto.createHash('sha256').update(b).digest('hex');

const fail = [];
const check = (c, what) => { if(!c) fail.push(what); };

/* A file whose bytes are reproducible and whose every region is distinguishable — so a chunk written
 * at the wrong offset, or dropped, changes the hash. */
function content(seed, size){
  const buf = Buffer.alloc(size);
  for(let i = 0; i < size; i += 4) buf.writeUInt32BE(((seed * 2654435761) ^ i) >>> 0, i);
  return buf;
}

// The shared, content-addressed blob store both devices talk to.
function cloud(){
  const blobs = new Map();
  let manifest = {};
  return {
    blobs, get manifest(){ return manifest; }, set manifest(m){ manifest = m; },
    put(bytes){ const h = sha(bytes); blobs.set(h, Buffer.from(bytes)); return h; },
    get(h){ return blobs.get(h); },
  };
}

function device(name, disk, sky, opts){
  const o = opts || {};
  const CH = o.chunk;
  const state = { base: {}, verified: [], committed: {} };
  const parts = {};                       // rel -> Buffer being assembled

  const fs = {
    chunkBytes: CH,
    scan: async () => {
      const files = {};
      for(const rel in disk) files[rel] = { size: disk[rel].length, mtime: 1000 };
      return { files, skipped: [] };
    },
    scanPage: async (id, so, off, lim) => {
      const keys = Object.keys(disk), end = Math.min(keys.length, off + lim), files = {};
      for(let i = off; i < end; i++) files[keys[i]] = { size: disk[keys[i]].length, mtime: 1000 };
      return { files, skipped: [], total: keys.length, done: end >= keys.length };
    },
    // The phone can hash a file where it lies; the desktop bridge cannot (it has no such method).
    hashFile: o.hashFile ? async (id, rel) => sha(disk[rel]) : undefined,
    read: async (id, rel) => new Uint8Array(disk[rel]),
    readPart: async (id, rel, off, len) => new Uint8Array(disk[rel].subarray(off, off + len)),
    writePart: async (id, rel, off, bytes) => {
      const b = Buffer.from(bytes);
      const cur = parts[rel] || Buffer.alloc(0);
      const need = off + b.length;
      const next = Buffer.alloc(Math.max(cur.length, need));
      cur.copy(next); b.copy(next, off);
      parts[rel] = next;
    },
    partSize: async (id, rel) => (parts[rel] ? parts[rel].length : 0),
    discardPart: async (id, rel) => { delete parts[rel]; },
    hashPart: async (id, rel) => { state.verified.push(rel); return sha(parts[rel] || Buffer.alloc(0)); },
    writeCommit: async (id, rel) => {
      disk[rel] = parts[rel] || Buffer.alloc(0);
      delete parts[rel];
      state.committed[rel] = disk[rel].length;
      return { size: disk[rel].length, mtime: 2000 };
    },
    write: async (id, rel, bytes) => {
      disk[rel] = Buffer.from(bytes);
      state.committed[rel] = disk[rel].length;
      return { size: disk[rel].length, mtime: 2000 };
    },
    move: async () => {}, trash: async (id, rel) => { delete disk[rel]; },
    sweepParts: async () => ({ removed: 0 }),
  };
  if(!o.hashFile) delete fs.hashFile;

  const store = {
    manifest: async () => JSON.parse(JSON.stringify(sky.manifest)),
    base: async () => JSON.parse(JSON.stringify(state.base)),
    saveBase: async (k, b) => { state.base = JSON.parse(JSON.stringify(b || {})); },
    save: async (k, m) => {
      const next = (m && m.manifest) || {};
      const cur = sky.manifest;
      for(const p of (m && m.touched) || Object.keys(next)){
        if(next[p]) cur[p] = next[p]; else delete cur[p];
      }
      sky.manifest = cur;
      if(m && m.base) state.base = JSON.parse(JSON.stringify(m.base));
    },
    putBlob: async (bytes) => ({ sha: sky.put(bytes) }),
    getBlob: async (h) => new Uint8Array(sky.get(h) || Buffer.alloc(0)),
    putParts: async (readPart, size, onProg, cs) => {
      const use = cs || CH, chunks = [];
      for(let off = 0; off < size; off += use){
        const len = Math.min(use, size - off);
        chunks.push(sky.put(Buffer.from(await readPart(off, len))));
        if(onProg) onProg(Math.min(off + len, size), size);
      }
      return { chunks, parts: chunks, cs: use };
    },
    getParts: async (chunks, write, size, have, cs) => {
      const use = cs || CH;
      for(let i = 0; i < chunks.length; i++){
        const b = sky.get(chunks[i]);
        if(!b) throw new Error('the store has forgotten chunk ' + i);
        await write(i * use, new Uint8Array(b));
      }
    },
    hashBytes: async (b) => sha(Buffer.from(b)),
    blobSha: async (b) => sha(Buffer.from(b)),
    chunkShas: async (readPart, size, cs) => {
      const use = cs || CH, out = [];
      for(let off = 0; off < size; off += use)
        out.push(sha(Buffer.from(await readPart(off, Math.min(use, size - off)))));
      return out;
    },
  };
  return { name, fs, store, state, disk };
}

const opts = (chunk) => ({
  id: 't', key: 'Pictures', device: 'dev', now: Date.now(), excludes: [],
  maxBytes: 8 * 1024 * MB, chunkBytes: chunk, chunkAbove: chunk,
});

(async () => {
  const sky = cloud();
  // A phone with a 40 MB video and a small photo; a desktop that has neither.
  const VIDEO = content(7, 40 * MB), PHOTO = content(9, 300 * 1024);
  const phoneDisk = { 'DCIM/video.mp4': VIDEO, 'DCIM/photo.jpg': PHOTO };
  const deskDisk = {};

  const phone = device('phone', phoneDisk, sky, { chunk: 4 * MB, hashFile: true });
  const desk = device('desktop', deskDisk, sky, { chunk: 16 * MB, hashFile: false });

  await RUN.sweep(phone.fs, phone.store, opts(4 * MB));
  await RUN.sweep(desk.fs, desk.store, opts(16 * MB));

  const got = deskDisk['DCIM/video.mp4'];
  check(!!got, 'the video never arrived on the desktop at all');
  check(!!got && got.length === VIDEO.length,
        'the video arrived TRUNCATED: ' + (got ? got.length : 0) + ' of ' + VIDEO.length + ' bytes');
  check(!!got && sha(got) === sha(VIDEO),
        'the video arrived CORRUPT — same length, different bytes (a chunk at the wrong offset '
        + 'reassembles to exactly this)');
  const p2 = deskDisk['DCIM/photo.jpg'];
  check(!!p2 && sha(p2) === sha(PHOTO), 'the photo did not survive the round trip');

  // …and the receiving device must have been ABLE to check it, which is the half that went missing.
  const entry = sky.manifest['DCIM/video.mp4'] || {};
  check(!!entry.csum,
        'the manifest entry for a chunked upload carries no content identity, so the far side '
        + 'verifies nothing it receives — a truncated file is written and played');
  check(desk.state.verified.indexOf('DCIM/video.mp4') >= 0,
        'the desktop never hashed what it received before putting it in place');

  console.log(JSON.stringify({
    videoBytes: got ? got.length : 0, expected: VIDEO.length,
    identical: !!got && sha(got) === sha(VIDEO),
    entryHasCsum: !!entry.csum, verifiedOnArrival: desk.state.verified.length,
    failures: fail,
  }, null, 1));
  process.exit(fail.length ? 1 : 0);
})().catch(e => { console.error('FAILED: ' + (e && e.stack || e)); process.exit(1); });
