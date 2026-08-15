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
  /* Sizes for the humans reading this screen.
   *
   * Local, and NOT app.js's `_fmtBytes`. That one exists but is not on `PC` — it is passed into
   * git.js's factory, which is easy to mistake for an export list and I did: `PC._fmtBytes is not a
   * function` reached a user, on the confirmation dialog of an irreversible action, where a throw
   * means the button simply reports "action failed". Six lines here cannot be broken by anything
   * app.js does to its own internals. */
  const _bytes = (n) => {
    n = +n || 0;
    const u = ['B', 'KB', 'MB', 'GB', 'TB'];
    let i = 0;
    while(n >= 1024 && i < u.length - 1){ n /= 1024; i++; }
    return (i === 0 ? n : n.toFixed(n < 10 ? 1 : 0)) + ' ' + u[i];
  };

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
  /* HOW MANY FILES ARE IN THIS FOLDER. "Documents · 15,790 files" is the difference between a card
   * that says a folder is being synced and one that says what is in it — and it is the first thing
   * anybody checks when they suspect a sync is incomplete.
   *
   * Read from the account's own manifest list (`_acct`, /client/sync-folders), because that is the
   * count the RELAY holds — the agreed contents of the pair — rather than whatever this device has
   * managed to scan so far. While it is loading there is deliberately no number at all: a confident
   * "0 files" on a folder with fifteen thousand in it is worse than a blank. */
  function _countOf(f){
    const k = keyOf(f);
    const rec = Array.isArray(_acct) ? _acct.find(x => x && x.key === k) : null;
    if(!rec || typeof rec.n !== 'number') return '';
    return '· ' + rec.n.toLocaleString() + ' file' + (rec.n === 1 ? '' : 's');
  }

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
    /* …AND "this sweep changed nothing" IS NOT A REASON TO SKIP THE MERGE — it is the strongest
     * reason to do it.
     *
     * This used to require `s.touched.length`, so the re-read was skipped by exactly the sweep that
     * had nothing of its own to contribute: a DOWNLOAD-ONLY sweep calls `agree()` (which sets
     * `dirty`) and never `remember()` (which fills `touched`), so it wrote its minutes-old snapshot
     * whole. Device A joins a folder and spends twenty minutes downloading; device B uploads three
     * files in that window; A's save erases those three keys. B then reads its own entries as
     * `remote` gone, `base` present, local present — "deleted elsewhere" — and TRASHES the files it
     * had just uploaded. The last-writer-wins loss this merge exists to prevent, arriving through
     * the one door it was not watching.
     *
     * With an empty `touched` the merge simply resolves to whatever the manifest holds now, which is
     * the correct thing to write back. */
    async save(id, s){
      let paths = s.manifest || {};
      if(Array.isArray(s.touched)){
        let fresh = null;
        try{ fresh = await this.manifest(id); }
        catch(_){ /* keep our snapshot — see above */ }
        if(fresh && typeof fresh === 'object'){
          /* `verify` gets the LAST read before the write, which is the only copy that can answer
           * "is this still safe?" — the merge below is what would otherwise quietly overwrite a
           * path another device published while we were deciding. Throwing here saves nothing. */
          if(typeof s.verify === 'function') s.verify(fresh);
          const merged = Object.assign({}, fresh);
          for(const p of s.touched) if(paths[p] !== undefined) merged[p] = paths[p];
          paths = merged;
        }
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
      //
      // OMITTING `base` means "I have no agreement to record", which is what a manifest edit made
      // from Files → Synced folders is (see _mutate). `{}` still WRITES an empty agreement, because
      // a sweep that genuinely settled on nothing has to be able to say so.
      if(s.base !== undefined) await _saveBase(id, s.base);
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
  async function accountFolders(force){
    if(!PC.me || !PC.me() || !PC.me().pubkey){ _acct = null; return false; }
    const age = Date.now() - _acctAt;
    /* `force` is for a device that has just CHANGED the folder — its own write is the one answer the
     * TTL must not sit on, or the count beside the folder stays two minutes behind what it did.
     *
     * A fetch already IN FLIGHT was issued before that write, so its answer is stale by construction
     * and joining it would cache the pre-edit count for another two minutes. We cannot cancel it, so
     * the TTL is expired instead: this call still returns false, and the next repaint refetches
     * rather than reading a number that is known to be old. */
    if(_acctBusy){ if(force) _acctAt = 0; return false; }
    if(!force && _acct !== null && age < (_acct === 'error' ? _ACCT_RETRY : _ACCT_TTL)) return false;
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
  /* ---- EDITING A SYNCED FOLDER FROM A DEVICE THAT DOES NOT HOLD IT ----------------------------
   *
   * Files → Synced folders browses the MANIFEST — the document every device agrees on — which is why
   * it works in a browser that syncs nothing. Adding, renaming and deleting there therefore cannot
   * touch a file: it edits that agreement, and the devices carry it out on their next sweep, through
   * the same download/trash paths every other change takes. Nothing here reaches a disk, and nothing
   * here is a second kind of sync.
   *
   * WHICH MEANS IT IS THE ONE PLACE IN THE APP THAT CAN DELETE FILES OFF EVERY MACHINE YOU OWN
   * WITHOUT LOOKING AT ANY OF THEM. So it goes through `store.save` like a sweep does, and inherits
   * all of it: the re-read and merge (a device syncing right now must not lose the paths it just
   * added), the server's collapse guard, and the `removed` count that lets a deliberate mass delete
   * through while a shrink nobody accounts for is still refused.
   *
   * A DELETE IS A TOMBSTONE, never a removed key — the same record `plan.deleteRemote` writes, so
   * same()/live()/gone() read a web edit exactly as they read a device's. Dropping the key instead
   * leaves the document unable to say a file was ever deleted, which is a fact the next device to
   * join has no other source for. (Measured: on a device that HAS agreed, the two are equivalent;
   * see the three-way equivalence scenario in tests/client/two_device_sim.js.)
   *
   * WHAT NEITHER SHAPE CAN DO is stop a device whose `base` was cleared — a reinstall, "Stop
   * syncing" and back — from re-uploading a file it still holds. Both sides look changed there, and
   * diff() applies DELETE LOSES TO EDIT on purpose. That is exactly what happens when the deletion
   * is made on a device, too; this screen is not a weaker way to delete, it is the same one.
   *
   * A RENAME IS A TOMBSTONE PLUS A NEW ENTRY POINTING AT THE SAME BLOB. No bytes move: the devices
   * that hold the file trash their copy under the old name and fetch the new one from Blossom, which
   * is the only shape that works given a device may be offline for either half.
   */
  // `.pc-trash` is the folder's own trash, at any depth. Nothing may be written into it from here:
  // the sweep would then sync a deletion back out as a file.
  const RESERVED = /(^|\/)\.pc-trash(\/|$)/;
  /* A MANIFEST HAS NO DIRECTORIES — only paths — and a real filesystem does. So a path is only
   * writable if nothing else in the manifest makes it impossible on disk:
   *
   *   `notes` when `notes/todo.md` lives     → every device is asked to write a FILE where it has a
   *                                            DIRECTORY. That fails (EISDIR) on every sweep for
   *                                            ever, and `base` never advances past it.
   *   `notes/x.md` when `notes` is a file    → the mirror image, and just as permanent.
   *
   * The first one is also how a delete becomes much larger than the confirmation said: a live entry
   * at `notes` draws as one file, while `_liveUnder('notes')` covers everything under `notes/`. */
  function _blockedBy(paths, path){
    const pre = path + '/';
    for(const p in paths){
      if(!paths[p] || paths[p].deletedAt) continue;
      if(p.indexOf(pre) === 0) return 'there is already a folder called “' + path + '” here';
      if(path.indexOf(p + '/') === 0) return '“' + p + '” is a file, so nothing can go inside it';
    }
    return null;
  }
  function _liveUnder(paths, path){
    const out = [];
    if(paths[path] && !paths[path].deletedAt) out.push(path);
    const pre = path + '/';
    for(const p in paths) if(p.indexOf(pre) === 0 && paths[p] && !paths[p].deletedAt) out.push(p);
    return out;
  }
  /* One read-modify-write of the shared manifest. `build` is SYNCHRONOUS on purpose — anything slow
   * (hashing, uploading) happens before it is called, so the window between reading the manifest and
   * saving it stays as short as it can be, and the merge in store.save covers the rest. */
  async function _mutate(key, build, verify){
    const paths = await store.manifest(key);
    const next = Object.assign({}, paths);
    const touched = [], now = Date.now();
    let removed = 0;
    // A build that THROWS saves nothing: every check a caller wants to make against the current
    // manifest belongs in here, where it is reading the copy that is about to be written.
    build({
      paths, now,
      put(p, entry){ next[p] = entry; touched.push(p); },
      drop(p){
        if(!next[p] || next[p].deletedAt) return;      // already gone — not a deletion, and not a shrink
        next[p] = { deletedAt: now }; touched.push(p); removed++;
      },
    });
    if(!touched.length) return { touched: [], removed: 0 };
    /* NO `base`. An edit made here is not this device's agreement about anything — it may not even
     * hold the folder — and writing one back would do two bad things: roll back the agreement a
     * sweep running in this same tab has just advanced, and let an IndexedDB failure throw AFTER the
     * manifest is safely published, reporting a committed edit as a failure. store.save skips the
     * write entirely when no base is passed. */
    await store.save(key, { manifest: next, touched, removed, verify });
    return { touched, removed };
  }
  /* The per-blob ceiling this NODE will accept, which is not the same question as maxBytes() asks:
   * that one is about a platform's filesystem adapter, and a browser upload always has File.slice.
   * Cached for the session like maxBytes, and 0 means "the node did not say". */
  let _srvMax = null;
  async function serverMaxBytes(){
    if(_srvMax !== null) return _srvMax;
    let server = 0;
    try{
      const r = await fetch('/client/config');
      const j = await r.json();
      const mb = +(j && (j.blossom_max_upload_mb || j.max_upload_mb)) || 0;
      server = mb > 0 ? mb * 1024 * 1024 : 0;
    }catch(_){}
    _srvMax = server;
    return server;
  }
  /* THE BYTES HALF OF AN UPLOAD: everything up to (but not including) the manifest write.
   *
   * Separate so a batch can put many entries in with ONE manifest write — see uploadMany. Nothing
   * here touches the shared document, so a failure costs an upload and never an agreement. */
  async function _prepareUpload(key, dir, file, onProgress){
    const path = (dir ? dir + '/' : '') + file.name;
    if(RESERVED.test(path)) throw new Error('that name is reserved by folder sync');
    const size = file.size;
    if(size > SYNC_MAX_BYTES) throw new Error('bigger than folder sync can carry ('
      + Math.round(SYNC_MAX_BYTES / (1024*1024*1024)) + ' GB)');
    const srv = await serverMaxBytes();
    // The chunk has to be something the node will accept, since a chunk IS the request.
    const CH = srv > 0 ? Math.min(CHUNK_FALLBACK, srv) : CHUNK_FALLBACK;
    const mtime = +file.lastModified || Date.now();
    let entry;
    if(size > CH){
      if(!store.putParts) throw new Error('this build cannot upload a file that big');
      const readPart = async (off, len) =>
        new Uint8Array(await file.slice(off, off + len).arrayBuffer());
      const res = await store.putParts(readPart, size, onProgress, CH);
      /* No `csum` on this path, deliberately. csum is the hash of the whole CONTENT and computing
       * it here would mean holding the whole file, which is the one thing chunking exists to avoid.
       * The chunk list is the identity instead — the same entry an incremental Android scan
       * produces, and syncrun already compares against it (see the R0.chunks verify path).
       *
       * `cs` IS NOT OPTIONAL HERE, even though a sweep's own upload omits it. A sweep uses the
       * platform's chunk size, which is what `chunkShas(..., R0.cs || 0)` falls back to; this path
       * picks its own — capped by what the NODE accepts, so it is routinely smaller. Without it
       * recorded, a device verifying the file re-chunks at the default size, computes a different
       * list, decides the file changed and re-uploads it. */
      entry = { chunks: res.chunks, cs: res.cs || CH, size, mtime, device: deviceName() };
    } else {
      const bytes = new Uint8Array(await file.arrayBuffer());
      const put = await store.putBlob(bytes);
      const sha = (put && typeof put === 'object') ? put.sha : put;
      let csum = null;
      try{ csum = await store.hashBytes(bytes); }catch(_){}
      entry = { sha, size, mtime, device: deviceName() };
      if(csum) entry.csum = csum;
      if(onProgress) { try{ onProgress(size, size); }catch(_){} }
    }
    return { path, entry };
  }
  const edit = {
    /* A file or a whole directory. Everything live UNDER a directory is tombstoned, because a folder
     * does not exist in a manifest — only the paths inside it do.
     *
     * `expect` IS THE NUMBER THE USER WAS SHOWN, and it is checked against what this actually covers
     * at write time. A screen left open while another device fills the folder would otherwise ask
     * "delete these 3 files?" and delete four hundred — and `removed` accounting for the shrink is
     * exactly what waves that past the server's collapse guard, so nothing downstream would query
     * it either. */
    async remove(key, path, expect){
      return _mutate(key, api => {
        const list = _liveUnder(api.paths, path);
        if(expect != null && list.length > expect)
          throw new Error('“' + path + '” now holds ' + list.length + ' files, not ' + expect
                          + ' — another device changed it. Nothing was deleted; look again.');
        for(const p of list) api.drop(p);
      });
    },
    // How many live files a remove would take. Read fresh, because it is what the confirmation
    // promises and what `expect` is then checked against.
    async count(key, path){
      return _liveUnder(await store.manifest(key), path).length;
    },
    /* Rename a file or a directory. `to` is a full path, not a leaf, so this is also a move.
     *
     * REFUSED IF ANYTHING LIVE IS ALREADY THERE. Overwriting by rename would tombstone the target's
     * entry and put ours in its place, and the devices holding it would trash a file the user never
     * named — there is no undo for that anywhere in this feature except .pc-trash.
     *
     * Every one of those checks runs INSIDE the builder, against the manifest that is about to be
     * saved. Checked against an earlier read they are a race with a good disguise: a path deleted
     * between the two reads would be re-created here as a live entry pointing at a blob the folder
     * had just let go of. */
    async rename(key, from, to){
      if(RESERVED.test(to)) throw new Error('that name is reserved by folder sync');
      let dests = [];
      return _mutate(key, api => {
        const list = _liveUnder(api.paths, from);
        if(!list.length) throw new Error('“' + from + '” is not in this folder any more');
        const blocked = _blockedBy(api.paths, to);
        // A rename INTO its own subtree is the one case _blockedBy would refuse wrongly — moving
        // `2025` to `2025/old` is nonsense, but it is nonsense the UI cannot produce (a name may not
        // contain a slash), so refusing it here is right.
        if(blocked) throw new Error(blocked);
        dests = [];
        for(const p of list){
          const dest = to + p.slice(from.length);
          if(dest === p) throw new Error('that is the same name');
          if(api.paths[dest] && !api.paths[dest].deletedAt) throw new Error('“' + dest + '” already exists');
          // The SAME blob under a new name: sha/chunks/cs/csum/size/mtime are the file's, and only
          // the path changes. `device` records who moved it, which is what the others' reports show.
          api.put(dest, Object.assign({}, api.paths[p], { device: deviceName() }));
          api.drop(p);
          dests.push(dest);
        }
      }, /* verify at merge time */ fresh => {
        /* THE CHECK ABOVE IS NOT THE LAST WORD, because store.save re-reads the manifest and merges
         * `touched` over whatever it holds NOW. A device that published a file at one of our
         * destinations in the seconds since would have it overwritten by the merge — the user
         * renamed one file and a different file's contents were replaced on every machine. This runs
         * on that final read, and throwing here aborts the save with nothing written. */
        for(const d of dests)
          if(fresh[d] && !fresh[d].deletedAt)
            throw new Error('“' + d + '” was created on another device a moment ago — nothing was changed');
      });
    },
    /* PUT FILES INTO A SYNCED FOLDER FROM HERE, with the manifest written every CHECKPOINT of them
     * rather than every one.
     *
     * The bytes take exactly the path a sweep's upload takes — encrypted with the drive key,
     * content-addressed, deduped, chunked when big — because the devices that receive them have only
     * one way to read a manifest entry.
     *
     * Each _mutate is a read, a re-read inside store.save, and a whole fresh encrypted copy of the
     * manifest — which for a folder Folder Sync has filed thousands of paths into is megabytes. Once
     * per file, a fifty-photo drop moves more manifest than photos. The sweep has the same problem
     * and answers it the same way: checkpoint, so an interrupted batch keeps what it already put in
     * the folder instead of losing all of it.
     *
     * The BLOBS still go up one file at a time — that half is bounded by renderer memory, not by
     * round trips, and the reason it is sequential has not changed. */
    async uploadMany(key, dir, files, hooks){
      const h = hooks || {}, CHECKPOINT = 20;
      const done = [];
      let pending = [];
      const flush = async () => {
        if(!pending.length) return;
        const batch = pending; pending = [];
        await _mutate(key, api => {
          for(const it of batch){
            const blocked = _blockedBy(api.paths, it.path);
            // One impossible path must not take the other nineteen down with it.
            if(blocked){ it.error = blocked; continue; }
            api.put(it.path, it.entry);
            it.ok = true;
          }
        });
        for(const it of batch){
          if(h.onFile) try{ h.onFile(it.i, it.ok ? 'added' : ('failed: ' + it.error)); }catch(_){}
          done.push(it);
        }
      };
      /* One read up front, purely to REFUSE EARLY. The authoritative check is inside the flush,
       * against the manifest actually being written — this only saves sending a 2 GB file to a path
       * that was never going to be accepted, and leaving its blobs referenced by nothing. */
      let known = null;
      try{ known = await store.manifest(key); }catch(_){}
      for(let i = 0; i < files.length; i++){
        const f = files[i];
        try{
          if(known){
            const blocked = _blockedBy(known, (dir ? dir + '/' : '') + f.name);
            if(blocked) throw new Error(blocked);
          }
          if(h.onFile) try{ h.onFile(i, 'uploading…'); }catch(_){}
          const { path, entry } = await _prepareUpload(key, dir, f,
            (a, b) => { if(h.onProgress) try{ h.onProgress(i, a, b); }catch(_){} });
          pending.push({ i, path, entry });
          if(pending.length >= CHECKPOINT) await flush();
        }catch(e){
          const msg = (e && e.message) || String(e);
          if(h.onFile) try{ h.onFile(i, 'failed: ' + msg); }catch(_){}
          done.push({ i, error: msg });
        }
      }
      await flush();
      return { ok: done.filter(x => x.ok).length, failed: done.filter(x => !x.ok).length, done };
    },
  };


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
    // liveOnly: a decline is ONE LINE of text, and it is the commonest outcome there is — the watcher
    // fires for every file a sweep itself writes, so a folder downloading a thousand files asks a
    // thousand times and is told no a thousand times. Rebuilding the screen for each of those is what
    // "the UI keeps refreshing during sync" was.
    if(!decision.run && !o.dryRun){ setStatus(f.id, decision.why, null, true); return { skipped:true, why:decision.why }; }

    const job = (async () => {
      setStatus(f.id, o.dryRun ? 'checking…' : 'syncing…', null, true);
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
          forceTrash: !!o.forceTrash,
          /* ONLY A SWEEP SOMEBODY IS WATCHING MAY ASK. An automatic one — the watcher, a resume, the
           * heartbeat — has no one in front of it, so a dialog there is a modal nobody answers
           * blocking a background job; it refuses instead and says so on the card, where "Delete
           * anyway" is waiting. `o.manual` is the button. */
          confirmTrash: (o.manual && !o.dryRun) ? (m => PC.uiConfirm(
            '“' + keyOf(f) + '” — move ' + m.n + ' file' + (m.n === 1 ? '' : 's')
            + ' on this device to the trash?\n\nThey are marked deleted on your other devices, and '
            + 'this sweep keeps only ' + m.keep + '. If you did not delete them somewhere else, '
            + 'cancel — nothing is removed and your files stay where they are.\n\nNothing is erased '
            + 'either way: a delete here is a move into .pc-trash.')) : null,
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
    /* FIRST, not appended after "3 up". A sweep that refused to trash ten thousand files has done
     * one thing worth reading, and burying it behind the counts is how a guard becomes a line nobody
     * saw — which is the same silence the guard exists to break. */
    if(rep.refusedTrash) bits.unshift('kept ' + rep.refusedTrash.n + ' file'
      + (rep.refusedTrash.n === 1 ? '' : 's') + ' the others say are deleted — nothing trashed');
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
  /* A SWEEP THAT THREW MUST SAY SO ON THE CARD.
   *
   * Every automatic caller wrote `sweep(f).catch(()=>{})`, which is right about not wanting an
   * unhandled rejection and wrong about everything else: a folder whose sweep throws — the
   * filesystem plugin missing, a SAF permission the OS dropped after an update, a manifest the
   * server refused — keeps whatever status it had, and for a folder that has never swept that is
   * the placeholder "not synced yet". Reported as exactly that: "why is Documents in Folder Sync
   * 'not syncing yet', it was working before". The error existed the whole time; nothing showed it.
   *
   * A DECLINE is not an error and still goes through setStatus with its reason ("on battery", "on
   * cellular"), which is the normal, frequent case. This is only for the throw. */
  function swept(f, opts){
    return sweep(f, opts).catch(err => {
      const why = (err && (err.message || err.detail)) || 'sync failed';
      try{ setStatus(f.id, String(why).slice(0, 140)); }catch(_){}
      return { error: why };
    });
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
      // What the guard would not do, named. "Nothing trashed" is only believable next to the list of
      // what it declined to trash — and pressing Sync is what asks about it.
      + grp('Kept — your other devices say these were deleted',
            rep.refusedTrash ? (p.deleteLocal || []) : [], a => a.path + ' — ' + a.why)
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
  /* MEASURED, on the real Blossom endpoint: 4 MB chunks cost a flat 76 ms each (mean over 384 MB,
   * p95 80 ms, no drift as the blob count grows) at ~50 MB/s, so 5 GB is ~1280 chunks and about a
   * minute and a half of uploading on a LAN. Client memory does not enter into it — readPart/
   * writePart bound it to one chunk by construction, which is the thing the old 256 MB number
   * existed to protect and no longer describes. scripts/measure_chunked_upload.py is the harness.
   *
   * 8 GB rather than no limit at all: a ceiling is still worth having, because past this a single
   * file dominates a whole sweep and its chunk list alone (~70 bytes each, so ~140 KB here) is
   * several times the manifest's inline budget. Over it a file is REPORTED as skipped, never
   * silently dropped. */
  const SYNC_MAX_BYTES = 8 * 1024 * 1024 * 1024;
  /* ...and much lower where the file cannot be chunked at all. Without slice I/O the whole file, its
   * ciphertext and the upload body are all in memory at once, and an Android WebView has far less
   * headroom than Electron: a tablet crashed on a Pictures folder that a desktop swallowed. Once a
   * platform gains readPart this stops applying, because chunking makes size irrelevant. */
  const SYNC_MAX_UNCHUNKED = 32 * 1024 * 1024;
  // What a platform gets when it exposes readPart but no chunkBytes — the same figure syncrun uses
  // as `chunkAbove`, so "a chunk" means one thing on both sides of this file.
  const CHUNK_FALLBACK = 16 * 1024 * 1024;
  let _maxBytes = null;
  async function maxBytes(){
    if(_maxBytes !== null) return _maxBytes;
    const server = await serverMaxBytes();
    const fs = FS();
    const chunked = !!(fs && typeof fs.readPart === 'function');
    /* The server's limit is PER UPLOAD, and a chunked upload's uploads are CHUNKS. Taking the lower
     * of it and the file ceiling was conflating the two: a node configured with a 100 MB maximum
     * capped synced FILES at 100 MB while every request it was actually being sent was 4 MB. So the
     * server bound applies to the file only where the file IS the request — and where it is smaller
     * than a chunk, that is what it bounds instead. */
    if(chunked){
      const chunk = (fs && fs.chunkBytes) || CHUNK_FALLBACK;
      _maxBytes = (server > 0 && server < chunk) ? server : SYNC_MAX_BYTES;
      return _maxBytes;
    }
    _maxBytes = server > 0 ? Math.min(server, SYNC_MAX_UNCHUNKED) : SYNC_MAX_UNCHUNKED;
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
    // Through _mutate, which is the one place that knows what a manifest edit looks like — a fresh
    // read, tombstones (never removed keys), and the `removed` count the collapse guard reads. It
    // also re-reads, so a copy another device deleted while this ran is not re-tombstoned.
    if(tombstone.length) await _mutate(key, api => { for(const path of tombstone) api.drop(path); });
    return { list, moved, absent, failed, tombstoned: tombstone.length };
  }

  // ---- the screen ------------------------------------------------------------------------------
  /* Repainting is a full rebuild of every card, so it is COALESCED and it never happens under a
   * cursor. Leading-edge: the first call draws immediately (entering the screen, pressing a button
   * must feel instant), and anything asked for during the settling window collapses into one repaint
   * at the end of it.
   *
   * The typing guard is the one that costs data rather than comfort: the exclusions box is a
   * textarea inside these cards, so a rebuild mid-edit throws away what has been typed and the focus
   * with it. A pending repaint waits for the cursor to leave instead of being dropped. */
  const _PAINT_SETTLE = 400;
  let _paintT = null, _paintQ = false;
  function _editing(){
    const a = document.activeElement;
    return !!(a && (a.tagName === 'TEXTAREA' || a.tagName === 'INPUT') && a.closest && a.closest('.sync-card'));
  }
  /* OPENING THE SCREEN STARTS THE WATCHERS, if something earlier could not.
   *
   * startAll runs five seconds after login and returns early when the platform adapter is not
   * installed yet; nothing called it again, so a bridge that arrived late left folder sync dead for
   * the session with every folder at its placeholder status. This is the moment somebody is looking
   * at the screen and expecting it to work, and startAll is idempotent, so it costs a boolean when
   * the earlier call already succeeded.
   *
   * The explanation lives ABOVE the function rather than inside it: paint()'s body is read by
   * tests/client/test_sync_repaint.py through a fixed window from its opening brace, and seven lines
   * of prose pushed the coalescing out of view. */
  function paint(){
    try{ if(FS()) startAll(); }catch(_){}
    if(_paintT || _editing()){ _paintQ = true; if(!_paintT) _arm(); return; }
    _paintNow();
    _arm();
  }
  function _arm(){
    clearTimeout(_paintT);
    _paintT = setTimeout(() => { _paintT = null; if(_paintQ){ _paintQ = false; paint(); } }, _PAINT_SETTLE);
  }
  function _paintNow(){
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
          <span class="sync-n muted small">${_countOf(f)}</span>
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
      card.querySelector('.sync-dry').onclick = () => swept(get(), { manual:true, dryRun:true });
      { const now = card.querySelector('.sync-now');
        if(now) now.onclick = () => swept(get(), { manual:true }); }
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
          swept(get(), { manual:true });
        }; }
      card.querySelector('.sync-deep').onclick = () => swept(get(), { manual:true, deep:true });
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
      /* EMPTY TRASH HAS TO BE ABLE TO EMPTY THE TRASH.
       *
       * `.pc-trash` lives INSIDE the synced root, so everything in it is still counted by Explorer,
       * by a disk-usage tool and by a quota. Every layer of this hardcoded 30 days and there was no
       * automatic sweep for that floor to serve, so the only caller was this button — which could
       * therefore never reclaim anything recent. Reported after deleting a 40 GB Pictures folder:
       * pressed Empty trash, folder still 40 GB. The only way out was deleting `.pc-trash` by hand
       * in a file manager, i.e. the app sending the user around itself.
       *
       * It empties EVERYTHING now, and the confirmation states the real cost instead of naming a
       * policy: these files are already gone from the other devices, so this copy is the last one.
       * That is the sentence that belongs in front of an irreversible act — not a retention window,
       * which is what somebody pressing a button called "Empty trash" is least interested in. */
      card.querySelector('.sync-trash').onclick = async () => {
        let stat = null;
        try{ stat = FS().trashStat ? await FS().trashStat(id) : null; }catch(_){}
        const what = stat && stat.files
          ? stat.files + ' file' + (stat.files === 1 ? '' : 's') + ' · ' + _bytes(stat.bytes)
          : 'everything in it';
        if(stat && !stat.files){ PC.toast('the trash is already empty'); return; }
        if(!await PC.uiConfirm('Permanently delete ' + what + ' from this folder’s .pc-trash?\n\n'
                               + 'This includes items deleted today. They are already gone from your '
                               + 'other devices, so this is the last copy — it cannot be undone.',
                               { ok: 'Delete permanently' })) return;
        try{
          const r = await FS().emptyTrash(id, 0);          // 0 = everything; see fsbridge.emptyTrash
          /* A PARTIAL EMPTY IS NOT A FAILURE, and must not be reported as one — nor as a success.
           * On Windows the preview pane, the search indexer, OneDrive and every antivirus hold
           * handles on a folder of pictures, so some days really will refuse. Say how much came off
           * AND what would not, with the reason, since "close whatever is holding it and press it
           * again" is only actionable if the message says that is what happened. */
          const bad = (r.failed || []).length;
          PC.toast((r.bytes ? 'freed ' + _bytes(r.bytes) + ' · ' + (r.files || 0) + ' file(s)'
                            : 'emptied ' + (r.removed || 0) + ' day(s)')
                   + (bad ? ' · ' + bad + ' day(s) in use: ' + (r.failed[0].why || 'locked') : ''));
        }
        catch(e){ PC.toast('failed: ' + ((e && e.message) || e)); }
        paint();
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
  /* "Nobody is looking, so do not spend the radio" — true for a phone in a pocket, false for the
   * desktop app, which can now be CLOSED TO THE TRAY and started at login precisely so it will sync
   * while out of sight. Chromium also reports `hidden` for a desktop window that is merely covered by
   * another one, so without this a second app in front of it was enough to stop sync for as long as
   * it stayed there. `pcShell` is the Electron preload bridge — present only in the desktop app. */
  /* …and the same is now true of a PHONE that has been deliberately kept alive. "Stay connected"
   * (StayAwakeService) is an explicit opt-in with a permanent notification saying it costs battery,
   * so an app running under it is not an app nobody is looking at — it is one somebody asked to keep
   * working. Without this, `document.hidden` refused every background nudge on Android and folder
   * sync could only ever run with the app on screen.
   *
   * It is safe to allow because it changes WHO MAY ASK, not what is allowed: RUN.due still requires
   * charging, an unmetered link and a battery that is not low, so on a phone in a pocket on cellular
   * the answer is still no and nothing spins up. Re-read on resume, because the switch can move. */
  let _keptAlive = false;
  async function _readKeptAlive(){
    try{
      const P = PC.capPlugin && PC.capPlugin('PosterChanPush', 'stayConnected');
      if(!P) return;
      _keptAlive = !!(((await P.stayConnected()) || {}).on);
    }catch(_){ /* an APK older than that plugin: the answer is no, which is what it already was */ }
  }
  const _idle = () => document.hidden && !window.pcShell && !_keptAlive;
  let _nudgeT = null;
  /* `force` SKIPS THE IDLE TEST, and only the native tick may pass it.
   *
   * `_idle()` answers "is anybody looking at this app", which is the right question for a trigger
   * that fires whether or not the user asked for background work — a heartbeat in a tab nobody is
   * looking at should spend nothing. It is the WRONG question for a signal that can only be produced
   * by a foreground service the user explicitly switched on: the tick's existence IS the answer, and
   * re-deriving it from `_keptAlive` means one failed `stayConnected()` read (an older APK, a plugin
   * call that threw) silently swallows every tick while the service dutifully keeps sending them.
   *
   * It skips the LOOKING test, not the policy: `swept` still runs `shouldSync`, so charging, metered
   * and the minimum interval all still decide. */
  /* …AND IT MUST SURVIVE THE COALESCING, which is where it was being lost.
   *
   * There is ONE timer, and a later call clears it and installs a closure carrying its OWN `force`.
   * So an unforced nudge arriving inside the 1500ms window REPLACED a forced one and the flag was
   * gone — not a rare race but a correlated one: the phone wakes, the pending tick fires forced,
   * and the reconnecting radio raises `online` (or Capacitor's resume) milliseconds later. 1500ms
   * on, `_idle()` is true — screen off, `_keptAlive` false because the stayConnected read threw,
   * exactly the case `force` exists for — and the sweep is skipped for another whole tick period.
   *
   * The flag therefore belongs to the PENDING nudge, not to the call that scheduled it: once
   * anything has asked to bypass the idle test, coalescing more triggers into it cannot un-ask. */
  let _nudgeForce = false;
  function nudge(why, force){
    clearTimeout(_nudgeT);
    _nudgeForce = _nudgeForce || !!force;
    // Coalesced: resume, visible and online all fire together when a laptop lid opens.
    _nudgeT = setTimeout(() => {
      const forced = _nudgeForce;
      _nudgeForce = false;
      if(!forced && _idle()) return;
      folders().forEach(f => { swept(f, {}); });
    }, 1500);
  }

  let _started = false;
  function startAll(){
    const fs = FS();
    /* NO ADAPTER YET IS NOT "NO ADAPTER". This returned and left `_started` false, which is correct —
     * but nothing ever called it again, so if the platform adapter installed a moment later (the
     * Capacitor bridge arriving after the page's scripts) folder sync was dead for the whole session
     * with every folder sitting at its placeholder status. Bounded retry; fs-android.js is doing the
     * same on its own side, and either one winning is fine because this is idempotent. */
    if(!fs){
      if(startAll._t) return;
      let n = 0;
      startAll._t = setInterval(() => {
        if(FS() || ++n > 40){ clearInterval(startAll._t); startAll._t = 0; if(FS()) startAll(); }
      }, 500);
      return;
    }
    /* ONCE. Every line below attaches something that has no matching detach — a document listener, a
     * window listener, a Capacitor listener, an interval — so a second call would double the sweeps
     * for every resume, focus and heartbeat, and a third would treble them. It is called from one
     * place today; this is what keeps that true when it is called from two. */
    if(_started) return;
    _started = true;
    folders().forEach(f => watch(f.id));
    /* COALESCED, per folder. A sweep's own downloads are filesystem changes, so a folder receiving a
     * thousand files generates a thousand notifications — each of which would otherwise run the
     * battery/network policy check and ask for a sweep it is certain to be refused. The flag is set
     * immediately (it must not be lost) and only the ASKING is delayed. */
    const _chT = new Map();
    if(fs.onChanged) fs.onChanged((id) => {
      const l = folders(); const f = l.find(x => x.id === id); if(!f) return;
      f._dirty = true;
      clearTimeout(_chT.get(id));
      _chT.set(id, setTimeout(() => {
        _chT.delete(id);
        const cur = folders().find(x => x.id === id); if(!cur) return;
        cur._dirty = true;
        swept(cur, {});                // the policy may well decline; that is the point of asking
      }, 1500));
    });
    document.addEventListener('visibilitychange', () => { if(!document.hidden) nudge('visible'); });
    window.addEventListener('online', () => nudge('online'));
    window.addEventListener('focus', () => nudge('focus'));
    // Capacitor's own resume is more reliable than visibilitychange in a WebView that the OS froze.
    try{
      if(window.Capacitor && window.Capacitor.Plugins && window.Capacitor.Plugins.App){
        window.Capacitor.Plugins.App.addListener('appStateChange', (st) => {
          _readKeptAlive();                       // the switch may have moved while we were away
          if(st && st.isActive) nudge('resume');
        });
      }
    }catch(_){}
    _readKeptAlive();
    setInterval(() => { if(!_idle()) nudge('heartbeat'); }, HEARTBEAT_MS);
    /* A NATIVE CLOCK, on the one platform where a JS one does not run.
     *
     * The interval above is the only automatic trigger Android has — there is no watcher, SAF offers
     * no tree notification worth having — and Android throttles timers in a hidden WebView, so with
     * the screen off it effectively never fires. "Stay connected" kept the process alive and nothing
     * ever asked it to sync: reported as syncing stopping every time the screen goes off, with the
     * switch already on.
     *
     * StayAwakeService's Handler is not throttled (it is the service's thread, not the renderer's),
     * so the clock is native and this is the same nudge everything else raises — `shouldSync` still
     * decides, which is what "only when plugged in" and "Wi-Fi only" already mean. Deliberately NOT
     * a manual sweep: nobody pressed anything.
     *
     * FORCED past `_idle()` — see nudge(). That test asks whether anyone is looking, and this tick
     * can only be produced by a foreground service the user switched on precisely so that work
     * happens while nobody is. */
    try{ if(fs.onTick) fs.onTick(() => nudge('native', true)); }catch(_){}
    // Re-assert the stored preference on every start. Scheduling is idempotent on the Android side
    // (ExistingPeriodicWorkPolicy.KEEP), so this cannot reset the period and starve a job that has
    // been waiting for a charger.
    try{ if(fs.backgroundCheck) fs.backgroundCheck(!!ClientSettings.get('syncBgCheck', false), 180); }catch(_){}
    /* Tray → "Sync folders now". Deliberately NOT nudge(): nudge asks the policy, which says no on
     * battery, on a metered link, or within ten seconds of the last sweep — right for an automatic
     * trigger and wrong for someone who has just chosen the menu item. This is a manual sweep, the
     * same one the button on the folder card runs. */
    try{
      if(window.pcShell && window.pcShell.onSyncNow){
        window.pcShell.onSyncNow(() => { folders().forEach(f => { swept(f, { manual:true }); }); });
      }
    }catch(_){}
    nudge('startup');
  }

  // accountFolders/acct are shared with Files → Blossom, which lists the same pair keys as browsable
  // roots. One fetch, one cache, one answer about what this account syncs.
  // `edit` is how Files → Synced folders adds, renames and deletes: it writes the shared manifest
  // through the same guarded save a sweep uses, and the devices carry the change out.
  window.PCSync = { paint, folders, sweep, startAll, store, status, edit,
                    accountFolders, acct: () => _acct };
})();
