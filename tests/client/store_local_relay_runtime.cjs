/* The SHIPPED store.js against a stub local relay (WebSocket) and a stub IndexedDB.
 *
 * PosterChanOS: while the local relay's socket is open, events go to it as EVENT frames instead of into
 * IndexedDB (pinned documents ALSO stay in IndexedDB); with it down -- at boot, or dying mid-session -- every
 * event lands in IndexedDB exactly as before, including the ones the relay was holding unacknowledged and the
 * ones it refused. Prints a JSON verdict per scenario. */
const fs = require('fs'), vm = require('vm'), path = require('path');
// PC_STORE_JS: run another checkout's store.js (the fail-first proof runs master's).
const SRC = fs.readFileSync(process.env.PC_STORE_JS || path.join(__dirname, '..', '..', 'static', 'js', 'client', 'store.js'), 'utf8');

function fakeIDB(){
  const stores = {};
  const req = (fn) => { const r = {}; setTimeout(() => { try { r.result = fn(); r.onsuccess && r.onsuccess({ target: r }); } catch(e){ r.error = e; r.onerror && r.onerror(e); } }, 0); return r; };
  const objectStore = (name) => {
    const m = stores[name];
    return {
      put(v){ const key = v.id || v.pubkey || v.k; m.set(key, JSON.parse(JSON.stringify(v))); fake.puts[name] = (fake.puts[name] || 0) + 1; return req(() => key); },
      get(k){ return req(() => m.get(k)); },
      getAll(){ return req(() => [...m.values()]); },
      count(){ return req(() => m.size); },
      delete(k){ m.delete(k); return req(() => undefined); },
      openCursor(){ const vals = [...m.values()]; let i = 0; const r = {};
        const step = () => setTimeout(() => { r.result = i < vals.length ? { value: vals[i], continue(){ i++; step(); } } : null; r.onsuccess && r.onsuccess(); }, 0);
        step(); return r; },
      createIndex(){},
    };
  };
  const db = {
    objectStoreNames: { contains: (n) => n in stores },
    createObjectStore(n){ stores[n] = new Map(); return objectStore(n); },
    transaction(n){ return { objectStore: () => objectStore(n) }; },
  };
  const fake = { stores, puts: {},
    open(){ const r = {}; setTimeout(() => { r.result = db; r.onupgradeneeded && r.onupgradeneeded(); r.onsuccess && r.onsuccess(); }, 0); return r; } };
  return fake;
}

/* A local relay: `mode` = 'up' | 'down' | 'refuse'. Records every EVENT frame; answers OK; serves REQs. */
function relayClass(state){
  return class FakeWS {
    constructor(url){ this.url = url; this.readyState = 0; state.sockets.push(this);
      setTimeout(() => { if (state.mode === 'down'){ this.readyState = 3; this.onerror && this.onerror(); this.onclose && this.onclose(); return; }
        this.readyState = 1; this.onopen && this.onopen(); }, 5); }
    send(raw){ const m = JSON.parse(raw);
      if (m[0] === 'EVENT'){ state.frames.push(m[1]);
        if (state.silent) return;
        setTimeout(() => this.onmessage && this.onmessage({ data: JSON.stringify(['OK', m[1].id, state.mode !== 'refuse', state.mode === 'refuse' ? 'blocked: no' : '']) }), 2); }
      if (m[0] === 'REQ') setTimeout(() => { for (const ev of state.held) this.onmessage && this.onmessage({ data: JSON.stringify(['EVENT', m[1], ev]) });
        this.onmessage && this.onmessage({ data: JSON.stringify(['EOSE', m[1]]) }); }, 2); }
    close(){ this.readyState = 3; this.onclose && this.onclose(); }
  };
}

async function scenario(name, { mode, url = 'ws://127.0.0.1:21001/', held = [], silent = false, killAfterSend = false }){
  const idb = fakeIDB();
  const state = { mode, frames: [], sockets: [], held, silent };
  const ctx = { console, setTimeout, clearTimeout, setInterval, clearInterval, indexedDB: idb,
    WebSocket: relayClass(state), JSON, Date, Math, Promise,
    localStorage: { _d: {}, getItem(k){ return this._d[k] || null; }, setItem(k, v){ this._d[k] = String(v); }, removeItem(k){ delete this._d[k]; } },
    navigator: { onLine: true }, pcShell: { localRelay: () => url } };
  ctx.window = ctx; ctx.self = ctx; ctx.globalThis = ctx;
  vm.createContext(ctx); vm.runInContext(SRC, ctx);
  const S = ctx.window.Store;
  S.setViewer('me'.padEnd(64, '0'));
  await S.init();
  const post = (i) => ({ id: ('p' + String(i).padStart(4, '0') + 'x').padEnd(64, '0'), pubkey: 'them'.padEnd(64, '0'), kind: 1, created_at: 1000 + i, tags: [], content: 'post ' + i, sig: 's' });
  const note = { id: 'n'.padEnd(64, '0'), pubkey: 'me'.padEnd(64, '0'), kind: 30078, created_at: 5, tags: [['d', 'pcai:note:1']], content: 'c', sig: 's' };
  for (let i = 0; i < 50; i++) S.saveEvent(post(i));
  S.saveEvent(note);
  await new Promise(r => setTimeout(r, 900));               // the 700 ms write batch
  if (killAfterSend){ for (const s of state.sockets) if (s.readyState === 1) s.close(); await new Promise(r => setTimeout(r, 900)); }
  const ids = [...(idb.stores.events || new Map()).keys()];
  return { name, backend: S.backend(), stats: S.backendStats(), frames: state.frames.length,
    idbEvents: ids.length, idbHasNote: ids.includes(note.id), memPosts: S.query([{ kinds: [1] }]).length,
    hydrated: S.query([{ kinds: [1], authors: ['held'.padEnd(64, '0')] }]).length };
}

(async () => {
  const heldEv = (i) => ({ id: ('h' + String(i).padStart(4, '0') + 'x').padEnd(64, '0'), pubkey: 'held'.padEnd(64, '0'), kind: 1, created_at: 2000 + i, tags: [], content: 'from the cache ' + i, sig: 's' });
  const out = [];
  out.push(await scenario('up', { mode: 'up', held: [heldEv(1), heldEv(2)] }));
  out.push(await scenario('down', { mode: 'down' }));
  out.push(await scenario('no-relay', { mode: 'up', url: '' }));
  out.push(await scenario('not-loopback', { mode: 'up', url: 'ws://192.168.0.5:21001/' }));
  out.push(await scenario('refused', { mode: 'refuse' }));
  out.push(await scenario('dies-holding', { mode: 'up', silent: true, killAfterSend: true }));
  process.stdout.write(JSON.stringify(out));
  process.exit(0);   // the relay's reconnect timer would keep node alive
})().catch(e => { console.error(e && e.stack || e); process.exit(1); });
