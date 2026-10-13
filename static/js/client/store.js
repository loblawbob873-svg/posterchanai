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
  /* YOUR OWN NOTIFICATIONS ARE NOT FIREHOSE, and the newest-N rule was treating them as if they
   * were. Reported from the phone as "I have many notifications, mobile only shows 4 when I load
   * the app".
   *
   * A mention, a reaction, a zap or a repost that p-tags YOU is a handful of events about you
   * personally, and they sit in the same cache as the global feed — which produces thousands. A few
   * minutes of reading the timeline evicts every one of them, so on the next cold start the
   * Notifications view renders whatever survived, which is a single-digit number, while the relay
   * still holds them all. It is the same mistake Notes, the vault, playlists, the desktop layout
   * and the SMS archive each had to be rescued from, and the symptom is the giveaway: not empty,
   * just far too few.
   *
   * CAPPED, because anybody can p-tag anybody — this is the second pin here written by strangers
   * rather than by the signed-in user, so it takes the same treatment as the mini-app one: newest
   * kept, the overflow rejoins the ordinary population and evicts normally. Never a growing set of
   * unevictable rows a spammer can mint. */
  const NOTIF_KINDS = new Set([1, 6, 7, 9735, 1984, 1111, 1621, 1617]);
  /* Read at most every few seconds, never per event: _isPinned runs inside eviction loops over the
     whole cache, and localStorage + JSON.parse per event is a real cost on a phone. */
  /* TOLD, NOT GUESSED — and the guess was wrong for most people.
   *
   * This read `Session.load().pubkey`, and the saved session only carries one for `nip55`. An nsec
   * login saves `{mode:'local', sk}` and an extension login saves `{mode:'nip07'}` — no pubkey at
   * all. So the viewer resolved to '' , `_isNotifPin` answered false for every event, and pinning
   * notifications did NOTHING for the two commonest logins. Reported from a phone signed in with an
   * nsec as "showing only 2 notifications", which is the unpinned behaviour exactly.
   *
   * `window.ME` is not an option: the client keeps ME inside its own IIFE and os.js documents that
   * window.ME is undefined. So the app SETS it at sign-in, and the session remains a fallback for
   * the one mode that does record it. */
  let _mePk = '', _mePkAt = 0;
  function setViewer(pk){ _mePk = String(pk || ''); _mePkAt = Date.now(); }
  function _pinnedViewer(){
    if (_mePk) return _mePk;
    const now = Date.now();
    if (now - _mePkAt > 3000){ _mePkAt = now;
      try { _mePk = String((Session.load() || {}).pubkey || ''); } catch(_){ _mePk = ''; } }
    return _mePk;
  }
  function _isNotifPin(ev){
    if (!NOTIF_KINDS.has(ev.kind)) return false;
    const me = _pinnedViewer();
    /* Your own posts are not notifications, and with no session there is nobody to notify. */
    if (!me || ev.pubkey === me) return false;
    for (const t of ev.tags || []) if (t && t[0] === 'p' && t[1] === me) return true;
    return false;
  }
  function _isPinned(ev){
    if (!ev) return false;
    if (_isNotifPin(ev)) return true;
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
          ['application/x-webxdc','application/webxdc+zip','application/vnd.webxdc+zip']
            .includes(String(t[1] || '').toLowerCase().trim())) return true;
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
         /* `pcai:files-index` IS THE DRIVE. Every name, folder and encryption flag in Files comes
            from this one document — a Blossom server keys on hashes and knows none of it — so
            evicting it does not cost a cached listing, it costs the whole drive until a relay hands
            it back. It was never on this list, and it survived only because the ordinary keep
            budget happened to be big enough; the moment anything else took room, Files could not
            see files that were all still there. Reported as exactly that. */
         t[1].startsWith('pcai:files-index') ||
         t[1].startsWith('pcai:playlist') || t[1] === 'pcai:budget' ||
         t[1] === 'pcai:desktop' || t[1] === 'pcai:automute' || t[1] === 'pcai:agent-tasks' ||
         /* Where each Concord channel was last read, account-wide (concord.js READ_D). Evicted, a second
            device reads every mention from the last week as new again. */
         t[1] === 'pcai:concord-read' ||
         /* What notifications and DMs this account has read (seen.js). Evicted, a phone that read the
            global feed for a while counts every one of them as new again until a relay answers. */
         t[1] === 'pcai:seen' ||
         /* Community folders (concord.js RAIL_D). Evicted, the rail draws without them as if never made. */
         t[1] === 'pcai:concord-rail' ||
         /* Go Live templates (client/livetemplates.js). Evicted, the menu reads as "you never saved any". */
         t[1] === 'pcai:livetemplates' ||
         /* Which shared playlists you accepted. Evicted by a firehose it reads as "you have decided
            nothing", so every offer you already answered comes back to be answered again. */
         t[1] === 'pcai:musicshares' ||
         t[1] === 'pcai:dmkey' || t[1] === 'pcai:dmcache' ||
         /* The VM hosts somebody added by hand. Evicted, Virtual Machines shows no hosts until a
            relay hands the list back — which reads as the hosts being gone. */
         t[1] === 'pcai:vmhosts' ||
         /* Music this account SHARED (musicshare.js). Pinned only when WE wrote it: it is the one
            record "Shared by me" and a revoke are built from, whereas a share addressed to us is
            anybody's to publish — pinning those would let a stranger mint unevictable entries. */
         (t[1].startsWith('pcai:musicshare:') && ev.pubkey && ev.pubkey === _pinnedViewer()))) return true;
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
  /* Roughly what the Notifications view can page through, and far more than the 150 the
     subscription asks for on load. Same reasoning as XDC_PIN_MAX: generous against the real case,
     bounded against a stranger who p-tags you a hundred thousand times. */
  const NOTIF_PIN_MAX = 400;
  /* HOW MANY OF THE LAST SPLIT WERE NOTIFICATIONS — see the ceiling note in _evictMem. */
  let _notifPinned = 0;
  function _splitPinned(list){
    const pin = [], xdc = [], notif = [], rest = [];
    for (const ev of list){
      if (!_isPinned(ev)) { rest.push(ev); continue; }
      if (ev.kind === 1063) xdc.push(ev); else if (_isNotifPin(ev)) notif.push(ev); else pin.push(ev);
    }
    notif.sort((a,b)=>b.created_at-a.created_at);
    _notifPinned = Math.min(notif.length, NOTIF_PIN_MAX);
    for (let i = 0; i < notif.length; i++) (i < NOTIF_PIN_MAX ? pin : rest).push(notif[i]);
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
  function _evictMem(force){
    if (!force && mem.events.size <= MEM_MAX + _pinCount) return;
    const [pin, rest] = _splitPinned([...mem.events.values()]);
    /* THE CEILING RISES FOR DOCUMENTS, NEVER FOR NOTIFICATIONS.
     *
     * `MEM_MAX + _pinCount` exists so a large notebook does not hold the cache permanently over its
     * limit and re-sort on every firehose event. It is a concession to data that CANNOT be refetched
     * — a note, a vault item, an SMS archive — where evicting it loses it.
     *
     * A notification is not that: it is on the relay, and the subscription re-reads 150 of them on
     * every load. Counting them here let 400-800 of them raise a phone's in-memory ceiling by up to
     * a fifth, on top of keeping them all in full — and the screen that pays for that first is the
     * heaviest one, Folder Sync, which was reported loading and then returning to the launcher: a
     * WebView renderer killed for memory, which Android reports as the app closing rather than as
     * anything the app can log. So notifications keep their protection from the eviction ORDER and
     * are excluded from the BUDGET. */
    _pinCount = pin.length - _notifPinned;
    if (rest.length <= MEM_KEEP) return;   // nothing worth trimming — don't pay for a reindex
    // Pinned events are kept in full and come OUT of the budget, but never take all of it: a large
    // notebook must not leave the timeline with nothing cached to render.
    /* AND THE KEEP BUDGET IS COMPUTED THE SAME WAY, which is the half that actually hurt.
     * `MEM_KEEP - pin.length` is how much ordinary cache survives a trim, and `pin` now contains
     * notifications — so pinning hundreds of them SHRANK the room left for everything else by the
     * same number. Everything else includes documents that are not on this list, and the drive
     * index is one: Files went blind and could not list a drive that was perfectly intact. */
    const arr = pin.concat(rest.slice(0, Math.max(MEM_KEEP - (pin.length - _notifPinned), 500)));
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
  /* The combining marks Zalgo text is made of (Combining Diacritical Marks and their supplements,
   * extended, for symbols, half marks). Indic, Thai, Hebrew and Arabic vowel marks are other blocks and
   * never match, and an ordinary accent is precomposed by NFC, so a real name is never touched. */
  const _ZALGO_MARK = /[\u0300-\u036f\u1ab0-\u1aff\u1dc0-\u1dff\u20d0-\u20ff\ufe20-\ufe2f]/g;
  const _ZALGO_RUN = /[\u0300-\u036f\u1ab0-\u1aff\u1dc0-\u1dff\u20d0-\u20ff\ufe20-\ufe2f]{3,}/;
  function _tameName(v){
    if(typeof v !== 'string' || !v) return v;
    // Decomposed, so a mark NFC would have fused into a letter ("U" + hook → "Ủ") is seen and removed
    // too; a real name decomposes to at most two marks per letter (Vietnamese "ễ") and is left alone.
    let n = v; try{ n = v.normalize('NFD'); }catch(_){ return v; }
    if(!_ZALGO_RUN.test(n)) return v;
    const out = n.replace(_ZALGO_MARK, '');
    try{ return out.normalize('NFC'); }catch(_){ return out; }
  }
  const _shownCache = new WeakMap();
  function _shown(meta){
    if(!meta || typeof meta !== 'object') return meta;
    const hit = _shownCache.get(meta); if(hit) return hit;
    let out = meta;
    for(const k of ['name', 'display_name', 'displayName']){
      const t = _tameName(meta[k]);
      if(t !== meta[k]){ if(out === meta) out = Object.assign({}, meta); out[k] = t; }
    }
    _shownCache.set(meta, out);
    return out;
  }
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
  /* ---- PosterChanOS: THE LOCAL RELAY IS THIS CACHE'S DISK, AND INDEXEDDB IS ITS FALLBACK. ----
   *
   * Measured on the desktop and the laptop (2026-10-12): Chromium's LevelDB rewrote this origin's
   * IndexedDB tens to hundreds of MB a minute in compactions. On PosterChanOS a `posterchan-relay` user
   * service (app/services/nostr_relay/osmode.py) is ONE append-only PosterChanDB store on loopback for
   * every window, so the events this cache would have put into IndexedDB go to it instead, as ordinary
   * signed EVENTs. Three rules make that safe:
   *   1. FALLBACK, NEVER A GAP. The relay is only used while its socket is OPEN. Every event sent is held
   *      until the relay answers OK; a socket that closes, or a refusal, puts what it held back into the
   *      IndexedDB batch. With the relay down at boot this file behaves exactly as it always did, and a
   *      timeline is never blank because a service did not start.
   *   2. THE USER'S OWN DOCUMENTS STAY ON THIS DEVICE TOO. Pinned events (`_isPinned`: notes, the vault, the
   *      drive index…) are written to IndexedDB as well, so a relay that is down at the next boot costs
   *      nothing that cannot be refetched. They are few and rarely written.
   *   3. ONLY A LOOPBACK URL, ONLY FROM THE SHELL. The URL comes from the desktop's preload
   *      (`pcShell.localRelay`, read from the file the relay writes once it is listening); anything that is
   *      not ws:// on 127.0.0.1/::1 is ignored, so a page can never be pointed at a remote "cache".
   * Profiles follow the same path (a kind-0 IS an event): while the relay holds them the profile store
   * in IndexedDB is not rewritten per sighting, except for the viewer's own. */
  const LR = { url: '', ws: null, up: false, sent: new Set(), pending: new Map(), retry: 600, timer: null,
               reqs: new Map(), oks: new Map(), stats: { relay: 0, idb: 0, refused: 0, fellBack: 0, opened: 0 } };
  function _lrUrl(){
    try {
      const sh = window.pcShell;
      const u = String((sh && typeof sh.localRelay === 'function') ? (sh.localRelay() || '') : '');
      return /^ws:\/\/(127\.0\.0\.1|\[::1\]):\d{2,5}\/?$/.test(u) ? u : '';
    } catch(_){ return ''; }
  }
  function _lrLive(){ return !!(LR.up && LR.ws && LR.ws.readyState === 1); }
  /* What the relay was holding goes back to IndexedDB: a closed socket proves nothing was stored. */
  /* Straight into IndexedDB, never back through the batch: a batch may find the relay up again and hand
     the same event to it, and an event the relay REFUSED would go round that loop for ever. */
  function _idbPut(list){
    if (!db || !list.length) return;
    LR.stats.idb += list.length;
    try { const s = db.transaction('events','readwrite').objectStore('events'); for (const ev of list) s.put(ev); } catch(_){}
  }
  function _lrFallBack(){
    if (!LR.pending.size) return;
    const back = [...LR.pending.values()];
    LR.pending.clear();
    for (const ev of back){ LR.sent.delete(ev.id); LR.stats.fellBack++; }
    _idbPut(back);
  }
  function _lrOpen(){
    if (!LR.url || LR.ws) return;
    let ws;
    try { ws = new WebSocket(LR.url); } catch(_){ return _lrRetry(); }
    LR.ws = ws;
    ws.onopen = () => { LR.up = true; LR.retry = 600; LR.stats.opened++; };
    ws.onmessage = (m) => {
      let msg; try { msg = JSON.parse(m.data); } catch(_){ return; }
      if (!Array.isArray(msg)) return;
      if (msg[0] === 'OK'){
        const w = LR.oks.get(msg[1]);
        if (w){ LR.oks.delete(msg[1]); const ok = !!msg[2] || /^duplicate/i.test(String(msg[3] || '')); for (const f of w) f(ok); }
        const ev = LR.pending.get(msg[1]); if (!ev) return;
        LR.pending.delete(msg[1]);
        // A duplicate is a success (it is already there); any other refusal keeps the device copy.
        if (!msg[2] && !/^duplicate/i.test(String(msg[3] || ''))){
          LR.stats.refused++; _idbPut([ev]);
        }
      } else if (msg[0] === 'EVENT' || msg[0] === 'EOSE' || msg[0] === 'CLOSED'){
        const r = LR.reqs.get(msg[1]); if (!r) return;
        if (msg[0] === 'EVENT') r.events.push(msg[2]); else { LR.reqs.delete(msg[1]); r.done(r.events); }
      }
    };
    ws.onclose = ws.onerror = () => {
      if (LR.ws !== ws) return;
      LR.ws = null; LR.up = false;
      for (const r of LR.reqs.values()) r.done(r.events);
      LR.reqs.clear();
      for (const w of LR.oks.values()) for (const f of w) f(false);
      LR.oks.clear();
      _lrFallBack();
      _lrRetry();
    };
  }
  function _lrRetry(){
    clearTimeout(LR.timer);
    LR.timer = setTimeout(_lrOpen, LR.retry);
    LR.retry = Math.min(30000, LR.retry * 2);
  }
  function _lrSend(ev){
    if (LR.sent.has(ev.id)) return true;
    try { LR.ws.send(JSON.stringify(['EVENT', ev])); } catch(_){ return false; }
    if (LR.sent.size > 50000) LR.sent.clear();
    LR.sent.add(ev.id); LR.pending.set(ev.id, ev); LR.stats.relay++;
    return true;
  }
  /* One REQ on the store's own socket (the hydrate at boot); resolves [] when the relay is not there. */
  function _lrQuery(filters, ms){
    return new Promise(res => {
      if (!_lrLive()) return res([]);
      const id = 'st' + Math.random().toString(36).slice(2, 9);
      let t = null;
      const done = (evs) => { clearTimeout(t); res(evs || []); };
      LR.reqs.set(id, { events: [], done });
      t = setTimeout(() => { const r = LR.reqs.get(id); LR.reqs.delete(id); try { LR.ws.send(JSON.stringify(['CLOSE', id])); } catch(_){} done(r ? r.events : []); }, ms || 2500);
      try { LR.ws.send(JSON.stringify(['REQ', id].concat(filters))); } catch(_){ LR.reqs.delete(id); done([]); }
    });
  }
  function _lrWait(ms){
    return new Promise(res => { const t0 = Date.now(); const iv = setInterval(() => {
      if (_lrLive() || !LR.ws || Date.now() - t0 > ms){ clearInterval(iv); res(_lrLive()); } }, 25); });
  }
  // batched writes — one transaction (or one burst of frames) per burst instead of per event
  let _wbuf = [], _wt = null;
  function _flushWrites(){
    _wt = null; if (!_wbuf.length) return;
    const batch = _wbuf; _wbuf = [];
    const idb = [];
    for (const ev of batch){
      if (_lrLive() && ev && ev.sig && _lrSend(ev)){ if (_isPinned(ev)) idb.push(ev); }
      else idb.push(ev);
    }
    _idbPut(idb);
  }
  // Keep the on-disk event store bounded too (otherwise it balloons over weeks → slow reloads).
  const IDB_CAP = 8000, IDB_KEEP = 5000;
  /* THE PROFILE STORE HAD NO CEILING, AND NOTHING ANYWHERE DELETED FROM IT.
   *
   * `saveProfile` writes a record for EVERY author whose kind-0 arrives — which, on a client that
   * reads the global feed, is every author on the network the socket has ever mentioned. `_pruneIDB`
   * trims `events` and has never touched `profiles`, so that store was append-only for the life of
   * an install, and `init()` reads ALL of it into memory with `getAll()` before the app paints a
   * pixel. It is the one allocation here that grows with USE rather than with the user's own data,
   * which is exactly the shape of "this was working perfectly fine before".
   *
   * That is a renderer that dies at BOOT, and dies harder every time: the prune runs 8 seconds after
   * the hydrate, so a hydrate that never finishes is a store that is never trimmed. Reported from
   * Windows as "posterchan ran out of memory" at start, while the same bundle ran fine on a laptop
   * that had read less.
   *
   * A PROFILE IS THE ONE THING HERE THAT IS SAFE TO EVICT, and the reasoning is `_isPinned` exactly
   * inverted. Every pin on that list is a document only its author can decrypt, where dropping it
   * means it is GONE until a relay hands it back. A kind-0 is public, on every relay, and this client
   * re-queries one the moment `haveProfile` says no — so evicting one costs a cache miss and nothing
   * else. Profiles are the population the newest-N rule is RIGHT for.
   *
   * The key is `at` — when THIS device last saw the profile — never the kind-0's own `created_at`,
   * which is when that person last edited their bio. Ranked by `created_at`, somebody you read every
   * day who has not touched their profile since 2023 evicts before a spam account that rewrote its
   * bio yesterday. */
  const PROF_CAP = 12000, PROF_KEEP = 8000;
  /* Records written before `at` existed carry only `created_at`. That ranks an old install's whole
     store below anything seen since, which is the right way round: those are the ones nothing has
     confirmed is still worth holding. */
  const _profOrd = (r) => Number((r && (r.at || r.created_at)) || 0);
  /* Which profiles this device keeps. Shared by the memory cap and the disk prune so they cannot
     drift on what "oldest" means — and the viewer's OWN profile is never a candidate, at any size:
     evicting it turns the header into "anon" and hands an edit a half-loaded record to publish
     over. */
  function _profVictims(rows, keep){
    const me = _pinnedViewer();
    const cand = rows.filter(r => r && r.pubkey !== me);
    if (cand.length <= keep) return [];
    cand.sort((a, b) => _profOrd(b) - _profOrd(a));
    return cand.slice(keep);
  }

  function _evictProfiles(){
    if (mem.profiles.size <= PROF_CAP) return;
    for (const r of _profVictims([...mem.profiles.values()], PROF_KEEP)) mem.profiles.delete(r.pubkey);
  }

  /* NEVER `getAll()`. This runs on a store that may already hold hundreds of thousands of records —
     that is the state this prune exists to end — so materialising it to measure it would be the
     same allocation, once more, on the boot that was supposed to fix it. A cursor hands values over
     one at a time and only the key and the rank are retained. */
  async function _pruneProfiles(){
    if (!db) return;
    try {
      const n = await pr(tx('profiles','readonly').count());
      if (n <= PROF_CAP) return;
      const rank = [];
      await new Promise((res, rej) => {
        const q = tx('profiles','readonly').openCursor();
        q.onsuccess = () => {
          const c = q.result;
          if (!c){ res(); return; }
          rank.push({ pubkey: c.value && c.value.pubkey, at: _profOrd(c.value) });
          c.continue();
        };
        q.onerror = () => rej(q.error);
      });
      const del = _profVictims(rank, PROF_KEEP);
      const st = tx('profiles','readwrite');
      for (const r of del) if (r.pubkey) st.delete(r.pubkey);
    } catch(e){ console.warn('profile prune failed', e); }
  }

  /* THE HYDRATE HAS TO SURVIVE THE STORE IT INHERITS, which is the whole point: every install that
     has this bug already has the oversized store on disk, and a fix that only takes effect after one
     successful boot at the old size fixes nobody. So this is a cursor with a BOUNDED selection —
     peak memory is 2x PROF_KEEP records however many are on disk — rather than `getAll()` plus a
     trim, which pays the full cost first and trims the copy. */
  async function _hydrateProfiles(){
    const pick = [];
    /* Read ONCE, not per record: this walk is the hot loop of boot, and on the store this exists to
       bound it runs hundreds of thousands of times. Boot usually does not know the viewer yet —
       sign-in happens after init() — and that is survivable here in a way it is not in the prune:
       an own-profile missing from MEMORY is a refetch, where one missing from DISK is gone. The
       prune runs 8 seconds later, by which time it does know. */
    const me = _pinnedViewer();
    let mine = null;
    const trim = () => {
      pick.sort((a, b) => _profOrd(b) - _profOrd(a));
      pick.length = PROF_KEEP;
    };
    await new Promise((res, rej) => {
      const q = tx('profiles','readonly').openCursor();
      q.onsuccess = () => {
        const c = q.result;
        if (!c){ res(); return; }
        if (c.value && c.value.pubkey){
          if (me && c.value.pubkey === me) mine = c.value; else pick.push(c.value);
        }
        if (pick.length >= PROF_KEEP * 2) trim();
        c.continue();
      };
      q.onerror = () => rej(q.error);
    });
    if (pick.length > PROF_KEEP) trim();
    for (const p of pick) mem.profiles.set(p.pubkey, p);
    if (mine) mem.profiles.set(mine.pubkey, mine);
  }

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

  /* A kind-0 the local relay now holds needs no IndexedDB profile record -- the hydrate below reads them
     back from the relay. The viewer's own profile always keeps its device copy (see _profVictims). */
  function _profToRelay(ev){
    if (!_lrLive() || !ev || !ev.sig || ev.kind !== 0 || ev.pubkey === _pinnedViewer()) return false;
    return _lrSend(ev);
  }
  /* The boot hydrate's second half: what the local relay holds that IndexedDB no longer does (it stops
     receiving the firehose once the relay takes over). Bounded like the IndexedDB hydrate; a relay that is
     not up within a moment costs nothing -- the IndexedDB copy has already been read. */
  async function _hydrateFromLocalRelay(){
    if (!LR.url || !(await _lrWait(1500))) return;
    try {
      const [evs, profs] = await Promise.all([
        _lrQuery([{ limit: 2000 }], 2500),
        _lrQuery([{ kinds: [0], limit: 3000 }], 2500)]);
      let added = 0;
      for (const ev of evs){
        if (!ev || typeof ev.id !== 'string' || mem.events.has(ev.id)) continue;
        LR.sent.add(ev.id); mem.events.set(ev.id, _normEvent(ev)); added++;
      }
      if (added) _reindex();
      const now = Math.floor(Date.now() / 1000);
      for (const ev of profs){
        if (!ev || ev.kind !== 0) continue;
        LR.sent.add(ev.id);
        const cur = mem.profiles.get(ev.pubkey);
        if (cur && cur.created_at >= ev.created_at) continue;
        let meta = {}; try { meta = JSON.parse(ev.content || '{}') || {}; } catch(_){}
        mem.profiles.set(ev.pubkey, { pubkey: ev.pubkey, created_at: ev.created_at, meta, at: now });
      }
      if (mem.events.size > MEM_MAX) _evictMem();
    } catch(e){ console.warn('local relay hydrate failed', e); }
  }

  const Store = {
    async init(){
      LR.url = _lrUrl();
      if (LR.url) _lrOpen();
      try { db = await open(); } catch(e){ console.warn('IDB open failed, memory-only', e); await _hydrateFromLocalRelay(); return; }
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
        await _hydrateProfiles();
      } catch(e){ console.warn('hydrate failed', e); }
      await _hydrateFromLocalRelay();
      setTimeout(() => { _pruneIDB(); _pruneProfiles(); }, 8000);   // trim the on-disk stores after startup (non-blocking)
    },
    /* Where this cache is writing: 'relay' while the PosterChanOS local relay's socket is open, else
       'idb'. `backendStats()` counts what went where (and how much fell back) -- the numbers a report of
       "my timeline was empty" needs first. */
    backend(){ return _lrLive() ? 'relay' : 'idb'; },
    backendStats(){ return Object.assign({ backend: _lrLive() ? 'relay' : 'idb', url: LR.url, pending: LR.pending.size }, LR.stats); },
    localRelayUrl(){ return LR.url; },
    /* THE LOCAL RELAY FOR OTHER CACHES OF SIGNED EVENTS (Concord's envelopes, concord-cache.js). `lrPut`
       answers WHICH ids the relay acknowledged, and nothing else: it never falls back to IndexedDB on the
       caller's behalf -- this store's IndexedDB is the timeline's, and a Concord envelope hydrated into it
       is a kind-1059 the DM code would try to open. The caller keeps its own copy of anything not in the
       answer. `lrGet` reads by id; a missing id is one the relay no longer holds (its byte cap evicts),
       which to a cache is a miss, never an error. Both answer empty while the relay is not up. */
    lrLive(){ return _lrLive(); },
    lrPut(events, ms){
      if (!_lrLive() || !Array.isArray(events) || !events.length) return Promise.resolve(new Set());
      const ok = new Set();
      return Promise.all(events.map(ev => new Promise(res => {
        if (!ev || typeof ev.id !== 'string' || !ev.sig) return res();
        let t = null;
        const fin = (good) => { clearTimeout(t); if (good) ok.add(ev.id); res(); };
        const w = LR.oks.get(ev.id) || []; w.push(fin); LR.oks.set(ev.id, w);
        t = setTimeout(() => { const l = LR.oks.get(ev.id); if (l){ const i = l.indexOf(fin); if (i >= 0) l.splice(i, 1); if (!l.length) LR.oks.delete(ev.id); } fin(false); }, ms || 4000);
        try { LR.ws.send(JSON.stringify(['EVENT', ev])); } catch(_){ fin(false); }
      }))).then(() => ok);
    },
    async lrGet(ids, ms){
      const out = new Map();
      const want = [...new Set((ids || []).filter(x => typeof x === 'string' && x))];
      for (let i = 0; i < want.length && _lrLive(); i += 500){
        const chunk = want.slice(i, i + 500), set = new Set(chunk);
        for (const ev of await _lrQuery([{ ids: chunk, limit: chunk.length }], ms || 3000))
          if (ev && typeof ev.id === 'string' && set.has(ev.id)) out.set(ev.id, ev);
      }
      return out;
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
      if (db || LR.url){ _wbuf.push(ev); if(!_wt) _wt=setTimeout(_flushWrites, 700); }  // batch writes (relay or IDB)
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
        if ((ev.kind===1 || ev.kind===6 || ev.kind===1068 || ev.kind===30023 || ev.kind===40) && (!filterFn || filterFn(ev))) out.push(ev);
      }
      // Collapse EDITED addressable/replaceable events to their LATEST version before returning —
      // the cache keeps every version by id, so an edited kind-30023 article otherwise renders as
      // one duplicate Home/Global card per revision. _latestReplaceable keys
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
        const now = Math.floor(Date.now() / 1000);
        if (cur && cur.created_at >= ev.created_at) {
          // Not newer — but BACKFILL emoji/proxy if this profile was cached before they were stored
          // (so re-fetching the same kind-0 still surfaces them) instead of returning blind.
          let ch = false;
          if (Object.keys(em).length && !cur.emojis) { cur.emojis = em; ch = true; }
          if (proxy && !cur._proxy) { cur._proxy = proxy[1]; ch = true; }
          /* SEEN AGAIN, WHICH IS WHAT THE PRUNE NEEDS TO KNOW — and this is the branch it happens
             in. For anybody you already have, every kind-0 that arrives lands right here and used
             to return without recording that it had. Ranked on the create path alone, a contact
             whose profile you cached a year ago and have received a thousand times since would be
             the FIRST thing evicted.
             Stamped in memory unconditionally (free, and it is what the memory cap reads) and
             written to disk at most once a day per profile: a write per firehose profile event is
             a cost this path cannot carry, and a day of lag against an 8000-record keep decides
             nothing. */
          const seen = Number(cur.at) || 0;
          cur.at = now;
          if ((ch || seen < now - 86400) && db && !_profToRelay(ev)) try { tx('profiles','readwrite').put(cur); } catch(_){}
          return;
        }
        const rec = { pubkey: ev.pubkey, created_at: ev.created_at, meta, at: now };
        if (Object.keys(em).length) rec.emojis = em;
        if (proxy) rec._proxy = proxy[1];
        mem.profiles.set(ev.pubkey, rec);
        if (mem.profiles.size > PROF_CAP) _evictProfiles();
        if (db && !_profToRelay(ev)) try { tx('profiles','readwrite').put(rec); } catch(_){}
      } catch(_){}
    },
    /* WHAT A NAME LOOKS LIKE ON SCREEN — never what it is. A "Zalgo" display name buries every letter
     * under 5-12 stacked combining marks ("the text in his name is not displaying correctly, some new
     * font or something"): a smear across the card that no font can lay out and nobody can read. Every
     * reader of a profile gets a copy with those runs removed (_tameName); the ONE reader that must
     * see the real thing is the owner's own profile editor, which republishes it — profileRaw(). */
    profile(pk){ return _shown((mem.profiles.get(pk)||{}).meta || null); },
    profileRaw(pk){ return (mem.profiles.get(pk)||{}).meta || null; },
    profileEmojis(pk){ return (mem.profiles.get(pk)||{}).emojis || null; },   // NIP-30 name emoji (kept off meta)
    profileProxy(pk){ return (mem.profiles.get(pk)||{})._proxy || null; },     // NIP-48 AP actor URL of a bridged account
    haveProfile(pk){ return mem.profiles.has(pk); },
    profileList(){ return [...mem.profiles.entries()].map(([pubkey,rec])=>({ pubkey, meta: _shown(rec.meta||{}) })); },
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
  /* PARSED ONCE, NOT ON EVERY READ. get() used to re-read and JSON.parse the WHOLE blob per key -- and the
   * blob carries the follow list twice (followsCache + followsSafetyCache, ~70 KB each; 153 KB measured on
   * a PosterChanOS desktop), while DM unwrapping reads `dmSeen` once per message. Profiled on that desktop
   * during an ordinary burst: 419 ms of a 6 s window was this one line, on the page that also draws the
   * taskbar and answers the pointer -- felt as mouse lag. The cache is dropped by our own set() and by a
   * `storage` event, which is how a write from another monitor's page or a window arrives here.
   * An object comes back as a COPY, as it always did: callers have been free to change what get() handed
   * them, and that must not leak into the next read. */
  let _settingsCache = null;
  try{ window.addEventListener('storage', e => { if(!e || e.key === null || e.key === 'pc_nostr_settings') _settingsCache = null; }); }catch(_){}
  const Settings = {
    all(){ try { return JSON.parse(localStorage.getItem('pc_nostr_settings')||'{}'); } catch(_){ return {}; } },
    get(k, d){
      if(!_settingsCache) _settingsCache = Settings.all();
      const v = _settingsCache[k];
      if(v===undefined) return d;
      return (v && typeof v === 'object') ? JSON.parse(JSON.stringify(v)) : v;
    },
    set(k, v){ const a = Settings.all(); a[k]=v; localStorage.setItem('pc_nostr_settings', JSON.stringify(a)); _settingsCache = null; }
  };

  Store.setViewer = setViewer;
  /* THE PHONE SAYS IT IS SHORT OF MEMORY (MainActivity.onTrimMemory → `pc:trim-memory`). Trim the
   * in-memory cache to its keep size NOW rather than waiting for the cap: the alternative Android has
   * is killing the WebView's renderer, which the user sees as the app reloading. Pinned documents
   * (notes, vault, the drive index) are never in the trim -- see _evictMem. */
  try{ window.addEventListener('pc:trim-memory', () => { try{ _evictMem(true); }catch(_){ } }); }catch(_){ }
  window.Store = Store; window.Session = Session; window.ClientSettings = Settings;
})();
