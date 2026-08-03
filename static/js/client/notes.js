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
      unmount(){ _sel=null; },
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

  function blankNote(folder){
    return { v:1, id:uid(), title:'', body:'', folder:folder&&folder!==FOLDER_ALL&&folder!==FOLDER_NONE?folder:'',
             tags:[], created:now(), updated:now(), res:[] };
  }

  // In-memory library, rebuilt from the relay/cache. `notes` and `folders` are keyed by id.
  let _lib = null;          // {notes:Map, folders:Map}
  let _loading = null;
  let _sel = null;          // id of the open note
  let _filter = { folder:FOLDER_ALL, q:'', tag:'' };

  async function load(force){
    if(_lib && !force) return _lib;
    if(!_loading) _loading = _load().finally(()=>{ _loading=null; });
    return _loading;
  }

  /* Cache first, then network — the same shape as the app's other cache-first reads. The cache
   * answer is what makes Notes work offline; the network answer is folded IN, never OVER: a relay
   * that returns nothing (unreachable, throttled, or just slow) must leave the local library alone.
   * That asymmetry is the whole anti-wipe rule this codebase keeps relearning. */
  async function _load(){
    const filter = { authors:[ME().pubkey], kinds:[KIND], '#l':[L_TAG], limit:5000 };
    const lib = { notes:new Map(), folders:new Map() };
    let cached = [];
    try{ cached = Store().query([filter]) || []; }catch(_){ cached = []; }
    await _absorb(lib, cached);
    _lib = lib;
    try{
      const live = await Relay().query([filter]);
      if(live && live.length) await _absorb(lib, live);
    }catch(_){ /* offline: the cache stands on its own */ }
    return _lib;
  }

  /* Decrypt events into the library. Newest wins per id — events for one `d` can arrive from
   * several relays with different created_at, and an older copy must never overwrite a newer one. */
  async function _absorb(lib, evs){
    for(const ev of evs){
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
      if(PC.VIEW==='notes' && !_dirty) render();
    }
    return sent;
  }

  /* Write one note/folder. Returns {ok, queued}. NEVER throws on a dead relay: offline, the note is
   * saved locally and queued, and the UI says so — losing what someone just typed because the wifi
   * dropped is the one failure a notes app may not have. */
  async function save(obj, kind){
    const isFolder = kind === 'folder';
    obj.updated = now();
    const d = (isFolder ? D_FOLDER : D_NOTE) + obj.id;
    const body = Object.assign({}, obj); delete body._at;
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

  function folderName(id){
    if(id === FOLDER_ALL) return 'All notes';
    if(id === FOLDER_NONE || !id) return 'Unfiled';
    const f = _lib && _lib.folders.get(id);
    return f ? f.name : 'Unfiled';
  }

  function visibleNotes(){
    if(!_lib) return [];
    const q = _filter.q.trim().toLowerCase();
    let list = Array.from(_lib.notes.values());
    if(_filter.folder === FOLDER_NONE) list = list.filter(n => !n.folder || !_lib.folders.has(n.folder));
    else if(_filter.folder !== FOLDER_ALL) list = list.filter(n => n.folder === _filter.folder);
    if(_filter.tag) list = list.filter(n => (n.tags||[]).includes(_filter.tag));
    if(q) list = list.filter(n => (n.title||'').toLowerCase().includes(q) || (n.body||'').toLowerCase().includes(q));
    return list.sort((a,b) => (b.updated||0) - (a.updated||0));
  }

  function allTags(){
    const c = new Map();
    if(_lib) for(const n of _lib.notes.values()) for(const t of (n.tags||[])) c.set(t, (c.get(t)||0)+1);
    return Array.from(c.entries()).sort((a,b)=> b[1]-a[1] || a[0].localeCompare(b[0]));
  }

  async function render(){
    const feed = $('#feed');
    if(!feed) return;
    if(!_lib){
      feed.innerHTML = '<div class="nt-wrap"><div class="spinner"></div></div>';
      try{ await load(); }
      catch(e){ feed.innerHTML = `<div class="nt-wrap"><div class="empty">Couldn’t open your notes: ${enc(e.message||'error')}</div></div>`; return; }
    }
    const notes = visibleNotes();
    const pend = pending().length;
    const folders = Array.from(_lib.folders.values()).sort((a,b)=>(a.name||'').localeCompare(b.name||''));
    const tags = allTags();
    const total = _lib.notes.size;

    feed.innerHTML = `<div class="nt-wrap${_sel?' nt-open':''}">
      <aside class="nt-side">
        <div class="nt-side-head">
          <button class="btn btn-cyan nt-new"><svg class="ic b-ic" aria-hidden="true"><use href="#i-pen"></use></svg>New note</button>
          <button class="btn nt-import" title="Import from Joplin"><svg class="ic b-ic" aria-hidden="true"><use href="#i-folder"></use></svg>Import</button>
        </div>
        <input class="input nt-search" type="search" placeholder="Search notes…" value="${enc(_filter.q)}" autocomplete="off">
        <nav class="nt-folders">
          <button class="nt-folder${_filter.folder===FOLDER_ALL?' active':''}" data-f="${FOLDER_ALL}"><span>All notes</span><i>${total}</i></button>
          ${folders.map(f=>`<button class="nt-folder${_filter.folder===f.id?' active':''}" data-f="${enc(f.id)}"><span>${enc(f.name||'Untitled')}</span><i>${Array.from(_lib.notes.values()).filter(n=>n.folder===f.id).length}</i></button>`).join('')}
          <button class="nt-folder${_filter.folder===FOLDER_NONE?' active':''}" data-f="${FOLDER_NONE}"><span>Unfiled</span><i>${Array.from(_lib.notes.values()).filter(n=>!n.folder||!_lib.folders.has(n.folder)).length}</i></button>
          <button class="nt-folder nt-addfolder"><span>+ New folder</span></button>
        </nav>
        ${tags.length?`<div class="nt-tags">${tags.slice(0,30).map(([t,c])=>`<button class="nt-tag${_filter.tag===t?' active':''}" data-t="${enc(t)}">#${enc(t)} <i>${c}</i></button>`).join('')}</div>`:''}
        ${pend?`<div class="nt-pending muted small">${pend} note${pend===1?'':'s'} waiting to sync</div>`:''}
      </aside>
      <section class="nt-list" aria-label="Notes">
        <div class="nt-list-head">
          <b>${enc(folderName(_filter.folder))}</b>
          <span class="muted small">${notes.length} note${notes.length===1?'':'s'}</span>
        </div>
        ${notes.length ? notes.map(n=>`
          <button class="nt-item${_sel===n.id?' active':''}" data-id="${enc(n.id)}">
            <b>${enc(n.title || 'Untitled')}</b>
            <span class="nt-snip muted small">${enc((n.body||'').replace(/[#*`>\-\n]+/g,' ').trim().slice(0,90))}</span>
            <span class="nt-meta muted small">${_fmt(n.updated)}${n.res&&n.res.length?` · ${n.res.length} 📎`:''}</span>
          </button>`).join('')
        : `<div class="empty">${total ? 'Nothing matches that.' : 'No notes yet. Write one, or import your Joplin export.'}</div>`}
      </section>
      <section class="nt-editor" aria-label="Editor"></section>
    </div>`;

    $('.nt-new', feed).onclick = () => openNote(blankNote(_filter.folder), true);
    $('.nt-import', feed).onclick = openImport;
    $('.nt-addfolder', feed).onclick = addFolder;
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
    const name = await uiPrompt('Name this folder', {ok:'Create', placeholder:'e.g. Work'});
    if(!name || !name.trim()) return;
    const f = { v:1, id:uid(), name:name.trim().slice(0,60), created:now(), updated:now() };
    _lib.folders.set(f.id, f);
    const r = await save(f, 'folder');
    if(r.queued) toast('folder saved — it will sync when you’re back online');
    render();
  }

  /* The editor. Saves are debounced and LAST-WRITE-WINS on this device; every save is a whole-note
   * publish, so there is no partial state to reconcile. */
  let _saveT = null, _dirty = false;
  function openNote(n, isNew){
    _sel = n.id;
    const host = document.querySelector('.nt-editor');
    if(!host) return;
    document.querySelector('.nt-wrap').classList.add('nt-open');
    const folders = Array.from(_lib.folders.values()).sort((a,b)=>(a.name||'').localeCompare(b.name||''));
    host.innerHTML = `
      <div class="nt-ed-head">
        <button class="nt-back" aria-label="Back to the list"><svg class="ic b-ic" aria-hidden="true"><use href="#i-chevron-left"></use></svg></button>
        <input class="input nt-title" placeholder="Title" value="${enc(n.title||'')}" maxlength="200">
        <span class="nt-state muted small"></span>
        <button class="btn nt-preview" title="Preview">👁</button>
        <button class="btn btn-red small nt-del" title="Delete note"><svg class="ic b-ic" aria-hidden="true"><use href="#i-trash"></use></svg></button>
      </div>
      <div class="nt-ed-bar">
        <select class="input nt-folder-sel">
          <option value="">Unfiled</option>
          ${folders.map(f=>`<option value="${enc(f.id)}"${n.folder===f.id?' selected':''}>${enc(f.name||'Untitled')}</option>`).join('')}
        </select>
        <input class="input nt-tagin" placeholder="tags, comma separated" value="${enc((n.tags||[]).join(', '))}">
        <button class="btn nt-attach" title="Attach a file (encrypted)">📎</button>
      </div>
      <textarea class="nt-body" placeholder="Write…  (markdown)">${enc(n.body||'')}</textarea>
      <div class="nt-render markdown hidden"></div>
      <div class="nt-res"></div>`;

    const title = $('.nt-title', host), body = $('.nt-body', host), state = $('.nt-state', host);
    const mark = txt => { state.textContent = txt; };
    const commit = async () => {
      n.title = title.value.trim();
      n.body = body.value;
      n.folder = $('.nt-folder-sel', host).value || '';
      n.tags = $('.nt-tagin', host).value.split(',').map(s=>s.trim()).filter(Boolean).slice(0,30);
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
      _saveT = setTimeout(commit, 700);
    };
    title.oninput = body.oninput = touch;
    $('.nt-folder-sel', host).onchange = touch;
    $('.nt-tagin', host).onchange = touch;
    // Leaving the field saves NOW rather than waiting out the debounce — closing the tab or the
    // laptop lid inside that 700ms is exactly when the edit would be lost.
    body.onblur = title.onblur = () => { if(_dirty){ clearTimeout(_saveT); commit(); } };

    $('.nt-back', host).onclick = () => { _sel=null; document.querySelector('.nt-wrap').classList.remove('nt-open'); render(); };
    $('.nt-preview', host).onclick = () => {
      const r = $('.nt-render', host);
      const showing = !r.classList.contains('hidden');
      if(showing){ r.classList.add('hidden'); body.classList.remove('hidden'); }
      else { r.innerHTML = renderBody(n, body.value); r.classList.remove('hidden'); body.classList.add('hidden'); hydrateRes(r, n); }
    };
    $('.nt-del', host).onclick = async () => {
      if(!await uiConfirm(`Delete “${n.title||'Untitled'}”? This can’t be undone.`, {ok:'Delete', danger:true})) return;
      await remove(n, 'note');
      _sel = null;
      render();
    };
    $('.nt-attach', host).onclick = () => attach(n, host);
    renderRes(n, host);
    if(isNew) title.focus();
  }

  function renderSideCounts(){
    const el = document.querySelector('.nt-list-head span');
    if(el) el.textContent = `${visibleNotes().length} notes`;
  }

  /* A note body is UNTRUSTED markdown — most of them arrive from an import file. mdToHtml escapes
   * before it renders, which is why notes go through it rather than any innerHTML of their own. */
  function renderBody(n, src){
    return mdToHtml(String(src||''));
  }

  // `pcres:<sha>` links (written by the importer) resolve to a decrypted object URL at view time.
  // They can't be resolved earlier: the URL is a blob: handle that dies with the page.
  async function hydrateRes(root, n){
    for(const el of Array.from(root.querySelectorAll('img[src^="pcres:"], a[href^="pcres:"]'))){
      const isImg = el.tagName === 'IMG';
      const sha = (isImg ? el.getAttribute('src') : el.getAttribute('href')).slice(6);
      try{
        const u = await PC.encFileUrl(sha);
        if(isImg) el.src = u; else { el.href = u; el.target = '_blank'; el.rel = 'noopener'; }
      }catch(_){
        if(isImg){ const s=document.createElement('span'); s.className='muted small';
          s.textContent = navigator.onLine ? '[attachment unavailable]' : '[attachment not downloaded — open this note online once]';
          el.replaceWith(s); }
      }
    }
  }

  function renderRes(n, host){
    const box = $('.nt-res', host); if(!box) return;
    if(!n.res || !n.res.length){ box.innerHTML=''; return; }
    box.innerHTML = `<div class="nt-res-head muted small">Attachments</div>` + n.res.map(r=>
      `<button class="nt-res-item" data-sha="${enc(r.sha)}">📎 ${enc(r.name||r.sha.slice(0,8))} <i class="muted small">${((r.size||0)/1024).toFixed(0)} KB</i></button>`).join('');
    $$('.nt-res-item', box).forEach(b => b.onclick = async () => {
      try{ const u = await PC.encFileUrl(b.dataset.sha); window.open(u, '_blank', 'noopener'); }
      catch(e){ toast(navigator.onLine ? 'couldn’t open that attachment' : 'that attachment isn’t downloaded for offline use'); }
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

  // ---------------------------------------------------------------- import

  function openImport(){
    modal(`<h3>Import notes</h3>
      <p class="muted small">Export from Joplin with <b>File → Export → JEX</b>, then pick that file here.
      Everything is encrypted with your key before it leaves this device — the import runs in your browser,
      the server never sees a note.</p>
      <div class="nt-imp-pick">
        <label class="btn btn-cyan">Choose a .jex file<input type="file" accept=".jex,.tar" id="nt-imp-jex" hidden></label>
        <label class="btn">Choose a folder of .md files<input type="file" id="nt-imp-md" webkitdirectory directory multiple hidden></label>
      </div>
      <div id="nt-imp-out"></div>`, root => {
      $('#nt-imp-jex', root).onchange = e => runImport(e.target.files[0], 'jex', root);
      $('#nt-imp-md', root).onchange = e => runImport(Array.from(e.target.files), 'md', root);
    });
  }

  async function runImport(input, mode, root){
    const out = $('#nt-imp-out', root);
    const say = h => { out.innerHTML = h; };
    if(!input || (Array.isArray(input) && !input.length)) return;
    if(!window.PCJoplin){ say('<div class="empty">the importer didn’t load — reload the page</div>'); return; }
    say('<div class="spinner"></div><div class="muted small">reading the export…</div>');
    let parsed;
    try{
      if(mode === 'jex') parsed = await PCJoplin.parseJex(await input.arrayBuffer());
      else {
        const files = [];
        for(const f of input){
          const path = f.webkitRelativePath || f.name;
          if(/\.md$/i.test(path)) files.push({ name:path, text: await f.text() });
          else files.push({ name:path, data: new Uint8Array(await f.arrayBuffer()) });
        }
        parsed = PCJoplin.parseMarkdownFiles(files);
      }
    }catch(e){ say(`<div class="empty">${enc(e.message||'could not read that file')}</div>`); return; }

    const c = parsed.counts;
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
  async function doImport(parsed, root){
    const prog = $('#nt-imp-prog', root);
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
    let rdone = 0, rfail = 0;
    for(const res of parsed.resources){
      if(!res.data || !res.data.length){ rfail++; continue; }
      try{
        const name = res.filename || res.title || (res.id + (res.ext?'.'+res.ext:''));
        const file = new File([res.data], name, { type: res.mime || 'application/octet-stream' });
        const sha = await PC.uploadEncFile(file, 'Notes');
        shaByRes.set(res.id, sha);
        resMeta.set(res.id, { sha, name, mime:res.mime, size:res.data.length });
      }catch(_){ rfail++; }
      prog.innerHTML = `<div class="muted small">attachments ${++rdone}/${parsed.resources.length}${rfail?` (${rfail} failed)`:''}…</div>`;
    }

    // Notes. Existing ones are found by their Joplin id (kept in src.id), so a second run of the
    // same export updates in place.
    const bySrc = new Map();
    for(const n of _lib.notes.values()) if(n.src && n.src.id) bySrc.set(n.src.id, n);

    let done = 0, failed = 0, queued = 0;
    for(const jn of parsed.notes){
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
      try{
        const r = await save(rec, 'note');
        if(r.queued) queued++;
        else if(!r.ok) failed++;
      }catch(_){ failed++; }
      done++;
      if(done % 5 === 0 || done === parsed.notes.length){
        prog.innerHTML = `<div class="nt-imp-bar"><i style="width:${Math.round(done/parsed.notes.length*100)}%"></i></div>
          <div class="muted small">${done} / ${parsed.notes.length} notes${failed?` · ${failed} failed`:''}${queued?` · ${queued} queued offline`:''}</div>`;
      }
    }

    prog.innerHTML = `<div class="nt-imp-done">
      <b>Imported ${done - failed} note${done-failed===1?'':'s'}.</b>
      ${rfail?`<div class="nt-warn small">⚠ ${rfail} attachment(s) could not be stored — their notes imported with the link left as-is.</div>`:''}
      ${failed?`<div class="nt-warn small">⚠ ${failed} note(s) failed to save. Run the import again — it updates rather than duplicates.</div>`:''}
      ${queued?`<div class="muted small">${queued} saved on this device and will sync when you’re back online.</div>`:''}
    </div>`;
    toast(`imported ${done - failed} notes`);
    setTimeout(()=>{ closeModal(); render(); }, 1200);
  }

  if(document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot);
  else boot();
})();
