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
  // PINNED events are never evicted, from memory or from disk. Every cache bound here is "keep the
  // newest N by created_at", which is right for the firehose — timeline content is refetchable and
  // endless — and wrong for the one class of event that is the user's OWN DOCUMENT: Notes. A note is
  // only readable by its author, so an evicted note is not "refetch it when you scroll back", it is a
  // note MISSING from your notebook, and missing precisely when you have no network to refetch it
  // with. Browsing the global feed for a few minutes is enough to push a whole imported library out
  // of a 3000-event window, so this is the normal case, not an edge one.
  // Matched by the d-tag rather than by author because Store has no idea who is logged in — and it
  // needs none: `pcai:note…` under kind 30078 is by construction this user's own notes and folders.
  function _isPinned(ev){
    if (!ev) return false;
    /* A MINI APP'S ANNOUNCEMENT (kind 1063, `m application/x-webxdc`).
     *
     * The only non-30078 thing pinned here, and for the same reason as the rest: the newest-N rule is
     * right for a firehose and wrong for a small set of documents that are looked up by name. The
     * apps on the network today were announced in February; a few minutes of reading the global feed
     * evicts every one of them, so Games → Webxdc would go from "nine games" to "nothing here yet"
     * with the relays unreachable — which is exactly when a downloaded, cached, playable game is
     * worth the most. They are tiny (a dozen tags) and there are single digits of them.
     *
     * AND IT IS CAPPED, WHICH NOTHING ELSE HERE IS. Every other pin is a kind-30078 document the
     * signed-in user WROTE, so its size is their own doing. This one is written by anybody: a
     * spammer publishing a few thousand cheap announcements would otherwise mint that many
     * unevictable entries — `_evictMem` keeps pinned events in full, `_pruneIDB` may not delete
     * them, so the set only grows, the quota fills, and the cache can free nothing to recover. The
     * cap is applied in `_splitPinned` (newest kept, the rest evict normally) because a predicate on
     * one event cannot count.
     *
     * DELIBERATELY NOT kind-1 posts that carry an app. `t webxdc` is a tag anyone can put on
     * anything, and an app post ages out like the note it is — the gallery re-queries — while the
     * 1063 announcement, which is what a directory is actually built from, stays. */
    if (ev.kind === 1063){
      for (const t of ev.tags || []) if (t && t[0] === 'm' &&
          String(t[1] || '').toLowerCase() === 'application/x-webxdc') return true;
      return false;
    }
    if (ev.kind !== 30078) return false;
    // `pcai:note*` (Notes) and `pcai:pw*` (the password vault — items, folders and the vault key
    // event itself). Both are documents only their author can decrypt, so evicting one by the
    // newest-N rule that is right for the firehose means it is simply GONE from this device until a
    // relay hands it back — and for the vault key, "until a relay hands it back" is the difference
    // between a working password manager on a plane and an empty one.
    // `pcai:budget` joined them for the same reason and a sharper one: budget.js absorbs relay
    // results into its own state and never calls saveEvent, so the doc is only ever in memory when
    // this device published it — evict that and the relay-change carry has nothing to copy.
    // `pcai:playlist:` joined them for exactly the same reason: a playlist is a kind-30078 document
    // only its author can decrypt, so evicting one by the newest-N rule that is right for the
    // firehose means it is simply GONE from this device until a relay hands it back — and a phone
    // that reads the global feed for a few minutes would do that to a whole library.
    // `pcai:desktop` (the desktop's own arrangement — order, folders, hidden icons) is here for the
    // same reason: it is self-encrypted, and evicting it means the desktop draws the DEFAULT layout
    // until a relay hands it back. That is not a blank screen, which is what makes it dangerous —
    // it looks like the layout was lost rather than like a cache miss.
    // `pcai:dmkey` is the key the decrypted-DM cache is stored under. Evicting it does not lose a
    // message — the gift wraps are still on the relay — it loses the CACHE, so every message is
    // decrypted through the signer again: 800 round trips to a phone, which is the exact cost that
    // key exists to remove.
    // `pcai:sms` (and `pcai:smsout`) — the phone's text messages, archived across devices. Same
    // reason as every entry above and a sharper consequence: the phone's own provider is
    // authoritative, so evicting the archive here does not lose a message on the phone — it loses it
    // on the LAPTOP, which has no other copy. Reading the global feed for a few minutes would empty
    // a year of somebody's texts off every device that is not the phone, silently. `smsout` is a
    // send another device asked for and has not been told about yet; evicting one drops a message
    // that was typed and never sent.
    for (const t of ev.tags || []) if (t && t[0] === 'd' && typeof t[1] === 'string' &&
        (t[1].startsWith('pcai:note') || t[1].startsWith('pcai:pw') ||
         t[1].startsWith('pcai:sms') ||
         t[1].startsWith('pcai:playlist') || t[1] === 'pcai:budget' ||
         t[1] === 'pcai:desktop' || t[1] === 'pcai:agent-tasks' ||
         t[1] === 'pcai:dmkey' || t[1] === 'pcai:dmcache')) return true;
    return false;
  }
  /* ONE event with no `tags` used to take down every timeline in the app, permanently.
   *
   * `tags` is REQUIRED by NIP-01, so nothing well-formed loses anything here — but this cache is fed
   * from places that cannot promise it. A kind-6 repost carries its original as arbitrary JSON in
   * `content`, and whatever that parsed to was saved as an event on the strength of having an `id`.
   * Readers then split into two camps: the defensive ones (`ev.tags || []` — which is why a bad event
   * stores SILENTLY, indexes fine, and looks harmless) and the ones that just walk it. buildCounts()
   * is the second kind, it is the shared count index EVERY note card asks for, and it assigns CIDX
   * only at the END — so one tag-less event threw "Cannot read properties of undefined (reading
   * 'length')" once per card, and the whole feed became "couldn't render this post" while the
   * reply-context line above each stub (which does guard) kept rendering perfectly. That combination
   * — every timeline dead, notifications and the "replying to X" labels fine — is the signature.
   *
   * Normalising HERE, at the boundary, is also the only fix that heals the people it already happened
   * to: the bad event is on their disk, so it is restored into memory on every reload and the feed
   * stays broken no matter how many times they refresh. init()'s hydrate normalises for that reason. */
  function _normEvent(ev){
    if (!Array.isArray(ev.tags)) ev.tags = [];
    else if (!ev.tags.every(Array.isArray)) ev.tags = ev.tags.filter(Array.isArray);
    return ev;
  }
  // Split a list into [pinned, rest-newest-first] — shared by the three places that trim a cache so
  // they cannot drift on what "keep" means.
  /* How many third-party mini-app announcements may be pinned at once. Generous against the real
     network (single digits today, and galLoad asks for at most 300 per load) and small against the
     abuse case, which is unbounded — see the kind-1063 note in _isPinned. */
  const XDC_PIN_MAX = 400;
  function _splitPinned(list){
    const pin = [], xdc = [], rest = [];
    for (const ev of list) (_isPinned(ev) ? (ev.kind === 1063 ? xdc : pin) : rest).push(ev);
    /* The capped pin. Newest announcements are kept pinned; the overflow is not DELETED, it simply
       rejoins the ordinary newest-N population and evicts like any other event — so an app that
       falls out of the cache is one the gallery re-queries, not one that is gone. */
    xdc.sort((a,b)=>b.created_at-a.created_at);
    for (let i = 0; i < xdc.length; i++) (i < XDC_PIN_MAX ? pin : rest).push(xdc[i]);
    rest.sort((a,b)=>b.created_at-a.created_at);
    return [pin, rest];
  }
  // Pinned events are counted, not just filtered, because the eviction CEILING has to rise with
  // them. Without this, a 5000-note notebook keeps mem.events permanently above MEM_MAX, so every
  // single incoming firehose event re-ran a full sort + _reindex of the whole cache — a UI that
  // gets slower the more notes you own, which is precisely backwards.
  let _pinCount = 0;
  function _evictMem(){
    if (mem.events.size <= MEM_MAX + _pinCount) return;
    const [pin, rest] = _splitPinned([...mem.events.values()]);
    _pinCount = pin.length;
    if (rest.length <= MEM_KEEP) return;   // nothing worth trimming — don't pay for a reindex
    // Pinned events are kept in full and come OUT of the budget, but never take all of it: a large
    // notebook must not leave the timeline with nothing cached to render.
    const arr = pin.concat(rest.slice(0, Math.max(MEM_KEEP - pin.length, 500)));
    mem.events.clear();
    for (const ev of arr) mem.events.set(ev.id, ev);
    _reindex();
  }
  // ---- local relay: in-memory indexes so we can answer Nostr filters LOCALLY (author / kind / single-
  // letter tag). The point: reads (feed, profile, thread) resolve from cache instantly and we only hit
  // the network for what's genuinely missing — less latency, far less bandwidth on mobile. Indexes are
  // derived from mem.events (already bounded by MEM_MAX), so they can't grow unbounded. ----
  const idx = { author: new Map(), kind: new Map(), tag: new Map() };  // key -> Set(event id)
  function _idxAddTo(map, key, id){ let s = map.get(key); if (!s){ s = new Set(); map.set(key, s); } s.add(id); }
  function _idxDelFrom(map, key, id){ const s = map.get(key); if (s){ s.delete(id); if (!s.size) map.delete(key); } }
  function _indexAdd(ev){
    _idxAddTo(idx.author, ev.pubkey, ev.id);
    _idxAddTo(idx.kind, ev.kind, ev.id);
    for (const t of ev.tags || []){ if (t && t.length >= 2 && typeof t[0] === 'string' && t[0].length === 1) _idxAddTo(idx.tag, t[0] + ':' + t[1], ev.id); }
  }
  function _indexDel(ev){
    if (!ev) return;
    _idxDelFrom(idx.author, ev.pubkey, ev.id);
    _idxDelFrom(idx.kind, ev.kind, ev.id);
    for (const t of ev.tags || []){ if (t && t.length >= 2 && typeof t[0] === 'string' && t[0].length === 1) _idxDelFrom(idx.tag, t[0] + ':' + t[1], ev.id); }
  }
  // `_pinCount` raises the eviction CEILING, so it has to agree with _splitPinned about the cap —
  // counting every announcement would lift the ceiling by the abuse case itself, which is the growth
  // the cap exists to stop.
  function _reindex(){ idx.author.clear(); idx.kind.clear(); idx.tag.clear(); _pinCount = 0;
    let xdc = 0;
    for (const ev of mem.events.values()){ _indexAdd(ev);
      if (!_isPinned(ev)) continue;
      if (ev.kind === 1063 && ++xdc > XDC_PIN_MAX) continue;
      _pinCount++; } }
  // Candidate event-id set for ONE filter, using the most selective index; null = "scan everything".
  function _candidates(f){
    if (f.ids) return new Set(f.ids.filter(id => mem.events.has(id)));
    const sets = [];
    if (f.authors){ const u = new Set(); for (const a of f.authors){ const s = idx.author.get(a); if (s) for (const id of s) u.add(id); } sets.push(u); }
    if (f.kinds){ const u = new Set(); for (const k of f.kinds){ const s = idx.kind.get(k); if (s) for (const id of s) u.add(id); } sets.push(u); }
    for (const key in f){ if (key.length === 2 && key[0] === '#'){ const u = new Set(); for (const v of f[key]){ const s = idx.tag.get(key[1] + ':' + v); if (s) for (const id of s) u.add(id); } sets.push(u); } }
    if (!sets.length) return null;
    // AND across constraint types: intersect the candidate sets (smallest first is cheapest).
    sets.sort((a, b) => a.size - b.size);
    let acc = sets[0];
    for (let i = 1; i < sets.length; i++){ const nxt = new Set(); for (const id of acc) if (sets[i].has(id)) nxt.add(id); acc = nxt; }
    return acc;
  }
  function _matchOne(ev, f){
    if (f.since && ev.created_at < f.since) return false;
    if (f.until && ev.created_at > f.until) return false;
    if (f.authors && !f.authors.includes(ev.pubkey)) return false;
    if (f.kinds && !f.kinds.includes(ev.kind)) return false;
    if (f.ids && !f.ids.includes(ev.id)) return false;
    for (const key in f){ if (key.length === 2 && key[0] === '#'){ const want = f[key];
      if (!(ev.tags || []).some(t => t && t[0] === key[1] && want.includes(t[1]))) return false; } }
    return true;
  }
  // Newest-first ordering with the NIP-01 replaceable tiebreak: on equal created_at the LOWER event id
  // wins, so _latestReplaceable (keep-first-per-key) picks the spec-defined version deterministically.
  function _newestFirst(a, b){ return (b.created_at - a.created_at) || (a.id < b.id ? -1 : a.id > b.id ? 1 : 0); }
  // Collapse replaceable (0/3/1xxxx) + addressable (3xxxx, keyed by `d` tag) events to the LATEST per
  // key — the cache stores every version by id, but a real relay only serves the newest. Input MUST be
  // newest-first, so the first occurrence per key is the one to keep.
  function _latestReplaceable(evsNewestFirst){
    const seen = new Set(); const out = [];
    for (const ev of evsNewestFirst){
      const k = ev.kind; let key = null;
      if (k === 0 || k === 3 || (k >= 10000 && k < 20000)) key = k + ':' + ev.pubkey;
      else if (k >= 30000 && k < 40000){ const d = ((ev.tags || []).find(t => t && t[0] === 'd') || [])[1] || ''; key = k + ':' + ev.pubkey + ':' + d; }
      if (key !== null){ if (seen.has(key)) continue; seen.add(key); }
      out.push(ev);
    }
    return out;
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
      // Notes are exempt (see _isPinned): this prune DELETES from disk, so trimming one would
      // destroy the offline copy of a document only its author can read.
      const [, rest] = _splitPinned(all);
      const del = rest.slice(IDB_KEEP);
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
        // Every note first, then the newest 2000 of everything else. Hydrating a flat newest-2000
        // would leave a large notebook on disk but out of memory — and query() only ever reads
        // memory, so those notes would be invisible in the app while sitting right there in IDB.
        const [pin, rest] = _splitPinned(evs);
        // Normalise on the way IN, not just on save: a cache poisoned before this shipped is on disk,
        // and without this it is restored intact on every reload and the timeline never comes back.
        for (const ev of pin) mem.events.set(ev.id, _normEvent(ev));
        for (const ev of rest.slice(0, 2000)) mem.events.set(ev.id, _normEvent(ev));
        _reindex();   // build the local query indexes over the hydrated events
        const profs = await pr(tx('profiles','readonly').getAll());
        for (const p of profs) mem.profiles.set(p.pubkey, p);
      } catch(e){ console.warn('hydrate failed', e); }
      setTimeout(_pruneIDB, 8000);   // trim the on-disk store after startup (non-blocking)
    },
    has(id){ return mem.events.has(id); },
    get(id){ return mem.events.get(id); },
    saveEvent(ev){
      if (!ev || typeof ev.id !== 'string') return false;   // nothing downstream can use an event with no id
      _normEvent(ev);
      if (mem.events.has(ev.id)) return false;
      mem.events.set(ev.id, ev);
      _indexAdd(ev);
      if (mem.events.size > MEM_MAX) _evictMem();   // bound in-memory cache (also reindexes)
      if (db){ _wbuf.push(ev); if(!_wt) _wt=setTimeout(_flushWrites, 700); }  // batch IDB writes
      return true;
    },
    /* Remove every event matching a predicate, from memory AND from disk. Store.query() collapses
     * replaceable events to their latest version, so a caller that iterates a query result can only
     * ever delete the newest copy of each and the older ones stay on disk — this walks the actual
     * object store. Used by maintenance actions (e.g. "clear this device's copy of my notes and
     * re-download"), never by anything automatic: nothing in this app deletes user data on a
     * heuristic. Returns how many were removed. */
    async purge(match){
      let n = 0;
      for (const ev of [...mem.events.values()]) if (match(ev)) { _indexDel(ev); mem.events.delete(ev.id); n++; }
      if (db) try {
        const all = await pr(tx('events','readonly').getAll());
        const s = tx('events','readwrite');
        for (const ev of all) if (match(ev)) s.delete(ev.id);
      } catch(e){ console.warn('purge: IDB sweep failed', e); }
      return n;
    },
    removeEvent(id){
      _indexDel(mem.events.get(id));
      mem.events.delete(id);
      if (db) try { tx('events','readwrite').delete(id); } catch(_){}
    },
    // Answer a Nostr REQ filter set from the LOCAL cache (a real relay-style query). Same shape as a
    // relay response: newest-first, deduped, replaceable/addressable events collapsed to their LATEST
    // version (like a real relay — the cache keeps every version by id), each filter's own `limit`
    // applied. Used for cache-first reads so the UI paints instantly and only the delta hits the network.
    query(filters){
      const seen = new Set(); const out = [];
      for (const f of (filters || [])){
        const cand = _candidates(f);
        const ids = cand === null ? mem.events.keys() : cand;
        const hits = [];
        for (const id of ids){ const ev = mem.events.get(id); if (ev && _matchOne(ev, f)) hits.push(ev); }
        hits.sort(_newestFirst);                                   // newest-first (NIP-01 tiebreak: lower id)
        const latest = _latestReplaceable(hits);                   // drop superseded replaceable versions
        const capped = (f.limit != null) ? latest.slice(0, f.limit) : latest;   // limit:0 → empty (NIP-01)
        for (const ev of capped){ if (!seen.has(ev.id)){ seen.add(ev.id); out.push(ev); } }
      }
      return _latestReplaceable(out.sort(_newestFirst));
    },
    // notes (kind 1) + reposts (kind 6) newest first, optional author filter
    feed(filterFn){
      const out = [];
      for (const ev of mem.events.values()){
        if ((ev.kind===1 || ev.kind===6 || ev.kind===1068 || ev.kind===30023 || ev.kind===34550 || ev.kind===40) && (!filterFn || filterFn(ev))) out.push(ev);
      }
      // Collapse EDITED addressable/replaceable events to their LATEST version before returning —
      // the cache keeps every version by id, so an edited kind-30023 article / 34550 community
      // otherwise renders as one duplicate Home/Global card per revision. _latestReplaceable keys
      // 30000-39999 by pubkey+kind+d-tag (regular kinds 1/6/1068/40 get no key and pass through
      // untouched, so distinct notes/channels are never merged). Sort with the NIP-01 tiebreak
      // (_newestFirst) so the surviving version is deterministic, matching the query() path.
      return _latestReplaceable(out.sort(_newestFirst));
    },
    byKind(kind){ return [...mem.events.values()].filter(e=>e.kind===kind); },
    all(){ return [...mem.events.values()]; },
    // profiles
    saveProfile(ev){
      try {
        const meta = JSON.parse(ev.content||'{}');
        const cur = mem.profiles.get(ev.pubkey);
        // Keep NIP-30 custom-emoji tags (for rendering :shortcodes: in the display NAME) on the RECORD,
        // NOT inside meta — meta is the publishable kind-0 content and editing your own profile spreads
        // it, which would pollute your kind-0 and drop your real emoji tags. Read via profileEmojis().
        const em = {}; for (const t of (ev.tags||[])) { if (t[0]==='emoji' && t[1] && t[2]) em[t[1]] = t[2]; }
        // NIP-48 proxy tag on a BRIDGED account's kind-0 → the real AP actor URL. Require protocol
        // 'activitypub' (t[2]) so a non-AP or mislabeled proxy can't be treated as a followable fedi
        // account. Kept on the RECORD (like emojis), never on meta, so it can't pollute the user's own
        // publishable kind-0. Powers "follow the bridged account on Pleroma too".
        const proxy = (ev.tags||[]).find(t => t[0]==='proxy' && t[1] && (t[2]||'').toLowerCase()==='activitypub');
        if (cur && cur.created_at >= ev.created_at) {
          // Not newer — but BACKFILL emoji/proxy if this profile was cached before they were stored
          // (so re-fetching the same kind-0 still surfaces them) instead of returning blind.
          let ch = false;
          if (Object.keys(em).length && !cur.emojis) { cur.emojis = em; ch = true; }
          if (proxy && !cur._proxy) { cur._proxy = proxy[1]; ch = true; }
          if (ch && db) try { tx('profiles','readwrite').put(cur); } catch(_){}
          return;
        }
        const rec = { pubkey: ev.pubkey, created_at: ev.created_at, meta };
        if (Object.keys(em).length) rec.emojis = em;
        if (proxy) rec._proxy = proxy[1];
        mem.profiles.set(ev.pubkey, rec);
        if (db) try { tx('profiles','readwrite').put(rec); } catch(_){}
      } catch(_){}
    },
    profile(pk){ return (mem.profiles.get(pk)||{}).meta || null; },
    profileEmojis(pk){ return (mem.profiles.get(pk)||{}).emojis || null; },   // NIP-30 name emoji (kept off meta)
    profileProxy(pk){ return (mem.profiles.get(pk)||{})._proxy || null; },     // NIP-48 AP actor URL of a bridged account
    haveProfile(pk){ return mem.profiles.has(pk); },
    profileList(){ return [...mem.profiles.entries()].map(([pubkey,rec])=>({ pubkey, meta: rec.meta||{} })); },
    async setMeta(k,v){ if(db) try{ await pr(tx('meta','readwrite').put({k,v})); }catch(_){} },
    async getMeta(k){ if(!db) return null; try{ const r = await pr(tx('meta','readonly').get(k)); return r?r.v:null; }catch(_){ return null; } }
  };

  // ---- session (who am I) ----
  const Session = {
    save(s){ localStorage.setItem('pc_nostr_session', JSON.stringify(s)); },
    load(){ try { return JSON.parse(localStorage.getItem('pc_nostr_session')||'null'); } catch(_){ return null; } },
    clear(){ localStorage.removeItem('pc_nostr_session'); },

    /* Remembered accounts, so a few identities can be kept side by side and swapped between.
     * A SEPARATE key from the live session on purpose: signing out clears the session, and the list
     * has to survive that or "switch account" would only ever work while you were already signed in.
     * Each entry is the session object it takes to sign back in, plus enough profile to draw a row.
     * The secrets here are exactly the ones `pc_nostr_session` already holds — this stores no key
     * that was not on this device a moment ago — but it does hold SEVERAL, so forget() is wired to
     * sign-out rather than being a hidden setting nobody finds. */
    accounts(){
      try { const a = JSON.parse(localStorage.getItem('pc_nostr_accounts')||'[]'); return Array.isArray(a)?a:[]; }
      catch(_){ return []; }
    },
    remember(sess, meta){
      const pk = (meta && meta.pubkey) || (sess && sess.pubkey);
      if(!pk || !sess) return;
      const list = Session.accounts().filter(a => a.pubkey !== pk);
      // Keep whatever we already knew about the profile when this call cannot say — renderMe fills
      // the name and picture in later, and a login knows neither yet.
      const prev = Session.accounts().find(a => a.pubkey === pk) || {};
      list.unshift({ pubkey: pk, sess,
                     npub: (meta && meta.npub) || prev.npub || '',
                     name: (meta && meta.name) || prev.name || '',
                     picture: (meta && meta.picture) || prev.picture || '' });
      try { localStorage.setItem('pc_nostr_accounts', JSON.stringify(list.slice(0, 12))); } catch(_){}
    },
    forget(pk){
      try { localStorage.setItem('pc_nostr_accounts',
              JSON.stringify(Session.accounts().filter(a => a.pubkey !== pk))); } catch(_){}
    }
  };

  /* A newly selected PosterChanOS identity arrives in a NEW Unix home, so it cannot inherit the
   * shared renderer's localStorage. The root switcher leaves exactly one 0600 handoff in that home;
   * Electron reads and deletes it synchronously, and the ordinary Session API owns it thereafter. */
  try{
    const b = window.pcOS && typeof window.pcOS.bootstrap === 'function' ? window.pcOS.bootstrap() : null;
    if(b && b.sess){
      Session.save(b.sess);
      Session.remember(b.sess, b.meta || {});
    }
  }catch(_){}

  // ---- client settings (browser-side) ----
  const Settings = {
    all(){ try { return JSON.parse(localStorage.getItem('pc_nostr_settings')||'{}'); } catch(_){ return {}; } },
    get(k, d){ const v = Settings.all()[k]; return v===undefined?d:v; },
    set(k, v){ const a = Settings.all(); a[k]=v; localStorage.setItem('pc_nostr_settings', JSON.stringify(a)); }
  };

  window.Store = Store; window.Session = Session; window.ClientSettings = Settings;
})();
