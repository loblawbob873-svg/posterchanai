/* A DEVICE THAT HAS NOTHING MUST NOT TELL EVERY OTHER DEVICE THAT EVERYTHING IS GONE.
 *
 * Found live, with a desktop mid-upload of 6,334 files and a phone still sweeping the same pair after
 * its folder had been removed: a device with an AGREEMENT and an EMPTY scan plans `deleteRemote` for
 * every path — tombstones, the record by which other devices learn a file was deleted — and
 * `massDelete()` returns null for it, because that guard only ever covered the LOCAL side (trashing
 * files on this disk).
 *
 * So the phone would have marked the entire folder deleted for everyone, silently, while the desktop
 * was still uploading it. The desktop's next sweep would then have read "deleted elsewhere" for
 * files it holds and offered to trash them: the same catastrophe, one sweep later.
 *
 * An empty scan is never evidence that a folder was emptied. It is a folder that was removed, a SAF
 * grant that lapsed, a disk that was not mounted, a path that moved.
 *
 * Usage: node empty_device_sim.js [files]
 */
'use strict';
const crypto = require('crypto');
const path = require('path');
const RUN = require(path.join(__dirname, '..', '..', 'static', 'js', 'client', 'syncrun.js'));

const N = parseInt(process.argv[2] || '500', 10);
const sha = (s) => crypto.createHash('sha256').update(String(s)).digest('hex');
const fail = [];
const check = (c, w) => { if(!c) fail.push(w); };

function world(opts){
  const o = opts || {};
  const disk = Object.assign({}, o.disk || {});
  const manifest = {}, base = {};
  for(let i = 0; i < N; i++){
    const r = 'Pictures/p' + i + '.jpg';
    manifest[r] = { size: 10, mtime: 1, csum: sha(r) };
    base[r] = { size: 10, mtime: 1, csum: sha(r) };      // this device agreed them once
  }
  const state = { manifest, base, trashed: [] };
  const fs = {
    chunkBytes: 4 * 1024 * 1024,
    scan: async () => {
      const files = {};
      for(const r in disk) files[r] = { size: 10, mtime: 1 };
      return { files, skipped: [] };
    },
    read: async () => new Uint8Array(10), readPart: async (i, r, of, l) => new Uint8Array(l),
    hashPart: async () => sha('p'), partSize: async () => 0, discardPart: async () => {},
    writePart: async () => {}, writeCommit: async () => ({ size: 10, mtime: 2 }),
    write: async (id, r) => { disk[r] = 1; return { size: 10, mtime: 2 }; },
    move: async () => {}, trash: async (id, r) => { state.trashed.push(r); },
    sweepParts: async () => ({ removed: 0 }),
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
    putBlob: async () => ({ sha: sha('u') }),
    putParts: async () => ({ chunks: [sha('u')], parts: [sha('u')], cs: 4 * 1024 * 1024 }),
    getBlob: async () => new Uint8Array(10),
    getParts: async (c, w) => { await w(0, new Uint8Array(10)); },
    hashBytes: async () => sha('h'), blobSha: async () => sha('h'), chunkShas: async () => [],
  };
  return { fs, store, state };
}

const opts = () => ({
  id: 't', key: 'Pictures', device: 'phone', now: Date.now(), excludes: [],
  maxBytes: 8 * 1024 * 1024 * 1024, chunkBytes: 4 * 1024 * 1024, chunkAbove: 4 * 1024 * 1024,
  // No handlers: a background sweep has nobody in front of it, and that is the dangerous case.
});

(async () => {
  // 1. THE REPORTED SITUATION: an agreement, and a scan that finds nothing.
  const w = world();
  const rep = await RUN.sweep(w.fs, w.store, opts());
  const live = Object.keys(w.state.manifest).filter(p => !w.state.manifest[p].deletedAt).length;

  check((rep.removedRemote || []).length === 0,
        'it tombstoned ' + (rep.removedRemote || []).length + ' paths for every device');
  check(live === N, 'the manifest lost ' + (N - live) + ' live entries — the folder was erased for '
        + 'everyone by a device that had nothing');
  check(!!rep.refusedRemoteDelete, 'it went quiet about refusing, so nobody would ever know');

  // 2. …and an ORDINARY delete still propagates: most files present, a few genuinely removed.
  const disk2 = {};
  for(let i = 3; i < N; i++) disk2['Pictures/p' + i + '.jpg'] = 1;      // 3 deleted, the rest kept
  const w2 = world({ disk: disk2 });
  const rep2 = await RUN.sweep(w2.fs, w2.store, opts());
  check((rep2.removedRemote || []).length === 3,
        'an ordinary delete of 3 files propagated ' + (rep2.removedRemote || []).length
        + ' — the guard is too broad and deletions no longer travel');
  check(!rep2.refusedRemoteDelete, 'an everyday deletion was refused');

  console.log(JSON.stringify({
    emptyDevice: { tombstoned: (rep.removedRemote || []).length, liveLeft: live,
                   refused: rep.refusedRemoteDelete || null },
    ordinaryDelete: { tombstoned: (rep2.removedRemote || []).length,
                      refused: rep2.refusedRemoteDelete || null },
    failures: fail,
  }, null, 1));
  process.exit(fail.length ? 1 : 0);
})().catch(e => { console.error('FAILED: ' + (e && e.stack || e)); process.exit(1); });
