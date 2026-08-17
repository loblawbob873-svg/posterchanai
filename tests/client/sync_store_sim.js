/* The folder-sync STORE — the layer that could not save a real folder.
 *
 * `PCSync.store` is where the manifest is sealed and where this device's agreement is written, and
 * both had a ceiling nothing checked:
 *
 *   NIP-44 refuses a plaintext over 65535 bytes. A manifest entry measures ~174, so the document
 *   held about 376 files — and a folder with more than that could not be saved AT ALL. Every sweep
 *   of a real 15790-file folder uploaded everything, threw at the very last step, wrote no
 *   agreement, and started again from the first file on the next sweep. Everything except the last
 *   step worked, which is why it looked like a sync.
 *
 *   `base` went to localStorage under a try/catch that swallowed everything, including the quota
 *   error a 2.6 MB agreement earns. Same infinite resync, different cause, equally silent.
 *
 * sync.js is a browser IIFE with no load-time side effects, so it is loaded here for real against
 * stub globals — the store under test IS the shipped one. The NIP-44 stub enforces the SAME
 * 65535-byte ceiling nostr-tools does, and localStorage the same 5 MB quota a browser does, because
 * a stub that cheerfully accepts 2.6 MB would test nothing at all.
 *
 * Usage: node sync_store_sim.js   → one JSON line per scenario, non-zero exit if any failed.
 */
'use strict';
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const CLIENT = path.join(__dirname, '..', '..', 'static', 'js', 'client');
const NIP44_MAX = 65535;          // nostr-tools: "invalid plaintext size: must be between 1 and 65535 bytes"
const NIP44_MIN_PAYLOAD = 132;    // ...and it rejects a payload shorter than this before reading it
const LS_QUOTA = 5 * 1024 * 1024; // what a browser gives an origin, shared with everything else

function makeWorld(){
  const w = {
    docs: new Map(),     // folder key -> the manifest document, as the server would hold it
    blobs: new Map(),    // sha -> bytes
    ls: new Map(),       // localStorage
    idb: new Map(),      // object store name -> Map
    posts: [],           // every /client/sync-manifest body
    saves: 0,
    idbBroken: false,    // flip to model a device that cannot write to IndexedDB at all
  };
  let seq = 0;

  w.localStorage = {
    getItem: k => (w.ls.has(k) ? w.ls.get(k) : null),
    setItem: (k, v) => {
      let total = String(v).length;
      for(const [kk, vv] of w.ls) if(kk !== k) total += String(vv).length;
      if(total > LS_QUOTA){ const e = new Error('QuotaExceededError'); e.name = 'QuotaExceededError'; throw e; }
      w.ls.set(k, String(v));
    },
    removeItem: k => { w.ls.delete(k); },
  };

  // Enough IndexedDB for one object store: open → transaction → get/put/delete, all async.
  w.indexedDB = {
    open(){
      const rq = {};
      setTimeout(() => {
        if(w.idbBroken){ rq.error = new Error('disk full'); return rq.onerror && rq.onerror(); }
        rq.result = {
          objectStoreNames: { contains: () => true },
          createObjectStore(){},
          transaction(name){
            if(!w.idb.has(name)) w.idb.set(name, new Map());
            const map = w.idb.get(name);
            const tx = {};
            const done = () => setTimeout(() => tx.oncomplete && tx.oncomplete(), 0);
            tx.objectStore = () => ({
              get(k){ const r = { result: map.get(k) }; done(); return r; },
              put(v, k){ map.set(k, v); done(); return { result: true }; },
              delete(k){ map.delete(k); done(); return { result: true }; },
            });
            return tx;
          },
        };
        rq.onsuccess && rq.onsuccess();
      }, 0);
      return rq;
    },
  };

  /* The endpoint, including its collapse guard — the server refuses a manifest that drops to under
   * half of what it holds, and that refusal is the only thing standing between a bug and a folder
   * emptied on every device. Modelled here rather than stubbed away, because the client's handling
   * of the 409 is what these scenarios are about. */
  w.collapseGuard = true;
  w.fetch = async (_url, opts) => {
    const body = JSON.parse((opts && opts.body) || '{}');
    w.posts.push(body);
    /* Every device's document for a folder. Keyed the way the server keys them —
     * `pcai:sync:<pair>:<device>` — so the store's own splitting is exercised rather than assumed. */
    if(body.views){
      const views = {};
      for(const [k, doc] of w.docs){
        const at = k.indexOf(':');
        if(at < 0 || k.slice(0, at) !== body.folder) continue;
        views[k.slice(at + 1)] = doc;
      }
      return { ok: true, json: async () => ({ ok: true, views, legacy: w.docs.get(body.folder) || null,
                                              unreadable: w.unreadable || 0 }) };
    }
    if(body.manifest === undefined){
      return { ok: true, json: async () => ({ ok: true, manifest: w.docs.get(body.folder) || {} }) };
    }
    const at = body.device ? body.folder + ':' + body.device : body.folder;
    const prev = w.docs.get(at);
    const oldN = prev && typeof prev.n === 'number' ? prev.n : null;
    const newN = typeof body.manifest.n === 'number' ? body.manifest.n : null;
    if(w.collapseGuard && !body.force && oldN !== null && newN !== null && oldN >= 10 && newN < Math.floor(oldN / 2)){
      w.refusals = (w.refusals || 0) + 1;
      return { ok: false, status: 409, json: async () => ({
        ok: false, collapse: true, old: oldN, new: newN, error: 'refused: ' + oldN + ' entries -> ' + newN }) };
    }
    w.saves++;
    if(body.force) w.forced = (w.forced || 0) + 1;
    w.docs.set(at, JSON.parse(JSON.stringify(body.manifest)));
    return { ok: true, json: async () => ({ ok: true }) };
  };

  w.nip44dec = async (_pk, ct) => {
    const s = String(ct);
    if(s.length < NIP44_MIN_PAYLOAD) throw new Error('invalid payload length: ' + s.length);
    if(!s.startsWith('ct:')) throw new Error('unknown encryption version');
    return Buffer.from(s.slice(3), 'base64').toString('utf8');
  };

  w.PC = {
    me: () => ({ pubkey: 'f'.repeat(64) }),
    signAuth: async () => ({ id: 'auth', sig: 'x' }),
    enc: s => String(s == null ? '' : s),
    toast(){}, uiConfirm: async () => true, uiPrompt: async () => '',
    nip44enc: async (_pk, text) => {                       // the ceiling is the whole point
      const n = Buffer.byteLength(String(text), 'utf8');
      if(n < 1 || n > NIP44_MAX)
        throw new Error('invalid plaintext size: must be between 1 and ' + NIP44_MAX + ' bytes');
      return 'ct:' + Buffer.from(String(text), 'utf8').toString('base64');
    },
    nip44dec: w.nip44dec,
    syncBlobs: {
      put: async (bytes) => { const id = 'blob' + (++seq); w.blobs.set(id, Buffer.from(bytes)); return id; },
      get: async (id) => { if(!w.blobs.has(id)) throw new Error('blob ' + id + ' unavailable (404)'); return w.blobs.get(id); },
    },
  };
  return w;
}

