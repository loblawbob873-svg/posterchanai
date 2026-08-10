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

  /* WHAT THIS DIRECTORY WAS PAIRED AS, remembered across "Stop syncing".
   *
   * Stopping removes the mapping, so picking the same folder up again asked "Name this folder — use
   * the SAME name on your other devices" about a folder that already HAS a name on the other
   * devices. It reads as a new pairing when it is a resumption, and getting it wrong quietly makes a
   * second folder that never meets the first.
   *
   * Kept per identity, keyed by the platform's handle for the directory (and by the directory path,
   * which is what survives a grant being re-issued with a fresh handle). Deliberately not deleted
   * when a folder is forgotten: remembering the NAME costs nothing and is the whole point. */
  const PAIRED_KEY = () => 'pc_sync_paired_' + ((PC.me && PC.me() && PC.me().pubkey) || 'anon');
  function pairedNames(){
    try{ const o = JSON.parse(localStorage.getItem(PAIRED_KEY()) || '{}'); return (o && typeof o === 'object') ? o : {}; }
    catch(_){ return {}; }
  }
  function rememberPair(id, dir, key){
    if(!key) return;
    const o = pairedNames();
    if(id) o[id] = key;
    if(dir) o['dir:' + dir] = key;
    try{ localStorage.setItem(PAIRED_KEY(), JSON.stringify(o)); }catch(_){}
  }
  function recallPair(id, dir){
    const o = pairedNames();
    return o[id] || o['dir:' + (dir || '')] || '';
  }

  /* Past this the paths leave the document for an encrypted Blossom blob. 45 KB is the FILES INDEX's
   * threshold, chosen for the same reason and kept identical on purpose: NIP-44 refuses a plaintext
   * over 65535 bytes, and the margin is for the JSON growing between the check and the encrypt. */
  const MANIFEST_INLINE_MAX = 45000;

  /* ---- `base`: this device's agreement, in IndexedDB ------------------------------------------
   *
   * It used to live in localStorage under a try/catch that swallowed everything. A base is the same
   * size as the manifest — ~174 bytes per file, so ~2.6 MB for a 15790-file folder — against a 5 MB
   * localStorage budget shared with every other thing this client stores. A quota failure there is
   * silent, and a base that does not persist means the next sweep re-reads the whole folder as new:
   * the same infinite-resync as a failed save, from a different cause, with nothing said either way.
   *
   * So: IndexedDB, which is sized for this, and a failure that is REPORTED. Reads still fall back to
   * the old localStorage key, so a device that already has an agreement keeps it and does not
   * re-upload its folder once on upgrade. */
  const _IDB = { DB:'pcsync', VER:1, STORE:'base', _db:null,
    _open(){
      if(this._db) return Promise.resolve(this._db);
      return new Promise((res, rej) => {
        let rq; try{ rq = indexedDB.open(this.DB, this.VER); }catch(e){ return rej(e); }
        rq.onupgradeneeded = () => { const db = rq.result;
          if(!db.objectStoreNames.contains(this.STORE)) db.createObjectStore(this.STORE); };
        rq.onsuccess = () => { this._db = rq.result; res(this._db); };
        rq.onerror = () => rej(rq.error || new Error('indexeddb unavailable'));
      });
    },
    async _tx(mode, fn){
      const db = await this._open();
      return new Promise((res, rej) => {
        const tx = db.transaction(this.STORE, mode), st = tx.objectStore(this.STORE);
        let out; try{ out = fn(st); }catch(e){ return rej(e); }
        // `'result' in out`, not `out.result !== undefined`: a MISS gives a request whose result is
        // undefined, and unwrapping on truthiness returns the request OBJECT instead — which reads
        // as a base containing one file called "result", so the localStorage fallback below is never
        // reached and every existing device re-uploads its whole folder once.
        tx.oncomplete = () => res(out && typeof out === 'object' && ('result' in out) ? out.result : out);
        tx.onerror = () => rej(tx.error); tx.onabort = () => rej(tx.error);
      });
    },
  };
  async function _loadBase(key){
    try{
      const v = await _IDB._tx('readonly', st => st.get(key));
      if(v && typeof v === 'object') return v;
    }catch(e){ console.warn('folder sync: could not read the local agreement', e); }
    // Pre-IndexedDB devices, and the small folders that fitted. Read once; the next save moves it.
    try{ return JSON.parse(localStorage.getItem(BASE_KEY(key)) || '{}') || {}; }catch(_){ return {}; }
  }
  async function _saveBase(key, base){
    // NOT swallowed. A base that silently fails to persist is an infinite resync, and the only way
    // anyone would ever find out is by watching their upload counter start again from one.
    await _IDB._tx('readwrite', st => st.put(base, key));
    try{ localStorage.removeItem(BASE_KEY(key)); }catch(_){}   // the old copy is now stale, not a fallback
  }
  async function _dropBase(key){
    try{ await _IDB._tx('readwrite', st => st.delete(key)); }catch(_){}
    try{ localStorage.removeItem(BASE_KEY(key)); }catch(_){}
  }

  // ---- the store -------------------------------------------------------------------------------
  const store = {
    async _post(body){
      const auth = await PC.signAuth('sync-manifest');
      const r = await fetch('/client/sync-manifest', {
        method:'POST', headers:{'Content-Type':'application/json'},
        body: JSON.stringify(Object.assign({ pubkey: PC.me().pubkey, auth: btoa(JSON.stringify(auth)) }, body)),
      });
      const j = await r.json().catch(() => ({}));
      if(r.status === 409 && j && j.collapse){
        const e = new Error(j.error || 'refused'); e.collapse = j; throw e;
      }
      if(!r.ok || !j.ok) throw new Error(j.error || ('manifest ' + r.status));
      return j;
    },
    /* The paths are NIP-44 self-encrypted before they leave, so the node stores a blob it cannot
     * read. The plaintext `n` beside it is the ONLY thing the server sees, and it is there because
     * the collapse guard needs a count — it does not need the names. Without this the manifest went
     * up under the user's SERVER-HELD storage key, which is the trade the calendar makes on purpose
     * (a CalDAV client sends plaintext, so the node must be able to answer it) and which a folder
     * sync has no reason to make: nothing on the server ever needs to know a filename. */
    /* The decrypted paths, cached by the POINTER they came from.
     *
     * Merging on save re-reads the manifest, and a sweep checkpoints about twenty times — so a big
     * folder was fetching and AES-decrypting a three-megabyte blob twenty times per sweep, then
     * parsing it, for an answer that had usually not changed. The document names the blob it points
     * at, and that name is plaintext, so an unchanged pointer means unchanged paths: the round trip
     * for the small document still happens (it is what detects another device's write), and the
     * expensive half is skipped.
     *
     * One entry. Two folders alternating would evict each other, which costs exactly what this used
     * to cost and never more. */
    _mcache: { sha:null, paths:null },
    async manifest(id){
      const j = await this._post({ folder: id });
      const doc = j.manifest || {};
      if(doc.pathsSha && this._mcache.sha === doc.pathsSha && this._mcache.paths){
        return JSON.parse(JSON.stringify(this._mcache.paths));   // a copy: callers mutate what they get
      }
      // v2 first: the paths are an encrypted Blossom blob. See save() for why they had to leave the
      // document, and for the marker `sealed` carries so an older build cannot misread this.
      if(doc.pathsSha){
        /* FETCH AND DECRYPT FAIL FOR OPPOSITE REASONS, so they must not share a message. "Stored but
         * unreadable" told someone nothing and sent an evening into checking the server, which had
         * the blob the whole time: present, kept, no expiry. One of these is a media-server or
         * network problem and the other is a key problem, and the fix for each is the other's
         * mistake. */
        let bytes;
        try{ bytes = await PC.syncBlobs.get(doc.pathsSha); }
        catch(e){
          const m = String((e && e.message) || e);
          if(/OperationError|decrypt|importKey|drive key/i.test(m)){
            throw new Error('this device cannot decrypt your folder list — it is stored and intact, '
                            + 'but the drive key here cannot open it (' + m + ')');
          }
          throw new Error('could not fetch your folder list from ' + (PC.mediaServer ? PC.mediaServer() : 'the media server')
                          + ' (' + m + '). The list itself is fine — check this device is pointed at the '
                          + 'same media server as your others.');
        }
        try{
          const paths = JSON.parse(new TextDecoder().decode(bytes)) || {};
          this._mcache = { sha: doc.pathsSha, paths };
          return JSON.parse(JSON.stringify(paths));
        }
        catch(e){ throw new Error('the stored folder list is damaged'); }
      }
      if(doc.sealed){
        try{ return JSON.parse(await PC.nip44dec(PC.me().pubkey, doc.sealed)) || {}; }
        catch(e){ throw new Error('could not decrypt the manifest — wrong key or damaged'); }
      }
      return doc.paths || {};          // pre-seal manifests, still readable
    },
    // The per-device agreement. Local by definition, and a corrupt one is recoverable by resyncing —
    // it costs a full compare, never data.
    base(id){ return _loadBase(id); },
    /* NIP-44 REFUSES A PLAINTEXT OVER 65535 BYTES, and a manifest entry is ~174 of them — so this
     * document could hold about 376 files, and a folder with more than that COULD NOT BE SAVED AT
     * ALL. Measured on a real 15790-file folder: every sweep uploaded everything, the save threw at
     * the very end, `base` was never written, and the next sweep started from the beginning. For
     * ever. The sync looked like it worked, because everything except the last step did.
     *
     * So past ~45 KB the paths move into an encrypted Blossom blob and the document keeps a pointer
     * — exactly what the FILES INDEX does, for exactly this reason (`indexSha` in FilesIdx.push).
     * The plaintext `n` stays either way: it is what the server's collapse guard reads, and it is
     * the only thing it can read.
     *
     * `sealed` IS STILL SET, to a string that cannot possibly decrypt, and that is not decoration.
     * A client older than this change looks for `sealed`, falls back to `doc.paths`, and would read
     * a v2 document as an EMPTY manifest — which is not a harmless misread: an empty remote means
     * every file is "deleted elsewhere", and that device would move all of them to its trash and
     * publish tombstones the others would honour. With the marker present it throws instead, the
     * sweep fails, and nothing is touched. (nostr-tools rejects a payload under 132 chars outright,
     * so this is a deterministic failure, not a hopeful one.) */
    /* MERGE, DO NOT OVERWRITE, when this sweep knows which paths it changed.
     *
     * The manifest is one replaceable document per folder, and a sweep's copy of it is a snapshot
     * taken when that sweep started. Two devices syncing the same folder at once therefore each hold
     * a stale copy, and writing it whole means the later save erases every path the other added —
     * silently, because the blobs are still there and only the entries are gone.
     *
     * Re-reading here costs one round trip per checkpoint (about twenty in a big first sync) and
     * turns that into a merge: whatever the manifest holds NOW, plus the paths this sweep actually
     * touched. A read that FAILS falls back to writing our snapshot, which is what it did before —
     * worse than merging, better than not saving at all, and the collapse guard still stands behind
     * it because a merge can only ever add. */
    async save(id, s){
      let paths = s.manifest || {};
      if(Array.isArray(s.touched) && s.touched.length){
        try{
          const fresh = await this.manifest(id);
          if(fresh && typeof fresh === 'object'){
            const merged = Object.assign({}, fresh);
            for(const p of s.touched) if(paths[p] !== undefined) merged[p] = paths[p];
            paths = merged;
          }
        }catch(_){ /* keep our snapshot — see above */ }
      }
      const live = Object.keys(paths).filter(p => paths[p] && !paths[p].deletedAt).length;
      const json = JSON.stringify(paths);
      const doc = { n: live };
      if(json.length < MANIFEST_INLINE_MAX){
        doc.sealed = await PC.nip44enc(PC.me().pubkey, json);
      } else {
        // put() may answer {sha, existed} or a bare sha — normalise, or the pointer becomes an
        // object and every device reads a manifest it cannot find.
        const put = await PC.syncBlobs.put(new TextEncoder().encode(json));
        doc.pathsSha = (put && typeof put === 'object') ? put.sha : put;
        doc.sealed = 'v2:' + doc.pathsSha;      // the marker above — deliberately undecryptable
      }
      try{
        await this._post({ folder: id, manifest: doc });
      }catch(e){
        /* THE SERVER REFUSED A SHRINK. It holds a count and nothing else, so a deliberate mass
         * delete and a broken client about to empty the folder on every device look identical from
         * there — which is exactly why the guard is server-side. THIS side is not guessing: `removed`
         * is how many paths this sweep deleted, and if it accounts for the shrink then the write is
         * precisely what the user asked for and there is nothing to ask about.
         *
         * Without this the guard makes a legitimate mass delete IMPOSSIBLE: the save fails, the
         * agreement is never written, and every sweep from then on proposes the same delete and is
         * refused again. */
        if(!e || !e.collapse) throw e;
        const c = e.collapse, shrink = Math.max(0, (+c.old || 0) - (+c.new || 0));
        const removed = +(s.removed || 0);
        if(!(removed > 0 && removed >= shrink)){
          const ok = await PC.uiConfirm('“' + id + '” is about to go from ' + c.old + ' files to '
            + c.new + ' on every device.\n\nThis device only deleted ' + removed
            + '. If you did not expect that, cancel — nothing has been changed anywhere yet.');
          if(!ok) throw new Error('not saved — the change was refused (' + c.old + ' → ' + c.new + ')');
        }
        await this._post({ folder: id, manifest: doc, force: true });
      }
      // Only after the shared manifest is safely stored — a base that runs ahead of it would make
      // this device believe in an agreement the others never saw. AWAITED: an unawaited write is a
      // save that reports success before it has one, and its failure is an unhandled rejection
      // nobody sees.
      await _saveBase(id, s.base || {});
    },
    /* The FILE's hash, for the manifest's content identity — distinct from the blob address, which is
     * the hash of the ciphertext. Provided by the store because syncrun.js has no crypto of its own. */
    hashBytes: async (bytes) => {
      const h = await crypto.subtle.digest('SHA-256', bytes);
      return [...new Uint8Array(h)].map(b => b.toString(16).padStart(2, '0')).join('');
    },
    blobSha: (PC.syncBlobs && PC.syncBlobs.blobSha) ? (bytes) => PC.syncBlobs.blobSha(bytes) : null,
    chunkShas: (PC.syncBlobs && PC.syncBlobs.chunkShas)
      ? (read, size, cs) => PC.syncBlobs.chunkShas(read, size, cs) : null,
    putBlob: (bytes) => PC.syncBlobs.put(bytes),
    getBlob: (sha) => PC.syncBlobs.get(sha),
    // The chunked pair. Present only when the client build has them, so an older bundle simply does
    // not take the chunked path rather than calling something undefined.
    putParts: PC.syncBlobs && PC.syncBlobs.putParts
      ? (read, size, onProgress, cs) => PC.syncBlobs.putParts(read, size, onProgress, cs) : null,
    getParts: PC.syncBlobs && PC.syncBlobs.getParts
      ? (chunks, write) => PC.syncBlobs.getParts(chunks, write) : null,
  };

  /* ---- WHAT THIS ACCOUNT SYNCS, as opposed to what THIS DEVICE maps ---------------------------
   *
   * The mapping is device-local by necessity — a path on a laptop means nothing on a phone — but it
   * was device-local with no way back: reported after a Windows app update, "my existing Folder sync
   * was no longer there". A bundled app that changes its storage origin, a reinstall, a cleared
   * profile, or simply painting this screen before the signer has resolved all produce the same
   * screen: "No folders syncing under this account yet", about an account that syncs two folders.
   *
   * The manifests know better, and they are on the relay. /client/sync-folders lists the pair keys
   * this account actually has, so a device with no mapping can OFFER them back — point "Documents"
   * at a directory here and it rejoins the pair it always belonged to. Nothing is re-uploaded: the
   * first sweep finds the same bytes on both sides and records agreement (see the empty-base rule in
   * foldersync.js, without which this would conflict every file instead).
   *
   * Cached with a TTL because it is drawn on every repaint, and repaints happen per keystroke of an
   * exclusion list. A failure is remembered as a FAILURE, never as "this account syncs nothing" —
   * the whole point of this section is that an empty answer used to be a lie. */
  let _acct = null, _acctAt = 0, _acctBusy = false;
  const _ACCT_TTL = 120000, _ACCT_RETRY = 20000;
  async function accountFolders(){
    if(!PC.me || !PC.me() || !PC.me().pubkey){ _acct = null; return false; }
    const age = Date.now() - _acctAt;
    if(_acctBusy || (_acct !== null && age < (_acct === 'error' ? _ACCT_RETRY : _ACCT_TTL))) return false;
    _acctBusy = true; _acctAt = Date.now();
    try{
      const auth = await PC.signAuth('sync-folders');
      const r = await fetch('/client/sync-folders', { method:'POST', headers:{'Content-Type':'application/json'},
        body: JSON.stringify({ pubkey: PC.me().pubkey, auth: btoa(JSON.stringify(auth)) }) });
      const j = await r.json().catch(() => ({}));
      _acct = (r.ok && j && j.ok && Array.isArray(j.folders)) ? j.folders : 'error';
    }catch(_){ _acct = 'error'; }
    _acctBusy = false;
    return true;
  }
  // What this account syncs that this device has no directory for.
  function unmapped(){
    if(!Array.isArray(_acct)) return [];
    const mine = new Set(folders().map(keyOf));
    return _acct.filter(f => f && f.key && !mine.has(f.key));
  }

  // ---- running a sweep -------------------------------------------------------------------------
  /* ONE SWEEP PER FOLDER AT A TIME. The watcher fires while a sweep is running as a matter of course
   * — the sweep's own downloads are filesystem changes — and a second sweep would diff against a
   * half-applied state, which is the one input the engine is not designed for. */
  let granted = null;                 // what the PLATFORM says this device can reach
  const running = new Map();          // id -> promise
  // Folders asked to stop. Read by the sweep between files, so Pause takes effect on the run that is
  // actually happening rather than the one after it.
  const stopping = new Set();
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
      now: Date.now(), manual: !!o.manual, deep: !!o.deep, dirty: !!f._dirty,
      lastSyncAt: f.lastSyncAt || 0, lastFullScanAt: f.lastFullScanAt || 0,
    }), prefs(f));
    if(!decision.run && !o.dryRun){ setStatus(f.id, decision.why); return { skipped:true, why:decision.why }; }

    const job = (async () => {
      setStatus(f.id, o.dryRun ? 'checking…' : 'syncing…');
      try{
        const rep = await RUN.sweep(fs, store, {
          id: f.id, key: keyOf(f), device: deviceName(), now: Date.now(),
          excludes: f.excludes || [], maxBytes: await maxBytes(),
          shouldStop: () => stopping.has(f.id),
          // The platform decides how much it can hold at once; see fs-android.js.
          chunkBytes: (FS() && FS().chunkBytes) || 0,
          /* Above ONE CHUNK a file goes up in pieces. This used to be 64 MB, which left every file
           * under it on the whole-file path — and a 60 MB photo there costs ~240 MB of renderer
           * memory, which is what kept killing the window on a big Pictures folder. Chunking is the
           * normal path now, not the exception for enormous files: past 16 MB nothing is ever held
           * whole, so no single file can spike memory however large it is. */
          chunkAbove: 16 * 1024 * 1024,
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
    // Said out loud, because "900 up" for files that were never sent is how a working first sweep
    // gets mistaken for the resync bug it is recovering from.
    if(rep.alreadyStored) bits.push(rep.alreadyStored + ' already stored');
    // A checkpoint that could not be stored means the next sweep repeats this work. Say so — the
    // alternative is a progress bar that starts at one again with no explanation anywhere.
    if(rep.checkpointFailed) bits.push('couldn’t save progress (' + rep.checkpointFailed + ')');
    /* A FINISHED SWEEP DOES NOT BORROW A REASON FROM THE POLICY. `decision.why` answers "why is this
     * running, or not" — "waiting for Wi-Fi", "on battery — changed files only", "you asked for it" —
     * and those belong on a sweep that was SKIPPED, which is where setStatus already puts them.
     * Pasted after a completed one it produced "in step · you asked for it", which reads as a
     * non-answer to a question nobody asked. What someone wants here is what the sweep found. */
    if(!bits.length) return rep.unchanged
      ? ('in step · nothing to sync (' + rep.unchanged + ' file' + (rep.unchanged === 1 ? '' : 's') + ' checked)')
      : 'in step · nothing to sync';
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

  /* THE CEILING IS THE BROWSER'S, NOT THE SERVER'S, AND THAT WAS THE BUG.
   *
   * The admin setting is what the server will ACCEPT — 5 GB on this deployment. What the client can
   * carry is a different number entirely: sending one file holds the plaintext, the AES-GCM
   * ciphertext and the Blob at the same time, and hashes the result, so a single file costs three to
   * four times its size in renderer memory. A 1.9 GB document therefore asks for ~7 GB, and what
   * happens is not an error — the render process is killed. In the desktop app that is a BLACK
   * WINDOW, and on the way down its in-flight fetches fail in a way the console reports as CORS,
   * which is what sent this in the wrong direction for an hour.
   *
   * So the sweep takes the LOWER of the two. Over it, the file is REPORTED as skipped (syncrun does
   * that already, and "reported, never silent" is the rule) rather than taking the app down with it.
   * Streaming the encrypt and upload would raise this a lot, and until something does, a ceiling the
   * process survives beats a ceiling the server would allow. */
  const SYNC_MAX_BYTES = 256 * 1024 * 1024;
  /* ...and much lower where the file cannot be chunked at all. Without slice I/O the whole file, its
   * ciphertext and the upload body are all in memory at once, and an Android WebView has far less
   * headroom than Electron: a tablet crashed on a Pictures folder that a desktop swallowed. Once a
   * platform gains readPart this stops applying, because chunking makes size irrelevant. */
  const SYNC_MAX_UNCHUNKED = 32 * 1024 * 1024;
  let _maxBytes = null;
  async function maxBytes(){
    if(_maxBytes !== null) return _maxBytes;
    let server = 0;
    try{
      const r = await fetch('/client/config');
      const j = await r.json();
      const mb = +(j && (j.blossom_max_upload_mb || j.max_upload_mb)) || 0;
      server = mb > 0 ? mb * 1024 * 1024 : 0;
    }catch(_){}
    const fs = FS();
    const ceiling = (fs && typeof fs.readPart === 'function') ? SYNC_MAX_BYTES : SYNC_MAX_UNCHUNKED;
    _maxBytes = server > 0 ? Math.min(server, ceiling) : ceiling;
    return _maxBytes;
  }

  /* ---- tidying up conflict copies --------------------------------------------------------------
   *
   * Several rounds of getting content identity wrong produced conflict copies of files that were
   * never different. They are ordinary files now and nothing removes them on its own.
   *
   * The engine decides WHICH are redundant, on a real content identity and never on size+mtime (see
   * redundantConflicts). This only carries the answer out: trash the copies on THIS device, and let
   * the ordinary sweep tell the others. That is deliberate — nothing here writes a manifest, invents
   * a tombstone or touches another device. It removes local files the way the sweep does, into
   * `.pc-trash`, and the next sweep reads that as "deleted here" and propagates it through the same
   * path every other deletion takes.
   *
   * So the worst case is the worst case of a normal delete, and everything is still on disk. */
  async function conflictCleanup(f, opts){
    const fs = FS();
    if(!fs) throw new Error('this device has no filesystem access');
    const key = keyOf(f);
    const man = await store.manifest(key);
    const list = S.redundantConflicts(man);
    if(opts && opts.dryRun) return { list, bytes: list.reduce((n, x) => n + (x.size || 0), 0) };

    let moved = 0, absent = 0;
    const failed = [], tombstone = [];
    for(const item of list){
      try{ await fs.trash(f.id, item.path, Date.now()); moved++; }
      catch(e){
        const msg = String((e && e.message) || e);
        if(/ENOENT|not found|no such/i.test(msg)){ absent++; tombstone.push(item.path); }
        else failed.push({ path:item.path, error:msg });
      }
    }
    /* COPIES THIS DEVICE DOES NOT HOLD are marked deleted directly, rather than downloaded first.
     *
     * The alternative is genuinely worse: to remove a thousand copies made on a phone, every other
     * device would have to fetch all thousand purely so that one of them could delete them again.
     *
     * A tombstone here is the same record a local deletion would have produced — the devices that DO
     * hold the file move their copy into `.pc-trash` on their next sweep, exactly as they would for
     * any other deletion, and nothing is erased anywhere. It goes through the ordinary save, so the
     * merge and the server's collapse guard both still apply. */
    if(tombstone.length){
      const now = Date.now();
      const next = Object.assign({}, man);
      for(const path of tombstone) next[path] = { deletedAt: now };
      await store.save(key, { manifest: next, base: await store.base(key),
                              touched: tombstone, removed: tombstone.length });
    }
    return { list, moved, absent, failed, tombstoned: tombstone.length };
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
    /* What the ACCOUNT syncs that this device cannot see. Asked once per visit and repainted when it
     * lands — never awaited, because this screen must draw immediately and the answer is an extra. */
    const elsewhere = unmapped();
    accountFolders().then(changed => { if(changed && PC.VIEW === 'sync') paint(); });
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
        <div class="sync-status muted small">${lost ? 'this device can’t reach that folder any more — nothing has been lost, point it at the folder again'
          : PC.enc(st.text || 'not synced yet')}</div>
        ${lost ? '<div class="sync-actions"><button class="btn btn-neon small sync-relink">Point at the folder again…</button></div>' : ''}
        ${details(st.report)}
        <label class="sync-ex"><span class="muted small">Don't sync these (one per line — a folder name covers everything inside it)</span>
          <textarea class="input sync-ex-ta" rows="2" placeholder="Old&#10;*.tmp">${PC.enc((f.excludes||[]).join('\n'))}</textarea></label>
        <div class="sync-opts">
          <label><input type="checkbox" class="sync-charge"${pr.onlyWhenCharging?' checked':''}> Only when plugged in</label>
          <label><input type="checkbox" class="sync-wifi"${pr.wifiOnly?' checked':''}> Wi-Fi only</label>
        </div>
        ${pr.paused ? `<div class="sync-new"><b>Not syncing yet.</b>
          <span class="muted small">Nothing has been uploaded. Add anything you want left out below —
          a folder name covers everything inside it — then press Start. You can change it later.</span></div>` : ''}
        <div class="sync-actions">
          <button class="btn btn-ghost small sync-dry">Check</button>
          ${pr.paused ? '<button class="btn btn-neon small sync-start">Start syncing ▶</button>'
                      : '<button class="btn btn-neon small sync-now">Sync now</button>'}
          ${pr.paused ? '' : '<button class="btn btn-ghost small sync-pause" title="Stop this folder syncing until you press Start. Nothing is deleted and nothing is undone.">Pause</button>'}
          <button class="btn btn-ghost small sync-deep" title="Re-read and re-hash every file. Slow on a big folder — for a file edited in place without changing its size or timestamp.">Deep check</button>
          <button class="btn btn-ghost small sync-tidy">Tidy up conflict copies</button>
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
      ${rows || (fs ? ('<div class="empty">' + (Array.isArray(_acct) && _acct.length
          ? 'This device isn’t set up for any of them yet — pick one up below.'
          : (_acct === null ? 'Checking what this account syncs…' : 'No folders syncing under this account yet.'))
        + '</div>') : '')}
      ${elsewhere.length ? `<div class="sync-orphans"><b>Synced on your other devices</b>
        <p class="muted small">These folders belong to your account but this device has no directory for
        them — after a reinstall, an app update, or on a machine you have not set up yet. Choose where
        each one lives here and it rejoins the same folder; nothing is re-uploaded.</p>
        ${elsewhere.map(f => `<div class="sync-orphan"><span>🔄 ${PC.enc(f.key)}
            <span class="muted small">· ${f.n} file${f.n === 1 ? '' : 's'}</span></span>
          <button class="btn btn-neon small sync-attach" data-key="${PC.enc(f.key)}"${fs ? '' : ' disabled'}
            >Set up on this device…</button></div>`).join('')}</div>` : ''}
      ${_acct === 'error' ? '<p class="muted small">(Couldn’t check what your other devices sync just now.)</p>' : ''}
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
      const id = b.dataset.oid, dir = b.dataset.odir || '';
      if(l.some(x => x.id === id)) return;
      const add = (key) => {
        // Paused: adding a folder must not start uploading it before its exclusions are set.
        l.push({ id, key, dir, name: key, excludes: [], prefs: { paused: true }, lastSyncAt: 0, lastFullScanAt: 0 });
        saveFolders(l); rememberPair(id, dir, key); watch(id); paint();
      };
      /* ALREADY PAIRED ONCE → just resume it. Asking someone to name a folder they named last week,
       * with "use the SAME name on your other devices" underneath, invites them to type a different
       * one — and a different name is a different folder that will never meet the first. */
      const known = recallPair(id, dir);
      if(known){ add(known); PC.toast('syncing “' + known + '” again'); return; }
      const guess = pairKey(dir.split(/[/\\]/).pop()) || 'Folder';
      Promise.resolve(PC.uiPrompt('Name this folder — use the SAME name on your other devices.', guess))
        .then(ans => {
          const key = pairKey(ans || '');
          if(!key || key.length < 4) return;
          add(key);
        });
    }; });

    /* Re-attach a folder this account already syncs. The pair key is KNOWN, so there is no name to
     * ask for — asking would only give someone a chance to type a different one, which is a second
     * folder that never meets the first. All this needs is where it lives on this machine. */
    feed.querySelectorAll('.sync-attach').forEach(b => { b.onclick = async () => {
      const key = b.dataset.key;
      try{
        const picked = await FS().pick();
        if(!picked) return;
        const l = folders();
        if(l.some(x => x.id === picked.id)){ PC.toast('that folder is already syncing'); return; }
        l.push({ id: picked.id, key, dir: picked.dir, name: key,
                 excludes: [], prefs: { paused: true }, lastSyncAt: 0, lastFullScanAt: 0 });
        saveFolders(l); rememberPair(picked.id, picked.dir, key); watch(picked.id); paint();
        PC.toast('“' + key + '” is set up here — the first check compares, it does not re-upload');
      }catch(e){ PC.toast('could not set that up: ' + ((e && e.message) || e)); }
    }; });

    const add = document.getElementById('sync-add');
    if(add) add.onclick = async () => {
      try{
        const picked = await FS().pick();
        if(!picked) return;
        const list2 = folders();
        if(list2.some(x => x.id === picked.id)){ PC.toast('that folder is already syncing'); return; }
        // Picked a directory this identity has paired before? Then it already has a name on the
        // other devices; offer that, rather than a guess from the path that may not match it.
        const guess = recallPair(picked.id, picked.dir)
                   || pairKey(picked.dir.split(/[/\\]/).pop()) || 'Folder';
        /* The name IS the pairing. Two devices sync together because they used the same one, so this
         * asks rather than inventing a hidden id — and says so, because "Documents" on the laptop
         * meeting "Docs" on the phone is two folders, not one, and nothing later would explain why. */
        const key = pairKey(await PC.uiPrompt(
          'Name this folder. Use the SAME name on your other devices to sync them together — the '
          + 'folder can be anywhere on each one.', guess) || '');
        if(!key) return;
        if(key.length < 4){ PC.toast('use at least 4 letters or digits'); return; }
        list2.push({ id: picked.id, key, dir: picked.dir, name: key,
                     excludes: [], prefs: { paused: true }, lastSyncAt: 0, lastFullScanAt: 0 });
        saveFolders(list2); rememberPair(picked.id, picked.dir, key); watch(picked.id); paint();
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
      { const now = card.querySelector('.sync-now');
        if(now) now.onclick = () => sweep(get(), { manual:true }).catch(()=>{}); }
      /* PAUSE, for a folder that is already running.
       *
       * `paused` existed only for a NEWLY ADDED folder, so there was no way to stop one that was
       * already going — and every resume, focus or heartbeat starts a sweep, which is precisely when
       * someone wants it to stop. Asked for at the worst possible moment: a phone republishing a
       * thousand files that should not have existed, with nothing on the screen able to halt it.
       *
       * It stops the automatic paths only. Nothing is deleted, nothing already uploaded is undone,
       * and Start picks it up exactly where it was. */
      { const pz = card.querySelector('.sync-pause');
        if(pz) pz.onclick = () => {
          put(f => { f.prefs = Object.assign({}, f.prefs, { paused: true }); });
          stopping.add(id);                       // and stop the sweep that is running RIGHT NOW
          setStatus(id, running.has(id) ? 'stopping…' : 'paused — press Start when you want it to run again');
          paint();
        }; }
      { const st = card.querySelector('.sync-start');
        if(st) st.onclick = () => {
          // Commit whatever is in the exclusions box FIRST — someone types the patterns and presses
          // Start without leaving the field, so a change event may never have fired.
          const ta = card.querySelector('.sync-ex-ta');
          if(ta) put(f => { f.excludes = ta.value.split('\n').map(x => x.trim()).filter(Boolean); });
          put(f => { f.prefs = Object.assign({}, f.prefs, { paused: false }); });
          stopping.delete(id);
          paint();
          sweep(get(), { manual:true }).catch(()=>{});
        }; }
      card.querySelector('.sync-deep').onclick = () => sweep(get(), { manual:true, deep:true }).catch(()=>{});
      /* RECONNECT, rather than "remove it and add it again".
       *
       * A grant can go without the folder going: a desktop config file truncated by a crash, an
       * Android persisted-URI permission dropped, a drive that was not mounted at launch. The advice
       * used to be to delete the folder and re-add it, which throws away the exclusions and invites
       * someone to retype the pair name — and a different name is a second folder that never meets
       * the first. Re-picking keeps every one of those and swaps only the platform's handle for the
       * directory, which is the only thing that actually changed. */
      { const rl = card.querySelector('.sync-relink');
        if(rl) rl.onclick = async () => {
          try{
            const picked = await FS().pick();
            if(!picked) return;
            const l = folders();
            if(l.some(x => x.id === picked.id && x.id !== id)){ PC.toast('that folder is already syncing'); return; }
            const i = l.findIndex(x => x.id === id);
            if(i < 0) return;
            const key = keyOf(l[i]);
            l[i] = Object.assign({}, l[i], { id: picked.id, dir: picked.dir });
            saveFolders(l);
            rememberPair(picked.id, picked.dir, key);
            granted = null;                       // re-ask the platform, so the banner clears
            watch(picked.id); paint();
            PC.toast('“' + key + '” is connected again — its exclusions and name are unchanged');
          }catch(e){ PC.toast('could not reconnect: ' + ((e && e.message) || e)); }
        }; }
      card.querySelector('.sync-tidy').onclick = async () => {
        const f = get(); if(!f) return;
        setStatus(f.id, 'looking for redundant copies…');
        let found;
        try{ found = await conflictCleanup(f, { dryRun:true }); }
        catch(e){ PC.toast('couldn’t check: ' + ((e && e.message) || e)); paint(); return; }
        if(!found.list.length){ PC.toast('no conflict copies that are provably identical'); paint(); return; }
        const mb = (found.bytes / 1048576).toFixed(1);
        const ok = await PC.uiConfirm(found.list.length + ' conflict copies are byte-for-byte identical to the '
          + 'file they were made from (' + mb + ' MB).\n\nMove them to .pc-trash? They are removed on your other '
          + 'devices the same way any deletion is, and nothing is erased — everything stays in .pc-trash until you '
          + 'empty it.\n\nCopies that DIFFER from the original are left alone.');
        if(!ok){ paint(); return; }
        setStatus(f.id, 'tidying…');
        try{
          const r = await conflictCleanup(f, {});
          PC.toast('moved ' + r.moved + ' to trash'
                   + (r.tombstoned ? ' · ' + r.tombstoned + ' marked for your other devices' : '')
                   + (r.failed.length ? ' · ' + r.failed.length + ' failed' : ''));
          await sweep(get(), { manual:true });            // tell the other devices, the ordinary way
        }catch(e){ PC.toast('tidy failed: ' + ((e && e.message) || e)); }
        paint();
      };
      card.querySelector('.sync-trash').onclick = async () => {
        if(!await PC.uiConfirm('Empty this folder’s .pc-trash of anything older than 30 days?')) return;
        try{ const r = await FS().emptyTrash(id, 30); PC.toast('emptied ' + (r.removed||0) + ' day(s)'); }
        catch(e){ PC.toast('failed: ' + ((e && e.message) || e)); }
      };
      card.querySelector('.sync-forget').onclick = async () => {
        if(!await PC.uiConfirm('Stop syncing this folder?\n\nNothing is deleted — the files stay on this '
                               + 'device and on your other devices. It simply stops being kept in step.')) return;
        try{ await FS().forget(id); }catch(_){}
        /* CLEAR THE AGREEMENT UNDER THE KEY IT WAS WRITTEN WITH — and under the old one too.
         *
         * `base` moved from the platform id to the pair key when the manifest did; this removeItem
         * did not move with it, so "Stop syncing" left the agreement behind. Re-adding the folder
         * later then starts from a base claiming files are synced that are no longer on this disk,
         * the engine correctly reads that as "deleted here", and they are removed from every other
         * device. Both keys go, because a build older than the pair key wrote the id one.
         * tests/client/two_device_sim.js — 'stale-base-is-what-deletes-everything'. */
        const f = folders().find(x => x.id === id);
        await _dropBase(id);                       // a build older than the pair key wrote this one
        if(f) await _dropBase(keyOf(f));
        saveFolders(folders().filter(x => x.id !== id));
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

  let _started = false;
  function startAll(){
    const fs = FS(); if(!fs) return;
    /* ONCE. Every line below attaches something that has no matching detach — a document listener, a
     * window listener, a Capacitor listener, an interval — so a second call would double the sweeps
     * for every resume, focus and heartbeat, and a third would treble them. It is called from one
     * place today; this is what keeps that true when it is called from two. */
    if(_started) return;
    _started = true;
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

  // accountFolders/acct are shared with Files → Blossom, which lists the same pair keys as browsable
  // roots. One fetch, one cache, one answer about what this account syncs.
  window.PCSync = { paint, folders, sweep, startAll, store, status,
                    accountFolders, acct: () => _acct };
})();
