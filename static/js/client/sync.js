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
  const S_ENGINE = window.PCSyncEngine;
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
      /* `entries` IS PLAINTEXT AND HAS TO BE, for the same reason `n` is.
       *
       * The paths are sealed, so a server looking at this document cannot tell "no entries at all"
       * from "every entry is a tombstone" — both report n = 0. That difference decides whether a
       * folder still exists: a forgotten folder must leave the account's list, and a folder whose
       * files were all deleted must NOT, because its tombstones are how another device learns they
       * were deleted. Without this the wipe worked and the folder stayed listed for ever.
       *
       * It discloses one number — how many paths the manifest holds — to a server that already
       * learns the live count for the collapse guard. It says nothing about what they are. */
      const doc = { n: live, entries: Object.keys(paths).length };
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
      ? (read, size, onProgress, cs) => {
          const w = _stallGuard('upload');
          return Promise.race([
            PC.syncBlobs.putParts((off, len) => { w.bump(); return read(off, len); }, size,
                                  (done, total) => { w.bump(); if(onProgress) onProgress(done, total); },
                                  cs),
            w.tripped,
          ]).then(v => { w.stop(); return v; }, e => { w.stop(); throw e; });
        } : null,
    getParts: PC.syncBlobs && PC.syncBlobs.getParts
      ? (chunks, write) => {
          const w = _stallGuard('download');
          return Promise.race([
            PC.syncBlobs.getParts(chunks, (off, bytes) => { w.bump(); return write(off, bytes); }),
            w.tripped,
          ]).then(v => { w.stop(); return v; }, e => { w.stop(); throw e; });
        } : null,
  };

  /* ---- THE FOLDER, AS EVERY DEVICE DESCRIBES IT -------------------------------------------------
   *
   * One document per device, and nothing but that device ever writes it:
   *
   *     pcai:sync:<pair>:<device>
   *
   * Reading is therefore a plain read of them all, and writing is a plain write of ours. There is no
   * merge on save, no re-read per checkpoint, no compare-and-swap and no server-side collapse guard
   * to get wrong — those all existed to make ONE document survive several writers, and it did not.
   *
   * A DEVICE THAT COULD NOT BE READ IS COUNTED, NEVER SKIPPED. Left out, its files are absent from
   * the merge, and absent is indistinguishable from deleted — which is the confusion that emptied a
   * Pictures folder. The count travels with the answer and the checker refuses every deletion while
   * it is above zero.
   */
  const docs = {
    /** {views: {device: {path: entry}}, missing: n} — throws if the server could not be asked. */
    async views(key){
      const j = await store._post({ folder: key, views: true });
      const raw = (j && j.views) || {};
      const views = {};
      // WHICH devices could not be read, not just how many. A view that cannot be opened holds back
      // every deletion in the pair, and the only way out is for somebody to say "that one is gone" —
      // which they cannot do if nothing will name it.
      const cannot = Array.isArray(j && j.cannot) ? j.cannot.slice() : [];
      // The sealed path-list blobs are REFERENCES too: a storage reclaim that cannot see them would
      // delete the very blob a manifest's paths live in. Collected off the raw docs, before opening.
      const sealedIds = [];
      for(const dev of Object.keys(raw)){
        const d0 = raw[dev];
        if(d0 && d0.pathsSha) sealedIds.push(d0.pathsSha);
      }
      let missing = +(j && j.unreadable) || 0;
      for(const dev of Object.keys(raw)){
        try{ views[dev] = await _openDoc(raw[dev]); }
        catch(e){
          missing++;
          /* A DEVICE THAT CANNOT BE READ AND A SERVER THAT BLINKED ARE NOT THE SAME THING, and only
           * one of them is worth offering to retire. `_openDoc` throws for both: a media server that
           * 502s while fetching the sealed path list, and a document this account has no key for.
           * The first is a minute-long outage; the second is a fact about the record.
           *
           * Only the second joins `cannot`, because that list is what the repair screen offers to
           * retire — and retiring a LIVE device on the strength of a blip zeroes its published
           * record, which takes its paths out of the merge until it next sweeps. */
          const why = String((e && e.message) || e);
          if(/decrypt|drive key|damaged/i.test(why)) cannot.push(dev);
          console.warn('folder sync: could not open ' + dev + '\u2019s view', e);
        }
      }
      /* The single shared document older builds still write, read as one more view. It carries no
       * versions, so its entries compare by content — which is exactly what this engine did before
       * versions existed, and it is how a pair that predates this upgrade keeps working. */
      if(j && j.legacy){
        if(j.legacy.pathsSha) sealedIds.push(j.legacy.pathsSha);
        try{ const v = await _openDoc(j.legacy); if(Object.keys(v).length) views['(shared)'] = v; }
        catch(e){ missing++; }
      }
      return { views, missing, cannot, sealedIds };
    },
    /** Publish OUR view. One writer, so this is a write and nothing else. */
    async publish(key, entries){
      const doc = await _sealDoc(entries);
      await store._post({ folder: key, device: deviceId(), manifest: doc, force: true });
    },
    /* RETIRE ONE DEVICE'S RECORD — the escape hatch for the rule above.
     *
     * A view that cannot be read holds back every deletion in the pair, for ever and silently. That
     * is the right default (absent from the merge is indistinguishable from deleted), but it needs a
     * way out, or a phone that was thrown away can freeze a folder permanently. Named explicitly by
     * a person: it is never inferred from a failed read, because a failed read is exactly what a
     * device that is merely offline looks like. */
    async retire(key, device){
      await store._post({ folder: key, forgetDevice: device });
    },
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
        const r = await fetch(PC.mediaServer() + '/' + sha, { method:'HEAD', cache:'no-store' });
        if(r && r.ok) return true;
        if(r && (r.status === 404 || r.status === 410)) return false;
        return null;
      }catch(_){ return null; }
    },
  };

  /* A document's paths, whether they are sealed inline or in an encrypted blob. Fetch and decrypt
   * fail for opposite reasons and must not share a message: one is a media-server problem and the
   * other is a key problem, and the fix for each is the other's mistake. */
  async function _openDoc(doc){
    if(!doc || typeof doc !== 'object') return {};
    if(doc.pathsSha){
      let bytes;
      /* BOUNDED. `fetch` imposes no timeout of its own, and this is a media-server request made from
       * a SCREEN — Files → Synced folders opens a folder by reading its records. A server that
       * accepts the connection and never answers leaves that await pending for ever: the view sits
       * blank, nothing errors, and nothing can be retried. Reported as "Blossom is hanging trying to
       * load the Posts folder that emptied". */
      try{ bytes = await _bounded(PC.syncBlobs.get(doc.pathsSha), 'media server', _POST_TIMEOUT_MS); }
      catch(e){
        const m = String((e && e.message) || e);
        if(/OperationError|decrypt|importKey|drive key/i.test(m))
          throw new Error('this device cannot decrypt that folder list (' + m + ')');
        throw new Error('could not fetch that folder list from the media server (' + m + ')');
      }
      try{ return JSON.parse(new TextDecoder().decode(bytes)) || {}; }
      catch(_){ throw new Error('that stored folder list is damaged'); }
    }
    if(doc.sealed){
      try{ return JSON.parse(await PC.nip44dec(PC.me().pubkey, doc.sealed)) || {}; }
      catch(e){ throw new Error('could not decrypt that folder list'); }
    }
    return doc.paths || {};
  }

  /* Our view, ready to publish. Past ~45 KB the paths move into an encrypted blob and the document
   * keeps a pointer — NIP-44 refuses a plaintext over 65535 bytes, and at ~174 bytes an entry that
   * ceiling arrives at about 376 files. `n` and `entries` stay in the clear because the server reads
   * them: one is the count it guards on, the other is how it tells a folder somebody forgot from one
   * whose files were all deleted. Neither says anything about what the files are. */
  async function _sealDoc(entries){
    const paths = entries || {};
    const live = Object.keys(paths).filter(p => paths[p] && !paths[p].deletedAt).length;
    const json = JSON.stringify(paths);
    const doc = { n: live, entries: Object.keys(paths).length, by: deviceId() };
    if(json.length < MANIFEST_INLINE_MAX){
      doc.sealed = await PC.nip44enc(PC.me().pubkey, json);
    } else {
      const put = await PC.syncBlobs.put(new TextEncoder().encode(json));
      doc.pathsSha = (put && typeof put === 'object') ? put.sha : put;
      /* Still set, to something that cannot decrypt: a client older than the blob split looks for
       * `sealed`, falls back to `doc.paths`, and would read this as an EMPTY view. */
      doc.sealed = 'v2:' + doc.pathsSha;
    }
    return doc;
  }

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
  /* AN EDIT MADE FROM FILES IS THIS DEVICE'S CLAIM, LIKE ANY OTHER.
   *
   * Files → Synced folders can add, rename and delete in a folder this browser does not hold — it
   * edits the record, and the devices carry it out on their next sweep through the same paths they
   * always use. Under one shared document that needed a read, a merge, a compare-and-swap and a
   * server-side guard. It needs none of them now: the edit is published into THIS device's own
   * document, at a version above whatever the folder currently shows, and the merge does the rest.
   *
   * A device that could not be read makes this refuse to DELETE. The count a deletion is confirmed
   * against comes from the merged folder, so a missing view makes that number too small — and a
   * confirmation for the wrong number of files is not a confirmation.
   */
  async function _mutate(key, build, verify){
    const got = await docs.views(key);
    const m = S_ENGINE.merge(got.views || {});
    const meId = deviceId();
    const mine = Object.assign({}, (got.views || {})[meId] || {});
    const touched = [], now = Date.now();
    let removed = 0;
    // A build that THROWS saves nothing: every check a caller wants to make against the current
    // folder belongs in here, where it is reading what is about to be published against.
    build({
      paths: m.global, now,
      put(p, entry){
        mine[p] = Object.assign({}, entry, { v: S_ENGINE.versionOf(m.global[p]) + 1, by: meId });
        touched.push(p);
      },
      drop(p){
        const cur = m.global[p];
        if(!cur || cur.deletedAt) return;          // already gone: not a deletion
        if(got.missing) throw new Error('one of your devices could not be read, so this cannot be '
                                        + 'counted safely — nothing was deleted. Try again in a moment.');
        mine[p] = { v: S_ENGINE.versionOf(cur) + 1, by: meId, deletedAt: now };
        touched.push(p); removed++;
      },
    });
    if(!touched.length) return { touched: [], removed: 0 };
    if(typeof verify === 'function') verify(m.global);
    await docs.publish(key, mine);
    return { touched, removed };
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
      const got = await docs.views(key);
      const m = S_ENGINE.merge(got.views || {});
      const all = Object.keys(m.global);
      const live = all.filter(p => m.global[p] && !m.global[p].deletedAt).length;
      const devices = Object.keys(got.views || {}).length;
      if(!all.length && !devices) return { removed: 0, live: 0, tombstones: 0, devices: 0 };
      await store._post({ folder: key, forgetAll: true });
      let after = null;
      try{ after = await docs.views(key); }catch(_){ after = null; }
      const left = after ? Object.keys(S_ENGINE.merge(after.views || {}).global).length : null;
      if(left) throw new Error('the record still holds ' + left + ' entr' + (left === 1 ? 'y' : 'ies')
                               + ' — nothing was cleared');
      return { removed: all.length, live, tombstones: all.length - live, devices,
               verified: left === 0 };
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
      const got = await docs.views(key);
      return _liveUnder(S_ENGINE.merge(got.views || {}).global, path).length;
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
      try{ known = S_ENGINE.merge((await docs.views(key)).views || {}).global; }catch(_){}
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
            setStatus(f.id, ev.phase + of + where, null, true);
          },
        }));
        if(!o.dryRun){
          if(rep && rep.badFetch) _rememberBadFetch(keyOf(f), rep.badFetch);
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
      }
    })();
    running.set(f.id, job);
    return job;
  }

  /* What a person is actually being asked. One sentence per kind of refusal, in the terms of what
   * happens to their files — never in the engine's terms. Nothing erases anything either way: a
   * delete here is a move into `.pc-trash`. */
  function _ask(key, v){
    if(v.kind === 'massTrash')
      return '“' + key + '” — move ' + v.n + ' file' + (v.n === 1 ? '' : 's')
           + ' on this device to the trash?\n\nThey are marked deleted on your other devices, and '
           + 'this sweep keeps only ' + v.keep + '. If you did not delete them somewhere else, '
           + 'cancel — nothing is removed and your files stay where they are.\n\nNothing is erased '
           + 'either way: a delete here is a move into .pc-trash.';
    if(v.kind === 'massTombstone')
      return '“' + key + '” — tell your other devices to delete ' + v.n + ' file'
           + (v.n === 1 ? '' : 's') + '?\n\nThey are gone from this device and this sweep keeps '
           + 'only ' + v.keep + '. If this device lost sight of the folder rather than you deleting '
           + 'them, cancel — nothing changes anywhere.';
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
      if(!n) return 'already in step';
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
    // Said out loud, because "900 up" for files that were never sent is how a working first sweep
    // gets mistaken for the resync bug it is recovering from.
    if(rep.alreadyStored) bits.push(rep.alreadyStored + ' already stored');
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
      bits.unshift('kept ' + rep.refusedTrash.n + ' file'
        + (rep.refusedTrash.n === 1 ? '' : 's') + ' the others say are deleted — nothing trashed');
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
    setStatus(f.id, 'checking your files…', null, true);
    let v;
    try{
      v = await EXEC.verify(fs, docs, { id: f.id, key: keyOf(f), deep: true,
        excludes: f.excludes || [],
        onProgress: (ev) => setStatus(f.id, ev.phase + (ev.n ? ' ' + ev.i + '/' + ev.n : ''), null, true) });
    }catch(e){
      setStatus(f.id, 'could not check: ' + ((e && e.message) || e), null, true);
      return;
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
    if(v.disagree.length) bits.push(v.disagree.length + ' your devices disagree about');
    if(v.missingViews) bits.push(v.missingViews + ' device(s) unreadable');
    setStatus(f.id, bits.length ? bits.join(' · ') : 'everything checks out', null, true);

    /* A DEVICE NOBODY CAN READ IS THE ONE FAULT THAT HIDES ITSELF: it holds back every deletion in
     * the pair, silently and for ever, and the sweep is right to let it. So this is where it gets
     * said out loud and where somebody can decide that device is gone for good. Never inferred —
     * a device that is merely offline reads exactly the same way from here. */
    for(const dev of (v.cannot || [])){
      const ok = await PC.uiConfirm('“' + keyOf(f) + '” — the record from “' + dev + '” cannot be '
        + 'read.\n\nWhile that is true, nothing in this folder will be deleted on any device, '
        + 'because a record that cannot be read is not the same as a device that holds nothing.\n\n'
        + 'If that device is gone for good, retire its record. If it is only offline or its key is '
        + 'not on this device, cancel and it will sort itself out.');
      if(!ok) continue;
      try{ await docs.retire(keyOf(f), dev); PC.toast('retired ' + dev); }
      catch(e){ PC.toast('could not retire that record: ' + ((e && e.message) || e)); }
    }

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
    try{ merged = S_ENGINE.merge((await docs.views(keyOf(f))).views || {}).global; }
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
      + unf
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
      // Named, not tallied. "republished 3,930 files another device deleted" is not something anyone
      // can act on without knowing which — and deciding whether to delete them again is exactly the
      // decision this leaves the user holding.
      + grp('Republished — deleted elsewhere, but changed here since',
            rep.resurrected || [], a => a.path)
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
    const key = keyOf(f);
    const man = S_ENGINE.merge((await docs.views(key)).views || {}).global;
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
          <button class="btn btn-ghost small sync-dry" title="What this sweep would change, without changing anything.">Preview</button>
          ${pr.paused ? '<button class="btn btn-neon small sync-start">Start syncing ▶</button>'
                      : '<button class="btn btn-neon small sync-now">Sync now</button>'}
          ${pr.paused ? '' : '<button class="btn btn-ghost small sync-pause" title="Stop this folder syncing until you press Start. Nothing is deleted and nothing is undone.">Pause</button>'}
          <button class="btn btn-ghost small sync-deep" title="Re-read and re-hash every file. Slow on a big folder — for a file edited in place without changing its size or timestamp.">Deep check</button>
          <button class="btn btn-ghost small sync-verify" title="Read every file on this device and check it against what your devices agree the folder holds. Changes nothing.">Verify</button>
          <button class="btn btn-ghost small sync-tidy">Tidy up conflict copies</button>
          <button class="btn btn-ghost small sync-trash">Empty trash</button>
          ${(FS() && FS().tickStats) ? '<button class="btn btn-ghost small sync-bg" title="What this phone measured about background syncing: alarms scheduled, alarms that fired, and ticks that reached the app.">Background details</button>' : ''}
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
      { const vb = card.querySelector('.sync-verify');
        if(vb) vb.onclick = () => verifyFolder(get()); }
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
      /* THE ONLY PLACE ANY OF THIS CAN BE OBSERVED. The counters exist because this failure reports
       * SUCCESS — an alarm that never fires, a tick emitted into a dead page and a sweep that ran
       * are all silence from every other vantage point — and counters nothing renders are no better
       * than no counters, which is what shipped in the first version of them. */
      { const bg = card.querySelector('.sync-bg');
        if(bg) bg.onclick = async () => {
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
  window.PCSync = { paint, folders, sweep, startAll, store, docs, status, edit, verifyFolder,
                    accountFolders, acct: () => _acct, deviceId };
})();