/* Load the SHIPPED sync.js into its own context, so module-level state (the IndexedDB handle) cannot
 * leak between scenarios. */
function boot(){
  const world = makeWorld();
  const ctx = {
    console, setTimeout, clearTimeout, setInterval, clearInterval,
    JSON, Promise, Date, Math, Object, Array, String, Number, Error, Boolean, Map, Set, RegExp,
    TextEncoder, TextDecoder, Buffer, crypto: require('crypto').webcrypto,
    localStorage: world.localStorage,
    indexedDB: world.indexedDB,
    navigator: { onLine: true, userAgent: 'node' },
    document: { hidden: false, addEventListener(){}, querySelector: () => null,
                getElementById: () => null, querySelectorAll: () => [] },
    fetch: world.fetch,
    btoa: s => Buffer.from(String(s), 'binary').toString('base64'),
  };
  ctx.window = ctx; ctx.globalThis = ctx; ctx.self = ctx;
  ctx.__PC = world.PC;
  ctx.PCFolderSync = require(path.join(CLIENT, 'foldersync.js'));
  ctx.PCSyncRun = require(path.join(CLIENT, 'syncrun.js'));
  ctx.PCSyncEngine = require(path.join(CLIENT, 'syncengine.js'));
  ctx.PCSyncExec = require(path.join(CLIENT, 'syncexec.js'));
  ctx.ClientSettings = { get: (k, d) => d, set(){} };
  vm.createContext(ctx);
  vm.runInContext(fs.readFileSync(path.join(CLIENT, 'sync.js'), 'utf8'), ctx, { filename: 'sync.js' });
  return { world, ctx, store: ctx.PCSync.store, docs: ctx.PCSync.docs };
}

// A manifest of `n` files, shaped like a real one: a path, a sha, a size, an mtime, a device.
function manifest(n){
  const out = {};
  for(let i = 0; i < n; i++){
    out['Pictures/2019/Holiday/DSC_' + String(i).padStart(5, '0') + '.jpg'] =
      { sha: (i % 16).toString(16).repeat(64).slice(0, 64), size: 4000000 + i,
        mtime: 1786000000000 + i, device: 'DESKTOP-7QK1' };
  }
  return out;
}

