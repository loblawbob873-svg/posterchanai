/* Folder sync — the executor. Turns a plan from foldersync.js into actual file and network I/O.
 *
 * Everything it touches arrives as an injected adapter (`fs`, `store`, `log`), so this file has no
 * idea whether it is driving Electron's fs bridge or Android's SAF, and tests drive it with fakes.
 * The engine decides WHAT; this decides IN WHAT ORDER, and what to do when a step fails — which is
 * where a sync actually loses data.
 *
 * THE ORDER IS THE SAFETY. Four rules, each one a way to lose a file:
 *
 *  1. CONFLICT: rename the local copy FIRST, then write the incoming one. Do it the other way and a
 *     crash in between has overwritten an edit that now exists nowhere. Renaming first means the
 *     worst case is two copies and no canonical file — recoverable by hand, and the next sweep fixes
 *     it anyway.
 *  2. DOWNLOAD before DELETE. A sweep that deletes first and then fails to download has removed a
 *     file it cannot replace.
 *  3. `base` ADVANCES PER FILE, and only for the ones that actually succeeded. A sweep interrupted
 *     halfway — laptop closed, phone unplugged, network dropped — must resume, not restart, and must
 *     never record agreement about a file it did not move. Advancing the whole plan at the end is
 *     the bug that turns one failed upload into a file silently deleted on the next sweep, because
 *     `base` says it was synced and the local scan says it is gone.
 *  4. ONE FAILURE IS NOT A FAILED SWEEP. An unreadable file, a 413, a blob the server has forgotten
 *     — each is that path's problem. The sweep records it and carries on, or a single locked
 *     Outlook .pst blocks every other file in the folder forever.
 *
 * SIZE. The per-blob ceiling is the SERVER's (blossom_max_upload_mb, an admin setting), so it is
 * read at runtime and never assumed here. Something over it is reported, not silently skipped — a
 * file that never syncs and never says so is the worst outcome available.
 */
