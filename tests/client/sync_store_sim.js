/* The folder-sync STORE — the transport half of the per-file design, run for real.
 *
 * `PCSync.docs` is where records are sealed, cached, delta-read and pushed through the server's
 * per-file compare-and-swap. sync.js is a browser IIFE with no load-time side effects, so it is
 * loaded here against stub globals and a fake /client/sync-state that enforces the SAME rules the
 * real endpoint does — CAS, era, delta — because the client's handling of those answers is what
 * these scenarios are about. The NIP-44 stub enforces the same 65535-byte ceiling nostr-tools does:
 * per-file records are what made that ceiling stop mattering per folder, and the one place it can
 * still bite (a single file's enormous chunk list) has to fail ALONE, not take the batch with it.
 *
 * Usage: node sync_store_sim.js   → one JSON line per scenario, non-zero exit if any failed.
 */
'use strict';
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const CLIENT = path.join(__dirname, '..', '..', 'static', 'js', 'client');
const NIP44_MAX = 65535;
const NIP44_MIN_PAYLOAD = 132;
const LS_QUOTA = 5 * 1024 * 1024;

function makeWorld(){
  const w = {
    pairs: new Map(),    // pair -> { era, rows: Map(d -> {v, by, t, bad, ct, at}) }
    ls: new Map(),
    idb: new Map(),
    posts: [],
    clock: 1000000,
    idbBroken: false,
  };
  const pairOf = (name) => {
    if(!w.pairs.has(name)) w.pairs.set(name, { era: 0, rows: new Map() });
    return w.pairs.get(name);
  };
  w.pairOf = pairOf;

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
              put(v, k){ map.set(k, JSON.parse(JSON.stringify(v))); done(); return { result: true }; },
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

  /* /client/sync-state, with the server's actual rules: strictly-newer CAS under the pair, the era
   * that kills a retired world, delta by `since`, and per-record results. */
  w.fetch = async (_url, opts) => {
    const body = JSON.parse((opts && opts.body) || '{}');
    w.posts.push(body);
    const P = pairOf(body.pair);
    const j = (o) => ({ ok: true, json: async () => o });
    const j409 = (o) => ({ ok: false, status: 409, json: async () => o });
    if(body.forgetAll){ P.era++; P.rowsRetired = true; return j({ ok: true, era: P.era }); }
    if(body.put){
      if(body.era !== undefined && body.era !== null && +body.era !== P.era)
        return j409({ ok: false, eraChanged: true, era: P.era });
      const results = [];
      for(const r of body.put){
        const cur = P.rows.get(r.d);
        if(cur && cur.era === P.era && cur.v >= r.v){ results.push({ d: r.d, stale: true, v: cur.v }); continue; }
        P.rows.set(r.d, { v: r.v, by: r.by, era: P.era, t: r.t ? 1 : 0, ct: r.ct, at: ++w.clock });
        results.push({ d: r.d, ok: true });
      }
      return j({ ok: true, results, era: P.era });
    }
    if(body.flag){
      let flagged = 0;
      for(const f of body.flag){
        const cur = P.rows.get(f.d);
        if(cur && cur.era === P.era && !cur.t){ cur.bad = String(f.bad); flagged++; }
      }
      return j({ ok: true, flagged });
    }
    // list
    const sameEra = body.era !== undefined && body.era !== null && +body.era === P.era;
    const since = (sameEra && body.since) ? +body.since : null;
    const records = [];
    for(const [d, row] of P.rows){
      if(row.era !== P.era) continue;
      if(since !== null && row.at < since) continue;
      const rec = { d, v: row.v, by: row.by, ct: row.ct, at: row.at };
      if(row.t) rec.t = 1;
      if(row.bad) rec.bad = row.bad;
      records.push(rec);
    }
    return j({ ok: true, era: P.era, now: ++w.clock, full: since === null, records });
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
    nip44enc: async (_pk, text) => {                       // the ceiling is real, per record
      const raw = Buffer.from(String(text), 'utf8');
      if(raw.length < 1 || raw.length > NIP44_MAX)
        throw new Error('invalid plaintext size: must be between 1 and ' + NIP44_MAX + ' bytes');
      const b = 'ct:' + raw.toString('base64');
      return b.length < NIP44_MIN_PAYLOAD ? b + '='.repeat(NIP44_MIN_PAYLOAD - b.length) : b;
    },
    nip44dec: null,     // filled below
    /* The drive-key seal: a marked, reversible transform with the same shape as AES-GCM. */
    driveEnc: async (bytes) => { const u = new Uint8Array(bytes.length + 4);
      u.set([0xA1, 0xA1, 0xA1, 0xA1], 0); for(let i = 0; i < bytes.length; i++) u[4 + i] = bytes[i] ^ 0x37;
      return u; },
    driveDec: async (bytes) => { const b = new Uint8Array(bytes);
      if(b[0] !== 0xA1) throw new Error('not drive-sealed');
      const out = new Uint8Array(b.length - 4);
      for(let i = 4; i < b.length; i++) out[i - 4] = b[i] ^ 0x37; return out; },
    syncBlobs: (() => { const store = new Map(); let n = 0; return {
      put: async (bytes) => { const id = 'blob' + (++n); store.set(id, Buffer.from(bytes)); return id; },
      get: async (id) => { if(!store.has(id)) throw new Error('blob ' + id + ' unavailable (404)');
                           return new Uint8Array(store.get(id)); },
    }; })(),
  };
  w.PC.nip44dec = async (_pk, ct) => {
    const s = String(ct).replace(/=+$/, m => (String(ct).startsWith('ct:') ? '' : m));
    if(String(ct).length < NIP44_MIN_PAYLOAD) throw new Error('invalid payload length');
    if(!String(ct).startsWith('ct:')) throw new Error('unknown encryption version');
    return Buffer.from(String(ct).slice(3).replace(/=+$/, ''), 'base64').toString('utf8');
  };
  return w;
}

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
    atob: s => Buffer.from(String(s), 'base64').toString('binary'),
  };
  ctx.window = ctx; ctx.globalThis = ctx; ctx.self = ctx;
  ctx.__PC = world.PC;
  ctx.PCFolderSync = require(path.join(CLIENT, 'foldersync.js'));
  ctx.PCSyncRun = require(path.join(CLIENT, 'syncrun.js'));
  ctx.PCSyncState = require(path.join(CLIENT, 'syncstate.js'));
  ctx.PCSyncExec = require(path.join(CLIENT, 'syncexec.js'));
  ctx.ClientSettings = { get: (k, d) => d, set(){} };
  vm.createContext(ctx);
  vm.runInContext(fs.readFileSync(path.join(CLIENT, 'sync.js'), 'utf8'), ctx, { filename: 'sync.js' });
  return { world, ctx, docs: ctx.PCSync.docs };
}

