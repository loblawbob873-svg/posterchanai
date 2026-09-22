/* FilesIdx — the encrypted drive index: the folder tree and each file's metadata, its master key,
 * the local cache, the batched save and the pull that hydrates it. Split out of app.js.
 *
 * It ships with the page rather than loading on demand: nearly every screen reads it synchronously
 * (`FilesIdx.meta(sha)`, `FilesIdx.folderOf`, `FilesIdx._ensureMK()`), and Files, Music, Mail and
 * the AI attachments all take it as a dependency. app.js keeps the NAME, bound to a Proxy onto the
 * object in here (see `_lzProxy`), and builds the factory the first time anything touches it.
 *
 * The code below is BYTE-IDENTICAL to what it replaced apart from its reads of app.js's live `let`
 * bindings, which the parser rewrote to `S.<name>` (getters on `dep.state`) at exact identifier
 * offsets.
 */
window.PCFilesIndexFactory = function(dep){
  const S = dep.state;   // live app.js bindings: S.ME, S.signer
  const {
    MusicOffline, _masterDecrypt, _masterEncrypt, _shaFromUrl, _u8b64, _unwrapMK, mediaServer,
    selfProof, toast, uiConfirm, uploadBlob,
  } = dep;
  // Folder index — the folder tree + each file's {name,folder,...}. One encrypted doc under the storage
  // key (cross-device, survives PWA reinstalls), cached in localStorage for instant render. Blossom is
  // flat/content-addressed, so foldering is this client-side overlay keyed by blob sha256.
  const FilesIdx = {
    // _pullDone = a pull attempt finished. _pullOk = the pull actually MATERIALISED the index (or
    // proved the server has none). _pullBlocked = the server HAS an index we could not read — the one
    // state in which writing anything would destroy it. The three are not the same thing, and
    // conflating _pullDone with "we have the index" is what wiped a drive's folders: see _save/_gc.
    data: { folders: ['Music'], files: {}, encFolders: [] }, _pulling:false, _pullP:null, _ensuring:null, _pullDone:false, _pullOk:false, _pullBlocked:false, _t:null, mk:null, _mkWrapped:null, _batch:false, _lastIndexSha:null, _indexShas:new Set(), _dirty:false, _saving:false,
    // Derived drive summaries use this revision instead of walking a multi-thousand-file index on
    // every repaint. It changes whenever local or pulled state is materialised.
    _rev:0,
    // A collapsing write the user has already confirmed, and the retry that follows a failure.
    _forceOk:false, _saveFailed:false, _retryT:null, _retryN:0, _saveAgain:false, _savingP:null,
    /* When this device last AGREED with the server, in unix seconds. It is what tells a
     * deletion made elsewhere apart from a file added here — see _mergeFiles. Persisted, or a
     * reload would forget every deletion it had already accepted. */
    _syncedAt:0,
    _key(){ return 'pc_files_idx_'+((S.ME&&S.ME.pubkey)||'anon'); },
    _norm(){ if(!this.data||typeof this.data!=='object') this.data={folders:['Music'],files:{},encFolders:[]};
      if(!Array.isArray(this.data.folders)) this.data.folders=['Music'];
      if(!this.data.files||typeof this.data.files!=='object') this.data.files={};
      if(!Array.isArray(this.data.encFolders)) this.data.encFolders=[];   // names of encrypted folders
      if(!this.data.folders.includes('Music')) this.data.folders.unshift('Music'); return this.data; },
    loadLocal(){ try{ const d=JSON.parse(localStorage.getItem(this._key())||'null'); if(d) this.data=d; }catch(_){}
      try{ this._syncedAt = +(localStorage.getItem(this._key()+'_sync')||0) || 0; }catch(_){}
      try{ this._mkWrapped = localStorage.getItem(this._key()+'_mk') || this._mkWrapped; }catch(_){}
      this._rev++; return this._norm(); },
    saveLocal(){ this._norm(); this._rev++; try{ localStorage.setItem(this._key(), JSON.stringify(this.data)); if(this._mkWrapped) localStorage.setItem(this._key()+'_mk', this._mkWrapped); if(this._syncedAt) localStorage.setItem(this._key()+'_sync', String(this._syncedAt)); }catch(_){} },
    // We and the server now hold the same thing. Everything before this moment that the server does
    // not have was deleted by somebody, not added by us (see _mergeFiles).
    _synced(){ this._syncedAt = Math.floor(Date.now()/1000); this.saveLocal(); },
    // The master key (AES-256) is generated once, NIP-44 self-wrapped, and kept in the index pointer.
    async _ensureMK(){
      if(this.mk) return this.mk;
      // A wrapped key that won't unwrap (signer not ready / wrong account / NIP-44 denied) must FAIL,
      // never silently mint a replacement: the new key can't read the existing encrypted index or Music
      // blobs, and re-wrapping it over the old one in localStorage destroys the only way back to them.
      /* A BAD LOCAL KEY IS NOT A DEAD END — the server holds the real one.
       *
       * This used to unwrap the local copy and throw if it would not, so a device whose stored key
       * was corrupt, truncated, or wrapped to a different account could never read its own files
       * again: it never reached the pull below, where the correct key has been sitting the whole
       * time. The tablet that reported "the folder list is stored but unreadable", then "failed to
       * execute importKey", was exactly this — locked out by a local value while the real key was
       * one request away.
       *
       * So a local key that will not unwrap is DISCARDED and the server is asked. Discarding is safe
       * precisely because it is unusable: it decrypts nothing, so nothing is lost with it, and the
       * copy that matters lives in the drive index. Minting still never happens here. */
      if(this._mkWrapped){
        try{ this.mk=await _unwrapMK(this._mkWrapped); return this.mk; }
        catch(e){
          // Only a key that CANNOT become a key is discarded. A signer that did not answer is asked
          // again next time; throwing that copy away would be the one irreversible move here.
          if(!e || !e.badKey) throw e;
          console.warn('files: the drive key stored on this device is unusable, asking the server —', e.message);
          this._mkWrapped=null;
          try{ this.saveLocal(); }catch(_){ }
        }
      }
      /* ABSENT IS NOT "NONE". The guard above covers a key that won't unwrap and misses the case
       * that actually happened: no local key at all — a fresh device, cleared storage, a private
       * window, or a saveLocal() that failed under quota pressure. Minting there produced a key that
       * decrypts nothing, saved it over the empty slot, and left the device permanently unable to
       * read its own files while the real key sat on the server. So ASK THE SERVER FIRST, and mint
       * only if it answered and genuinely had none. A failed pull is not an answer. */
      // NOT from inside pull() — pull() calls this to unwrap the key it just fetched, and
      // `_pullDone` is only set at its END, so a pointer with no mk would recurse until the stack
      // gave out. `_pulling` is the re-entrancy guard.
      if(!this._pullDone && !this._pulling){
        try{ await this.pull(); }catch(_){ }
        if(this.mk) return this.mk;
        if(this._mkWrapped){ this.mk=await _unwrapMK(this._mkWrapped); return this.mk; }
        if(!this._pullDone) throw new Error('couldn’t reach your drive to load its key — nothing was changed');
      }
      this.mk=crypto.getRandomValues(new Uint8Array(32));
      this._mkWrapped=await S.signer.nip44enc(S.ME.pubkey, JSON.stringify({k:_u8b64(this.mk)})); this.saveLocal();
      /* CLAIM THE KEY AT THE SERVER IN THE SAME BREATH IT IS MINTED. A folder-sync-only device
       * never touches the drive index, so a locally minted key used to stay local — the server's
       * first-writer-wins guard protected an empty slot while two fresh devices each sealed
       * thousands of blobs under their own mint (measured by check_sync_full: A uploaded 12, B
       * downloaded 12, B could open none). The save answers with the canonical key when this mint
       * lost a race, _saveOnce adopts it on the spot, and the pull right after covers the
       * both-saves-raced ordering. Failure here is tolerable: the next save retries, and every
       * seal until then carries the named wrong-key fixer rather than silence. */
      try{ this._dirty = true; await this._saveOnce(); }catch(_){}
      try{ this._pullDone = false; this._pullOk = false; await this.pull(); }catch(_){}
      if(this._mkWrapped && !this.mk) this.mk = await _unwrapMK(this._mkWrapped);
      return this.mk;
    },
    /* TOMBSTONES — a deletion the merge cannot otherwise express.
     *
     * `Object.assign({}, srv.files, loc.files)` folds the server's copy under the local one, and a
     * file the user just DELETED is not in either side's "local wins" — it is simply absent locally
     * and present on the server, so the merge puts it straight back. That is not a corner case: it
     * is what happens on the ordinary path, because _save() pulls first whenever it has not yet
     * confirmed what the server holds, and a pull with edits pending merges.
     *
     * Measured: "Remove 2422 missing tracks" removed them, the save pulled, the merge re-added all
     * 2422, and the write went out with the SAME 3990 entries — no collapse, so the server's guard
     * saw nothing wrong, the save honestly reported success, and the tracks were still there. Twice
     * over two days, and once more after the toast was made truthful.
     *
     * So a forget() is recorded, and an incoming index cannot resurrect what it names. Kept after a
     * successful save on purpose: another device holding a stale copy would otherwise merge them
     * back the next time it wrote. Bounded by age and count, since this is only ever a shield
     * against copies still in flight. Deliberately NOT published in the doc — it is ~64 bytes per
     * entry against a 133KB index, and the server-side copy is already correct once a save lands. */
    _DEL_MAX: 20000, _DEL_TTL: 90*86400,
    _tomb(sha){
      this._norm();
      if(!this.data.deleted || typeof this.data.deleted!=='object') this.data.deleted={};
      this.data.deleted[sha]=Math.floor(Date.now()/1000);
      const keys=Object.keys(this.data.deleted);
      if(keys.length>this._DEL_MAX || keys.length%512===0){
        const cut=Math.floor(Date.now()/1000)-this._DEL_TTL;
        const live=keys.filter(k=>(this.data.deleted[k]||0)>cut)
                       .sort((a,b)=>this.data.deleted[b]-this.data.deleted[a]).slice(0,this._DEL_MAX);
        const kept={}; for(const k of live) kept[k]=this.data.deleted[k];
        this.data.deleted=kept;
      }
    },
    // Drop everything this device has deleted from a files map that came off the server.
    _dropDeleted(files){
      const del=this.data && this.data.deleted;
      if(del) for(const sha in del) delete files[sha];
      return files;
    },
    /* Fold a server index UNDER the local one: folders/encFolders union, per-file local wins (a local
       entry is the newer edit). Used only when a pull lands while edits are pending — the alternative
       there is dropping one side, and dropping the server's side loses the whole drive's foldering.
       The known cost is that a folder deleted locally in that window can come back; resurrecting a
       folder is recoverable, replacing 400 files' metadata with nothing is not. (A deleted FILE is
       covered — see the tombstones above.) */
    /* A DELETION MADE ON ANOTHER DEVICE, without publishing a tombstone list.
     *
     * The local tombstones above only speak for THIS device. The phone that never saw the deletion
     * merges its own copy back, `Object.assign` restores every entry, and the file list the user
     * deleted on their laptop comes back — that is exactly what happened: 2422 entries removed on a
     * desktop, resurrected by a phone still holding the pre-deletion library, and then the desktop's
     * next save refused as "collapsing" against them.
     *
     * The signal is TIME. Every entry carries the `ts` it was added at, and we know when this device
     * last successfully synced with the server. So for a file we hold that the server does NOT:
     *
     *   ts <= syncedAt  →  the server knew about it when we last agreed, and no longer does.
     *                      Somebody deleted it. Let it go.
     *   ts >  syncedAt  →  we added it here since; the server has simply not been told yet. Keep it.
     *
     * Which needs no extra bytes in the document — publishing 2422 sha256s would be ~155KB on a
     * 133KB index — and it degrades safely: with no recorded sync (`_syncedAt` 0) nothing is
     * dropped and this is the old union, which is the behaviour that never loses data. */
    _mergeFiles(srv, loc){
      const out=Object.assign({}, srv.files||{}, loc.files||{});
      const since=this._syncedAt||0;
      if(since){
        const s=srv.files||{};
        for(const sha in out){
          if(s[sha]) continue;                            // the server still has it
          if(!(loc.files||{})[sha]) continue;             // not ours to judge
          const ts=+((loc.files[sha]||{}).ts)||0;
          if(ts && ts <= since) delete out[sha];          // known before our last sync, gone now
        }
      }
      return this._dropDeleted(out);
    },
    _merge(srv){
      this._norm();
      const loc=this.data;
      this.data={ folders:[...new Set([...(srv.folders||[]), ...loc.folders])],
                  encFolders:[...new Set([...(srv.encFolders||[]), ...loc.encFolders])],
                  deleted: loc.deleted,
                  files:this._mergeFiles(srv, loc) };
      this.saveLocal();
    },
    /* ONE WAY TO SAY "I NEED THE INDEX", because four call sites each had their own and every one of
     * them latched on the ATTEMPT rather than on the result:
     *
     *     if(!FilesIdx._pulled){ FilesIdx._pulled = true; try{ await FilesIdx.pull(); }catch(_){} }
     *
     * `_pulled` is set BEFORE the pull and the failure is swallowed, so ONE pull that did not
     * materialise the index — and `pull()` begins by asking the SIGNER for a kind-27235, which with
     * a remote signer means a phone that may be slow, busy or asleep — convinces every picker on the
     * page, for the life of the page, that the drive has no folders. Reported in one breath as "the
     * folder list is gone on the reply post blossom file picker", "new post is missing the blossom
     * folder picker too" and "folder choose is broken all across blossom": one latch, every surface.
     *
     * The honest flag already exists. `_pullOk` means the index was actually materialised (or the
     * server proved it has none) — it is what `_save` gates on, for the same reason: acting on an
     * index we never read is how a drive's folders get wiped. So the latch is `_pullOk`, a failed
     * attempt leaves nothing behind, and the next picker tries again. Concurrent callers share the
     * one in-flight pull rather than starting four. */
    ensure(){
      if(this._pullOk) return Promise.resolve(true);
      if(!this._ensuring){
        this._ensuring = (async()=>{
          try{ await this.pull(); }catch(_){}
          this._ensuring = null;
          return !!this._pullOk;
        })();
      }
      return this._ensuring;
    },
    async history(){
      const auth=await selfProof();
      const r=await fetch('/client/files-index',{method:'POST',headers:{'Content-Type':'application/json'},
        body:JSON.stringify({pubkey:S.ME.pubkey,auth,history:true})});
      const j=await r.json().catch(()=>({}));
      if(!r.ok || !j.ok) throw new Error(j.error||('history HTTP '+r.status));
      return Array.isArray(j.backups)?j.backups:[];
    },
    async restore(slot){
      const auth=await selfProof();
      const r=await fetch('/client/files-index',{method:'POST',headers:{'Content-Type':'application/json'},
        body:JSON.stringify({pubkey:S.ME.pubkey,auth,restore:Number(slot)})});
      const j=await r.json().catch(()=>({}));
      if(!r.ok || !j.ok) throw new Error(j.error||('restore HTTP '+r.status));
      // Throw away every in-memory proof about the version we just replaced. The normal pull then
      // decrypts and materialises the selected version through the exact same path as a fresh login.
      this._pullDone=false; this._pullOk=false; this._pullBlocked=false; this._dirty=false;
      await this.pull();
      if(!this._pullOk) throw new Error('the backup was restored but could not be opened on this device');
      return j;
    },
    async pull(){
      if(this._pullP) return this._pullP;
      this._pulling = true;
      /* `_pulling` is cleared in a FINALLY, not at the end of the try.
       *
       * Everything below is wrapped in a catch that swallows, and the very first statement asks the
       * signer to sign — which rejects when a remote signer does not answer (after its full ceiling).
       * On that path `_pulling` was left TRUE for ever, and it is the flag `_ensureMK` checks before
       * deciding to pull the drive's key: one slow signer, and the drive could not be read again
       * until the page was reloaded. Same shape as the latch above, one line further in. */
      this._pullP = this._pull();
      try{ return await this._pullP; }
      finally{ this._pulling = false; this._pullP = null; }
    },
    async _pull(){
      try{ const auth=await selfProof();
        const r=await fetch('/client/files-index',{method:'POST',headers:{'Content-Type':'application/json'},
          body:JSON.stringify({pubkey:S.ME.pubkey,auth})}).then(r=>r.json());
        const ptr=r&&r.ok&&r.index;
        if(ptr&&typeof ptr==='object'){
          /* THE SERVER'S WRAPPED KEY WINS, and a stale local one is thrown away — including the
           * already-unwrapped copy in memory, which `_ensureMK` returns before it looks at anything
           * else. Without that, a device that once minted a key of its own is stuck with it for the
           * whole session AND across restarts (saveLocal persisted it), so every encrypted file it
           * owns fails to decrypt with OperationError — "couldn't load this image, operation
           * failed" — while the correct key sits on the server untouched. This is the line that
           * lets such a device heal itself on the next load. */
          if(ptr.mk && ptr.mk !== this._mkWrapped){
            this._mkWrapped = ptr.mk;
            this.mk = null;                 // re-unwrap from the authority, not from the local guess
            this.saveLocal();
          }
          let idx=null;
          if(ptr.indexSha){                         // v2: index lives in an encrypted Blossom blob (scales to 1000s)
            this._lastIndexSha=ptr.indexSha;        // remembered so the grid can hide the index blob itself
            this._indexShas.add(ptr.indexSha);
            // Own try: a signer that can't unwrap the master key, an offline media host or a 404'd blob
            // must fall through to the _pullBlocked check below, NOT escape to the outer catch — which
            // would leave us looking like a fresh empty drive that is safe to overwrite.
            try{
              await this._ensureMK();               // (without this, every session leaked its old index blob)
              const br=await fetch(mediaServer()+'/'+ptr.indexSha);
              if(br.ok){ const d=JSON.parse(new TextDecoder().decode(await _masterDecrypt(this.mk, new Uint8Array(await br.arrayBuffer()))));
                if(d&&d.files) idx=d; }
            }catch(e){ console.warn('files-index: could not read index blob', e); }
          } else if(ptr.files){ idx=ptr; }          // v1: small index stored inline in the pointer
          // Don't clobber edits made WHILE this (possibly slow blob-fetch) pull was in flight — local is
          // newer and syncs on the next save. Without this, creating a folder + uploading during the
          // initial load got wiped: the files lost their metadata and vanished / showed as octet-stream.
          if(idx){
            if(!this._dirty && !this._saving){
              // SELF-REPAIR. If this device holds far more than the server does, the server's copy was
              // truncated (a browser with no local state saved its empty default over it — the wipe
              // this whole guard-set exists to stop). Blindly applying the server copy here is what
              // would finish the job: the last good index would be overwritten by the bad one and the
              // drive's folders would be gone for good. Keep ours, fold theirs under it, push it back.
              /* …but count only what a truncation could explain.
               *
               * "I hold more than the server" has two causes and this rule assumed the wrong one.
               * The other is that somebody DELETED those files on another device — and then this
               * branch fires, calls it a truncation, and pushes them all back. Which is precisely
               * what happened: a phone holding a pre-deletion library restored 2422 entries a
               * desktop had removed, and announced it as "Restored 3990 files from this device".
               *
               * A file the server no longer has, that we already knew about at our last sync, is a
               * deletion — not evidence of truncation. Only the rest counts here, and _mergeFiles
               * applies the same rule if we do decide to repair. */
              const locFiles=this._norm().files||{}, srvFiles=idx.files||{}, since=this._syncedAt||0;
              const locN=Object.keys(locFiles).filter(sha=>{
                if(srvFiles[sha] || !since) return true;
                const ts=+((locFiles[sha]||{}).ts)||0;
                return !(ts && ts <= since);          // deleted elsewhere → not ours to restore
              }).length;
              const srvN=Object.keys(srvFiles).length;
              if(locN >= 5 && locN > srvN + 4){
                this._merge(idx);
                this._dirty=true;
                try{ toast(`📁 Restored ${Object.keys(this.data.files).length} files and ${this.data.folders.length} folders from this device.`); }catch(_){}
                console.warn('files-index: server copy was truncated ('+srvN+' vs '+locN+' here) — restoring from this device');
                setTimeout(()=>this._save(), 50);   // _pullOk is set a few lines below, in this same tick
              } else {
                // Adopting the server's copy wholesale must not undo a deletion either — and it must
                // carry the tombstones forward, since `idx` has none of its own.
                const del=(this.data&&this.data.deleted)||null;
                this.data=idx; if(del) this.data.deleted=del;
                this._norm(); this._dropDeleted(this.data.files);
                this.saveLocal();
              }
            }
            // Edits in flight: the old code SKIPPED the server index entirely, so a first upload on a
            // fresh device saved {Music, that one file} straight over a full drive. Fold the server's
            // index UNDER the local edits instead — local wins per file, nothing is dropped.
            else this._merge(idx);
          }
          // Did we actually GET the index? A pointer that names a blob we couldn't fetch or decrypt is
          // the dangerous case: the server HAS folders, we're holding an empty default, and the next
          // save would replace theirs with ours. Flag it and refuse to write until a pull succeeds.
          /* `_pullBlocked` is CLEARED on every successful read, not only on a reset. It is latched
           * state describing one attempt, and a flag that can only ever go true would keep naming
           * an unreadable index long after the read recovered — so the message below would blame
           * the wrong thing for the rest of the session. */
          if(idx){ this._pullOk=true; this._pullBlocked=false; if(!this._dirty && !this._saving) this._synced(); }
          else if(ptr.indexSha || (ptr.files && Object.keys(ptr.files).length)) this._pullBlocked=true;
          else { this._pullOk=true; this._pullBlocked=false; }   // a pointer with nothing in it: server really is empty
        } else if(r && r.ok){
          this._pullOk=true; this._pullBlocked=false;  // server has no index at all — a fresh drive, safe to save
        }
      }catch(_){
        /* A THROW HERE USED TO MEAN "STILL LOADING", FOR EVER.
         *
         * `_pullDone` was the last statement INSIDE the try, so any failure — an unreachable
         * server, a blob that would not fetch, a decrypt that threw — was swallowed here and the
         * flag stayed false with nothing to set it. The upload guard reads it and says "One sec —
         * still loading your folders. Try that again in a moment", which is untrue in both halves:
         * nothing is loading, and trying again never helps. Reported as "can't even upload to
         * files now, still loading folders despite loaded".
         *
         * The comment on the flags above already states the rule this broke: `_pullDone` means a
         * pull ATTEMPT FINISHED. Whether it succeeded is `_pullOk`, and whether writing is unsafe
         * is `_pullBlocked` — which a throw leaves false, so this cannot make a dangerous write
         * look safe. "Could not ask" is not "not finished". */
      }finally{
        this._pullDone=true;
      }
      return this._norm();
    },
    push(){ this._dirty=true; this.saveLocal(); if(this._batch) return; clearTimeout(this._t); this._t=setTimeout(()=>this._save(), 900); },
    /* ONE save at a time, always.
     *
     * Nothing enforced this: _saving was set but never checked on the way IN, so every scheduled
     * save started its own request. That is survivable until a save can ASK A QUESTION — and it can,
     * on a collapsing write. Uploading a folder while the server disagreed produced a refused save
     * every two seconds, each one a fresh "This removes most of your file list" dialog, hundreds of
     * them, on a screen the user was just adding songs to.
     *
     * Collapsed into one: a save that arrives while another is running does not queue a second
     * request, it asks the one in flight to go round again when it lands (`_saveAgain`). So the
     * question is asked at most once per answer, and the pending edits ride along with whatever the
     * in-flight save is already sending. */
    _save(){
      if(this._saving){ this._saveAgain=true; return this._savingP || Promise.resolve(false); }
      this._savingP = this._saveOnce().then(async ok=>{
        if(this._saveAgain && this._dirty){ this._saveAgain=false; return await this._save(); }
        this._saveAgain=false;
        return ok;
      });
      return this._savingP;
    },
    async _saveOnce(){
      // NEVER overwrite a server index we failed to READ. The doc is replaceable, so one save from a
      // browser holding the empty default (fresh device, cleared storage, a blob fetch that 404'd)
      // replaces every folder and filename with nothing — which is exactly how a drive lost its
      // folders while all 417 blobs sat untouched in Blossom. Retry the pull once; if it still can't
      // be read, keep the edit local (_dirty stays set) and say so rather than writing over it.
      // The gate is "we have CONFIRMED what's on the server" (_pullOk), not "the pull didn't set an
      // error flag" — an errored/unauthorised pull sets neither, and defaulting that to "safe to
      // write" is the same mistake one level along.
      if(!this._pullOk){
        await this.pull();
        if(!this._pullOk){
          /* TWO DIFFERENT PROBLEMS, AND THEY NEED TWO DIFFERENT SENTENCES.
           *
           * `_pullBlocked` means the server HAS an index and this device could not READ it — a
           * missing drive key, or an index blob that would not fetch or decrypt. Reloading does not
           * help with that; it is about this device's key, and the fix is to unlock the drive or
           * open it where the key is. Everything else here is "we could not ask the server at all",
           * where reloading is exactly right. Until now both printed the same "try reloading",
           * which sent people to re-do the one thing that could not work. The flag that tells them
           * apart was set on every pull and read by nothing. */
          const blocked = this._pullBlocked;
          console.warn('files-index: ' + (blocked
            ? 'server HAS an index this device could not read — not saving (would overwrite it)'
            : 'could not read the server index — not saving (would overwrite it)'));
          try{ toast(blocked
            ? '⚠️ Your folders are on the server but this device couldn\'t unlock them — nothing was saved, so they aren\'t overwritten. Open your drive (or check your drive key) and try again.'
            : '⚠️ Couldn\'t read your folders from the server — not saving, so your existing folders aren\'t overwritten. Try reloading.'); }catch(_){}
          return false;
        }
      }
      this._saving=true;   // while a save's index-blob upload + POST is in flight the server is NOT yet
                           // up to date — pull() must not apply stale server data during this window (it
                           // would wipe the very file being saved). Cleared in finally.
      try{ this._norm();
        this._dirty=false;   // capture point: edits AFTER this re-mark dirty (and reschedule) so pull won't clobber them
        const idx={folders:this.data.folders, files:this.data.files, encFolders:this.data.encFolders}; const json=JSON.stringify(idx);
        // `n` is the entry count in PLAINTEXT. The index body may be an encrypted blob the server
        // cannot read, so without this it has no way to notice a save collapsing 400 files to 1 —
        // which is the write it must refuse. Cheap, leaks only a magnitude.
        const ptr={n:Object.keys(idx.files||{}).length}; if(this._mkWrapped) ptr.mk=this._mkWrapped;
        if(json.length < 45000){ ptr.folders=idx.folders; ptr.files=idx.files; ptr.encFolders=idx.encFolders; }   // small → inline (NIP-44 doc)
        else {                                                                       // large → encrypted Blossom blob
          const mk=await this._ensureMK(); ptr.mk=this._mkWrapped;
          // MIRRORED (no noMirror): every ordinary photo is DR-copied to a second host, and the one
          // file holding every filename and folder was the sole exception — backwards. It is
          // ciphertext, so the mirror learns nothing, and it is the blob worth having off-site.
          const url=await uploadBlob(new File([await _masterEncrypt(mk, new TextEncoder().encode(json))],'files-index.enc',{type:'application/octet-stream'}), {keep:true, noCompress:true});
          ptr.indexSha=_shaFromUrl(url);
        }
        const auth=await selfProof();
        /* Already confirmed once? Then send FORCE with the first request.
         *
         * The 409 path costs TWO signatures for one action — and with a remote signer each is a
         * prompt on a phone, so the second one is a second chance to lose the write. That is how a
         * confirmed mass-delete kept failing: the user said yes, the second signature went out, and
         * whatever happened to it took the whole save with it. Remembered until the write lands, so
         * a retry is one signature and no second prompt. */
        const sr=await fetch('/client/files-index',{method:'POST',headers:{'Content-Type':'application/json'},
          body:JSON.stringify(Object.assign({pubkey:S.ME.pubkey,auth,index:ptr},
                                            this._forceOk?{force:true}:{}))});
        // 409 = the server refused a write that would collapse the index. That is right when it's a
        // bug and wrong when the user genuinely just deleted a big folder — and since nothing else
        // ever sets `force`, without this branch the guard makes a legitimate mass-delete impossible.
        // So ASK, and honour the answer. Never report success for a write the server threw away.
        /* The server KEEPS the account's first drive key (first-writer-wins) — if this save
         * carried a losing key, the answer names the canonical one. Adopt it immediately: every
         * further second on the losing key is another upload nobody else can open. What this
         * device already sealed shows the wrong-key card line, whose fixer is "Send them again". */
        try{ if(sr && sr.ok){ const jr=await sr.clone().json();
          if(jr && jr.mk && jr.mk !== this._mkWrapped){
            console.warn('files-index: this device held a losing drive key — adopting the account\u2019s');
            this._mkWrapped = jr.mk; this.mk = null; this.saveLocal();
            try{ toast('this device\u2019s drive key was out of step and has been corrected — if some files show \u201csealed with a different key\u201d, press \u201cSend them again\u201d on that folder'); }catch(_){}
          } } }catch(_){}
        if(sr && sr.status===409){
          let why=''; try{ why=((await sr.json())||{}).error||''; }catch(_){}
          console.warn('files-index: server refused a collapsing save —', why);
          /* DON'T ASK A QUESTION WE ALREADY KNOW THE ANSWER TO.
           *
           * The server refuses a shrink it cannot explain — it holds a count and nothing else, so a
           * deliberate mass-delete and a broken client about to wipe the list look identical from
           * there. THIS side is not guessing: `deleted` is the list of entries the user removed on
           * this device. If it accounts for the shrink, the write is exactly what they asked for and
           * there is nothing to ask about.
           *
           * Without this, someone who deleted 2422 dead tracks in the morning got "This removes most
           * of your file list" every time the index saved for the rest of the day — including once
           * per checkpoint while uploading a music folder, which is the least appropriate moment
           * imaginable to ask whether they meant to delete something. And the dialog's own advice
           * ("if you did NOT expect this, cancel") is exactly wrong there: they did not expect it,
           * they were uploading, so cancelling was the sensible-looking answer and the loop never
           * ended.
           *
           * The guard still does its job. A stale bundle, a fresh device or a third-party client
           * carrying an empty index has no tombstones to show, cannot explain the shrink, and is
           * asked — which is the case that once cost a drive 417 filenames. */
          // Tolerant of the wording, not just of the numbers: the server says "N entries -> M"
          // today, and a message that reads slightly differently must not quietly turn this
          // back into a dialog nobody can answer.
          const m = /(\d+)\D+?(\d+)\s*$/.exec(String(why||'').trim());
          const shrink = m ? (+m[1] - +m[2]) : 0;
          const oldCount = m ? +m[1] : 0, newCount = m ? +m[2] : -1;
          const tombs = Object.keys((this.data && this.data.deleted) || {}).length;
          if(shrink > 0 && tombs >= shrink){
            console.warn('files-index: shrink of ' + shrink + ' is covered by ' + tombs
                         + ' deletions made here — forcing without asking');
            this._forceOk = true;
          }
          /* ZERO WITH NO TOMBSTONES IS NEVER A QUESTION.
           *
           * A disconnected phone just produced 5968 -> 0 and the guard asked the user whether to
           * continue. There is no informed "yes" to that dialog: deleting every file through our
           * UI creates one tombstone per entry and was accepted by the branch above. An unexplained
           * zero can therefore only be an unread/cleared local index, and offering Force turns a
           * connection failure into permanent metadata loss. Refuse it, invalidate the read proof,
           * and pull the protected server copy back under the empty local copy. */
          if(!this._forceOk && oldCount > 0 && newCount === 0){
            console.error('files-index: refused unexplained zero-list; restoring the server copy');
            this._dirty=false; this._forceOk=false; this._pullOk=false;
            try{ await this.pull(); }catch(_){}
            try{ toast('Your file list was not deleted. This device lost its connection, so the protected server copy is being restored.'); }catch(_){ }
            return false;
          }
          const intended = this._forceOk || await uiConfirm('This removes most of your file list ('+(why.replace(/^refused: /,'')||'large drop')+
            ').\n\nIf you just deleted a folder, that\'s expected — continue?\n\nIf you did NOT expect this, cancel: '+
            'your file list is still safe on the server.');
          if(!intended){
            this._dirty=true; this._forceOk=false;
            try{ toast('Kept your file list — nothing was changed on the server.'); }catch(_){}
            return false;
          }
          this._forceOk=true;   // said yes — a retry must not ask again, or sign twice again
          const auth2=await selfProof();
          const fr=await fetch('/client/files-index',{method:'POST',headers:{'Content-Type':'application/json'},
            body:JSON.stringify({pubkey:S.ME.pubkey,auth:auth2,index:ptr,force:true})});
          if(!fr || !fr.ok){ this._dirty=true; throw new Error('files-index forced save HTTP '+(fr?fr.status:'?')); }
        } else if(!sr || !sr.ok) throw new Error('files-index save HTTP '+(sr?sr.status:'?'));
        // The superseded index blob is deliberately KEPT. It is ~133 KB and it is the only
        // standalone backup of every filename and folder on the drive — deleting it to reclaim
        // that space is what left a wiped index with nothing to restore from.
        if(ptr.indexSha){ this._lastIndexSha=ptr.indexSha; this._indexShas.add(ptr.indexSha); }
        this._synced();                       // the server now holds exactly what we just sent
        this._saveFailed=false; this._forceOk=false; this._retryAt=0;
        clearTimeout(this._retryT); this._retryT=null;
        return true;
      }catch(e){
        // The edit is NOT saved, so it must not stay marked clean: `_dirty=false` was set at the
        // capture point above, and leaving it there tells the next pull() it's free to overwrite local
        // with the server's copy — silently discarding the rename/move/upload that just failed.
        this._dirty=true;
        console.warn('files-index save failed', e);
        /* …and SAY so. This was a console.warn and nothing else, which is how "Remove 2422 missing
         * tracks" reported success twice while the server kept all 3990 entries: the signer could not
         * sign the write (a remote signer that is not answering), the whole save threw in here, and
         * the caller's own "removed 2422 tracks" toast fired regardless because _save swallowed it.
         *
         * Once per run of failures, not per attempt: a save retries on a debounce and an offline
         * device would otherwise toast on a timer. Cleared by the next success. */
        if(!this._saveFailed){
          this._saveFailed=true;
          try{ toast('⚠️ Couldn\'t save your file list to the server — the change is only on this '
                     + 'device for now. If you sign with a remote signer, check it is reachable.'); }catch(_){}
        }
        this._retryLater();
        return false;
      }
      finally{ this._saving=false; }
    },
    /* TRY AGAIN. Nothing did.
     *
     * A failed save left `_dirty` set and stopped there, and the only thing that ever calls _save()
     * again is another edit — but the edit that failed has usually removed its own way back: once
     * "Remove 2422 missing" has emptied the local list, that button is gone, so clicking it again
     * does nothing at all and the change can never reach the server. Reported as "I already did all
     * that", with the server still holding every entry.
     *
     * Backs off (20s, 1m, 3m, 10m, then every 10m) because the usual cause is a signer nobody is
     * holding — and it is silent: _save's own toast is once-per-run, so a retry does not nag. A
     * confirmed collapsing write carries its `force` along (see _forceOk), so a retry needs one
     * signature and no second prompt. */
    _RETRY_STEPS: [20000, 60000, 180000, 600000],
    _retryLater(){
      if(this._retryT) return;                       // one timer, however many failures
      const step=this._RETRY_STEPS[Math.min(this._retryN||0, this._RETRY_STEPS.length-1)];
      this._retryN=(this._retryN||0)+1;
      this._retryT=setTimeout(()=>{
        this._retryT=null;
        if(!this._dirty){ this._retryN=0; return; }   // something else saved it in the meantime
        this._save().then(ok=>{ if(ok) this._retryN=0; });
      }, step);
    },
    /* A COUNT, NOT A FLAG — because two things batch this index at once.
     *
     * A Joplin import batches while it uploads attachments; a folder sync batches while it uploads
     * files; the Music bulk delete batches while it forgets tracks. Any two of those can overlap —
     * reported from exactly that: an import running while a sweep was going. With a boolean the
     * FIRST endBatch turns batching off for everybody, so the other operation's remaining hundreds
     * of pushes each schedule a full save of this single encrypted document, and its own endBatch
     * then runs against a flag it no longer owns.
     *
     * Counted, the batch ends when the last holder lets go, which is what every caller already
     * believes it is asking for. */
    beginBatch(){
      this._batchN = (this._batchN|0) + 1;
      this._batch = true;
      /* AND IT CANNOT BE HELD FOR EVER.
       *
       * With the old boolean, any endBatch cleared a leaked flag; counted, a beginBatch whose
       * endBatch is skipped — an early return, a throw in a caller with no `finally` — pins the
       * count above zero for the rest of the session. `push()` then returns without scheduling
       * anything, so every later edit to the drive index is local-only, with no error and no retry
       * armed: the exact silent-loss shape this file is full of warnings about.
       *
       * The deadline is generous (a bulk import of thousands of files is minutes) and it does not
       * cut a batch short — it only releases one nobody is holding any more, and saves what is
       * pending. */
      try{ clearTimeout(this._batchGuard); }catch(_){ }
      this._batchGuard = setTimeout(() => {
        if(!this._batchN) return;
        console.warn('files: a batch was never closed — releasing it and saving');
        this._batchN = 0; this._batch = false;
        if(this._dirty) this._save();
      }, 10 * 60 * 1000);
    },
    /* Answers whether the batch actually REACHED the server, so a caller can stop claiming it did.
     *
     * The batch flag is dropped AFTER the save, not before: this awaits, the save can sit on a
     * dialog for as long as the user takes to read it, and every push() during that window used to
     * see `_batch === false` and schedule a save of its own. An upload adding a file a second turned
     * that into a save a second. Held until the write lands, those pushes stay batched — which is
     * what a batch is for. */
    async endBatch(){
      try{ return await this._save(); }
      finally{
        this._batchN = Math.max(0, (this._batchN|0) - 1);
        if(!this._batchN){
          this._batch = false;
          try{ clearTimeout(this._batchGuard); }catch(_){ }
        }
      }
    },
    folders(){ return this._norm().folders; },
    /* Encryption belongs to the folder TREE, not only its exact root spelling. A directory import
     * can resolve `Private/photos/a.jpg` to `Private/photos`; checking only that exact child misses
     * the encrypted `Private` ancestor and uploads the file in plaintext. The longest/first ancestor
     * distinction does not matter here: any encrypted ancestor makes every descendant encrypted. */
    isEncFolder(name){ name=String(name||'').replace(/^\/+|\/+$/g,'');
      return name==='Music' || this._norm().encFolders.some(root=>name===root||name.startsWith(root+'/')); },
    /* `exact` is for a folder PATH produced by an import (`Photos/2024/Summer trip/Day 3`). The files
     * are tagged with that full path, so the registry must hold the very same string: the 40-character
     * cap below is for a name somebody TYPES, and applied to a path it registered a truncated folder
     * that nothing was filed under while the real one never appeared. Still bounded, never silently. */
    addFolder(name, enc, exact){ name=exact ? String(name||'').trim().replace(/\/\/+/g,'/').replace(/^\/+|\/+$/g,'')
        : (name||'').trim().slice(0,40);
      if(exact && name.length>1024) return false;
      if(!name||this._norm().folders.includes(name)) return false; this.data.folders.push(name); if(enc&&!this.data.encFolders.includes(name)) this.data.encFolders.push(name); this.push(); return true; },
    removeFolder(name){ this._norm(); if(name==='Music'||!name) return false; this.data.folders=this.data.folders.filter(f=>f!==name); this.data.encFolders=this.data.encFolders.filter(f=>f!==name); for(const sha in this.data.files){ if(this.data.files[sha].folder===name) this.data.files[sha].folder=''; } this.push(); return true; },
    meta(sha){ return this._norm().files[sha]||null; },
    folderOf(sha){ const m=this._norm().files[sha]; return (m&&m.folder)||''; },
    // Re-uploading something that was deleted must bring it back — so it stops being a tombstone.
    setFile(sha, m){ this._norm(); if(this.data.deleted) delete this.data.deleted[sha]; this.data.files[sha]=Object.assign(this.data.files[sha]||{}, m); this.push(); },
    move(sha, folder){ this._norm(); this.data.files[sha]=Object.assign(this.data.files[sha]||{}, {folder}); this.push(); },
    /* Gone from the library means gone from the DEVICE too.
     *
     * This is the one choke point for "this file is no longer mine" — the ✕ on a file (which sends a
     * Blossom DELETE to the server first), the Music tidy, everything. Without dropping the offline
     * copy here, a downloaded track deleted from the server left its bytes in IndexedDB with no
     * index entry pointing at them: invisible in every list, and therefore impossible to reclaim
     * from the UI. Storage that only grows and nothing can name is a leak, not a cache.
     *
     * Fire-and-forget because forget() is synchronous and its callers are mid-render; a miss (every
     * non-music file) is a no-op in IDB. */
    forget(sha){ this._norm(); delete this.data.files[sha]; this._tomb(sha);
      try{ MusicOffline.drop(sha); }catch(_){}
      this.push(); },
  };

  return {

    get FilesIdx(){ return FilesIdx; },
  };
};
