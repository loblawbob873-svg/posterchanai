/* Browser-side cache + session/settings. Notes and profiles are persisted in IndexedDB so
 * timelines render instantly from local data on reload and we only ask the relay for what's
 * NEW (a `since` cursor) instead of refetching everything — that's the performance win. */
(function(){
  const DB_NAME = 'posterchan-nostr', DB_VER = 1;
  let db = null;

  function open(){
    return new Promise((res, rej) => {
      const r = indexedDB.open(DB_NAME, DB_VER);
      r.onupgradeneeded = () => {
        const d = r.result;
        if (!d.objectStoreNames.contains('events')) {
          const s = d.createObjectStore('events', { keyPath: 'id' });
          s.createIndex('kind_created', ['kind','created_at']);
          s.createIndex('pubkey', 'pubkey');
        }
        if (!d.objectStoreNames.contains('profiles')) d.createObjectStore('profiles', { keyPath: 'pubkey' });
        if (!d.objectStoreNames.contains('meta')) d.createObjectStore('meta', { keyPath: 'k' });
      };
      r.onsuccess = () => res(r.result);
      r.onerror = () => rej(r.error);
    });
  }
  function tx(store, mode){ return db.transaction(store, mode).objectStore(store); }
  function pr(req){ return new Promise((res,rej)=>{ req.onsuccess=()=>res(req.result); req.onerror=()=>rej(req.error); }); }

  // in-memory mirror for fast render
  const mem = { events: new Map(), profiles: new Map() };
  // Bound the in-memory event cache: the global firehose streams forever, so an uncapped Map
  // would grow without limit — bloating memory AND making the full-store scans (feed / counts)
  // slower over a long session until the UI goes sluggish. Keep the newest N by created_at.
  const MEM_MAX = 4500, MEM_KEEP = 3000;
  function _evictMem(){
    if (mem.events.size <= MEM_MAX) return;
    const arr = [...mem.events.values()].sort((a,b)=>b.created_at-a.created_at).slice(0, MEM_KEEP);
    mem.events.clear();
    for (const ev of arr) mem.events.set(ev.id, ev);
  }
  // batched IndexedDB writes — one transaction per burst instead of per event (busy-feed perf)
  let _wbuf = [], _wt = null;
  function _flushWrites(){
    _wt = null; if (!db || !_wbuf.length) return;
    const batch = _wbuf; _wbuf = [];
    try { const s = db.transaction('events','readwrite').objectStore('events'); for (const ev of batch) s.put(ev); } catch(_){}
  }
  // Keep the on-disk event store bounded too (otherwise it balloons over weeks → slow reloads).
  const IDB_CAP = 8000, IDB_KEEP = 5000;
  async function _pruneIDB(){
    if (!db) return;
    try {
      const n = await pr(tx('events','readonly').count());
      if (n <= IDB_CAP) return;
      const all = await pr(tx('events','readonly').getAll());
      all.sort((a,b)=>b.created_at-a.created_at);
      const del = all.slice(IDB_KEEP);
      const s = tx('events','readwrite');
      for (const ev of del) s.delete(ev.id);
    } catch(e){ console.warn('IDB prune failed', e); }
  }

  const Store = {
    async init(){
      try { db = await open(); } catch(e){ console.warn('IDB open failed, memory-only', e); return; }
      // hydrate recent notes + all profiles into memory
      try {
        const evs = await pr(tx('events','readonly').getAll());
        evs.sort((a,b)=>b.created_at-a.created_at);
        for (const ev of evs.slice(0, 2000)) mem.events.set(ev.id, ev);
        const profs = await pr(tx('profiles','readonly').getAll());
        for (const p of profs) mem.profiles.set(p.pubkey, p);
      } catch(e){ console.warn('hydrate failed', e); }
      setTimeout(_pruneIDB, 8000);   // trim the on-disk store after startup (non-blocking)
    },
    has(id){ return mem.events.has(id); },
    get(id){ return mem.events.get(id); },
    saveEvent(ev){
      if (mem.events.has(ev.id)) return false;
      mem.events.set(ev.id, ev);
      if (mem.events.size > MEM_MAX) _evictMem();   // bound in-memory cache
      if (db){ _wbuf.push(ev); if(!_wt) _wt=setTimeout(_flushWrites, 700); }  // batch IDB writes
      return true;
    },
    removeEvent(id){
      mem.events.delete(id);
      if (db) try { tx('events','readwrite').delete(id); } catch(_){}
    },
    // notes (kind 1) + reposts (kind 6) newest first, optional author filter
    feed(filterFn){
      const out = [];
      for (const ev of mem.events.values()){
        if ((ev.kind===1 || ev.kind===6 || ev.kind===1068) && (!filterFn || filterFn(ev))) out.push(ev);
      }
      return out.sort((a,b)=>b.created_at-a.created_at);
    },
    byKind(kind){ return [...mem.events.values()].filter(e=>e.kind===kind); },
    all(){ return [...mem.events.values()]; },
    // profiles
    saveProfile(ev){
      try {
        const meta = JSON.parse(ev.content||'{}');
        const cur = mem.profiles.get(ev.pubkey);
        if (cur && cur.created_at >= ev.created_at) return;
        const rec = { pubkey: ev.pubkey, created_at: ev.created_at, meta };
        mem.profiles.set(ev.pubkey, rec);
        if (db) try { tx('profiles','readwrite').put(rec); } catch(_){}
      } catch(_){}
    },
    profile(pk){ return (mem.profiles.get(pk)||{}).meta || null; },
    haveProfile(pk){ return mem.profiles.has(pk); },
    profileList(){ return [...mem.profiles.entries()].map(([pubkey,rec])=>({ pubkey, meta: rec.meta||{} })); },
    async setMeta(k,v){ if(db) try{ await pr(tx('meta','readwrite').put({k,v})); }catch(_){} },
    async getMeta(k){ if(!db) return null; try{ const r = await pr(tx('meta','readonly').get(k)); return r?r.v:null; }catch(_){ return null; } }
  };

  // ---- session (who am I) ----
  const Session = {
    save(s){ localStorage.setItem('pc_nostr_session', JSON.stringify(s)); },
    load(){ try { return JSON.parse(localStorage.getItem('pc_nostr_session')||'null'); } catch(_){ return null; } },
    clear(){ localStorage.removeItem('pc_nostr_session'); }
  };

  // ---- client settings (browser-side) ----
  const Settings = {
    all(){ try { return JSON.parse(localStorage.getItem('pc_nostr_settings')||'{}'); } catch(_){ return {}; } },
    get(k, d){ const v = Settings.all()[k]; return v===undefined?d:v; },
    set(k, v){ const a = Settings.all(); a[k]=v; localStorage.setItem('pc_nostr_settings', JSON.stringify(a)); }
  };

  window.Store = Store; window.Session = Session; window.ClientSettings = Settings;
})();
