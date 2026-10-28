/* The event cache is written to IndexedDB once a MINUTE, not every 0.7 s ("why not just write the event cache every
 * min instead of every 0.7s ?", 2026-10-28) -- and nothing is lost for it. Runs the SHIPPED store.js against a stub
 * IndexedDB and document:
 *   * somebody else's posts are NOT written within seconds of arriving;
 *   * the account's own pinned document (a note) still is, within a second;
 *   * hiding the page (switching app, closing the window) writes everything buffered at once;
 *   * a buffer that fills (2000 events) is written early. */
const fs = require('fs'), vm = require('vm'), path = require('path');
const SRC = fs.readFileSync(process.env.PC_STORE_JS || path.join(__dirname, '..', '..', 'static', 'js', 'client', 'store.js'), 'utf8');

function fakeIDB(){
  const stores = {};
  const req = (fn) => { const r = {}; setTimeout(() => { try { r.result = fn(); r.onsuccess && r.onsuccess({ target: r }); } catch(e){ r.error = e; r.onerror && r.onerror(e); } }, 0); return r; };
  const objectStore = (name) => { const m = stores[name]; return {
    put(v){ const key = v.id || v.pubkey || v.k; m.set(key, v); return req(() => key); },
    get(k){ return req(() => m.get(k)); }, getAll(){ return req(() => [...m.values()]); },
    count(){ return req(() => m.size); }, delete(k){ m.delete(k); return req(() => undefined); },
    openCursor(){ const vals = [...m.values()]; let i = 0; const r = {};
      const step = () => setTimeout(() => { r.result = i < vals.length ? { value: vals[i], continue(){ i++; step(); } } : null; r.onsuccess && r.onsuccess(); }, 0);
      step(); return r; }, createIndex(){} }; };
  const db = { objectStoreNames: { contains: (n) => n in stores }, createObjectStore(n){ stores[n] = new Map(); return objectStore(n); },
               transaction(n){ return { objectStore: () => objectStore(n) }; } };
  return { stores, open(){ const r = {}; setTimeout(() => { r.result = db; r.onupgradeneeded && r.onupgradeneeded(); r.onsuccess && r.onsuccess(); }, 0); return r; } };
}

async function boot(ls){
  const idb = fakeIDB(), listeners = {};
  const doc = { visibilityState: 'visible', addEventListener(t, f){ (listeners[t] = listeners[t] || []).push(f); } };
  const ctx = { console, setTimeout, clearTimeout, setInterval, clearInterval, indexedDB: idb, JSON, Date, Math, Promise,
    document: doc, localStorage: ls || { _d: {}, getItem(k){ return this._d[k] || null; }, setItem(k, v){ this._d[k] = String(v); }, removeItem(k){ delete this._d[k]; } },
    navigator: { onLine: true } };
  ctx.window = ctx; ctx.self = ctx; ctx.globalThis = ctx;
  ctx.addEventListener = (t, f) => { (listeners[t] = listeners[t] || []).push(f); };
  vm.createContext(ctx); vm.runInContext(SRC, ctx);
  const S = ctx.window.Store;
  S.setViewer('me'.padEnd(64, '0'));
  await S.init();
  const fire = (t) => { if (t === 'visibilitychange') doc.visibilityState = 'hidden'; (listeners[t] || []).forEach(f => f({})); };
  const held = () => [...(idb.stores.events || new Map()).keys()];
  return { S, fire, held, ls: ctx.localStorage };
}
const post = (i) => ({ id: ('p' + String(i).padStart(5, '0') + 'x').padEnd(64, '0'), pubkey: 'them'.padEnd(64, '0'), kind: 1, created_at: 1000 + i, tags: [], content: 'post ' + i, sig: 's' });
const note = { id: 'n'.padEnd(64, '0'), pubkey: 'me'.padEnd(64, '0'), kind: 30078, created_at: 5, tags: [['d', 'pcai:note:1']], content: 'c', sig: 's' };
const wait = (ms) => new Promise(r => setTimeout(r, ms));

(async () => {
  const out = {};
  for (const how of ['visibilitychange', 'pagehide']){
    const b = await boot();
    for (let i = 0; i < 50; i++) b.S.saveEvent(post(i));
    b.S.saveEvent(note);
    await wait(1500);
    const before = b.held();
    b.fire(how);
    await wait(100);
    out[how] = { postsAfter1500ms: before.filter(k => k.startsWith('p')).length, noteAfter1500ms: before.includes(note.id),
                 postsAfterHide: b.held().filter(k => k.startsWith('p')).length };
  }
  { const b = await boot();
    for (let i = 0; i < 2000; i++) b.S.saveEvent(post(i));
    await wait(300);
    out.full = { written: b.held().filter(k => k.startsWith('p')).length }; }
  { // An unload whose IndexedDB write never lands (a reload): the next start takes the spill back.
    const b = await boot();
    for (let i = 0; i < 30; i++) b.S.saveEvent(post(i));
    b.fire('pagehide');
    const spilled = JSON.parse(b.ls.getItem('pc_store_spill') || '[]').length;
    const again = await boot(b.ls);                    // a fresh IndexedDB: as if the write was aborted
    out.spill = { spilled, restored: again.S.query([{ kinds: [1] }]).length, cleared: b.ls.getItem('pc_store_spill') === null }; }
  process.stdout.write(JSON.stringify(out));
  process.exit(0);
})();