const scenarios = [];
const scenario = (name, fn) => scenarios.push({ name, fn });

/* THE ONE THIS FILE EXISTS FOR. A real 15790-file folder found this; 2000 is five times over the
 * ceiling and quick to build. */
scenario('a-folder-past-the-nip44-ceiling-saves', async () => {
  const { world, docs } = boot();
  const paths = manifest(2000);
  let err = '';
  try{ await docs.publish('Documents', paths); await docs.saveIndex('Documents', paths); }
  catch(e){ err = (e && e.message) || String(e); }
  // Stored under `<pair>:<device>` now: one document per device, and only that device writes it.
  let doc = {};
  for(const [k, d] of world.docs) if(k.indexOf('Documents:') === 0) doc = d;
  return {
    ok: !err && !!doc.pathsSha && doc.n === 2000,
    detail: { plaintextBytes: JSON.stringify(paths).length, ceiling: NIP44_MAX, error: err,
              storedAs: doc.pathsSha ? 'blossom blob' : 'inline', n: doc.n },
  };
});

scenario('a-huge-manifest-round-trips', async () => {
  const { docs } = boot();
  const paths = manifest(2000);
  await docs.publish('Documents', paths);
  const got = await docs.views('Documents');
  const back = got.views[Object.keys(got.views)[0]] || {};
  const keys = Object.keys(paths);
  return {
    ok: Object.keys(back).length === keys.length
        && back[keys[0]].sha === paths[keys[0]].sha
        && back[keys[keys.length-1]].mtime === paths[keys[keys.length-1]].mtime,
    detail: { wrote: keys.length, read: Object.keys(back).length },
  };
});

/* A CLIENT OLDER THAN THIS CHANGE MUST FAIL, NOT READ AN EMPTY FOLDER. It looks for `sealed` and
 * falls back to `doc.paths`, so without a marker a v2 document reads as {} — and an empty remote is
 * not a harmless misread: every file becomes "deleted elsewhere", and that device trashes all of
 * them and publishes tombstones the other devices honour. */
scenario('an-old-client-cannot-read-a-v2-manifest-as-empty', async () => {
  const { world, docs } = boot();
  await docs.publish('Documents', manifest(2000));
  // Stored under `<pair>:<device>` now: one document per device, and only that device writes it.
  let doc = {};
  for(const [k, d] of world.docs) if(k.indexOf('Documents:') === 0) doc = d;

  // Exactly what the pre-v2 client did with a document: seal first, `paths` otherwise.
  let oldResult = null, threw = '';
  if(doc.sealed){
    try{ oldResult = JSON.parse(await world.nip44dec('x', doc.sealed)); }
    catch(e){ threw = (e && e.message) || String(e); }
  } else {
    oldResult = doc.paths || {};       // the empty-manifest path — the one that must be unreachable
  }
  return {
    ok: !!threw && oldResult === null,
    detail: { sealedPresent: !!doc.sealed, sealed: String(doc.sealed).slice(0, 20), threw, oldResult },
  };
});

/* `base` must survive a folder too big for localStorage: ~2.6 MB for 15790 entries, against a 5 MB
 * budget shared with everything else this client stores. */
scenario('a-huge-base-persists', async () => {
  const { world, store } = boot();
  const base = manifest(15790);
  await store.save('Documents', { manifest: {}, base });
  const back = await store.base('Documents');
  return {
    ok: Object.keys(back).length === 15790,
    detail: { approxBytes: JSON.stringify(base).length, readBack: Object.keys(back).length,
              wentToLocalStorage: world.ls.size },
  };
});

/* And a base that CANNOT be written must say so. The old try/catch made a quota failure look
 * identical to a successful save; the only symptom was the next sweep starting from file one. */
scenario('a-base-that-cannot-be-stored-throws', async () => {
  const { world, store } = boot();
  world.idbBroken = true;
  let threw = '';
  try{ await store.save('Documents', { manifest: {}, base: manifest(3) }); }
  catch(e){ threw = (e && e.message) || String(e); }
  return { ok: !!threw, detail: { threw } };
});

/* A device that already has an agreement in localStorage keeps it — otherwise everyone's first
 * sweep after this change re-uploads their whole folder once, for nothing. */
scenario('an-existing-localstorage-base-is-still-read', async () => {
  const { world, store } = boot();
  const old = manifest(20);
  world.ls.set('pc_sync_base_Documents', JSON.stringify(old));
  const back = await store.base('Documents');
  return { ok: Object.keys(back).length === 20, detail: { readBack: Object.keys(back).length } };
});