(function(root){
  'use strict';

  const S = (typeof require !== 'undefined' && typeof module !== 'undefined')
    ? require('./foldersync.js') : root.PCFolderSync;

  /* How many files may move before the agreement is stored.
   *
   * A checkpoint re-writes the WHOLE manifest — that is what the document is — so this is not a free
   * knob: at 200 files flat, a first sync of a 15790-file folder would write it 79 times, and past
   * ~376 files the manifest is a fresh encrypted blob each time. So the interval also SCALES with
   * the work in front of it, capped at _MAX_CHECKPOINTS writes per sweep. A small folder checkpoints
   * every 200 files; a huge one checkpoints twenty times, whatever its size.
   *
   * (Superseded manifest blobs are not collected yet — see docs/FOLDER_SYNC.md. That is the reason
   * this is bounded rather than generous.) */
  const _CHECKPOINT = 200;
  const _MAX_CHECKPOINTS = 20;

  /* One sweep of one folder.
   *
   * fs     : { scan, read, write, move, trash }        — the platform adapter
   * store  : { manifest, putBlob, getBlob, save }      — encrypted Blossom + the shared manifest
   * opts   : { id, device, excludes, maxBytes, now, hash, dryRun }
   */
  async function sweep(fs, store, opts){
    const o = opts || {};
    /* TWO IDENTIFIERS, and conflating them is what stopped devices seeing each other.
     *   id  — the PLATFORM's handle for this directory. Device-local: a random hex id on desktop, a
     *         SAF tree URI on Android. Only the filesystem adapter may see it.
     *   key — the PAIR key, the name the user gave this folder. The same on every device, so it is
     *         what the manifest is stored under. */
    const id = o.id, key = o.key || o.id, device = o.device || 'this device', now = o.now || 0;
    const report = { uploaded:[], downloaded:[], trashed:[], conflicted:[], removedRemote:[],
                     failed:[], skipped:[], excluded:0, unchanged:0, dryRun:!!o.dryRun };
    /* Progress, per file. A folder sync is the one operation where "it is doing something" is not
     * good enough feedback: the first sweep of a Pictures folder is minutes of silence, and silence
     * is indistinguishable from a hang, a refused login or a 404 on the manifest. The caller renders
     * whatever it likes; nothing here depends on it. */
    const tick = (typeof o.onProgress === 'function') ? o.onProgress : function(){};
    const step = (phase, path, i, n) => { try{ tick({ phase, path, i, n }); }catch(_){} };

    /* THE MANIFEST AND THE AGREEMENT ARE READ FIRST, because whether this device has ever agreed
     * about this folder decides HOW to scan it.
     *
     * A device joining a folder that already exists — the second machine, a reinstall, a reconnect —
     * has an empty `base` and a full local directory, so every path looks changed on both sides at
     * once. Settling that means comparing CONTENT, and content is a hash: size+mtime cannot do it,
     * because a file downloaded on Android gets whatever last-modified the SAF provider decides (it
     * has no writable column, see fs-android.js) and so never matches the mtime the manifest holds.
     * Without hashing, a tablet joining Documents would call every identical file divergent and
     * write a conflict copy of the entire folder.
     *
     * So a first sweep hashes whatever the battery policy said. It is the one sweep where that cost
     * is unambiguously worth paying — the alternative is re-uploading, or duplicating, everything. */
    step('reading the manifest');
    const remote = await store.manifest(key);           // {} when the folder has never synced
    const base = (await store.base(key)) || {};
    const firstEver = !Object.keys(base).length;

    step('scanning');
    /* THE SCAN MUST NOT CAP WHAT THE UPLOADER CAN CHUNK. The adapter drops anything over `maxBytes`
     * during the walk — right when a big file cannot be sent at all, and wrong the moment it can,
     * because then the engine never even sees the file it is now able to handle. Shipping chunked
     * uploads without this left every file over the ceiling skipped exactly as before. */
    const chunky = typeof fs.readPart === 'function' && !!store.putParts;
    const scanned = await fs.scan(id, { hash: !!o.hash || firstEver, excludes:o.excludes||[],
                                        maxBytes: chunky ? 0 : (o.maxBytes || 0) });
    report.skipped = scanned.skipped || [];
    /* The scan reports the FILE's hash in `sha`; a manifest entry's `sha` is the address of its
     * encrypted blob. Renaming the scan's into `csum` here is what stops the engine ever comparing
     * the two — which it used to, and called every identical file divergent. */
    const local = {};
    for(const p in (scanned.files || {})){
      const e = scanned.files[p];
      local[p] = (e && e.sha) ? { size:e.size, mtime:e.mtime, csum:e.sha } : e;
    }

    const plan = S.diff({ local, remote, base, device, now, excludes:o.excludes||[] });
    report.unchanged = plan.unchanged;
    report.excluded = plan.excluded;
    report.plan = plan;
    if(o.dryRun) return report;

    // Agreement is recorded per file, the moment that file is actually in step — see rule 3.
    const nextBase = Object.assign({}, base);
    const nextRemote = Object.assign({}, remote);
    /* WHICH PATHS THIS SWEEP ACTUALLY TOUCHED.
     *
     * The manifest is a replaceable document and `nextRemote` is a snapshot taken when this sweep
     * started, so writing it whole is last-writer-wins: two devices syncing the SAME folder at once
     * each save their own stale copy, and the later one erases every path the other added. The blobs
     * survive — they are uploaded and content-addressed — but the ENTRIES vanish, so the files are
     * missing from the folder on every other device and the device that uploaded them will not add
     * them again, because its own `base` says they are agreed.
     *
     * With the list of paths we changed, the store can merge onto whatever the manifest holds NOW
     * instead of overwriting it. */
    const touched = new Set();
    const remember = (path, entry) => { nextRemote[path] = entry; touched.add(path); };
    let dirty = false;

    /* CHECKPOINTS, because rule 3 was only half true.
     *
     * `base` advanced per file in MEMORY and was written once, at the very end. So a sweep that did
     * not reach the end — the laptop closed, the phone unplugged, the app killed mid-upload —
     * persisted nothing at all and the next one started from the first file. On a 15790-file folder
     * that is not a slow resume, it is a folder that can never finish on a machine anyone ever
     * closes. Measured: a first sync of that folder restarted from zero every time.
     *
     * Every `_CHECKPOINT` files, the agreement so far is stored. It is always safe to do: the
     * manifest only ever GAINS entries and tombstones during a sweep, never loses them, so a
     * checkpoint cannot trip the server's collapse guard; and `base` only names files that have
     * actually moved, so a resumed sweep re-does exactly the ones that had not.
     *
     * A FAILED checkpoint is not a failed sweep. The work is real either way, and the final save is
     * still the one that decides — but it is recorded, because a folder that silently cannot store
     * its agreement is the infinite resync above wearing a different hat. */
    const _work = plan.conflicts.length + plan.download.length + plan.upload.length + plan.deleteLocal.length;
    const _every = Math.max(_CHECKPOINT, Math.ceil(_work / _MAX_CHECKPOINTS));
    let sinceCheck = 0;
    const checkpoint = async (force) => {
      if(!dirty || (!force && ++sinceCheck < _every)) return;
      sinceCheck = 0;
      try{
        await store.save(key, { manifest: nextRemote, base: nextBase, touched: [...touched] });
        report.checkpoints = (report.checkpoints || 0) + 1;
      }catch(e){
        report.checkpointFailed = (e && e.message) || String(e);
      }
    };
    const agree = (path, entry) => { nextBase[path] = entry; dirty = true; };
    const fail = (path, e, what) => {
      report.failed.push({ path, what, error: (e && e.message) || String(e) });
    };

    /* 1 & 2 — conflicts first: they are the only step that both writes AND renames, and the rename
     * has to happen before anything else can clobber the local copy. */
    let ci=0;
    for(const c of plan.conflicts){
      step('conflict', c.path, ++ci, plan.conflicts.length);
      /* VERIFY BEFORE DUPLICATING.
       *
       * Every entry written before `csum` existed carries no content identity at all — only `sha`,
       * the address of its encrypted blob. A device joining such a folder therefore has nothing to
       * compare and falls back to size+mtime, which on Android can never match, because SAF assigns
       * its own last-modified. The result is a conflict copy of every file it already had.
       *
       * But that address IS a content identity, for this user: encryption is deterministic, so the
       * same bytes under the same drive key always produce the same blob hash. So before duplicating
       * a file, ask what its bytes WOULD be addressed as. If that is what the manifest already holds,
       * the two sides are identical and there was never a conflict — and the entry is upgraded with a
       * `csum` on the way past, so no other device has to do this again.
       *
       * Bounded by the whole-file ceiling: this reads the file, and reading a 2 GB one to avoid a
       * conflict would trade a duplicate for a dead renderer. */
      const R0 = remote[c.path] || {}, L0 = local[c.path] || {};
      if(!R0.csum && R0.sha && store.blobSha && L0.size && (!o.maxBytes || L0.size <= o.maxBytes)){
        let settled = false;
        try{
          const bytes = await fs.read(id, c.path);
          if(await store.blobSha(bytes) === R0.sha){
            const entry = Object.assign({}, R0, { csum: L0.csum, size: L0.size, mtime: L0.mtime });
            if(!entry.csum) delete entry.csum;
            remember(c.path, entry);
            agree(c.path, entry);
            report.unchanged++;
            settled = true;
          }
        }catch(_){ }
        if(settled){ await checkpoint(); continue; }
      }
      try{
        await fs.move(id, c.path, c.keepAs);            // the local edit is safe from here on
        const bytes = await store.getBlob(c.sha);
        const st = await fs.write(id, c.path, bytes, (remote[c.path] || {}).mtime || 0);
        agree(c.path, { sha:c.sha, csum:(remote[c.path]||{}).csum, size:st.size, mtime:st.mtime });
        report.conflicted.push({ path:c.path, keptAs:c.keepAs });
        // The renamed copy is a new local file; the next sweep uploads it as one. Deliberately not
        // uploaded here — a conflict should not also become a network burst mid-sweep.
      }catch(e){ fail(c.path, e, 'conflict'); }
      await checkpoint();
    }

    let di=0;
    for(const d of plan.download){
      step('downloading', d.path, ++di, plan.download.length);
      try{
        const R = remote[d.path] || {};
        let st;
        if(R.chunks && R.chunks.length && store.getParts && typeof fs.writePart === 'function'){
          // Written a chunk at a time into the same `.part` file the whole-file path uses, and only
          // renamed into place at the end — so an interrupted download leaves a partial temp file and
          // never a half-written file under the real name.
          await store.getParts(R.chunks, (off, bytes) => fs.writePart(id, d.path, off, bytes));
          st = await fs.writeCommit(id, d.path, R.mtime || 0);
        } else if(R.chunks && R.chunks.length){
          fail(d.path, new Error('this device cannot receive a file that large'), 'download');
          continue;
        } else {
          const bytes = await store.getBlob(d.sha);
          st = await fs.write(id, d.path, bytes, R.mtime || 0);
        }
        agree(d.path, { sha:d.sha, csum:R.csum, chunks:R.chunks, size:st.size, mtime:st.mtime });
        report.downloaded.push(d.path);
      }catch(e){ fail(d.path, e, 'download'); }
      await checkpoint();
    }

    let ui=0;
    for(const u of plan.upload){
      step('uploading', u.path, ++ui, plan.upload.length);
      const meta = local[u.path];
      try{
        /* BIG FILES GO UP IN PIECES, when the platform can read a slice and the store can take one.
         * The whole-file path holds the plaintext, the ciphertext and the upload body at once — three
         * to four times the file — so it is the file SIZE, not the server's limit, that decides which
         * path a file takes. Where slicing is unavailable (a platform whose adapter has no readPart)
         * an oversized file is reported and skipped exactly as before: never silently dropped. */
        const big = meta && o.chunkAbove && meta.size > o.chunkAbove;
        const canChunk = big && chunky;
        if(big && !canChunk){
          report.skipped.push({ path:u.path, why:'too big for this device', size:meta.size });
          continue;
        }
        if(!big && o.maxBytes && meta && meta.size > o.maxBytes){
          report.skipped.push({ path:u.path, why:'too big', size:meta.size });
          continue;                                     // reported, never silent — and base does NOT advance
        }
        if(canChunk){
          const res = await store.putParts(
            (off, len) => fs.readPart(id, u.path, off, len), meta.size,
            (done, total) => step('uploading', u.path + ' ' + Math.round(done / total * 100) + '%', ui, plan.upload.length));
          /* `sha` MUST KEEP MEANING "the hash of this file's content", and it is the scan that
           * computes it (streamed, in the adapter). An earlier version put the hash of the CHUNK
           * LIST here, which no scan will ever produce — so every sweep compared a whole-file hash
           * against a list hash, called the file changed, and re-uploaded it. For ever, on every
           * device. An incremental scan hashes nothing and leaves it undefined, which is correct:
           * same() then falls back to size+mtime, exactly as it does for every other file. */
          const entry = { csum:meta.csum, chunks:res.chunks, size:meta.size, mtime:meta.mtime || now, device };
          if(!entry.csum) delete entry.csum;   // `chunks` is the identity when the scan did not hash
          remember(u.path, entry);
          agree(u.path, entry);
          if(res.existed) report.alreadyStored = (report.alreadyStored || 0) + 1;
          report.uploaded.push(u.path);
          await checkpoint();
          continue;
        }
        const bytes = await fs.read(id, u.path);
        /* putBlob may answer with a bare sha or with {sha, existed} — the second lets this report
         * "already stored" for a file whose bytes the server turned out to hold, which on a first
         * sweep after a lost agreement is most of them. Both shapes are accepted so a caller that
         * only ever returns a sha (and every existing test double) keeps working. */
        const put = await store.putBlob(bytes);         // encrypt + upload; dedups on identical bytes
        const sha = (put && typeof put === 'object') ? put.sha : put;
        if(put && typeof put === 'object' && put.existed){
          report.alreadyStored = (report.alreadyStored || 0) + 1;
          step('already stored', u.path, ui, plan.upload.length);
        }
        /* csum is computed here when the scan did not hash, because it is the only thing that lets
         * ANOTHER device recognise this file as one it already has. Without it the manifest carries
         * no content identity at all and every joining device falls back to size+mtime — which on
         * Android cannot match, since SAF assigns its own last-modified. We are holding the bytes
         * already; the hash is the cheapest part of this loop. */
        let csum = meta && meta.csum;
        if(!csum && store.hashBytes){ try{ csum = await store.hashBytes(bytes); }catch(_){} }
        const entry = { sha, csum, size:(meta&&meta.size)||bytes.length, mtime:(meta&&meta.mtime)||now, device };
        if(!entry.csum) delete entry.csum;
        remember(u.path, entry);
        agree(u.path, entry);
        report.uploaded.push(u.path);
      }catch(e){ fail(u.path, e, 'upload'); }
      await checkpoint();
    }

    let ti=0;
    for(const t of plan.deleteLocal){
      step('to trash', t.path, ++ti, plan.deleteLocal.length);
      try{
        const to = await fs.trash(id, t.path, now);
        agree(t.path, { deletedAt: (remote[t.path]||{}).deletedAt || now });
        report.trashed.push({ path:t.path, to });
      }catch(e){ fail(t.path, e, 'delete'); }
      await checkpoint();
    }

    for(const r of plan.deleteRemote){
      remember(r.path, { deletedAt: now });             // a tombstone, so other devices learn of it
      agree(r.path, { deletedAt: now });
      report.removedRemote.push(r.path);
      dirty = true;
    }

    // Paths the engine settled without any I/O (same bytes both sides, deleted on both) still have
    // to be recorded, or every sweep re-decides them forever.
    for(const n of plan.notes){
      const l = local[n.path], rm = remote[n.path];
      agree(n.path, l ? { csum:l.csum, chunks:(rm&&rm.chunks), size:l.size, mtime:l.mtime }
                      : { deletedAt:(rm&&rm.deletedAt)||now });
    }

    /* The final save is deliberately NOT a checkpoint: a checkpoint that fails is a slower resume,
     * and this one failing means the sweep's whole result was never recorded. It throws.
     *
     * `removed` travels with it because the SERVER refuses a manifest that shrinks sharply and
     * cannot tell a deliberate mass delete from a bug — this is the only place that knows how many
     * paths were deliberately removed, so it is what lets the store answer without asking. */
    if(dirty){
      step('saving');
      await store.save(key, { manifest: nextRemote, base: nextBase, touched: [...touched],
                              removed: report.removedRemote.length });
    }
    report.ok = report.failed.length === 0;
    return report;
  }

  /* Should a sweep run at all right now — the battery policy, plus the folder's own state. Kept here
   * so a caller has one function to poll rather than two, and so the reason is a sentence the UI can
   * show ("waiting until you plug in") instead of a silent no. */
  function due(state, prefs){ return S.shouldSync(state, prefs); }

  const API = { sweep, due };
  root.PCSyncRun = API;
  if(typeof module !== 'undefined' && module.exports) module.exports = API;
})(typeof globalThis !== 'undefined' ? globalThis : this);
