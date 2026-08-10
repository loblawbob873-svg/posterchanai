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

    step('scanning');
    const scanned = await fs.scan(id, { hash:!!o.hash, excludes:o.excludes||[], maxBytes:o.maxBytes||0 });
    report.skipped = scanned.skipped || [];
    const local = scanned.files || {};

    step('reading the manifest');
    const remote = await store.manifest(key);           // {} when the folder has never synced
    const base = (await store.base(key)) || {};

    const plan = S.diff({ local, remote, base, device, now, excludes:o.excludes||[] });
    report.unchanged = plan.unchanged;
    report.excluded = plan.excluded;
    report.plan = plan;
    if(o.dryRun) return report;

    // Agreement is recorded per file, the moment that file is actually in step — see rule 3.
    const nextBase = Object.assign({}, base);
    const nextRemote = Object.assign({}, remote);
    let dirty = false;
    const agree = (path, entry) => { nextBase[path] = entry; dirty = true; };
    const fail = (path, e, what) => {
      report.failed.push({ path, what, error: (e && e.message) || String(e) });
    };

    /* 1 & 2 — conflicts first: they are the only step that both writes AND renames, and the rename
     * has to happen before anything else can clobber the local copy. */
    let ci=0;
    for(const c of plan.conflicts){
      step('conflict', c.path, ++ci, plan.conflicts.length);
      try{
        await fs.move(id, c.path, c.keepAs);            // the local edit is safe from here on
        const bytes = await store.getBlob(c.sha);
        const st = await fs.write(id, c.path, bytes, (remote[c.path] || {}).mtime || 0);
        agree(c.path, { sha:c.sha, size:st.size, mtime:st.mtime });
        report.conflicted.push({ path:c.path, keptAs:c.keepAs });
        // The renamed copy is a new local file; the next sweep uploads it as one. Deliberately not
        // uploaded here — a conflict should not also become a network burst mid-sweep.
      }catch(e){ fail(c.path, e, 'conflict'); }
    }

    let di=0;
    for(const d of plan.download){
      step('downloading', d.path, ++di, plan.download.length);
      try{
        const bytes = await store.getBlob(d.sha);
        const st = await fs.write(id, d.path, bytes, (remote[d.path] || {}).mtime || 0);
        agree(d.path, { sha:d.sha, size:st.size, mtime:st.mtime });
        report.downloaded.push(d.path);
      }catch(e){ fail(d.path, e, 'download'); }
    }

    let ui=0;
    for(const u of plan.upload){
      step('uploading', u.path, ++ui, plan.upload.length);
      const meta = local[u.path];
      try{
        if(o.maxBytes && meta && meta.size > o.maxBytes){
          report.skipped.push({ path:u.path, why:'too big', size:meta.size });
          continue;                                     // reported, never silent — and base does NOT advance
        }
        const bytes = await fs.read(id, u.path);
        const sha = await store.putBlob(bytes);         // encrypt + upload; dedups on identical bytes
        const entry = { sha, size:(meta&&meta.size)||bytes.length, mtime:(meta&&meta.mtime)||now, device };
        nextRemote[u.path] = entry;
        agree(u.path, entry);
        report.uploaded.push(u.path);
      }catch(e){ fail(u.path, e, 'upload'); }
    }

    let ti=0;
    for(const t of plan.deleteLocal){
      step('to trash', t.path, ++ti, plan.deleteLocal.length);
      try{
        const to = await fs.trash(id, t.path, now);
        agree(t.path, { deletedAt: (remote[t.path]||{}).deletedAt || now });
        report.trashed.push({ path:t.path, to });
      }catch(e){ fail(t.path, e, 'delete'); }
    }

    for(const r of plan.deleteRemote){
      nextRemote[r.path] = { deletedAt: now };          // a tombstone, so other devices learn of it
      agree(r.path, { deletedAt: now });
      report.removedRemote.push(r.path);
      dirty = true;
    }

    // Paths the engine settled without any I/O (same bytes both sides, deleted on both) still have
    // to be recorded, or every sweep re-decides them forever.
    for(const n of plan.notes){
      const l = local[n.path], rm = remote[n.path];
      agree(n.path, l ? { sha:l.sha, size:l.size, mtime:l.mtime } : { deletedAt:(rm&&rm.deletedAt)||now });
    }

    if(dirty){ step('saving'); await store.save(key, { manifest: nextRemote, base: nextBase }); }
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
