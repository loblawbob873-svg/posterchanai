/* Notes — private, encrypted, offline-first note taking.
 *
 * STORAGE. One kind-30078 event PER NOTE, `d = pcai:note:<id>`, whose content is the note JSON
 * NIP-44-encrypted to the user's OWN pubkey (like budget.js, and unlike the rest of the app's docs,
 * which use a server-held storage key). Nobody but this user can read a note — not the operator,
 * not the relay, not a database dump — which also means the server can't offer notes over Telegram
 * or to the AI, and that is the trade being made deliberately.
 *
 * Why per-note and not one document like Budget: a document is a read-modify-write of the whole
 * dataset on every keystroke-save, so two devices editing different notes lose one of them, and a
 * Joplin library of a few thousand notes does not fit in one event at all. Per-note, each note is
 * its own conflict domain, a sync is a delta, and the blast radius of any failure is one note.
 * The cost is that the `d` tag and the event timestamps are public metadata: an observer of the
 * relay learns how many notes you have and when you write them, never what is in them. Note ids are
 * therefore opaque — never a slug of the title.
 *
 * Folders are the same shape (`pcai:notefolder:<id>`), so there is no index document anywhere. An
 * index would be a second source of truth that can be wiped by one empty read — the failure that
 * has already cost this app a follows list and a drive's file index. The list is derived by
 * querying, every time.
 *
 * OFFLINE. Reads come from the local relay cache first (Store), which persists to IndexedDB, so
 * every note you have synced is readable with no network at all. Writes are the harder half: the
 * app's Outbox deliberately REFUSES replaceable kinds (re-sending a stale replaceable event is how
 * the follows list got wiped), so Notes carries its own queue with rules the generic one can't
 * assume — see Pending below. Nothing here ever replays blindly.
 *
 * ATTACHMENTS. Encrypted with the client's master key and stored on Blossom by
 * PC.uploadEncFile(file, 'Notes') — the same path as encrypted music, so there is one encrypted
 * drive and not two. The note holds only the sha256; the bytes are useless without the key. Those
 * uploads set `keep`, which exempts them from the server's age sweep forever (see
 * blossom_service._cleanup_once) — they are ciphertext whose only copy is there.
 */
