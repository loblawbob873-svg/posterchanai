/* seen.js — WHAT THIS ACCOUNT HAS ALREADY SEEN, on every device it signs in on.
 *
 * Reported as: "the blue counts on Notifications and Messages go away -- then I open poster.place on
 * my phone and all those same notifications and DMs claim to be new until I look at them AGAIN." The
 * read markers were localStorage (`pc_notif_seen`, ClientSettings `dmSeen`), i.e. one browser profile,
 * so every device, the PWA, the APK and the desktop app each had to be told separately.
 *
 * The markers now also live in ONE kind-30078 document, `d=pcai:seen`, NIP-44-sealed to the user's
 * own key -- the relay and everybody else learn neither whom the user talks to nor when they read:
 *
 *     { v:1, notif:<newest notification seen, created_at>,
 *       dmAll:<newest DM seen when the Messages list was opened>,
 *       dm:{ <peer pubkey>: <newest message read in that conversation> } }
 *
 * MERGE RULE: every field is a MAX. A marker only ever moves forward, the merge runs on every read
 * and again right before every write, so two devices can never un-read each other and an older copy
 * arriving late changes nothing. The replaceable-doc rule on top of it: NOTHING is published until a
 * relay has ANSWERED a read of the document (complete, and decryptable) -- an absent answer, a timeout
 * or a ciphertext we cannot open is "could not ask", never "nothing was seen", and publishing over it
 * would replace somebody's real markers. A failed read costs a sync, never the markers.
 *
 * Writes are debounced (`delay`, a burst of reads is one event) and flushed on pagehide/hidden by the
 * app. The localStorage copy (`pc_seen:<owner>`, per ACCOUNT) is the fast first paint and carries a
 * `pending` flag, so a mark made just before the page died is published on the next start.
 *
 * DOM-free and dependency-injected: tests/client/seen_sync_runtime.mjs runs this exact file. */
