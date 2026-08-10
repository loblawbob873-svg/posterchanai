/* Playlists for the encrypted Music library.
 *
 * A playlist is a NAME and an ORDER over tracks the library already holds. It stores sha256s and
 * nothing else — no titles, no bytes — so it costs almost nothing and can never disagree with the
 * library about what a track is called.
 *
 * STORAGE. One kind-30078 event PER PLAYLIST, `d = pcai:playlist:<id>`, content NIP-44-encrypted to
 * the user's OWN key, tagged `l = pcai-music` so the whole set is one indexed subscription. The same
 * shape Notes uses, for the same two reasons: ONE document holding every playlist would be a
 * read-modify-write of all of them per save (two devices editing different playlists lose one), and
 * an INDEX document is a second source of truth that one empty read can wipe. There isn't one —
 * `all()` is whatever decrypted, which is the only answer that cannot be wrong.
 *
 * Encrypted to the user's own key means the server cannot read a playlist: no `playlist` command,
 * nothing on Telegram, and the AI cannot see them. The same trade the music library itself makes.
 *
 * EVERY ONE OF THESE WAS A REAL BUG IN THE FIRST DRAFT, and each loses data silently:
 *
 *  1. `Relay.publish` ALWAYS resolves an object — `{ok:false,msg:'timeout'}` on failure — so
 *     `!!(await publish(ev))` is true even when nothing accepted it. The offline queue then cleared
 *     itself on a failed re-send and the edit was gone. It is `r && r.ok`, as notes.js has it.
 *  2. The client cache evicts newest-N by created_at, which is right for the firehose and fatal for
 *     a document only its author can decrypt: minutes of feed reading would drop the library. That
 *     is why `_isPinned` in store.js exempts `pcai:playlist` — not optional, and asserted by
 *     tests/test_client_store_pinning.py.
 *  3. A tombstone that merely DELETES the map entry can be undone by an older copy arriving later in
 *     the same batch (two relays disagreeing): with the entry gone, the `have._at >= created_at`
 *     guard has nothing to compare against. A deletion is recorded, not erased.
 *  4. The kind-5 must not out-rank the tombstone. The relay deletes every event for the address with
 *     `created_at <= ` the kind-5's, so publishing it AFTER the tombstone deletes the tombstone —
 *     and a device that was offline then sees no event at all, keeps its cached copy, and
 *     republishes it on the next edit. The kind-5 goes first.
 *  5. NIP-44 refuses a plaintext over 65535 bytes, so a playlist runs out of room at roughly a
 *     thousand tracks — and the throw lands at the LAST step of a save, after the user has seen the
 *     track appear. The real serialised body is measured BEFORE publishing.
 *  6. A save that did not happen must not leave a playlist on screen. Every mutator rolls back.
 *  7. A queue nothing drains is a queue that loses writes. `flush()` runs on `online` and at load.
 *
 * A track missing from THIS device is not a track that was deleted — unresolvable shas stay in the
 * document for ever and are dropped only at RENDER, exactly as folder sync keeps a path it cannot
 * see yet.
 */