(function(){
  'use strict';

  const KIND = 30078;
  const D_NOTE = 'pcai:note:';
  const D_FOLDER = 'pcai:notefolder:';
  // Single-letter tag → the relay indexes it, so the whole library is ONE filtered subscription
  // rather than "fetch every 30078 this user has ever written and sift out the chats".
  const L_TAG = 'pcai-notes';
  const PENDING_KEY = 'pcaiNotesPending';
  const FOLDER_ALL = '__all', FOLDER_NONE = '__none';

  let PC=null, $, $$, enc, toast, uiConfirm, uiPrompt, modal, closeModal, publish, mdToHtml;
  const Relay = () => window.Relay;
  const Store = () => window.Store;
  const ME = () => PC.ME;

  let _booted=false;
  function boot(){
    if(_booted) return;
    PC = window.__PC;
    if(!PC) return setTimeout(boot, 50);
    _booted = true;
    ({ $, $$, enc, toast, uiConfirm, uiPrompt, modal, closeModal, publish, mdToHtml } = PC);
    window.PCNotes = {
      render(){ if(document.querySelector('.nt-wrap')) return; render(); },
      unmount(){ _sel=null; unwatch(); },
      // Called on reconnect — flush anything written while offline.
      flush: flushPending,
      // For the offline bar / nav badge: how many writes are still queued.
      pendingCount: () => pending().length,
    };
    // A reconnect is the moment to drain the queue. 'online' fires on the window; the relay's own
    // reopen is the more reliable signal on mobile (a phone can be "online" with no route), so both.
    window.addEventListener('online', () => { flushPending(); });
    setInterval(() => { if(navigator.onLine && pending().length) flushPending(); }, 45000);
  }

  // ---------------------------------------------------------------- model

  const uid = () => {
    const b = new Uint8Array(16); crypto.getRandomValues(b);
    return Array.from(b).map(x=>x.toString(16).padStart(2,'0')).join('');
  };
  const now = () => Math.floor(Date.now()/1000);

  function blankNote(path){
    // `path` is what the sidebar selects; a note stores a folder ID. A synthesised parent (a path
    // with no record of its own) gets one created on demand rather than silently filing the note
    // at the top level.
    let id = '';
    if(path && path !== FOLDER_ALL && path !== FOLDER_NONE && _lib){
      const rec = Array.from(_lib.folders.values()).find(f => f.name === path);
      id = rec ? rec.id : '';
    }
    return { v:1, id:uid(), title:'', body:'', folder:id,
             tags:[], created:now(), updated:now(), res:[] };
  }

  // In-memory library, rebuilt from the relay/cache. `notes` and `folders` are keyed by id.
  let _lib = null;          // {notes:Map, folders:Map}
  let _loading = null;
  let _sel = null;          // id of the open note
  let _filter = { folder:FOLDER_ALL, q:'', tag:'' };

  const FILTER = () => ({ authors:[ME().pubkey], kinds:[KIND], '#l':[L_TAG], limit:5000 });

  /* CACHE FIRST, and resolve the moment the cache is in — the network refresh runs behind it.
   * This used to await the relay before it returned, so opening Notes sat on a spinner for as long
   * as the round trip took, and on a dead relay for as long as the timeout. Every other list in
   * this app paints from cache instantly and refreshes underneath (see _cacheFirstList in app.js);
   * a notebook, which is entirely the user's own already-synced data, has even less excuse to wait.
   *
   * The network answer is folded IN, never OVER: a relay that returns nothing — unreachable,
   * throttled, or merely slow — must leave the local library alone. That asymmetry is the anti-wipe
   * rule this codebase keeps relearning. */
  async function load(force){
    if(_lib && !force) return _lib;
    if(!_loading) _loading = _loadCache().finally(()=>{ _loading=null; });
    return _loading;
  }

  async function _loadCache(){
    const lib = { notes:new Map(), folders:new Map() };
    let cached = [];
    try{ cached = Store().query([FILTER()]) || []; }catch(_){ cached = []; }
    // Progressive: repaint every 60 decrypts so a big library fills in visibly instead of holding
    // a spinner. Decryption is per note and there is no way around it — the title lives inside the
    // ciphertext — so with a thousand notes the difference is a blank screen versus a filling one.
    await _absorb(lib, cached, n => { if(n % 60 === 0){ _lib = lib; _paint(); } });
    _lib = lib;
    return _lib;
  }

  /* The background half. Never blocks a paint; re-renders only if something actually changed and
   * the user isn't mid-edit. */
  let _refreshing = false;
  async function refresh(){
    if(_refreshing || !_lib) return;
    _refreshing = true;
    try{
      const live = await Relay().query([FILTER()]);
      if(live && live.length){
        const before = _stamp();
        await _absorb(_lib, live);
        if(_stamp() !== before && PC.VIEW === 'notes' && !_dirty) _paint();
      }
    }catch(_){ /* offline: the cache stands on its own */ }
    finally{ _refreshing = false; }
  }
  // Cheap change detector: count + newest event stamp. Comparing whole documents would mean
  // re-serialising every note on every refresh.
  function _stamp(){
    let n = 0, top = 0;
    for(const o of _lib.notes.values()){ n++; if(o._at > top) top = o._at; }
    for(const o of _lib.folders.values()){ n++; if(o._at > top) top = o._at; }
    return n + ':' + top;
  }

  /* A LIVE subscription, so a note written on another device shows up here without a reload —
   * which is what "syncs" has to mean when the same library is open on a laptop and a phone. */
  let _sub = null;
  function watch(){
    if(_sub || !Relay().subscribe) return;
    try{
      // `since`, NOT the plain library filter. Subscribing with the full filter makes the relay
      // replay the entire library as the subscription's initial batch — a few thousand events, each
      // one decrypted a second time (refresh() has just done them all) and each one triggering a
      // change check. The live sub only wants the TAIL; the backfill is refresh()'s job. The 120s
      // slack covers clock skew between this device and the relay.
      const f = Object.assign(FILTER(), { since: now() - 120 });
      delete f.limit;
      _sub = Relay().subscribe([f], { live:true, onEvent: async (ev) => {
        if(!_lib) return;
        const before = _stamp();
        await _absorb(_lib, [ev]);
        if(_stamp() !== before && PC.VIEW === 'notes' && !_dirty) _paint();
      }});
    }catch(_){ _sub = null; }
  }
  function unwatch(){ if(_sub){ try{ Relay().close(_sub); }catch(_){ } _sub = null; } }

  /* Decrypt events into the library. Newest wins per id — events for one `d` can arrive from
   * several relays with different created_at, and an older copy must never overwrite a newer one.
   * Newest FIRST, so on a big library the notes someone actually wants to see decrypt first. */
  async function _absorb(lib, evs, onProgress){
    let seen = 0;
    evs = (evs || []).slice().sort((a,b) => (b.created_at||0) - (a.created_at||0));
    for(const ev of evs){
      if(onProgress) onProgress(++seen);
      const d = (ev.tags||[]).find(t=>t[0]==='d');
      if(!d || !d[1]) continue;
      const isNote = d[1].startsWith(D_NOTE), isFolder = d[1].startsWith(D_FOLDER);
      if(!isNote && !isFolder) continue;
      const id = d[1].slice((isNote?D_NOTE:D_FOLDER).length);
      const into = isNote ? lib.notes : lib.folders;
      const have = into.get(id);
      if(have && have._at >= ev.created_at) continue;
      // A deletion is an empty content — a tombstone. Addressable events can't be un-published, and
      // a relay that never honours the kind-5 keeps serving the last real version forever, so the
      // tombstone IS the delete as far as any client is concerned.
      if(!ev.content){ into.delete(id); continue; }
      let obj = null;
      try{ obj = JSON.parse(await PC.nip44dec(ME().pubkey, ev.content)); }
      catch(_){ continue; }   // not ours / not decryptable with this key — skip, never drop
      if(!obj || typeof obj !== 'object') continue;
      obj.id = id; obj._at = ev.created_at;
      if(!Array.isArray(obj.tags)) obj.tags = [];
      if(!Array.isArray(obj.res)) obj.res = [];
      into.set(id, obj);
    }
  }

  // ---------------------------------------------------------------- writes

  /* Pending queue. The app's Outbox refuses replaceable kinds on purpose, so this is the narrow
   * version for notes only, and it is safe for exactly the reasons the generic one is not:
   *
   *  - what's queued is the SIGNED, ENCRYPTED event, so it leaks nothing extra on disk;
   *  - a note is a self-contained document, not a membership list, so re-sending one can only ever
   *    restore that note — it cannot silently erase items the client didn't know about (which is
   *    what a replayed follows/mute list does);
   *  - the relay resolves addressable events by created_at, so a queued edit that lost a race to a
   *    newer edit on another device is DISCARDED on flush rather than published. `_at` is checked
   *    against the library before every resend.
   */
  function pending(){
    try{ return JSON.parse(localStorage.getItem(PENDING_KEY)||'[]') || []; }catch(_){ return []; }
  }
  function setPending(list){
    // A full localStorage (quota is per-origin and small) must not lose the note the user just
    // typed silently — the write itself already succeeded into Store, so say what was lost.
    try{ localStorage.setItem(PENDING_KEY, JSON.stringify(list)); }
    catch(_){ toast('this device is out of storage — that note is saved here but may not sync'); }
  }
  function queue(ev){
    const list = pending().filter(e => _dOf(e) !== _dOf(ev));   // one entry per note: newest wins
    list.push(ev);
    setPending(list);
  }
  const _dOf = ev => ((ev.tags||[]).find(t=>t[0]==='d')||[])[1] || '';

  async function flushPending(){
    const list = pending();
    if(!list.length) return 0;
    const left = [];
    let sent = 0;
    for(const ev of list){
      const d = _dOf(ev);
      const id = d.startsWith(D_NOTE) ? d.slice(D_NOTE.length) : d.slice(D_FOLDER.length);
      const cur = (_lib && (d.startsWith(D_NOTE) ? _lib.notes : _lib.folders).get(id)) || null;
      // Superseded while we were offline (another device wrote a newer version, or this device did
      // and that write landed). Dropping it is the correct resolution: publishing would resurrect
      // an older body over a newer one.
      if(cur && cur._at > ev.created_at) continue;
      let ok = false;
      try{ const r = await Relay().publish(ev); ok = !!(r && r.ok); }catch(_){ ok = false; }
      if(ok){ sent++; try{ Store().saveEvent(ev); }catch(_){ } }
      else left.push(ev);
    }
    setPending(left);
    if(sent){
      toast(sent===1 ? 'a note you wrote offline has synced' : sent+' notes you wrote offline have synced');
      // NOT while something is half-typed: render() rebuilds the whole pane from innerHTML, so a
      // flush landing mid-sentence would replace the textarea with the last SAVED body and drop
      // whatever is inside the current debounce window.
      if(PC.VIEW==='notes' && !_dirty) _paint();
    }
    return sent;
  }

  /* Write one note/folder. Returns {ok, queued}. NEVER throws on a dead relay: offline, the note is
   * saved locally and queued, and the UI says so — losing what someone just typed because the wifi
   * dropped is the one failure a notes app may not have. */
  /* A note bigger than one relay message cannot be published as one event. The relay caps a message
   * at 512 KB and NIP-44 + base64 expands the plaintext by about 1.37x, so anything past roughly
   * 380 KB of JSON is unsendable — and a real notebook has such notes (this was found on an import
   * carrying an 8.5 MB one). Those notes failed to save with nothing to explain why.
   *
   * So an oversized BODY is offloaded to an encrypted Blossom blob and the event keeps a pointer,
   * exactly the way FilesIdx spills its index past the inline limit. The event still carries the
   * title, folder, tags and a short snippet, so the list, search-by-title and every folder count
   * work without fetching anything; the body is pulled when the note is actually opened.
   * Conservative threshold: the cost of guessing high is an unsendable event, the cost of guessing
   * low is one extra blob. */
  const BODY_INLINE_MAX = 250000;

  async function save(obj, kind){
    const isFolder = kind === 'folder';
    obj.updated = now();
    const d = (isFolder ? D_FOLDER : D_NOTE) + obj.id;
    const body = Object.assign({}, obj); delete body._at;
    if(!isFolder && body.body && JSON.stringify(body).length > BODY_INLINE_MAX){
      const bytes = new TextEncoder().encode(body.body);
      const f = new File([bytes], (obj.title || 'note') + '.md', { type:'text/markdown' });
      const sha = await PC.uploadEncFile(f, 'Notes');
      obj.bodyRef = body.bodyRef = sha;
      obj.snippet = body.snippet = body.body.slice(0, 400);
      obj.bodyBytes = body.bodyBytes = bytes.length;
      body.body = '';        // the in-memory copy KEEPS its text; only the event sheds it
    } else if(body.bodyRef && body.body){
      // Edited back under the limit — inline it again and drop the pointer, so a note doesn't keep
      // a stale blob reference that would win over its own text on the next read.
      delete body.bodyRef; delete obj.bodyRef;
      delete body.snippet; delete obj.snippet;
    }
    const ct = await PC.nip44enc(ME().pubkey, JSON.stringify(body));
    // opts.noQueue: publish()'s own Outbox would refuse a 30078 anyway, but saying so explicitly
    // keeps this from depending on that. {quiet} because we report the outcome ourselves.
    const r = await publish(KIND, ct, [['d', d], ['l', L_TAG]], {quiet:true, noQueue:true});
    const ev = r && r.ev;
    if(ev){
      obj._at = ev.created_at;
      (isFolder ? _lib.folders : _lib.notes).set(obj.id, obj);
    }
    if(r && r.ok) return { ok:true, queued:false };
    if(ev){
      // publish() rolled the optimistic Store save back when the relay refused it. Put it back:
      // offline, the local cache IS the note, and a rollback would make it vanish as you typed.
      try{ Store().saveEvent(ev); }catch(_){ }
      queue(ev);
      return { ok:false, queued:true };
    }
    return { ok:false, queued:false };
  }

  async function remove(obj, kind){
    const isFolder = kind === 'folder';
    const d = (isFolder ? D_FOLDER : D_NOTE) + obj.id;
    // Empty content = tombstone (see _absorb), plus a NIP-09 delete for relays that honour it. The
    // tombstone is what actually makes it disappear everywhere; kind 5 is the polite half.
    const r = await publish(KIND, '', [['d', d], ['l', L_TAG]], {quiet:true, noQueue:true});
    if(r && r.ev && !r.ok){ try{ Store().saveEvent(r.ev); }catch(_){ } queue(r.ev); }
    (isFolder ? _lib.folders : _lib.notes).delete(obj.id);
    try{ await publish(5, '', [['a', KIND+':'+ME().pubkey+':'+d]], {quiet:true, noQueue:true}); }catch(_){ }
    return r;
  }

  // ---------------------------------------------------------------- view

  const _fmt = ts => { if(!ts) return ''; const d=new Date(ts*1000);
    return d.toLocaleDateString(undefined,{year:'numeric',month:'short',day:'numeric'}); };

  /* Joplin nests notebooks, and the import brings the full path across as the folder's name
   * ("Family/Tax Returns"). Rendered as a flat list that reads as unrelated top-level folders with
   * slashes in them, which for a real library is most of the sidebar. So the paths are parsed back
   * into a TREE, with collapsible parents — including intermediate levels that have no folder
   * record of their own (a note can sit in "Tech/VPN and SSH" when nothing was ever filed directly
   * in "Tech"). Selection is by PATH, not by id, so a synthesised parent is selectable too. */
  function folderName(path){
    if(path === FOLDER_ALL) return 'All notes';
    if(path === FOLDER_NONE || !path) return 'Unfiled';
    return path;
  }
  // path -> the folder record that owns it (a synthesised parent has none)
  function _folderByPath(){
    const m = new Map();
    if(_lib) for(const f of _lib.folders.values()) if(f.name) m.set(f.name, f);
    return m;
  }
  const _pathOf = n => { const f = _lib && _lib.folders.get(n.folder); return f ? f.name : ''; };
  // Notes in this path OR anything beneath it — picking "Family" must not hide "Family/Work".
  const _inPath = (n, path) => { const p = _pathOf(n); return p === path || p.startsWith(path + '/'); };

  function folderTree(){
    const paths = new Set();
    if(_lib) for(const f of _lib.folders.values()){
      if(!f.name) continue;
      const parts = f.name.split('/');
      for(let i = 1; i <= parts.length; i++) paths.add(parts.slice(0, i).join('/'));   // synthesise parents
    }
    const nodes = new Map();
    for(const p of paths){
      const parts = p.split('/');
      nodes.set(p, { path:p, label:parts[parts.length-1], depth:parts.length-1, kids:[] });
    }
    const roots = [];
    for(const n of nodes.values()){
      const parent = n.path.includes('/') ? nodes.get(n.path.slice(0, n.path.lastIndexOf('/'))) : null;
      (parent ? parent.kids : roots).push(n);
    }
    const bylabel = (a,b) => a.label.localeCompare(b.label);
    roots.sort(bylabel); for(const n of nodes.values()) n.kids.sort(bylabel);
    return roots;
  }

  // Collapsed state survives navigation and reloads — re-opening every branch on each visit is the
  // thing that makes a tree annoying rather than useful.
  const COLLAPSE_KEY = 'pcaiNotesCollapsed';
  function collapsed(){
    try{ return new Set(JSON.parse(localStorage.getItem(COLLAPSE_KEY) || '[]')); }catch(_){ return new Set(); }
  }
  function toggleCollapse(path){
    const c = collapsed();
    c.has(path) ? c.delete(path) : c.add(path);
    try{ localStorage.setItem(COLLAPSE_KEY, JSON.stringify(Array.from(c))); }catch(_){ }
  }
  function _renderTree(roots, sel, col){
    const out = [];
    const walk = (n) => {
      const count = Array.from(_lib.notes.values()).filter(x => _inPath(x, n.path)).length;
      const isCol = col.has(n.path);
      out.push(`<div class="nt-frow" style="--d:${n.depth}">
        ${n.kids.length
          ? `<button class="nt-fcaret${isCol?'':' open'}" data-caret="${enc(n.path)}" aria-label="${isCol?'Expand':'Collapse'} ${enc(n.label)}"><svg class="ic" aria-hidden="true"><use href="#i-chevron-right"></use></svg></button>`
          : `<span class="nt-fcaret"></span>`}
        <button class="nt-folder${sel===n.path?' active':''}" data-f="${enc(n.path)}"><span>${enc(n.label)}</span><i>${count}</i></button>
      </div>`);
      if(!isCol) n.kids.forEach(walk);
    };
    roots.forEach(walk);
    return out.join('');
  }

  function visibleNotes(){
    if(!_lib) return [];
    const q = _filter.q.trim().toLowerCase();
    let list = Array.from(_lib.notes.values());
    if(_filter.folder === FOLDER_NONE) list = list.filter(n => !n.folder || !_lib.folders.has(n.folder));
    else if(_filter.folder !== FOLDER_ALL) list = list.filter(n => _inPath(n, _filter.folder));
    if(_filter.tag) list = list.filter(n => (n.tags||[]).includes(_filter.tag));
    if(q) list = list.filter(n => (n.title||'').toLowerCase().includes(q) || (n.body||'').toLowerCase().includes(q));
    return list.sort((a,b) => (b.updated||0) - (a.updated||0));
  }

  function allTags(){
    const c = new Map();
    if(_lib) for(const n of _lib.notes.values()) for(const t of (n.tags||[])) c.set(t, (c.get(t)||0)+1);
    return Array.from(c.entries()).sort((a,b)=> b[1]-a[1] || a[0].localeCompare(b[0]));
  }

  /* Entry point. Paints from cache as soon as it can, then refreshes and subscribes BEHIND the
   * paint — neither is awaited, so opening Notes never waits on a network round trip. */
  async function render(){
    const feed = $('#feed');
    if(!feed) return;
    if(!_lib){
      feed.innerHTML = '<div class="nt-wrap"><div class="spinner"></div></div>';
      try{ await load(); }
      catch(e){ feed.innerHTML = `<div class="nt-wrap"><div class="empty">Couldn’t open your notes: ${enc(e.message||'error')}</div></div>`; return; }
    }
    _paint();
    watch();      // live: a note written on another device appears without a reload
    refresh();    // deliberately NOT awaited
  }

  // Build the DOM from whatever is in _lib right now. Pure render — no I/O, so it is safe to call
  // from a background refresh or a live event.
  function _paint(){
    const feed = $('#feed');
    if(!feed || !_lib) return;
    const notes = visibleNotes();
    const pend = pending().length;
    const folders = Array.from(_lib.folders.values()).sort((a,b)=>(a.name||'').localeCompare(b.name||''));
    const tags = allTags();
    const total = _lib.notes.size;

    feed.innerHTML = `<div class="nt-wrap${_sel?' nt-open':''}">
      <aside class="nt-side">
        <div class="nt-searchwrap">
          <svg class="ic nt-searchic" aria-hidden="true"><use href="#i-search"></use></svg>
          <input class="input nt-search" type="search" placeholder="Search notes…" value="${enc(_filter.q)}" autocomplete="off">
        </div>
        <button class="btn btn-cyan nt-new"><svg class="ic b-ic" aria-hidden="true"><use href="#i-pen"></use></svg>New note</button>
        <div class="nt-sec">
          <span>Folders</span>
          <button class="nt-icobtn nt-addfolder" title="New folder" aria-label="New folder"><svg class="ic" aria-hidden="true"><use href="#i-plus"></use></svg></button>
        </div>
        <nav class="nt-folders">
          <div class="nt-frow" style="--d:0"><span class="nt-fcaret"></span>
            <button class="nt-folder${_filter.folder===FOLDER_ALL?' active':''}" data-f="${FOLDER_ALL}"><span>All notes</span><i>${total}</i></button></div>
          ${_renderTree(folderTree(), _filter.folder, collapsed())}
          <div class="nt-frow" style="--d:0"><span class="nt-fcaret"></span>
            <button class="nt-folder${_filter.folder===FOLDER_NONE?' active':''}" data-f="${FOLDER_NONE}"><span>Unfiled</span><i>${Array.from(_lib.notes.values()).filter(n=>!n.folder||!_lib.folders.has(n.folder)).length}</i></button></div>
        </nav>
        ${tags.length?`<div class="nt-sec"><span>Tags</span></div>
          <div class="nt-tags">${tags.slice(0,30).map(([t,c])=>`<button class="nt-tag${_filter.tag===t?' active':''}" data-t="${enc(t)}">${enc(t)} <i>${c}</i></button>`).join('')}</div>`:''}
        <div class="nt-side-foot">
          ${pend?`<div class="nt-pending small">${pend} waiting to sync</div>`:''}
          <div class="nt-foot-actions">
            <button class="nt-link nt-import" title="Import a Joplin export or a Notes backup"><svg class="ic" aria-hidden="true"><use href="#i-download"></use></svg>Import</button>
            <button class="nt-link nt-export" title="Save a backup archive of every note"><svg class="ic" aria-hidden="true"><use href="#i-cloud"></use></svg>Backup</button>
          </div>
        </div>
      </aside>
      <section class="nt-list" aria-label="Notes">
        <div class="nt-list-head">
          <b>${enc(folderName(_filter.folder))}</b>
          <span class="nt-count">${notes.length}</span>
        </div>
        ${notes.length ? notes.map(n=>`
          <button class="nt-item${_sel===n.id?' active':''}" data-id="${enc(n.id)}">
            <b>${enc(n.title || 'Untitled')}</b>
            <span class="nt-snip muted small">${enc(((n.body||n.snippet||'')).replace(/[#*`>\-\n]+/g,' ').trim().slice(0,90))}</span>
            <span class="nt-meta muted small">${_fmt(n.updated)}${n.res&&n.res.length?` · ${n.res.length} 📎`:''}</span>
          </button>`).join('')
        : `<div class="empty">${total ? 'Nothing matches that.' : 'No notes yet. Write one, or import your Joplin export.'}</div>`}
      </section>
      <section class="nt-editor" aria-label="Editor"></section>
    </div>`;

    $('.nt-new', feed).onclick = () => openNote(blankNote(_filter.folder), true);
    $('.nt-import', feed).onclick = openImport;
    $('.nt-export', feed).onclick = exportBackup;
    $('.nt-addfolder', feed).onclick = addFolder;
    $$('.nt-fcaret[data-caret]', feed).forEach(b => b.onclick = (e) => {
      e.stopPropagation(); toggleCollapse(b.dataset.caret); _paint();
    });
    const s = $('.nt-search', feed);
    let t=null;
    s.oninput = () => { clearTimeout(t); t=setTimeout(()=>{ _filter.q = s.value; renderList(); }, 160); };
    $$('.nt-folder[data-f]', feed).forEach(b => b.onclick = () => { _filter.folder = b.dataset.f; _filter.tag=''; render(); });
    $$('.nt-tag', feed).forEach(b => b.onclick = () => { _filter.tag = (_filter.tag===b.dataset.t?'':b.dataset.t); render(); });
    $$('.nt-item', feed).forEach(b => b.onclick = () => { const n=_lib.notes.get(b.dataset.id); if(n) openNote(n, false); });
    if(_sel && _lib.notes.has(_sel)) openNote(_lib.notes.get(_sel), false);
  }

  // Re-render just the list (search typing) so the editor and its caret survive.
  function renderList(){
    const wrap = document.querySelector('.nt-wrap'); if(!wrap) return;
    const keep = _sel;
    render().then(()=>{ if(keep){ const el=document.querySelector('.nt-search'); if(el){ el.focus(); el.setSelectionRange(el.value.length, el.value.length); } } });
  }

  async function addFolder(){
    // Seeded with the selected path, so "New folder" inside Family offers "Family/" and a subfolder
    // is one word of typing rather than retyping the whole path.
    const base = (_filter.folder && _filter.folder !== FOLDER_ALL && _filter.folder !== FOLDER_NONE)
      ? _filter.folder + '/' : '';
    const name = await uiPrompt('Name this folder — use / for a subfolder',
                                {ok:'Create', value:base, placeholder:'e.g. Work/Clients'});
    if(!name || !name.trim()) return;
    const clean = name.trim().replace(/^\/+|\/+$/g, '').replace(/\/{2,}/g, '/').slice(0,120);
    if(!clean) return;
    if(_lib && Array.from(_lib.folders.values()).some(f => f.name === clean)){
      toast('you already have a folder called that'); return;
    }
    const f = { v:1, id:uid(), name:clean, created:now(), updated:now() };
    _lib.folders.set(f.id, f);
    const r = await save(f, 'folder');
    if(r.queued) toast('folder saved — it will sync when you’re back online');
    render();
  }

  /* The editor. Saves are debounced and LAST-WRITE-WINS on this device; every save is a whole-note
   * publish, so there is no partial state to reconcile. */
  let _saveT = null, _dirty = false, _pendingCommit = null;
  /* Run any debounced save that is still waiting, NOW. Called before the editor is rebuilt and
   * before leaving it: `commit` closes over the note it belongs to, so letting it fire after the
   * pane has been replaced is how one note's edit lands on another (see openNote). */
  function flushEdit(){
    if(!(_dirty && _pendingCommit)) return;
    const fn = _pendingCommit;
    clearTimeout(_saveT); _pendingCommit = null;
    fn();
  }
  function openNote(n, isNew){
    flushEdit();          // whatever was half-saved belongs to the note we are leaving
    _sel = n.id;
    const host = document.querySelector('.nt-editor');
    if(!host) return;
    /* READ FIRST. Opening straight into the raw markdown source meant an imported note showed
     * `![](pcres:9f3b…)` where its picture should be — the attachments were encrypted, uploaded and
     * correctly linked, and none of that was visible. A note with a body opens RENDERED, with its
     * images decrypted and shown; the pen switches to editing. A new or empty note opens in the
     * editor, because there is nothing to read. */
    const readFirst = !isNew && !!(n.body || '').trim();
    document.querySelector('.nt-wrap').classList.add('nt-open');
    const folders = Array.from(_lib.folders.values()).sort((a,b)=>(a.name||'').localeCompare(b.name||''));
    host.innerHTML = `
      <div class="nt-ed-head">
        <button class="nt-back" aria-label="Back to the list"><svg class="ic b-ic" aria-hidden="true"><use href="#i-chevron-left"></use></svg></button>
        <input class="input nt-title" placeholder="Title" value="${enc(n.title||'')}" maxlength="200">
        <span class="nt-state muted small"></span>
        <button class="btn nt-ico nt-preview" title="Edit" aria-label="Edit"><svg class="ic b-ic" aria-hidden="true"><use href="#i-pen"></use></svg></button>
        <button class="btn btn-red nt-ico nt-del" title="Delete note" aria-label="Delete note"><svg class="ic b-ic" aria-hidden="true"><use href="#i-trash"></use></svg></button>
      </div>
      <div class="nt-ed-bar">
        <select class="input nt-folder-sel">
          <option value="">Unfiled</option>
          ${folders.map(f=>`<option value="${enc(f.id)}"${n.folder===f.id?' selected':''}>${enc(f.name||'Untitled')}</option>`).join('')}
        </select>
        <input class="input nt-tagin" placeholder="tags, comma separated" value="${enc((n.tags||[]).join(', '))}">
        <button class="btn nt-attach" title="Attach a file (encrypted)">📎</button>
      </div>
      <textarea class="nt-body${readFirst?' hidden':''}" placeholder="Write…  (markdown)">${enc(n.body||'')}</textarea>
      <div class="nt-render markdown${readFirst?'':' hidden'}"></div>
      <div class="nt-res"></div>`;

    // CAPTURE every field, and never re-query them inside commit(). `host` is the ONE persistent
    // .nt-editor element whose innerHTML is replaced per note, so a debounced save that looks its
    // inputs up when it fires reads whichever note is on screen THEN — the title and body would be
    // note A's (captured in the closure) while the folder and tags came from note B. A save that is
    // half one note and half another, and looks entirely plausible afterwards.
    const title = $('.nt-title', host), body = $('.nt-body', host), state = $('.nt-state', host),
          folderSel = $('.nt-folder-sel', host), tagIn = $('.nt-tagin', host);
    const mark = txt => { if(state.isConnected) state.textContent = txt; };
    const commit = async () => {
      _pendingCommit = null;
      n.title = title.value.trim();
      n.body = body.value;
      n.folder = folderSel.value || '';
      n.tags = tagIn.value.split(',').map(s=>s.trim()).filter(Boolean).slice(0,30);
      // save() is written not to throw on a dead relay, but it CAN throw on a signer that refuses
      // or disconnects (NIP-46/Amber). Unhandled, that would leave "saving…" on screen forever with
      // the text unsaved and nothing said about it — the worst possible way to lose a note.
      try{
        const r = await save(n, 'note');
        _dirty = false;
        mark(r.ok ? 'saved' : r.queued ? 'saved on this device — will sync' : 'NOT saved');
        if(!r.ok && !r.queued) toast('couldn’t save that note');
      }catch(e){
        mark('NOT saved');
        toast('couldn’t save that note: ' + (e.message || 'signer error'));
      }
      renderSideCounts();
    };
    const touch = () => {
      _dirty = true; mark('saving…');
      clearTimeout(_saveT);
      _pendingCommit = commit;
      _saveT = setTimeout(commit, 700);
    };
    title.oninput = body.oninput = touch;
    folderSel.onchange = touch;
    tagIn.onchange = touch;
    // Leaving the field saves NOW rather than waiting out the debounce — closing the tab or the
    // laptop lid inside that 700ms is exactly when the edit would be lost.
    body.onblur = title.onblur = flushEdit;

    $('.nt-back', host).onclick = () => { flushEdit(); _sel=null; document.querySelector('.nt-wrap').classList.remove('nt-open'); render(); };
    const tog = $('.nt-preview', host);
    const setMode = (read) => {
      const r = $('.nt-render', host);
      if(read){
        r.innerHTML = renderBody(n, body.value);
        r.classList.remove('hidden'); body.classList.add('hidden');
        tog.title = tog.ariaLabel = 'Edit';
        tog.querySelector('use').setAttribute('href', '#i-pen');
        hydrateRes(r, n);
      } else {
        r.classList.add('hidden'); body.classList.remove('hidden');
        tog.title = tog.ariaLabel = 'Read';
        tog.querySelector('use').setAttribute('href', '#i-eye');
        body.focus();
      }
    };
    if(readFirst){ tog.title = tog.ariaLabel = 'Edit'; }
    else { tog.querySelector('use').setAttribute('href', '#i-eye'); tog.title = tog.ariaLabel = 'Read'; }
    tog.onclick = () => setMode(body.classList.contains('hidden') ? false : true);
    $('.nt-del', host).onclick = async () => {
      if(!await uiConfirm(`Delete “${n.title||'Untitled'}”? This can’t be undone.`, {ok:'Delete', danger:true})) return;
      await remove(n, 'note');
      _sel = null;
      render();
    };
    $('.nt-attach', host).onclick = () => attach(n, host);
    renderRes(n, host);
    // An offloaded body is fetched on OPEN, never during the list load — pulling every large note's
    // blob just to render a list of titles would be the same mistake as reading the whole .jex.
    if(n.bodyRef && !n.body){
      const setBusy = t => { const r=$('.nt-render', host); if(r && readFirst) r.innerHTML = `<p class="muted">${t}</p>`;
                             body.placeholder = t; };
      setBusy('loading this note…');
      (async () => {
        try{
          const u = await PC.encFileUrl(n.bodyRef, 'text/markdown');
          n.body = await (await fetch(u)).text();
          if(_sel !== n.id) return;                     // navigated away while it loaded
          body.value = n.body;
          const r = $('.nt-render', host);
          if(r && !r.classList.contains('hidden')){ r.innerHTML = renderBody(n, n.body); hydrateRes(r, n); }
          body.placeholder = 'Write…  (markdown)';
        }catch(e){
          setBusy('couldn’t load this note’s text — it is stored separately because of its size, ' +
                  'and that file could not be read' + (navigator.onLine ? '.' : ' (you are offline).'));
        }
      })();
    }
    if(readFirst){
      const r = $('.nt-render', host);
      r.innerHTML = renderBody(n, n.body);
      hydrateRes(r, n);
    }
    if(isNew) title.focus();
  }

  function renderSideCounts(){
    const el = document.querySelector('.nt-list-head .nt-count');
    if(el) el.textContent = String(visibleNotes().length);
  }

  /* A note body is UNTRUSTED markdown — most of them arrive from an import file. mdToHtml escapes
   * before it renders, which is why notes go through it rather than any innerHTML of their own. */
  function renderBody(n, src){
    return mdToHtml(String(src||''));
  }

  // `pcres:<sha>` links (written by the importer) resolve to a decrypted object URL at view time.
  // They can't be resolved earlier: the URL is a blob: handle that dies with the page.
  async function hydrateRes(root, n){
    const byShaMime = new Map((n.res||[]).map(r => [r.sha, r.mime]));
    const els = Array.from(root.querySelectorAll('img[src^="pcres:"], a[href^="pcres:"]'));
    // In PARALLEL. Each one is a fetch plus a decrypt, and a note full of screenshots resolved
    // serially shows its pictures appearing one at a time over several seconds.
    await Promise.all(els.map(async el => {
      const isImg = el.tagName === 'IMG';
      const sha = (isImg ? el.getAttribute('src') : el.getAttribute('href')).slice(6);
      try{
        // The note carries the mime itself, so a picture still renders when the drive index has no
        // entry for the blob (an import interrupted before its index flush) — an object URL typed
        // application/octet-stream does not display in an <img>.
        const u = await PC.encFileUrl(sha, byShaMime.get(sha));
        if(isImg){ el.src = u; el.loading = 'lazy'; el.onclick = () => window.open(u, '_blank', 'noopener'); }
        else { el.href = u; el.target = '_blank'; el.rel = 'noopener'; }
      }catch(_){
        if(isImg){ const p=document.createElement('span'); p.className='muted small nt-img-miss';
          p.textContent = navigator.onLine ? '[image unavailable]' : '[image not downloaded — open this note once while online]';
          el.replaceWith(p); }
      }
    }));
  }

  function renderRes(n, host){
    const box = $('.nt-res', host); if(!box) return;
    if(!n.res || !n.res.length){ box.innerHTML=''; return; }
    const isImg = r => /^image\//.test(r.mime || '');
    box.innerHTML = `<div class="nt-res-head muted small">Attachments</div>` + n.res.map(r=>
      isImg(r)
        ? `<button class="nt-res-thumb" data-sha="${enc(r.sha)}" data-mime="${enc(r.mime||'')}" title="${enc(r.name||'')}">
             <img alt="${enc(r.name||'attachment')}" loading="lazy"><span class="muted small">${enc(r.name||r.sha.slice(0,8))}</span></button>`
        : `<button class="nt-res-item" data-sha="${enc(r.sha)}" data-mime="${enc(r.mime||'')}">📎 ${enc(r.name||r.sha.slice(0,8))} <i class="muted small">${((r.size||0)/1024).toFixed(0)} KB</i></button>`
    ).join('');
    const open = async (b) => {
      try{ const u = await PC.encFileUrl(b.dataset.sha, b.dataset.mime); window.open(u, '_blank', 'noopener'); }
      catch(e){ toast(navigator.onLine ? 'couldn’t open that attachment' : 'that attachment isn’t downloaded for offline use'); }
    };
    $$('.nt-res-item', box).forEach(b => b.onclick = () => open(b));
    // Thumbnails are decrypted in place: there is no server-side thumbnail for an encrypted blob and
    // there cannot be — the server can't read the picture. Lazy + parallel so a note with thirty
    // attachments doesn't decrypt them one after another.
    $$('.nt-res-thumb', box).forEach(b => {
      b.onclick = () => open(b);
      PC.encFileUrl(b.dataset.sha, b.dataset.mime)
        .then(u => { const im = b.querySelector('img'); if(im) im.src = u; })
        .catch(() => b.classList.add('nt-thumb-miss'));
    });
  }

  async function attach(n, host){
    const inp = document.createElement('input');
    inp.type='file';
    inp.onchange = async () => {
      const f = inp.files && inp.files[0]; if(!f) return;
      const state = $('.nt-state', host);
      try{
        state.textContent = 'encrypting…';
        await _ensureNotesFolder();
        const sha = await PC.uploadEncFile(f, 'Notes');
        n.res = n.res || [];
        n.res.push({ sha, name:f.name, mime:f.type||'application/octet-stream', size:f.size });
        const body = $('.nt-body', host);
        const ref = /^image\//.test(f.type) ? `\n![${f.name}](pcres:${sha})\n` : `\n[${f.name}](pcres:${sha})\n`;
        body.value = body.value + ref;
        n.body = body.value;
        await save(n, 'note');
        state.textContent = 'saved';
        renderRes(n, host);
      }catch(e){ toast('attach failed: '+(e.message||'error')); state.textContent=''; }
    };
    inp.click();
  }

  // ---------------------------------------------------------------- backup

  /* Export every note as a .jex — the SAME format the importer reads.
   *
   * One format, one code path: the backup round-trips through parseJexFile (there is a test that
   * proves export→import is lossless), and because it is a real Joplin archive it also opens in
   * Joplin itself. A backup that only this app can read is a backup that depends on this app still
   * existing, which is the one thing a backup is supposed to survive.
   *
   * Attachments are DECRYPTED into the archive. That is the point of a backup — the .jex is a
   * plaintext artifact and belongs somewhere you trust — but it is worth being explicit about, so
   * the confirm says so.
   *
   * Streamed to disk through showSaveFilePicker where the browser has it, because a library with
   * attachments is gigabytes and assembling that as one Blob would fail exactly the way importing
   * one did. Without it, notes-only (small, safe to hold in memory) rather than a crash.
   */
  async function exportBackup(){
    await load();
    const notes = Array.from(_lib.notes.values());
    if(!notes.length){ toast('no notes to back up'); return; }
    const resCount = notes.reduce((n,x)=> n + ((x.res||[]).length), 0);
    const resBytes = notes.reduce((n,x)=> n + (x.res||[]).reduce((m,r)=> m + (r.size||0), 0), 0);
    const canStream = typeof window.showSaveFilePicker === 'function';

    const withFiles = resCount > 0 && canStream;
    const msg = `Back up ${notes.length} note${notes.length===1?'':'s'}` +
      (resCount ? (withFiles
        ? ` and ${resCount} attachment${resCount===1?'':'s'} (${(resBytes/1073741824).toFixed(2)} GB)`
        : ` — WITHOUT the ${resCount} attachment(s), which this browser can't stream to disk`) : '') +
      '.\n\nThe archive is a Joplin .jex and is NOT encrypted — anything in it is readable by ' +
      'whoever holds the file. Keep it somewhere you trust.';
    if(!await uiConfirm(msg, {ok:'Back up'})) return;

    let writer = null, parts = null, handle = null;
    const stamp = new Date().toISOString().slice(0,10);
    if(canStream){
      try{
        handle = await window.showSaveFilePicker({ suggestedName:`posterchan-notes-${stamp}.jex`,
          types:[{ description:'Joplin export', accept:{'application/x-tar':['.jex']} }] });
        writer = await handle.createWritable();
      }catch(e){ if(e && e.name === 'AbortError') return; writer = null; }
    }
    if(!writer) parts = [];
    const put = async (u8) => { if(writer) await writer.write(u8); else parts.push(u8); };
    const enc8 = new TextEncoder();
    const entry = async (name, bytes) => {
      await put(PCJoplin.tarHeader(name, bytes.length));
      await put(bytes);
      await put(PCJoplin.tarPad(bytes.length));
    };

    const hex32 = (v) => String(v||'').replace(/[^0-9a-f]/gi,'').toLowerCase().padEnd(32,'0').slice(0,32);
    const t = PCJoplin._iso;
    let done = 0, failed = 0;
    const note = (h) => toast(h);
    try{
      // Folders keep their FULL PATH as the title, flat. Re-importing resolves the path back to the
      // same tree; in Joplin they appear as folders literally named "Family/Tax Returns", which is
      // ugly but lossless — and losing which notes belong together would not be.
      for(const f of _lib.folders.values()){
        const md = PCJoplin.serializeItem(f.name || 'Untitled', '', {
          id: hex32(f.id), created_time: t(f.created), updated_time: t(f.updated),
          user_created_time: t(f.created), user_updated_time: t(f.updated),
          encryption_applied: 0, parent_id: '', type_: 2 });
        await entry(`${hex32(f.id)}.md`, enc8.encode(md));
      }
      for(const n of notes){
        // pcres:<sha> → :/<joplin id>, so the links still resolve after a round trip.
        let body = String(n.body || '');
        for(const r of (n.res || [])) body = body.split('pcres:' + r.sha).join(':/' + hex32(r.sha));
        const md = PCJoplin.serializeItem(n.title || 'Untitled', body, {
          id: hex32(n.id), parent_id: n.folder ? hex32(n.folder) : '',
          created_time: t(n.created), updated_time: t(n.updated),
          user_created_time: t(n.created), user_updated_time: t(n.updated),
          is_conflict: 0, is_todo: 0, encryption_applied: 0, markup_language: 1, type_: 1 });
        await entry(`${hex32(n.id)}.md`, enc8.encode(md));

        if(withFiles) for(const r of (n.res || [])){
          const rid = hex32(r.sha);
          const ext = (r.name || '').includes('.') ? (r.name.split('.').pop() || '').slice(0,8) : '';
          try{
            const u = await PC.encFileUrl(r.sha, r.mime);
            const bytes = new Uint8Array(await (await fetch(u)).arrayBuffer());
            const rmd = PCJoplin.serializeItem(r.name || rid, '', {
              id: rid, mime: r.mime || 'application/octet-stream', filename: r.name || '',
              file_extension: ext, size: bytes.length, encryption_applied: 0, type_: 4 });
            await entry(`${rid}.md`, enc8.encode(rmd));
            await entry(`resources/${rid}${ext?'.'+ext:''}`, bytes);
          }catch(_){ failed++; }
        }
        if(++done % 20 === 0) note(`backing up… ${done}/${notes.length}`);
      }
      await put(PCJoplin.tarEnd());
      if(writer) await writer.close();
      else {
        const blob = new Blob(parts, {type:'application/x-tar'});
        const a = document.createElement('a');
        a.href = URL.createObjectURL(blob); a.download = `posterchan-notes-${stamp}.jex`;
        a.click(); setTimeout(()=>URL.revokeObjectURL(a.href), 20000);
      }
      toast(failed ? `backed up ${done} notes — ${failed} attachment(s) could not be read`
                   : `backed up ${done} notes`);
    }catch(e){
      try{ if(writer) await writer.abort(); }catch(_){ }
      toast('backup failed: ' + (e.message || 'error'));
    }
  }

  // ---------------------------------------------------------------- import

  /* An import is a long, stateful job — thousands of encrypt-and-upload round trips — and the modal
   * it reports into is an ordinary sheet that closes when you click the backdrop. Clicking beside it
   * therefore took the progress bar away mid-run, which reads exactly like "the import stopped", and
   * the natural response is to start it again. So: while one is running the backdrop does nothing,
   * there is an explicit Stop, and a second run is refused rather than racing the first. */
  let _importing = false, _cancel = false;

  function openImport(){
    modal(`<h3>Import notes</h3>
      <p class="muted small">Export from Joplin with <b>File → Export → JEX</b>, then pick that file here.
      Everything is encrypted with your key before it leaves this device — the import runs in your browser,
      the server never sees a note.</p>
      <div class="nt-imp-pick">
        <label class="btn btn-cyan">Choose a .jex file<input type="file" accept=".jex,.tar" id="nt-imp-jex" hidden></label>
        <label class="btn">Choose a folder of .md files<input type="file" id="nt-imp-md" webkitdirectory directory multiple hidden></label>
      </div>
      <div id="nt-imp-out"></div>
      <div class="nt-imp-reset">
        <button class="btn small" id="nt-imp-reset">Re-download from the relay</button>
        <span class="muted small">Clears this device’s cached copy and reads your notes back from the relay.
        It does not delete anything — the relay is the source of truth, so everything still there comes back.</span>
        <button class="btn btn-red small" id="nt-imp-nuke">Delete ALL notes everywhere</button>
        <span class="muted small">Deletes every note and folder from the relay AND this device. Permanent.
        Attachments stay in Files → Blossom → Notes.</span>
      </div>`, root => {
      $('#nt-imp-reset', root).onclick = () => resetLocal(root);
      $('#nt-imp-nuke', root).onclick = () => deleteEverything(root);
      $('#nt-imp-jex', root).onchange = e => runImport(e.target.files[0], 'jex', root);
      $('#nt-imp-md', root).onchange = e => runImport(Array.from(e.target.files), 'md', root);
      // The sheet closes on a backdrop click like every other modal, which for a job that runs for
      // several minutes meant a stray click beside it took the progress bar away — indistinguishable
      // from "the import stopped", and the natural next move is to start it again. While one is
      // running the backdrop does nothing and there is an explicit Stop instead.
      const bg = root.parentElement;
      if(bg) bg.onclick = (e) => {
        if(e.target !== bg) return;
        e.preventDefault(); e.stopPropagation();
        if(_importing){ toast('import running — use Stop if you want to end it'); return; }
        closeModal();
      };
    });
  }

  /* Turn a File-API failure into something someone can ACT on. The browser's own wording for a
   * NotReadableError is "The requested file could not be read, typically due to permission problems
   * that have occurred after a reference to a file was acquired" — accurate, and useless: it names
   * no file, suggests nothing, and reads like the app is broken. The overwhelmingly common cause on
   * Linux is a Flatpak/Snap browser that can offer a file in the picker and then not be allowed to
   * read it, and the fix is always the same: put the file somewhere the browser can reach. */
  function _readErr(e, mode){
    const name = (e && e.name) || '';
    const msg = (e && e.message) || '';
    if(name === 'NotReadableError' || name === 'SecurityError' || /could not be read/i.test(msg)){
      return 'The browser could not read that ' + (mode === 'jex' ? 'file' : 'folder') + '. ' +
        'If it lives somewhere your browser is sandboxed out of (a Flatpak or Snap build can only ' +
        'reach places like Downloads), copy it there and pick it again. It can also mean the file ' +
        'moved after you chose it.';
    }
    if(name === 'NotFoundError') return 'That file is no longer where the picker found it — re-export or pick it again.';
    return msg || 'could not read that file';
  }

  /* Clear this device's cached copy and re-read from the relay.
   *
   * Needed because deleting a note somewhere else cannot, on its own, empty this device: the cache
   * is deliberately authoritative when the relay says nothing, since "the relay returned no notes"
   * is exactly what an unreachable relay also looks like, and treating that as "you have no notes"
   * is the wipe this codebase keeps guarding against. So a genuine reset has to be something you
   * ask for. Queued offline writes are lost with it, which the confirm says.
   */
  async function resetLocal(root){
    const queued = pending().length;
    const msg = 'Clear this device’s copy of your notes and re-download from the relay?' +
      (queued ? `\n\n${queued} note(s) written offline have not synced yet and WILL be lost.` : '');
    if(!await uiConfirm(msg, {ok:'Clear', danger:true})) return;
    const out = $('#nt-imp-out', root);
    if(out) out.innerHTML = '<div class="spinner"></div>';
    try{ localStorage.removeItem(PENDING_KEY); }catch(_){ }
    let n = 0;
    try{
      n = await Store().purge(ev => ev.kind === KIND &&
        (ev.tags||[]).some(t => t[0]==='d' && typeof t[1]==='string' && t[1].startsWith('pcai:note')));
    }catch(e){ if(out) out.innerHTML = `<div class="empty">couldn’t clear: ${enc(e.message||'error')}</div>`; return; }
    _lib = null; _sel = null; _loading = null;
    unwatch();
    await load();          // straight back from the relay
    await refresh();
    closeModal();
    _paint();
    toast(`cleared ${n} cached note event(s) — re-read ${_lib ? _lib.notes.size : 0} from the relay`);
  }

  /* Delete every note and folder, for real. "Clear this device's copy" deliberately does NOT do
   * this — it re-reads from the relay, so anything still published comes straight back, which is
   * correct and also exactly what looks like the button not working. This is the other one.
   *
   * Typed confirmation rather than a yes/no: it is unrecoverable and it is one tap away from a
   * button people press to fix a display problem. */
  async function deleteEverything(root){
    await load();
    const n = _lib.notes.size, f = _lib.folders.size;
    if(!n && !f){ toast('there are no notes to delete'); return; }
    const typed = await uiPrompt(`Permanently delete ${n} note(s) and ${f} folder(s) from the relay and this device? ` +
      `This cannot be undone.\n\nType DELETE to confirm.`, {ok:'Delete everything', placeholder:'DELETE'});
    if((typed||'').trim().toUpperCase() !== 'DELETE'){ toast('not deleted'); return; }
    const out = $('#nt-imp-out', root);
    const items = Array.from(_lib.notes.values()).map(o=>[o,'note'])
      .concat(Array.from(_lib.folders.values()).map(o=>[o,'folder']));
    let done = 0, failed = 0;
    for(const [obj, kind] of items){
      try{ await remove(obj, kind); }catch(_){ failed++; }
      if(++done % 5 === 0 || done === items.length){
        out.innerHTML = `<div class="nt-imp-bar"><i style="width:${Math.round(done/items.length*100)}%"></i></div>
          <div class="muted small">deleting ${done} / ${items.length}${failed?` · ${failed} failed`:''}…</div>`;
      }
    }
    try{ localStorage.removeItem(PENDING_KEY); }catch(_){ }
    try{ await Store().purge(ev => ev.kind === KIND &&
      (ev.tags||[]).some(t => t[0]==='d' && typeof t[1]==='string' && t[1].startsWith('pcai:note'))); }catch(_){ }
    _lib = { notes:new Map(), folders:new Map() }; _sel = null; _filter.folder = FOLDER_ALL;
    out.innerHTML = `<div class="nt-imp-done"><b>Deleted ${done - failed} item(s).</b>` +
      (failed ? `<div class="nt-warn small">⚠ ${failed} could not be deleted — run it again.</div>` : '') +
      `<div class="muted small">Attachments are still in Files → Blossom → Notes.</div></div>`;
    toast(`deleted ${done - failed} notes and folders`);
    setTimeout(()=>{ closeModal(); _paint(); }, 1400);
  }

  async function runImport(input, mode, root){
    const out = $('#nt-imp-out', root);
    const say = h => { out.innerHTML = h; };
    if(!input || (Array.isArray(input) && !input.length)) return;
    if(!window.PCJoplin){ say('<div class="empty">the importer didn’t load — reload the page</div>'); return; }
    say('<div class="spinner"></div><div class="muted small">reading the export…</div>');
    let parsed, unreadable = [];
    try{
      if(mode === 'jex'){
        // STREAMED, never `await input.arrayBuffer()`. A real library is gigabytes (the one this
        // was rebuilt against is 2.17 GB), and Chrome refuses to materialise a blob that size,
        // throwing a NotReadableError whose wording blames permissions — so a perfectly good
        // export looked like a file the browser wasn't allowed to open. parseJexFile reads only
        // the tar headers and the notes; attachments stay on disk until each one is uploaded.
        parsed = await PCJoplin.parseJexFile(input, {
          onScan: (at, total) => say(`<div class="spinner"></div><div class="muted small">scanning the archive… ${Math.round(at/total*100)}%</div>`),
          onRead: (n, total) => say(`<div class="spinner"></div><div class="muted small">reading notes… ${n} / ${total}</div>`),
        });
        parsed._file = input;   // resources are read from it lazily during the import
      } else {
        const files = [];
        for(const f of input){
          const path = f.webkitRelativePath || f.name;
          // PER FILE, not per import: a folder export is thousands of files and the browser can
          // fail to read any ONE of them (a broken symlink, a file the sandbox can't reach, one
          // that moved since the picker ran). Aborting the whole run for that would throw away
          // 2999 readable notes because of one — collect and report them at the end instead.
          try{
            if(/\.md$/i.test(path)) files.push({ name:path, text: await f.text() });
            else files.push({ name:path, data: new Uint8Array(await f.arrayBuffer()) });
          }catch(_){ unreadable.push(path); }
        }
        if(!files.length) throw new Error('none of those files could be read');
        parsed = PCJoplin.parseMarkdownFiles(files);
      }
    }catch(e){ say(`<div class="empty">${enc(_readErr(e, mode))}</div>`); return; }

    const c = parsed.counts;
    if(unreadable.length){
      parsed.warnings.push(`${unreadable.length} file(s) could not be read and were skipped` +
        (unreadable.length <= 3 ? ': ' + unreadable.join(', ') : ''));
    }
    say(`<div class="nt-imp-sum">
        <b>Found ${c.notes} note${c.notes===1?'':'s'}</b>
        <span class="muted small">${c.folders} folder(s) · ${c.tags||0} tag(s) · ${c.resources} attachment(s)</span>
        ${parsed.warnings.map(w=>`<div class="nt-warn small">⚠ ${enc(w)}</div>`).join('')}
      </div>
      <div class="nt-imp-run">
        <button class="btn btn-cyan" id="nt-imp-go">Import ${c.notes} note${c.notes===1?'':'s'}</button>
        <span class="muted small">Existing notes from a previous import of the same library are updated, not duplicated.</span>
      </div>
      <div id="nt-imp-prog"></div>`);
    $('#nt-imp-go', root).onclick = () => doImport(parsed, root);
  }

  /* The actual write. Deliberately sequential and RESUMABLE: a Joplin library is commonly thousands
   * of notes, each write is a signature plus an encryption (an external signer prompts per call and
   * a phone is slow), and the tab WILL be closed part way through at least once. Every note is keyed
   * by its Joplin id, so a re-run updates rather than duplicates — the same "match what's already
   * there" resume the music bulk-import uses.
   *
   * Attachments are uploaded ONCE per Joplin resource id and shared by every note that references
   * them, because the encryption uses a content-derived IV: identical bytes produce identical
   * ciphertext, so Blossom dedups them anyway and re-uploading is pure waste. */
  /* What the media server will accept, in bytes, or 0 for "don't know — try everything". BUD-06's
   * HEAD /upload answers a too-large pre-flight with `X-Reason: max N MB`, which is the only place
   * the number is published; the endpoint sets Access-Control-Expose-Headers so it is readable
   * cross-origin. Asking beats hardcoding: this cap is an admin setting, so a LAN node can be set
   * to 10 GB while a CDN-fronted one is stuck at 100 MB, and an importer that assumed either would
   * be wrong on the other. */
  async function _uploadLimit(){
    try{
      const r = await fetch(PC.mediaServer() + '/upload', {
        method:'HEAD', headers:{ 'X-Content-Length': String(64 * 1024 * 1024 * 1024) } });
      const m = /max\s+(\d+)\s*MB/i.exec(r.headers.get('X-Reason') || '');
      if(m) return parseInt(m[1], 10) * 1048576;
    }catch(_){ }
    return 0;
  }

  /* Register "Notes" as an ENCRYPTED folder in the drive index before anything is filed into it.
   * uploadEncFile() stamps each blob with a folder NAME but never creates the folder, so without
   * this the drive holds a thousand files under a folder that does not exist in its own index —
   * which is why Files → Blossom showed no Notes folder at all — and the files are not marked as
   * living in an encrypted folder, so the drive treats them as ordinary blobs it can preview.
   * addFolder is idempotent (it returns false if the name is already there). */
  async function _ensureNotesFolder(){
    const FI = PC.filesIdx ? PC.filesIdx() : null;
    if(!FI || !FI.addFolder) return;
    try{
      if(FI.pull) await FI.pull();          // know what's there before adding to it
      FI.addFolder('Notes', true);
    }catch(_){ }
  }

  async function doImport(parsed, root){
    const prog = $('#nt-imp-prog', root);
    if(_importing){ toast('an import is already running'); return; }
    // Offline, every note would land in the pending queue instead of on the relay — a few thousand
    // encrypted events into localStorage, which blows its quota long before the import finishes and
    // leaves an unknown fraction of the library saved. Attachments can't upload at all. The import
    // is resumable but there is no reason to start it in a state where it cannot succeed.
    if(!navigator.onLine){
      prog.innerHTML = '<div class="nt-warn small">⚠ You’re offline. An import has to reach the relay ' +
        '(and, for attachments, the file store). Reconnect and run it again.</div>';
      return;
    }
    const go = $('#nt-imp-go', root); if(go) go.disabled = true;
    _importing = true; _cancel = false;
    // Stop is explicit, and safe: an import is resumable, so stopping loses nothing that re-running
    // won't redo. The browser's own "leave site?" guard covers a closed tab.
    if(go){
      const stop = document.createElement('button');
      stop.className = 'btn btn-red small'; stop.textContent = 'Stop';
      stop.onclick = () => { _cancel = true; stop.disabled = true; stop.textContent = 'stopping…'; };
      go.parentElement.appendChild(stop);
    }
    const beforeUnload = (e) => { e.preventDefault(); e.returnValue = ''; return ''; };
    window.addEventListener('beforeunload', beforeUnload);
    try{
    await load();

    const paths = PCJoplin.folderPaths(parsed.folders);
    const tagsFor = PCJoplin.tagsByNote(parsed.tags, parsed.noteTags);

    // Folders first — a note needs its folder's id to exist. Matched by PATH so re-importing the
    // same library reuses the folder instead of making a second "Work".
    const folderIdByJoplin = new Map();
    const byName = new Map(Array.from(_lib.folders.values()).map(f=>[f.name, f.id]));
    let fdone = 0;
    for(const f of parsed.folders){
      const name = (paths.get(f.id) || f.title || 'Untitled').slice(0, 60);
      let id = byName.get(name);
      if(!id){
        const rec = { v:1, id:uid(), name, created:f.created||now(), updated:now() };
        _lib.folders.set(rec.id, rec);
        await save(rec, 'folder');
        byName.set(name, rec.id);
        id = rec.id;
      }
      folderIdByJoplin.set(f.id, id);
      prog.innerHTML = `<div class="muted small">folders ${++fdone}/${parsed.folders.length}…</div>`;
    }

    // Attachments. Failures are non-fatal: the note still imports, its link is left pointing at the
    // original `:/id` text, and the count is reported at the end. Losing 3000 notes because one
    // picture failed to upload would be the worst possible trade.
    const shaByRes = new Map();
    const resMeta = new Map();
    /* RESUME. Everything a previous run of the same library uploaded is already recorded on the
     * notes themselves, so a re-import should not re-encrypt and re-send gigabytes to arrive at
     * byte-identical blobs the server would only dedup anyway. Keyed on name+size, which is what
     * the note stores; a collision would at worst reuse an identical-sized file of the same name.
     * This is what makes "run it again to pick up what failed" a reasonable instruction rather
     * than a half-hour penalty. */
    const known = new Map();
    for(const n of _lib.notes.values())
      for(const r of (n.res || [])) if(r && r.sha && r.name) known.set(r.name + '|' + (r.size||0), r);
    let reused = 0;
    let rdone = 0;
    const failed = [];    // {id,name} — reported by NAME and retryable, never just a count
    const tooBig = [];
    // Ask the server what it will actually accept rather than hardcoding a number: the cap is an
    // admin setting (blossom_max_upload_mb), so a node on a LAN can be set far higher than one
    // behind a CDN. BUD-06 answers a HEAD with the reason, and the endpoint exposes it to JS.
    const limit = await _uploadLimit();
    const totalBytes = parsed.resources.reduce((n,r)=> n + ((r.data && r.data.length) || 0), 0);

    /* BATCH the drive index. Every uploadEncFile() records the blob in FilesIdx, and each record
     * schedules a save of the WHOLE index document — which for 1200 attachments means 1200 saves of
     * a document that has grown past the inline limit, so each one re-encrypts and re-uploads a
     * fresh index blob. That storm is what produced a screen full of "Couldn't read your folders
     * from the server": the index's own anti-wipe guard refuses to write when it can't first
     * confirm what is on the server, and it could not, because the previous save was still in
     * flight. beginBatch/endBatch exist for precisely this and I should have used them from the
     * start. Flushed periodically as well as at the end, so an interrupted import doesn't lose the
     * whole index — and the notes carry their own sha+mime regardless, so pictures render even if
     * the index never lands. */
    const FI = PC.filesIdx ? PC.filesIdx() : null;
    await _ensureNotesFolder();
    const flushIdx = async () => { if(FI && FI.endBatch){ try{ await FI.endBatch(); }catch(_){ } FI.beginBatch(); } };
    if(FI && FI.beginBatch) FI.beginBatch();

    const paint = () => {
      prog.innerHTML = `<div class="nt-imp-bar"><i style="width:${Math.round(rdone/Math.max(parsed.resources.length,1)*100)}%"></i></div>
        <div class="muted small">attachments ${rdone}/${parsed.resources.length}` +
        `${totalBytes?` · ${(totalBytes/1073741824).toFixed(2)} GB total`:''}` +
        `${reused?` · ${reused} already uploaded`:''}` +
        `${failed.length?` · ${failed.length} failed (will retry)`:''}${tooBig.length?` · ${tooBig.length} too large`:''}…</div>`;
    };

    /* Upload one attachment, with RETRIES. "2 attachments failed" is not an acceptable outcome for
     * an import: a transient 502 or a dropped socket somewhere in 1200 uploads is close to certain,
     * and one that isn't retried is a picture silently missing from a note forever. Three attempts
     * with a widening gap, then it goes on the failed list BY NAME so it can be retried explicitly
     * rather than reported as a number. */
    async function putResource(res){
      const size = (res.data && res.data.length) || 0;
      const name = res.filename || res.title || (res.id + (res.ext?'.'+res.ext:''));
      if(!size) return { ok:false, name, why:'empty' };
      if(limit && size > limit){ tooBig.push(`${name} (${Math.round(size/1048576)} MB)`); return { ok:'skip' }; }
      const prev = known.get(name + '|' + size);
      if(prev && prev.sha){
        shaByRes.set(res.id, prev.sha);
        resMeta.set(res.id, { sha:prev.sha, name, mime:res.mime || prev.mime, size });
        reused++;
        return { ok:true };
      }
      let lastErr = '';
      for(let attempt = 0; attempt < 3; attempt++){
        if(_cancel) return { ok:false, name, why:'cancelled' };
        try{
          // Read THIS attachment only — for a streamed .jex `res.data` is an {offset,length}
          // handle, so peak memory is one file rather than the whole library.
          const bytes = await PCJoplin.readResource(parsed._file, res);
          if(!bytes || !bytes.length) return { ok:false, name, why:'unreadable in the export' };
          const file = new File([bytes], name, { type: res.mime || 'application/octet-stream' });
          const sha = await PC.uploadEncFile(file, 'Notes');
          shaByRes.set(res.id, sha);
          resMeta.set(res.id, { sha, name, mime:res.mime, size });
          return { ok:true };
        }catch(e){
          lastErr = (e && e.message) || 'upload failed';
          await new Promise(r => setTimeout(r, 400 * (attempt + 1) * (attempt + 1)));
        }
      }
      return { ok:false, name, why:lastErr };
    }

    for(const res of parsed.resources){
      if(_cancel) break;
      const r = await putResource(res);
      if(r.ok === false) failed.push({ id:res.id, name:r.name, why:r.why });
      rdone++;
      if(rdone % 2 === 0 || rdone === parsed.resources.length) paint();
      if(rdone % 100 === 0) await flushIdx();   // bound what an interruption costs
    }

    // One last sweep over anything still failing, after everything else has settled — a failure at
    // attachment 40 is often just the server being busy, and by the end it isn't.
    if(failed.length && !_cancel){
      const retry = failed.splice(0, failed.length);
      let n = 0;
      for(const f of retry){
        if(_cancel){ failed.push(f); continue; }
        prog.innerHTML = `<div class="muted small">retrying ${++n}/${retry.length} failed attachment(s)…</div>`;
        const res = parsed.resources.find(x => x.id === f.id);
        const r = res ? await putResource(res) : { ok:false, name:f.name, why:f.why };
        if(r.ok === false) failed.push(r.name ? { id:f.id, name:r.name, why:r.why } : f);
      }
    }
    if(FI && FI.endBatch){ try{ await FI.endBatch(); }catch(_){ } }

    // Notes. Existing ones are found by their Joplin id (kept in src.id), so a second run of the
    // same export updates in place.
    const bySrc = new Map();
    for(const n of _lib.notes.values()) if(n.src && n.src.id) bySrc.set(n.src.id, n);

    let done = 0, noteFail = 0, queued = 0;
    const failedNotes = [];
    for(const jn of parsed.notes){
      if(_cancel) break;
      const key = jn.id || ('md:' + jn.title);
      const existing = bySrc.get(key);
      const rec = existing || blankNote('');
      rec.title = jn.title || 'Untitled';
      rec.body = PCJoplin.rewriteLinks(jn.body || '', id => {
        const sha = shaByRes.get(id);
        return sha ? 'pcres:' + sha : null;   // a note-to-note link stays as written
      });
      rec.folder = folderIdByJoplin.get(jn.parent_id) || '';
      rec.tags = (tagsFor.get(jn.id) || jn.tagNames || []).slice(0, 30);
      rec.created = jn.created || rec.created || now();
      rec.src = { app:'joplin', id:key };
      if(jn.conflict) rec.tags = Array.from(new Set(rec.tags.concat(['conflict'])));
      if(jn.todo) rec.tags = Array.from(new Set(rec.tags.concat([jn.done?'done':'todo'])));
      // MERGE the attachment list, never replace it: on a re-import, an existing note may carry
      // files the user attached here by hand, which are in no Joplin export and would be dropped.
      const fromJoplin = Array.from(PCJoplin.linkedIds(jn.body||''))
        .map(id => resMeta.get(id)).filter(Boolean);
      const seenSha = new Set(fromJoplin.map(x => x.sha));
      rec.res = fromJoplin.concat((rec.res || []).filter(x => x && !seenSha.has(x.sha)));
      _lib.notes.set(rec.id, rec);
      // RETRY, like the attachments. A note save can throw or time out under the load the import
      // itself is generating — the client gives a publish 8 seconds for its OK, and thousands of
      // uploads on the same socket push past that — and a note counted as "failed" on the first
      // attempt is a note whose text is on no relay at all. Three tries, then it is named.
      let saved = false;
      for(let attempt = 0; attempt < 3 && !saved; attempt++){
        if(_cancel) break;
        try{
          const r = await save(rec, 'note');
          if(r.ok || r.queued){ if(r.queued) queued++; saved = true; }
        }catch(_){ }
        if(!saved) await new Promise(r => setTimeout(r, 500 * (attempt + 1) * (attempt + 1)));
      }
      if(!saved){ noteFail++; failedNotes.push(rec.title || 'Untitled'); }
      done++;
      if(done % 5 === 0 || done === parsed.notes.length){
        prog.innerHTML = `<div class="nt-imp-bar"><i style="width:${Math.round(done/parsed.notes.length*100)}%"></i></div>
          <div class="muted small">${done} / ${parsed.notes.length} notes${noteFail?` · ${noteFail} failed`:''}${queued?` · ${queued} queued offline`:''}</div>`;
      }
    }

    }finally{
      _importing = false;
      window.removeEventListener('beforeunload', beforeUnload);
    }
    prog.innerHTML = `<div class="nt-imp-done">
      <b>${_cancel?'Stopped after':'Imported'} ${done - noteFail} note${done-noteFail===1?'':'s'}.</b>
      ${reused?`<div class="muted small">${reused} attachment(s) were already uploaded and were reused.</div>`:''}
      ${_cancel?'<div class="muted small">Nothing is lost — run the import again and it picks up where this left off, updating rather than duplicating.</div>':''}
      ${failed.length?`<div class="nt-warn small">⚠ ${failed.length} attachment(s) could not be stored after 3 tries — their notes imported with the link left as text:
        ${enc(failed.slice(0,8).map(f=>f.name).join(', '))}${failed.length>8?' …':''}.
        Run the import again to retry just those; it updates rather than duplicating.</div>`:''}
      ${tooBig.length?`<div class="nt-warn small">⚠ ${tooBig.length} attachment(s) are larger than this server accepts and were skipped:
        ${enc(tooBig.slice(0,5).join(', '))}${tooBig.length>5?' …':''}.
        Raise “Max upload (MB)” in Admin → Media and run the import again — it updates rather than duplicating.</div>`:''}
      ${noteFail?`<div class="nt-warn small">⚠ ${noteFail} note(s) failed to save after 3 tries:
        ${enc(failedNotes.slice(0,8).join(', '))}${failedNotes.length>8?' …':''}.
        Run the import again — it updates rather than duplicating.</div>`:''}
      ${queued?`<div class="muted small">${queued} saved on this device and will sync when you’re back online.</div>`:''}
    </div>`;
    toast(`imported ${done - noteFail} notes`);
    setTimeout(()=>{ closeModal(); render(); }, 1200);
  }

  if(document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot);
  else boot();
})();