(function(root){
  'use strict';
  const D = 'pcai:seen', CAP = 500;
  const HEX = /^[0-9a-f]{64}$/;
  const num = v => { const n = Number(v); return Number.isFinite(n) && n > 0 ? Math.floor(n) : 0; };

  /* Anything that is not a well-formed marker is dropped, never trusted: the document is ours, but a
   * cache can be corrupted and a hostile relay can hand back anything. */
  function clean(raw){
    const out = { notif: 0, dmAll: 0, dm: {} };
    if(!raw || typeof raw !== 'object' || Array.isArray(raw)) return out;
    out.notif = num(raw.notif); out.dmAll = num(raw.dmAll);
    const dm = raw.dm && typeof raw.dm === 'object' && !Array.isArray(raw.dm) ? raw.dm : {};
    for(const [k, v] of Object.entries(dm)){ const n = num(v); if(HEX.test(k) && n) out.dm[k] = n; }
    return out;
  }
  /* Per-field MAX of `from` into `into`. Returns whether anything in `into` moved. */
  function merge(into, from){
    let changed = false;
    const f = clean(from);
    if(f.notif > into.notif){ into.notif = f.notif; changed = true; }
    if(f.dmAll > into.dmAll){ into.dmAll = f.dmAll; changed = true; }
    for(const [k, v] of Object.entries(f.dm)) if(v > (into.dm[k] || 0)){ into.dm[k] = v; changed = true; }
    return changed;
  }
  /* A conversation mark at or below `dmAll` says nothing `dmAll` does not, so it is dropped first;
   * only then, past CAP, the OLDEST marks go -- the ones `dmAll` is most likely to cover already. */
  function trim(st){
    const rows = Object.entries(st.dm).filter(([, v]) => v > st.dmAll).sort((a, b) => b[1] - a[1]).slice(0, CAP);
    st.dm = Object.fromEntries(rows);
    return st;
  }
  /* Does `a` hold anything `b` lacks? (Whether a write would tell the relay something new.) */
  function ahead(a, b){
    if(a.notif > b.notif || a.dmAll > b.dmAll) return true;
    for(const [k, v] of Object.entries(a.dm)) if(v > Math.max(b.dm[k] || 0, b.dmAll)) return true;
    return false;
  }
  const dTag = ev => ((ev && Array.isArray(ev.tags) ? ev.tags : []).find(t => t && t[0] === 'd') || [])[1];

  function create(o){
    const owner = String(o.owner || '');
    if(!HEX.test(owner)) throw new Error('seen: an account is required');
    const key = 'pc_seen:' + owner;
    const delay = o.delay == null ? 4000 : o.delay, retry = o.retry == null ? 30000 : o.retry;
    const now = o.now || (() => Math.floor(Date.now() / 1000));
    const current = () => !stopped && (!o.isCurrent || !!o.isCurrent(owner));
    let stopped = false, answered = false, loading = null, saving = null, timer = null, loadTries = 0;
    let st = { notif: 0, dmAll: 0, dm: {} }, pending = false;
    try{
      const c = JSON.parse(o.storage.get(key) || 'null');
      if(c && c.owner === owner){ st = clean(c); pending = c.pending === true; }
    }catch(_){ }
    function persist(){
      try{ o.storage.set(key, JSON.stringify({ owner, v: 1, notif: st.notif, dmAll: st.dmAll, dm: st.dm, pending })); }catch(_){ }
    }
    function changed(){ persist(); if(o.onChange) try{ o.onChange(snapshot()); }catch(_){ } }
    const snapshot = () => ({ owner, notif: st.notif, dmAll: st.dmAll, dm: { ...st.dm } });

    /* {ok, state}: ok = a relay answered COMPLETELY and the newest copy (if any) could be opened --
     * only then may a write follow. `state` is whatever could be read, adopted either way (MAX is safe). */
    async function read(){
      let evs = null;
      try{ evs = await o.query([{ authors: [owner], kinds: [30078], '#d': [D], limit: 1 }]); }catch(_){ evs = null; }
      let cached = [];
      try{ cached = (o.cached && o.cached()) || []; }catch(_){ cached = []; }
      const all = [...(Array.isArray(evs) ? evs : []), ...cached]
        .filter(e => e && e.pubkey === owner && e.kind === 30078 && dTag(e) === D && typeof e.content === 'string')
        .sort((a, b) => b.created_at - a.created_at);
      const seenIds = new Set(); let state = null, bad = false, first = true;
      for(const ev of all){
        if(seenIds.has(ev.id) || seenIds.size >= 3) continue; seenIds.add(ev.id);
        let doc = null;
        try{ doc = JSON.parse(await o.decrypt(ev.content) || 'null'); }catch(_){ doc = null; }
        if(!doc || typeof doc !== 'object'){ if(first) bad = true; first = false; continue; }
        first = false;
        if(!state) state = { notif: 0, dmAll: 0, dm: {} };
        merge(state, doc);
      }
      /* "Answered" = every relay EOSE'd, OR this node's own relay did (o.homeAnswered, relay.js). The
       * node's relay holds this document -- every write goes to it -- and with "use my own relays" a
       * single relay in the pool that never answers made every read incomplete, so the marks of an
       * account with such a list were never synced at all. */
      let home = false;
      try{ home = !!(o.homeAnswered && Array.isArray(evs) && o.homeAnswered(evs)); }catch(_){ home = false; }
      return { ok: Array.isArray(evs) && (evs.complete !== false || home) && !bad, state };
    }
    function adopt(state){ if(state && merge(st, state)){ trim(st); changed(); } }
    function load(){
      if(stopped) return Promise.resolve(false);
      if(loading) return loading;
      loading = (async () => {
        const r = await read();
        if(!current()) return false;
        adopt(r.state);
        if(r.ok){ answered = true; loadTries = 0; if(pending) schedule(0); }
        else if(++loadTries <= 6){ setTimeout(() => { if(current() && !answered) load(); }, retry); }
        return r.ok;
      })().finally(() => { loading = null; });
      return loading;
    }
    function schedule(ms){
      if(stopped) return;
      if(timer) clearTimeout(timer);
      timer = setTimeout(() => { timer = null; save(); }, ms == null ? delay : ms);
    }
    function save(){
      if(stopped || !pending) return Promise.resolve(false);
      if(saving){ return saving.then(() => pending && schedule(delay)); }
      saving = (async () => {
        if(!answered){ await load(); if(!answered) return false; }   // never write before a relay answered
        /* Re-read right before writing and keep the MAX: another device may have written since. A read
         * that cannot be completed publishes nothing -- the mark stays pending and is retried. */
        const fresh = await read();
        if(!current()) return false;
        if(!fresh.ok){ adopt(fresh.state); schedule(retry); return false; }
        const remote = fresh.state || { notif: 0, dmAll: 0, dm: {} };
        adopt(remote);
        if(!ahead(st, clean(remote))){ pending = false; persist(); return true; }   // the relay already knows
        trim(st);
        const body = JSON.stringify({ v: 1, notif: st.notif, dmAll: st.dmAll, dm: st.dm });
        const content = await o.encrypt(body);
        if(!current()) return false;
        let ok = false;
        try{ const r = await o.publish(30078, content, [['d', D]]); ok = !!(r && r.ok !== false); }catch(_){ ok = false; }
        if(!current()) return false;
        if(ok){ pending = false; persist(); } else schedule(retry);
        return ok;
      })().catch(() => { schedule(retry); return false; }).finally(() => { saving = null; });
      return saving;
    }
    /* A mark that does not move a marker forward is not a change and costs nothing. */
    function mark(fn){
      if(!current()) return false;
      const before = JSON.stringify(st);
      fn();
      if(JSON.stringify(st) === before) return false;
      trim(st); pending = true; changed(); schedule(delay);
      return true;
    }
    /* NEVER MARK THE FUTURE READ. A mention from a fast clock is dated ahead of now; taken as the
     * marker, it would cover every genuine notification that arrives before that date, on every device
     * (tests/client/test_bell_clears_for_posts_dated_ahead_full_app.py caught exactly that). This
     * device remembers those by id (notifview markNotifsRead); the account only learns "up to now". */
    const cap = ts => Math.min(num(ts), now());
    return {
      owner, D,
      snapshot,
      notif: () => st.notif,
      /* Everything up to this moment in conversation `pk` has been read. */
      dmFor: pk => Math.max(st.dmAll, st.dm[pk] || 0),
      markNotif: ts => mark(() => { const n = cap(ts); if(n > st.notif) st.notif = n; }),
      markDmAll: ts => mark(() => { const n = cap(ts); if(n > st.dmAll) st.dmAll = n; }),
      markDm: (pk, ts) => mark(() => { const n = cap(ts); if(HEX.test(String(pk)) && n > Math.max(st.dmAll, st.dm[pk] || 0)) st.dm[pk] = n; }),
      /* A copy that arrived on a live subscription: adopted (MAX), but it never grants a write. */
      async ingest(ev){
        if(!current() || !ev || ev.pubkey !== owner || ev.kind !== 30078 || dTag(ev) !== D) return false;
        let doc = null;
        try{ doc = JSON.parse(await o.decrypt(ev.content) || 'null'); }catch(_){ return false; }
        if(!current()) return false;
        const before = JSON.stringify(st); adopt(doc); return JSON.stringify(st) !== before;
      },
      load,
      flush: () => { if(timer){ clearTimeout(timer); timer = null; } return save(); },
      answered: () => answered,
      pending: () => pending,
      stop(){ stopped = true; if(timer) clearTimeout(timer); timer = null; },
    };
  }
  root.PCSeen = { create, merge, clean, D, CAP };
})(typeof window !== 'undefined' ? window : globalThis);
