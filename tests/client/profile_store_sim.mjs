/* Run the SHIPPED store.js against a fake IndexedDB.
 *
 * The bug lives entirely in the IndexedDB paths — what boot reads and what nothing ever deleted —
 * so the existing store harness (which runs with `indexedDB: undefined`) is blind to it by
 * construction. This fake implements only what store.js asks of it, and COUNTS the calls, because
 * one of the questions is not what the hydrate produced but how it got there: a `getAll()` on a
 * store holding hundreds of thousands of records is the allocation being fixed, and a hydrate that
 * pays it and then trims the copy passes every assertion about its result. */
import fs from 'node:fs';
import vm from 'node:vm';

const src = fs.readFileSync(new URL('../../static/js/client/store.js', import.meta.url), 'utf8');
const plan = JSON.parse(process.argv[2]);

const calls = { getAll: [], count: [], openCursor: [] };
const stores = { events: new Map(), profiles: new Map(), meta: new Map() };
const KEYPATH = { events: 'id', profiles: 'pubkey', meta: 'k' };

/* Requests resolve on a later microtask, the way a real one does — code that assumes a synchronous
 * answer would pass against anything simpler. */
function req(fn) {
  const r = { onsuccess: null, onerror: null, result: undefined };
  queueMicrotask(() => {
    try { r.result = fn(); r.onsuccess && r.onsuccess(); }
    catch (e) { r.error = e; r.onerror && r.onerror(); }
  });
  return r;
}

function objectStore(name) {
  const m = stores[name];
  return {
    getAll: () => { calls.getAll.push(name); return req(() => [...m.values()]); },
    count: () => { calls.count.push(name); return req(() => m.size); },
    get: (k) => req(() => m.get(k)),
    put: (v) => req(() => { m.set(v[KEYPATH[name]], v); }),
    delete: (k) => req(() => { m.delete(k); }),
    createIndex: () => {},
    openCursor: () => {
      calls.openCursor.push(name);
      const keys = [...m.keys()];
      let i = 0;
      const r = { onsuccess: null, onerror: null, result: null };
      const step = () => queueMicrotask(() => {
        if (i >= keys.length) { r.result = null; r.onsuccess && r.onsuccess(); return; }
        const k = keys[i++];
        r.result = { key: k, value: m.get(k), continue: step };
        r.onsuccess && r.onsuccess();
      });
      step();
      return r;
    },
  };
}

const fakeDb = {
  objectStoreNames: { contains: (n) => n in stores },
  createObjectStore: (n) => { stores[n] = stores[n] || new Map(); return objectStore(n); },
  transaction: (name) => ({ objectStore: () => objectStore(name) }),
};

/* Seed the on-disk profile store BEFORE the client boots — this is an install that ALREADY has the
   oversized store, which is the only state the fix has to work in. Generated here rather than
   passed in, so a hundred thousand records cost nothing on the command line. */
for (const p of plan.seed || []) stores.profiles.set(p.pubkey, p);
for (let i = 0; i < (plan.seedCount || 0); i++) {
  /* `at` ascends with the index, so p0 is the oldest thing on disk and the last one is the newest.
     A slice of records deliberately carries NO `at` at all — that is what an install written before
     this existed looks like, and they must rank below everything that has been seen since. */
  const rec = { pubkey: 'p' + i, created_at: 1000 + i, meta: { name: 'p' + i } };
  if (i >= (plan.legacy || 0)) rec.at = 100000 + i;
  stores.profiles.set(rec.pubkey, rec);
}

/* The startup prune is scheduled 8 seconds OUT, and that gap is part of the behaviour: boot does not
   know who is signed in, and sign-in lands inside it. So the timer is held rather than fired, and
   the plan decides what happens in between — which is the only way to model "the prune ran knowing
   the viewer that init() could not see". No test hook in the shipped file; this drives the callback
   the real timer would have. */
const deferred = [];
const fastTimeout = (fn, ms) => (ms >= 8000 ? (deferred.push(fn), 0) : setTimeout(fn, ms));

const ctx = {
  console, clearTimeout, setInterval, clearInterval, queueMicrotask,
  setTimeout: fastTimeout,
  crypto: (await import('node:crypto')).webcrypto,
  localStorage: { _d: {}, getItem(k){ return this._d[k] || null; },
                  setItem(k, v){ this._d[k] = String(v); }, removeItem(k){ delete this._d[k]; } },
  navigator: { onLine: true },
  indexedDB: { open: () => {
    const r = { onupgradeneeded: null, onsuccess: null, onerror: null, result: fakeDb };
    queueMicrotask(() => { r.onupgradeneeded && r.onupgradeneeded(); r.onsuccess && r.onsuccess(); });
    return r;
  } },
};
ctx.window = ctx; ctx.self = ctx; ctx.globalThis = ctx;
vm.createContext(ctx);
vm.runInContext(src, ctx);
const Store = ctx.window.Store;

if (plan.viewer) Store.setViewer(plan.viewer);
await Store.init();
// Whoever signs in during the 8 seconds between boot and the prune.
if (plan.viewerAfterBoot) Store.setViewer(plan.viewerAfterBoot);

const k0 = (pk, created) => ({ id: 'e' + pk, pubkey: pk, kind: 0, created_at: created,
                               tags: [], content: JSON.stringify({ name: pk }), sig: 'x' });
for (const s of plan.save || []) Store.saveProfile(k0(s.pubkey, s.created_at));

// …and now the 8-second timer fires.
for (const fn of deferred) fn();
// Let the prune (and the writes it makes) drain.
for (let i = 0; i < 50; i++) await new Promise(r => setTimeout(r, 0));

process.stdout.write(JSON.stringify({
  calls, scheduled: deferred.length,
  hydrated: Store.profileList().length,
  held: Store.profileList().map(p => p.pubkey).sort(),
  disk: [...stores.profiles.keys()].sort(),
  diskRec: Object.fromEntries([...stores.profiles.entries()].map(([k, v]) => [k, v.at || null])),
}));
