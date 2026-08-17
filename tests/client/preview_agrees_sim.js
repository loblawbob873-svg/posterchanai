/* THE PREVIEW AND THE SWEEP MUST BE THE SAME ENGINE.
 *
 * Reported: "would upload shows new files, sync says in step on phone". Two surfaces, one folder,
 * opposite answers — and no way for anyone to tell that from a broken sweep.
 *
 * The cause was structural rather than arithmetic: a first sweep runs in bounded batches, and the
 * dry run was excluded from that, so the preview ran the whole-folder path while the sweep ran the
 * batched one. Worse, the batched report carried no `plan` at all — and `summarise` reads
 * `rep.plan` for the preview and `details` reads it for the card, so a batched sweep could only ever
 * say "in step", whatever it had actually found.
 *
 * This drives the SHIPPED engine and asserts the invariant directly: for one world, what the preview
 * says it WOULD do equals what the sweep DOES — in both regimes, batched and single-pass.
 *
 * Usage: node preview_agrees_sim.js [old] [new]
 */
'use strict';
const crypto = require('crypto');
const path = require('path');

const RUN = require(path.join(__dirname, '..', '..', 'static', 'js', 'client', 'syncrun.js'));

const OLD = parseInt(process.argv[2] || '2000', 10);
const NEW = parseInt(process.argv[3] || '5', 10);
const MB = 1024 * 1024, CHUNK = 4 * MB;
const sha = (s) => crypto.createHash('sha256').update(String(s)).digest('hex');

const fail = [];
const check = (c, what) => { if(!c) fail.push(what); };

/* A folder that has been syncing for a while, and then somebody comes home with photos. */
function world(nOld, nNew, opts){
  const o = opts || {};
  const disk = {}, manifest = {}, base = {};
  for(let i = 0; i < nOld; i++){
    const rel = 'DCIM/old' + i + '.jpg';
    const e = { size: 3 * MB, mtime: 1000 + i };
    disk[rel] = e;
    manifest[rel] = Object.assign({}, e, { sha: sha('b' + rel), csum: sha('c' + rel) });
    base[rel] = Object.assign({}, e, { sha: sha('b' + rel), csum: sha('c' + rel) });
  }
  for(let i = 0; i < nNew; i++) disk['DCIM/new' + i + '.jpg'] = { size: 5 * MB, mtime: 9000 + i };

  const state = { manifest, base: o.noBase ? {} : base };
  const fs = {
    chunkBytes: CHUNK,
    scan: async () => ({ files: JSON.parse(JSON.stringify(disk)), skipped: [] }),
    scanPage: async (id, so, off, lim) => {
      const k = Object.keys(disk), end = Math.min(k.length, off + lim), out = {};
      for(let i = off; i < end; i++) out[k[i]] = disk[k[i]];
      return { files: out, skipped: [], total: k.length, done: end >= k.length };
    },
    hashFile: async (id, rel) => sha('c' + rel),
    read: async () => new Uint8Array(16), readPart: async (i, r, o2, l) => new Uint8Array(l),
    hashPart: async () => sha('p'), partSize: async () => 0, discardPart: async () => {},
    writePart: async () => {}, writeCommit: async () => ({ mtime: 1 }),
    write: async () => ({ mtime: 1 }), move: async () => {}, trash: async () => {},
    sweepParts: async () => ({ removed: 0 }),
  };
  const store = {
    manifest: async () => JSON.parse(JSON.stringify(state.manifest)),
    base: async () => JSON.parse(JSON.stringify(state.base)),
    saveBase: async (k, b) => { state.base = JSON.parse(JSON.stringify(b || {})); },
    save: async (k, m) => { if(m && m.base) state.base = JSON.parse(JSON.stringify(m.base)); },
    putBlob: async () => ({ sha: sha('u') }),
    putParts: async () => ({ sha: sha('u'), parts: [sha('u')], cs: CHUNK }),
    getBlob: async () => new Uint8Array(16),
    getParts: async (c, w) => { await w(0, new Uint8Array(16)); },
    hashBytes: async () => sha('h'), blobSha: async () => sha('h'), chunkShas: async () => [],
  };
  return { fs, store, state };
}

const opts = (e) => Object.assign({
  id: 't', key: 'Pictures', device: 'phone', now: Date.now(), excludes: [],
  maxBytes: 8 * 1024 * MB, chunkBytes: CHUNK, chunkAbove: CHUNK,
}, e || {});

(async () => {
  const out = {};
  for(const [name, mk] of [['agreedBase', () => world(OLD, NEW)],
                           ['emptyBase', () => world(OLD, NEW, { noBase: true })]]){
    const a = mk(), dry = await RUN.sweep(a.fs, a.store, opts({ dryRun: true }));
    const b = mk(), real = await RUN.sweep(b.fs, b.store, opts({}));
    const p = dry.plan || {};
    const would = (p.upload || []).length;
    const did = (real.uploaded || []).length;
    out[name] = { would, did, batches: real.batches === undefined ? 1 : real.batches,
                  previewHasPlan: !!dry.plan, sweepHasPlan: !!real.plan };

    check(!!dry.plan, name + ': the preview has no plan at all, so the card can only say "in step"');
    check(!!real.plan, name + ': the sweep reports no plan, so its card cannot say what it found');
    check(would === did,
          name + ': the preview said it would upload ' + would + ' and the sweep uploaded ' + did
          + ' — two answers about one folder');
    check(did === NEW, name + ': ' + did + ' of ' + NEW + ' new files were uploaded');
    // A dry run must never write. If it did, the second sweep would find nothing left to do.
    check(Object.keys(a.state.base).length === (name === 'emptyBase' ? 0 : OLD),
          name + ': the PREVIEW changed the agreement — it is supposed to be read-only');
  }
  console.log(JSON.stringify(Object.assign({ old: OLD, new: NEW, failures: fail }, out), null, 1));
  process.exit(fail.length ? 1 : 0);
})().catch(e => { console.error('FAILED: ' + (e && e.stack || e)); process.exit(1); });
