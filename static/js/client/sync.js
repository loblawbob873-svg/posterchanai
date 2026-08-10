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
 *
 * WHAT THE SERVER CAN AND CANNOT SEE. File CONTENTS are AES-256-GCM under the drive's master key
 * before they are uploaded, so a blob is ciphertext to everyone including this node. The manifest —
 * every path and size — is NIP-44 self-encrypted on top of that, so the node stores a blob it cannot
 * read either. What it does see, unavoidably: how many live entries there are (the plaintext `n`,
 * which is what the collapse guard checks), how many blobs there are and how big each is, and when
 * they arrived. And because the IV is derived from the content so identical bytes dedup, a party
 * holding a candidate file can confirm whether that exact file is stored. That is the same trade
 * every deduplicating encrypted store makes, and it is worth saying out loud rather than implying
 * the metadata is private too.
 */
(function(){
  'use strict';
  const PC = window.__PC || {};
  const S = window.PCFolderSync, RUN = window.PCSyncRun;
  const FS = () => window.pcFs || null;            // desktop only, for now — Android SAF lands next

  /* The mapping is keyed by IDENTITY, deliberately: switching account must never silently start
   * uploading this machine's documents into a different one's manifest. The cost is that a folder
   * added under another identity — or before login resolved, when me() is still null and the key
   * falls back to _anon — is not in this list. The GRANT is still there either way (the desktop keeps
   * roots in its own config, Android in the system's persisted URI permissions), so `granted` below
   * is what makes those visible instead of leaving someone staring at an empty screen wondering where
   * their folders went. */
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

  /* THE PAIR KEY — what makes two devices the same folder.
   *
   * `f.id` is the PLATFORM's handle for a directory and it is device-local by construction: a random
   * hex id on desktop, a SAF tree URI on Android. Keying the manifest on it meant every device wrote
   * and read a DIFFERENT document, so each one synced happily with itself and never saw the others.
   * Sync between devices could not work at all.
   *
   * So the manifest is keyed on a name the user gives the pair — "Documents", "Pictures" — which is
   * the same on every device by definition, while the local path stays local. That is also what makes
   * the mapping per-device: same pair, different directory, chosen on each machine.
   *
   * The server sanitises this into a d-tag (`pcai:sync:<key>`), so it is normalised the same way here
   * to keep what the user typed and what gets addressed in step. */
  function pairKey(name){
    return String(name || '').trim().replace(/[^A-Za-z0-9_-]/g, '').slice(0, 64);
  }
  /* Older entries pre-date the pair key and were keyed on the platform id. Derive one from the folder
   * NAME so two devices that both synced "Documents" land on the same manifest — which is the answer
   * they should have had all along. Their old per-device manifests are simply left behind; nothing is
   * deleted, and the first sweep after this rebuilds from what is actually on disk. */
  function keyOf(f){ return f.key || pairKey(f.name || (f.dir || '').split(/[/\\]/).pop()) || 'folder'; }

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
    /* The paths are NIP-44 self-encrypted before they leave, so the node stores a blob it cannot
     * read. The plaintext `n` beside it is the ONLY thing the server sees, and it is there because
     * the collapse guard needs a count — it does not need the names. Without this the manifest went
     * up under the user's SERVER-HELD storage key, which is the trade the calendar makes on purpose
     * (a CalDAV client sends plaintext, so the node must be able to answer it) and which a folder
     * sync has no reason to make: nothing on the server ever needs to know a filename. */
    async manifest(id){
      const j = await this._post({ folder: id });
      const doc = j.manifest || {};
      if(doc.sealed){
        try{ return JSON.parse(await PC.nip44dec(PC.me().pubkey, doc.sealed)) || {}; }
        catch(e){ throw new Error('could not decrypt the manifest — wrong key or damaged'); }
      }
      return doc.paths || {};          // pre-seal manifests, still readable
    },
    // The per-device agreement. Local by definition, and a corrupt one is recoverable by resyncing —
    // it costs a full compare, never data.
    async base(id){
      try{ return JSON.parse(localStorage.getItem(BASE_KEY(id)) || '{}') || {}; }catch(_){ return {}; }
    },
    async save(id, s){
      const paths = s.manifest || {};
      const live = Object.keys(paths).filter(p => paths[p] && !paths[p].deletedAt).length;
      const sealed = await PC.nip44enc(PC.me().pubkey, JSON.stringify(paths));
      await this._post({ folder: id, manifest: { n: live, sealed } });
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
  let granted = null;                 // what the PLATFORM says this device can reach
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
          id: f.id, key: keyOf(f), device: deviceName(), now: Date.now(),
          excludes: f.excludes || [], maxBytes: await maxBytes(),
          hash: decision.mode === 'full', dryRun: !!o.dryRun,
          // The first sweep of a Pictures folder is minutes of silence, and silence is
          // indistinguishable from a hang, a failed login or a 404 on the manifest — which is
          // exactly how this looked the first time it was tried for real.
          onProgress: (ev) => {
            const where = ev.path ? ' ' + ev.path.split('/').pop() : '';
            const of = ev.n > 1 ? ' ' + ev.i + '/' + ev.n : '';
            setStatus(f.id, ev.phase + of + where, null, true);
          },
        });
        if(!o.dryRun){
          // Tell the background checker what "synced" now looks like, or its next run compares
          // against a stale signature and notifies about changes that are already up.
          try{ if(fs.markSynced) await fs.markSynced(); }catch(_){}
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
  function setStatus(id, text, report, liveOnly){
    const prev = status.get(id) || {};
    status.set(id, { when: Date.now(), text, report: report || (liveOnly ? prev.report : null),
                     busy: !!liveOnly });
    if(PC.VIEW !== 'sync') return;
    // A running sweep repaints its one line rather than the whole screen: rebuilding the cards would
    // throw away a half-typed exclusion list and the focus with it, several times a second.
    const el = document.querySelector('.sync-card[data-id="' + (window.CSS && CSS.escape ? CSS.escape(id) : id) + '"] .sync-status');
    if(liveOnly && el){ el.textContent = text; return; }
    paint();
  }

  /* What actually happened, in files. "3 up · 1 conflict" is the headline; this is the part that
   * lets someone believe it — and the part that makes a failure actionable instead of a word. */
  function details(rep){
    if(!rep) return '';
    const cap = 12;
    const grp = (label, items, fmt) => {
      if(!items || !items.length) return '';
      const shown = items.slice(0, cap).map(fmt).map(t => '<li>' + PC.enc(t) + '</li>').join('');
      const more = items.length > cap ? '<li class="muted">…and ' + (items.length - cap) + ' more</li>' : '';
      return '<div class="sync-grp"><b>' + PC.enc(label) + '</b><ul>' + shown + more + '</ul></div>';
    };
    const p = rep.plan || {};
    if(rep.dryRun){
      return '<div class="sync-details">'
        + grp('Would upload', p.upload, a => a.path + ' — ' + a.why)
        + grp('Would download', p.download, a => a.path + ' — ' + a.why)
        + grp('Would move to trash', p.deleteLocal, a => a.path + ' — ' + a.why)
        + grp('Would remove from the cloud', p.deleteRemote, a => a.path + ' — ' + a.why)
        + grp('Conflicts', p.conflicts, a => a.path + ' → ' + a.keepAs)
        + '</div>';
    }
    return '<div class="sync-details">'
      + grp('Failed', rep.failed, a => a.path + ' — ' + a.what + ': ' + a.error)
      + grp('Skipped', rep.skipped, a => a.path + ' — ' + a.why)
      + grp('Conflicts kept', rep.conflicted, a => a.path + ' → ' + a.keptAs)
      + grp('Uploaded', rep.uploaded, a => a)
      + grp('Downloaded', rep.downloaded, a => a)
      + grp('Moved to trash', rep.trashed, a => a.path + ' → ' + a.to)
      + '</div>';
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
    // Ask the platform once per visit, then repaint. Cheap (a config read on desktop, a permissions
    // query on Android) and it is the only way to notice a grant that this identity has not mapped.
    if(fs && granted === null){
      // `asked` is separate from the ANSWER on purpose. Setting granted=[] as a "in flight" marker
      // made the very first paint treat every mapped folder as one whose grant had been withdrawn —
      // an empty list is a real answer meaning "this device can reach nothing", and it must not be
      // borrowed to mean "we have not looked yet". A repaint corrected it a moment later, which is
      // exactly the kind of flicker that reads as "my folder disappeared".
      granted = undefined;
      fs.list().then(r => { granted = r || []; if(PC.VIEW === 'sync') paint(); })
               .catch(() => { granted = null; });
    }
    const mapped = new Set(list.map(f => f.id));
    const orphans = (Array.isArray(granted) ? granted : []).filter(g => !mapped.has(g.id));
    const rows = list.map(f => {
      const st = status.get(f.id) || {};
      const pr = prefs(f);
      // A grant can be revoked in system settings, or the drive can be gone. Saying so beats
      // "unknown sync folder" on every sweep forever.
      // Only when the platform has actually answered — never while the question is in flight.
      const lost = Array.isArray(granted) && !granted.some(g => g.id === f.id);
      return `<div class="sync-card" data-id="${PC.enc(f.id)}">
        <div class="sync-head"><b>${PC.enc(keyOf(f))}</b>
          <span class="muted small">${PC.enc(f.dir || '')}</span>
          <span class="sync-pair muted small">pairs with “${PC.enc(keyOf(f))}” on your other devices</span></div>
        <div class="sync-status muted small">${lost ? 'access to this folder was withdrawn — remove it and add it again'
          : PC.enc(st.text || 'not synced yet')}</div>
        ${details(st.report)}
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
      ${fs ? '' : `<div class="empty">This device can't reach a folder. Folder sync needs the desktop app —
        a browser has no filesystem, and Firefox has no File System Access API at all. Your files are
        still readable here under Files.</div>`}
      <p class="muted small">Folders are kept in step across your devices, encrypted with your own key
        before they leave. Deletions go to <code>.pc-trash</code> inside the folder, never straight out.
        Where a folder lives is set per device.</p>
      ${(!PC.me || !PC.me()) ? '<div class="empty">Sign in to sync — folder mappings belong to an '
        + 'account, so that switching identity never uploads this machine\'s files into someone '
        + 'else\'s.</div>' : ''}
      ${rows || (fs ? '<div class="empty">No folders syncing under this account yet.</div>' : '')}
      ${orphans.length ? `<div class="sync-orphans"><b>Already allowed on this device</b>
        <p class="muted small">You granted access to these, but they are not syncing under the account
        you are signed in with now. Nothing has been lost — pick one up to start syncing it here.</p>
        ${orphans.map(g => `<div class="sync-orphan"><span>${PC.enc(g.dir || g.id)}</span>
          <button class="btn btn-ghost small sync-adopt" data-oid="${PC.enc(g.id)}"
                  data-odir="${PC.enc(g.dir || '')}">Sync here</button></div>`).join('')}</div>` : ''}
      ${fs ? '<button class="btn btn-neon" id="sync-add">Add a folder…</button>' : ''}
      ${(fs && fs.backgroundCheck) ? `<label class="sync-bg"><input type="checkbox" id="sync-bg"${
          ClientSettings.get('syncBgCheck', false) ? ' checked' : ''}>
        <span>Watch for changes in the background<br>
        <span class="muted small">Checks while charging on Wi-Fi and tells you when there is something to
        sync. It cannot upload on its own: every upload is signed by your key, and with a remote signer
        that key is not on this device — so opening the app is what syncs.</span></span></label>` : ''}
    </div>`;

    { const bg = document.getElementById('sync-bg');
      if(bg) bg.onchange = async () => {
        ClientSettings.set('syncBgCheck', bg.checked);
        try{ await FS().backgroundCheck(bg.checked, 180); }
        catch(e){ PC.toast('could not change that: ' + ((e && e.message) || e)); }
      }; }

    feed.querySelectorAll('.sync-adopt').forEach(b => { b.onclick = () => {
      const l = folders();
      if(l.some(x => x.id === b.dataset.oid)) return;
      const guess = pairKey((b.dataset.odir || '').split(/[/\\]/).pop()) || 'Folder';
      Promise.resolve(PC.uiPrompt('Name this folder — use the SAME name on your other devices.', guess))
        .then(ans => {
          const key = pairKey(ans || '');
          if(!key || key.length < 4) return;
          l.push({ id: b.dataset.oid, key, dir: b.dataset.odir, name: key,
                   excludes: [], prefs: {}, lastSyncAt: 0, lastFullScanAt: 0 });
          saveFolders(l); watch(b.dataset.oid); paint();
        });
    }; });

    const add = document.getElementById('sync-add');
    if(add) add.onclick = async () => {
      try{
        const picked = await FS().pick();
        if(!picked) return;
        const list2 = folders();
        if(list2.some(x => x.id === picked.id)){ PC.toast('that folder is already syncing'); return; }
        const guess = pairKey(picked.dir.split(/[/\\]/).pop()) || 'Folder';
        /* The name IS the pairing. Two devices sync together because they used the same one, so this
         * asks rather than inventing a hidden id — and says so, because "Documents" on the laptop
         * meeting "Docs" on the phone is two folders, not one, and nothing later would explain why. */
        const key = pairKey(await PC.uiPrompt(
          'Name this folder. Use the SAME name on your other devices to sync them together — the '
          + 'folder can be anywhere on each one.', guess) || '');
        if(!key) return;
        if(key.length < 4){ PC.toast('use at least 4 letters or digits'); return; }
        list2.push({ id: picked.id, key, dir: picked.dir, name: key,
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

  /* NOTICING A CHANGE, on platforms that will and will not tell you about one.
   *
   * Desktop gets a real recursive watcher, so a saved file is picked up seconds later. Android's SAF
   * has no tree notification worth having, and polling one is the battery bug the policy exists to
   * avoid — so there, and anywhere else the watcher is absent, changes are noticed at the moments the
   * platform DOES hand us for free:
   *
   *   resume / visible   coming back to the app. On a phone this is the big one: it is when you have
   *                      just finished taking the photos, and it costs nothing to check.
   *   online             a laptop that was offline in a cafe and is now not.
   *   heartbeat          a long, visible-only interval, so "we are now on Wi-Fi" or "we are now
   *                      charging" is eventually noticed rather than waiting for a file to move.
   *                      Gated on document visibility, so a backgrounded tab does nothing at all.
   *
   * Every one of these only ASKS. shouldSync still decides, so on battery, on cellular, or ten
   * seconds after the last sweep the answer is no and nothing spins up. */
  const HEARTBEAT_MS = 15 * 60 * 1000;
  let _nudgeT = null;
  function nudge(why){
    clearTimeout(_nudgeT);
    // Coalesced: resume, visible and online all fire together when a laptop lid opens.
    _nudgeT = setTimeout(() => {
      if(document.hidden) return;
      folders().forEach(f => { sweep(f, {}).catch(()=>{}); });
    }, 1500);
  }

  function startAll(){
    const fs = FS(); if(!fs) return;
    folders().forEach(f => watch(f.id));
    if(fs.onChanged) fs.onChanged((id) => {
      const l = folders(); const f = l.find(x => x.id === id); if(!f) return;
      f._dirty = true;
      sweep(f, {}).catch(()=>{});      // the policy may well decline; that is the point of asking
    });
    document.addEventListener('visibilitychange', () => { if(!document.hidden) nudge('visible'); });
    window.addEventListener('online', () => nudge('online'));
    window.addEventListener('focus', () => nudge('focus'));
    // Capacitor's own resume is more reliable than visibilitychange in a WebView that the OS froze.
    try{
      if(window.Capacitor && window.Capacitor.Plugins && window.Capacitor.Plugins.App){
        window.Capacitor.Plugins.App.addListener('appStateChange', (st) => { if(st && st.isActive) nudge('resume'); });
      }
    }catch(_){}
    setInterval(() => { if(!document.hidden) nudge('heartbeat'); }, HEARTBEAT_MS);
    // Re-assert the stored preference on every start. Scheduling is idempotent on the Android side
    // (ExistingPeriodicWorkPolicy.KEEP), so this cannot reset the period and starve a job that has
    // been waiting for a charger.
    try{ if(fs.backgroundCheck) fs.backgroundCheck(!!ClientSettings.get('syncBgCheck', false), 180); }catch(_){}
    nudge('startup');
  }

  window.PCSync = { paint, folders, sweep, startAll, store, status };
})();
