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
  const S = window.PCFolderSync, RUN = window.PCSyncRun, EXEC = window.PCSyncExec;
  const S_ENGINE = window.PCSyncState;
  const FS = () => window.pcFs || null;            // desktop only, for now — Android SAF lands next
  /* Sizes for the humans reading this screen.
   *
   * Local, and NOT app.js's `_fmtBytes`. That one exists but is not on `PC` — it is passed into
   * git.js's factory, which is easy to mistake for an export list and I did: `PC._fmtBytes is not a
   * function` reached a user, on the confirmation dialog of an irreversible action, where a throw
   * means the button simply reports "action failed". Six lines here cannot be broken by anything
   * app.js does to its own internals. */
  /* THE SPRITE, NOT AN EMOJI. Every other control in this client draws its icon from the shared
     symbol sheet, so an emoji here renders in the system's font — a different weight, a different
     colour, a different size on every platform, and no theme awareness at all ("no emojis! flat
     icons like the rest of the UI"). Local for the same reason `_bytes` is: six lines that nothing
     in app.js can take away. */
  const _num = (n) => (+n || 0).toLocaleString();
  const _ic = (name) => '<svg class="ic b-ic" aria-hidden="true"><use href="#i-' + name + '"></use></svg>';
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
    // Every switch, add and removal comes through here, so the native clock's copy of the policy
    // cannot drift from the one shouldSync reads.
    try{ _pushTickPolicy(); }catch(_){}
    // …and the native sweep's copy of the SAME list, for the same reason: a folder removed here and
    // still swept in the background is a folder the user cannot get rid of.
    try{ const r = _pushNativeConfig(); if(r && r.catch) r.catch(()=>{}); }catch(_){}
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
  /* COPIES THIS DEVICE COULD NOT VERIFY — path -> the identity that failed.
   *
   * Small, per folder, and deliberately in localStorage rather than in the shared manifest: it is
   * this device's experience of a copy, not a fact about the folder, and writing it to the manifest
   * would tell every other device something only this one observed. It is keyed on the copy's
   * identity so a re-upload clears it automatically — there is nothing to expire and nothing to go
   * stale. */
  const _BADF = 'pc-sync-badfetch:';
  function _badFetch(key){
    try{ return JSON.parse(localStorage.getItem(_BADF + key) || '{}') || {}; }catch(_){ return {}; }
  }
  function _rememberBadFetch(key, add){
    try{
      const cur = _badFetch(key);
      let changed = false;
      for(const p in (add || {})) if(cur[p] !== add[p]){ cur[p] = add[p]; changed = true; }
      if(changed) localStorage.setItem(_BADF + key, JSON.stringify(cur));
    }catch(_){}
  }

  async function _saveBase(key, base){
    // NOT swallowed. A base that silently fails to persist is an infinite resync, and the only way
    // anyone would ever find out is by watching their upload counter start again from one.
    await _IDB._tx('readwrite', st => st.put(base, key));
    try{ localStorage.removeItem(BASE_KEY(key)); }catch(_){}   // the old copy is now stale, not a fallback
  }
  /* CLEARING THE AGREEMENT MUST NOT FAIL QUIETLY.
   *
   * "Stop syncing" exists almost entirely to clear this, and the agreement is what decides whether
   * the next sweep sees a folder full of files or a folder full of deletions. A swallowed failure
   * here reports success, removes the card, and leaves the agreement in IndexedDB — so re-adding the
   * folder proposes moving every file to the trash, exactly as it did before, however many times
   * somebody repeats the process. Reported as precisely that, and it was unfalsifiable from the
   * outside because nothing anywhere said the clear had not happened.
   *
   * The localStorage half stays best-effort: it is the OLD location, it may legitimately not exist,
   * and its removal is not what decides anything. */
  async function _dropBase(key){
    /* IT IS NOT CLEARED UNTIL A READ SAYS SO.
     *
     * Deleting the key and assuming it worked is what produced "I already went through that process
     * a few times": the delete threw, the failure was swallowed, the card went away, and the
     * agreement stayed — so re-adding the folder proposed moving every file in it to the trash,
     * again and again, with nothing anywhere admitting the clear had not happened.
     *
     * Three ways, in increasing order of violence, each CHECKED by reading the value back:
     *   1. delete the key;
     *   2. overwrite it with an EMPTY agreement — to the engine that is the same thing, and a store
     *      that refuses deletes may still accept a put;
     *   3. drop the whole database, which holds nothing but these agreements.
     * Only if the value survives all three does this throw, and then the caller keeps the folder and
     * says so rather than removing a card whose record is still live. */
    const cleared = async () => {
      try{
        const v = await _IDB._tx('readonly', st => st.get(key));
        return !(v && typeof v === 'object' && Object.keys(v).length);
      }catch(_){ return false; }          // cannot read ≠ cleared
    };
    let err = null;
    try{ await _IDB._tx('readwrite', st => st.delete(key)); }catch(e){ err = e; }
    try{ localStorage.removeItem(BASE_KEY(key)); }catch(_){}
    if(await cleared()) return;

    try{ await _IDB._tx('readwrite', st => st.put({}, key)); }catch(e){ err = err || e; }
    if(await cleared()) return;

    try{
      if(_IDB._db){ try{ _IDB._db.close(); }catch(_){} _IDB._db = null; }
      await new Promise((res, rej) => {
        const rq = indexedDB.deleteDatabase(_IDB.DB);
        rq.onsuccess = res; rq.onblocked = res;
        rq.onerror = () => rej(rq.error || new Error('the database refused to be deleted'));
      });
    }catch(e){ err = err || e; }
    if(await cleared()) return;

    throw err || new Error('the sync record is still there after three attempts');
  }

  /* How long a manifest request may hang before it is treated as failed. Generous — this is a
   * small JSON POST and a slow radio is not a broken one — but bounded, which it was not. */
  /* Twenty seconds, not forty-five. These are small JSON requests, and a sweep makes SEVERAL of
   * them before it moves a byte (`/client/config`, then the manifest) — so the ceiling is paid more
   * than once and a generous one compounds: at 45s a phone on a dead network sat for a minute and a
   * half before admitting it, which is its own kind of hang. Long enough for a slow radio, short
   * enough that giving up still feels like an answer. */
  const _POST_TIMEOUT_MS = 20000;
  /* One whole-file transfer. Generous — a slow radio is not a broken one — but a ceiling, which
   * there was not: past this the socket is dead rather than slow, and waiting longer only means
   * waiting for ever. */
  const _XFER_TIMEOUT_MS = 5 * 60 * 1000;
  /* HOW LONG A CHUNKED TRANSFER MAY SHOW NO MOVEMENT AT ALL.
   *
   * Not a ceiling on the transfer — see the chunked pair below — a ceiling on SILENCE. Generous
   * enough that one 4 MB chunk on a bad radio still lands (that is minutes, not seconds, at the
   * speeds a phone can reach in a basement), short enough that a dead socket is admitted while
   * somebody is still watching. */
  const _STALL_MS = 3 * 60 * 1000;
  function _stallGuard(what, ms){
    let timer = null, boom = null, over = false;
    const tripped = new Promise((_res, rej) => { boom = rej; });
    const bump = () => {
      if(over) return;
      if(timer) clearTimeout(timer);
      timer = setTimeout(() => {
        if(over) return;
        over = true;
        boom(new Error('the ' + what + ' stopped moving — will try again'));
      }, ms || _STALL_MS);
    };
    const stop = () => { over = true; if(timer){ clearTimeout(timer); timer = null; } };
    bump();
    /* A rejection nobody is awaiting yet is an unhandled rejection in some runtimes; the race below
     * always awaits it, and this keeps that true even if a caller forgets. */
    tripped.catch(() => {});
    return { tripped, bump, stop };
  }
  function _bounded(p, what, ms){
    return new Promise((res, rej) => {
      let done = false;
      const t = setTimeout(() => { if(!done){ done = true;
        rej(new Error('the ' + what + ' stopped responding — will try again')); } }, ms || _XFER_TIMEOUT_MS);
      Promise.resolve(p).then(v => { if(!done){ done = true; clearTimeout(t); res(v); } },
                              e => { if(!done){ done = true; clearTimeout(t); rej(e); } });
    });
  }

  /* ---- WHO THIS DEVICE IS -----------------------------------------------------------------------
   *
   * A stable id, because it names the document only this device ever writes. The human name is not
   * enough: two Windows machines would share it, and sharing a document is the whole thing this
   * design exists to stop. Generated once and kept — losing it costs one extra document holding a
   * view nobody updates any more, which the merge simply outvotes as the others move on. */
  function deviceId(){
    let id = '';
    try{ id = localStorage.getItem('pc_sync_device_id') || ''; }catch(_){}
    if(!id){
      // `typeof`, not a bare reference: an undefined global THROWS rather than being falsy, and this
      // runs during boot on every platform including two shells and a test VM.
      const rnd = (typeof crypto !== 'undefined' && crypto.getRandomValues)
        ? [...crypto.getRandomValues(new Uint8Array(4))].map(b => b.toString(16).padStart(2,'0')).join('')
        : String(Date.now()).slice(-8);
      id = String(deviceName() || 'device').replace(/[^A-Za-z0-9_-]/g, '') + '-' + rnd;
      try{ localStorage.setItem('pc_sync_device_id', id); }catch(_){}
    }
    return id;
  }

  // ---- the store -------------------------------------------------------------------------------
  const store = {
    /* A REQUEST THAT NEVER ANSWERS MUST NOT BLOCK THE FOLDER FOR EVER.
     *
     * Nothing in this path had a timeout or an AbortController, and `fetch` does not impose one. A
     * socket that dies without an RST — a phone leaving the house, Wi-Fi to cellular, a laptop lid —
     * leaves the request pending indefinitely: it neither resolves nor rejects. The sweep is then
     * stuck on this await, `running` is only cleared in a `finally` that never runs, and every later
     * press of Sync silently returns that dead promise. Reported exactly: "left the house and came
     * back, stuck, no progress; I click Pause and Sync now and it says already syncing but no file
     * transfer." Pause could not rescue it either, because `stopping()` is checked BETWEEN files and
     * the sweep is stuck inside one.
     *
     * A bounded wait turns that into an ordinary failure: the sweep throws, the `finally` clears
     * `running`, the card says what happened, and the next sweep resumes from the checkpoint. This
     * is a small JSON POST — never a file — so the ceiling can be generous and still be a ceiling. */
    async _post(body){
      const auth = await PC.signAuth('sync-manifest');
      /* THE RACE IS THE GUARANTEE; THE ABORT IS THE COURTESY.
       *
       * The first version bounded this with an AbortController alone — and when the runtime has no
       * AbortController the constructor throws, the catch sets it to null, and NO timeout is armed:
       * a hang-proofing that silently does not apply, on exactly the runtimes least likely to
       * behave. Caught by the check for this, which still hung with the fix "in".
       *
       * So the ceiling is a race, which needs nothing but setTimeout, and the abort rides along when
       * it exists to actually cancel the request rather than just stop waiting for it. */
      let ctl = null, timer = null;
      try{ ctl = new AbortController(); }catch(_){ ctl = null; }
      if(ctl) timer = setTimeout(() => { try{ ctl.abort(); }catch(_){} }, _POST_TIMEOUT_MS);
      let r;
      try{
        r = await _bounded(fetch('/client/sync-manifest', {
          method:'POST', headers:{'Content-Type':'application/json'},
          signal: ctl ? ctl.signal : undefined,
          body: JSON.stringify(Object.assign({ pubkey: PC.me().pubkey, auth: btoa(JSON.stringify(auth)) }, body)),
        }), 'server', _POST_TIMEOUT_MS);
      }catch(e){
        const aborted = e && (e.name === 'AbortError' || /abort|stopped responding/i.test(String(e.message || e)));
        throw new Error(aborted ? 'the server did not answer in time — will try again'
                                : ('could not reach the server: ' + ((e && e.message) || e)));
      }finally{ if(timer) clearTimeout(timer); }
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
    /* The FILE's hash, for the manifest's content identity — distinct from the blob address, which is
     * the hash of the ciphertext. Provided by the store because syncrun.js has no crypto of its own. */
    hashBytes: async (bytes) => {
      const h = await crypto.subtle.digest('SHA-256', bytes);
      return [...new Uint8Array(h)].map(b => b.toString(16).padStart(2, '0')).join('');
    },
    blobSha: (PC.syncBlobs && PC.syncBlobs.blobSha) ? (bytes) => PC.syncBlobs.blobSha(bytes) : null,
    chunkShas: (PC.syncBlobs && PC.syncBlobs.chunkShas)
      ? (read, size, cs) => PC.syncBlobs.chunkShas(read, size, cs) : null,
    /* BOUNDED, for the reason `_post` is. A transfer that hangs strands the sweep exactly as a hung
     * manifest request does — and worse, because this is where a sweep spends its time, so it is the
     * likelier place to be standing when the network goes. The phone that had to be force-closed was
     * stuck on one of these.
     *
     * A RACE, not an abort: these go through PC.syncBlobs, which is shared with the drive, Notes and
     * the music library, and threading a signal through all of that to fix folder sync would be a
     * much larger change made at speed. The loser keeps running and is collected; what matters is
     * that the SWEEP stops waiting, `running` clears, the card says so and the next sweep resumes
     * from its checkpoint. A dangling request is a leak measured in one buffer; a stranded sweep is
     * a folder that never syncs again until the app is killed. */
    putBlob: (bytes) => _bounded(PC.syncBlobs.put(bytes), 'upload'),
    getBlob: (sha) => _bounded(PC.syncBlobs.get(sha), 'download'),
    // The chunked pair. Present only when the client build has them, so an older bundle simply does
    // not take the chunked path rather than calling something undefined.
    /* THE CHUNKED PAIR IS BOUNDED BY PROGRESS, NOT BY TOTAL TIME.
     *
     * It moves a file of any size, so a ceiling on the whole transfer would be a guess about
     * somebody's connection — that part of the original reasoning stands. What did not: "a stall
     * shows up as no progress, which the resume path already handles". The resume path only helps if
     * the sweep ENDS, and a chunk whose socket dies without an RST leaves an await that never
     * settles. The sweep stops there for ever, `running` never clears, and Pause cannot rescue it
     * because `stopping()` is checked between FILES and the sweep is stuck inside one. Reported from
     * a tablet: "stuck on 14/2291, never progressing".
     *
     * So the watchdog measures the thing that actually distinguishes slow from dead: every chunk
     * read and every progress report bumps it, and only a total absence of movement trips it. A file
     * that takes an hour is fine; a file that has not moved a byte in three minutes is not coming
     * back, and the sweep is better off recording it as failed and going on to file 15 — one failure
     * is not a failed sweep, and the next run resumes from the checkpoint.
     *
     * The loser of the race keeps running and is collected; what matters is that the SWEEP stops
     * waiting. Threading a real abort through PC.syncBlobs — shared with the drive, Notes and the
     * music library — is a much larger change than this is worth. */
    putParts: PC.syncBlobs && PC.syncBlobs.putParts
      ? (read, size, onProgress, cs, ...rest) => {
          const w = _stallGuard('upload');
          return Promise.race([
            PC.syncBlobs.putParts((off, len) => { w.bump(); return read(off, len); }, size,
                                  (done, total) => { w.bump(); if(onProgress) onProgress(done, total); },
                                  cs, ...rest),
            w.tripped,
          ]).then(v => { w.stop(); return v; }, e => { w.stop(); throw e; });
        } : null,
    /* EVERY ARGUMENT, OR A BIG FILE CAN NEVER LAND.
     *
     * This wrapper declared `(chunks, write)` and forwarded exactly those two, while the executor
     * has always called it as `(chunks, write, expect, have, cs)`. Three arguments went in the bin,
     * and each one is load-bearing:
     *
     *   `have` + `cs` are how a part file RESUMES. Without them `getParts` cannot compute how many
     *   chunks are already on disk, so `skip` stays 0 and every attempt restarts at byte 0. On a
     *   small file that is invisible — it finishes inside one window either way. On a 2 GB file it
     *   is fatal: the stall guard trips at three minutes of silence, the sweep retries, and the
     *   retry throws away every byte and begins again. It can never finish, and the size is what
     *   decides it. Reported as "ANDROID ISSUES WRITING >2GB FILE, DOWNLOAD STOPPED MOVING WILL TRY
     *   AGAIN" — the retry was real, the progress was not.
     *
     *   `expect` is the size check. Without it `getParts` skips "rebuilt N bytes, expected M" and a
     *   short rebuild is committed with no complaint — the checksum catches most of that, but only
     *   after the bytes are on disk and only when the record carries one.
     *
     * Forwarded with ...rest rather than by name, so the next argument this call grows cannot be
     * silently swallowed the same way. */
    getParts: PC.syncBlobs && PC.syncBlobs.getParts
      ? (chunks, write, ...rest) => {
          /* THE CEILING ON SILENCE HAS TO KNOW HOW MUCH HAS TO ARRIVE BEFORE THE NEXT WORD.
           *
           * The guard bumps when a CHUNK lands, so between bumps a whole chunk must download and
           * decrypt. Three minutes is right for the 4 MB a phone cuts, and wrong for the 16 MB a
           * desktop cut — 16 MB inside three minutes demands ~90 KB/s sustained, which a phone on a
           * poor link simply does not have, so a transfer that was working was declared dead every
           * time. Reported as "the download stopped moving, will try again", over and over, on the
           * one file big enough to be chunked that way.
           *
           * So the window is the greater of the old floor and what the incoming chunk needs at a
           * deliberately pessimistic 32 KB/s. It still catches a dead socket — nothing arriving for
           * eight minutes on a 16 MB chunk is not a slow link — and it no longer punishes a file for
           * having been uploaded by a faster machine. Cheap to be wrong now in the other direction:
           * a trip costs the chunk in flight, because resume works. */
          const _cs = +rest[2] || 0;
          const w = _stallGuard('download', Math.max(_STALL_MS, Math.ceil(_cs / 32768) * 1000));
          return Promise.race([
            PC.syncBlobs.getParts(chunks, (off, bytes) => { w.bump(); return write(off, bytes); },
                                  ...rest),
            w.tripped,
          ]).then(v => { w.stop(); return v; }, e => { w.stop(); throw e; });
        } : null,
  };

  /* ---- THE FOLDER, ONE RECORD PER FILE ----------------------------------------------------------
   *
   * Every file has exactly one versioned record on the server — `pcai:fs:<pair>:<sha256(path)>` —
   * and the server refuses any write that is not strictly newer than what it holds. There is no
   * per-device document, no merge, and no read whose absence, emptiness or staleness can describe
   * more than one file. The ENTRY (path, checksums, blob addresses) is NIP-44-sealed to this
   * account's own key before it leaves; the server sees only a version, a device name and an era.
   *
   * READS ARE A CACHE PLUS A DELTA. The record set is kept in IndexedDB and each load asks the
   * server only for records written since the last look — a 12,000-file folder costs one full read
   * ever, then a few rows per sweep. Falling behind is SAFE by construction: a record this device
   * has not seen yet is a file the sweep does not touch, and a deletion is always a positive
   * tombstone record, never an inference from absence.
   *
   * THE ERA is what makes "remove the folder and add it back" clean: retiring a pair bumps one
   * integer, every existing record becomes part of a dead world, and a device that shows up with a
   * journal from that world clears it and rejoins by content — no ghosts, no resurrections. */
  /* \u267b RESTORE FROM TRASH — shared by the folder card and Files \u2192 Synced folders.
   * Bridge-level (below the exclusion machinery), never overwrites, per-operation timeouts so one
   * stuck file is a counted failure rather than a hung button. `only` restricts to a subset of
   * listTrash rows (Files' per-date restore). */
  async function restoreTrash(folderId, only){
    const fs2 = FS(); if(!fs2 || !fs2.listTrash){ PC.toast('this device has no filesystem access'); return null; }
    let rows = [];
    try{ rows = await fs2.listTrash(folderId) || []; }catch(_){}
    if(only && only.length){ const want = new Set(only); rows = rows.filter(r => want.has(r.at)); }
    if(!rows.length){ PC.toast('the trash is empty'); return { done:0, skipped:0, failed:0 }; }
    if(!await PC.uiConfirm('Put ' + rows.length + ' file' + (rows.length === 1 ? '' : 's')
         + ' back where they came from?\n\nNothing is overwritten: a file that already '
         + 'exists again is skipped and its trash copy stays put.')) return null;
    let done = 0, skipped = 0, failed = 0;
    const back = [];                 // paths actually put back, for the sweep that follows
    const timed = (pr, ms) => Promise.race([pr,
      new Promise((_, rej) => setTimeout(() => rej(new Error('timed out')), ms))]);
    for(const r of rows){
      try{
        let free = true;
        /* Free = provably absent, OR its parent directory is gone entirely (trash() prunes
         * emptied dirs, and move() recreates them). Only a CONFIRMED still-there skips. */
        try{ const ev = fs2.confirmGone ? await timed(fs2.confirmGone(folderId, r.to), 15000) : null;
             free = !!(ev && (ev.gone === true || ev.parentAlive === false)); }catch(_){ free = false; }
        if(!free){ skipped++; continue; }
        await timed(fs2.move(folderId, r.at, r.to), 30000); done++; back.push(r.to);
      }catch(_){ failed++; }
      const n = done + skipped + failed;
      if(n % 10 === 0) setStatus(folderId, 'restoring\u2026 ' + n + ' / ' + rows.length
                                    + (failed ? ' (' + failed + ' failed)' : ''));
    }
    PC.toast('restored ' + done + (skipped ? ' \u00b7 ' + skipped + ' already back in place' : '')
             + (failed ? ' \u00b7 ' + failed + ' failed' : ''));
    /* "RESTORED 0 · 271 ALREADY BACK IN PLACE" IS TRUE AND TELLS NOBODY ANYTHING.
     *
     * Every row was skipped because the file is already at its destination — which means a previous
     * restore DID put them back and the trash copies were never unlinked (Android's `moveDocument`
     * is an optional capability that some providers satisfy by COPYING, leaving the original where
     * it was; fixed on that side too, but a folder in this state stays in it). From the outside that
     * is indistinguishable from the button doing nothing, and it is the third round of "restore N
     * from trash did nothing" this has produced.
     *
     * So it is CHECKED and then said. A sample of the skipped pairs is hashed on both sides; only if
     * they match is the reassuring sentence printed, because "your files are safe, delete the trash"
     * is not something to say on an assumption. Different bytes get the opposite advice. */
    if(!done && skipped && typeof fs2.hashFile === 'function'){
      const sample = rows.filter(r => r && r.at && r.to).slice(0, 5);
      let same = 0, diff = 0;
      for(const r of sample){
        try{
          const a = await timed(fs2.hashFile(folderId, r.at), 20000);
          const b = await timed(fs2.hashFile(folderId, r.to), 20000);
          if(a && b) (a === b ? same++ : diff++);
        }catch(_){}
      }
      if(same && !diff){
        /* AND THEN OFFER THE ONE THING THAT ENDS IT, HERE, ON THIS BUILD.
         *
         * The files are back; the trash holds byte-identical copies that a restore can never clear,
         * because restoring them is a no-op — their destinations are occupied by themselves. On
         * Android that state is produced by a `moveDocument` the provider satisfied by COPYING (a
         * build carrying the fix unlinks the source, but a device is not updated by being told
         * about a fix). Every press of Restore from then on reports "N already back in place" and
         * the count never falls: "Restore 172 files comes back again".
         *
         * `emptyTrash` is the only call that can remove them and it has been in every build. What
         * was missing is the CONFIRMATION that it is safe to press — which is exactly what the
         * hashes above establish. So it is offered, with the count and the proof, instead of being
         * left as a scary red button somebody has to reason their way to. */
        const line = skipped + ' file' + (skipped === 1 ? ' is' : 's are')
          + ' already back in your folder \u2014 what is left in .pc-trash are duplicate copies of '
          + 'them (checked ' + same + '). Nothing you need is in there.';
        setStatus(folderId, line + ' Empty trash reclaims the space.');
        const f2 = folders().find(x => x.id === folderId);
        if(f2 && fs2.emptyTrash && await PC.uiConfirm(line + '\n\nRemove those ' + skipped
             + ' leftover copies now?\n\nThe files themselves stay exactly where they are \u2014 this '
             + 'only clears the duplicates in .pc-trash, which is what keeps asking you to restore '
             + 'them.', { ok: 'Remove the duplicates' })){
          setStatus(folderId, 'clearing ' + skipped + ' leftover copies\u2026', null, true);
          try{
            const r2 = await fs2.emptyTrash(folderId, 0);
            const n2 = (r2 && (r2.files || r2.removed)) || 0;
            setStatus(folderId, 'cleared ' + (n2 || skipped) + ' leftover cop'
                      + ((n2 || skipped) === 1 ? 'y' : 'ies') + ' \u2014 your files are untouched');
          }catch(e2){ setStatus(folderId, 'could not clear them: ' + ((e2 && e2.message) || e2)); }
        }
      }
      else if(diff)
        setStatus(folderId, skipped + ' trash copies were left alone \u2014 the file already in the '
          + 'folder has DIFFERENT contents, so these are older versions. Open .pc-trash yourself '
          + 'before emptying it.');
    }
    /* AND THE SWEEP IS TOLD WHAT WAS RESTORED, or it undoes it.
     *
     * Putting a file back is a statement of intent, and it was left entirely implicit: the bytes
     * returned to the disk and nothing else changed, so the next sweep had to re-derive the intent
     * from versions and timestamps. It derives the opposite. The restored bytes ARE the bytes the
     * tombstone describes, so wherever this device's journal entry is missing — struck by a lost
     * compare-and-swap, cleared by an era change, or never there — a hashed scan reads "deleted
     * elsewhere, and this copy is the deleted version" and moves all of them straight back to
     * .pc-trash. Reported as "I did restore from trash and it clears then goes right back to
     * restore 172 from trash", with every restore honestly reporting success.
     *
     * `resend` says it outright instead: send these exact paths, at a version past whatever the
     * folder shows. It is the same channel the card's "Put them back everywhere" uses, and a named
     * path is now removed from the sweep's trash list (see the executor), so the two cannot fight.
     * Done HERE rather than in each caller, because Files restores through this same function and a
     * second copy of this rule is how the two would drift. */
    if(back.length){
      const f = folders().find(x => x.id === folderId);
      if(f){
        setStatus(folderId, 'telling your other devices about ' + back.length + ' restored file'
                  + (back.length === 1 ? '' : 's') + '\u2026', null, true);
        try{ await swept(f, { manual: true, resend: back }); }catch(_){}
      }
    }
    return { done, skipped, failed, restored: back };
  }

  /* The pair's era + cursor + decrypted record set, cached per pair. */
  const _SKEY = (k) => 'state:' + k;
  /* RECORDS ARE SEALED WITH THE DRIVE KEY, NOT THE SIGNER. `a1:` marks the format: AES-GCM under
   * the master key every syncing device holds for the file bytes themselves — WebCrypto, hardware,
   * microseconds. The old NIP-44-to-self seal routed EVERY record through the signer backend: 17k
   * one-at-a-time native-bridge calls on a tablet, 17k relay round trips on a remote-signer laptop
   * ("about 37 min left", before any byte moved). Old records still read via the fallback, and the
   * device whose journal holds them in plaintext re-publishes them in `a1` as it sweeps. */
  const _b64e = (u8) => { let out = ''; for(let i = 0; i < u8.length; i += 8192)
    out += String.fromCharCode.apply(null, u8.subarray(i, i + 8192)); return btoa(out); };
  const _b64d = (str) => { const bin = atob(str); const u8 = new Uint8Array(bin.length);
    for(let i = 0; i < bin.length; i++) u8[i] = bin.charCodeAt(i); return u8; };
  async function _sealRec(obj){
    const buf = new TextEncoder().encode(JSON.stringify(obj));
    return 'a1:' + _b64e(new Uint8Array(await PC.driveEnc(buf)));
  }
  async function _unsealRec(ct){
    if(String(ct).indexOf('a1:') === 0)
      return JSON.parse(new TextDecoder().decode(await PC.driveDec(_b64d(String(ct).slice(3)))));
    return JSON.parse(await PC.nip44dec(PC.me().pubkey, ct));   // the pre-a1 seal, read-only
  }
  async function _pathD(path){
    const h = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(String(path)));
    return [...new Uint8Array(h)].slice(0, 12).map(b => b.toString(16).padStart(2, '0')).join('');
  }
  /* ONE SIGNATURE PER WINDOW, NOT PER REQUEST. The server accepts a signed proof for five
   * minutes; asking the signer per request turned a seed into hundreds of signatures — trivial
   * for a local key, a relay round trip EACH for a remote signer (Amber, NIP-46), which is what
   * made the signer feel broken the day sync arrived. Sign once, reuse inside the window, refresh
   * with a minute and a half of margin. */
  let _authAt = 0, _authB64 = '';
  /* THE DEVICE TOKEN: the signer, once per device, ever. Minted on the first signed call and kept
   * with this identity's local state; every later sync call presents it instead of a signature —
   * so a sleeping phone signer can no longer stop a laptop syncing. An unknown/revoked token falls
   * back to exactly one signature and a fresh mint. */
  const _TOK_KEY = () => 'pc_sync_token_' + ((PC.me && PC.me() && PC.me().pubkey) || 'anon');
  function _syncToken(){ try{ return localStorage.getItem(_TOK_KEY()) || ''; }catch(_){ return ''; } }
  async function _mintToken(){
    const authB64 = await _syncAuth();
    const r = await _bounded(fetch('/client/sync-state', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({ pubkey: PC.me().pubkey, auth: authB64, pair: 'mint', mintToken: true }),
    }), 'server', _POST_TIMEOUT_MS);
    const j = await r.json().catch(() => ({}));
    if(r.ok && j && j.ok && j.token){
      try{ localStorage.setItem(_TOK_KEY(), j.token); }catch(_){}
      return j.token;
    }
    return '';
  }
  async function _syncAuth(){
    if(_authB64 && (Date.now() - _authAt) < 210000) return _authB64;
    /* THE SIGNER IS AN AWAIT LIKE ANY OTHER, and it was the one with no ceiling. A remote signer
     * that never answers (a sleeping phone, a dropped NIP-46 relay) left the sweep inside this
     * line for ever: the card said "syncing", the folder held its slot, the queue starved — and
     * nothing anywhere named the signer. Thirty seconds, then the truth. */
    const auth = await _bounded(PC.signAuth('sync-state'), 'signer', 30000)
      .catch(e => { throw new Error('the signer did not answer — check the signer connection on '
                                    + 'this device (' + ((e && e.message) || e) + ')'); });
    _authB64 = btoa(JSON.stringify(auth));
    _authAt = Date.now();
    return _authB64;
  }
  async function _statePost(body, _retry){
    let tok = _syncToken();
    if(!tok){ try{ tok = await _mintToken(); }catch(e){ tok = ''; } }
    const authB64 = tok ? '' : await _syncAuth();
    let ctl = null, timer = null;
    try{ ctl = new AbortController(); }catch(_){ ctl = null; }
    if(ctl) timer = setTimeout(() => { try{ ctl.abort(); }catch(_){} }, _POST_TIMEOUT_MS);
    let r;
    try{
      r = await _bounded(fetch('/client/sync-state', {
        method:'POST', headers:{'Content-Type':'application/json'},
        signal: ctl ? ctl.signal : undefined,
        body: JSON.stringify(Object.assign({ pubkey: PC.me().pubkey,
                                             auth: authB64, token: tok }, body)),
      }), 'server', _POST_TIMEOUT_MS);
    }catch(e){
      const aborted = e && (e.name === 'AbortError' || /abort|stopped responding/i.test(String(e.message || e)));
      throw new Error(aborted ? 'the server did not answer in time — will try again'
                              : ('could not reach the server: ' + ((e && e.message) || e)));
    }finally{ if(timer) clearTimeout(timer); }
    const j = await r.json().catch(() => ({}));
    if(r.status === 401 && j && j.tokenInvalid && !_retry){
      // Revoked or foreign token: one signature mints a fresh one, then the call repeats once.
      try{ localStorage.removeItem(_TOK_KEY()); }catch(_){}
      await _mintToken();
      return _statePost(body, true);
    }
    if(r.status === 409 && j && j.eraChanged){
      const e = new Error('this folder was retired or re-added elsewhere — syncing again from the top');
      e.eraChanged = true; e.era = j.era; throw e;
    }
    if(r.status === 409 && j && j.backstop){
      const e = new Error(j.error || 'refused'); e.backstop = true; throw e;
    }
    if(!r.ok || !j.ok) throw new Error(j.error || ('sync state ' + r.status));
    return j;
  }
  const stateS = {
    async _cache(key){
      try{ const v = await _IDB._tx('readonly', st => st.get(_SKEY(key)));
           if(v && typeof v === 'object' && v.entries) return v; }catch(_){}
      return null;
    },
    async _saveCache(key, c){
      try{ await _IDB._tx('readwrite', st => st.put(c, _SKEY(key))); }catch(_){}
    },
    async clear(key){
      try{ await _IDB._tx('readwrite', st => st.delete(_SKEY(key))); }catch(_){}
    },
    /* The record set. Throws when the server could not be asked — a failed read is never an empty
     * folder. On an era shift (the pair was retired/re-added elsewhere) this device's JOURNAL is a
     * record of a dead world: it is cleared here, before the executor reads it, and the folder
     * re-settles by content on this very sweep. */
    async load(key, onTick){
      const tick = (i, n) => { try{ if(onTick) onTick({ phase: 'reading the folder\u2019s shared records', i, n }); }catch(_){} };
      const cache = await this._cache(key);
      const body = { pair: key };
      if(cache && cache.cursor){ body.since = Math.max(0, cache.cursor - 60); body.era = cache.era; }
      const j = await _statePost(body);
      let entries, d2p;
      if(j.full){ entries = {}; d2p = {}; }
      else { entries = cache.entries; d2p = cache.d2p || {}; }
      if(cache && j.era !== cache.era){
        /* The world changed under this device. Its journal answers for records that no longer
         * exist; kept, every path would read "lost record — restoring", which resurrects a folder
         * somebody deliberately retired. Cleared, the next reconcile settles by content. */
        try{ await _saveBase(key, {}); }catch(_){}
      }
      let und = 0, got = 0, seen = 0;
      const oldSeal = [];
      const total = (j.records || []).length;
      for(const rec of (j.records || [])){
        if((++seen % 400) === 0) tick(seen, total);
        let e = null;
        const isOld = String(rec.ct).indexOf('a1:') !== 0;
        try{ e = await _unsealRec(rec.ct); }catch(_){ e = null; }
        if(!e || typeof e !== 'object' || !e.path){ und++; continue; }
        try{ if((await _pathD(e.path)) !== rec.d){ und++; continue; } }catch(_){}
        if(e.ps && !e.chunks){
          /* The pointer resolves back into the list; one that cannot be fetched makes the record
           * UNREADABLE (counted, nothing moved) rather than address-less — address-less on a
           * holder is a resend order, and this is not that. */
          try{
            const raw = JSON.parse(new TextDecoder().decode(await PC.syncBlobs.get(e.ps)));
            if(Array.isArray(raw.chunks) && raw.chunks.length){ e.chunks = raw.chunks; e.cs = raw.cs || e.cs; }
            else { und++; continue; }
          }catch(_){ und++; continue; }
        }
        /* The ENVELOPE is authoritative for version and device — it is what the server's
         * compare-and-swap actually judged. The sealed entry carries the rest. */
        e.v = rec.v; if(rec.by) e.by = rec.by;
        if(rec.t && !e.deletedAt) e.deletedAt = rec.at || 1;
        if(rec.bad) e.bad = rec.bad; else delete e.bad;
        const path = e.path; delete e.path;
        if(isOld) oldSeal.push(path);
        entries[path] = e; d2p[rec.d] = path;
        got++;
      }
      /* A FULL read where NOTHING decrypts is a key problem and must stop the sweep; one corrupt
       * record in a small delta is one file left alone, never a reason to abort — the counted
       * `undecryptable` reaches the card either way. */
      if(j.full && (j.records || []).length > 2 && !got && und)
        throw new Error('none of this folder’s records could be decrypted — this device’s '
                        + 'key cannot open them');
      await this._saveCache(key, { era: j.era, cursor: j.now || 0, entries, d2p });
      const state = {}, flagged = {};
      for(const p in entries){
        const e = Object.assign({}, entries[p]);
        if(e.bad){ flagged[p] = e.bad; delete e.bad; }
        state[p] = e;
      }
      /* Which paths still wear the pre-a1 seal — the sweep re-publishes the ones whose plaintext
       * its journal already holds, and the whole folder converts in one cheap pass. */
      return { state, flagged, era: j.era, undecryptable: und, oldSeal };
    },
    /* Publish records, batched, through the server's per-file compare-and-swap. Returns
     * {ok:[paths], stale:[paths], failed:[paths]}. Each entry is sealed here; `confirmed` is the
     * deliberate-delete flow and nothing else may pass it. */
    async put(key, recs, opts){
      const o = opts || {};
      const cache = await this._cache(key);
      const era = cache ? cache.era : 0;
      const me = PC.me().pubkey;
      const out = { ok: [], stale: [], failed: [] };
      for(let at = 0; at < recs.length; at += 400){
        const batch = recs.slice(at, at + 400);
        const put = [], byD = {};
        for(const r of batch){
          const entry = Object.assign({}, r.entry);
          delete entry.local; delete entry.bad;
          /* A HUGE FILE'S CHUNK LIST CAN OUTGROW THE SEAL — NIP-44 refuses a plaintext over 65535
           * bytes, and an Android-chunked (4 MB) file past ~4 GB lists more chunks than that. The
           * list moves into its own encrypted blob and the record carries the pointer; the drive
           * key already encrypts it, the store already dedups it, and the reclaim already counts
           * `ps` as a reference. */
          try{
            if(entry.chunks && JSON.stringify(entry.chunks).length > 58000){
              const sealed = new TextEncoder().encode(
                JSON.stringify({ chunks: entry.chunks, cs: entry.cs || 0 }));
              const putB = await PC.syncBlobs.put(sealed);
              entry.ps = (putB && typeof putB === 'object') ? putB.sha : putB;
              entry.pn = entry.chunks.length;
              delete entry.chunks;
            }
          }catch(e){ out.failed.push(r.path); continue; }
          let d, ct;
          try{
            d = await _pathD(r.path);
            ct = await _sealRec(Object.assign({ path: r.path }, entry));
          }catch(e){
            out.failed.push(r.path);
            continue;
          }
          byD[d] = r.path;
          const row = { d, v: +entry.v || 1, by: String(entry.by || deviceId()), ct };
          if(entry.deletedAt) row.t = 1;
          put.push(row);
        }
        if(!put.length) continue;
        const j = await _statePost({ pair: key, era, put, confirmed: !!o.confirmed });
        for(const res of (j.results || [])){
          const path = byD[res.d];
          if(!path) continue;
          if(res.ok) out.ok.push(path);
          else if(res.stale) out.stale.push(path);
          else out.failed.push(path);
        }
        // Keep the cache in step with what landed, so the next delta starts from the truth.
        if(cache){
          for(const r of batch){
            if(out.ok.indexOf(r.path) === -1) continue;
            const e = Object.assign({}, r.entry); delete e.local;
            const d = await _pathD(r.path);
            cache.entries[r.path] = e; cache.d2p = cache.d2p || {}; cache.d2p[d] = r.path;
          }
        }
      }
      if(cache) await this._saveCache(key, cache);
      return out;
    },
    /* Mark records whose STORED copy failed its checksum here — the flag rides the record, the
     * holder verifies its local copy and re-sends, and the fresh address clears everything. */
    async flag(key, items){
      const cache = await this._cache(key);
      const flag = [];
      for(const it of items || []){
        try{ flag.push({ d: await _pathD(it.path), bad: String(it.id || '') }); }catch(_){}
      }
      // Chunked under the server's batch cap — a report larger than the cap used to silently
      // truncate, which is a repair list with the tail torn off.
      for(let at = 0; at < flag.length; at += 400){
        await _statePost({ pair: key, era: cache ? cache.era : 0, flag: flag.slice(at, at + 400) });
      }
    },
  };

  const docs = {
    /* The io the executor sweeps through. `state` throws rather than answering {} — a failed read
     * must never look like an empty folder. */
    state: (key, onTick) => stateS.load(key, onTick),
    putState: (key, recs, o) => stateS.put(key, recs, o),
    flagBad: (key, items) => stateS.flag(key, items),
    index: (key) => _loadBase(key),
    saveIndex: (key, idx) => _saveBase(key, idx),
    getBlob: (sha) => store.getBlob(sha),
    putBlob: (bytes) => store.putBlob(bytes),
    getParts: store.getParts,
    putParts: store.putParts,
    hashBytes: (b) => store.hashBytes(b),
    blobSha: store.blobSha,
    chunkShas: store.chunkShas,
    /** Does the store still hold these bytes? For the consistency check; unknown is never "missing". */
    /* THREE ANSWERS, NOT TWO. `true` it is there, `false` it is NOT there, `null` I could not ask.
     *
     * This drives a repair that publishes deletions, and a HEAD returns plenty of things that are
     * not an answer: 429 from a rate limiter (this asks thousands of times in a row, so that is the
     * expected case, not the exotic one), 500, 403, a redirect. Reading any of those as "missing"
     * turns one bad server minute into a folder-wide tombstone. */
    async hasBlob(sha){
      try{
        // Cache-busted: a proxy caching these immutable URLs answered 200 for deleted blobs, and
        // this feeds a repair — a cached lie here becomes a wrong verdict about somebody's bytes.
        const r = await fetch(PC.mediaServer() + '/' + sha + '?probe=' + Date.now(),
                              { method:'HEAD', cache:'no-store' });
        if(r && r.ok) return true;
        if(r && (r.status === 404 || r.status === 410)) return false;
        return null;
      }catch(_){ return null; }
    },
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
      const j = await _statePost({ pair: 'pairs', pairs: true });
      _acct = (j && Array.isArray(j.folders)) ? j.folders : 'error';
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
  /* AN EDIT MADE FROM FILES IS THIS DEVICE'S CLAIM, LIKE ANY OTHER.
   *
   * Files → Synced folders can add, rename and delete in a folder this browser does not hold — it
   * edits the record, and the devices carry it out on their next sweep through the same paths they
   * always use. Under one shared document that needed a read, a merge, a compare-and-swap and a
   * server-side guard. It needs none of them now: the edit is published into THIS device's own
   * document, at a version above whatever the folder currently shows, and the merge does the rest.
   *
   * The read is STRICT — a record set that could not be fetched throws before anything is decided
   * — and every write goes through the server's per-file compare-and-swap, so an edit racing a
   * sweep loses one file's write, learns it immediately, and nothing is silently overwritten.
   */
  async function _mutate(key, build, verify){
    const got = await stateS.load(key);
    const paths = got.state;
    const meId = deviceId();
    const puts = [], now = Date.now();
    let removed = 0;
    // A build that THROWS saves nothing: every check a caller wants to make against the current
    // folder belongs in here, where it is reading what is about to be published against.
    build({
      paths, now,
      put(p, entry){
        puts.push({ path: p, entry: Object.assign({}, entry,
                    { v: S_ENGINE.versionOf(paths[p]) + 1, by: meId }) });
      },
      drop(p){
        const cur = paths[p];
        if(!cur || cur.deletedAt) return;          // already gone: not a deletion
        /* THE TOMBSTONE KEEPS THE FILE'S ADDRESS — the account-wide Restore reads it back. */
        const keep = {};
        for(const k of ['sha','csum','size','mtime','chunks','cs','ps'])
          if(cur[k] !== undefined) keep[k] = cur[k];
        puts.push({ path: p, entry: Object.assign(keep,
                    { v: S_ENGINE.versionOf(cur) + 1, by: meId, deletedAt: now }) });
        removed++;
      },
    });
    if(!puts.length) return { touched: [], removed: 0 };
    if(typeof verify === 'function') verify(paths);
    /* `confirmed` because every deleting caller here has ALREADY confirmed with the person —
     * remove() checks its expected count, removeMany is handed a reviewed list. The server's
     * backstop exists for a sweep gone wrong, not for a person who said yes. */
    const res = await stateS.put(key, puts, { confirmed: removed > 0 });
    if(res.stale.length || res.failed.length){
      const n = res.stale.length + res.failed.length;
      throw new Error(n + ' of ' + puts.length + ' change' + (puts.length === 1 ? '' : 's')
                      + ' could not be written' + (res.stale.length
                        ? ' — another device changed those files at the same moment; look again'
                        : ' — try again in a moment'));
    }
    return { touched: puts.map(x => x.path), removed };
  }

  /* The per-blob ceiling this NODE will accept, which is not the same question as maxBytes() asks:
   * that one is about a platform's filesystem adapter, and a browser upload always has File.slice.
   * Cached for the session like maxBytes, and 0 means "the node did not say". */
  let _srvMax = null;
  async function serverMaxBytes(){
    if(_srvMax !== null) return _srvMax;
    let server = 0;
    /* BOUNDED, because this runs BEFORE the sweep and a hang here strands it earlier than anything
     * else could — the folder never reaches its first file, so nothing reports progress and nothing
     * reports a failure either. It is also the least important request in the whole path: it asks
     * the node how large an upload it accepts, and there is a sane fallback for not knowing. This is
     * where the check for "a request that never answers" was actually getting stuck, several layers
     * above the manifest post I bounded first. */
    try{
      const r = await _bounded(fetch('/client/config'), 'server', _POST_TIMEOUT_MS);
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
    /* FORGET A FOLDER'S SHARED RECORD ENTIRELY — the thing that had no way to happen.
     *
     * Removing a folder from every device leaves the manifest behind: it is keyed on the NAME, not
     * on any device, so the pair goes on existing with its whole history. It shows up in the account
     * list as "🔄 Pictures · 0 files" — every path a tombstone — and any device that later pairs that
     * name inherits it. The only escape was a name nobody had used, which is not an answer.
     *
     * This WIPES the document rather than tombstoning what is in it. Tombstones are precisely what
     * makes a record poisonous: they say "these files were deleted", for ever, to everyone who ever
     * joins. An empty document says nothing, which is the truth about a folder nobody syncs.
     *
     * IT IS FOR A FOLDER NOTHING IS SYNCING. A device that still holds this pair keeps its own
     * agreement, and to that device an empty manifest reads as "deleted elsewhere" — the mass-delete
     * guard would catch it and ask, but the honest thing is to say so in the confirmation rather
     * than rely on the last line of defence. */
    /* RETIRE A FOLDER FOR THE WHOLE ACCOUNT.
     *
     * Every other write in this feature touches only this device's own document — that single-writer
     * rule is what makes concurrent devices safe. Retiring a pair is the one deliberate exception:
     * it has to empty every device's document, or the folder comes straight back the moment another
     * device publishes again, and the name stays unusable. So it is done in one place, by the
     * server, which holds the account's storage key, and only when somebody asks for it by name.
     *
     * IT IS CHECKED. This once reported "8,132 entries cleared" on a write that changed nothing, and
     * repeated it every time it was pressed; a read-back is one request and it is the only thing
     * that can tell the difference. */
    async forget(key){
      let all = [], live = 0, readOk = false;
      try{
        const got = await stateS.load(key);
        all = Object.keys(got.state);
        live = all.filter(p => got.state[p] && !got.state[p].deletedAt).length;
        readOk = true;
      }catch(_){ /* retiring is one write either way; the counts are for the toast */ }
      /* NOTHING TO FORGET IS NOT A WRITE. A confirmed-empty record gets no era bump — the second
       * press honestly reports zero instead of re-retiring a pair that is already gone. */
      if(readOk && !all.length) return { removed: 0, live: 0, tombstones: 0, devices: 0,
                                         verified: true };
      /* Retiring is ONE ERA BUMP, atomic on the server: every record becomes part of a dead world
       * the moment it lands, whatever the folder's size. Nothing to read back — the server answers
       * with the new era or it answers with an error. */
      const j = await _statePost({ pair: key, forgetAll: true });
      await stateS.clear(key);
      try{ await _saveBase(key, {}); }catch(_){}
      return { removed: all.length, live, tombstones: all.length - live, devices: 0,
               verified: !!(j && j.ok) };
    },
    /* REMOVE A NAMED SET OF PATHS. Not `remove`, which takes one path and everything under it and
     * re-counts to protect against a screen left open — this is handed an exact list that something
     * has already established cannot be fetched by anyone, so re-deriving it would only give it a
     * chance to be wrong. */
    async removeMany(key, paths){
      const want = (paths || []).slice();
      if(!want.length) return { removed: 0 };
      const r = await _mutate(key, api => { for(const p of want) api.drop(p); });
      return { removed: r.removed };
    },
    /* \u267b RESTORE ON EVERY DEVICE — the account-wide undo. A tombstone that kept its address
     * (the executors retain sha/chunks on delete now) is republished LIVE at a bumped version;
     * every device fetches the bytes from the store on its next sweep. Entries whose address was
     * never kept (old-era tombstones) are counted out loud rather than silently skipped. */
    async restoreMany(key, paths){
      const want = (paths || []).slice();
      if(!want.length) return { restored: 0, unaddressed: 0 };
      let restored = 0, unaddressed = 0;
      await _mutate(key, api => {
        for(const p of want){
          const e = api.paths[p];
          if(!e || !e.deletedAt) continue;
          if(!e.sha && !(e.chunks && e.chunks.length)){ unaddressed++; continue; }
          const live = {};
          for(const k of ['sha','csum','size','mtime','chunks','cs','ps'])
            if(e[k] !== undefined) live[k] = e[k];
          api.put(p, live); restored++;
        }
      });
      return { restored, unaddressed };
    },
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
      const got = await stateS.load(key);
      return _liveUnder(got.state, path).length;
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
      try{ known = (await stateS.load(key)).state; }catch(_){}
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
  /* Folders that asked to sweep while another folder held the page — drained on settle. */
  const _syncQueue = new Map();       // id -> {f, o}
  function _drainSyncQueue(){
    if(running.size > 0 || !_syncQueue.size) return;
    const next = [..._syncQueue.values()][0];
    _syncQueue.delete(next.f.id);
    /* Through swept(), so a throw lands on the card instead of nowhere. */
    setTimeout(() => { try{ swept(next.f, next.o); }catch(_){} }, 250);
  }
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
    /* A SWEEP ALREADY RUNNING MAKES THE BUTTON DO NOTHING, AND THAT HAS TO BE SAID OUT LOUD.
     *
     * `running` is cleared in a `finally`, so it clears when the sweep's promise SETTLES — and a
     * sweep that never settles never clears it. One await that does not come back (a fetch with no
     * timeout, a blob read that hangs, a chunk the media server never answers) leaves the folder id
     * in this map for the life of the page. From then on every press of Sync silently returns that
     * dead promise: no request, no status change, no error, nothing in any log. Reported exactly
     * that way — "clicked sync, nothing changed" — and the button is genuinely inert, which is a
     * fair description of the code as it stood.
     *
     * This does not unstick it (nothing here can know a hung await from a slow one — a first sweep
     * of 15,790 files is legitimately many minutes). It removes the SILENCE, which is the part that
     * made it undiagnosable: an explicit press now says a sweep is already going and points at Stop,
     * so "the button is broken" and "it is still working" stop looking identical. Only for a manual
     * press: an automatic trigger landing on a running sweep is the normal case and says nothing. */
    if(running.has(f.id)){
      if(o.manual){
        /* The live progress line, NOT whatever is on the card — which may be this same message from
         * the last press. Appending to it grew the text on every press ("already syncing — already
         * syncing — …"), unbounded, on precisely the stuck sweep this exists to explain. */
        const st = status.get(f.id) || {};
        const prog = (st.busy && st.text && !/^already syncing/.test(st.text)) ? ' — ' + st.text : '';
        setStatus(f.id, 'already syncing' + prog + ' · press Stop to interrupt it', null, true);
      }
      /* A DRY RUN MUST NOT ADOPT A REAL SWEEP'S RESULT. Returning the running job handed Check the
       * other sweep's promise, so when that settled the panel rendered "Uploaded / Conflicts kept"
       * under a button pressed to PREVIEW — a report of things that were done, presented as things
       * that would be. A preview that cannot run says so and returns nothing. */
      if(o.dryRun) return { skipped:true, why:'a sync is already running on this folder' };
      return running.get(f.id);
    }
    /* PRESSING SYNC NOW CLEARS THE STOP FLAG, because that is what the button means.
     *
     * Pause sets it to interrupt the sweep that is running, and until now only the Start button ever
     * cleared it — so a paused folder answered every later "Sync now" by halting on its first check
     * and reporting a sweep that had done nothing. Which summarised as "in step · nothing to sync",
     * about a folder with six thousand files outstanding, while Check — which returns its plan
     * before any stop check exists — offered to download all of them. Two buttons, one engine,
     * opposite answers: "check would download all this shit but sync now says nothing to sync".
     *
     * Safe here specifically: this is BELOW the `running` guard, so nothing is sweeping this folder
     * — the flag can only be a leftover from a sweep that has already ended. It does not un-pause
     * the folder; Sync now is one run, not a policy change. */
    if(o.manual && !o.dryRun) stopping.delete(f.id);

    /* ONE FOLDER'S REAL SWEEP AT A TIME, PER PAGE. Documents' upload lanes and Pictures' hashing
     * first pass ran side by side in one renderer, each holding plaintext + ciphertext + request
     * bodies — and the window died of it ("windows app ran out of memory"). The per-transfer
     * backpressure bounds ONE sweep; nothing bounded two. So a second folder WAITS, visibly, and
     * is started the moment the active one settles — never dropped: `_syncQueue` is drained from
     * the running sweep's finally. Previews are exempt (they move nothing), and so is a folder
     * already mid-sweep (that is the `running` guard above). */
    if(!o.dryRun && running.size > 0){
      _syncQueue.set(f.id, { f, o });
      const busyWith = [...running.keys()][0];
      const who = (folders().find(x => x.id === busyWith) || {}).key || 'another folder';
      setStatus(f.id, 'waiting for “' + who + '” to finish — one folder syncs at a '
                + 'time so the app keeps its memory', null, true);
      return { skipped: true, why: 'queued behind ' + who };
    }
    _syncQueue.delete(f.id);
    /* THE RESERVATION IS SYNCHRONOUS, OR THE GATE IS A RACE. Between this check and the job
     * registering itself there are awaits (the power read, the policy), and two folders starting
     * in the same moment both saw "nothing running" and both ran — the tablet did exactly that.
     * The slot is taken HERE, atomically with the check; every decline path below hands it back. */
    if(!o.dryRun) running.set(f.id, Promise.resolve({ skipped: true, why: 'starting' }));
    const _unreserve = () => { if(!o.dryRun && running.get(f.id) && !(running.get(f.id) instanceof Promise
      && running.get(f.id)._job)) running.delete(f.id); };

    const fs = FS();
    if(!fs){ if(!o.dryRun){ running.delete(f.id); _drainSyncQueue(); }
             throw new Error('this device has no filesystem access'); }

    const p = await power();
    const decision = RUN.due(Object.assign({}, p, {
      now: Date.now(), manual: !!o.manual, deep: !!o.deep, dirty: !!f._dirty,
      lastSyncAt: f.lastSyncAt || 0, lastFullScanAt: f.lastFullScanAt || 0,
    }), prefs(f));
    // liveOnly: a decline is ONE LINE of text, and it is the commonest outcome there is — the watcher
    // fires for every file a sweep itself writes, so a folder downloading a thousand files asks a
    // thousand times and is told no a thousand times. Rebuilding the screen for each of those is what
    // "the UI keeps refreshing during sync" was.
    if(!decision.run && !o.dryRun){
      /* THE SCHEDULER'S REASON IS A FOOTNOTE, NEVER THE HEADLINE. "Nothing changed since the last
       * sweep" is the policy declining a redundant pass — printed bare it replaced "in step ·
       * 11,950 files checked" and read as the sync having died. A folder with a clean standing
       * state keeps it on the card; the decline rides behind it in parentheses. */
      const _prev = status.get(f.id);
      const _base = _prev && _prev.text ? String(_prev.text).split(' · watching (')[0] : '';
      const _line = /^in step/.test(_base) ? _base + ' · watching (' + decision.why + ')' : decision.why;
      setStatus(f.id, _line, null, true);
      if(!o.dryRun){ running.delete(f.id); _drainSyncQueue(); }
      return { skipped:true, why:decision.why };
    }

    const job = (async () => {
      setStatus(f.id, o.dryRun ? 'checking…' : 'syncing…', null, true);
      /* KEEP THE CPU UP FOR THE DURATION, on a platform that otherwise suspends underneath us.
       *
       * "Stay connected" keeps the process resident; it does not keep the processor running. Measured
       * on a real phone: 23 downloads in the minute before the screen went off, 0 in the minute
       * after. The alarm was firing and the tick was arriving the whole time — there was simply no
       * CPU to sweep with. Taken only for a REAL sweep (a dry run reads a manifest and stops), and
       * only after `shouldSync` has already decided this folder may run at all, so the "only when
       * plugged in" and "Wi-Fi only" switches still gate it. Absent on desktop and on an older APK,
       * where it is a no-op. */
      const _wake = FS();
      /* RENEWED ON A CLOCK, not on progress. The lease is timed and a sweep can spend far longer
       * than it inside ONE operation: `putParts` reports per chunk (so `step` renews it), but
       * `getParts` takes no progress callback at all, so downloading a single large video ran the
       * whole way with no renewal, lost the CPU part-way, and stalled. Anything else long — a hash
       * of a big file, a slow chunk — has the same shape.
       *
       * An interval needs no cooperation from the thing it is protecting. It is throttled in a
       * hidden WebView, but only to about once a minute, which is ten times more often than a
       * ten-minute lease needs; and the CPU is held up by the very lock it is renewing, so it is not
       * competing with a sleeping device. Cleared in the same `finally` that releases the lock, so a
       * sweep that throws cannot leave it running. */
      let _wakeTimer = null;
      if(!o.dryRun && _wake && _wake.wakeBegin){
        try{ await _wake.wakeBegin(); }catch(_){}
        _wakeTimer = setInterval(() => {
          try{ const r = _wake.wakeBegin(); if(r && r.catch) r.catch(()=>{}); }catch(_){}
        }, 60000);
      }
      /* AND THE OTHER ENGINE MUST NOT BE IN THIS FOLDER AT THE SAME TIME.
       *
       * On Android the sweep now also exists in Java, because Chromium throttles a hidden page's
       * JavaScript however awake the processor is — so the alarm can be mid-sweep at the exact
       * moment somebody opens the app. Two sweeps writing the same manifest is last-writer-wins on
       * the document that decides whether files exist. The lock is native so both sides see it;
       * `running` above is this page's own guard and cannot.
       *
       * A dry run does not claim: it reads a manifest and stops, and blocking Check behind a
       * background transfer would make the button look broken. A platform without the lock (desktop,
       * an older APK) answers true, which is the behaviour it has today. */
      /* Pushed here as well as at startup: the drive key arrives from the signer some time AFTER
       * the page loads, so the startup push often carries an empty one and the phone would sit
       * unable to sweep until the next time a switch was touched. */
      if(!o.dryRun) { try{ await _pushNativeConfig(); }catch(_){} }
      let _claimed = false;
      if(!o.dryRun && _wake && _wake.claimSweep){
        try{ _claimed = await _wake.claimSweep(keyOf(f)); }catch(_){ _claimed = true; }
        if(!_claimed){
          // Same order as the `finally` below, and for the same reasons: the folder stops being
          // busy, the renewal stops, and the processor goes back.
          running.delete(f.id);
          if(_wakeTimer){ clearInterval(_wakeTimer); _wakeTimer = null; }
          if(_wake.wakeEnd){ try{ await _wake.wakeEnd(); }catch(_){} }
          /* AND IT SAYS WHAT THAT SWEEP IS DOING, instead of asking somebody to take it on trust.
           *
           * A refused claim means the native engine is mid-sweep on this folder — correct, and the
           * page must not join in — but the card then printed one static sentence, which on six
           * thousand files is indistinguishable from a hang and was reported as one ("what bullshit
           * is that, i need to see activity"). The sweep already knows its phase, its file and its
           * position; nothing was reading it. */
          const why = 'this folder is syncing in the background — it will finish on its own';
          setStatus(f.id, why, null, true);
          _watchNative(f, why);
          return { skipped:true, why };
        }
      }
      try{
        const rep = _forTheCard(await EXEC.sweep(fs, docs, {
          id: f.id, key: keyOf(f), device: deviceId(), now: Date.now(),
          excludes: f.excludes || [], maxBytes: await maxBytes(),
          shouldStop: () => stopping.has(f.id),
          manual: !!o.manual,
          /* THE CARD NARRATES THE SWEEP. The executor has ticked {phase, path, i, n} all along and
           * nothing listened — so a first sweep hashing 17,000 files sat behind a bare "syncing…"
           * for many minutes, indistinguishable from a hang. Throttled: a tick per file at disk
           * speed would out-paint the work. */
          onProgress: (pp) => {
            const t = Date.now();
            if(t - (_progAt.get(f.id) || 0) < 400) return;
            _progAt.set(f.id, t);
            let line = String((pp && pp.phase) || 'working') + '\u2026';
            if(pp && pp.i) line += ' ' + pp.i + (pp.n ? ' / ' + pp.n : '');
            if(pp && pp.path) line += ' \u2014 ' + String(pp.path).split('/').pop().slice(0, 40);
            setStatus(f.id, line, null, true);
          },
          // Paths a repair has established the store no longer holds; see the executor.
          resend: o.resend || null,
          /* WHICH COPIES THIS DEVICE HAS ALREADY FAILED TO VERIFY, so a bad one in the store is not
           * re-fetched on every sweep for ever — measured, two videos looping all evening. Keyed on
           * the copy's identity, so a re-upload from the other device clears it with no action from
           * anyone. */
          skipFetch: _badFetch(keyOf(f)),
          /* HOW BIG A CHUNK MAY BE IS TWO ANSWERS, AND THE SMALLER ONE WINS.
           *
           * The PLATFORM says how much it can hold at once (Android's 4 MB, because every chunk
           * crosses the Capacitor bridge as base64 held as UTF-16; the desktop's 16 MB). The NODE
           * says how large a single upload it will accept, and a chunk IS one upload — so a node
           * configured below the platform's chunk rejects every chunk of every large file, and a
           * folder of videos simply never syncs while small files sail through.
           *
           * The Files upload path has always taken the lower of the two. The sweep did not, which
           * is the path that matters for a Pictures folder. */
          chunkBytes: await chunkSize(),
          /* ABOVE ONE CHUNK A FILE GOES UP IN PIECES — AND "ONE CHUNK" IS THE PLATFORM'S, NOT A
           * NUMBER PICKED HERE.
           *
           * This said exactly that and then hardcoded 16 MB, which is the DESKTOP's chunk. Android's
           * is 4 MB, deliberately, because every chunk crosses the Capacitor bridge as base64 held
           * as UTF-16 (see fs-android.js). So on a phone every file between 4 and 16 MB — an
           * ordinary photo from a recent camera, and every video in a Pictures folder — took the
           * whole-file path, where the plaintext, the base64 crossing the bridge, the ciphertext and
           * the upload body are all live at once: three to four times the file, so up to ~65 MB of
           * renderer memory for a single 16 MB file, thousands of times over in one sweep.
           *
           * That is the renderer being killed mid-sweep: the app vanishes from the screen and STAYS
           * IN THE RECENTS LIST, because the process never died — only the WebView's renderer.
           * Nothing is thrown, nothing is logged, and no crash handler can see it.
           *
           * Deriving it removes the class rather than moving the threshold: a platform that says how
           * much it can hold gets held to its own answer. */
          chunkAbove: await chunkSize() || CHUNK_FALLBACK,
          hash: decision.mode === 'full', dryRun: !!o.dryRun,
          forceTrash: !!o.forceTrash,
          forceResurrect: !!o.forceResurrect,
          /* ONLY A SWEEP SOMEBODY IS WATCHING MAY ASK. An automatic one — the watcher, a resume,
           * the heartbeat — has nobody in front of it, so a dialog there is a modal no one answers
           * blocking a background job. It refuses instead and says so on the card. `o.manual` is
           * the button, and a fatal verdict is never offered at all. */
          confirm: (o.manual && !o.dryRun) ? (v => PC.uiConfirm(_ask(keyOf(f), v))) : null,
          // The first sweep of a Pictures folder is minutes of silence, and silence is
          // indistinguishable from a hang, a failed login or a 404 on the manifest — which is
          // exactly how this looked the first time it was tried for real.
          onProgress: (ev) => {
            const where = ev.path ? ' ' + ev.path.split('/').pop() : '';
            const of = ev.n > 1 ? ' ' + ev.i + '/' + ev.n : '';
            /* A stranger needs an ETA, not counters to interpret. Rate = this phase's own history;
             * only shown once there is enough of it to mean something, and rounded hard — a wrong
             * minute is worse than a vague one. */
            let eta = '';
            if(ev.n > 20 && ev.i > 5){
              const ph = _phaseAt.get(f.id);
              if(!ph || ph.phase !== ev.phase) _phaseAt.set(f.id, { phase: ev.phase, t0: Date.now(), i0: ev.i });
              else if(ev.i - ph.i0 > 5){
                const per = (Date.now() - ph.t0) / (ev.i - ph.i0);
                const min = Math.round(per * (ev.n - ev.i) / 60000);
                eta = min >= 120 ? ' · about ' + Math.round(min / 60) + ' h left'
                    : min >= 2 ? ' · about ' + min + ' min left'
                    : min >= 1 ? ' · about a minute left' : ' · nearly done';
              }
            }
            setStatus(f.id, ev.phase + of + where + eta, null, true);
          },
        }));
        if(!o.dryRun){
          if(rep && rep.badFetch){
            _rememberBadFetch(keyOf(f), rep.badFetch);
            /* THE OTHER HALF OF THE CHECKSUM REPAIR: the refusal is this device's memory, and the
             * fix belongs to whoever still holds a good copy. The flag rides the file's own record;
             * the holder's next sweep verifies its copy and re-sends, and the fresh storage address
             * clears both the flag and this device's memory of the bad one. */
            const _fl = [];
            for(const _p in rep.badFetch){
              const _r = rep.badFetch[_p];
              /* BOTH failure kinds reach the holder now. A checksum-bad copy and bytes the store
               * no longer has have the same repair — the device still holding the file re-sends
               * it — and requiring a person to press Verify for the second kind is how a
               * mid-seed reclaim became an afternoon of manual recovery. */
              if(_r && _r.id && (_r.why === 'checksum' || _r.why === 'gone'))
                _fl.push({ path: _p, id: _r.id });
            }
            if(_fl.length){ try{ await stateS.flag(keyOf(f), _fl); }catch(_){} }
          }
          // Tell the background checker what "synced" now looks like, or its next run compares
          // against a stale signature and notifies about changes that are already up.
          try{ if(fs.markSynced) await fs.markSynced(); }catch(_){}
          /* A SWEEP THAT DID NOT FINISH IS NOT A SYNC, AND MUST NOT BUY 15 MINUTES OF QUIET.
           *
           * `lastSyncAt` is what the policy measures the minimum interval from, and `_dirty` is what
           * lets a folder skip that interval. Setting both regardless meant a sweep that lost the
           * network half way — files still to move, failures recorded — told the scheduler it had
           * just synced, so the next automatic attempt was refused for a quarter of an hour with
           * work plainly outstanding. On a flaky link that is the difference between catching up and
           * looking stale.
           *
           * So an incomplete sweep leaves the folder due: the next tick, resume or `online` event
           * picks it straight back up, and the transfers that already landed are journalled, so it
           * costs nothing to try again. A FULL scan still records itself either way — the rehash
           * happened, whatever the transfers did. */
          const clean = !!(rep && rep.ok && !rep.stopped && !(rep.failed || []).length);
          if(clean) f.lastSyncAt = Date.now();
          /* The scan READING the folder is proof of the grant, whatever the transfers did — keyed
           * separately from `clean`, because a folder mid-recovery fails transfers on every sweep
           * and the "point at the folder again" banner kept showing over a folder being scanned
           * every few minutes ("devices still saying point at this folder again despite syncing"). */
          if(rep && !rep.skipped) f.lastScanOkAt = Date.now();
          if(decision.mode === 'full') f.lastFullScanAt = Date.now();
          f._dirty = !clean;
          const all = folders().map(x => x.id === f.id ? f : x); saveFolders(all);
        }
        setStatus(f.id, summarise(rep, decision), rep);
        return rep;
      }catch(e){
        setStatus(f.id, 'failed: ' + ((e && e.message) || e));
        throw e;
      } finally {
        running.delete(f.id);
        // Released on EVERY exit — a sweep that threw still has to give the processor back, and the
        // renewal must stop with it or it holds the lease open for ever. Same for the cross-engine
        // lock: a claim never released is a folder the background sweep can never touch again.
        if(_wakeTimer){ clearInterval(_wakeTimer); _wakeTimer = null; }
        if(_claimed && _wake && _wake.releaseSweep){ try{ await _wake.releaseSweep(keyOf(f)); }catch(_){} }
        if(!o.dryRun && _wake && _wake.wakeEnd){ try{ await _wake.wakeEnd(); }catch(_){} }
        _drainSyncQueue();
      }
    })();
    running.set(f.id, job);
    return job;
  }

  /* What a person is actually being asked. One sentence per kind of refusal, in the terms of what
   * happens to their files — never in the engine's terms. Nothing erases anything either way: a
   * delete here is a move into `.pc-trash`. */
  function _ask(key, v){
    /* THE COUNT IN THE SENTENCE HAS TO BE THE ONE THAT MADE IT ASK. Both of these read "this sweep
     * keeps only N", which was written for the RATIO rule and is nonsense under the absolute floor
     * that now fires alongside it — "move 59 files to the trash, this sweep keeps only 1,000" reads
     * as a typo, on the one dialog that has to be trusted. So the survivor count is quoted only when
     * it is actually the alarming part. */
    const shortList = (x) => (x.keep != null && x.n > x.keep)
      ? ' — and this sweep keeps only ' + x.keep + ', which is fewer than it would remove' : '';
    if(v.kind === 'massTrash')
      return '“' + key + '” — move ' + v.n + ' file' + (v.n === 1 ? '' : 's')
           + ' on this device to the trash?' + '\n\nYour other devices marked them deleted'
           + shortList(v) + '. If you did not delete them somewhere else — or you have just put '
           + 'them back from a backup — cancel: nothing is removed and your files stay exactly '
           + 'where they are.\n\nIf a device still HAS these files, the way to keep them everywhere '
           + 'is “Put them back everywhere” on that device, not Yes here.\n\nNothing is erased '
           + 'either way: a delete here is a move into .pc-trash.';
    if(v.kind === 'massTombstone')
      return '“' + key + '” — tell your other devices to delete ' + v.n + ' file'
           + (v.n === 1 ? '' : 's') + '?\n\nThey are gone from this device' + shortList(v)
           + '. If this device lost sight of the folder rather than you deleting them, cancel '
           + '— nothing changes anywhere.';
    if(v.kind === 'massResurrect')
      return '“' + key + '” — put ' + v.n + ' file' + (v.n === 1 ? '' : 's')
           + ' back on your other devices?\n\nYour other devices deleted them, but they look '
           + 'changed on this one, so syncing would republish them everywhere.\n\nIf these files '
           + 'were restored from a backup or copied in, their timestamps just look new and this is '
           + 'not an edit — cancel, and the deletions stand.';
    return '“' + key + '” — ' + (v.why || 'something unexpected') + '. Go ahead?';
  }

  /* The report in the words the card already speaks. The executor names its buckets after what it
   * does — fetch, send, trash, tombstone — and every screen here was written against the older
   * names, so the translation is done ONCE, here, rather than in fifteen places. */
  function _forTheCard(rep){
    if(!rep) return rep;
    const p = rep.plan || {};
    rep.plan = { upload: p.send || [], download: p.fetch || [], deleteLocal: p.trash || [],
                 deleteRemote: p.tombstone || [], conflicts: p.keepBoth || [],
                 notes: p.settle || [], unchanged: p.unchanged || 0, excluded: p.excluded || 0 };
    for(const v of rep.refused || []){
      if(v.kind === 'massTrash' || v.kind === 'partialViews') rep.refusedTrash = v;
      else if(v.kind === 'massResurrect') rep.refusedResurrect = v;
      else if(v.kind === 'massTombstone' || v.kind === 'partialViewsOut') rep.refusedRemoteDelete = v;
    }
    return rep;
  }

  function summarise(rep, decision){
    if(rep.dryRun){
      const p = rep.plan || {};
      const c = (p.conflicts||[]).length;
      const n = (p.upload||[]).length + (p.download||[]).length + (p.deleteLocal||[]).length
              + (p.deleteRemote||[]).length + c;
      const _unrd = (rep.unreadable || []).length;
      if(!n) return 'already in step' + (_unrd ? ' \u00b7 ' + _unrd + ' path'
        + (_unrd === 1 ? '' : 's') + ' couldn\u2019t be read \u2014 left alone, not counted' : '');
      /* The conflict count is called out separately because a dry run has NOT verified it — see
       * details(). Folding it into one number told someone their phone had hundreds of conflicts
       * when a real sweep would have settled every one of them without making a copy. */
      return n + ' change' + (n>1?'s':'') + ' to make'
             + (c ? ' (' + c + ' of them only if the bytes really differ)' : '')
             /* Or the preview promises downloads the sweep will decline — the store has already
              * said it does not hold those bytes, and only the device that has the files can fix
              * that (by sending them again). */
             + (rep.plannedGone ? ' \u2014 ' + rep.plannedGone + ' of them cannot be fetched right now: '
                + 'the store does not have the bytes' : '');
    }
    /* A SWEEP THAT WAS STOPPED DID NOT FIND A FOLDER IN STEP — it never finished looking.
     *
     * Every line below describes work, so a halted sweep produced an EMPTY list of them and fell
     * through to "in step · nothing to sync": the single most reassuring sentence this card can
     * print, about the one state in which nothing has been checked. Said in front of a folder that
     * was mid-transfer when Pause was pressed, and in front of one whose stop flag was simply never
     * cleared. What it did before it stopped is still worth saying, so it is said. */
    if(rep.stopped){
      const did = [];
      if(rep.uploaded.length) did.push(rep.uploaded.length + ' up');
      if(rep.downloaded.length) did.push(rep.downloaded.length + ' down');
      if(rep.trashed.length) did.push(rep.trashed.length + ' to trash');
      return 'stopped' + (did.length ? ' after ' + did.join(' · ') : ' before anything moved')
             + ' — press Sync now to carry on';
    }
    const bits = [];
    if(rep.uploaded.length) bits.push(rep.uploaded.length + ' up');
    if(rep.downloaded.length) bits.push(rep.downloaded.length + ' down');
    if(rep.trashed.length) bits.push(rep.trashed.length + ' to trash');
    if(rep.conflicted.length) bits.push(rep.conflicted.length + ' conflict' + (rep.conflicted.length>1?'s':''));
    if(rep.failed.length) bits.push(rep.failed.length + ' failed');
    if(rep.skipped.length) bits.push(rep.skipped.length + ' skipped');
    /* These were silently absent: the sweep skipped them, nothing failed, bits stayed empty and the
     * card said "in step" about a folder with paths nothing can fetch. The store does not have the
     * bytes; only the device that has the files can put them back. */
    const _unf = (rep.unfetchable || []).length;
    if(_unf) bits.push(_unf + ' can\u2019t be fetched \u2014 the store doesn\u2019t have those bytes');
    /* An unreadable subtree is neither synced nor deleted — but silence about it reads as "in
     * step", about paths this sweep never saw. */
    const _unr = (rep.unreadable || []).length;
    if(_unr) bits.push(_unr + ' path' + (_unr === 1 ? '' : 's') + ' couldn\u2019t be read on this device \u2014 left alone');
    /* A file being WRITTEN is not a problem, it is a queue: the download finishing is what lets
     * the next sweep take it. Separate from "no permission", which needs a person. */
    const _dsg = (rep.skippedByDesign || []).length;
    if(_dsg) bits.push(_dsg + ' system link' + (_dsg === 1 ? '' : 's') + ' — never synced, by design');
    const _busy = (rep.busyNow || []).length;
    if(_busy) bits.push(_busy + ' file' + (_busy === 1 ? '' : 's') + ' being written right now \u2014 '
      + 'will sync when ' + (_busy === 1 ? 'it settles' : 'they settle'));
    /* A deletion claim the sweep could not PROVE — the file was missing from the listing but its
     * absence could not be positively confirmed. Nothing is deleted anywhere on an unproven claim. */
    const _unc = (rep.unconfirmedAbsent || []).length;
    if(_unc) bits.push(_unc + ' deletion' + (_unc === 1 ? '' : 's') + ' held \u2014 couldn\u2019t be confirmed on disk, so nothing was deleted');
    // Only when it matters: a sweep that peaked over a gigabyte of JS heap names the number and the
    // phase, which is what turns the next out-of-memory report into a diagnosis.
    if((rep.peakHeapMB || 0) > 1024) bits.push('peak memory ' + rep.peakHeapMB + ' MB during ' + (rep.peakHeapPhase || '?'));
    // Said out loud, because "900 up" for files that were never sent is how a working first sweep
    // gets mistaken for the resync bug it is recovering from.
    if(rep.alreadyStored) bits.push(rep.alreadyStored + ' already stored');
    if(rep.adopted) bits.push(rep.adopted + ' already identical here — recorded, not downloaded');
    /* A write another device beat by a moment. Not a failure — the loser's journal forgot the
     * path and the next sweep resolves it as a conflict, both copies kept — but silence about it
     * reads as files quietly vanishing from the count. */
    if(rep.raced) bits.push(rep.raced + ' crossed with another device — sorted next sweep');
    // A checkpoint that could not be stored means the next sweep repeats this work. Say so — the
    // alternative is a progress bar that starts at one again with no explanation anywhere.
    if(rep.checkpointFailed) bits.push('couldn’t save progress (' + rep.checkpointFailed + ')');
    /* FIRST, not appended after "3 up". A sweep that refused to trash ten thousand files has done
     * one thing worth reading, and burying it behind the counts is how a guard becomes a line nobody
     * saw — which is the same silence the guard exists to break. */
    /* THE TWO WAYS A FOLDER LOOKS IN STEP WHILE ANOTHER DEVICE'S DELETIONS SIT HERE UNDONE, both of
     * which used to be reported as an ordinary sweep — the second not reported at all.
     *
     * `resurrected`: files the manifest says were deleted, republished because they look edited
     * here. Correct (delete loses to edit), and indistinguishable from a normal upload in the
     * counts, so a laptop whose timestamps changed under it reads as "3,930 up" and nobody learns
     * that it just undid a deletion.
     *
     * `excluded`: an exclusion means "stop looking at this", so those paths are dropped from all
     * three snapshots and can never be deleted by anyone, ever. A pattern covering the deleted files
     * keeps them for good — silently, since the summary never mentioned exclusions at all. */
    const _res = (rep.resurrected || []).length;
    if(_res) bits.unshift('republished ' + _res + ' file' + (_res === 1 ? '' : 's')
      + ' another device deleted — changed here since');
    if(rep.refusedResurrect) bits.unshift('did NOT republish ' + rep.refusedResurrect.n + ' file'
      + (rep.refusedResurrect.n === 1 ? '' : 's') + ' your other devices deleted');
    /* WHY it was refused, not just that it was. A mass delete and an unreadable device are refused
     * by the same rule and mean opposite things: one is "your other devices really did delete these",
     * the other is "one of your devices did not answer, so I cannot know". Printing the first
     * sentence for the second case tells somebody their files were deleted elsewhere when nothing of
     * the sort has happened. */
    if(rep.refusedTrash && rep.refusedTrash.kind === 'partialViews'){
      bits.unshift('nothing deleted — ' + (rep.refusedTrash.n) + ' file'
        + (rep.refusedTrash.n === 1 ? '' : 's') + ' held back because a device could not be read');
    } else if(rep.refusedTrash){
      /* NAME THE WAY OUT. "kept N files, nothing trashed" is a true report of a refusal and reads
         as a malfunction to somebody who has just deliberately deleted those files somewhere else
         — "no files disappearing". The sweep declines because nobody was there to ask; saying so,
         and saying which button asks, is the difference between a guard and a dead end. */
      bits.unshift('kept ' + rep.refusedTrash.n + ' file'
        + (rep.refusedTrash.n === 1 ? '' : 's') + ' the others say are deleted — press Sync now to '
        + 'be asked about them');
    }
    if(rep.refusedRemoteDelete && rep.refusedRemoteDelete.kind === 'partialViewsOut'){
      bits.unshift('did not publish ' + rep.refusedRemoteDelete.n + ' deletion'
        + (rep.refusedRemoteDelete.n === 1 ? '' : 's') + ' — a device could not be read');
    } else if(rep.refusedRemoteDelete){
      /* The OTHER kind, which said nothing at all: this device has lost sight of most of the folder
       * and the sweep declined to tell everyone else to delete it. That is the single most important
       * thing a sweep can decline to do, and it was silent. */
      bits.unshift('did NOT tell your other devices to delete ' + rep.refusedRemoteDelete.n
        + ' file' + (rep.refusedRemoteDelete.n === 1 ? '' : 's') + ' — this device kept only '
        + (rep.refusedRemoteDelete.keep || 0));
    }
    /* A FINISHED SWEEP DOES NOT BORROW A REASON FROM THE POLICY. `decision.why` answers "why is this
     * running, or not" — "waiting for Wi-Fi", "on battery — changed files only", "you asked for it" —
     * and those belong on a sweep that was SKIPPED, which is where setStatus already puts them.
     * Pasted after a completed one it produced "in step · you asked for it", which reads as a
     * non-answer to a question nobody asked. What someone wants here is what the sweep found. */
    /* EXCLUSIONS RIDE ALONGSIDE, THEY DO NOT COUNT AS SOMETHING HAVING HAPPENED.
     *
     * `rep.excluded` is a standing property of the folder — how many paths its patterns drop — not
     * work this sweep did, and it is set on EVERY sweep. Pushed into `bits` it made the list never
     * empty, so an idle folder that excludes `Old` lost the "in step" line and the checked-file
     * count entirely and read as a bare "5000 excluded": a five-thousand-file number reported by a
     * sweep that did nothing, which is the opposite of what saying it was for. It is here because
     * an exclusion is the quiet reason a deletion never arrives — an excluded path is dropped from
     * all three snapshots and can never be deleted by anyone — so it belongs on the line, not
     * instead of it. */
    const _exc = rep.excluded ? ' · ' + rep.excluded + ' excluded' : '';
    /* PATHS, NOT FILES. `unchanged` counts everything the sweep had to decide about and found
     * settled — which includes tombstones: a deletion both sides already agree on is a path that
     * needs nothing. Calling those "files" is why a folder could read "5,556 files" on its card and
     * "6,159 files checked" on the line underneath, which reads as a contradiction and sent somebody
     * looking for a bug that was not there. */
    /* AN ADVISORY IS NOT A HEADLINE. A folder that checked 11,950 files and moved nothing is IN
     * STEP — and a card whose only line was "13 paths couldn't be read — left alone" read as "no
     * longer syncing" to the person in front of it ("How do you expect me to explain to a user
     * that Documents is no longer syncing"). When the sweep did no real work, the healthy state
     * leads and the advisories follow it; only real work or real failure may take the headline. */
    const _advisory = (b) => /couldn\u2019t be read on this device|being written right now|never synced, by design|deletion.*held|already stored|already identical here/.test(b);
    if(bits.length && bits.every(_advisory) && rep.unchanged){
      const gone = +rep.settledGone || 0;
      return 'in step · ' + (rep.unchanged - gone) + ' file' + ((rep.unchanged - gone) === 1 ? '' : 's')
             + ' checked · ' + bits.join(' · ') + _exc;
    }
    if(!bits.length){
      if(!rep.unchanged) return 'in step · nothing to sync' + _exc;
      const gone = +rep.settledGone || 0;
      /* THE ARITHMETIC, ON SCREEN. "6,159 checked" beside a card reading "5,556 files" is two
       * numbers that cannot both be right, and the missing term is the one nobody can guess. */
      const how = gone
        ? (' (' + (rep.unchanged - gone) + ' file' + ((rep.unchanged - gone) === 1 ? '' : 's')
           + ' + ' + gone + ' deletion' + (gone === 1 ? '' : 's') + ' on record = '
           + rep.unchanged + ' paths checked)')
        : (' (' + rep.unchanged + ' path' + (rep.unchanged === 1 ? '' : 's') + ' checked)');
      return 'in step · nothing to sync' + how + _exc;
    }
    return bits.join(' · ') + _exc;
  }
  /* ---- WATCHING THE OTHER ENGINE --------------------------------------------------------------
   *
   * Only ever a READER. It takes no claim, moves no bytes and writes nothing: it asks the plugin
   * what the native sweep is doing and paints it on the card that could not take the claim.
   *
   * It stops by itself — when the sweep ends, when the numbers stop moving for long enough that
   * they cannot be trusted (a process killed mid-sweep leaves the last line frozen for ever), or
   * when this page starts its own sweep of the same folder. One at a time per folder.
   *
   * A build whose plugin has no `nativeLive` answers null, and null keeps the old sentence — the
   * one honest thing to print about a sweep that cannot say what it is doing. */
  const _phaseAt = new Map();         // folder id -> {phase, t0, i0} — the ETA's memory
  const _natWatch = new Map();
  const _NAT_POLL_MS = 1200, _NAT_STALE_MS = 90000;
  function _watchNative(f, fallback){
    const fs = FS();
    if(!fs || typeof fs.nativeLive !== 'function') return;
    const id = f.id;
    if(_natWatch.has(id)) return;
    const t = setInterval(async () => {
      // The page won the folder in the meantime — its own progress is the better answer.
      if(running.has(id)) return _stopWatchNative(id);
      let live = null;
      try{ live = await fs.nativeLive(); }catch(_){ live = null; }
      if(!live) return _stopWatchNative(id);                    // an APK that cannot answer: leave the line alone
      if(!live.running){
        _stopWatchNative(id);
        // It finished while nobody was looking. Whatever it did is in the stored report, and the
        // next repaint reads that; asking for one is cheaper than duplicating the summary here.
        try{ paint(); }catch(_){}
        return;
      }
      if(live.at && (Date.now() - live.at) > _NAT_STALE_MS){
        setStatus(id, 'syncing in the background — no progress for a while', null, true);
        return;
      }
      const phase = live.phase || 'syncing';
      const n = +live.total || 0, i = +live.done || 0;
      const where = live.path ? ' · ' + String(live.path).split('/').pop() : '';
      setStatus(id, 'in the background: ' + phase + (n ? ' ' + i + '/' + n : '') + where, null, true);
    }, _NAT_POLL_MS);
    _natWatch.set(id, t);
    // Fallback stays on screen until the first answer lands, so nothing flickers through blank.
    if(fallback) setStatus(id, fallback, null, true);
  }
  /* HOW MANY OF THESE FILES THE STORE ACTUALLY HOLDS — the third number, and the one the other two
   * cannot imply. "N here" is this disk, "N in the folder" is what the devices agreed; neither says
   * whether the BYTES are on the server, and that is the difference between a folder that can be
   * restored to a new device and one that only looks like it can. Asked for as "the counter for
   * local files so we can compare what is on the blossom server".
   *
   * ONE REQUEST, not one per file: the store lists every blob this account has, and the records are
   * already in hand, so it is a set intersection. A record whose chunks are all present counts as
   * held; one missing chunk is one file that cannot be fetched, which is exactly how a "complete"
   * folder turns out not to be.
   *
   * "COULD NOT ASK" IS NEVER "MISSING". A listing that fails, times out or comes back as anything
   * other than an array answers null and the chip stays away — the same rule the drive check and the
   * admin store scan already hold, and for the same reason: this number is read as evidence. */
  const _storeSeen = new Map();          // pair key -> {at, ok, missing}
  const _STORE_TTL = 60000;
  async function _storeCount(f, force){
    const key = keyOf(f);
    const hit = _storeSeen.get(key);
    if(!force && hit && (Date.now() - hit.at) < _STORE_TTL) return hit;
    let list = null;
    try{
      const server = (PC.mediaServer && PC.mediaServer()) || '';
      const me = PC.me && PC.me();
      if(!server || !me || !me.pubkey) return null;
      const r = await _bounded(fetch(server + '/list/' + me.pubkey, { cache: 'no-store' }),
                               'store listing', 30000);
      /* A CEILING ON WHAT IS PARSED. An account with a long drive history lists a very large number
       * of blobs, and `r.json()` on a phone builds every one of them as an object before anything
       * can look at them. Over the cap the chip is simply not shown — an unanswerable question is
       * better than a reclaimed renderer, which is the failure this device actually has. */
      if(r.ok){
        const txt = await r.text();
        if(txt.length > 24 * 1024 * 1024) return null;
        list = JSON.parse(txt);
      }
    }catch(_){ list = null; }
    if(!Array.isArray(list)) return null;
    const have = new Set(list.map(b => b && b.sha256).filter(Boolean));
    let st;
    try{ st = await stateS.load(key); }catch(_){ return null; }
    let ok = 0, missing = 0;
    for(const p in st.state){
      const e = st.state[p];
      if(!e || e.deletedAt) continue;
      const ids = (e.chunks && e.chunks.length) ? e.chunks : (e.sha ? [e.sha] : []);
      if(!ids.length){ missing++; continue; }
      if(ids.every(id => have.has(id))) ok++; else missing++;
    }
    const out = { at: Date.now(), ok, missing };
    _storeSeen.set(key, out);
    return out;
  }
  /* Kicked from the paint, never awaited by it — a screen that waits on the network is the failure
   * this codebase keeps paying for. Repaints once when the answer lands. */
  function _storeAsk(f){
    const key = keyOf(f);
    const hit = _storeSeen.get(key);
    if(hit && (Date.now() - hit.at) < _STORE_TTL) return hit;
    if(_storeSeen.get('~asking:' + key)) return hit || null;
    _storeSeen.set('~asking:' + key, true);
    _storeCount(f).then(() => {}).catch(() => {}).then(() => {
      _storeSeen.delete('~asking:' + key);
      if(PC.VIEW === 'sync') paint();
    });
    return hit || null;
  }

  /* THE COUNTS ON ENTRY, not only after you have run something.
   *
   * They were rendered from the last sweep's report, which means a freshly opened screen showed
   * nothing at all — "where is the local and remote counter?" — and the only way to see the two
   * numbers the whole feature is about was to press Sync and wait. Exactly the shape the card's
   * recovery button had, and exactly the wrong way round: the counts are most wanted BEFORE you
   * decide whether to sync.
   *
   * Both sources here are LOCAL and already on disk: this device's journal (what it last applied)
   * and the cached record set. Neither costs a scan or a round trip. A sweep still overrides them
   * with what it actually measured — the journal is what the engine believes, a scan is the truth,
   * and the chip says which it is showing. */
  const _cntSeen = new Map();            // pair key -> {at, here, shared, gone}
  const _CNT_TTL = 30000;
  function _countsAsk(f){
    const key = keyOf(f);
    const hit = _cntSeen.get(key);
    if(hit && (Date.now() - hit.at) < _CNT_TTL) return hit;
    if(_cntSeen.get('~c:' + key)) return hit || null;
    _cntSeen.set('~c:' + key, true);
    /* THE TWO READS ARE INDEPENDENT, AND THE FAST ONE MUST NOT WAIT FOR THE SLOW ONE. The journal
     * is on this disk and answers in milliseconds; the record set can take tens of seconds on a
     * pair this device has not read before (a cold cache, a relay still admitting the key). Awaited
     * together, the local number nobody had to ask anyone for was held hostage to a network read —
     * which is how "where is the local and remote counter?" survived the first attempt at it. Each
     * lands on its own and repaints. */
    const _bump = (patch) => {
      const cur = _cntSeen.get(key) || { here: null, shared: null, gone: null };
      _cntSeen.set(key, Object.assign({}, cur, patch, { at: Date.now() }));
      if(PC.VIEW === 'sync') paint();
    };
    let _left = 2;
    const _done = () => { if(--_left <= 0) _cntSeen.delete('~c:' + key); };
    docs.index(key).then(idx => {
      _bump({ here: Object.keys(idx || {}).filter(p => idx[p] && !idx[p].deletedAt).length });
    }).catch(() => {}).then(_done);
    stateS.load(key).then(st => {
      let shared = 0, gone = 0;
      for(const p in st.state){ if(st.state[p] && st.state[p].deletedAt) gone++; else shared++; }
      _bump({ shared, gone });
    }).catch(() => { /* could not read the folder — say nothing rather than say zero */ }).then(_done);
    return hit || null;
  }

  /* The last background sweep's own report, read once per visit. A phone sweeps with the screen
   * off and cannot ask anybody anything, so its refusals sit here until the app is opened — which
   * is precisely when they have to be readable. */
  let _natLast = null, _natLastAt = 0;
  const _NAT_LAST_TTL = 20000;
  function _readNativeLast(){
    const fs = FS();
    if(!fs || typeof fs.nativeReport !== 'function') return;
    if(Date.now() - _natLastAt < _NAT_LAST_TTL) return;
    _natLastAt = Date.now();
    fs.nativeReport().then(nat => {
      let rep = null;
      try{ rep = nat && nat.report ? JSON.parse(nat.report) : null; }catch(_){ rep = null; }
      const was = _natLast && _natLast.at;
      _natLast = rep;
      if(rep && rep.at !== was && PC.VIEW === 'sync') paint();
    }).catch(() => {});
  }
  /* One sentence, or nothing. Only the states where the sweep has STOPPED and will not resume on
   * its own get a line — a refusal needs a person, and a person needs to know that. */
  function _natLastFor(key){
    const r = _natLast;
    if(!r || r.key !== key) return '';
    if(r.refusedTrash)
      return 'A background sync would have moved files here to the trash and stopped to ask. '
           + 'Press Sync now to see how many and decide — nothing has been removed.';
    if(r.refusedResurrect)
      return 'A background sync would have put files back on your other devices and stopped to ask. '
           + 'Press Sync now to decide, or use “Put them back everywhere”.';
    if(r.refusedRemoteDelete)
      return 'A background sync would have told your other devices to delete files and stopped to '
           + 'ask. Press Sync now to see how many — nothing has been deleted anywhere.';
    return '';
  }
  function _stopWatchNative(id){
    const t = _natWatch.get(id);
    if(t){ clearInterval(t); _natWatch.delete(id); }
  }

  /* ---- IS WHAT I HAVE ACTUALLY WHAT THE FOLDER SAYS IT IS? -------------------------------------
   *
   * The sweep answers "what should change". This answers the question a sweep cannot: are the bytes
   * on this disk the bytes the folder agreed on. It re-hashes every file against the merged record,
   * asks the store whether it still holds what the entries point at, and says which devices disagree
   * with each other. It writes nothing, fetches nothing and deletes nothing.
   *
   * REPAIR IS A SEPARATE, EXPLICIT STEP, and it can only ever pull. A deep SWEEP cannot fix
   * corruption — it re-hashes, sees bytes that differ from the record, and correctly reads that as
   * an edit made here, which would publish the damage to every device. So a repair trashes the local
   * copy (into `.pc-trash`, like every other deletion here) and forgets this device's agreement for
   * that path, which is exactly the state in which the next sweep fetches a fresh copy.
   */
  async function verifyFolder(f){
    const fs = FS();
    if(!fs){ PC.toast('this device has no filesystem access'); return; }
    if(running.size > 0 || _syncQueue.size > 0){
      setStatus(f.id, 'a sync is running — Verify opens again when it finishes', null, true);
      return;
    }
    setStatus(f.id, 'checking your files…', null, true);
    /* HOLD THE PROCESSOR, THE SAME WAY A SWEEP DOES. This reads and re-hashes every file in the
     * folder and then asks the store about every record — minutes of work on a tablet, and a sweep
     * takes a wake lock for exactly that reason. This did not, so the screen could dim mid-check,
     * the process go to the background, and Android reclaim the WebView's renderer: the app comes
     * back rebuilt and the check is simply gone. Released on EVERY exit, including a throw — a lock
     * never given back is a flat battery, which is the one way this could be worse than the bug. */
    const _w = fs;
    let _held = false;
    if(_w && _w.wakeBegin){ try{ await _w.wakeBegin(); _held = true; }catch(_){} }
    let v;
    try{
      v = await EXEC.verify(fs, docs, { id: f.id, key: keyOf(f), deep: true,
        excludes: f.excludes || [],
        onProgress: (ev) => setStatus(f.id, ev.phase + (ev.n ? ' ' + ev.i + '/' + ev.n : ''), null, true) });
    }catch(e){
      setStatus(f.id, 'could not check: ' + ((e && e.message) || e), null, true);
      return;
    }finally{
      if(_held && _w.wakeEnd){ try{ await _w.wakeEnd(); }catch(_){} }
    }
    const bits = [];
    if(v.checked) bits.push(v.checked + ' verified');
    if(v.corrupt.length) bits.push(v.corrupt.length + ' damaged here');
    if(v.missingHere.length) bits.push(v.missingHere.length + ' missing here');
    if(v.extra.length) bits.push(v.extra.length + ' here only');
    if(v.unverified.length) bits.push(v.unverified.length + ' could not be checked');
    if((v.unaddressed || []).length) bits.push((v.unaddressed || []).length
      + ' with no stored copy recorded');
    if((v.missingBytes || []).length) bits.push((v.missingBytes || []).length
      + ' the store no longer holds');
    if(v.undecryptable) bits.push(v.undecryptable + ' record' + (v.undecryptable === 1 ? '' : 's')
      + ' this device could not decrypt');
    setStatus(f.id, bits.length ? bits.join(' · ') : 'everything checks out', null, true);

    /* BYTES THE STORE NO LONGER HAS, ON A DEVICE THAT STILL HAS THE FILE, ARE REPAIRED BY SENDING
     * THEM AGAIN — never by fetching, which is what "repair" means for a damaged local copy and is
     * the exact opposite of what this case needs.
     *
     * Measured on a real folder: the record named files whose blobs existed on NEITHER node. Every
     * other device then plans a download, gets a 404 and reports a failure, on every sweep, while
     * the device holding the file sees nothing wrong — its copy is fine and its journal says it is
     * published. Clearing this device's journal entry for those paths is what makes the next sweep
     * see them as new here and upload them again. */
    if(running.has(f.id)){
      setStatus(f.id, 'a sweep is running — press Stop, then repair', null, true);
      return;
    }
    const here = new Set(v.missingHere || []);
    const gone = (v.missingBytes || []).filter(p => !here.has(p));
    if(gone.length){
      const ok = await PC.uiConfirm('“' + keyOf(f) + '” — the store no longer has the bytes for '
        + gone.length + ' file' + (gone.length === 1 ? '' : 's') + ' this device still holds.\n\n'
        + 'Send them again? Nothing is deleted and nothing is overwritten — the copies here are the '
        + 'good ones, and your other devices cannot fetch them until they are back in the store.');
      if(ok){
        /* SAID OUTRIGHT, not implied by editing the journal. Clearing the journal entry does NOT
         * make the next sweep upload: both sides then read as changed, the reconciler asks whether
         * they are the same anyway, the checksums match — because it is the same file — and it
         * settles. That repair reported "queued to send again" and sent nothing. */
        setStatus(f.id, 'sending ' + gone.length + ' file' + (gone.length === 1 ? '' : 's')
                  + ' again…', null, true);
        swept(f, { manual: true, resend: gone });
        return;
      }
    }

    /* AND THE WAY OUT: entries nobody can fetch and nobody can supply.
     *
     * A path whose bytes the store does not have AND which is not on this device cannot be repaired
     * by anyone — every device plans a download, gets a 404 and reports a failure, on every sweep,
     * for ever. Reported as "243 failures on desktop despite being the source we started with", and
     * there was no way out of it: deleting the local file changes nothing, because the RECORD is
     * what names them.
     *
     * Removing them is safe in the one way that matters: there are no bytes anywhere to lose. It is
     * a tombstone like any other deletion, so the other devices apply it once and stop asking. */
    const phantom = (v.missingBytes || []).filter(p => here.has(p));
    if(phantom.length){
      /* THE SAME RULE AS EVERY OTHER BULK DELETE HERE, and it is not a formality: this publishes
       * TOMBSTONES, and a device that still has the file will move its copy into `.pc-trash` when it
       * reads them. That is the whole point — the record is unusable — but it is a deletion, and the
       * first version of this dialog said "nothing is deleted", which is false in precisely the case
       * the feature exists for. */
      const keeping = Object.keys(v.checkedPaths || {}).length
        || ((v.checked || 0) + (v.unverified || []).length);
      if(phantom.length >= 20 && phantom.length > keeping){
        setStatus(f.id, 'not offering to remove ' + phantom.length + ' entries — that is more than '
                  + 'this folder keeps, so the store is more likely unreachable than empty',
                  null, true);
      } else {
      const ok = await PC.uiConfirm('“' + keyOf(f) + '” — ' + phantom.length + ' file'
        + (phantom.length === 1 ? '' : 's') + ' cannot be fetched: the store does not have the bytes '
        + 'and this device does not have the file.\n\nRemove them from the folder?\n\nThis '
        + 'publishes a deletion. If another device still has one of these files, it will move its '
        + 'copy into .pc-trash — recoverable there, but do this on the device that has the files '
        + 'first if you want them kept (Verify → send them again).');
      if(ok){
        try{
          const r = await edit.removeMany(keyOf(f), phantom);
          PC.toast('removed ' + r.removed + ' unfetchable entr' + (r.removed === 1 ? 'y' : 'ies'));
        }catch(e){ PC.toast('could not remove those: ' + ((e && e.message) || e)); }
      }
      }
    }

    const bad = v.corrupt.map(c => c.path);
    if(!bad.length) return;
    /* THE ONE QUESTION HASHING CANNOT ANSWER, ASKED OF THE PERSON WHO KNOWS.
     *
     * A file whose bytes no longer match the record is either an edit made here in place or a
     * damaged copy, and they are byte-for-byte indistinguishable — that is the entire reason this
     * used to be two buttons. "Deep sync" answered "edit" for all of them and published whatever
     * was on the disk, which spreads corruption to every device; this screen answered "damage" for
     * all of them and offered to overwrite the files from the store, which throws an edit away.
     * Both answers are right some of the time and neither is right by default.
     *
     * So it is asked, once, with the count and the consequence — and the SAFE answer is the
     * default: cancelling here does nothing at all, and the second question (fetch fresh copies)
     * still cannot run without its own yes. */
    const mine = await PC.uiConfirm('“' + keyOf(f) + '” — ' + bad.length + ' file'
      + (bad.length === 1 ? '' : 's') + ' on this device no longer match what your devices agreed '
      + 'the folder holds.\n\nDid YOU change ' + (bad.length === 1 ? 'it' : 'them') + ' on this '
      + 'device?\n\nYes — send ' + (bad.length === 1 ? 'it' : 'them') + ' to your other devices as '
      + 'the new version. Only answer yes if you edited '
      + (bad.length === 1 ? 'this file' : 'these files') + ' here: a damaged copy sent this way '
      + 'replaces the good copy everywhere.\n\nNo — this screen will offer to fetch fresh copies '
      + 'instead.', { ok: 'Yes, these are my edits' });
    if(mine){
      /* Named paths, through the same channel as every other deliberate send — so the mass-restore
       * floor does not question a list a person just reviewed, and the sweep cannot decide to trash
       * them in the pass it was told to send them in. */
      setStatus(f.id, 'sending ' + bad.length + ' edited file' + (bad.length === 1 ? '' : 's')
                + ' to your other devices…', null, true);
      swept(f, { manual: true, resend: bad });
      return;
    }
    const ok = await PC.uiConfirm('“' + keyOf(f) + '” — ' + bad.length + ' file'
      + (bad.length === 1 ? '' : 's') + ' on this device do not match what your devices agreed.\n\n'
      + 'Fetch fresh copies? The copies here move to .pc-trash first — nothing is erased, and '
      + 'nothing damaged is sent to your other devices.');
    if(!ok) return;
    /* NOTHING IS SET ASIDE UNTIL THE REPLACEMENT IS KNOWN TO EXIST.
     *
     * Repair trashes the damaged copy so the next sweep fetches a fresh one — which is only a repair
     * if there IS one. If the store has lost the bytes, this would move somebody's only copy into
     * `.pc-trash` on the strength of a checksum that says it is damaged, which it may be in a way
     * they would still rather have. So each one is checked first, and the ones the store cannot
     * supply are left exactly where they are and named. */
    let done = 0;
    const stranded = [];
    const idx = await docs.index(keyOf(f));
    let merged = null;
    // Guarded: this runs AFTER the person has said yes, and a blip here would throw out of the
    // handler — leaving the status line reading the pre-repair summary and nothing set aside, with
    // only a generic "action failed" to show for it.
    try{ merged = (await stateS.load(keyOf(f))).state; }
    catch(e){
      setStatus(f.id, 'could not check the store before repairing — nothing was changed', null, true);
      return;
    }
    for(const p of bad){
      const e = merged[p] || {};
      const ids = (e.chunks && e.chunks.length) ? e.chunks : (e.sha ? [e.sha] : []);
      let there = !!ids.length;
      for(const id of ids){
        let ok = true;
        try{ ok = await docs.hasBlob(id); }catch(_){ ok = true; }   // unknown is never "missing"
        if(!ok){ there = false; break; }
      }
      if(!there){ stranded.push(p); continue; }
      try{ await fs.trash(f.id, p, Date.now()); delete idx[p]; done++; }
      catch(e2){ console.warn('folder sync: could not set aside ' + p, e2); }
    }
    if(stranded.length){
      PC.toast(stranded.length + ' damaged file' + (stranded.length === 1 ? '' : 's')
               + ' left alone — the store no longer has a copy to replace them with');
    }
    await docs.saveIndex(keyOf(f), idx);
    setStatus(f.id, done + ' set aside — syncing fresh copies', null, true);
    swept(f, { manual: true });
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
    const unf = grp('Can\u2019t be fetched \u2014 the store doesn\u2019t have the bytes',
                    rep.unfetchable || [], a => a.path + ' \u2014 ' + a.why);
    const unc = grp('Deletions held \u2014 absence couldn\u2019t be confirmed on disk',
                    rep.unconfirmedAbsent || [], a => a.path + ' \u2014 ' + a.why);
    /* Named, not tallied — "13 paths couldn't be read" is only actionable if you can see which 13:
     * a file locked by another program, a cloud placeholder, a permission. They are excluded from
     * every decision until they can be read, so the list is the whole story. */
    const unr = grp('Couldn\u2019t be read on this device \u2014 left alone, retried every sweep',
                    rep.unreadable || [], a => String(a))
              + grp('Being written right now \u2014 syncs by itself when the file settles',
                    rep.busyNow || [], a => String(a))
              + grp('System links and junctions \u2014 never synced, by design',
                    rep.skippedByDesign || [], a => String(a));
    if(rep.dryRun){
      return '<div class="sync-details">'
        + grp('Would upload', p.upload, a => a.path + ' — ' + a.why)
        + grp('Would download', p.download, a => a.path + ' — ' + a.why)
        + grp('Would move to trash', p.deleteLocal, a => a.path + ' — ' + a.why)
        + grp('Would remove from the cloud', p.deleteRemote, a => a.path + ' — ' + a.why)
        /* NOT "Conflicts" — a dry run has not checked them, and saying so is the difference between
         * a screen someone can act on and one that frightens them.
         *
         * `if(o.dryRun) return report` sits ABOVE the conflict loop in syncrun, so Check reports what
         * diff() proposed and never runs the three passes that dissolve most of it: hash against
         * `csum`, chunk list against `chunks`, blob address against `sha`. On Android that is the
         * normal case rather than an edge one — SAF gives every file it writes its own
         * last-modified, so an incremental scan's size+mtime can never match the manifest and diff()
         * flags the whole folder. The real sweep then hashes them, finds them identical, and records
         * `unchanged` without duplicating anything. Reported as "phone has many conflicts when
         * clicking check", on a phone that had none. */
        + grp('Possible conflicts — each is checked against the stored copy first, and only a file '
              + 'that really differs is ever duplicated', p.conflicts, a => a.path)
        + '</div>';
    }
    return '<div class="sync-details">'
      + unf + unc + unr
      + grp('Failed', rep.failed, a => a.path + ' — ' + a.what + ': ' + a.error)
      + grp('Skipped', rep.skipped, a => a.path + ' — ' + a.why)
      + grp('Couldn\u2019t be compared \u2014 both copies left alone, retried next sync',
            rep.uncompared || [], a => a.path + ' \u2014 ' + a.why)
      + grp('Conflicts kept', rep.conflicted, a => a.path + ' → ' + a.keptAs)
      + grp('Uploaded', rep.uploaded, a => a)
      + grp('Downloaded', rep.downloaded, a => a)
      + grp('Moved to trash', rep.trashed, a => a.path + ' → ' + a.to)
      // What the guard would not do, named. "Nothing trashed" is only believable next to the list of
      // what it declined to trash — and pressing Sync is what asks about it.
      + grp('Kept — your other devices say these were deleted',
            rep.refusedTrash ? (p.deleteLocal || []) : [], a => a.path + ' — ' + a.why)
      // Named, not tallied. "republished 3,930 files another device deleted" is not something anyone
      // can act on without knowing which — and deciding whether to delete them again is exactly the
      // decision this leaves the user holding.
      + grp('Republished — deleted elsewhere, but changed here since',
            rep.resurrected || [], a => a.path)
      /* THE MEMORY HIGH-WATER MARK, ON SCREEN. The sweep has sampled it all along and nothing ever
         showed it, so every "the app reloaded itself" has been answered with a theory. Chromium
         reclaims a renderer under pressure and the APK says so in a toast — this is the other half:
         how close the sweep got, and to WHAT. */
      + ((rep.peakHeapMB && rep.peakHeapMB >= 200)
          ? '<div class="sync-grp"><b>Memory</b><ul><li>peaked at ' + rep.peakHeapMB + ' MB during “'
            + PC.enc(String(rep.peakHeapPhase || 'the sweep')) + '”'
            + (rep.peakHeapMB >= 700 ? ' — close to what a phone\u2019s browser process is given, '
               + 'which is what makes the screen reload itself' : '')
            + '</li></ul></div>'
          : '')
      + grp('NOT republished — your other devices deleted these',
            rep.refusedResurrect ? (p.upload || []).filter(a => a && a.resurrect) : [],
            a => a.path)
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
  /* The chunk size this device will actually use: the platform's, never above what the node accepts.
   * Cached for the session like maxBytes — it is two settings, and neither moves under a sweep. */
  async function chunkSize(){
    const fs = FS();
    const plat = (fs && fs.chunkBytes) || CHUNK_FALLBACK;
    const srv = await serverMaxBytes();
    return (srv > 0 && srv < plat) ? srv : plat;
  }
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
      /* A PLATFORM THAT CAN CHUNK HAS NO FILE CEILING FROM THE NODE, now that the chunk itself is
       * clamped to what the node accepts (see chunkSize). It used to fall back to the server's
       * limit whenever that was smaller than a chunk — which EXCLUDED every larger file from the
       * scan, so on a node with a small upload limit the folder of videos still never synced, now
       * silently rather than with an error. The clamp is the fix; this was the workaround. */
      _maxBytes = SYNC_MAX_BYTES;
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
  /* Reading two whole files to retire one duplicate is worth it; reading two 4 GB ones is not —
   * that is the renderer kill chunking exists to avoid, and the same bound the sweep's own
   * conflict verification uses.
   *
   * AND IT IS THE SAME BOUND IN THE SAME WRONG UNITS. Two gigabytes is generous on a desktop and
   * meaningless on a phone, where `fs.read` crosses the bridge as base64 before anything reads a
   * byte of it: this button crashed the app on a folder of ordinary photos, exactly as the sweep's
   * conflict verification did. Where the adapter can hash a file ITSELF the read never happens and
   * the old generosity costs nothing; where it cannot, the pair is only compared if this platform
   * can hold it. */
  const _TIDY_VERIFY_MAX = 2 * 1024 * 1024 * 1024;
  function _tidyMax(fs){
    if(fs && typeof fs.hashFile === 'function') return _TIDY_VERIFY_MAX;
    const chunk = (fs && fs.chunkBytes) || 0;
    return chunk > 0 ? chunk : _TIDY_VERIFY_MAX;
  }
  /* One file's content identity, the cheapest way this platform allows — native and streamed where
   * that exists, otherwise the read this function has always done. */
  async function _csumOf(fs, id, rel){
    if(typeof fs.hashFile === 'function'){
      const sha = await fs.hashFile(id, rel);
      if(sha) return sha;
    }
    return store.hashBytes(await fs.read(id, rel));
  }
  async function conflictCleanup(f, opts){
    const fs = FS();
    if(!fs) throw new Error('this device has no filesystem access');
    if(!(opts && opts.dryRun) && (running.size > 0 || _syncQueue.size > 0))
      throw new Error('a sync is running — tidy up when it finishes');
    const key = keyOf(f);
    const man = (await stateS.load(key)).state;
    const list = S.redundantConflicts(man);
    /* IF THE MANIFEST CANNOT PROVE IT, READ THE FILES — this is the one operation where that is the
     * right trade, and without it the button could not clean up the copies it exists for.
     *
     * `redundantConflicts` is deliberately strict: it removes a copy only on a matching `csum` or
     * chunk list, because size+mtime is not evidence for a copy taken FROM a file. But a manifest
     * only carries an identity for entries some sweep happened to upload with one, and a folder that
     * has been through a few devices has plenty that do not. Reported from three machines at once —
     * a phone downloading a pile of `(conflict from windows, …)` copies while every device answered
     * "no conflict copies that are provably identical".
     *
     * So: for a pair the manifest cannot settle, hash BOTH files on this disk and compare. That is
     * exact, it is the same proof `redundantConflicts` demands, and reading is acceptable here in a
     * way it is not during a sweep — somebody pressed a button and is waiting for the answer.
     *
     * SIZE IS A PRE-FILTER, NEVER THE PROOF. Two files of equal length are a candidate and nothing
     * more; only equal hashes qualify. Anything this device does not hold, or cannot read, is left
     * alone rather than guessed at — the whole point of this function is that the alternative to
     * proof is deleting somebody's file. */
    if(!(opts && opts.noVerify) && store.hashBytes && typeof fs.read === 'function' && S.conflictCandidates){
      for(const c of S.conflictCandidates(man)){
        if(c.size > _tidyMax(fs)) continue;
        try{
          const a = await _csumOf(fs, f.id, c.path);
          const b = await _csumOf(fs, f.id, c.original);
          if(a && b && a === b) list.push(Object.assign({}, c, { byRead: true }));
        }catch(_){ /* not on this disk, or unreadable — leave it for a device that has it */ }
      }
    }
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
    /* WHAT THE SWEEP THAT RAN WITHOUT THIS PAGE DECIDED. `_watchNative` says "whatever it did is in
     * the stored report, and the next repaint reads that" — and no repaint ever did: the only
     * reader was the Background details button, which copies a JSON blob to the clipboard. So a
     * background sweep that REFUSED something — the case that most needs a person, and the only
     * case where nothing else will happen until one arrives — was invisible on the screen the
     * person is looking at. Asked once per visit, and only a refusal or a failure is worth a line;
     * an ordinary background sweep says nothing, as it should. */
    _readNativeLast();
    const rows = list.map(f => {
      const st = status.get(f.id) || {};
      const nat = _natLastFor(keyOf(f));
      const pr = prefs(f);
      // A grant can be revoked in system settings, or the drive can be gone. Saying so beats
      // "unknown sync folder" on every sweep forever.
      // Only when the platform has actually answered — never while the question is in flight.
      /* COMPARED NORMALISED, AND OVERRULED BY A RECENT SWEEP. Android grants are honoured by the
       * OS semantically, but this check compared URI STRINGS — an id stored by an older build
       * differs from today's persisted-permission string in nothing but percent-encoding, so the
       * card said "point at the folder again" forever over a folder that was syncing on every
       * sweep (reported from the phone and the laptop in the same afternoon). And whatever the
       * strings say, a folder that completed a sweep minutes ago self-evidently still has its
       * grant — the sweep is the strongest evidence there is. */
      const _gid = u => { try{ return decodeURIComponent(String(u||'')).replace(/\/+$/,''); }
                          catch(_){ return String(u||''); } };
      const recentlyOk = (Date.now() - Math.max(f.lastSyncAt||0, f.lastScanOkAt||0)) < 900000;   // ms
      const lost = Array.isArray(granted) && !granted.some(g => _gid(g.id) === _gid(f.id))
                   && !recentlyOk;
      return `<div class="sync-card" data-id="${PC.enc(f.id)}">
        <div class="sync-head"><b>${PC.enc(keyOf(f))}</b>
          <span class="sync-n muted small">${_countOf(f)}</span>
          <span class="muted small">${PC.enc(f.dir || '')}</span>
          <span class="sync-pair muted small">pairs with “${PC.enc(keyOf(f))}” on your other devices</span></div>
        <div class="sync-status muted small">${lost ? 'this device can’t reach that folder any more — nothing has been lost, point it at the folder again'
          : PC.enc(st.text || 'not synced yet')}</div>
        ${lost ? '<div class="sync-actions"><button class="btn btn-neon small sync-relink">Point at the folder again…</button></div>' : ''}
        ${nat ? `<div class="sync-new"><b>Waiting for you.</b> <span class="muted small">${PC.enc(nat)}</span></div>` : ''}
        ${(() => {
          /* Measured beats remembered: a sweep scanned the disk, the fallback is what this device
             last agreed to. Shown either way, and the tooltip says which. */
          const _c = _countsAsk(f);
          const _r = st.report && st.report.shared != null ? st.report : null;
          const _here   = _r ? _r.here   : (_c ? _c.here   : null);
          const _shared = _r ? _r.shared : (_c ? _c.shared : null);
          const _gone   = _r ? _r.sharedGone : (_c ? _c.gone : null);
          if(_here == null && _shared == null) return '';
          const _how = _r ? 'measured by the last sync' : 'from this device\u2019s record \u2014 press Sync now to re-measure';
          /* THE STORE LISTING IS NOT FETCHED ON ENTRY, and on a phone that is the whole point. It
           * asks the server for EVERY blob this account has — on a drive with a folder-sync history
           * that is a multi-megabyte JSON parsed into an array of objects inside the renderer, and
           * the renderer is exactly what keeps being reclaimed on the tablet ("tablet keeps
           * reloading UI"). The other two counts are local and cost nothing, so they are always
           * there; this one waits until a sweep has run, which is both when the number is worth
           * having and when the person is watching. */
          const _s = _r ? _storeAsk(f) : null;
          return `<div class="sync-counts muted small">
          ${_here == null ? '' : `<span title="Files this device holds in the folder (${_how})">${_num(_here)} here</span>`}
          ${_shared == null ? '' : `<span title="Files your devices agree the folder contains">${_num(_shared)} in the folder</span>`}
          ${_s ? `<span title="Files whose encrypted bytes are on the Blossom server — what a new device could actually fetch">${_num(_s.ok)} in the store</span>` : ''}
          ${(_s && _s.missing) ? `<span class="warn" title="The folder names these files but the store does not hold all of their bytes — no device can fetch them until whoever still has the file sends it again (Check files → send again)">${_num(_s.missing)} not in the store</span>` : ''}
          ${_gone ? `<span title="Deleted files the folder still remembers, so any device can undo them">${_num(_gone)} deleted</span>` : ''}
        </div>`; })()}
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
        <!-- FOUR THINGS STAY OUT ON THE CARD AND THE REST GO IN A MENU.
             Eleven equal-looking buttons is not a set of choices, it is a wall — and the two that
             matter (the one you press every day, and the one that recovers files) were lost in it.
             Out here: the PRIMARY action, anything CONDITIONAL (it only exists when something needs
             attention, and burying a recovery is how this feature earned its reputation), the menu,
             and Stop syncing, which is asked for by name.
             Everything else is one press away and none of it is urgent. -->
        <div class="sync-actions">
          ${pr.paused ? `<button class="btn btn-neon small sync-start">${_ic('play')}Start syncing</button>`
                      : `<button class="btn btn-neon small sync-now">${_ic('refresh')}Sync now</button>`}
          ${(st.report && st.report.refusedResurrect)
            ? `<button class="btn btn-neon small sync-putback">${_ic('restore')}Put ${st.report.refusedResurrect.n} file${st.report.refusedResurrect.n===1?'':'s'} back everywhere</button>` : ''}
          <button class="btn btn-ghost small sync-restore hidden">${_ic('restore')}Restore from trash</button>
          <button class="btn btn-ghost small danger sync-forget">${_ic('close')}Stop syncing</button>
          <button class="btn btn-ghost small sync-more" title="Preview, check, tidy up, trash and background details.">${_ic('menu')}More</button>
        </div></div>`;
    }).join('');

    feed.innerHTML = `<div class="sync-view">
      ${fs ? '' : `<div class="empty">This device can't reach a folder. Folder sync needs the desktop app —
        a browser has no filesystem, and Firefox has no File System Access API at all. Your files are
        still readable here under Files.</div>`}
      <p class="muted small">Build <code>${PC.enc(String(window.__PC_BUILD || 'unknown'))}</code> \u2014 quote this
        when reporting anything here; it is the only way to tell whether a fix is on this device.</p>
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
        /* ONE MAPPING PER PAIR PER DEVICE. Two mappings with the same name share one journal and
         * one record set while scanning two different directories — every sweep then contradicts
         * the last one, permanently. Refused with the reason, not deduped silently. */
        if(l.some(x => keyOf(x) === key)){ PC.toast('“' + key + '” is already syncing on this '
          + 'device — one folder per name'); return; }
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
        if(l.some(x => keyOf(x) === key)){ PC.toast('“' + key + '” is already syncing on this '
          + 'device — one folder per name'); return; }
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
        /* A FOLDER THAT WILL BE FORGOTTEN ON RESTART HAS TO SAY SO NOW. The desktop stores this
         * mapping in the app's own config; if that write failed the pick works, the sweep works, and
         * on the next launch the folder's handle resolves to nothing and the app asks the user to
         * point at it again — every launch, with nothing to explain it. */
        if(picked.persisted === false){
          PC.toast('added — but this device could not save the folder mapping ('
                   + (picked.why || 'unknown error') + '), so it will ask again after a restart');
        }
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
        if(list2.some(x => keyOf(x) === key)){ PC.toast('“' + key + '” is already syncing on this '
          + 'device — one folder per name'); return; }
        /* ADDING A FOLDER STARTS IT CLEAN, WHATEVER WAS LEFT BEHIND.
         *
         * The agreement is keyed on the NAME, so a record from a previous pairing of that name
         * survives the folder being removed — and "Stop syncing" could fail to clear it while
         * reporting success, which left the record live. The next sweep then read a manifest whose
         * entries were deleted elsewhere and offered to move every file in the folder to the trash;
         * repeating the remove-and-re-add did nothing, because the thing that had to change was the
         * record neither step could be trusted to clear.
         *
         * Clearing here removes the whole class: a folder you have just added has, by definition,
         * agreed nothing yet. It is also the SAFE direction — deletion requires an agreement, so a
         * folder with none can only upload, never delete. If it cannot be cleared, say so and stop,
         * because adding the folder anyway is what produces the dialog. */
        try{ await _dropBase(key); await _dropBase(picked.id); }
        catch(e){
          PC.toast('could not clear the old sync record for \u201c' + key + '\u201d — '
                   + ((e && e.message) || e) + '. The folder was NOT added: syncing it now would '
                   + 'offer to delete your files.');
          return;
        }
        /* RE-READ THE LIST HERE, NOT BEFORE THE PROMPT.
         *
         * `list2` was taken before `PC.uiPrompt` — seconds of somebody typing a name — and then
         * written back afterwards, so any other writer in that window either lost its change or
         * overwrote this one with its own older copy. A sweep recording `lastSyncAt`, the watcher,
         * a repaint: all of them write this list. The folder was added and then quietly vanished,
         * which is why it took a second attempt to stick.
         *
         * The stale copy is still used for the duplicate check above — that is a question about what
         * existed when the user picked, and asking it again here would only re-answer it. */
        const list3 = folders();
        if(list3.some(x => x.id === picked.id)){ PC.toast('that folder is already syncing'); return; }
        list3.push({ id: picked.id, key, dir: picked.dir, name: key,
                     excludes: [], prefs: { paused: true }, lastSyncAt: 0, lastFullScanAt: 0 });
        /* THE PICK IS A GRANT. `granted` was fetched when the screen painted, so the folder just
         * added is not in it, and the very first repaint drew "Point at the folder again…" on a
         * card that was already syncing — every add, for as long as the button takes to scroll off
         * the platform's next answer. The platform handed us this handle seconds ago; record it. */
        if(Array.isArray(granted) && !granted.some(g => g && g.id === picked.id))
          granted.push({ id: picked.id, dir: picked.dir });
        saveFolders(list3); rememberPair(picked.id, picked.dir, key); watch(picked.id); paint();
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
      /* EVERY ACTION IS A NAMED FUNCTION, AND THE MENU AND THE CARD BOTH CALL IT.
       * Half of these used to be bound as `card.querySelector('.sync-X').onclick = …` with no null
       * check, which is fine only while every button is always in the markup — the moment one moves
       * behind a menu that line throws, this whole binding function aborts, and EVERY control below
       * it silently stops working. That is the "three unrelated bugs at once" shape: the card looks
       * perfect and nothing is in any log. */
      const _doPreview = () => swept(get(), { manual:true, dryRun:true });
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
      const _doPause = () => {
        put(f => { f.prefs = Object.assign({}, f.prefs, { paused: true }); });
        stopping.add(id);                       // and stop the sweep that is running RIGHT NOW
        setStatus(id, running.has(id) ? 'stopping…' : 'paused — press Start when you want it to run again');
        paint();
      };
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
      /* ONE CHECK, NOT TWO. "Deep check" and "Verify" both re-read and re-hash every file in the
       * folder — the entire expensive half is identical — and they differed only in what they did
       * with the answer: the deep one SYNCED it (publishing whatever the bytes now are), the other
       * only reported. Two buttons for one job, and the one that sounded like an inspection was the
       * one that moved files ("cant you combine Deep and Verify? This is silly to have 2
       * functions").
       *
       * They cannot simply be added together, and that is the whole reason they were separate: a
       * file whose bytes no longer match the record is EITHER an edit you made in place or a
       * damaged copy, and no amount of hashing can tell which. Syncing assumes the first and
       * publishes damage everywhere; repairing assumes the second and overwrites your edit.
       *
       * So they are one action that looks FIRST and then asks, which is what a person could have
       * answered all along. `verifyFolder` reads without writing, reports, and puts the choice
       * where the evidence is. */
      const _doCheck = () => verifyFolder(get());
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
      /* \u267b RESTORE FROM TRASH. Everything a sweep ever "deleted" is sitting intact under
       * .pc-trash mirroring the folder's own structure, and telling somebody to go move it back in
       * a file manager is not a feature. Enumerated by the bridge, moved back by the ordinary
       * move(), NEVER overwriting: a path that exists again is skipped and its trash copy left in
       * place. The button names its count before it does anything. */
      { const rb = card.querySelector('.sync-restore');
        if(rb){
          (async () => {
            try{ const rows = FS().listTrash ? await FS().listTrash(id) : [];
              /* innerHTML, not textContent: the label carries the sprite <svg>, and textContent
                 would drop the icon and leave the button visibly different from its neighbours. */
              if(rows && rows.length){ rb.innerHTML = _ic('restore') + 'Restore ' + rows.length + ' file'
                    + (rows.length === 1 ? '' : 's') + ' from trash';
                rb.classList.remove('hidden'); } }catch(_){}
          })();
          rb.onclick = async () => { rb.disabled = true;
            try{ await restoreTrash(id); }finally{ rb.disabled = false; paint(); } };
        } }
      /* THE WAY OUT OF A STANDOFF, AND IT HAD TO BE A BUTTON.
       *
       * "NOT republished — your other devices deleted these" is a true report of a refusal and a
       * dead end: the sweep declines every time, the message returns every time, and the only thing
       * offered was the same mid-sweep dialog that started the round trip. Reported as "it always
       * wants to republish the conflict files… if I click ok it just restarts the drama".
       *
       * This names the paths instead of re-deciding them. They go through `resend`, which is the
       * caller saying "send these, I mean it" — not an inference from a timestamp — so the
       * resurrect floor does not apply to them (see the executor) and the files come back on every
       * device on its next sweep. One press, and the folder converges instead of oscillating. */
      { const pb = card.querySelector('.sync-putback');
        if(pb) pb.onclick = async () => {
          const f = get(); if(!f) return;
          const rep = (status.get(f.id) || {}).report || {};
          const paths = ((rep.plan || {}).upload || []).filter(a => a && a.resurrect).map(a => a.path);
          if(!paths.length){ PC.toast('nothing left to put back — sync again to look afresh'); return; }
          if(!await PC.uiConfirm('Put ' + paths.length + ' file' + (paths.length === 1 ? '' : 's')
             + ' back on every device that syncs \u201c' + keyOf(f) + '\u201d?\n\nThese are files this '
             + 'device still has and your other devices marked deleted. They are uploaded from THIS '
             + 'copy, and every other device fetches them back on its next sweep.\n\nIf you meant '
             + 'the deletions to stand, cancel and press Sync — it will ask before removing '
             + 'anything here.')) return;
          pb.disabled = true;
          try{ await swept(f, { manual: true, resend: paths }); }
          finally{ pb.disabled = false; paint(); }
        }; }
      const _doTidy = async () => {
        const f = get(); if(!f) return;
        setStatus(f.id, 'looking for redundant copies…');
        let found;
        try{ found = await conflictCleanup(f, { dryRun:true }); }
        catch(e){ PC.toast('couldn’t check: ' + ((e && e.message) || e)); paint(); return; }
        /* SAY WHICH QUESTION WAS ASKED. "No conflict copies" next to a Check reporting hundreds of
         * "conflicts" reads as a contradiction, and it is not: Check lists files that MIGHT be
         * duplicated on the next sweep (and on Android almost never are — see details()), while this
         * looks for copies already sitting on the disk named "(conflict from …)". Two different
         * things, one word. */
        if(!found.list.length){
          PC.toast('no duplicate copies to remove — every “(conflict from …)” file here differs from '
                   + 'its original, or its original is gone');
          paint(); return;
        }
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
      const _doEmptyTrash = async () => {
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
      /* THE ONLY PLACE ANY OF THIS CAN BE OBSERVED. The counters exist because this failure reports
       * SUCCESS — an alarm that never fires, a tick emitted into a dead page and a sweep that ran
       * are all silence from every other vantage point — and counters nothing renders are no better
       * than no counters, which is what shipped in the first version of them. */
      const _doBg = async () => {
          let st = null;
          try{ st = await FS().tickStats(); }catch(_){ st = null; }
          if(!st){ PC.toast('this build can’t report background sync'); return; }
          const ago = (t) => !t ? 'never' : Math.round((Date.now() - t) / 60000) + ' min ago';
          /* THE CLOCK IS THE FIRST LINE, because for the whole life of this feature it was the
           * answer and nothing reported it: the alarm lived inside "Stay connected", which is off by
           * default, so a phone that had never touched that switch had no clock at all and every
           * counter below it read zero — indistinguishable from an alarm that fires and is eaten. */
          const line = 'clock: armed ' + ago(st.lastArmedAt) + ', last fired ' + ago(st.lastFiredAt)
            + (st.clockPeriodMin ? ' · every ' + st.clockPeriodMin + ' min' : '')
            + (st.clockExact === false ? ' · INEXACT alarm (no exact-alarm permission)' : '')
            + '\nalarms: ' + st.armed + ' scheduled, ' + st.fired + ' fired'
            + '\nsweeps: ' + (st.foreground || 0) + ' as a foreground service, '
            + (st.job || 0) + ' as a background job'
            + ((st.foregroundRefused || 0)
                 ? ' (' + st.foregroundRefused + ' foreground starts refused by Android)' : '')
            + (st.sweepServiceUp ? ' · sweeping now' : '')
            + '\nticks to the app: ' + st.delivered + ' delivered, ' + st.dropped + ' dropped, '
            + st.suppressed + ' skipped (last delivered ' + ago(st.lastDeliveredAt) + ')'
            + '\nwaiting for: ' + ([st.needCharging && 'a charger', st.needUnmetered && 'Wi-Fi']
                                     .filter(Boolean).join(' + ') || 'nothing')
            // Reported last, and only as context: background sync no longer needs it.
            + '\nstay-connected ' + (st.stayConnected ? 'ON' : 'off')
            + ' (not required for syncing)';
          /* AND WHAT THE SWEEP THAT RUNS WITHOUT THIS PAGE ACTUALLY DID.
           *
           * The counters above answer "did the clock tick", which was the right question while the
           * sweep was JavaScript the tick could only ASK to run. It is Java now, and the ways that
           * fails are invisible from here in the same way: an account whose key is not on the device
           * (Amber), a folder deferred because it has never synced, a manifest the server refused to
           * shrink. Every one of those is a phone that ticks perfectly and syncs nothing, so the
           * reason it gives itself has to be readable. */
          let nat = null;
          try{ nat = FS().nativeReport ? await FS().nativeReport() : null; }catch(_){ nat = null; }
          let extra = '';
          if(nat){
            extra = '\nnative sweep: ' + (nat.enabled ? 'on' : 'OFF' + (nat.why_off ? ' — ' + nat.why_off : ''))
                  + (nat.haveKey ? '' : ' (no key on this device — Amber signs elsewhere)')
                  + (nat.running ? ', running now' : '')
                  + (nat.why ? '\n  last decision: ' + nat.why : '')
                  + (nat.report ? '\n  last run: ' + nat.report : '\n  last run: never');
          }
          /* COPIED, NOT PRINTED ON THE CARD. This went to the status line, which a running sweep
           * overwrites with its per-file progress several times a second — so the one reading that
           * explains why background sync is not working was unreadable on the one device it
           * describes, and could not be quoted to anybody either. copyValue puts it on the clipboard
           * (the APK's WebView refuses navigator.clipboard, which is why this helper exists) and
           * falls back to a dialog that STAYS until dismissed when even that is refused. */
          const full = line + extra;
          PC.copyValue ? PC.copyValue(full, 'background details copied', 'Background sync:')
                       : setStatus(id, full.replace(/\n/g, ' · '));
      };
      /* THE MENU. One list, built from what this device and this folder can actually do — a row
       * that offers Pause on a paused folder, or Background details on a desktop, is a row that
       * teaches people not to read it. Every entry calls the SAME function the card would have. */
      { const more = card.querySelector('.sync-more');
        if(more) more.onclick = () => {
          /* PLAIN WORDS. `openMenuPopover` renders each label through `enc()` — markup in a label
             comes out as literal angle brackets — so a sprite cannot go in one without changing a
             component every other menu in the app shares. Emoji would render, which is exactly
             what is not wanted, so the rows say what they do and nothing else. */
          const items = [['preview', 'Preview \u2014 what would change']];
          if(!prefs(get()).paused) items.push(['pause', 'Pause syncing']);
          items.push(['check', 'Check files \u2014 read every one, change nothing']);
          items.push(['tidy', 'Tidy up conflict copies']);
          items.push(['trash', 'Empty trash']);
          if(FS() && FS().tickStats) items.push(['bg', 'Background sync details']);
          const pick = (a) => {
            if(a === 'preview') return _doPreview();
            if(a === 'pause') return _doPause();
            if(a === 'check') return _doCheck();
            if(a === 'tidy') return _doTidy();
            if(a === 'trash') return _doEmptyTrash();
            if(a === 'bg') return _doBg();
          };
          if(PC.openMenuPopover) PC.openMenuPopover(more, items, pick);
          else pick('check');            // a shell without the popover still reaches the main one
        }; }
      card.querySelector('.sync-forget').onclick = async () => {
        if(!await PC.uiConfirm('Stop syncing this folder?\n\nNothing is deleted — the files stay on this '
                               + 'device and on your other devices. It simply stops being kept in step.')) return;
        try{ await FS().forget(id); }catch(_){}
        // If the agreement cannot be cleared, STOP: removing the card while it survives is what
        // makes the next re-add propose trashing the whole folder, with nothing to explain why.
        let cleared = true, why = '';
        try{
          const f0 = folders().find(x => x.id === id);
          await _dropBase(id);
          if(f0) await _dropBase(keyOf(f0));
        }catch(e){ cleared = false; why = (e && e.message) || String(e); }
        if(!cleared){
          PC.toast('could not clear this folder\u2019s sync record — ' + why
                   + '. Nothing was changed; re-adding it now would offer to delete your files.');
          return;
        }
        /* CLEAR THE AGREEMENT UNDER THE KEY IT WAS WRITTEN WITH — and under the old one too.
         *
         * `base` moved from the platform id to the pair key when the manifest did; this removeItem
         * did not move with it, so "Stop syncing" left the agreement behind. Re-adding the folder
         * later then starts from a base claiming files are synced that are no longer on this disk,
         * the engine correctly reads that as "deleted here", and they are removed from every other
         * device. Both keys go, because a build older than the pair key wrote the id one.
         * tests/client/two_device_sim.js — 'stale-base-is-what-deletes-everything'. */
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

  /* TELL THE NATIVE CLOCK WHAT THE SWITCHES SAY, so it can stop waking the phone for nothing.
   *
   * `shouldSync` is still the decision — this only lets the alarm skip a tick it was certain to
   * decline. EVERY, not ANY: `needCharging` is true only when every folder that could otherwise run
   * requires a charger, because one folder willing to sync on battery must still be able to, and
   * suppressing on the strictest folder's preference would silently stop the others. A PAUSED folder
   * is excluded from the vote entirely — it is not waiting on a charger, it is not started.
   *
   * With no folders at all the answer is false/false: an alarm that suppresses everything would be
   * indistinguishable from the clock being broken, which is the state this whole mechanism exists to
   * get out of. */
  function _pushTickPolicy(){
    const fs = FS();
    if(!fs || !fs.setTickPolicy) return;                 // an APK older than the tick
    const live = folders().map(prefs).filter(p => p.enabled !== false && !p.paused);
    const all = (k) => live.length > 0 && live.every(p => !!p[k]);
    try{
      const r = fs.setTickPolicy({ needCharging: all('onlyWhenCharging'), needUnmetered: all('wifiOnly') });
      if(r && typeof r.catch === 'function') r.catch(()=>{});
    }catch(_){}
  }

  /* ---- HAND THE PHONE WHAT IT NEEDS TO SWEEP WITHOUT THIS PAGE --------------------------------
   *
   * Chromium throttles a hidden page's JavaScript however awake the processor is — a browser policy,
   * not a power one — so on Android the transfer also exists in Java. Everything it needs is
   * something only this page knows: which instance, which media server, which folders are paired to
   * which trees here, what is excluded, what the switches say, and the account's drive key.
   *
   * THE KEY GOES OVER WRAPPED. `PC.driveKeyWrapped()` is the NIP-44 self-wrapped value the drive
   * index already publishes; the phone unwraps it with the account secret its own signer holds. An
   * account signed in through Amber has no such secret on the device, so the native path answers
   * "not my job" and the old tick-the-WebView behaviour is what runs — which is the honest outcome,
   * since nothing on that phone can sign an upload.
   *
   * PUSHED ON EVERY SWEEP AND AT STARTUP, because a stale copy is the whole failure mode: a folder
   * removed here and still swept there, an exclusion typed here and ignored there. An EMPTY key does
   * not erase the stored one (the signer may not have answered yet) — see SyncStore.configure. */
  async function _pushNativeConfig(){
    const fs = FS();
    if(!fs || !fs.configureNative) return;               // desktop, or an APK older than this
    let mk = '';
    try{ mk = (PC.driveKeyWrapped && PC.driveKeyWrapped()) || ''; }catch(_){}
    const list = folders().map(f => {
      const p = prefs(f);
      return { key: keyOf(f), id: f.id, excludes: f.excludes || [],
               enabled: p.enabled !== false, paused: !!p.paused,
               onlyWhenCharging: !!p.onlyWhenCharging, wifiOnly: p.wifiOnly !== false,
               minBattery: p.minBattery };
    });
    /* AN ABSOLUTE BASE, AND `location.origin` IS THE WRONG ONE. Every fetch in this page is relative
     * and that is right, but the native sweep is not in this page — in the bundled app the page's
     * origin is `https://localhost`, which resolves to the app's own bundle and reaches nothing at
     * all. `PC.serverOrigin()` is the instance the shim injected, and it answers '' when there is no
     * instance (standalone), which is what turns the whole thing off rather than pointing the phone
     * at itself. */
    let api = '';
    try{ api = (PC.serverOrigin && PC.serverOrigin()) || ''; }catch(_){}
    const media = (() => { try{ return (PC.mediaServer && PC.mediaServer()) || ''; }catch(_){ return ''; } })();
    // Only with a key, a server, somewhere to put the bytes AND a folder. Without any of them the
    // phone would wake, fail every folder and write a report saying so, every sixteen minutes.
    const wanted = !!mk && !!api && !!media && list.length > 0;
    /* THE KEY IS ARMED ON `list.length`, NOT ON `wanted`, and that is a chicken-and-egg fix.
     *
     * `mk` is the drive key, which is exactly the value most likely to be missing on a cold start —
     * so gating the arming on it meant the one push that could have armed the key was the one push
     * that had nothing to arm with, on every launch. The phone then had folders, a server and no
     * key, which is the state the sweep declines in. Paired with a device: the native side no longer
     * lets an empty push switch anything off (SyncStore.nativeEnabled is derived now), so arming
     * early and configuring fully a moment later converges instead of fighting. */
    const haveFolders = list.length > 0;
    /* THE ACCOUNT KEY, AND ONLY ONCE THIS DEVICE ACTUALLY SYNCS SOMETHING.
     *
     * The native sweep signs every network step, so it needs the account secret sealed in the
     * Android keystore — and the only two things that ever put one there were the "Sign for other
     * apps on this phone" switch and pairing a laptop over NIP-46. Neither has anything to do with
     * syncing a folder, so on an ordinary account the sweep answered "the account key is not on this
     * device" about a key this page was holding, and background sync could not run at all. Reported
     * as syncing stopping shortly after the screen goes off, on two devices.
     *
     * GATED ON `wanted`, not called unconditionally: this function runs at startup on EVERY Android
     * launch, so arming here regardless would seal the nsec into the keystore of every local-key user
     * on the platform, including everyone who has never opened Folder Sync. Sealing a key is a
     * security upgrade over the WebView storage it already sits in, but it is not free — it is what
     * makes an unattended process able to sign as you — so it is asked for by the one feature that
     * needs it, when it needs it.
     *
     * Awaited but never fatal: an Amber/bunker account has nothing to hand over and answers false,
     * which is the honest outcome — that phone keeps the ask-the-page path, because nothing on it
     * can sign an upload unattended. See PC.armNativeSigner. */
    if(haveFolders){ try{ if(PC.armNativeSigner) await PC.armNativeSigner(); }catch(_){} }
    try{
      await fs.configureNative({
        enabled: wanted,
        apiBase: api,
        mediaBase: media,
        mkWrapped: mk,
        /* THE ID, NOT THE NAME. It names the document this device publishes, and the native sweep
         * must write the same one the page does — two ids is two documents for one device, each
         * holding half the folder's history and each looking to the other like a device that has
         * gone quiet. */
        device: deviceId(),
        folders: list,
      });
    }catch(_){}
  }

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
    _pushTickPolicy();
    try{ const r = _pushNativeConfig(); if(r && r.catch) r.catch(()=>{}); }catch(_){}
    /* THE DESKTOP'S OWN VERSION OF THE SAME HOLE, and the shell already had the signal.
     *
     * A laptop that sleeps wakes up with NOTHING telling this module: desktop/main.js says it in as
     * many words — a window that was never hidden fires no `visibilitychange`, no `pageshow`, and
     * `online` only if Chromium decided the interface went down, which a suspend usually does not.
     * That is exactly why `pc:wake` exists. app.js subscribes to it to redial the relay sockets;
     * folder sync never did, so after a resume its only remaining trigger was the 15-minute
     * heartbeat.
     *
     * AND THE WATCHER IS RE-ARMED, which is the half that does not come back on its own. A suspend
     * can take the `fs.watch` handle with it, and fsbridge's watcher swallows its own `error` event
     * (rightly — a dying watch must not take the process down) with nothing to re-establish it. So
     * the machine stops noticing LOCAL edits too, silently, until the app is restarted. `watch()`
     * unwatches first, so re-arming is idempotent and cannot double-fire.
     *
     * Not forced: `_idle()` is already false whenever `pcShell` exists, so `force` would only blur
     * what it means — a background tick nobody is watching. */
    try{
      if(window.pcShell && window.pcShell.onWake){
        window.pcShell.onWake(() => {
          folders().forEach(f => watch(f.id));
          nudge('wake');
        });
      }
    }catch(_){}
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
  // `docs` is the per-device document layer: Files borrows it to read a folder it does not hold,
  // and the tests drive it directly at the sizes where NIP-44's ceiling used to lose whole folders.
  window.PCSync = { paint, folders, sweep, startAll, store, docs, status, edit, verifyFolder, restoreTrash,
                    accountFolders, acct: () => _acct, deviceId,
                    /* Anything destructive asks this first: reclaim, verify-repair, tidy. A sweep
                     * mid-flight makes "unreferenced" and "redundant" unstable answers. */
                    busyNow: () => running.size > 0 || _syncQueue.size > 0 };
})();
