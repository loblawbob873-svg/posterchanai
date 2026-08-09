/* Folder sync — the store, the scheduler and the screen.
 *
 * The three pieces below it are already done and tested elsewhere: foldersync.js decides what should
 * happen, syncrun.js decides in what order, and the platform adapter (desktop: window.pcFs) does the
 * I/O. This file is the glue: where the manifest lives, when a sweep is allowed to run, and what the
 * user sees.
 *
 * THE MANIFEST GOES THROUGH THE SERVER, and that is a deliberate exception to "prefer Nostr over a
 * server". It is the only record of what every device agreed a folder contains, so an empty read
 * written back over a full one does not lose a setting — it loses the folder, because every other
 * device then reads the missing paths as "deleted elsewhere" and trashes its local copies. That is
 * the same replaceable-doc wipe that took out a drive's files index, and what fixed it there was the
 * collapse guard in /client/files-index: a check no client build can route around. /client/sync-manifest
 * is the same guard for the same reason, which is why a folder sync needs an instance even though
 * the bytes themselves are plain encrypted Blossom blobs.
 *
 * `base` — what THIS device last agreed with — is local and never shared. Two devices have different
 * bases by definition; that is the whole point of it.
 */
(function(){
  'use strict';
  const PC = window.__PC || {};
  const S = window.PCFolderSync, RUN = window.PCSyncRun;
  const FS = () => window.pcFs || null;            // desktop only, for now — Android SAF lands next

  const CFG_KEY = () => 'pc_sync_folders_' + ((PC.me && PC.me() && PC.me().pubkey) || 'anon');
  const BASE_KEY = (id) => 'pc_sync_base_' + id;

  /* Folder pairs live ON THE DEVICE and are never synced — that is what makes "define the mapping on
   * each device" fall out for free, and it is also the only sane answer: a path that exists on a
   * laptop means nothing on a phone. */
  function folders(){
    try{ const a = JSON.parse(localStorage.getItem(CFG_KEY()) || '[]'); return Array.isArray(a) ? a : []; }
    catch(_){ return []; }
  }
  function saveFolders(list){
    try{ localStorage.setItem(CFG_KEY(), JSON.stringify(list)); }catch(_){}
  }
  function prefs(f){ return Object.assign({}, S.DEFAULT_PREFS, (f && f.prefs) || {}); }

  // ---- the store -------------------------------------------------------------------------------
  const store = {
    async _post(body){
      const auth = await PC.signAuth('sync-manifest');
      const r = await fetch('/client/sync-manifest', {
        method:'POST', headers:{'Content-Type':'application/json'},
        body: JSON.stringify(Object.assign({ pubkey: PC.me().pubkey, auth: btoa(JSON.stringify(auth)) }, body)),
      });
      const j = await r.json().catch(() => ({}));
      if(!r.ok || !j.ok) throw new Error(j.error || ('manifest ' + r.status));
      return j;
    },
    async manifest(id){
      const j = await this._post({ folder: id });
      const doc = j.manifest || {};
      return doc.paths || {};          // {n, paths} on the wire; the engine only wants the paths
    },
    // The per-device agreement. Local by definition, and a corrupt one is recoverable by resyncing —
    // it costs a full compare, never data.
    async base(id){
      try{ return JSON.parse(localStorage.getItem(BASE_KEY(id)) || '{}') || {}; }catch(_){ return {}; }
    },
    async save(id, s){
      const paths = s.manifest || {};
      const live = Object.keys(paths).filter(p => paths[p] && !paths[p].deletedAt).length;
      await this._post({ folder: id, manifest: { n: live, paths } });
      // Only after the shared manifest is safely stored — a base that runs ahead of it would make
      // this device believe in an agreement the others never saw.
      try{ localStorage.setItem(BASE_KEY(id), JSON.stringify(s.base || {})); }catch(_){}
    },
    putBlob: (bytes) => PC.syncBlobs.put(bytes),
    getBlob: (sha) => PC.syncBlobs.get(sha),
  };

  // ---- running a sweep -------------------------------------------------------------------------
  /* ONE SWEEP PER FOLDER AT A TIME. The watcher fires while a sweep is running as a matter of course
   * — the sweep's own downloads are filesystem changes — and a second sweep would diff against a
   * half-applied state, which is the one input the engine is not designed for. */
  const running = new Map();          // id -> promise
  const status = new Map();           // id -> {when, text, report}

  function deviceName(){
    let n = '';
    try{ n = localStorage.getItem('pc_sync_device') || ''; }catch(_){}
    if(!n){
      const ua = navigator.userAgent || '';
      n = /Android/i.test(ua) ? 'Android' : /Mac/i.test(ua) ? 'Mac' : /Win/i.test(ua) ? 'Windows' : 'Linux';
      try{ localStorage.setItem('pc_sync_device', n); }catch(_){}
    }
    return n;
  }

  async function power(){
    const fs = FS();
    let p = { charging: true, metered: false, online: navigator.onLine !== false };
    try{ if(fs && fs.power) p = Object.assign(p, await fs.power()); }catch(_){}
    // The browser's own battery/connection hints, where they exist, beat our default of "assume
    // plugged in" — which is right for a desktop tower and wrong for a laptop.
    try{
      const c = navigator.connection;
      if(c && typeof c.saveData === 'boolean') p.metered = p.metered || !!c.saveData;
      if(c && /^(2g|slow-2g|3g)$/.test(String(c.effectiveType||''))) p.metered = true;
    }catch(_){}
    try{
      if(navigator.getBattery){ const b = await navigator.getBattery();
        p.charging = !!b.charging; p.battery = Math.round((b.level||1) * 100); }
    }catch(_){}
    return p;
  }

  async function sweep(f, opts){
    const o = opts || {};
    if(running.has(f.id)) return running.get(f.id);
    const fs = FS();
    if(!fs) throw new Error('this device has no filesystem access');

    const p = await power();
    const decision = RUN.due(Object.assign({}, p, {
      now: Date.now(), manual: !!o.manual, dirty: !!f._dirty,
      lastSyncAt: f.lastSyncAt || 0, lastFullScanAt: f.lastFullScanAt || 0,
    }), prefs(f));
    if(!decision.run && !o.dryRun){ setStatus(f.id, decision.why); return { skipped:true, why:decision.why }; }

    const job = (async () => {
      setStatus(f.id, o.dryRun ? 'checking…' : 'syncing…');
      try{
        const rep = await RUN.sweep(fs, store, {
          id: f.id, device: deviceName(), now: Date.now(),
          excludes: f.excludes || [], maxBytes: await maxBytes(),
          hash: decision.mode === 'full', dryRun: !!o.dryRun,
        });
        if(!o.dryRun){
          f.lastSyncAt = Date.now();
          if(decision.mode === 'full') f.lastFullScanAt = Date.now();
          f._dirty = false;
          const all = folders().map(x => x.id === f.id ? f : x); saveFolders(all);
        }
        setStatus(f.id, summarise(rep, decision), rep);
        return rep;
      }catch(e){
        setStatus(f.id, 'failed: ' + ((e && e.message) || e));
        throw e;
      } finally { running.delete(f.id); }
    })();
    running.set(f.id, job);
    return job;
  }

  function summarise(rep, decision){
    if(rep.dryRun){
      const p = rep.plan || {};
      const n = (p.upload||[]).length + (p.download||[]).length + (p.deleteLocal||[]).length
              + (p.deleteRemote||[]).length + (p.conflicts||[]).length;
      return n ? (n + ' change' + (n>1?'s':'') + ' to make') : 'already in step';
    }
    const bits = [];
    if(rep.uploaded.length) bits.push(rep.uploaded.length + ' up');
    if(rep.downloaded.length) bits.push(rep.downloaded.length + ' down');
    if(rep.trashed.length) bits.push(rep.trashed.length + ' to trash');
    if(rep.conflicted.length) bits.push(rep.conflicted.length + ' conflict' + (rep.conflicted.length>1?'s':''));
    if(rep.failed.length) bits.push(rep.failed.length + ' failed');
    if(rep.skipped.length) bits.push(rep.skipped.length + ' skipped');
    if(!bits.length) return 'in step' + (decision ? ' · ' + decision.why : '');
    return bits.join(' · ');
  }
  function setStatus(id, text, report){
    status.set(id, { when: Date.now(), text, report });
    if(PC.VIEW === 'sync') paint();
  }

  // The ceiling is the SERVER's, and an admin can change it — so it is asked for, not assumed.
  let _maxBytes = null;
  async function maxBytes(){
    if(_maxBytes !== null) return _maxBytes;
    _maxBytes = 0;
    try{
      const r = await fetch('/client/config');
      const j = await r.json();
      const mb = +(j && (j.blossom_max_upload_mb || j.max_upload_mb)) || 0;
      _maxBytes = mb > 0 ? mb * 1024 * 1024 : 0;
    }catch(_){}
    return _maxBytes;
  }

  // ---- the screen ------------------------------------------------------------------------------
  function paint(){
    const feed = document.getElementById('feed'); if(!feed) return;
    const list = folders();
    const fs = FS();
    const rows = list.map(f => {
      const st = status.get(f.id) || {};
      const pr = prefs(f);
      return `<div class="sync-card" data-id="${PC.enc(f.id)}">
        <div class="sync-head"><b>${PC.enc(f.name || f.dir || 'folder')}</b>
          <span class="muted small">${PC.enc(f.dir || '')}</span></div>
        <div class="sync-status muted small">${PC.enc(st.text || 'not synced yet')}</div>
        <label class="sync-ex"><span class="muted small">Don't sync these (one per line — a folder name covers everything inside it)</span>
          <textarea class="input sync-ex-ta" rows="2" placeholder="Old&#10;*.tmp">${PC.enc((f.excludes||[]).join('\n'))}</textarea></label>
        <div class="sync-opts">
          <label><input type="checkbox" class="sync-charge"${pr.onlyWhenCharging?' checked':''}> Only when plugged in</label>
          <label><input type="checkbox" class="sync-wifi"${pr.wifiOnly?' checked':''}> Wi-Fi only</label>
        </div>
        <div class="sync-actions">
          <button class="btn btn-ghost small sync-dry">Check</button>
          <button class="btn btn-neon small sync-now">Sync now</button>
          <button class="btn btn-ghost small sync-trash">Empty trash</button>
          <button class="btn btn-ghost small danger sync-forget">Stop syncing</button>
        </div></div>`;
    }).join('');

    feed.innerHTML = `<div class="sync-view">
      <h2><svg class="ic h-ic" aria-hidden="true"><use href="#i-refresh"></use></svg>Folder sync</h2>
      ${fs ? '' : `<div class="empty">This device can't reach a folder. Folder sync needs the desktop app —
        a browser has no filesystem, and Firefox has no File System Access API at all. Your files are
        still readable here under Files.</div>`}
      <p class="muted small">Folders are kept in step across your devices, encrypted with your own key
        before they leave. Deletions go to <code>.pc-trash</code> inside the folder, never straight out.
        Where a folder lives is set per device.</p>
      ${rows || (fs ? '<div class="empty">No folders yet.</div>' : '')}
      ${fs ? '<button class="btn btn-neon" id="sync-add">Add a folder…</button>' : ''}
    </div>`;

    const add = document.getElementById('sync-add');
    if(add) add.onclick = async () => {
      try{
        const picked = await FS().pick();
        if(!picked) return;
        const list2 = folders();
        if(list2.some(x => x.id === picked.id)){ PC.toast('that folder is already syncing'); return; }
        list2.push({ id: picked.id, dir: picked.dir, name: picked.dir.split(/[/\\]/).pop(),
                     excludes: [], prefs: {}, lastSyncAt: 0, lastFullScanAt: 0 });
        saveFolders(list2); watch(picked.id); paint();
      }catch(e){ PC.toast('could not add: ' + ((e && e.message) || e)); }
    };

    feed.querySelectorAll('.sync-card').forEach(card => {
      const id = card.dataset.id;
      const get = () => folders().find(x => x.id === id);
      const put = (fn) => { const l = folders(); const i = l.findIndex(x => x.id === id);
                            if(i < 0) return; fn(l[i]); saveFolders(l); };
      card.querySelector('.sync-ex-ta').onchange = (e) => {
        put(f => { f.excludes = e.target.value.split('\n').map(s => s.trim()).filter(Boolean); });
        PC.toast('exclusions saved — they take effect on the next sync');
      };
      card.querySelector('.sync-charge').onchange = (e) =>
        put(f => { f.prefs = Object.assign({}, f.prefs, { onlyWhenCharging: e.target.checked }); });
      card.querySelector('.sync-wifi').onchange = (e) =>
        put(f => { f.prefs = Object.assign({}, f.prefs, { wifiOnly: e.target.checked }); });
      card.querySelector('.sync-dry').onclick = () => sweep(get(), { manual:true, dryRun:true }).catch(()=>{});
      card.querySelector('.sync-now').onclick = () => sweep(get(), { manual:true }).catch(()=>{});
      card.querySelector('.sync-trash').onclick = async () => {
        if(!await PC.uiConfirm('Empty this folder’s .pc-trash of anything older than 30 days?')) return;
        try{ const r = await FS().emptyTrash(id, 30); PC.toast('emptied ' + (r.removed||0) + ' day(s)'); }
        catch(e){ PC.toast('failed: ' + ((e && e.message) || e)); }
      };
      card.querySelector('.sync-forget').onclick = async () => {
        if(!await PC.uiConfirm('Stop syncing this folder?\n\nNothing is deleted — the files stay on this '
                               + 'device and on your other devices. It simply stops being kept in step.')) return;
        try{ await FS().forget(id); }catch(_){}
        saveFolders(folders().filter(x => x.id !== id));
        try{ localStorage.removeItem(BASE_KEY(id)); }catch(_){}
        paint();
      };
    });
  }

  // ---- the watcher -----------------------------------------------------------------------------
  /* A change NOTIFIER, not a timer. The adapter debounces and tells us a root moved; we only mark it
   * dirty, and the policy decides whether that is worth a sweep right now. On a phone that is the
   * difference between "sync when it matters" and a radio that never sleeps. */
  function watch(id){ const fs = FS(); if(fs && fs.watch) fs.watch(id, 4000).catch(()=>{}); }
  function startAll(){
    const fs = FS(); if(!fs) return;
    folders().forEach(f => watch(f.id));
    if(fs.onChanged) fs.onChanged((id) => {
      const l = folders(); const f = l.find(x => x.id === id); if(!f) return;
      f._dirty = true;
      sweep(f, {}).catch(()=>{});      // the policy may well decline; that is the point of asking
    });
  }

  window.PCSync = { paint, folders, sweep, startAll, store, status };
})();