(function(){
  'use strict';

  const KIND = 30078;
  const D_PL = 'pcai:playlist:';
  // Single-letter tag → the relay indexes it, so the set is one filtered subscription rather than
  // "fetch every 30078 this user ever wrote and sift".
  const L_TAG = 'pcai-music';
  const PENDING_KEY = 'pcaiPlaylistsPending';

  /* The ceiling NIP-44 imposes, minus room for the envelope. Measured against the real JSON rather
   * than a track count: a long name eats the same budget, and a count that was right when it was
   * written stops being right the moment the shape changes. */
  const BODY_MAX = 60000;

  let PC=null, toast=null, publish=null;
  const Relay = () => window.Relay;
  const Store = () => window.Store;
  const ME = () => (PC && PC.me && PC.me()) || null;

  const now = () => Math.floor(Date.now()/1000);
  const _id = () => Math.random().toString(36).slice(2,10) + now().toString(36);

  let _lib = null;            // Map<id, playlist|deadMarker>; null = never loaded
  let _sub = null, _loading = null, _flushWired = false;
  const _watchers = new Set();

  function _boot(){
    if(PC) return true;
    PC = window.__PC;
    if(!PC) return false;
    toast = PC.toast; publish = PC.publish;
    if(!_flushWired){
      _flushWired = true;
      // (7) Something has to drain the queue. Coming back online is the moment that matters.
      try{ window.addEventListener('online', ()=>{ flush().catch(()=>{}); }); }catch(_){}
    }
    return true;
  }

  const FILTER = () => ({ kinds:[KIND], authors:[ME().pubkey], '#l':[L_TAG] });
  function _changed(){ for(const fn of _watchers){ try{ fn(); }catch(_){} } }

  // ---------------------------------------------------------------- shape

  const _isDead = p => !!(p && p._dead);

  /* Normalise whatever came out of the relay into the shape the rest of the code may assume. A
   * document written by a newer client — or half-written by a crash — must not be able to make
   * `all()` throw, because one bad playlist would take the whole screen with it. */
  function _clean(obj, id, at){
    const seen = new Set(), out = [];
    for(const t of (Array.isArray(obj && obj.tracks) ? obj.tracks : [])){
      // Exact 64-hex only: anything else is not a Blossom sha and would sit in the list for ever as
      // a row that can never play. Deduped, because adding the same track twice is a slip.
      if(typeof t === 'string' && /^[0-9a-f]{64}$/i.test(t)){
        const k = t.toLowerCase();
        if(!seen.has(k)){ seen.add(k); out.push(k); }
      }
    }
    return { id,
      name: String((obj && obj.name) || 'Untitled playlist').slice(0,120),
      tracks: out,
      created: Number(obj && obj.created) || at || now(),
      updated: Number(obj && obj.updated) || at || now(),
      _at: at || 0 };
  }
  const _body = pl => ({ name:pl.name, tracks:pl.tracks, created:pl.created, updated:pl.updated });
  // (5) The event carries the CIPHERTEXT; NIP-44's limit is on the plaintext, which is this.
  const _tooBig = pl => JSON.stringify(_body(pl)).length > BODY_MAX;

  // ---------------------------------------------------------------- read

  async function _absorb(lib, evs){
    // Newest first per id: copies of one `d` arrive from several relays with different created_at,
    // and an older one must never overwrite a newer.
    for(const ev of (evs||[]).slice().sort((a,b)=>(b.created_at||0)-(a.created_at||0))){
      const d = (ev.tags||[]).find(t=>t[0]==='d');
      if(!d || !d[1] || !d[1].startsWith(D_PL)) continue;
      const id = d[1].slice(D_PL.length);
      if(!id) continue;
      const have = lib.get(id);
      if(have && have._at >= ev.created_at) continue;
      /* (3) A deletion is RECORDED, not erased. Empty content is the tombstone — an addressable
       * event cannot be un-published, and a relay that ignores kind-5 serves the last real version
       * for ever, so this is what a delete is. Keeping a dated marker is what lets the guard above
       * reject an older copy of the same playlist arriving afterwards; deleting the entry outright
       * left nothing to compare against and the playlist came back. */
      if(!ev.content){ lib.set(id, { id, _at: ev.created_at, _dead:true }); continue; }
      let obj = null;
      try{ obj = JSON.parse(await PC.nip44dec(ME().pubkey, ev.content)); }
      catch(_){ continue; }        // not ours / not decryptable here — skip, never drop
      if(!obj || typeof obj !== 'object') continue;
      lib.set(id, _clean(obj, id, ev.created_at));
    }
  }

  /* Paint from the local cache first, then reconcile with the relay: a playlist made on this device
   * is in Store already, so the list is never blank while the network thinks. */
  async function load(force){
    if(!_boot() || !ME()) return [];
    if(_lib && !force) return all();
    if(_loading) return _loading;
    _loading = (async () => {
      const lib = new Map();
      try{ await _absorb(lib, Store().query([FILTER()]) || []); }catch(_){}
      _lib = lib; _changed();
      try{ await _absorb(lib, await Relay().query([FILTER()]) || []); }catch(_){}
      _changed(); watch();
      flush().catch(()=>{});     // (7) anything queued from a previous session goes now
      return all();
    })();
    try{ return await _loading; } finally { _loading = null; }
  }

  /* Live, so a playlist made on the laptop appears on the phone without a reload. `since` only — the
   * full filter would make the relay replay every playlist as the opening batch, decrypting each a
   * second time straight after load() has just done them all. */
  function watch(){
    if(_sub || !Relay().subscribe || !ME()) return;
    try{
      const f = Object.assign(FILTER(), { since: now() - 120 });
      _sub = Relay().subscribe([f], { live:true, onEvent: async (ev) => {
        if(!_lib) return;
        const before = _stamp();
        await _absorb(_lib, [ev]);
        if(_stamp() !== before) _changed();
      }});
    }catch(_){ _sub = null; }
  }
  function unwatch(){ if(_sub){ try{ Relay().close(_sub); }catch(_){} _sub = null; } }
  function _stamp(){
    if(!_lib) return '';
    let s = _lib.size + ':';
    for(const pl of _lib.values()) s += pl.id + '@' + pl._at + (pl._dead?'x':'') + ',';
    return s;
  }

  function all(){
    if(!_lib) return [];
    return [..._lib.values()].filter(p=>!_isDead(p)).sort((a,b)=>(b.updated||0)-(a.updated||0));
  }
  function get(id){ const p = _lib && _lib.get(id); return (p && !_isDead(p)) ? p : null; }
  function count(){ return all().length; }
  function playlistsWith(sha){
    const k = String(sha||'').toLowerCase();
    return k ? all().filter(pl => pl.tracks.includes(k)) : [];
  }

  // ---------------------------------------------------------------- write

  async function _save(pl){
    if(!_boot() || !ME()) return { ok:false };
    pl.updated = now();
    if(_tooBig(pl)){
      // (5) Refused BEFORE the encrypt, with a sentence. Left to NIP-44 this throws at the last step
      // of the save — after the track has been added in memory and drawn — and says nothing.
      toast('“' + pl.name + '” is full — a playlist holds about a thousand tracks');
      return { ok:false, full:true };
    }
    let ct;
    try{ ct = await PC.nip44enc(ME().pubkey, JSON.stringify(_body(pl))); }
    catch(e){ toast('couldn’t encrypt that playlist'); return { ok:false }; }
    // noQueue: publish()'s Outbox refuses replaceable kinds on purpose (blind replay is what caused
    // the follows wipe). Saying so explicitly keeps this from depending on that behaviour.
    const r = await publish(KIND, ct, [['d', D_PL + pl.id], ['l', L_TAG]], { quiet:true, noQueue:true });
    const ev = r && r.ev;
    if(ev){ pl._at = ev.created_at; if(_lib) _lib.set(pl.id, pl); }
    if(r && r.ok){ _changed(); return { ok:true, queued:false }; }
    if(ev){
      /* publish() rolls its optimistic Store save back when the relay refuses. Put it back: offline,
       * the local cache IS the playlist, and the rollback would make an edit vanish as it is made. */
      try{ Store().saveEvent(ev); }catch(_){}
      queue(ev);
      _changed();
      return { ok:false, queued:true };
    }
    return { ok:false, queued:false };
  }
  const _stuck = r => !r || (!r.ok && !r.queued);   // neither published nor safely held

  async function create(name, tracks){
    if(!_boot() || !ME()) return null;
    if(!_lib) await load();
    const pl = _clean({ name: name || 'New playlist', tracks: tracks || [], created: now() }, _id(), 0);
    if(_lib) _lib.set(pl.id, pl);
    const r = await _save(pl);
    // (6) A playlist that was never published must not be handed back as if it exists — the caller
    // would navigate into something that disappears on the next reload with no explanation.
    if(_stuck(r)){ if(_lib) _lib.delete(pl.id); _changed(); return null; }
    return pl;
  }

  async function rename(id, name){
    const pl = get(id); if(!pl) return false;
    const n = String(name||'').trim(); if(!n) return false;
    const was = pl.name;
    pl.name = n.slice(0,120);
    const r = await _save(pl);
    if(_stuck(r)){ pl.name = was; _changed(); return false; }
    return true;
  }

  /* Add tracks, keeping the order given and ignoring ones already present. Returns how many were
   * actually added, so the caller can say "3 added, 2 already there" rather than claiming five. */
  async function add(id, shas){
    const pl = get(id); if(!pl) return 0;
    const have = new Set(pl.tracks);
    const fresh = [];
    for(const s of (Array.isArray(shas) ? shas : [shas])){
      const k = String(s||'').toLowerCase();
      if(/^[0-9a-f]{64}$/.test(k) && !have.has(k)){ have.add(k); fresh.push(k); }
    }
    if(!fresh.length) return 0;
    const before = pl.tracks.slice();
    pl.tracks = before.concat(fresh);
    const r = await _save(pl);
    if(_stuck(r)){ pl.tracks = before; _changed(); return 0; }
    return fresh.length;
  }

  /* Swap one sha for another, IN PLACE, across every playlist that holds it.
   *
   * Needed because a playlist stores content hashes and Blossom is content-addressed: replacing a
   * track with a different file gives it a different sha, so without this every playlist that held
   * it would silently point at a blob that is about to be deleted — the track would vanish from the
   * playlist while still being in the library, which is the worst of both.
   *
   * In place, not remove-then-add: a playlist's ORDER is its content, and appending the replacement
   * to the end is a different playlist. Returns how many were changed so the caller can say so.
   */
  async function replaceTrack(oldSha, newSha){
    const from = String(oldSha||'').toLowerCase(), to = String(newSha||'').toLowerCase();
    if(!/^[0-9a-f]{64}$/.test(from) || !/^[0-9a-f]{64}$/.test(to) || from === to) return 0;
    let n = 0;
    for(const pl of all()){
      const i = pl.tracks.indexOf(from);
      if(i < 0) continue;
      const before = pl.tracks.slice();
      // Already holds the replacement (the same original picked twice): drop the old entry rather
      // than creating a duplicate, which _clean would strip on the next read anyway.
      pl.tracks = before.includes(to)
        ? before.filter(t => t !== from)
        : before.map(t => t === from ? to : t);
      const r = await _save(pl);
      if(_stuck(r)) pl.tracks = before; else n++;
    }
    if(n) _changed();
    return n;
  }

  async function removeTrack(id, sha){
    const pl = get(id); if(!pl) return false;
    const k = String(sha||'').toLowerCase();
    const before = pl.tracks.slice();
    pl.tracks = before.filter(s => s !== k);
    if(pl.tracks.length === before.length) return false;
    const r = await _save(pl);
    if(_stuck(r)){ pl.tracks = before; _changed(); return false; }
    return true;
  }

  /* Move the track at `from` so it sits at `to`. Pure index arithmetic on purpose — it is the one
   * part of reordering that can be wrong in a way nobody notices until the order is saved. */
  function reorder(tracks, from, to){
    const out = (tracks||[]).slice();
    if(!out.length) return out;
    from = Math.max(0, Math.min(out.length-1, from|0));
    to   = Math.max(0, Math.min(out.length-1, to|0));
    if(from === to) return out;
    const [t] = out.splice(from,1);
    out.splice(to,0,t);
    return out;
  }
  async function move(id, from, to){
    const pl = get(id); if(!pl) return false;
    const before = pl.tracks.slice();
    const next = reorder(before, from, to);
    if(next.join() === before.join()) return false;
    pl.tracks = next;
    const r = await _save(pl);
    if(_stuck(r)){ pl.tracks = before; _changed(); return false; }
    return true;
  }

  async function remove(id){
    if(!get(id) || !_boot() || !ME()) return false;
    /* (4) THE kind-5 GOES FIRST. The relay deletes every event for the address with `created_at <=`
     * the kind-5's, so publishing it after the tombstone deletes the tombstone with the rest — and a
     * device that was offline then finds NOTHING for that d-tag, keeps its cached copy, and
     * republishes it on the next edit. Ordered this way the tombstone is the newest event and
     * survives, which is what actually makes the delete travel. */
    try{ await publish(5, '', [['a', KIND+':'+ME().pubkey+':'+D_PL+id]], { quiet:true, noQueue:true }); }catch(_){}
    const r = await publish(KIND, '', [['d', D_PL + id], ['l', L_TAG]], { quiet:true, noQueue:true });
    if(r && r.ev && !r.ok){ try{ Store().saveEvent(r.ev); }catch(_){} queue(r.ev); }
    if(_lib) _lib.set(id, { id, _at: (r && r.ev && r.ev.created_at) || now(), _dead:true });
    _changed();
    return true;
  }

  // ---------------------------------------------------------------- offline queue

  /* The narrow, playlist-only Outbox, safe for the reasons the generic one is not: what is queued is
   * the SIGNED, ENCRYPTED event (nothing extra on disk); a playlist is a self-contained document
   * rather than a membership list, so a replay can only restore THAT playlist and never erase
   * entries this client never saw; and a queued edit that lost a race to a newer one on another
   * device is DISCARDED on flush rather than published. */
  function pending(){
    try{ return JSON.parse(localStorage.getItem(PENDING_KEY)||'[]') || []; }catch(_){ return []; }
  }
  function setPending(list){
    try{ localStorage.setItem(PENDING_KEY, JSON.stringify(list)); }
    catch(_){ if(toast) toast('this device is out of storage — that playlist is saved here but may not sync'); }
  }
  const _dOf = ev => ((ev.tags||[]).find(t=>t[0]==='d')||[])[1] || '';
  function queue(ev){
    const list = pending().filter(e => _dOf(e) !== _dOf(ev));   // one entry per playlist, newest wins
    list.push(ev);
    if(list.length > 100){
      // Dropping the OLDEST silently would lose a write. There is one entry per playlist, so a
      // hundred of them is already far past anything real — say so rather than quietly forget.
      if(toast) toast('too many unsent playlist changes on this device — the oldest are being dropped');
    }
    setPending(list.slice(-100));
  }
  async function flush(){
    if(!_boot() || !ME()) return;
    const list = pending();
    if(!list.length) return;
    const keep = [];
    for(const ev of list){
      const have = _lib && _lib.get(_dOf(ev).slice(D_PL.length));
      // Already holding a NEWER revision — publishing this would resurrect a replaced edit.
      if(have && have._at > (ev.created_at||0)) continue;
      let ok = false;
      // (1) Relay.publish resolves an OBJECT even on failure, so the result has to be inspected.
      // `!!(await publish(ev))` was true for a timeout, and the queue cleared itself on a send that
      // never happened.
      try{ const r = await Relay().publish(ev); ok = !!(r && r.ok); }catch(_){ ok = false; }
      if(!ok) keep.push(ev);
    }
    setPending(keep);
  }

  window.PCPlaylists = {
    load, all, get, count, playlistsWith,
    create, rename, add, removeTrack, replaceTrack, move, remove,
    flush, unwatch, reorder,          // reorder is pure — exported for tests
    onChange(fn){ _watchers.add(fn); return () => _watchers.delete(fn); },
    _BODY_MAX: BODY_MAX, _D: D_PL, _L: L_TAG,
  };
})();
