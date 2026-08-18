/* Folder sync — the executor. It moves the bytes the reconciler decided on, and nothing else.
 *
 * The split is the safety: syncengine.js decides and cannot touch a file; this decides nothing and
 * cannot be reached without a plan that has been checked. Every rule about WHAT should happen lives
 * over there, where it is pure and can be run exhaustively; every rule about HOW to do it without
 * losing anything lives here.
 *
 * WHAT THIS FILE IS RESPONSIBLE FOR, and each line was paid for by a real failure:
 *
 *   AN INPUT IT COULD NOT READ IS NEVER A VALUE. Not the journal, not a device's view, not the
 *   scan. A read that fails throws; a device that does not answer is COUNTED, and the count reaches
 *   the checker, which refuses every deletion. Empty and unreadable were once the same thing here,
 *   and that is what put a Pictures folder in the trash.
 *
 *   IT RESUMES. Every file is journalled the moment it lands, so an interrupted sweep repeats at
 *   most the last few files — and repeating one is free, because an upload whose bytes the server
 *   already holds is skipped and a download that is already on disk hashes equal and settles.
 *
 *   EVERY BYTE IS CHECKED. A download is written to a temp file, hashed, and only then renamed over
 *   the real name; an upload records the file's own hash so the other devices can do the same. A
 *   file that fails its checksum is reported, never committed.
 *
 *   IT FITS IN MEMORY. The scan is paged, big files stream a chunk at a time, small files go four at
 *   a time and large ones one at a time. What is held for the whole sweep is three compact maps —
 *   about a hundred bytes per path — and never the folder's contents.
 *
 *   IT PUBLISHES ONLY ITS OWN VIEW. One writer per document, so two devices syncing at once cannot
 *   overwrite each other, and there is no merge-on-save, no re-read per checkpoint and no
 *   server-side collapse guard to get wrong.
 */
