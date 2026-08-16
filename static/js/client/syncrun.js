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
 *  2. DOWNLOAD before DELETE — for the same PATH. A sweep that overwrites first and then fails to
 *     fetch the replacement has removed a file it cannot restore, which is why a conflict renames
 *     before it writes. It does NOT mean deletions wait for unrelated transfers: diff() puts each
 *     path in exactly one bucket, so nothing in `deleteLocal` has a download coming. Local deletes
 *     therefore run FIRST — they are renames into `.pc-trash`, instant and recoverable, and queuing
 *     them behind hours of upload is why a folder with a backlog appeared never to apply a delete
 *     at all.
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
  // How large a file is worth re-reading to avoid duplicating it. Reading costs time; a conflict
  // copy costs a permanent duplicate on every device, so this is deliberately generous.
  const _VERIFY_MAX = 2 * 1024 * 1024 * 1024;
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
    /* STOPPING HAS TO STOP THIS SWEEP, not the next one.
     *
     * Pausing a folder used to set a flag the POLICY reads, which decides whether a sweep may start —
     * so pressing it during a sweep of several hundred files did nothing at all for as long as that
     * sweep took, and the button plainly lied. Checked between files here: a file in flight finishes
     * (interrupting mid-upload just wastes the transfer), then the sweep stores what it has agreed
     * and returns. Everything already done stays done, and the next run resumes from the checkpoint,
     * because that is what checkpoints are for. */
    const stopping = () => { try{ return typeof o.shouldStop === 'function' && !!o.shouldStop(); }catch(_){ return false; } };
    const halt = async () => { report.stopped = true; await checkpointRef.fn(true); return report; };
    const checkpointRef = { fn: async () => {} };

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

    /* COLLECT ABANDONED `.part` FILES BEFORE SCANNING, and this needs a caller or it is decoration.
     *
     * They are invisible to everything else: `ignored()` keeps them out of the scan (rightly — a
     * half-written file must never be uploaded), so nothing ever looks at them again and every
     * interrupted download leaves its bytes on the disk for good. On a folder of videos that is real
     * money, and it is the same shape as the trash that could not be emptied.
     *
     * A DAY, not an hour: a part file this sweep is about to resume from must survive, and the only
     * safe way to say "no download is coming back for this" is an age no sweep can still be inside.
     * Best-effort — a folder that cannot be swept is not a folder that cannot be synced. */
    if(typeof fs.sweepParts === 'function'){
      try{
        const gone = await fs.sweepParts(id, 24 * 3600000);
        if(gone && gone.removed) report.partsCollected = gone;
      }catch(_){}
    }

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

    // `let`, because a refused mass delete replaces it with a copy carrying no deletions — while
    // `report.plan` keeps the original, so the panel can still show what was refused.
    let plan = S.diff({ local, remote, base, device, now, excludes:o.excludes||[] });
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
    checkpointRef.fn = checkpoint;
    const agree = (path, entry) => { nextBase[path] = entry; dirty = true; };

    /* A DOWNLOAD MUST BE THE FILE IT CLAIMS TO BE, checked against the checksum the manifest carries.
     *
     * Until this existed a download was recorded as agreed carrying the REMOTE's `csum` without
     * anyone ever hashing what actually landed: right length, wrong bytes, and `base` then asserted
     * the file was correct for ever. Nothing would look at it again short of a Deep check — which is
     * exactly how a corrupt copy outlives the bug that made it.
     *
     * `csum` is the scan's own sha256 of the PLAINTEXT file, so re-hashing what we wrote and
     * comparing is a like-for-like test. It is checked BEFORE the commit, on the `.part` file: after
     * writeCommit the new file has already been renamed into place over the old one, so a check
     * there is a report rather than a defence. A mismatch throws, the loop's catch records it as a
     * per-file failure, and the part file is thrown away — deliberately NOT into `.pc-trash`, which
     * is for somebody's files, not for bytes we could not confirm.
     *
     * Both guards are skipped when there is nothing to check against: an entry written before `csum`
     * existed carries no content identity, and a platform whose adapter has no `hashPart` (any older
     * shell) keeps the previous behaviour rather than refusing to sync at all. */
    const verifyPart = async (p, R) => {
      if(!R || !R.csum || typeof fs.hashPart !== 'function') return;
      let got = null;
      try{ got = await fs.hashPart(id, p); }catch(_){ return; }   // cannot check ≠ known bad
      if(!got || got === R.csum) return;
      try{ if(typeof fs.discardPart === 'function') await fs.discardPart(id, p); }catch(_){}
      throw new Error('checksum mismatch after download — refusing to write it (wanted '
                      + String(R.csum).slice(0, 12) + ', got ' + String(got).slice(0, 12) + ')');
    };
    const verifyBytes = async (p, R, bytes) => {
      if(!R || !R.csum || !store.hashBytes) return;
      let got = null;
      try{ got = await store.hashBytes(bytes); }catch(_){ return; }
      if(!got || got === R.csum) return;
      throw new Error('checksum mismatch after download — refusing to write it (wanted '
                      + String(R.csum).slice(0, 12) + ', got ' + String(got).slice(0, 12) + ')');
    };
    const fail = (path, e, what) => {
      report.failed.push({ path, what, error: (e && e.message) || String(e) });
    };

    /* GIVE EVERY ENTRY A CONTENT IDENTITY WHILE WE ARE HERE.
     *
     * An entry written before `csum` existed can never be compared by content, so every device that
     * hashes has to fall back to size+mtime — which Android can never match, because SAF assigns its
     * own last-modified. Those paths conflict for ever, and the verify below only rescues the ones
     * already headed for a conflict on THIS sweep; the rest stay unverifiable and conflict on the
     * next device, and the next.
     *
     * So whenever this device has hashed a file and the manifest agrees it is the same file, the
     * entry gains that hash. It is not a new fact — it is the one this sweep just established,
     * written down where the other devices can use it. Nothing else about the entry changes, and a
     * path that is genuinely different is untouched, because same() had to be true to get here.
     *
     * This is what turns "all my devices are on the latest build and it still conflicts" into a
     * folder that repairs itself on the first hashing sweep from any device that can read it. */
    let repaired = 0;
    for(const path in local){
      const L = local[path], R = remote[path];
      if(!L || !L.csum || !R || R.csum || R.deletedAt) continue;
      if(!S.same(L, R)) continue;                     // only where the two already agree
      remember(path, Object.assign({}, R, { csum: L.csum }));
      repaired++;
    }
    if(repaired){ report.repaired = repaired; dirty = true; }

    /* DELETIONS RUN FIRST, BEFORE ANY BYTE MOVES, and that ordering is the whole reason a delete
     * appeared never to happen.
     *
     * They used to run last — after conflicts, downloads and uploads. On a settled folder that is
     * invisible; on a folder with a BACKLOG it means the deletions are queued behind hours of
     * network transfer and are simply never reached. Measured: a laptop whose Check reported 3,041
     * changes, of which ~300 were deletions, moving files at ~7 a minute. Every sweep spent itself
     * on transfers, was interrupted (screen off, app closed, Stop), and restarted the transfer loops
     * from the top — so the deletions sat there across sweep after sweep while the user pressed Sync
     * and watched Check report the same number. "How do I actually make it remove?"
     *
     * A local delete is a RENAME into `.pc-trash`: instant, no network, and recoverable. There is no
     * reason for it to wait behind a 40 GB upload.
     *
     * THIS DOES NOT BREAK RULE 2 ("download before delete"), because the two never touch the same
     * path. diff() puts each path in exactly ONE bucket: `deleteLocal` is where the manifest holds a
     * tombstone, `download` is where it holds live bytes. No file being trashed here has a download
     * coming for it, so the case rule 2 protects — deleting something and then failing to fetch its
     * replacement — cannot arise. The rule still governs conflicts, which rename before they write.
     */
    /* A SWEEP THAT WOULD EMPTY THE FOLDER STOPS AND ASKS — and if nobody is there to ask, it does
     * not delete. See S.massDelete for what happened without this.
     *
     * REFUSING SUPPRESSES DELETION ONLY, never the uploads and downloads above it, for the reason
     * the contacts sweep learned the hard way: a guard that aborts the whole sweep turns "it deleted
     * everything" into "it syncs nothing, for ever", which is the same bug with the sign flipped.
     *
     * The refused paths are deliberately NOT agreed. `base` still says nothing about them, so the
     * next sweep re-proposes exactly this and asks again — a refusal has to be a question that keeps
     * being asked, not a decision recorded once. Saying yes re-runs with `forceTrash`. */
    const mass = S.massDelete(plan);
    if(mass && !o.forceTrash){
      let ok = false;
      if(typeof o.confirmTrash === 'function'){
        try{ ok = !!(await o.confirmTrash(mass)); }catch(_){ ok = false; }
      }
      if(!ok){ report.refusedTrash = mass; plan = Object.assign({}, plan, { deleteLocal: [] }); }
    }

    let ti=0;
    for(const t of plan.deleteLocal){
      if(stopping()) return await halt();
      step('to trash', t.path, ++ti, plan.deleteLocal.length);
      try{
        const to = await fs.trash(id, t.path, now);
        agree(t.path, { deletedAt: (remote[t.path]||{}).deletedAt || now });
        report.trashed.push({ path:t.path, to });
      }catch(e){ fail(t.path, e, 'delete'); }
      await checkpoint();
    }

    /* 1 & 2 — conflicts first: they are the only step that both writes AND renames, and the rename
     * has to happen before anything else can clobber the local copy. */
    let ci=0;
    for(const c of plan.conflicts){
      if(stopping()) return await halt();
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
      /* THE OTHER HALF: the manifest HAS a content identity and this scan does not.
       *
       * An ordinary sweep never hashes — that is what makes it ordinary — so `L0.csum` is absent and
       * the comparison falls back to size+mtime, which on Android can never match, because SAF gives
       * a downloaded file whatever last-modified it likes. So a folder whose entries all carry a
       * checksum STILL conflicted on every file, for want of hashing the one file about to be
       * duplicated. Everything below covered the reverse case and missed this one.
       *
       * Hash it now. It is one file, already about to be read and copied, and the answer is exact. */
      /* BOUNDED BY _VERIFY_MAX, NOT by maxBytes. `fs.read` pulls the WHOLE file into the renderer —
       * plaintext, then ciphertext or a hash pass over it — and maxBytes() now answers 8 GB for any
       * adapter that can slice, because that is the ceiling on what CHUNKING can carry. Reading a
       * 6 GB file whole to avoid one conflict copy is the renderer kill that chunking exists to
       * prevent: Chromium takes the process, which in the desktop app is a black window. The chunked
       * verify sitting between these two already uses _VERIFY_MAX and readPart; these are the same
       * decision and get the same bound. */
      if(R0.csum && !L0.csum && store.hashBytes && L0.size && L0.size <= _VERIFY_MAX){
        let settled = false;
        try{
          if(await store.hashBytes(await fs.read(id, c.path)) === R0.csum){
            const entry = Object.assign({}, R0, { size: L0.size, mtime: L0.mtime });
            remember(c.path, entry);
            agree(c.path, Object.assign({}, entry, { csum: R0.csum }));
            report.unchanged++; settled = true;
          }
        }catch(_){ }
        if(settled){ await checkpoint(); continue; }
      }
      /* A CHUNKED entry has no `sha` at all — its address is the LIST — so the check below skipped
       * every file over one chunk and duplicated it anyway. Reported after the first fix landed:
       * still conflicting, on the big ones. Comparing the list costs a read and an encrypt of the
       * local file and no transfer at all, which is far cheaper than the copy it avoids. */
      if(!R0.csum && R0.chunks && R0.chunks.length && store.chunkShas && typeof fs.readPart === 'function'
         && L0.size && L0.size <= _VERIFY_MAX){
        let settled = false;
        try{
          // At the size the ENTRY used — our own preference would produce a different list and
          // 'different' is exactly the wrong answer here.
          const mine = await store.chunkShas((off, len) => fs.readPart(id, c.path, off, len), L0.size, R0.cs || 0);
          if(mine.length === R0.chunks.length && mine.every((x, i) => x === R0.chunks[i])){
            const entry = Object.assign({}, R0, { csum: L0.csum, size: L0.size, mtime: L0.mtime });
            if(!entry.csum) delete entry.csum;
            remember(c.path, entry); agree(c.path, entry);
            report.unchanged++; settled = true;
          }
        }catch(_){ }
        if(settled){ await checkpoint(); continue; }
      }
      /* BOUNDED BY _VERIFY_MAX, NOT by maxBytes. `fs.read` pulls the WHOLE file into the renderer —
       * plaintext, then ciphertext or a hash pass over it — and maxBytes() now answers 8 GB for any
       * adapter that can slice, because that is the ceiling on what CHUNKING can carry. Reading a
       * 6 GB file whole to avoid one conflict copy is the renderer kill that chunking exists to
       * prevent: Chromium takes the process, which in the desktop app is a black window. The chunked
       * verify sitting between these two already uses _VERIFY_MAX and readPart; these are the same
       * decision and get the same bound. */
      if(!R0.csum && R0.sha && store.blobSha && L0.size && L0.size <= _VERIFY_MAX){
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
        /* THE INCOMING COPY ARRIVES THE SAME WAY A DOWNLOAD DOES, chunks included. This used to be
         * `store.getBlob(c.sha)` alone, and a chunked entry has no `sha` — so a real conflict on a
         * file over one chunk threw AFTER the rename above had already happened: the local copy sat
         * under its conflict name, the incoming one never arrived, and the original name was simply
         * gone. Renaming first is what makes that survivable; it is not a reason to leave the second
         * half broken. */
        const R = remote[c.path] || {};
        let st;
        if(R.chunks && R.chunks.length && store.getParts && typeof fs.writePart === 'function'){
          await store.getParts(R.chunks, (off, bytes) => fs.writePart(id, c.path, off, bytes), R.size);
          await verifyPart(c.path, R);            // the incoming copy takes the NAME — it had better be right
          st = await fs.writeCommit(id, c.path, R.mtime || 0);
        } else {
          const bytes = await store.getBlob(c.sha || R.sha);
          await verifyBytes(c.path, R, bytes);
          st = await fs.write(id, c.path, bytes, R.mtime || 0);
        }
        /* `cs` TRAVELS WITH `chunks`, always. An entry is only comparable to another made at the
         * same chunk size, and same() reads `(a.cs||0) === (b.cs||0)` — so a base entry that kept
         * the chunk list and dropped the size can never match the manifest entry it came from. A
         * chunked upload from an incremental sweep carries no `csum` either, so the comparison then
         * falls to size+mtime, which Android can never satisfy (SAF assigns its own last-modified).
         * The file is re-downloaded on EVERY sweep, for ever, and each writeCommit moves the
         * previous copy into .pc-trash — so the disk fills as well. */
        agree(c.path, { sha:c.sha || R.sha, csum:R.csum, chunks:R.chunks, cs:R.cs, size:st.size, mtime:st.mtime });
        report.conflicted.push({ path:c.path, keptAs:c.keepAs });
        // The renamed copy is a new local file; the next sweep uploads it as one. Deliberately not
        // uploaded here — a conflict should not also become a network burst mid-sweep.
      }catch(e){ fail(c.path, e, 'conflict'); }
      await checkpoint();
    }

    let di=0;
    for(const d of plan.download){
      if(stopping()) return await halt();
      step('downloading', d.path, ++di, plan.download.length);
      try{
        const R = remote[d.path] || {};
        let st;
        if(R.chunks && R.chunks.length && store.getParts && typeof fs.writePart === 'function'){
          // Written a chunk at a time into the same `.part` file the whole-file path uses, and only
          // renamed into place at the end — so an interrupted download leaves a partial temp file and
          // never a half-written file under the real name.
          // `R.size` is the length the manifest recorded — see getParts. A chunk list that does not
          // rebuild to it is refused here rather than committed over a good file.
          /* RESUME what a dropped connection left behind. Uploads have always resumed (a chunk is
           * skipped when the server already holds it); this is the receiving side finally doing the
           * same, instead of re-fetching an 8 GB video from byte zero because the link blinked at
           * 95%. Only whole chunks are reused, and only alongside verifyPart below — a part file
           * left by a DIFFERENT version of this path is caught by the checksum and discarded, so a
           * bad prefix cannot be resumed onto for ever. */
          /* RESUME ONLY WHERE THE RESULT CAN BE CHECKED. A part file is not tied to a chunk list by
           * anything but its length, so resuming onto one left by an EARLIER generation of the same
           * path splices two files together — and the only thing that catches that is the checksum.
           * `verifyPart` returns early when the entry has no `csum` (routine: an incremental sweep
           * uploads chunks and deletes csum) and on any adapter without `hashPart` (every Android
           * build today), so in exactly those cases resuming is an unverified splice. Start from
           * zero there: a slower download is not a bug, a silently wrong file is. */
          const canVerify = !!(R.csum && typeof fs.hashPart === 'function');
          let have = 0;
          if(canVerify){
            try{ if(typeof fs.partSize === 'function') have = await fs.partSize(id, d.path); }catch(_){}
          } else {
            // …and clear whatever is lying there, or it is resumed onto the moment a csum appears.
            try{ if(typeof fs.discardPart === 'function') await fs.discardPart(id, d.path); }catch(_){}
          }
          await store.getParts(R.chunks, (off, bytes) => fs.writePart(id, d.path, off, bytes),
                               R.size, have, R.cs || 0);
          await verifyPart(d.path, R);            // …and it must BE the file it claims to be
          st = await fs.writeCommit(id, d.path, R.mtime || 0);
        } else if(R.chunks && R.chunks.length){
          fail(d.path, new Error('this device cannot receive a file that large'), 'download');
          continue;
        } else {
          const bytes = await store.getBlob(d.sha);
          await verifyBytes(d.path, R, bytes);
          st = await fs.write(id, d.path, bytes, R.mtime || 0);
        }
        /* `cs` TRAVELS WITH `chunks`, always. An entry is only comparable to another made at the
         * same chunk size, and same() reads `(a.cs||0) === (b.cs||0)` — so a base entry that kept
         * the chunk list and dropped the size can never match the manifest entry it came from. A
         * chunked upload from an incremental sweep carries no `csum` either, so the comparison then
         * falls to size+mtime, which Android can never satisfy (SAF assigns its own last-modified).
         * The file is re-downloaded on EVERY sweep, for ever, and each writeCommit moves the
         * previous copy into .pc-trash — so the disk fills as well. */
        agree(d.path, { sha:d.sha, csum:R.csum, chunks:R.chunks, cs:R.cs, size:st.size, mtime:st.mtime });
        report.downloaded.push(d.path);
      }catch(e){ fail(d.path, e, 'download'); }
      await checkpoint();
    }

    let ui=0;
    /* REPUBLISHING SOMEBODY'S DELETIONS IS GUARDED THE SAME WAY DELETING THEM IS.
     *
     * `delete loses to edit` is right per file and catastrophic in bulk: a device whose timestamps
     * moved under it (restored from backup, copied in, rsynced without -t) reads every tombstoned
     * path as edited here and refills the folder on every other device. The mass-delete guard could
     * not see it — that one only ever suppresses `deleteLocal`, and it runs AFTER this loop, so the
     * files were already back on every device by the time anything asked.
     *
     * Refusing drops only the resurrections. Ordinary uploads, downloads and deletions still run,
     * and the refused paths are NOT agreed — so the next sweep proposes them again and asks again,
     * rather than recording a decision nobody made. */
    const massUp = S.massResurrect(plan);
    if(massUp && !o.forceResurrect){
      let ok = false;
      if(typeof o.confirmResurrect === 'function'){
        try{ ok = !!(await o.confirmResurrect(massUp)); }catch(_){ ok = false; }
      }
      if(!ok){
        report.refusedResurrect = massUp;
        plan = Object.assign({}, plan, { upload: plan.upload.filter(u => !(u && u.resurrect)) });
      }
    }

    /* COUNTED WHERE IT ACTUALLY HAPPENED, at the two points an upload is known to have landed.
     *
     * Incremented at the top of the loop it claimed files were republished that were then skipped
     * for being too big, or that threw — a status line asserting a thing the manifest flatly
     * contradicts, on the one sweep somebody is reading it to find out what went wrong. PATHS, not
     * a tally: "kept 3,930 files another device deleted" is unactionable without them, and every
     * other reported category already lists what it is talking about. */
    const uploaded = (u) => {
      report.uploaded.push(u.path);
      if(u && u.resurrect) (report.resurrected = report.resurrected || []).push({ path: u.path, why: u.why });
    };

    for(const u of plan.upload){
      if(stopping()) return await halt();
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
          /* THE PLATFORM'S CHUNK SIZE, which sync.js works out and this used to throw away.
           *
           * `o.chunkBytes` is FS().chunkBytes — 4 MB on Android, chosen precisely because every
           * chunk crosses the Capacitor bridge as base64 (four bytes of string per three of data,
           * held as UTF-16), so a 16 MB chunk is ~21 MB of string before the plaintext, the
           * ciphertext and the Java-side copy are counted. Omitting it here fell back to
           * _SYNC_CHUNK, 16 MB, on every device — which is the tablet reloading mid-sync that the
           * 4 MB figure was measured to prevent. */
          const res = await store.putParts(
            (off, len) => fs.readPart(id, u.path, off, len), meta.size,
            (done, total) => step('uploading', u.path + ' ' + Math.round(done / total * 100) + '%', ui, plan.upload.length),
            o.chunkBytes || 0);
          /* `sha` MUST KEEP MEANING "the hash of this file's content", and it is the scan that
           * computes it (streamed, in the adapter). An earlier version put the hash of the CHUNK
           * LIST here, which no scan will ever produce — so every sweep compared a whole-file hash
           * against a list hash, called the file changed, and re-uploaded it. For ever, on every
           * device. An incremental scan hashes nothing and leaves it undefined, which is correct:
           * same() then falls back to size+mtime, exactly as it does for every other file. */
          /* `cs` IS PART OF THE IDENTITY, and this path was dropping it.
           *
           * A chunk list identifies content at the size it was made with and at no other: split the
           * same video 16 MB at a time and 4 MB at a time and the two lists have nothing in common.
           * Android chooses 4 MB (its bridge copies every chunk as base64) and the desktop 16 MB, so
           * that is not hypothetical — it is what happens the moment a phone and a laptop hold the
           * same file. `same()` compares `(a.cs||0) === (b.cs||0)`, so two entries that both LOST it
           * compare as though they were made the same way, and the differing lists then read as an
           * edit: a conflict copy on every device, for a file nobody touched.
           *
           * sync.js's web-upload path has always written it (`cs: res.cs || CH`); this one, the one
           * every actual sweep goes through, did not. It is also what lets an interrupted download
           * resume, since resuming needs to know how big a whole chunk is. */
          const entry = { csum:meta.csum, chunks:res.chunks, cs:res.cs || 0,
                          size:meta.size, mtime:meta.mtime || now, device };
          if(!entry.csum) delete entry.csum;   // `chunks` is the identity when the scan did not hash
          if(!entry.cs) delete entry.cs;       // absent means "the one size that existed before cs did"
          remember(u.path, entry);
          agree(u.path, entry);
          if(res.existed) report.alreadyStored = (report.alreadyStored || 0) + 1;
          uploaded(u);
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
        uploaded(u);
      }catch(e){ fail(u.path, e, 'upload'); }
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
