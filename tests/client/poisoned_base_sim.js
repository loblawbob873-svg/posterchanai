/* A SWEEP THAT WOULD KEEP NOTHING IS NOT A SWEEP — IT IS A BROKEN AGREEMENT.
 *
 * Reported, repeatedly, on a folder whose files were all present and correct:
 *
 *   "Pictures — move 6331 files on this device to the trash? They are marked deleted on your other
 *    devices, and this sweep keeps only 0."
 *
 * Every rule fired correctly to produce that. The local agreement said those files had been synced;
 * the manifest said they were deleted elsewhere; therefore delete here. The agreement had simply
 * outlived the history it described, and no amount of clearing it by hand stuck.
 *
 * There is no legitimate reading of "act on this and the folder is empty". So the agreement is
 * discarded and the comparison made again without one — which can only produce uploads, because a
 * deletion REQUIRES an agreement. Same direction the engine already leans (delete loses to edit) and
 * the recoverable one: getting this wrong costs one more delete, the other way costs the folder.
 *
 * Usage: node poisoned_base_sim.js [files]
 */
'use strict';
const crypto = require('crypto');
const path = require('path');
const RUN = require(path.join(__dirname, '..', '..', 'static', 'js', 'client', 'syncrun.js'));

const N = parseInt(process.argv[2] || '300', 10);
const sha = (s) => crypto.createHash('sha256').update(String(s)).digest('hex');
const fail = [];
const check = (c, w) => { if(!c) fail.push(w); };

function world(opts){
  const o = opts || {};
  const disk = {}, manifest = {}, base = {};
  for(let i = 0; i < N; i++){
    const r = 'Pictures/p' + i + '.jpg';
    disk[r] = { size: 1000 + i, mtime: 5000 + i };
    manifest[r] = { deletedAt: 4000, device: 'phone' };          // erased on the other devices
    base[r] = { size: 1000 + i, mtime: 5000 + i };               // …but this device agreed them once
  }
  if(o.someKept){                        // a folder only PARTLY deleted elsewhere must not qualify
    for(let i = 0; i < 5; i++){
      const r = 'Pictures/keep' + i + '.jpg';
      disk[r] = { size: 7, mtime: 1 };
      manifest[r] = { size: 7, mtime: 1, csum: sha('k' + i) };
      base[r] = { size: 7, mtime: 1, csum: sha('k' + i) };
    }
  }
  const state = { manifest, base: o.noBase ? {} : base, trashed: [] };
  const fs = {
    chunkBytes: 4 * 1024 * 1024,
    scan: async () => ({ files: JSON.parse(JSON.stringify(disk)), skipped: [] }),
    read: async () => new Uint8Array(16), readPart: async (i, r, of, l) => new Uint8Array(l),
    hashPart: async () => sha('p'), partSize: async () => 0, discardPart: async () => {},
    writePart: async () => {}, writeCommit: async () => ({ size: 16, mtime: 9 }),
    write: async () => ({ size: 16, mtime: 9 }), move: async () => {},
    trash: async (id, rel) => { state.trashed.push(rel); }, sweepParts: async () => ({ removed: 0 }),
  };
  const store = {
    manifest: async () => JSON.parse(JSON.stringify(state.manifest)),
    base: async () => JSON.parse(JSON.stringify(state.base)),
    saveBase: async (k, b) => { state.base = JSON.parse(JSON.stringify(b || {})); },
    save: async (k, m) => {
      const n = (m && m.manifest) || {};
      for(const p of (m && m.touched) || Object.keys(n)){
        if(n[p]) state.manifest[p] = n[p]; else delete state.manifest[p];
      }
      if(m && m.base) state.base = JSON.parse(JSON.stringify(m.base));
    },
    putBlob: async () => ({ sha: sha('u') }),
    putParts: async () => ({ chunks: [sha('u')], parts: [sha('u')], cs: 4 * 1024 * 1024 }),
    getBlob: async () => new Uint8Array(16),
    getParts: async (c, w) => { await w(0, new Uint8Array(16)); },
    hashBytes: async () => sha('h'), blobSha: async () => sha('h'), chunkShas: async () => [],
  };
  return { fs, store, state };
}
const opts = (e) => Object.assign({
  id: 't', key: 'Pictures', device: 'desktop', now: Date.now(), excludes: [],
  maxBytes: 8 * 1024 * 1024 * 1024, chunkBytes: 4 * 1024 * 1024, chunkAbove: 4 * 1024 * 1024,
  manual: true,
}, e || {});

(async () => {
  // The reported state, with the user saying YES to republishing.
  const w = world();
  const r1 = await RUN.sweep(w.fs, w.store, opts({ confirmResurrect: async () => true,
                                                   confirmTrash: async () => false }));
  check((r1.discardedBase || 0) >= N, 'the agreement was not discarded: ' + (r1.discardedBase || 0));
  check(!r1.refusedTrash, 'it still proposed trashing the folder');
  check(w.state.trashed.length === 0, w.state.trashed.length + ' files were moved to the trash');
  check((r1.uploaded || []).length === N,
        'it uploaded ' + (r1.uploaded || []).length + ' of ' + N + ' — the files were not republished');

  // …and it stays fixed: the next sweep has nothing to do and proposes nothing.
  const r2 = await RUN.sweep(w.fs, w.store, opts({ confirmTrash: async () => false }));
  check(!r2.refusedTrash && (r2.uploaded || []).length === 0,
        'the second sweep was not quiet: trash=' + !!r2.refusedTrash
        + ' uploads=' + (r2.uploaded || []).length);

  // A folder PARTLY deleted elsewhere is ordinary work and must still be asked about, not swallowed.
  const p = world({ someKept: true });
  const r3 = await RUN.sweep(p.fs, p.store, opts({ confirmTrash: async () => false,
                                                   confirmResurrect: async () => false }));
  check(!(r3.discardedBase || 0),
        'an ordinary partial delete was treated as a poisoned agreement — the rule is too broad');
  check(!!r3.refusedTrash, 'a real mass delete stopped being asked about');

  console.log(JSON.stringify({
    files: N,
    poisoned: { discarded: r1.discardedBase || 0, trashed: w.state.trashed.length,
                uploaded: (r1.uploaded || []).length, askedToTrash: !!r1.refusedTrash },
    secondSweep: { uploads: (r2.uploaded || []).length, askedToTrash: !!r2.refusedTrash },
    ordinaryPartialDelete: { discarded: r3.discardedBase || 0, askedToTrash: !!r3.refusedTrash },
    failures: fail,
  }, null, 1));
  process.exit(fail.length ? 1 : 0);
})().catch(e => { console.error('FAILED: ' + (e && e.stack || e)); process.exit(1); });