/* Small folders stay INLINE: the blob path costs an upload and a fetch, most folders never need it,
 * and staying inline is what keeps every manifest written before this change readable. */
/* The manifest cache must not hand the same object to two callers: a sweep mutates what it gets. */
scenario('a-cached-manifest-is-not-shared-between-callers', async () => {
  const { store } = boot();
  const paths = manifest(2000);
  await store.save('Documents', { manifest: paths, base: {} });
  const a = await store.manifest('Documents');
  a['Pictures/2019/Holiday/DSC_00000.jpg'].size = 1;         // a caller mutating its copy
  const b = await store.manifest('Documents');               // served from the cache
  return {
    ok: b['Pictures/2019/Holiday/DSC_00000.jpg'].size !== 1,
    detail: { mutatedLeakedIntoNextRead: b['Pictures/2019/Holiday/DSC_00000.jpg'].size === 1 },
  };
});

scenario('a-small-folder-stays-inline', async () => {
  const { world, store } = boot();
  const paths = manifest(50);
  await store.save('Notes', { manifest: paths, base: {} });
  const doc = world.docs.get('Notes') || {};
  const back = await store.manifest('Notes');
  return {
    ok: !doc.pathsSha && typeof doc.sealed === 'string' && Object.keys(back).length === 50,
    detail: { bytes: JSON.stringify(paths).length, storedAs: doc.pathsSha ? 'blob' : 'inline' },
  };
});

/* The count the server's collapse guard reads must stay truthful when the paths are in a blob — it
 * is the ONLY thing the server can see, and it is what stands between a bug and a wiped folder. */
scenario('the-collapse-guard-still-gets-a-count', async () => {
  const { world, docs } = boot();
  const paths = manifest(2000);
  paths['Pictures/gone.jpg'] = { deletedAt: 1786000000000 };     // a tombstone is not a live file
  await docs.publish('Documents', paths);
  // Stored under `<pair>:<device>` now: one document per device, and only that device writes it.
  let doc = {};
  for(const [k, d] of world.docs) if(k.indexOf('Documents:') === 0) doc = d;
  return { ok: doc.n === 2000, detail: { n: doc.n, entries: Object.keys(paths).length } };
});

/* THE COLLAPSE GUARD IS GONE, AND ITS ABSENCE IS THE POINT.
 *
 * It existed because one document had many writers: a device with a stale copy could write it back
 * and erase everything another device had added, and the server refusing a sharp shrink was the only
 * thing standing between that and an emptied folder. A document with ONE writer for ever cannot have
 * that problem — a shrink in this device's own record is this device's own doing, and the other
 * devices' records are untouched by it.
 *
 * What replaces it is the self-heal below: our view is rebuilt from our journal on every sweep, so
 * even a document that is somehow emptied comes back without re-uploading a byte. */
scenario('our own document is restored from the journal, not from the network', async () => {
  const { world, docs } = boot();
  const paths = manifest(40);
  await docs.publish('Documents', paths);
  let key = null;
  for(const [k] of world.docs) if(k.indexOf('Documents:') === 0) key = k;
  world.docs.delete(key);
  await docs.publish('Documents', paths);              // the sweep republishes what it knows
  const back = world.docs.get(key);
  return { ok: !!back && back.n === 40, detail: { restored: !!back, n: back && back.n } };
});

scenario('each-save-points-at-a-fresh-blob', async () => {
  const { world, store } = boot();
  const paths = manifest(2000);
  await store.save('Documents', { manifest: paths, base: {} });
  const first = (world.docs.get('Documents') || {}).pathsSha;
  paths['Pictures/2019/Holiday/NEW.jpg'] = { sha: 'b'.repeat(64), size: 1, mtime: 2, device: 'x' };
  await store.save('Documents', { manifest: paths, base: {} });
  const second = (world.docs.get('Documents') || {}).pathsSha;
  return {
    ok: !!first && !!second && first !== second && world.blobs.size === 2,
    detail: { first, second, blobs: world.blobs.size },
  };
});

(async () => {
  const rows = [];
  for(const s of scenarios){
    try{ const r = await s.fn(); rows.push({ name: s.name, ok: !!r.ok, detail: r.detail }); }
    catch(e){ rows.push({ name: s.name, ok: false, detail: { threw: (e && e.stack) || String(e) } }); }
  }
  process.stdout.write(JSON.stringify(rows, null, 1));
  process.exit(rows.every(r => r.ok) ? 0 : 1);
})();