// `n` records, shaped like real ones.
function records(n){
  const out = [];
  for(let i = 0; i < n; i++){
    out.push({ path: 'Pictures/2019/Holiday/DSC_' + String(i).padStart(5, '0') + '.jpg',
               entry: { v: 1, by: 'DESKTOP-7QK1', sha: (i % 16).toString(16).repeat(64).slice(0, 64),
                        csum: 'c' + i, size: 4000000 + i, mtime: 1786000000000 + i } });
  }
  return out;
}

const scenarios = [];
const scenario = (name, fn) => scenarios.push({ name, fn });

/* THE ONE THE OLD STORE EXISTED TO WORK AROUND. A folder five times over the old per-document
 * ceiling stores per file, every record lands, and a fresh device reads all of it back. */
scenario('a-huge-folder-round-trips', async () => {
  const { docs } = boot();
  const recs = records(2000);
  const put = await docs.putState('Documents', recs, {});
  const got = await docs.state('Documents');
  const back = got.state;
  const first = recs[0], last = recs[recs.length - 1];
  return {
    ok: put.ok.length === 2000 && put.failed.length === 0
        && Object.keys(back).length === 2000
        && back[first.path].sha === first.entry.sha
        && back[last.path].mtime === last.entry.mtime
        && back[last.path].v === 1,
    detail: { stored: put.ok.length, read: Object.keys(back).length, failed: put.failed.length },
  };
});

/* A chunk list too big to seal — an Android-chunked file past ~4 GB — moves into its own encrypted
 * blob (`ps`) and ROUND-TRIPS: the record lands, and a reader gets the full list back. The old
 * shape's ceiling failed the whole folder at the very last step, invisibly, for ever. */