(function(root){
  'use strict';

  const E = root.PCSyncEngine || (typeof require === 'function' ? require('./syncengine.js') : null);
  const P = root.PCFolderSync || (typeof require === 'function' ? require('./foldersync.js') : null);
  if(!E || !P) throw new Error('syncexec.js needs syncengine.js and foldersync.js');

  const SCAN_PAGE = 1000;          // paths per bridge call — bounds the payload, not the folder
  const LANES = 4;                 // small files in flight at once; the wait is the round trip
  /* AND "SMALL" IS A SIZE, NOT "NOT CHUNKED".
   *
   * A transfer that is not chunked is held WHOLE, and an upload holds it three or four times over:
   * the plaintext, the ciphertext, the request body, and a hash pass across them. At the chunking
   * threshold — 16 MB on the desktop — that is some 64 MB for one file, and four of those at once is
   * a quarter of a gigabyte of transient allocation. Reported immediately: "windows app keeps
   * running out of memory trying to sync".
   *
   * So overlapping is for files where it is nearly free, which is the case it was added for: six
   * thousand photos, where the wait is the round trip and the bytes are nothing. Anything larger goes
   * one at a time, whether it is chunked or not. */
  const PARALLEL_MAX = 2 * 1024 * 1024;
  const SAVE_EVERY = 200;          // files between journal writes
  const SAVE_MS = 20 * 1000;
  const PUBLISH_EVERY = 500;       // changes before telling the other devices, mid-sweep

  const now0 = () => Date.now();

  /* ---- the sweep ------------------------------------------------------------------------------- */

  async function sweep(fs, io, opts){
    const o = opts || {};
    const key = o.key || o.id;
    const me = o.device || 'this device';
    const now = o.now || now0();
    /* LATCHED FOR THE LIFE OF THE SWEEP. The caller's signal is a Set the Start button clears —
     * and pressing Pause then Start inside one scan (minutes, on a hashing first sweep) used to
     * un-latch it mid-flight: the scan had already returned a PARTIAL disk, the next check saw
     * false, and every unscanned file read as "deleted locally". A sweep that has seen a stop is
     * halting, whatever the button does next; Start begins a fresh sweep with fresh state. */
    let _stopSeen = false;
    const stopping = () => {
      if(_stopSeen) return true;
      try{ _stopSeen = typeof o.shouldStop === 'function' && !!o.shouldStop(); }
      catch(_){ _stopSeen = false; }
      return _stopSeen;
    };
    const tick = (p) => { try{ if(typeof o.onProgress === 'function') o.onProgress(p); }catch(_){} };
    /* RENEW THE CPU LEASE WHILE THERE IS STILL WORK.
     *
     * Android's wake lock is TIMED — held is not renewed — so a sweep longer than that bound loses
     * the processor part-way and stops mid-file, which is the same failure as having no lock at all,
     * arriving ten minutes later. Renewed from `step`, the one call every loop already makes per
     * file, and throttled because it crosses the Capacitor bridge. A no-op on desktop and on an APK
     * without it. */
    let _wokeAt = 0;
    const _keepAwake = () => {
      if(typeof fs.wakeBegin !== 'function') return;
      const t = now0();
      if(t - _wokeAt < 60000) return;
      _wokeAt = t;
      try{ const r = fs.wakeBegin(); if(r && r.catch) r.catch(()=>{}); }catch(_){}
    };
    const step = (phase, path, i, n) => { _keepAwake(); tick({ phase, path, i, n }); };

    const report = { uploaded:[], downloaded:[], trashed:[], conflicted:[], removedRemote:[],
                     failed:[], skipped:[], unchanged:0, settledGone:0, excluded:0, refused:[], device: me,
                     dryRun: !!o.dryRun, ok: true };

    /* 1. What this device has already applied. A journal that cannot be read is not an empty
     *    journal: with no journal every file on both sides looks new and independently changed,
     *    which is a conflict copy per path — thousands of them, and a folder to repair by hand. */
    let index;
    try{ index = (await io.index(key)) || {}; }
    catch(e){ throw new Error('could not read this device’s sync record — nothing has been changed. ('
                              + msg(e) + ')'); }

    /* 2. What every device says the folder holds. Ours included, so a path we hold is never absent
     *    from the merge. A device that does not answer is counted, not assumed empty. */
    let views, missing = 0;
    try{
      const got = await io.views(key);
      views = (got && got.views) || {};
      missing = (got && got.missing) || 0;
      report.cannot = (got && got.cannot) || [];
    }catch(e){
      throw new Error('could not read what your devices have — nothing has been changed. ('
                      + msg(e) + ')');
    }
    if(!Object.keys(views).length && missing) throw new Error('none of your devices could be read — '
                                                              + 'nothing has been changed');
    const merged = E.merge(views);
    report.devices = merged.devices.length;
    report.missingViews = missing;

    /* COLLECT ABANDONED `.part` FILES BEFORE SCANNING, and this needs a caller or it is decoration.
     *
     * They are invisible to everything else: the scan ignores them (rightly — a half-written file
     * must never be uploaded), so nothing ever looks at them again and every interrupted download
     * leaves its bytes on the disk for good. On a folder of videos that is real money.
     *
     * A DAY, not an hour: a part file THIS sweep is about to resume from must survive, and the only
     * safe way to say "no download is coming back for this" is an age no sweep can still be inside.
     * Best-effort — a folder that cannot be swept is not a folder that cannot be synced. */
    if(typeof fs.sweepParts === 'function'){
      try{
        const gone = await fs.sweepParts(o.id, 24 * 3600000);
        if(gone && gone.removed) report.partsCollected = gone.removed;
      }catch(_){}
    }

    /* AN EMPTY JOURNAL FORCES A HASH. THIS IS NOT AN OPTIMISATION, IT IS THE WHOLE ANSWER.
     *
     * With no journal, every path looks changed on BOTH sides at once — the device has files and
     * has applied nothing — so the reconciler falls through to "did they end up the same anyway?",
     * and that question is answered by content when there is a checksum and by size+mtime when there
     * is not. An ordinary sweep does not hash.
     *
     * On a phone, size+mtime CANNOT match: SAF assigns its own last-modified to everything it
     * writes, so a file the phone downloaded yesterday has a timestamp no other device ever saw.
     * Every path therefore reads as "edited on both" and the sweep makes a conflict copy of the
     * entire folder — reported as "phone is now downloading 1803 conflicts".
     *
     * So a device with nothing applied pays for one full hash and settles the folder by content,
     * which is what the native sweep has always done (`firstEver` in NativeSweep) and what the page
     * half was missing. It is expensive once; a conflict copy of every file is expensive for ever.
     */
    /* HOW MUCH OF THIS FOLDER CAN THE JOURNAL ACTUALLY ANSWER FOR?
     *
     * "Empty" was too narrow. The journal is written in batches and the settle entries — the bulk of
     * what a device that already holds the folder records — land at the very end, so a first sweep
     * interrupted anywhere (the renderer killed, a stop pressed, a failed write) leaves a handful of
     * entries behind. The next sweep then sees a non-empty journal, does not hash, and every
     * remaining path is "edited on both" again: the conflict storm returns by the back door.
     *
     * So the test is COVERAGE, not emptiness. Below half, this device cannot answer for its own
     * folder and must settle it by content.
     *
     * A dry run hashes too, or Preview and the sweep disagree by the whole folder — on precisely the
     * device this exists for. */
    const known = Object.keys(index).length;
    const seen = Object.keys(merged.global).length;
    const thin = known === 0 || (seen > 0 && known < seen / 2);
    const scanOpts = thin ? Object.assign({}, o, { hash: true }) : o;
    if(thin) report.hashed = true;

    /* 3. The disk, a page at a time. */
    step('scanning');
    let disk, unread;
    try{ const got = await scan(fs, scanOpts, stopping); disk = got.disk; unread = got.unread; }
    catch(e){ throw new Error('could not read the folder on this device — nothing has been changed. ('
                              + msg(e) + ')'); }
    if(stopping()) return halt(report);
    report.scanned = Object.keys(disk).length;
    if(unread.length) report.unreadable = unread.slice(0, 200);

    /* 4. Decide, check, and let a person answer for anything that is theirs to answer.
     * Paths the scan could not read join the exclusions: dropped from all three inputs, so an
     * unreadable subtree can neither be deleted here nor tombstoned to anyone. */
    let plan = E.reconcile({ disk, global: merged.global, rivals: merged.rivals, by: merged.by,
                             index, device: me, now,
                             excludes: (o.excludes || []).concat(unread) });

    /* PATHS THE CALLER DEMANDS BE SENT AGAIN.
     *
     * "The store lost these bytes and this device still has the file" cannot be expressed by editing
     * the journal: with the entry gone both sides read as changed, the reconciler asks whether they
     * are the same anyway, the checksums match — because it IS the same file — and it settles. The
     * repair reported "3 queued to send again" and sent nothing, which is the worst possible
     * outcome for a repair.
     *
     * So it is said outright. Only paths this device actually holds, and the version goes past
     * whatever the folder shows so the entry is not immediately outvoted. */
    if(o.resend && o.resend.length && !o.dryRun){
      const want = new Set(o.resend);
      const already = new Set(plan.send.map(u => u.path));
      const extra = [];
      for(const p of want){
        if(already.has(p) || !disk[p]) continue;
        extra.push({ path: p, v: E.bump(merged.global[p], index[p]), stat: disk[p],
                     why: 'sending again — the store no longer has these bytes' });
      }
      if(extra.length){
        const drop = new Set(extra.map(x => x.path));
        plan = Object.assign({}, plan, {
          send: plan.send.concat(extra),
          settle: plan.settle.filter(x => !drop.has(x.path)),
          fetch: plan.fetch.filter(x => !drop.has(x.path)),
          keepBoth: plan.keepBoth.filter(x => !drop.has(x.path)),
        });
        report.resent = extra.length;
      }
    }
    report.plan = plan;
    report.unchanged = plan.unchanged;
    report.settledGone = plan.settledGone || 0;
    report.excluded = plan.excluded;

    const verdicts = E.check(plan, { global: merged.global, missingViews: missing,
                                     indexSize: Object.keys(index).length });
    const allowed = [];
    for(const v of verdicts){
      if(v.fatal){ report.refused.push(v); continue; }
      let ok = false;
      if(!o.dryRun && o.manual && typeof o.confirm === 'function'){
        try{ ok = !!(await o.confirm(v)); }catch(_){ ok = false; }
      }
      if(ok) allowed.push(v.kind); else report.refused.push(v);
    }
    plan = E.apply(plan, verdicts, allowed);

    /* Which copies this device has already failed to fetch — supplied by the caller, which persists
     * it. Read here, above every loop that fetches, because the conflict path needs it as much as
     * the download path does. */
    const skipFetch = o.skipFetch || {};
    const idOf = (e) => (e && (e.csum || e.sha || (e.chunks && e.chunks.join(',')))) || '';
    /* What a remembered failure still applies to.
     *
     * `checksum` is for ever — the same bytes always hash the same way. `gone` is a 404 and EXPIRES,
     * because content-addressed bytes that come back come back under the same identity, so an
     * unexpiring block would make one bad minute permanent. A pressed button clears both: somebody
     * standing there asking again is the clearest possible signal to try. Older entries were a bare
     * string; they are read as permanent, which is what they were. */
    const GONE_FOR = 6 * 3600 * 1000;
    const skipId = (v, raw) => {
      if(!v) return '';
      if(typeof v === 'string') return v;
      if(o.manual && !raw) return '';
      if(v.why === 'gone' && (now0() - (v.at || 0)) > GONE_FOR) return '';
      return v.id || '';
    };

    if(o.dryRun){
      /* WHAT THE PREVIEW PROMISES, THE SWEEP MUST BE WILLING TO DO. A planned download whose bytes
       * the store has already answered 404 for is one the sweep will (rightly) decline — and a
       * preview that counts it as an ordinary change writes a cheque the sweep then quietly
       * declines to cash: "Would download 200" over a sweep that ends "in step". Counted with the
       * RAW memory (a preview is a look, not the pressed-button retry a manual sweep is). */
      report.plannedGone = plan.fetch.concat(plan.keepBoth).filter(d => {
        const id = idOf(d.entry);
        return id && skipId(skipFetch[d.path], true) === id;
      }).length;
      return report;
    }

    /* 5. Do it. The journal is the record of what has landed, and it is what makes this resumable. */
    const journal = new Journal(io, key, index, o);
    const mine = {};                                  // what THIS device will publish
    for(const p in index) mine[p] = strip(index[p]);
    journal.beforeSave = () => publish(io, key, mine, report);

    const record = (path, entry, local) => {
      const next = Object.assign({}, entry);
      if(local) next.local = local; else delete next.local;
      index[path] = next;
      mine[path] = strip(next);
      journal.touch();
    };

    // Deletions first: they are a rename into .pc-trash, they cost nothing, and queued behind hours
    // of transfer they are simply never reached.
    let ti = 0;
    for(const t of plan.trash){
      if(stopping()) return await halt(report, journal);
      step('to trash', t.path, ++ti, plan.trash.length);
      try{
        const to = await fs.trash(o.id, t.path, now);
        record(t.path, { v: t.v, by: (t.entry && t.entry.by) || me,
                         deletedAt: (t.entry && t.entry.deletedAt) || now }, null);
        report.trashed.push({ path: t.path, to });
      }catch(e){ failed(report, t.path, 'delete', e); }
      await journal.maybe();
    }

    // Conflicts: fetch the incoming copy, then rename ours out of its way.
    let ci = 0;
    for(const c of plan.keepBoth){
      if(stopping()) return await halt(report, journal);
      /* A CONFLICT AGAINST BYTES THAT DO NOT EXIST IS NOT A CONFLICT — it is one copy, and it is
       * the one on this disk.
       *
       * Two sides disagree, so both are kept: that rule assumes the incoming side can actually be
       * fetched. When it cannot — the store does not have those bytes, or this device has already
       * failed to verify that exact copy — resolving it "keeps both" by renaming the local file out
       * of the way and then failing to write anything in its place. The path is now missing, so the
       * next sweep reads it as new elsewhere, fetches nothing again, and the sweep after that makes
       * ANOTHER conflict copy. Measured: 1,803 conflicts, then 2,322, climbing every sweep, with
       * 11,000 failed fetches in ten minutes.
       *
       * So an unfetchable incoming copy leaves the local file exactly where it is and says so. The
       * moment somebody publishes bytes that exist, it settles normally. */
      const cid = (c.entry && (c.entry.csum || c.entry.sha
                  || (c.entry.chunks && c.entry.chunks.join(',')))) || '';
      if(cid && skipId(skipFetch[c.path]) === cid){
        report.unfetchable = report.unfetchable || [];
        report.unfetchable.push({ path: c.path, why: 'the incoming copy cannot be fetched, so your '
                                  + 'copy was left exactly as it is' });
        continue;
      }
      step('conflict', c.path, ++ci, plan.keepBoth.length);
      try{
        /* FETCH FIRST, RENAME SECOND. Both orders are recoverable and only one is quiet.
         *
         * Renaming first leaves a window where the local copy sits under the conflict name and the
         * real name holds nothing — and if the fetch then fails (a lost blob, a dropped link) that
         * is where it stays, until somebody notices a file has vanished and a strangely-named one
         * has appeared. Fetching first writes to the `.part` file, which is invisible to everything
         * else, so nothing moves until there is something to put in its place. */
        const st = await receive(fs, io, o, c.path, c.entry, () => {},
                                 () => fs.move(o.id, c.path, c.keepAs));
        record(c.path, Object.assign({}, c.entry), { size: st.size, mtime: st.mtime,
                                                     csum: c.entry.csum });
        report.conflicted.push({ path: c.path, keptAs: c.keepAs });
      }catch(e){
        /* Remembered exactly as a download failure is, and for the same reason: a copy that cannot
         * be fetched must not be attempted again every sweep — and here each attempt used to cost a
         * renamed file as well as a round trip. */
        const why = msg(e);
        if(cid && /checksum mismatch/.test(why)){
          report.badFetch = report.badFetch || {};
          report.badFetch[c.path] = { id: cid, why: 'checksum' };
          failed(report, c.path, 'conflict', e);
        } else if(cid && /unavailable \(404\)/.test(why)){
          // The same rule as the download loop below: a 404 is a fact about the store, reported as
          // "can't be fetched", never as this sweep failing — see the comment there.
          report.badFetch = report.badFetch || {};
          report.badFetch[c.path] = { id: cid, why: 'gone', at: now0() };
          report.unfetchable = report.unfetchable || [];
          report.unfetchable.push({ path: c.path, why: 'the incoming copy is not in the store — your '
                                    + 'copy was left exactly as it is' });
          report.ok = false;
        } else {
          failed(report, c.path, 'conflict', e);
        }
      }
      await journal.maybe();
    }

    /* (declared above the conflict loop, which consults it too) A COPY THAT HAS ALREADY FAILED ITS
     * CHECKSUM IS NOT WORTH FETCHING AGAIN, EVER.
     *
     * The bytes are content-addressed, so reassembling them is deterministic: the same stored copy
     * produces the same wrong hash every time. Retrying it is an infinite loop that moves real bytes
     * over somebody's connection — reported exactly that way, the same two videos failing on every
     * sweep, all evening.
     *
     * Keyed on the IDENTITY of the copy, not the path, so it lifts by itself the moment the holder
     * publishes a different one: nothing to clear, no state to go stale. DROPPING THE ENTRY WOULD BE
     * THE OBVIOUS REPAIR AND IS A CATASTROPHE — the device that HAS the file would read the gap as
     * "deleted elsewhere" and trash its only good copy. */
    // Downloads.
    await transfers(plan.fetch, o, stopping, journal, LANES,
      (d) => !!(d.entry && d.entry.chunks && d.entry.chunks.length)
             || ((d.entry && d.entry.size) || 0) > PARALLEL_MAX,
      async (d, i, n) => {
        /* An entry already published without an address cannot be fetched by anybody. Reported
         * rather than attempted, so the sweep stops failing on it every time and the card names the
         * file — which is the only way somebody can go and fix it. */
        const e = d.entry || {};
        if(!e.sha && !(e.chunks && e.chunks.length)){
          report.unfetchable = report.unfetchable || [];
          report.unfetchable.push({ path: d.path, why: 'the shared record does not say where this '
                                    + 'file is stored — delete it in Files → Synced folders and add '
                                    + 'it again from the device that has it' });
          return;
        }
        const badId = skipId(skipFetch[d.path]);
        if(badId && badId === idOf(d.entry)){
          report.unfetchable = report.unfetchable || [];
          report.unfetchable.push({ path: d.path, why: 'the copy in the store fails its checksum — '
                                    + 'the device that has this file must send it again' });
          return;
        }
        step('downloading', d.path, i, n);
        try{
          const st = await receive(fs, io, o, d.path, d.entry,
                                   (pc) => step('downloading', d.path + ' ' + pc + '%', i, n));
          record(d.path, Object.assign({}, d.entry), { size: st.size, mtime: st.mtime,
                                                       csum: d.entry.csum });
          report.downloaded.push(d.path);
        }catch(e){
          /* …and REMEMBER it, so the next sweep does not spend the same bytes on the same failure.
           *
           * TWO WAYS A COPY CAN BE UNUSABLE, and both are properties of the COPY rather than of the
           * moment: bytes that fail their checksum, and bytes the store does not have. The second
           * was retried on every sweep for ever — 243 failures a sweep, on a folder where nothing
           * was wrong except that those blobs are gone — because a 404 read as an ordinary error.
           *
           * Keyed on the copy's identity either way, so it clears itself the moment somebody
           * publishes a different one; and a 5xx or a dead socket is NOT remembered, because those
           * really are about the moment. */
          /* TWO KINDS OF UNUSABLE COPY, AND ONLY ONE OF THEM IS PERMANENT.
           *
           * A checksum failure is deterministic: the same stored bytes produce the same wrong hash
           * for ever, and the block lifts by itself when somebody publishes a DIFFERENT copy —
           * different bytes, different identity.
           *
           * A 404 is not like that at all. Blobs are content-addressed, so bytes restored by any
           * route — a re-upload, another device adding the same file, a backup — come back under the
           * SAME identity, and a block keyed on identity would never lift: one bad minute from a
           * media server would strand that path for ever. So it is remembered with a clock, and
           * expires. */
          const why = msg(e);
          if(/checksum mismatch/.test(why)){
            report.badFetch = report.badFetch || {};
            report.badFetch[d.path] = { id: idOf(d.entry), why: 'checksum' };
            failed(report, d.path, 'download', e);
          } else if(/unavailable \(404\)/.test(why)){
            /* A 404 IS A FACT ABOUT THE STORE, NOT A FAILURE OF THIS SWEEP — and the label decides
             * what a person does next. "231 failed" reads as breakage and invites pressing Sync now
             * again, which (a pressed button means try again) refetches all 231 and prints "231
             * failed" again: a loop of despair someone sat inside for a day. "231 can't be fetched —
             * the store doesn't have those bytes" is the truth, points at the device that can fix it
             * (Verify, on the device that holds the files), and stops indicting the sweep. */
            report.badFetch = report.badFetch || {};
            report.badFetch[d.path] = { id: idOf(d.entry), why: 'gone', at: now0() };
            report.unfetchable = report.unfetchable || [];
            report.unfetchable.push({ path: d.path, why: 'the store does not have these bytes — run '
                                      + 'Verify on the device that has this file to send it again' });
            report.ok = false;
          } else {
            failed(report, d.path, 'download', e);
          }
        }
      });

    // Uploads.
    let published = 0;
    await transfers(plan.send, o, stopping, journal, LANES,
      (u) => (u.stat && u.stat.size || 0) > PARALLEL_MAX,
      async (u, i, n) => {
        step('uploading', u.path, i, n);
        try{
          const entry = await send(fs, io, o, u, me,
                                   (pc) => step('uploading', u.path + ' ' + pc + '%', i, n));
          record(u.path, entry, { size: u.stat.size, mtime: u.stat.mtime, csum: entry.csum });
          report.uploaded.push(u.path);
          /* COUNTED SEPARATELY, because it is not an ordinary upload: it puts back a file another
           * device deliberately deleted. Reported as "3,930 up" once, on a sweep that had just
           * reversed a delete — a number that says nothing about what happened. */
          if(u.resurrect) (report.resurrected = report.resurrected || []).push(u.path);
          if(entry.existed) report.alreadyStored = (report.alreadyStored || 0) + 1;
          if(++published >= PUBLISH_EVERY){ published = 0; await publish(io, key, mine, report); }
        }catch(e){ failed(report, u.path, 'upload', e); }
      });

    // Deletions this device is announcing, and the agreements that need no bytes.
    for(const t of plan.tombstone){
      if(stopping()) break;
      record(t.path, { v: t.v, by: me, deletedAt: now }, null);
      report.removedRemote.push(t.path);
    }
    for(const s of plan.settle){
      const local = disk[s.path] ? { size: disk[s.path].size, mtime: disk[s.path].mtime,
                                     csum: disk[s.path].csum } : null;
      record(s.path, Object.assign({}, s.entry), local);
    }

    /* 6. Tell the other devices, and record what we agreed. The publish comes first: a journal that
     *    runs ahead of what we have published would make this device believe in an agreement nobody
     *    else has seen.
     *
     * AND IF OUR OWN DOCUMENT HAS GONE, PUT IT BACK — even on a sweep that changed nothing.
     *
     * `mine` is rebuilt from the journal at the top of every sweep, so it always holds everything
     * this device knows. Without this, a device whose document was lost or emptied would only
     * restore it the next time a file happened to change: until then its paths are missing from the
     * merge, and a path nobody claims is a path no joining device can fetch. The cost of being wrong
     * here is one document write. */
    /* STRUCTURAL, NOT A COUNT. "Same number of paths" cannot see an EDIT — a sweep that uploaded
     * a changed file, checkpointed, and arrived here with a clean journal used to skip the publish
     * on the strength of matching counts, and the edit was never announced. Entry for entry: the
     * version and the content have to match what the relay already holds, or we publish. */
    step('saving');
    if(journal.dirty){
      await journal.flush();                             // publishes first, then saves — one unit
    } else if(!viewEquals(mine, views[me] || {})){
      await publish(io, key, mine, report);              // nothing new applied, but the relay is behind
    } else {
      report.published = report.published || 0;          // nothing to say, and nothing missing
    }
    await journal.flush();
    if(journal.checkpointError) report.checkpointError = journal.checkpointError;

    if(stopping()) report.stopped = true;
    /* AN UNRESOLVED PATH IS NOT A CLEAN SWEEP.
     *
     * A skipped conflict adds nothing to `failed`, so the sweep reported success, the card said "in
     * step", and the caller stamped the clock — while a divergence sat unresolved AND the locally
     * edited copy went unpublished. Silence about that is exactly the shape this feature keeps
     * getting wrong. */
    report.ok = report.failed.length === 0 && !(report.unfetchable || []).length;
    return report;
  }

  /* ---- pieces ---------------------------------------------------------------------------------- */

  const msg = (e) => (e && e.message) || String(e);

  /** Entry for entry: does what we would publish match what the relay already holds? */
  function viewEquals(a, b){
    const ka = Object.keys(a), kb = Object.keys(b);
    if(ka.length !== kb.length) return false;
    for(const p of ka){
      const x = a[p], y = b[p];
      if(!y) return false;
      if(E.versionOf(x) !== E.versionOf(y)) return false;
      if(!P.same(x, y)) return false;
    }
    return true;
  }
  const strip = (e) => { const c = Object.assign({}, e); delete c.local; return c; };

  function failed(report, path, what, e){
    report.failed.push({ path, what, error: msg(e) });
    report.ok = false;
  }

  async function halt(report, journal){
    report.stopped = true;
    if(journal) await journal.flush();
    return report;
  }

  function chunkAbove(fs, o){
    return (o && o.chunkAbove) || (fs && fs.chunkBytes) || (16 * 1024 * 1024);
  }

  /* The folder, a page at a time, as one compact map.
   *
   * PAGED BECAUSE OF THE BRIDGE, not because of the engine: a scan crosses the Capacitor bridge as
   * one JSON string, and a whole Pictures folder in one call is what killed the WebView's renderer.
   * What is kept is three numbers per path. */
  async function scan(fs, o, stopping){
    const disk = {};
    /* WHAT THE SCAN COULD NOT READ IS NOT ABSENT — IT IS UNKNOWN, and the difference is a deleted
     * subtree. A folder the walk could not enter (permissions, a disk hiccup, an antivirus holding
     * it) leaves its files out of `disk`; read as "deleted locally" they are tombstoned to every
     * device. Measured: five files under one unreadable folder → five tombstones, guards silent
     * (below the mass floor). So the skipped paths travel with the result, and the sweep treats
     * them exactly like exclusions: dropped from all three inputs, deletable by no one. */
    const unread = [];
    const note = (r) => { for(const k of (r && r.skipped) || []){
      const p = (k && k.path) || k; if(p) unread.push(String(p)); } };
    const so = { hash: !!o.hash, excludes: o.excludes || [], maxBytes: 0 };
    if(typeof fs.scanPage !== 'function'){
      const r = await fs.scan(o.id, so);
      for(const p in (r && r.files) || {}) disk[p] = compact(r.files[p]);
      note(r);
      return { disk, unread };
    }
    let off = 0;
    for(;;){
      if(stopping()) return { disk, unread };
      const page = await fs.scanPage(o.id, so, off, SCAN_PAGE);
      const files = (page && page.files) || {};
      const n = Object.keys(files).length;
      for(const p in files) disk[p] = compact(files[p]);
      note(page);
      off += n;
      if(!n || !page || page.done) break;
    }
    return { disk, unread };
  }
  const compact = (f) => ({ size: (f && f.size) || 0, mtime: (f && f.mtime) || 0,
                            csum: (f && (f.csum || f.sha)) || undefined });

  /* Small files several at a time, large ones one at a time.
   *
   * A transfer is mostly waiting for a round trip, so serialising them leaves the connection idle —
   * on six thousand photos that is the difference between minutes and an hour. A LARGE file holds a
   * chunk of plaintext and a chunk of ciphertext while it runs, so those go alone: the ceiling here
   * is memory, not the network. */
  async function transfers(items, o, stopping, journal, lanes, isBig, run){
    const small = items.filter(x => !isBig(x)), big = items.filter(isBig);
    const n = items.length;
    let done = 0, next = 0;
    const lane = async () => {
      while(next < small.length && !stopping()){
        const item = small[next++];
        await run(item, ++done, n);
        await journal.maybe();
      }
    };
    await Promise.all(Array.from({ length: Math.max(1, Math.min(lanes, small.length)) }, lane));
    for(const item of big){
      if(stopping()) break;
      await run(item, ++done, n);
      await journal.maybe();
    }
  }

  /* A download: into a temp file, hashed, and only then renamed over the real name.
   *
   * A partial temp file left by an interrupted sweep is REUSED only where the result can be checked
   * — a part file is tied to nothing but its length, so resuming onto one left by a different
   * version of the same path splices two files together, and the checksum is the only thing that
   * would catch it. Without a checksum it starts again: a slower download is not a bug, a silently
   * wrong file is. */
  /** `beforeCommit` runs once the bytes are safely in the part file and before it takes the name —
   *  the one moment a conflict may move the local copy aside. */
  async function receive(fs, io, o, path, entry, onPercent, beforeCommit){
    const chunks = entry.chunks || null;
    if(chunks && chunks.length && io.getParts && typeof fs.writePart === 'function'){
      const canVerify = !!(entry.csum && typeof fs.hashPart === 'function');
      let have = 0;
      if(canVerify){ try{ if(fs.partSize) have = await fs.partSize(o.id, path); }catch(_){ have = 0; } }
      else { try{ if(fs.discardPart) await fs.discardPart(o.id, path); }catch(_){} }
      const total = entry.size || 0;
      const pull = async (from) => {
        let got = from;
        await io.getParts(chunks, (off, bytes) => {
          got = Math.max(got, off + ((bytes && bytes.length) || 0));
          if(total && onPercent) onPercent(Math.round(got / total * 100));
          return fs.writePart(o.id, path, off, bytes);
        }, entry.size, from, entry.cs || 0);
      };
      await pull(have);
      try{
        await verifyPart(fs, o, path, entry);
      }catch(e){
        /* A RESUMED DOWNLOAD THAT FAILS ITS CHECKSUM IS THE PART FILE'S FAULT UNTIL PROVEN OTHERWISE.
         *
         * A part file is tied to nothing but its length, so one left by an EARLIER version of the
         * same path resumes into a splice of two files — and the checksum, correctly, refuses it.
         * Blaming the stored copy there is worse than the corruption: the caller remembers that copy
         * as bad and never fetches it again, so a perfectly good file becomes permanently
         * unfetchable on this device because of a stale temp file we wrote ourselves.
         *
         * So: throw the part file away and fetch the whole thing. Only a from-scratch download that
         * still fails is evidence about the copy. */
        if(!have) throw e;
        try{ if(typeof fs.discardPart === 'function') await fs.discardPart(o.id, path); }catch(_){}
        await pull(0);
        await verifyPart(fs, o, path, entry);
      }
      if(beforeCommit) await beforeCommit();
      return await fs.writeCommit(o.id, path, entry.mtime || 0);
    }
    if(chunks && chunks.length) throw new Error('this device cannot receive a file that large');
    const bytes = await io.getBlob(entry.sha);
    if(entry.csum && io.hashBytes){
      const got = await io.hashBytes(bytes);
      if(got !== entry.csum) throw new Error('checksum mismatch after download — refusing to write it');
    }
    if(beforeCommit) await beforeCommit();
    return await fs.write(o.id, path, bytes, entry.mtime || 0);
  }

  async function verifyPart(fs, o, path, entry){
    if(!entry.csum || typeof fs.hashPart !== 'function') return;
    const got = await fs.hashPart(o.id, path);
    if(got !== entry.csum) throw new Error('checksum mismatch after download — refusing to write it');
  }

  /* An upload, and the entry the other devices will read.
   *
   * `csum` is the FILE's own hash and is always recorded: it is what a download is checked against,
   * what tells a restored backup from an edit, and what lets two devices holding the same file agree
   * without transferring it. `sha` (or the chunk list) is where the encrypted bytes live — a
   * different number entirely, and comparing one against the other is how a folder duplicates
   * itself. */
  async function send(fs, io, o, u, me, onPercent){
    const path = u.path, size = (u.stat && u.stat.size) || 0;
    const entry = { v: u.v, by: me, size, mtime: (u.stat && u.stat.mtime) || 0 };
    if(size > chunkAbove(fs, o) && io.putParts && typeof fs.readPart === 'function'){
      /* THE CALLER'S FIGURE FIRST. It is the platform's chunk clamped to what the NODE accepts, and a
       * chunk IS one upload — reading `fs.chunkBytes` here ignored the clamp entirely, so a node with
       * a small limit chunked at the right threshold and then sent pieces it would still reject.
       * Every large file fails while small ones sail through, which is the confusing half. */
      /* THE CHECKSUM MUST CERTIFY THE CHUNKS, and they used to be taken at different moments.
       *
       * The chunks are read over minutes; the csum came from the scan (minutes earlier) or from
       * hashing the file AFTER the last chunk (minutes later). A file edited anywhere inside that
       * window published chunks of a TORN file under a clean checksum of the final one — and then
       * every device that downloads it fails its checksum for ever, while the uploader's own journal
       * says all is well, because its local file really does hash to the published csum. Documents
       * are the folder people edit, which is where it was reported.
       *
       * So the file is hashed on BOTH sides of the chunk reads. Equal means it sat still for the
       * whole window and the csum certifies what was stored; different means the store holds a torn
       * copy, so nothing is recorded and the next sweep takes the file fresh. The scan's own hash
       * (when it ran) serves as the before-side for free. */
      const canHash = typeof fs.hashFile === 'function';
      const before = (u.stat && u.stat.csum) || (canHash ? await fs.hashFile(o.id, path) : undefined);
      const cs = (o.chunkBytes || 0) || (fs.chunkBytes || 0) || undefined;
      const r = await io.putParts((off, len) => fs.readPart(o.id, path, off, len), size,
                                  (doneB, totalB) => { if(onPercent && totalB)
                                                         onPercent(Math.round(doneB / totalB * 100)); },
                                  cs);
      entry.chunks = (r && (r.chunks || r.parts)) || [];
      entry.cs = (r && r.cs) || cs || 0;
      if(canHash && before){
        const after = await fs.hashFile(o.id, path);
        if(after !== before){
          throw new Error('the file changed while it was being uploaded — nothing was recorded; '
                          + 'it will be picked up next sweep');
        }
        entry.csum = after;
      } else {
        entry.csum = before;
      }
      if(!entry.csum) delete entry.csum;
      return addressed(entry, path);
    }
    /* A WHOLE-FILE READ IS EXACTLY THE FILE, OR IT IS A FAILURE — the same rule `_exactPart` applies
     * to a chunk, on the path that did not have it.
     *
     * This is the worse half of that bug, because a truncation here is SELF-CONSISTENT: the short
     * buffer is what gets hashed, so the entry's checksum matches the truncation, the receiving
     * device verifies it happily and writes a short file, and every check afterwards agrees. Nothing
     * would ever notice.
     *
     * A length that does not match the scan also happens when somebody edits the file WHILE the
     * sweep is reading it, and the right answer is the same either way: do not store this, report it,
     * pick it up next sweep. Storing a half-written file under a checksum that certifies it is the
     * one outcome there is no recovering from. */
    const bytes = await fs.read(o.id, path);
    const got = (bytes && bytes.length) || 0;
    if(got !== size){
      throw new Error('read ' + got + ' bytes of ' + size + ' — the file changed while it was being '
                      + 'read, or the read came back short; it will be picked up next sweep');
    }
    entry.csum = io.hashBytes ? await io.hashBytes(bytes) : undefined;
    const put = await io.putBlob(bytes);
    entry.sha = (put && typeof put === 'object') ? put.sha : put;
    if(put && put.existed) entry.existed = true;
    if(!entry.csum) delete entry.csum;
    return addressed(entry, path);
  }

  /* AN ENTRY THAT NAMES NO BYTES IS NOT A FILE.
   *
   * A live entry with neither a `sha` nor a chunk list says "this file exists" and does not say
   * where — so every other device plans a download, fetches nothing, and fails, for ever, while the
   * file browser answers "this file has no stored copy". Reported exactly that way.
   *
   * Publishing one is the bug; this is the last place before it goes out. It is cheap, it cannot
   * fire for a tombstone, and the alternative is a folder that can never settle. */
  function addressed(entry, path){
    const has = entry && (entry.sha || (entry.chunks && entry.chunks.length));
    if(entry && !entry.deletedAt && !has){
      throw new Error('the upload finished without an address for ' + path
                      + ' — refusing to record a file nothing can fetch');
    }
    return entry;
  }

  /* Our own document, and nothing else's. No merge, no re-read, no compare-and-swap: this is the one
   * writer this document will ever have. */
  async function publish(io, key, mine, report){
    await io.publish(key, mine);
    report.published = (report.published || 0) + 1;
  }

  /* The journal, written in batches.
   *
   * Per file would be correct and far too expensive; per sweep is what made an interrupted first
   * sync start from the beginning. In batches, an interruption costs at most the last few files —
   * and redoing one is nearly free: an upload whose bytes the server already has is skipped, and a
   * download already on disk hashes equal and settles. */
  /* THE VIEW GOES FIRST, ALWAYS — the rule the Android Journal already had and this one did not.
   *
   * A journal persisted ahead of the published view is a device that believes in an agreement no
   * other device has seen, and the next sweep reads its own stale view as "the folder changed":
   * that fetched old bytes back over an edit and resurrected deliberate deletions. So every save of
   * the journal publishes the view in the same breath, view first — a crash between the two leaves
   * the VIEW ahead, which is safe (the next sweep settles it by content). `beforeSave` is that
   * publish; the sweep wires it in before the first byte moves.
   *
   * A failed CHECKPOINT is not a failed sweep — the work is real either way, so `maybe` records the
   * error and carries on; only the final `flush` throws. */
  function Journal(io, key, index, o){
    this.io = io; this.key = key; this.index = index; this.o = o;
    this.dirty = false; this.since = 0; this.at = now0();
    this.beforeSave = null; this.checkpointError = null;
  }
  Journal.prototype.touch = function(){ this.dirty = true; this.since++; };
  Journal.prototype.maybe = async function(){
    if(!this.dirty) return;
    if(this.since < SAVE_EVERY && (now0() - this.at) < SAVE_MS) return;
    try{ await this.flush(); }
    catch(e){ this.checkpointError = msg(e); this.at = now0(); }
  };
  Journal.prototype.flush = async function(){
    if(!this.dirty) return;
    this.since = 0; this.at = now0();
    if(this.beforeSave) await this.beforeSave();
    await this.io.saveIndex(this.key, this.index);
    this.dirty = false;
  };

  /* ---- the consistency check -------------------------------------------------------------------
   *
   * Read-only, and it answers the question the sweep cannot: is what I have actually what the folder
   * says it is? It re-hashes the files on this disk against the merged view, asks the server whether
   * the bytes behind every entry are still there, and compares the devices' views with each other.
   * Nothing is written, nothing is deleted, nothing is fetched.
   */
  async function verify(fs, io, opts){
    const o = opts || {};
    const key = o.key || o.id;
    const tick = (p) => { try{ if(typeof o.onProgress === 'function') o.onProgress(p); }catch(_){} };
    const out = { corrupt:[], missingHere:[], missingBytes:[], extra:[], unverified:[],
                  checked:0, devices:[], disagree:[] };

    const got = await io.views(key);
    const views = (got && got.views) || {};
    out.missingViews = (got && got.missing) || 0;
    out.cannot = (got && got.cannot) || [];
    const merged = E.merge(views);
    out.devices = merged.devices;
    for(const p in merged.rivals) out.disagree.push(p);

    tick({ phase: 'scanning' });
    const disk = (await scan(fs, Object.assign({}, o, { hash: false }), () => false)).disk;

    const paths = Object.keys(merged.global).filter(p => merged.global[p] && !merged.global[p].deletedAt);
    let i = 0;
    for(const p of paths){
      const entry = merged.global[p];
      tick({ phase: 'checking', path: p, i: ++i, n: paths.length });
      if(!entry.sha && !(entry.chunks && entry.chunks.length)){
        out.unaddressed = out.unaddressed || [];
        out.unaddressed.push(p);
        continue;
      }
      const L = disk[p];
      if(!L){ out.missingHere.push(p); continue; }
      if(L.size !== entry.size){ out.corrupt.push({ path: p, why: 'wrong size' }); continue; }
      if(!entry.csum || typeof fs.hashFile !== 'function'){ out.unverified.push(p); continue; }
      let h = null;
      try{ h = await fs.hashFile(o.id, p); }catch(e){ out.unverified.push(p); continue; }
      out.checked++;
      if(h !== entry.csum) out.corrupt.push({ path: p, why: 'the bytes on this device do not match' });
    }
    for(const p in disk) if(!merged.global[p] || merged.global[p].deletedAt) out.extra.push(p);

    /* Are the bytes still on the server? Only asked of entries this device cannot prove locally,
     * and only when the store can answer a cheap existence question. */
    if(typeof io.hasBlob === 'function' && o.deep){
      const want = paths.slice(0, o.blobLimit || 5000);
      let j = 0;
      for(const p of want){
        const e = merged.global[p];
        const ids = e.chunks && e.chunks.length ? e.chunks : (e.sha ? [e.sha] : []);
        tick({ phase: 'checking the store', path: p, i: ++j, n: want.length });
        for(const id of ids){
          let there = null;
          try{ there = await io.hasBlob(id); }catch(_){ there = null; }
          // `null` is "I could not ask" — never "missing". This list drives a repair that publishes
          // deletions, and a rate limiter answering 429 a thousand times in a row is the expected
          // case here, not the exotic one.
          if(there === false){ out.missingBytes.push(p); break; }
          if(there === null){ out.unverified.push(p); break; }
        }
      }
    }
    return out;
  }

  const API = { sweep, verify, scan, SCAN_PAGE, LANES };
  root.PCSyncExec = API;
  if(typeof module !== 'undefined' && module.exports) module.exports = API;
})(typeof globalThis !== 'undefined' ? globalThis : this);
