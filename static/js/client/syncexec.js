/* Folder sync — the executor. It moves the bytes the engine decided on, and nothing else.
 *
 * The split is the safety: syncstate.js decides and cannot touch a file; this decides nothing and
 * cannot be reached without a plan that has been checked. Every rule about WHAT should happen lives
 * over there, where it is pure and can be run exhaustively; every rule about HOW to do it without
 * losing anything lives here.
 *
 * WHAT THIS FILE IS RESPONSIBLE FOR, and each line was paid for by a real failure:
 *
 *   AN INPUT IT COULD NOT READ IS NEVER A VALUE. Not the journal, not the record set, not the
 *   scan. A read that fails throws, and nothing has been changed. Empty and unreadable were once
 *   the same thing here, and that is what put a Pictures folder in the trash.
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
 *   IT PUBLISHES PER FILE, THROUGH THE SERVER'S COMPARE-AND-SWAP. A change this sweep made is one
 *   record, refused individually if another device got there first — there is no document to merge,
 *   re-read or collapse, and a refused write costs one conflict resolution, never a folder.
 */
(function(root){
  'use strict';

  const E = root.PCSyncState || (typeof require === 'function' ? require('./syncstate.js') : null);
  const P = root.PCFolderSync || (typeof require === 'function' ? require('./foldersync.js') : null);
  if(!E || !P) throw new Error('syncexec.js needs syncstate.js and foldersync.js');

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
  const SAVE_EVERY = 200;          // files between journal writes (each write publishes first)
  const SAVE_MS = 20 * 1000;

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
    /* HEAP TELEMETRY, because this is the second "windows app runs out of memory while syncing"
     * and the first was fixed by guessing right (PARALLEL_MAX). Chromium exposes usedJSHeapSize;
     * the sweep samples it at every step and the report carries the peak — so the next crash report
     * comes with a number and a phase attached instead of a feeling. */
    let _peakHeap = 0, _peakPhase = '';
    const _heap = () => { try{ return (performance && performance.memory && performance.memory.usedJSHeapSize) || 0; }catch(_){ return 0; } };
    const step = (phase, path, i, n) => {
      _keepAwake();
      const h = _heap();
      if(h > _peakHeap){ _peakHeap = h; _peakPhase = phase; }
      tick({ phase, path, i, n });
    };

    const report = { uploaded:[], downloaded:[], trashed:[], conflicted:[], removedRemote:[],
                     failed:[], skipped:[], unchanged:0, settledGone:0, excluded:0, refused:[], device: me,
                     dryRun: !!o.dryRun, ok: true };

    /* 1. The folder: ONE RECORD PER FILE, read strictly. There are no per-device views to merge
     *    and no view that can be partial — the transport either hands back the record set (its
     *    cache plus everything written since its last look) or it throws, and nothing has been
     *    changed. A record that could not be decrypted is COUNTED and its path left untouched:
     *    the safe direction for one unreadable record is one file the sweep does not move. */
    let got0;
    try{ got0 = await io.state(key); }
    catch(e){
      throw new Error('could not read the folder’s shared record — nothing has been changed. ('
                      + msg(e) + ')');
    }
    const state = (got0 && got0.state) || {};
    report.undecryptable = (got0 && got0.undecryptable) || 0;
    { const devs = new Set();
      for(const p in state) if(state[p] && state[p].by) devs.add(state[p].by);
      devs.add(me);
      report.devices = devs.size; }

    /* 2. What this device has already applied. A journal that cannot be read is not an empty
     *    journal: with no journal every file on both sides looks new and independently changed,
     *    which is a conflict copy per path — thousands of them, and a folder to repair by hand. */
    let index;
    try{ index = (await io.index(key)) || {}; }
    catch(e){ throw new Error('could not read this device’s sync record — nothing has been changed. ('
                              + msg(e) + ')'); }

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
     * has applied nothing — so the engine falls through to "did they end up the same anyway?",
     * and that question is answered by content when there is a checksum and by size+mtime when there
     * is not. An ordinary sweep does not hash.
     *
     * On a phone, size+mtime CANNOT match: SAF assigns its own last-modified to everything it
     * writes, so a file the phone downloaded yesterday has a timestamp no other device ever saw.
     * Every path therefore reads as "edited on both" and the sweep makes a conflict copy of the
     * entire folder — reported as "phone is now downloading 1803 conflicts".
     *
     * So a device with nothing applied pays for one full hash and settles the folder by content.
     * It is expensive once; a conflict copy of every file is expensive for ever.
     *
     * HOW MUCH OF THIS FOLDER CAN THE JOURNAL ACTUALLY ANSWER FOR? "Empty" was too narrow: the
     * journal is written in batches, so a first sweep interrupted anywhere leaves a handful of
     * entries behind, and the next sweep then did not hash — the conflict storm returned by the
     * back door. The test is COVERAGE, not emptiness: below half, this device cannot answer for
     * its own folder and must settle it by content. A dry run hashes too, or Preview and the sweep
     * disagree by the whole folder — on precisely the device this exists for. */
    const known = Object.keys(index).length;
    const seen = Object.keys(state).length;
    const thin = known === 0 || (seen > 0 && known < seen / 2);
    const scanOpts = thin ? Object.assign({}, o, { hash: true }) : o;
    if(thin) report.hashed = true;

    /* 3. The disk, a page at a time. */
    step('scanning');
    /* A HEARTBEAT FOR THE MUTE SCAN. The desktop bridge reads the whole folder in one call, and a
     * hashing first sweep of a real Pictures folder is tens of gigabytes of disk work with nothing
     * to say until it returns — reported as "scanning and no progress", which after this feature's
     * history reads as a hang. The clock cannot show progress the bridge does not report, but it
     * can prove the sweep is alive and say what the wait is. */
    const _scanAt = now0();
    const _scanBeat = setInterval(() => {
      const min = Math.round((now0() - _scanAt) / 60000);
      tick({ phase: (thin ? 'reading every file (first sweep — the whole folder is re-checked '
                            + 'by content, which takes a while on photos and video)'
                          : 'scanning') + (min ? ' · ' + min + ' min in, still going' : '') });
    }, 5000);
    let disk, unread;
    let unreadWhy = [];
    try{ const got = await scan(fs, scanOpts, stopping, o.onProgress);
         disk = got.disk; unread = got.unread; unreadWhy = got.unreadWhy || []; }
    catch(e){
      clearInterval(_scanBeat);
      /* "unknown sync folder" is not a read error — it is this device no longer holding the
       * MAPPING for the folder (a cleared app profile, a reinstall). The files and the shared
       * record are fine; only the handle is gone, and re-picking the folder mints a new one. */
      if(/unknown sync folder/i.test(String((e && e.message) || e)))
        throw new Error('this device no longer remembers where this folder lives — press '
                      + '“Point at the folder again…” on its card (or remove and '
                      + 're-add it). Your files and the shared record are untouched');
      throw new Error('could not read the folder on this device — nothing has been changed. ('
                              + msg(e) + ')'); }
    clearInterval(_scanBeat);
    if(stopping()) return halt(report);
    report.scanned = Object.keys(disk).length;
    { const busy = unreadWhy.filter(x => /in use|vanished/i.test(x.why)).map(x => x.path);
      const denied = unread.filter(p => busy.indexOf(p) === -1);
      if(busy.length) report.busyNow = busy.slice(0, 200);
      if(denied.length) report.unreadable = denied.slice(0, 200); }

    /* 4. Decide, check, and let a person answer for anything that is theirs to answer.
     * Paths the scan could not read join the exclusions: dropped from all three inputs, so an
     * unreadable subtree can neither be deleted here nor tombstoned to anyone. */
    let plan = E.plan({ disk, state, index, device: me, now,
                        excludes: (o.excludes || []).concat(unread) });

    /* HEAL WHAT OTHERS REFUSE. A record another device has FLAGGED — its stored copy failed a
     * checksum on download — is re-sent by whoever still holds a good local copy: the fresh upload
     * carries a new storage address (a new random IV means new ciphertext), so every puller's
     * remembered refusal expires by itself. Verified BEFORE sending: re-seeding a copy that is also
     * bad here helps nobody, and is counted as `badHere` so the card can say which device really
     * lost the file. */
    let _heal = [];
    { const flagged = (got0 && got0.flagged) || {};
      if(!o.dryRun){
        for(const p in flagged){
          const e = index[p]; if(!e || e.deletedAt || !disk[p]) continue;
          if(idOf(e) !== flagged[p]) continue;          // already re-sent under a new address
          try{
            if(e.csum && typeof fs.hashFile === 'function'){
              const h = await fs.hashFile(o.id, p);
              if(h !== e.csum){ (report.badHere = report.badHere || []).push(p); continue; }
            }
            _heal.push(p);
          }catch(_){}
        }
        if(_heal.length) (report.reseeding = _heal.slice());
      } }

    /* PATHS THE CALLER DEMANDS BE SENT AGAIN.
     *
     * "The store lost these bytes and this device still has the file" cannot be expressed by editing
     * the journal: with the entry gone both sides read as changed, the engine asks whether they
     * are the same anyway, the checksums match — because it IS the same file — and it settles. So it
     * is said outright. Only paths this device actually holds, and the version goes past whatever
     * the folder shows so the record is not immediately refused. */
    const _resend = (o.resend || []).concat(_heal);
    if(_resend.length && !o.dryRun){
      const want = new Set(_resend);
      const already = new Set(plan.send.map(u => u.path));
      const extra = [];
      for(const p of want){
        if(already.has(p) || !disk[p]) continue;
        extra.push({ path: p, v: E.bump(state[p], index[p]), stat: disk[p],
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

    const verdicts = E.check(plan, { state, indexSize: Object.keys(index).length,
                                     caseFolds: fs.caseFolds !== false });
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
    /* What a remembered failure still applies to.
     *
     * `checksum` clears when the holder re-uploads — a new ciphertext is a NEW ADDRESS, which is
     * why refusals are keyed on the STORAGE address (sha, or the chunk list) and never on the
     * file's own csum: a re-send of the same good bytes keeps the csum and changes the address, and
     * a csum key would refuse the repair for ever. `gone` is a 404 and EXPIRES, because
     * content-addressed bytes that come back come back under the same identity, so an unexpiring
     * block would make one bad minute permanent. A pressed button clears both: somebody standing
     * there asking again is the clearest possible signal to try. */
    const GONE_FOR = 6 * 3600 * 1000;
    const skipId = (v, raw, entry) => {
      if(!v) return '';
      if(typeof v === 'string') return v;
      if(o.manual && !raw) return '';
      if(v.why === 'gone' && (now0() - (v.at || 0)) > GONE_FOR) return '';
      /* THE RECORD MOVED ON — RETRY NOW, not in six hours. A holder's automatic re-send bumps the
       * file's version; a memory that ignored that held the repaired copy at arm's length for the
       * rest of the window, which turned a self-heal into an afternoon. */
      if(entry && v.v != null && E.versionOf(entry) > v.v) return '';
      return v.id || '';
    };

    if(o.dryRun){
      /* WHAT THE PREVIEW PROMISES, THE SWEEP MUST BE WILLING TO DO. A planned download whose bytes
       * the store has already answered 404 for is one the sweep will (rightly) decline — and a
       * preview that counts it as an ordinary change writes a cheque the sweep then quietly
       * declines to cash. Counted with the RAW memory (a preview is a look, not the pressed-button
       * retry a manual sweep is). */
      report.plannedGone = plan.fetch.concat(plan.keepBoth).filter(d => {
        const id = idOf(d.entry);
        return id && skipId(skipFetch[d.path], true, d.entry) === id;
      }).length;
      return report;
    }

    /* 5. Do it. The journal is the record of what has landed, and it is what makes this resumable.
     *
     * A CHANGE THIS SWEEP MAKES IS PUBLISHED PER FILE, BEFORE THE JOURNAL RECORDS IT — the folder
     * running ahead of the journal is safe (the next sweep adopts its own publish by content),
     * where a journal ahead of the folder is a device believing in an agreement nobody saw. A
     * record the server REFUSES (another device won the version) is struck from the journal on the
     * spot: this device then honestly knows nothing about that path, and the next sweep resolves
     * the divergence as a conflict, with both copies surviving. */
    const journal = new Journal(io, key, index, o);
    const pending = [];                             // records this sweep must publish
    const flushPuts = async () => {
      if(!pending.length) return;
      const batch = pending.splice(0);
      const res = await io.putState(key, batch,
        { confirmed: allowed.indexOf('massTombstone') !== -1 || !!o.allowMassTrash });
      for(const p of (res && res.stale) || []){
        delete index[p];
        report.raced = (report.raced || 0) + 1;
      }
      for(const p of (res && res.failed) || []){
        failed(report, p, 'publish', new Error('the record was not stored — will retry next sweep'));
      }
    };
    journal.beforeSave = flushPuts;

    const record = (path, entry, local, publish) => {
      const next = Object.assign({}, entry);
      if(local) next.local = local; else delete next.local;
      index[path] = next;
      if(publish) pending.push({ path, entry: strip(next) });
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
        record(t.path, Object.assign({}, t.entry), null);
        report.trashed.push({ path: t.path, to });
      }catch(e){ failed(report, t.path, 'delete', e); }
      await journal.maybe();
    }

    // Conflicts: fetch the incoming copy, then rename ours out of its way.
    let ci = 0;
    for(const c of plan.keepBoth){
      if(stopping()) return await halt(report, journal);
      /* A CONFLICT AGAINST BYTES THAT DO NOT EXIST IS NOT A CONFLICT — it is one copy, and it is
       * the one on this disk. When the incoming side cannot be fetched, resolving it "keeps both"
       * by renaming the local file out of the way and then failing to write anything in its place;
       * the next sweep reads the gap as new-elsewhere and the sweep after that makes ANOTHER
       * conflict copy. Measured: 1,803 conflicts, then 2,322, climbing every sweep. An unfetchable
       * incoming copy leaves the local file exactly where it is and says so. */
      const cid = idOf(c.entry);
      if(cid && skipId(skipFetch[c.path], false, c.entry) === cid){
        report.unfetchable = report.unfetchable || [];
        report.unfetchable.push({ path: c.path, why: 'the incoming copy cannot be fetched, so your '
                                  + 'copy was left exactly as it is' });
        continue;
      }
      /* A CONFLICT OVER IDENTICAL BYTES IS NOT A CONFLICT. An unhashed scan compares size+mtime,
       * and two copies of the same photo can differ on both — so before renaming anything, the one
       * cheap question that settles it for real: hash the local file against the incoming record.
       * Equal means both sides hold the same content and the only divergence was a timestamp; the
       * journal records agreement and no copy is minted. This is also what absorbs the CAS race —
       * two devices uploading the same file, the loser refused, resolving here. */
      if(c.entry && c.entry.csum && typeof fs.hashFile === 'function'){
        let h = null;
        try{ h = await fs.hashFile(o.id, c.path); }catch(_){ h = null; }
        if(h && h === c.entry.csum){
          const L = disk[c.path] || {};
          record(c.path, Object.assign({}, c.entry), { size: L.size, mtime: L.mtime, csum: h });
          continue;
        }
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
                                 () => fs.move(o.id, c.path, c.keepAs), stopping);
        record(c.path, Object.assign({}, c.entry), { size: st.size, mtime: st.mtime,
                                                     csum: c.entry.csum });
        report.conflicted.push({ path: c.path, keptAs: c.keepAs });
      }catch(e){
        if(isStop(e)) return await halt(report, journal);
        /* Remembered exactly as a download failure is, and for the same reason: a copy that cannot
         * be fetched must not be attempted again every sweep — and here each attempt used to cost a
         * renamed file as well as a round trip. */
        const why = msg(e);
        if(cid && /checksum mismatch/.test(why)){
          report.badFetch = report.badFetch || {};
          report.badFetch[c.path] = { id: cid, why: 'checksum', v: E.versionOf(c.entry) };
          failed(report, c.path, 'conflict', e);
        } else if(cid && /unavailable \(404\)/.test(why)){
          report.badFetch = report.badFetch || {};
          report.badFetch[c.path] = { id: cid, why: 'gone', at: now0(), v: E.versionOf(c.entry) };
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

    // Downloads.
    await transfers(plan.fetch, o, stopping, journal, LANES,
      (d) => !!(d.entry && d.entry.chunks && d.entry.chunks.length)
             || ((d.entry && d.entry.size) || 0) > PARALLEL_MAX,
      async (d, i, n) => {
        /* A record already published without an address cannot be fetched by anybody. Reported
         * rather than attempted, so the sweep stops failing on it every time and the card names the
         * file — which is the only way somebody can go and fix it. */
        const e = d.entry || {};
        /* A NAME THIS PLATFORM CANNOT HOLD IS REFUSED UP FRONT, with the fix named. A Linux
         * device can create `notes:v2.txt`; Windows cannot write it, so the fetch failed on every
         * sweep for ever with a message about disks. The record is fine — the NAME needs a human
         * (renaming where it was created), and the card says exactly that. */
        if(fs.platform === 'win32' && _winBad(d.path)){
          report.unfetchable = report.unfetchable || [];
          report.unfetchable.push({ path: d.path, why: 'this name cannot exist on Windows — '
                                    + 'rename it on the device it was created on' });
          return;
        }
        if(!e.sha && !(e.chunks && e.chunks.length)){
          report.unfetchable = report.unfetchable || [];
          report.unfetchable.push({ path: d.path, why: 'the shared record does not say where this '
                                    + 'file is stored — delete it in Files → Synced folders and add '
                                    + 'it again from the device that has it' });
          return;
        }
        const badId = skipId(skipFetch[d.path], false, d.entry);
        if(badId && badId === idOf(d.entry)){
          report.unfetchable = report.unfetchable || [];
          report.unfetchable.push({ path: d.path, why: 'the copy in the store fails its checksum — '
                                    + 'the device that has this file will send it again' });
          return;
        }
        step('downloading', d.path, i, n);
        try{
          const st = await receive(fs, io, o, d.path, d.entry,
                                   (pc) => step('downloading', d.path + ' ' + pc + '%', i, n),
                                   undefined, stopping);
          record(d.path, Object.assign({}, d.entry), { size: st.size, mtime: st.mtime,
                                                       csum: d.entry.csum });
          report.downloaded.push(d.path);
        }catch(e){
          if(isStop(e)) return;               // the loop sees the latch and halts cleanly
          /* TWO KINDS OF UNUSABLE COPY, AND ONLY ONE OF THEM IS PERMANENT-UNTIL-REPAIRED.
           *
           * A checksum failure is deterministic: the same stored bytes produce the same wrong hash
           * for ever. It is remembered by STORAGE ADDRESS and — the other half of the repair — it
           * is FLAGGED on the record, so the device still holding a good copy re-sends it without
           * anyone asking; the fresh upload is a fresh address and the memory lifts by itself.
           *
           * A 404 is not like that at all. Blobs are content-addressed, so bytes restored by any
           * route come back under the SAME identity, and a block keyed on identity would never
           * lift: one bad minute from a media server would strand that path for ever. So it is
           * remembered with a clock, and expires. A 5xx or a dead socket is NOT remembered,
           * because those really are about the moment. */
          const why = msg(e);
          if(/checksum mismatch/.test(why)){
            report.badFetch = report.badFetch || {};
            report.badFetch[d.path] = { id: idOf(d.entry), why: 'checksum', v: E.versionOf(d.entry) };
            failed(report, d.path, 'download', e);
          } else if(/unavailable \(404\)/.test(why)){
            report.badFetch = report.badFetch || {};
            report.badFetch[d.path] = { id: idOf(d.entry), why: 'gone', at: now0(), v: E.versionOf(d.entry) };
            report.unfetchable = report.unfetchable || [];
            report.unfetchable.push({ path: d.path, why: 'the store does not have these bytes — run '
                                      + 'Verify on the device that has this file to send it again' });
            report.ok = false;
          } else {
            failed(report, d.path, 'download', e);
          }
        }
      });

    // Uploads. Each one publishes its record in the next checkpoint's flush.
    await transfers(plan.send, o, stopping, journal, LANES,
      (u) => (u.stat && u.stat.size || 0) > PARALLEL_MAX,
      async (u, i, n) => {
        step('uploading', u.path, i, n);
        try{
          const entry = await send(fs, io, o, u, me,
                                   (pc) => step('uploading', u.path + ' ' + pc + '%', i, n), stopping);
          record(u.path, entry, { size: u.stat.size, mtime: u.stat.mtime, csum: entry.csum }, true);
          report.uploaded.push(u.path);
          /* COUNTED SEPARATELY, because it is not an ordinary upload: it puts back a file another
           * device deliberately deleted. */
          if(u.resurrect) (report.resurrected = report.resurrected || []).push(u.path);
          if(entry.existed) report.alreadyStored = (report.alreadyStored || 0) + 1;
        }catch(e){
          if(isStop(e)) return;               // the loop sees the latch and halts cleanly
          failed(report, u.path, 'upload', e);
        }
      });

    // Deletions this device is announcing, and the agreements that need no bytes.
    /* A DELETION CLAIM NEEDS POSITIVE PROOF, never inference from a listing. The engine plans a
     * tombstone when the scan did not see a file the journal knows — but every way a scan fails to
     * SEE (an unmounted drive, a revoked grant, a flaky provider answering an empty listing) used
     * to become a published deletion on every device. Before anything is announced, the exact path
     * is probed: ENOENT with a healthy parent is a deletion; anything else is UNKNOWN, which
     * deletes nothing anywhere and says so on the card. A build whose fs cannot answer confirms
     * nothing — the safe direction for a stale shell. */
    for(const t of plan.tombstone){
      if(stopping()) break;
      let ev = null;
      try{ ev = fs.confirmGone ? await fs.confirmGone(o.id, t.path) : null; }catch(_){ ev = null; }
      if(!ev || ev.gone !== true){
        (report.unconfirmedAbsent = report.unconfirmedAbsent || []).push({ path: t.path,
          why: !ev ? 'this build cannot confirm deletions'
             : ev.parentAlive === false ? 'its folder could not be read'
             : 'the file is still there' });
        continue;
      }
      /* THE TOMBSTONE KEEPS THE FILE'S ADDRESS. A dead record that forgets its sha is a deletion
       * nobody can undo account-wide — the store still holds the bytes, but nothing remembers
       * which bytes. ~100 bytes per tombstone buys "Restore on every device" for as long as the
       * record lives. */
      const _prev = index[t.path] || state[t.path] || {};
      const _keep = {};
      for(const k of ['sha','csum','size','mtime','chunks','cs','ps'])
        if(_prev[k] !== undefined) _keep[k] = _prev[k];
      record(t.path, Object.assign(_keep, { v: t.v, by: me, deletedAt: now }), null, true);
      report.removedRemote.push(t.path);
    }
    for(const s of plan.settle){
      const local = disk[s.path] ? { size: disk[s.path].size, mtime: disk[s.path].mtime,
                                     csum: disk[s.path].csum } : null;
      record(s.path, Object.assign({}, s.entry), local);
    }

    /* 6. Publish what is still queued, then save the journal — in that order, always. */
    step('saving');
    await flushPuts();
    await journal.flush();
    if(journal.checkpointError) report.checkpointError = journal.checkpointError;

    if(stopping()) report.stopped = true;
    if(_peakHeap){ report.peakHeapMB = Math.round(_peakHeap / 1048576); report.peakHeapPhase = _peakPhase; }
    /* AN UNRESOLVED PATH IS NOT A CLEAN SWEEP. A skipped conflict adds nothing to `failed`, so the
     * sweep used to report success and the card said "in step" while a divergence sat unresolved.
     * Silence about that is exactly the shape this feature keeps getting wrong. */
    report.ok = report.failed.length === 0 && !(report.unfetchable || []).length;
    return report;
  }

  /* ---- pieces ---------------------------------------------------------------------------------- */

  const msg = (e) => (e && e.message) || String(e);
  const STOPPED = 'stopped by the user — will pick up exactly here next sweep';
  const isStop = (e) => msg(e).indexOf('stopped by the user') === 0;

  /* THE STORAGE ADDRESS IS THE IDENTITY a fetch-refusal is keyed on — sha first, then the chunk
   * list, and the csum only for a record so old it names no storage. Never csum-first: a holder
   * re-sending the same good bytes keeps the csum and changes the address, and a csum key would
   * refuse the repair for ever. */
  const idOf = (e) => (e && (e.sha || (e.chunks && e.chunks.length && e.chunks.join(','))
                             || e.csum)) || '';

  const strip = (e) => { const c = Object.assign({}, e); delete c.local; return c; };

  /* Names NTFS refuses: reserved device words, characters no Windows API accepts, and a trailing
   * dot or space (silently stripped, which is its own corruption). */
  const _winBad = (p) => String(p).split('/').some(seg =>
    /[<>:"\\|?*\x00-\x1f]/.test(seg) || /[. ]$/.test(seg)
    || /^(con|prn|aux|nul|com[0-9\u00b9\u00b2\u00b3]|lpt[0-9\u00b9\u00b2\u00b3])(\.|$)/i.test(seg));

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
  async function scan(fs, o, stopping, onProgress){
    const disk = {};
    /* WHAT THE SCAN COULD NOT READ IS NOT ABSENT — IT IS UNKNOWN, and the difference is a deleted
     * subtree. A folder the walk could not enter (permissions, a disk hiccup, an antivirus holding
     * it) leaves its files out of `disk`; read as "deleted locally" they are tombstoned to every
     * device. Measured: five files under one unreadable folder → five tombstones, guards silent
     * (below the mass floor). So the skipped paths travel with the result, and the sweep treats
     * them exactly like exclusions: dropped from all three inputs, deletable by no one. */
    const unread = [];        // paths, for the exclusion machinery
    const unreadWhy = [];     // {path, why}, for the report — "no permission" and "being written
                              // right now" are opposite messages and must not share a count
    const note = (r) => { for(const k of (r && r.skipped) || []){
      const p = (k && k.path) || k; if(!p) continue;
      unread.push(String(p));
      unreadWhy.push({ path: String(p), why: String((k && k.why) || 'unreadable') });
    } };
    const so = { hash: !!o.hash, excludes: o.excludes || [], maxBytes: 0 };
    if(typeof fs.scanPage !== 'function'){
      const r = await fs.scan(o.id, so);
      for(const p in (r && r.files) || {}) disk[p] = compact(r.files[p]);
      note(r);
      return { disk, unread, unreadWhy };
    }
    let off = 0;
    for(;;){
      if(stopping()) return { disk, unread, unreadWhy };
      const page = await fs.scanPage(o.id, so, off, SCAN_PAGE);
      const files = (page && page.files) || {};
      const n = Object.keys(files).length;
      for(const p in files) disk[p] = compact(files[p]);
      note(page);
      off += n;
      /* SAY WHERE IT IS. A first sweep after a restore hashes every file — many minutes of disk
       * work behind a status that read only "syncing…", which is indistinguishable from a hang.
       * One line per page. */
      try{ if(onProgress) onProgress({ phase: so.hash ? 'reading every file (first sweep)' : 'scanning',
                                       i: off }); }catch(_){}
      if(!n || !page || page.done) break;
    }
    return { disk, unread, unreadWhy };
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
    /* BACKPRESSURE ON THE HEAP, NOT ON HOPE. Each in-flight small file holds plaintext, ciphertext
     * and the request body at once, and Chromium's renderer dies quietly when the heap tops out —
     * "windows app running out of memory while syncing" is that death. When the heap is already
     * past ~1.5 GB the lanes drop to one; recovered, they resume. Costs nothing when memory is
     * fine, and a slower sweep beats a dead window every time it comes up. */
    const _fat = () => { try{ const m = performance && performance.memory;
        return !!(m && m.usedJSHeapSize > 1.5 * 1024 * 1024 * 1024); }catch(_){ return false; } };
    const lane = async (id) => {
      while(next < small.length && !stopping()){
        if(id > 0 && _fat()){ await new Promise(r => setTimeout(r, 400)); continue; }
        const item = small[next++];
        await run(item, ++done, n);
        await journal.maybe();
      }
    };
    await Promise.all(Array.from({ length: Math.max(1, Math.min(lanes, small.length)) }, (_, i) => lane(i)));
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
  async function receive(fs, io, o, path, entry, onPercent, beforeCommit, stopping){
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
          if(stopping && stopping()) throw new Error(STOPPED);
          got = Math.max(got, off + ((bytes && bytes.length) || 0));
          if(total && onPercent) onPercent(Math.round(got / total * 100));
          return fs.writePart(o.id, path, off, bytes);
        }, entry.size, from, entry.cs || 0);
      };
      await pull(have);
      try{
        await verifyPart(fs, o, path, entry);
      }catch(e){
        /* A RESUMED DOWNLOAD THAT FAILS ITS CHECKSUM IS THE PART FILE'S FAULT UNTIL PROVEN
         * OTHERWISE. A part file is tied to nothing but its length, so one left by an EARLIER
         * version of the same path resumes into a splice of two files — and the checksum,
         * correctly, refuses it. Blaming the stored copy there is worse than the corruption: a
         * perfectly good file becomes permanently unfetchable on this device because of a stale
         * temp file we wrote ourselves. So: throw the part file away and fetch the whole thing.
         * Only a from-scratch download that still fails is evidence about the copy. */
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

  /* An upload, and the record the other devices will read.
   *
   * `csum` is the FILE's own hash and is always recorded: it is what a download is checked against,
   * what tells a restored backup from an edit, and what lets two devices holding the same file agree
   * without transferring it. `sha` (or the chunk list) is where the encrypted bytes live — a
   * different number entirely, and comparing one against the other is how a folder duplicates
   * itself. */
  /* `stopping` reaches INSIDE the transfer. Pause used to be honoured only between files, so a
   * multi-gigabyte ISO paused at 71% uploaded its remaining chunks for minutes while the card said
   * paused — the folder held its slot, everything queued behind it, and every button answered
   * "already syncing": reported as "then it breaks completely". A cut chunked upload costs almost
   * nothing to resume — the chunks already sent are content-addressed and skipped next sweep. */
  async function send(fs, io, o, u, me, onPercent, stopping){
    const path = u.path, size = (u.stat && u.stat.size) || 0;
    const entry = { v: u.v, by: me, size, mtime: (u.stat && u.stat.mtime) || 0 };
    if(size > chunkAbove(fs, o) && io.putParts && typeof fs.readPart === 'function'){
      /* THE CALLER'S FIGURE FIRST. It is the platform's chunk clamped to what the NODE accepts, and a
       * chunk IS one upload — reading `fs.chunkBytes` here ignored the clamp entirely, so a node with
       * a small limit chunked at the right threshold and then sent pieces it would still reject. */
      /* THE CHECKSUM MUST CERTIFY THE CHUNKS, and they used to be taken at different moments.
       *
       * The chunks are read over minutes; the csum came from the scan (minutes earlier) or from
       * hashing the file AFTER the last chunk (minutes later). A file edited anywhere inside that
       * window published chunks of a TORN file under a clean checksum of the final one — and then
       * every device that downloads it fails its checksum for ever, while the uploader's own journal
       * says all is well. So the file is hashed on BOTH sides of the chunk reads. Equal means it sat
       * still for the whole window and the csum certifies what was stored; different means the store
       * holds a torn copy, so nothing is recorded and the next sweep takes the file fresh. */
      const canHash = typeof fs.hashFile === 'function';
      const before = (u.stat && u.stat.csum) || (canHash ? await fs.hashFile(o.id, path) : undefined);
      const cs = (o.chunkBytes || 0) || (fs.chunkBytes || 0) || undefined;
      const r = await io.putParts((off, len) => {
                                    if(stopping && stopping()) throw new Error(STOPPED);
                                    return fs.readPart(o.id, path, off, len);
                                  }, size,
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
    /* A WHOLE-FILE READ IS EXACTLY THE FILE, OR IT IS A FAILURE. A truncation here is
     * SELF-CONSISTENT: the short buffer is what gets hashed, so the record's checksum matches the
     * truncation, the receiving device verifies it happily and writes a short file, and every check
     * afterwards agrees. Storing a half-written file under a checksum that certifies it is the one
     * outcome there is no recovering from. */
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

  /* A RECORD THAT NAMES NO BYTES IS NOT A FILE.
   *
   * A live record with neither a `sha` nor a chunk list says "this file exists" and does not say
   * where — so every other device plans a download, fetches nothing, and fails, for ever.
   * Publishing one is the bug; this is the last place before it goes out. */
  function addressed(entry, path){
    const has = entry && (entry.sha || (entry.chunks && entry.chunks.length));
    if(entry && !entry.deletedAt && !has){
      throw new Error('the upload finished without an address for ' + path
                      + ' — refusing to record a file nothing can fetch');
    }
    return entry;
  }

  /* The journal, written in batches.
   *
   * Per file would be correct and far too expensive; per sweep is what made an interrupted first
   * sync start from the beginning. In batches, an interruption costs at most the last few files —
   * and redoing one is nearly free.
   *
   * THE RECORDS GO FIRST, ALWAYS. A journal persisted ahead of the published records is a device
   * that believes in an agreement no other device has seen; records ahead of the journal are safe —
   * the next sweep adopts its own publish by content. `beforeSave` is that publish; the sweep wires
   * it in before the first byte moves. A failed CHECKPOINT is not a failed sweep — the work is real
   * either way, so `maybe` records the error and carries on; only the final `flush` throws. */
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
   * says it is? It re-hashes the files on this disk against the record set, and — deep — asks the
   * server whether the bytes behind every record are still there. Nothing is written, nothing is
   * deleted, nothing is fetched.
   */
  async function verify(fs, io, opts){
    const o = opts || {};
    const key = o.key || o.id;
    const tick = (p) => { try{ if(typeof o.onProgress === 'function') o.onProgress(p); }catch(_){} };
    const out = { corrupt:[], missingHere:[], missingBytes:[], extra:[], unverified:[],
                  checked:0, devices:[] };

    const got = await io.state(key);
    const state = (got && got.state) || {};
    out.undecryptable = (got && got.undecryptable) || 0;
    { const devs = new Set();
      for(const p in state) if(state[p] && state[p].by) devs.add(state[p].by);
      out.devices = [...devs]; }

    tick({ phase: 'scanning' });
    const disk = (await scan(fs, Object.assign({}, o, { hash: false }), () => false)).disk;

    const paths = Object.keys(state).filter(p => state[p] && !state[p].deletedAt);
    let i = 0;
    for(const p of paths){
      const entry = state[p];
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
    for(const p in disk) if(!state[p] || state[p].deletedAt) out.extra.push(p);

    /* Are the bytes still on the server? Only asked when the store can answer a cheap existence
     * question. `null` is "I could not ask" — never "missing": this list drives a repair that
     * publishes deletions, and a rate limiter answering 429 a thousand times in a row is the
     * expected case here, not the exotic one. */
    if(typeof io.hasBlob === 'function' && o.deep){
      const want = paths.slice(0, o.blobLimit || 5000);
      let j = 0;
      for(const p of want){
        const e = state[p];
        const ids = e.chunks && e.chunks.length ? e.chunks : (e.sha ? [e.sha] : []);
        tick({ phase: 'checking the store', path: p, i: ++j, n: want.length });
        for(const id of ids){
          let there = null;
          try{ there = await io.hasBlob(id); }catch(_){ there = null; }
          if(there === false){ out.missingBytes.push(p); break; }
          if(there === null){ out.unverified.push(p); break; }
        }
      }
    }
    return out;
  }

  const API = { sweep, verify, scan, idOf, SCAN_PAGE, LANES };
  root.PCSyncExec = API;
  if(typeof module !== 'undefined' && module.exports) module.exports = API;
})(typeof globalThis !== 'undefined' ? globalThis : this);