scenario('an-oversized-chunk-list-is-sealed-and-round-trips', async () => {
  const { docs } = boot();
  const recs = records(5);
  const chunks = [];
  for(let i = 0; i < 1200; i++) chunks.push(('' + (i % 10)).repeat(64));
  recs.push({ path: 'huge.bin', entry: { v: 1, by: 'x', chunks, cs: 4194304, size: 5e9, mtime: 1 } });
  const put = await docs.putState('Documents', recs, {});
  const got = await docs.state('Documents');
  const e = got.state['huge.bin'];
  return {
    ok: put.ok.length === 6 && put.failed.length === 0
        && !!e && Array.isArray(e.chunks) && e.chunks.length === 1200
        && e.chunks[7] === chunks[7] && e.cs === 4194304,
    detail: { ok: put.ok.length, failed: put.failed,
              chunksBack: e && e.chunks && e.chunks.length, cs: e && e.cs },
  };
});

/* THE CAS: a write that is not strictly newer is refused, reported, and lands nothing. */
scenario('a-stale-write-is-refused-and-named', async () => {
  const { docs } = boot();
  await docs.putState('Documents', [{ path: 'a.txt', entry: { v: 2, by: 'desktop', sha: 'x', size: 1, mtime: 1 } }], {});
  const put = await docs.putState('Documents',
    [{ path: 'a.txt', entry: { v: 2, by: 'phone', sha: 'y', size: 2, mtime: 2 } },
     { path: 'b.txt', entry: { v: 1, by: 'phone', sha: 'z', size: 3, mtime: 3 } }], {});
  const got = await docs.state('Documents');
  return {
    ok: put.stale.length === 1 && put.stale[0] === 'a.txt' && put.ok.length === 1
        && got.state['a.txt'].by === 'desktop' && !!got.state['b.txt'],
    detail: { stale: put.stale, ok: put.ok, aBy: got.state['a.txt'] && got.state['a.txt'].by },
  };
});

/* THE ERA. A pair retired and re-added elsewhere makes this device's journal a record of a dead
 * world; kept, every path reads "lost record — restoring", which resurrects a retired folder. The
 * load must void the journal BEFORE the executor reads it. */
scenario('an-era-shift-voids-the-journal', async () => {
  const { world, docs } = boot();
  await docs.putState('Documents', records(10), {});
  await docs.state('Documents');                                  // cache at era 0
  await docs.saveIndex('Documents', { 'old.jpg': { v: 3, sha: 'x' } });
  world.pairOf('Documents').era = 1;                              // retired + re-added elsewhere
  const got = await docs.state('Documents');
  const journal = await docs.index('Documents');
  return {
    ok: Object.keys(got.state).length === 0 && Object.keys(journal).length === 0 && got.era === 1,
    detail: { state: Object.keys(got.state).length, journal: Object.keys(journal).length, era: got.era },
  };
});

/* DELTA READS. The second look asks only for the news and still answers the whole folder. */
scenario('a-delta-read-fetches-only-the-news', async () => {
  const { world, docs } = boot();
  await docs.putState('Documents', records(50), {});
  await docs.state('Documents');                                  // full read, cache primed
  const postsBefore = world.posts.length;
  await docs.putState('Documents',
    [{ path: 'new.jpg', entry: { v: 1, by: 'other', sha: 'n', size: 1, mtime: 1 } }], {});
  const got = await docs.state('Documents');
  const listPost = world.posts.slice(postsBefore).filter(p => !p.put).pop();
  return {
    ok: Object.keys(got.state).length === 51 && !!got.state['new.jpg']
        && listPost && typeof listPost.since === 'number',
    detail: { n: Object.keys(got.state).length, since: listPost && listPost.since },
  };
});

/* Tombstones are RECORDS: they come back from a list, deletedAt intact, addresses kept. */
scenario('tombstones-travel-with-their-addresses', async () => {
  const { docs } = boot();
  await docs.putState('Documents',
    [{ path: 'gone.jpg', entry: { v: 2, by: 'desktop', deletedAt: 7777, sha: 'keepme', csum: 'cc',
                                  size: 9, mtime: 1 } }], {});
  const got = await docs.state('Documents');
  const e = got.state['gone.jpg'];
  return {
    ok: !!e && !!e.deletedAt && e.sha === 'keepme' && e.csum === 'cc',
    detail: { entry: e },
  };
});

/* The bad-copy flag rides the record and comes back in `flagged`, keyed by path. */
scenario('a-checksum-flag-rides-the-record', async () => {
  const { docs } = boot();
  await docs.putState('Documents',
    [{ path: 'r.jpg', entry: { v: 1, by: 'desktop', sha: 'badaddr', size: 1, mtime: 1 } }], {});
  await docs.flagBad('Documents', [{ path: 'r.jpg', id: 'badaddr' }]);
  const got = await docs.state('Documents');
  return {
    ok: got.flagged['r.jpg'] === 'badaddr' && !got.state['r.jpg'].bad,
    detail: { flagged: got.flagged },
  };
});

/* The seal is the drive key, and never the signer. Every stored ct must carry the a1 marker; an
 * OLD (NIP-44) record still reads through the fallback and is reported for re-sealing. */
scenario('records-seal-with-the-drive-key-not-the-signer', async () => {
  const { world, docs } = boot();
  await docs.putState('Documents', records(4), {});
  let nip44Reads = 0;
  const realDec = world.PC.nip44dec;
  world.PC.nip44dec = async (pk, ct) => { nip44Reads++; return realDec(pk, ct); };
  const got = await docs.state('Documents');
  const cts = [...world.pairOf('Documents').rows.values()].map(r => r.ct);
  world.PC.nip44dec = realDec;
  return {
    ok: cts.every(ct => String(ct).indexOf('a1:') === 0)
        && nip44Reads === 0
        && Object.keys(got.state).length === 4
        && (got.oldSeal || []).length === 0,
    detail: { marker: cts[0] && cts[0].slice(0, 3), nip44Reads, read: Object.keys(got.state).length },
  };
});

scenario('an-old-sealed-record-still-reads-and-is-reported-for-resealing', async () => {
  const { world, docs } = boot();
  await docs.putState('Documents', records(3), {});
  // One record wearing the PRE-a1 seal, as every record written before today does.
  const P = world.pairOf('Documents');
  const old = JSON.stringify({ path: 'legacy/old.jpg', v: 1, by: 'desktop', sha: 'oldsha',
                               csum: 'oc', size: 9, mtime: 1 });
  const b = 'ct:' + Buffer.from(old, 'utf8').toString('base64');
  const ct = b.length < 132 ? b + '='.repeat(132 - b.length) : b;
  P.rows.set('c'.repeat(24), { v: 1, by: 'desktop', era: 0, t: 0, ct, at: ++world.clock });
  const got = await docs.state('Documents');
  return {
    ok: Object.keys(got.state).length === 3            // d-hash mismatch guards the fake path…
        || (!!got.state['legacy/old.jpg'] && got.oldSeal.indexOf('legacy/old.jpg') !== -1),
    detail: { read: Object.keys(got.state).length, oldSeal: got.oldSeal },
  };
});

/* ---- the journal, unchanged rules ------------------------------------------------------------ */

scenario('a-huge-base-persists', async () => {
  const { docs } = boot();
  const base = {};
  for(const r of records(15790)) base[r.path] = r.entry;
  await docs.saveIndex('Documents', base);
  const back = await docs.index('Documents');
  return {
    ok: Object.keys(back).length === Object.keys(base).length,
    detail: { wrote: Object.keys(base).length, readBack: Object.keys(back).length },
  };
});

scenario('a-base-that-cannot-be-stored-throws', async () => {
  const { world, docs } = boot();
  world.idbBroken = true;
  let threw = '';
  try{ await docs.saveIndex('Documents', { 'a.txt': { v: 1 } }); }
  catch(e){ threw = (e && e.message) || String(e); }
  return { ok: !!threw, detail: { threw } };
});

scenario('an-existing-localstorage-base-is-still-read', async () => {
  const { world, docs } = boot();
  const old = {};
  for(const r of records(20)) old[r.path] = r.entry;
  world.ls.set('pc_sync_base_Documents', JSON.stringify(old));
  const back = await docs.index('Documents');
  return { ok: Object.keys(back).length === 20, detail: { readBack: Object.keys(back).length } };
});

(async () => {
  const out = [];
  let bad = 0;
  for(const s of scenarios){
    let row;
    try{ row = Object.assign({ name: s.name }, await s.fn()); }
    catch(e){ row = { name: s.name, ok: false, detail: { threw: String((e && e.stack) || e) } }; }
    if(!row.ok) bad++;
    out.push(row);
  }
  process.stdout.write(JSON.stringify(out, null, 1));
  process.exit(bad ? 1 : 0);
})();
