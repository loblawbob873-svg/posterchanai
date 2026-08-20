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
  /* How much whole-file transfer may be in flight at once, in BYTES rather than in files.
   *
   * A file count is the wrong instrument: three 6 MB photos and three 100 MB videos are the same
   * number and nowhere near the same risk. The budget is one large file's worth of holding, which
   * is the guarantee the Windows OOM scenario encodes — so a 12 MB TIFF still goes alone, exactly as
   * before, while 6 MB photographs go two at a time and 3 MB ones four. The multiplier is what a
   * transfer really costs: plaintext, ciphertext and a request body, all live at once. */
  /* ...AND IT IS SCALED TO THE HEAP THIS DEVICE ACTUALLY HAS. A desktop and a tablet run the same
   * code and are not the same machine: a tablet's WebView is killed at a fraction of a desktop's
   * ceiling, and it is killed by being RECREATED — "the tablet reloaded the screen again during
   * sync", which from the inside is the sweep simply ceasing to exist. `jsHeapSizeLimit` is what the
   * runtime will actually allow, so the budget is a small fraction of it rather than a number picked
   * on a desktop. Unknown (no performance.memory outside Chromium) keeps the desktop figure, which
   * is the value this was measured at. */
  const BIG_BUDGET = (() => {
    const FLOOR = 8 * 1024 * 1024, DESK = 36 * 1024 * 1024;
    try{
      const lim = performance && performance.memory && performance.memory.jsHeapSizeLimit;
      if(!lim) return DESK;
      return Math.max(FLOOR, Math.min(DESK, Math.floor(lim / 16)));
    }catch(_){ return DESK; }
  })();
  const BIG_COST = 3;
  const BIG_LANES = 6;              // an upper bound on lanes; the budget is what actually decides
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
      _mark(phase);
      tick({ phase, path, i, n });
    };
    /* SAMPLED WHERE THE MEMORY ACTUALLY GOES, not only where progress is reported. `step` is called
     * per FILE during transfers, so the peak it found was always a transfer — but the two biggest
     * allocations in a sweep happen before any of that and between two steps: decrypting the whole
     * record set (every path, every checksum, and for a chunked file a list of one hash per 4 MB,
     * so a 2 GB file alone is ~500 of them) and loading this device's journal. Both are live at
     * once while the plan is built. Unsampled, the report blamed whatever moved next. */
    const _mark = (phase) => {
      const h = _heap();
      if(h > _peakHeap){ _peakHeap = h; _peakPhase = phase; }
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
    _mark('before reading the folder\u2019s records');
    try{ got0 = await io.state(key, tick); }
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
    _mark('the folder\u2019s records, decrypted');
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
    /* BOTH SIDES OF THE FOLDER, AS TWO NUMBERS. "how many files do I have here" and "how many does
     * the folder agree exist" are the two questions every report in this feature has been about,
     * and neither was ever on the screen — so "271 in the trash", "3 restorable" and "nothing is
     * syncing" all had to be interpreted against a total nobody could see. Counted where both are
     * already in hand and free. Tombstones are counted separately: they are records, they are not
     * files, and folding them into one figure is what made a folder of 8,132 deletions read as a
     * folder of 8,132 files. */
    { let live = 0, gone = 0;
      for(const p in state){ if(state[p] && state[p].deletedAt) gone++; else live++; }
      report.here = report.scanned;
      report.shared = live;
      report.sharedGone = gone; }
    { const busy = [], design = [], denied = [];
      /* Three kinds of "could not read", and only one is anyone's problem: a file being WRITTEN
       * right now (a queue), a link or Windows junction (ignored BY DESIGN — `My Music` inside
       * every Documents is an access-denied reparse point, and a venv's `python` symlink must
       * never sync as a file), and a genuine permission failure (needs a person). The card once
       * printed all three as "couldn't be read — retried every sweep", which turned system junk
       * into a standing worry. */
      const JUNCTIONS = /^(my music|my pictures|my videos|application data|local settings|start menu|templates|cookies|nethood|printhood|recent|sendto)$/i;
      for(const x of unreadWhy){
        if(/in use|vanished/i.test(x.why)) busy.push(x.path);
        else if(/symlink/i.test(x.why) || JUNCTIONS.test(String(x.path).split('/').pop())) design.push(x.path);
        else denied.push(x.path);
      }
      if(busy.length) report.busyNow = busy.slice(0, 200);
      if(design.length) report.skippedByDesign = design.slice(0, 200);
      if(denied.length) report.unreadable = denied.slice(0, 200); }

    /* NAMES THAT ARE ONE FILE ON ONE PLATFORM AND TWO ON ANOTHER — REPORTED, NEVER RESOLVED.
     *
     * `Foo.txt` and `foo.txt` are two files on Linux and the SAME file on macOS and Windows. There
     * is no correct automatic answer: folding them loses one of two files a Linux user legitimately
     * holds, and leaving them makes a Windows device download each over the other for ever, one
     * version bump per sweep — which is a churn loop, and this feature has had enough of those to
     * know what one costs. Naming it is the whole job; a person has to rename one.
     *
     * The comparison is over the RECORD SET, not the disk: the collision is only visible from the
     * side that holds both spellings, and the device suffering for it is the one that can hold
     * neither. Bounded work — a lowercase map of paths already in memory. */
    { const seen = {}, clash = [];
      for(const p in state){ const R = state[p]; if(!R || R.deletedAt) continue;
        const lc = p.toLowerCase();
        if(seen[lc] && seen[lc] !== p){ clash.push([seen[lc], p]); } else seen[lc] = p; }
      if(clash.length) report.caseClash = clash.slice(0, 50); }

    /* IS ANOTHER SYNC ENGINE WRITING THIS FOLDER? Two authorities over one directory produce every
     * symptom this feature has ever been accused of, and neither engine can tell it is happening:
     * a file the OTHER one deletes reads here as a local deletion and is published to every device;
     * a conflict copy written here is replicated by the other and comes back after you remove it;
     * a 2 GB `.pcpart` is copied elsewhere while it is still being appended to.
     *
     * Measured on a real folder after five days of unexplained losses: `.stversions` held
     * PosterChan-named conflict copies from a fortnight earlier that Syncthing had archived away,
     * and nothing anywhere had ever mentioned that Syncthing was on the same tree. The scan already
     * skips these directories — that stops us SYNCING them, which is a different problem — so the
     * fact was on the disk the whole time and simply never said out loud.
     *
     * One stat per marker, on the path the scan already resolves. It changes no decision: it is
     * reported, and the card names it, because the fix is a person choosing which engine owns the
     * folder and nothing here can choose for them. */
    if(typeof fs.confirmGone === 'function'){
      for(const [marker, who] of [['.stfolder', 'Syncthing'], ['.sync', 'Resilio Sync'],
                                  ['.dropbox', 'Dropbox'], ['.nextcloudsync.log', 'Nextcloud']]){
        try{
          const ev = await fs.confirmGone(o.id, marker);
          if(ev && ev.gone === false && ev.parentAlive !== false){
            (report.otherEngines = report.otherEngines || []).push(who);
          }
        }catch(_){}
      }
    }

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
    /* A flag value is `<address>` on an older device and `<address>|<hash the downloader measured>`
     * now. Split rather than parsed: an address is hex and never contains a bar. */
    const seenHash = {};
    { const flagged0 = (got0 && got0.flagged) || {};
      const flagged = {};
      for(const p in flagged0){
        const raw = String(flagged0[p] || '');
        const bar = raw.indexOf('|');
        if(bar < 0){ flagged[p] = raw; continue; }
        flagged[p] = raw.slice(0, bar);
        seenHash[p] = raw.slice(bar + 1);
      }
      if(!o.dryRun){
        for(const p in flagged){
          const e = index[p]; if(!e || e.deletedAt || !disk[p]) continue;
          /* "ALREADY RE-SENT" IS A VERSION QUESTION, NOT AN ADDRESS ONE — and read as an address it
           * skipped the case this whole repair exists for.
           *
           * The check meant: our journal names different bytes from the flagged ones, so we must
           * have re-sent already; nothing to do. That holds only when our journal is at or past the
           * flagged record. When it is BEHIND, the addresses differ for the opposite reason — we
           * hold an older version and never applied the newer one — and that is precisely the shape
           * of a record whose checksum is wrong: the newer version cannot be downloaded by anybody,
           * every device fails it, and the one machine still holding good bytes is skipped here for
           * looking like it had already helped.
           *
           * Measured on a real folder: four receipts, a good copy on the desktop restored from a NAS
           * backup, a record one version ahead carrying a checksum that describes nothing, and no
           * path in the app that would put them right. Mirror this Device walks past a LIVE record;
           * Check files can do it by hand; nothing did it by itself.
           *
           * Re-sending an older copy over a newer one is a real cost and it is the right trade here,
           * because the newer one is not a usable file — it fails verification on every device that
           * fetches it. A working older version beats a newer one nobody can open. */
          if(E.versionOf(e) >= E.versionOf(state[p]) && idOf(e) !== flagged[p]) continue;
          try{
            /* A LOCAL COPY THAT DISAGREES WITH ITS OWN JOURNAL IS RE-SENT, NOT REFUSED — and this
             * used to be a dead end that no button could get out of.
             *
             * The rule was "my file does not match what I published, so my file is damaged; do not
             * spread it". That reads one of two possibilities and calls it the only one. The other
             * is that the CHECKSUM I published is wrong — which is a thing that really happened:
             * Android's digest stopped at a zero-length read and published the hash of a prefix.
             * After that build is fixed the holder hashes its file correctly, finds it disagrees
             * with the number it published, and declares its own perfectly good file bad. The
             * record stays flagged, nothing re-sends, and every other device refuses the file for
             * ever — the fix making the symptom permanent.
             *
             * Weigh what is actually known at this point. Another device already proved the STORE's
             * bytes hash to something other than this checksum (that is why the record is flagged),
             * and this device now finds its own bytes hash to something other than it too. Two
             * independent readings disagree with the checksum; the checksum is the odd one out. And
             * the alternative on offer is not "keep the good stored copy" — no device can use the
             * stored copy, it fails verification everywhere. Re-sending replaces a record nobody can
             * use with one that describes a real file that hashes.
             *
             * It is still SAID, because the other reading is possible: the path is reported under
             * `badHere` either way, now meaning "this device's copy did not match, and it was
             * re-sent under a fresh checksum" rather than "this device is out". */
            if(e.csum && typeof fs.hashFile === 'function'){
              const h = await fs.hashFile(o.id, p);
              if(h && h !== e.csum){
                /* MY COPY DISAGREES WITH WHAT I PUBLISHED. Two readings, opposite repairs:
                 *   - my file is damaged        → re-seeding it spreads the damage
                 *   - the checksum I published is wrong → refusing leaves the file unfetchable
                 *     everywhere, for ever
                 * The flag carries the hash the DOWNLOADER measured from the store, which decides
                 * it: if my bytes hash to the same thing the store's bytes hashed to, two
                 * independent devices agree about the content and only the recorded checksum is the
                 * odd one out — so it is re-sent, and the fresh upload records a checksum that
                 * describes what is really there. That case is real: Android's digest stopped at a
                 * zero-length read and published the hash of a prefix, and once that build is fixed
                 * the holder would otherwise declare its own perfectly good file bad and strand the
                 * file on every device.
                 * If they differ, the copies really are different and this one is not evidence of
                 * anything — refuse, and say so.
                 * An OLD flag carries no hash at all, and stays conservative. */
                if(!(seenHash[p] && seenHash[p] === h)){
                  (report.badHere = report.badHere || []).push(p);
                  continue;
                }
                (report.staleChecksum = report.staleChecksum || []).push(p);
              }
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
    /* "THIS DEVICE'S COPY IS CORRECT — PUBLISH IT." The recovery that was missing, and whose
     * absence made "remove the folder and add it again" the only way out of a folder carrying
     * deletions for files you still have.
     *
     * Retiring a pair works, but it is a sledgehammer: it throws away every record for every file,
     * so every other device re-reads the whole folder from nothing, and it is offered for a
     * situation — "the shared record says these are deleted and they are sitting right here" —
     * that needs one thing said, not everything forgotten.
     *
     * Deliberately NOT every file on the disk: only the paths where the FOLDER DISAGREES that the
     * file exists — no record at all, or a tombstone. A file whose record is live and correct needs
     * nothing said about it, so a 12,000-file folder republishes the handful actually in dispute
     * instead of re-encrypting and re-uploading everything. Named paths, so the resurrect floor
     * does not question a list the person chose, and (see below) they are taken out of the trash
     * list as well, because a path somebody asked to publish is not a deletion candidate. */
    let _reclaim = [];
    if(o.resendAll && !o.dryRun){
      for(const p in disk){
        const R = state[p];
        if(!R || R.deletedAt) _reclaim.push(p);
      }
      report.reclaiming = _reclaim.length;
    }
    const _resend = (o.resend || []).concat(_heal, _reclaim);
    if(_resend.length && !o.dryRun){
      const want = new Set(_resend);
      const already = new Set(plan.send.map(u => u.path));
      const extra = [];
      for(const p of want){
        if(already.has(p) || !disk[p]) continue;
        extra.push({ path: p, v: E.bump(state[p], index[p]), stat: disk[p],
                     why: 'sending again — the store no longer has these bytes' });
      }
      /* A PATH SOMEBODY NAMED IS NOT A GUESS, SO IT IS NOT GUARDED. `resurrect` marks a send the
       * ENGINE inferred from a fresh timestamp — it may be a real edit or it may be a backup
       * restored, the engine cannot tell, and past FLOOR it rightly asks. But this list did not come
       * from an inference: it came from a person pressing a button on these exact files. Left
       * flagged, the massResurrect verdict swept them straight back out of the plan again
       * (`apply()` drops every resurrect at once), so the one deliberate way out of a standoff —
       * "put these back everywhere" — silently did nothing and the folder stayed deleted. */
      /* `mustSend` DEFEATS THE SETTLE-BY-CONTENT SHORTCUT BELOW, and it has to.
       *
       * That shortcut skips an upload when the live record's checksum already matches the file on
       * disk — right for an ordinary sweep, and exactly wrong for these paths, whose whole reason
       * for being here is that the STORE lost the bytes the record describes. The record still
       * certifies the same content, so the shortcut would agree the file is safely stored, journal
       * it, and skip the repair — leaving a folder that reports itself in step while the bytes
       * behind it stay missing, and no way left to press. */
      let unflagged = 0, marked = 0;
      const relist = plan.send.map(u => {
        if(!want.has(u.path)) return u;
        marked++;
        if(!u.resurrect) return Object.assign({}, u, { mustSend: true });
        unflagged++;
        return Object.assign({}, u, { resurrect: false, mustSend: true,
                                      why: 'putting this back — asked for by name on this device' });
      });
      for(const x of extra) x.mustSend = true;
      /* `marked` BELONGS IN THIS CONDITION, and leaving it out threw the marks away.
       *
       * A path the sweep was already going to send needs no `extra` entry and is no `resurrect`, so
       * a named repair of an ordinary changed file made both counters zero — the relist carrying its
       * `mustSend` flags was computed and then dropped on the floor, and the shortcut swallowed the
       * repair after all. Everything visible said the plan was right; only the flag was missing. */
      if(extra.length || unflagged || marked){
        /* AND IT COMES OUT OF THE TRASH LIST, which is the half that was missing and the half that
         * mattered. `resend` dropped a named path from settle/fetch/keepBoth and left `trash`
         * alone — so a sweep could be told "send this file" and move it to .pc-trash in the same
         * pass. That is exactly what happens after Restore from trash: the restored bytes ARE the
         * bytes the tombstone describes, so on a hashed scan the engine reads "this copy is the
         * deleted version" and trashes it again. Restore, sweep, back in the trash, 172 files at a
         * time, with the restore reporting success every round. A path somebody named is not a
         * candidate for deletion in the sweep they named it in. */
        const drop = new Set(extra.map(x => x.path));
        const named = new Set(want);
        plan = Object.assign({}, plan, {
          send: relist.concat(extra),
          settle: plan.settle.filter(x => !drop.has(x.path)),
          fetch: plan.fetch.filter(x => !drop.has(x.path)),
          keepBoth: plan.keepBoth.filter(x => !drop.has(x.path)),
          trash: plan.trash.filter(x => !named.has(x.path)),
        });
        if(extra.length) report.resent = extra.length;
        if(unflagged) report.restoring = unflagged;
      }
    }
    report.plan = plan;
    report.unchanged = plan.unchanged;
    report.settledGone = plan.settledGone || 0;
    report.excluded = plan.excluded;

    const verdicts = E.check(plan, { state, indexSize: Object.keys(index).length,
                                     caseFolds: fs.caseFolds !== false });
    /* ONE QUESTION PER KIND, NOT ONE PER RULE. Two rules can raise `massTrash` — the proportional
     * "this removes more than it keeps" and the absolute floor — and both fire together on exactly
     * the sweep that matters most. Asked per verdict, that is the SAME dialog twice in a row about
     * the same files, which is how a person learns to click through the one that counts. `apply()`
     * has always keyed on `kind`, so one answer covers both; the SHORT-LIST wording is preferred
     * when it applies, because "fewer survive than would be removed" is the more alarming fact and
     * it is the one that must be read. Fatal verdicts are never offered at all. */
    const allowed = [];
    const asked = new Map();
    for(const v of verdicts){
      if(v.fatal){ report.refused.push(v); continue; }
      const seen = asked.get(v.kind);
      if(seen !== undefined){
        if(!seen) report.refused.push(v);
        continue;
      }
      const worst = verdicts.filter(x => !x.fatal && x.kind === v.kind)
                            .sort((a, b) => (a.rule === 'shortList' ? -1 : 0)
                                          - (b.rule === 'shortList' ? -1 : 0))[0] || v;
      let ok = false;
      if(!o.dryRun && o.manual && typeof o.confirm === 'function'){
        try{ ok = !!(await o.confirm(worst)); }catch(_){ ok = false; }
      }
      asked.set(v.kind, ok);
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
    /* FLAGS FLOW WITH THE CHECKPOINTS, NOT AFTER THE SWEEP. A sweep failing a thousand fetches
     * used to hold every flag until it finished — so the device that could REPAIR them sat idle
     * for the whole grind. Reported failures reach the record within a checkpoint now, and the
     * holder starts re-sending while the reporter is still discovering. */
    const flagQueue = [];
    const flushFlags = async () => {
      if(!flagQueue.length || typeof io.flagBad !== 'function') return;
      const batch = flagQueue.splice(0);
      try{ await io.flagBad(key, batch); }catch(_){ /* flags are advisory; the final pass retries */ }
    };
    const flushPuts = async () => {
      await flushFlags();
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

    /* RE-SEAL WHAT THE OLD FORMAT STILL HOLDS — FIRST, before a byte moves. It ran at the tail
     * of the sweep, after hours of transfers, on the machine that keeps running out of memory —
     * so the conversion everything else was waiting on kept dying unconverted. It is a couple of
     * minutes of pure record writes from the journal's plaintext; nothing about it needs to wait. Records written before the drive-key seal route
     * every reader through the signer backend — one call per record, which turned a tablet's join
     * into "about 37 min left". This device's journal holds those entries in PLAINTEXT, so it can
     * republish them in the new seal without decrypting or transferring anything: one version
     * bump, one record write each, capped per sweep to bound the batch. Tombstones are left as
     * they are (rare, and a republished tombstone spends the server's mass-delete backstop). */
    { let resealed = 0;
      for(const p2 of (got0.oldSeal || [])){
        if(resealed >= 20000) break;   // a record write is ~200 bytes; a whole folder converts in one pass
        const e2 = index[p2], R2 = state[p2];
        if(!e2 || e2.deletedAt || !R2 || R2.deletedAt) continue;
        if(E.versionOf(R2) !== E.versionOf(e2)) continue;      // mid-change: its writer will seal it
        const next = Object.assign(strip(e2), { v: E.versionOf(e2) + 1 });
        record(p2, next, e2.local || null, true);
        resealed++;
      }
      if(resealed){
        report.resealed = resealed;
        step('modernizing the folder\u2019s records');
        await flushPuts();
        await journal.flush();
      }
    }


    // Deletions first: they are a rename into .pc-trash, they cost nothing, and queued behind hours
    // of transfer they are simply never reached.
    let ti = 0;
    for(const t of plan.trash){
      if(stopping()) return await halt(report, journal);
      step('to trash', t.path, ++ti, plan.trash.length);
      try{
        const to = await fs.trash(o.id, t.path, now);
        record(t.path, Object.assign({}, t.entry), null);
        /* NAME THE DEVICE THAT ASKED. A deletion below the floor is applied without a dialog, which
         * is right — being asked about three files is how people learn to click through the
         * question about three thousand. But then the only trace is N files in the trash and no way
         * to tell whether you did it, another device did it, or something went wrong: reported as
         * "somehow tablet got 7 files in the trash magically". The record carries the device that
         * published the tombstone and when; both are free to report. */
        report.trashed.push({ path: t.path, to,
                              by: (t.entry && t.entry.by) || '',
                              at: (t.entry && t.entry.deletedAt) || 0 });
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
        /* A REMEMBERED failure still flags: skipping the retry saves bandwidth, but the holder
         * must keep hearing about the record until it is actually repaired. */
        flagQueue.push({ path: c.path, id: cid });
        continue;
      }
      /* A CONFLICT OVER IDENTICAL BYTES IS NOT A CONFLICT. An unhashed scan compares size+mtime,
       * and two copies of the same photo can differ on both — so before renaming anything, the one
       * cheap question that settles it for real: hash the local file against the incoming record.
       * Equal means both sides hold the same content and the only divergence was a timestamp; the
       * journal records agreement and no copy is minted. This is also what absorbs the CAS race —
       * two devices uploading the same file, the loser refused, resolving here. */
      /* A RECORD WITH NO CHECKSUM CANNOT BE COMPARED — SO IT MUST NOT BE DUPLICATED.
       *
       * The settle-by-content check below needs `entry.csum`, and a record published by the Files
       * upload path does not have one (computing it means holding the whole file, which is what
       * chunking exists to avoid). With no csum the check was SKIPPED ENTIRELY and the code fell
       * straight through to minting a copy — so on a device joining with an empty journal, where
       * `same()` has only size and mtime to work with and a restored backup carries fresh mtimes,
       * EVERY such file became a conflict copy. Reported as a desktop join producing "a bunch of
       * conflict files" on a folder whose bytes were already correct.
       *
       * Same size and no way to compare is not evidence of difference. Different size IS, and still
       * conflicts. This leaves both copies untouched, names the path, and the next sweep asks again
       * — the same answer as a hash that could not be computed, a few lines down. */
      if(c.entry && !c.entry.csum && disk[c.path]
         && (c.entry.size || 0) === (disk[c.path].size || 0)){
        report.uncompared = report.uncompared || [];
        report.uncompared.push({ path: c.path, why: 'the shared record carries no checksum for this '
          + 'file, so nothing here can tell the two copies apart — both were left exactly as they '
          + 'are. The device holding it will publish a checksum on its next sweep' });
        report.ok = false;
        continue;
      }
      if(c.entry && c.entry.csum && typeof fs.hashFile === 'function' && disk[c.path]){
        let h = null, asked = true;
        try{ h = await fs.hashFile(o.id, c.path); }catch(_){ h = null; asked = false; }
        if(h && h === c.entry.csum){
          const L = disk[c.path] || {};
          record(c.path, Object.assign({}, c.entry), { size: L.size, mtime: L.mtime, csum: h });
          continue;
        }
        /* "COULD NOT COMPARE" IS NOT "DIFFERENT", and this is the one place in the sweep where
         * getting that wrong duplicates a file instead of losing one.
         *
         * The hash above is the only thing standing between a timestamp difference and a conflict
         * copy — and on Android it reads the WHOLE file back through SAF. On a multi-gigabyte file
         * that is minutes of I/O that can throw, be killed with the renderer, or simply answer
         * nothing; every one of those landed as `h = null` and fell through to minting a second
         * copy of a 2 GB file. Reported the first time a big file finished syncing to a phone:
         * "phone now has conflict files".
         *
         * So an unanswered question leaves both copies exactly as they are and says so. The next
         * sweep asks again — the same shape as an unreadable scan path, a store that could not be
         * listed, and a deletion that could not be confirmed. A conflict is only ever minted from a
         * hash that actually came back and actually differed. */
        if(!asked || !h){
          report.uncompared = report.uncompared || [];
          report.uncompared.push({ path: c.path, why: 'this device could not read the file back to '
            + 'compare it (large files can take minutes) — both copies were left exactly as they '
            + 'are, and the next sync will try again' });
          report.ok = false;
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
          report.badFetch[c.path] = { id: cid, why: 'checksum', got: e && e.got,
                                      v: E.versionOf(c.entry) };
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
        /* BYTES WE ALREADY HOLD ARE NOT DOWNLOADED AGAIN — the mirror image of the send side's
         * settle-by-content, and the missing half of the same idea.
         *
         * `same(L, R)` in the planner compares content only when BOTH sides carry a checksum, and a
         * paged scan does not hash — it cannot afford to, on a folder of 12,000 files. So the local
         * side has no checksum and the comparison falls back to size and mtime. Anything that
         * rewrites a file's timestamp without changing a byte therefore looks like a different file:
         * a restore from backup, an rsync, a second sync engine, a touch.
         *
         * Measured: a desktop restored from a NAS backup and told to Mirror fetched 223 blobs in
         * twelve minutes — every one of them a file it already held byte-for-byte — while its actual
         * uploads sat at eleven in half an hour. The bandwidth was real and the work was not.
         *
         * One local hash answers it, and it is cheaper than the transfer it replaces by orders of
         * magnitude. Equal to the record's checksum means this device already has exactly these
         * bytes: journal the record's own version against the file that is here and download
         * nothing. Anything else — no checksum to compare, a hash that cannot be read, a real
         * difference — falls through to the download exactly as before. Like its counterpart on the
         * send side, it can only ever remove work; it decides nothing. */
        if(!o.dryRun && e.csum && disk[d.path] && typeof fs.hashFile === 'function'){
          let _h = null;
          try{ _h = await fs.hashFile(o.id, d.path); }catch(_){ _h = null; }
          if(_h && _h === e.csum){
            record(d.path, e, { size: disk[d.path].size, mtime: disk[d.path].mtime, csum: _h }, false);
            (report.heldAlready = report.heldAlready || []).push(d.path);
            return;
          }
        }
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
                                    + 'the device that has this file will send it again by itself' });
          flagQueue.push({ path: d.path, id: badId });   // the holder keeps hearing until it is fixed
          return;
        }
        /* A DOWNLOAD OF BYTES YOU ALREADY HOLD IS AN ADOPTION, NOT A TRANSFER. A record whose
         * version moved without its content (the re-seal, a redundant republish) reads as
         * "changed elsewhere" on every other device, and size+mtime cannot clear it — platforms
         * stamp their own times. One local hash answers it for real: equal means agree-and-record,
         * zero bytes moved. 712 phantom re-downloads after one conversion pass, made free. */
        if(e.csum && disk[d.path] && typeof fs.hashFile === 'function'){
          let h = null;
          try{ h = await fs.hashFile(o.id, d.path); }catch(_){ h = null; }
          if(h && h === e.csum){
            const L = disk[d.path];
            record(d.path, Object.assign({}, d.entry), { size: L.size, mtime: L.mtime, csum: h });
            report.adopted = (report.adopted || 0) + 1;
            return;
          }
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
          /* A BLOB SEALED WITH A DIFFERENT DRIVE KEY IS A THIRD KIND OF UNUSABLE COPY, and it was
           * the only one with no repair at all.
           *
           * The bytes are in the store and intact; this account simply has more than one drive key
           * in its history (two cold devices racing the mint is the known way that happens) and the
           * one this device holds does not open them. `hasBlob` says the blob is THERE, so none of
           * the missing-bytes machinery applies, and the error's own advice — "press Send them
           * again on the device that HAS this file" — pointed at a repair that only ever covered
           * blobs the store had LOST. So it failed on every sweep, for ever: "never recover".
           *
           * It is deterministic like a checksum failure — the same bytes will not open tomorrow —
           * so it is remembered by storage ADDRESS and FLAGGED on the record. Whoever still holds
           * the plaintext re-uploads it, which seals it under the CURRENT key and gives it a new
           * address, and the fresh address lifts every device's memory of the old one by itself.
           * On a device that both fails the fetch and holds the file — a restored desktop, which is
           * exactly where this was hit — the flag is picked up by its own next sweep and it repairs
           * itself. */
          if(/drive key does not open|different key/.test(why)){
            report.badFetch = report.badFetch || {};
            report.badFetch[d.path] = { id: idOf(d.entry), why: 'checksum', v: E.versionOf(d.entry) };
            flagQueue.push({ path: d.path, id: idOf(d.entry) });
            (report.wrongKey = report.wrongKey || []).push(d.path);
            failed(report, d.path, 'download', e);
          } else if(/checksum mismatch/.test(why)){
            report.badFetch = report.badFetch || {};
            report.badFetch[d.path] = { id: idOf(d.entry), why: 'checksum', got: e && e.got,
                                        v: E.versionOf(d.entry) };
            flagQueue.push({ path: d.path, id: idOf(d.entry), got: e && e.got });
            failed(report, d.path, 'download', e);
          } else if(/unavailable \(404\)/.test(why)){
            report.badFetch = report.badFetch || {};
            report.badFetch[d.path] = { id: idOf(d.entry), why: 'gone', at: now0(), v: E.versionOf(d.entry) };
            flagQueue.push({ path: d.path, id: idOf(d.entry) });
            report.unfetchable = report.unfetchable || [];
            report.unfetchable.push({ path: d.path, why: 'the store does not have these bytes yet — the '
                                      + 'device that has this file will send it again by itself' });
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
          /* BYTES THE STORE ALREADY HOLDS ARE NOT UPLOADED AGAIN — the send side's half of
           * adopt-by-content, and it was missing for the whole life of this engine.
           *
           * `diskChanged` compares a STAMP (size and mtime), because that is all a paged scan can
           * afford to know. So anything that rewrites a file without changing a byte — a restore
           * from backup, an rsync, a second sync engine on the same directory, a touch, a
           * virus scanner that rewrites in place — reads as "changed here" and is re-uploaded in
           * full. Reported after a sweep had finished cleanly, with every count in agreement
           * (11,939 here, 11,939 in the folder, 11,939 in the store): "why is desktop uploading
           * 2/19 files right now! sync was finished!" Nothing was wrong; the folder simply could
           * not tell a rewrite from an edit, and a multi-gigabyte file re-uploads either way.
           *
           * One whole-file hash answers it, and it is a hash the upload was about to take anyway —
           * `send()` hashes before it reads the first chunk. Equal to the live record's checksum
           * means the store holds these exact bytes: journal the new stamp at the record's own
           * version and publish NOTHING. Not equal, or unreadable, or no checksum to compare
           * against, and the upload proceeds exactly as before — the shortcut can only ever remove
           * work, never decide anything.
           *
           * The version is the record's, not a bump: this device learned nothing the folder did not
           * already know, and bumping would hand every other device a new version to re-examine —
           * one rewrite here becoming a round of work everywhere. */
          const _R = state[u.path];
          if(!u.mustSend && _R && !_R.deletedAt && _R.csum && typeof fs.hashFile === 'function'){
            let _h = null;
            try{ _h = await fs.hashFile(o.id, u.path); }catch(_){ _h = null; }
            if(_h && _h === _R.csum){
              record(u.path, _R, { size: u.stat.size, mtime: u.stat.mtime, csum: _h }, false);
              (report.settledByContent = report.settledByContent || []).push(u.path);
              return;
            }
          }
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
      /* MERGED, NOT PICKED. `index[p] || state[p]` reads as "prefer what we applied", and it is
       * how the address goes missing: a journal entry that lost its address (a struck CAS write, an
       * era change, a row written by an older build) SHADOWED a record that still had one, so the
       * tombstone was published naming nothing. Two things then break at once and neither says so —
       * "Deleted on every device" cannot offer the file (it lists only addressed tombstones: 107
       * deletions, 3 restorable), and a device still holding the file can never settle against it,
       * because the delete-loses-to-edit test compares csums and an absent csum always reads as an
       * edit — so it republishes for ever and trips the resurrect floor for ever. */
      const _keep = {};
      for(const _src of [state[t.path] || {}, index[t.path] || {}])
        for(const k of ['sha','csum','size','mtime','chunks','cs','ps'])
          if(_src[k] !== undefined) _keep[k] = _src[k];
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
    await flushFlags();
    await flushPuts();
    await journal.flush();
    if(journal.checkpointError) report.checkpointError = journal.checkpointError;

    if(stopping()) report.stopped = true;
    _mark('finishing');
    if(_peakHeap){ report.peakHeapMB = Math.round(_peakHeap / 1048576); report.peakHeapPhase = _peakPhase; }
    /* AN UNRESOLVED PATH IS NOT A CLEAN SWEEP. A skipped conflict adds nothing to `failed`, so the
     * sweep used to report success and the card said "in step" while a divergence sat unresolved.
     * Silence about that is exactly the shape this feature keeps getting wrong. */
    /* AN UNCOMPARED PATH IS UNRESOLVED TOO. It adds nothing to `failed` — nothing failed, a
     * question went unanswered — but the folder is not in step until it is asked again, and a sweep
     * that reports success there is the same silence this feature keeps paying for. */
    report.ok = report.failed.length === 0 && !(report.unfetchable || []).length
              && !(report.uncompared || []).length;
    return report;
  }

  /* ---- pieces ---------------------------------------------------------------------------------- */

  const msg = (e) => (e && e.message) || String(e);
  /* WHAT THE PERSON HAS TO DO, IN THE ERROR ITSELF. "checksum mismatch after download" is exactly
   * what happened and tells nobody what to do about it — pressed again it says the same thing, which
   * reads as the app being broken rather than as one damaged copy in the store. The repair is not
   * on this device (refetching gets the same bytes; that is what a content address means), so the
   * sentence names the device that can fix it. The prefix is what `badFetch` and the conflict loop
   * match on, so it stays first and stays stable. */
  /* "THE STORED COPY IS DAMAGED" WAS THE ONE EXPLANATION THIS CANNOT BE, and it sent people to the
   * wrong repair for as long as it was there.
   *
   * Every blob is checked against its own content address BEFORE it is decrypted (`_syncBlobBytes`
   * hashes what arrived and refuses a mismatch), and the seal is AES-GCM, which rejects the whole
   * message if a byte moved. So bytes that reach this point are provably the bytes that were
   * uploaded. If the file they assemble into disagrees with the record's checksum, the thing that is
   * wrong is the CHECKSUM — a number some device computed once and published — not the storage.
   *
   * And there was a way to compute it wrong: Android's digest looped `while (read(buf) > 0)`, and a
   * DocumentsProvider serves a file over a pipe, where a zero-length read is ordinary and is not the
   * end. It hashed a PREFIX and published that. Every other device then fetched the file perfectly,
   * checked it against a checksum describing its first few megabytes, and refused it — for ever,
   * while being told the store was damaged and that re-fetching was pointless. Both halves of that
   * were wrong: the store is fine, and the repair is a re-send from a device on a build whose hash
   * is right.
   *
   * The prefix is what `badFetch` and the conflict loop match on, so it stays first and stays
   * stable. */
  const BAD_COPY = 'checksum mismatch after download — refusing to write it. The bytes in the store '
                 + 'are intact (every piece was checked against its own address on the way in), so '
                 + 'what disagrees is the checksum recorded WITH them. Fix it from a device that '
                 + 'still holds this file: Check files there, and choose that its copy is correct — '
                 + 'that re-sends the bytes and records a fresh checksum for everybody';
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
  /* ONE FILE MUST HAVE ONE SPELLING ON EVERY PLATFORM, because the record's ADDRESS is
   * sha256(path). Two spellings of one name are two records, and two records for one file is the
   * duplication loop this engine has no defence against — each device downloads the other's
   * spelling, and the folder grows a second copy of everything with an accent in its name.
   *
   * macOS is where they diverge. HFS+ stored filenames decomposed and APFS still hands NFD back
   * through some APIs, so a file Linux and Windows both call "café.txt" (NFC, U+00E9) arrives from a
   * Mac as "cafe\u0301.txt" — visually identical, byte-different, a different sha256, a different
   * record. Normalising here means every device publishes the same spelling: a NO-OP on Windows and
   * Linux, which never decompose, so this changes nothing that exists today and closes the door
   * before a Mac joins. There is no Mac here to measure on; that is exactly why it is done at the
   * ONE boundary where a path enters the engine rather than sprinkled through the call sites.
   *
   * Case is deliberately NOT folded. macOS and Windows are case-INSENSITIVE while Linux is not, so
   * `Foo.txt` and `foo.txt` are one file on two platforms and two on the third — and folding would
   * make a Linux user lose one of two files they can legitimately hold. It is reported instead (see
   * `caseClash` below): the sweep says the collision exists and touches neither. */
  const normPath = (p) => { const s = String(p);
    try{ return s.normalize ? s.normalize('NFC') : s; }catch(_){ return s; } };

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
      unread.push(normPath(p));
      unreadWhy.push({ path: normPath(p), why: String((k && k.why) || 'unreadable') });
    } };
    const so = { hash: !!o.hash, excludes: o.excludes || [], maxBytes: 0 };
    if(typeof fs.scanPage !== 'function'){
      const r = await fs.scan(o.id, so);
      for(const p in (r && r.files) || {}) disk[normPath(p)] = compact(r.files[p]);
      note(r);
      return { disk, unread, unreadWhy };
    }
    let off = 0;
    for(;;){
      if(stopping()) return { disk, unread, unreadWhy };
      const page = await fs.scanPage(o.id, so, off, SCAN_PAGE);
      const files = (page && page.files) || {};
      const n = Object.keys(files).length;
      for(const p in files) disk[normPath(p)] = compact(files[p]);
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

    /* BIG FILES WERE STRICTLY ONE AT A TIME, AND THAT IS WHAT MADE A PHOTO LIBRARY TAKE HOURS.
     *
     * "Big" is anything over 2 MB, which is every photograph a camera has produced this decade — so
     * the serial path was not the exception it was written as, it was the whole folder. Measured
     * against a real store: one blob per second, sequential, because the wait is a round trip and
     * nothing else was allowed to be in flight during it. Sixty files a minute is then the ceiling
     * however fast the disk or the link is, and 64 photos really is "about 3 h left".
     *
     * The serial rule was protecting the HEAP, and that reason is real — each whole-file transfer
     * holds plaintext, ciphertext and a request body at once, and a Chromium renderer dies quietly
     * when it runs out ("windows app running out of memory while syncing"). But one at a time is far
     * more conservative than that requires: a handful of 6 MB files in flight is tens of megabytes,
     * not gigabytes. So big files get their own small lane count, under the SAME heap backpressure —
     * `_fat()` collapses every lane but the first the moment the heap is genuinely under strain,
     * which is the condition that mattered, rather than a size threshold standing in for it.
     *
     * CHUNKED files are excluded and stay strictly serial: those are the multi-gigabyte ones, they
     * stream a chunk at a time, and running several at once puts the part files, the wake lock and
     * the stall windows into competition for no gain — the link is already saturated by one. */
    const huge = big.filter(x => !!(x.entry && x.entry.chunks && x.entry.chunks.length));
    const wide = big.filter(x => !(x.entry && x.entry.chunks && x.entry.chunks.length));
    let bnext = 0, inflight = 0;
    const sizeOf = (x) => ((x.stat && x.stat.size) || (x.entry && x.entry.size) || 0) * BIG_COST;
    const blane = async (id) => {
      while(bnext < wide.length && !stopping()){
        if(id > 0 && _fat()){ await new Promise(r => setTimeout(r, 400)); continue; }
        const cost = sizeOf(wide[bnext]);
        /* Wait for room — unless nothing is in flight at all, in which case this file goes now
         * however large it is. Without that a file bigger than the whole budget would never start,
         * which is the one outcome worse than doing it slowly. */
        if(inflight > 0 && inflight + cost > BIG_BUDGET){
          await new Promise(r => setTimeout(r, 40));
          continue;
        }
        const item = wide[bnext++];
        inflight += cost;
        try{ await run(item, ++done, n); }
        finally{ inflight -= cost; }
        await journal.maybe();
      }
    };
    await Promise.all(Array.from({ length: Math.max(1, Math.min(BIG_LANES, wide.length)) },
                                 (_, i) => blane(i)));
    for(const item of huge){
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
      /* RESUME WITHOUT A WHOLE-FILE CHECKSUM — the case that needed it most and never had it.
       *
       * A part file is tied to nothing but its length, so resuming onto one left by a DIFFERENT
       * version of the same path splices two files together. With a `csum` the verify at the end
       * catches that; without one there was nothing to catch it, so the part file was discarded at
       * the start of every attempt and resume was simply off.
       *
       * And a large file added through Files → Synced folders has no csum ON PURPOSE: computing one
       * means holding the whole file, which is the single thing chunking exists to avoid. So the
       * files that could least afford to start over were the only ones that always did — reported
       * as a 2 GB .jex reaching 100% and going back to the beginning, again and again.
       *
       * What was missing is an IDENTITY for the part file, and it does not have to be a hash of the
       * content: it only has to say which record these bytes were being written for. That is one
       * small JSON beside the part file, written before the first byte and read before a resume —
       * `.pc-trash/.parts.json`, the name both platforms' scanners already skip, using `read` and
       * `write`, which every adapter already has. A part file whose identity is absent or different
       * is thrown away exactly as before; only a part file that provably belongs to THIS record is
       * resumed onto. */
      const _partsAt = '.pc-trash/.parts.json';
      const _readParts = async () => {
        try{ return JSON.parse(new TextDecoder().decode(await fs.read(o.id, _partsAt))) || {}; }
        catch(_){ return {}; }
      };
      const _writeParts = async (m) => {
        try{ await fs.write(o.id, _partsAt,
                            new TextEncoder().encode(JSON.stringify(m)), 0); }catch(_){}
      };
      const canVerify = !!(entry.csum && typeof fs.hashPart === 'function');
      const _id = idOf(entry);
      let have = 0;
      if(canVerify){ try{ if(fs.partSize) have = await fs.partSize(o.id, path); }catch(_){ have = 0; } }
      else if(_id && entry.cs > 0 && typeof fs.partSize === 'function'
              && typeof fs.read === 'function' && typeof fs.write === 'function'){
        let n = 0;
        try{ n = await fs.partSize(o.id, path); }catch(_){ n = 0; }
        const reg = await _readParts();
        if(n > 0 && n % entry.cs === 0 && reg[path] === _id) have = n;
        else if(n > 0){ try{ if(fs.discardPart) await fs.discardPart(o.id, path); }catch(_){} }
        if(reg[path] !== _id){ reg[path] = _id; await _writeParts(reg); }
      }
      else { try{ if(fs.discardPart) await fs.discardPart(o.id, path); }catch(_){} }
      const total = entry.size || 0;
      const pull = async (from) => {
        let got = from;
        /* THE CHUNK SIZE IS THE UPLOADER'S, AND THE RECEIVER HAS TO SURVIVE IT.
         *
         * `cs` is decided once, by whichever device stored the file — a desktop picks 16 MB. Every
         * byte handed to `writePart` then crosses the Capacitor bridge as base64 held as UTF-16, so
         * a 16 MB chunk is ~21 MB of base64 as ~42 MB of UTF-16, on top of the decrypted array and
         * whatever the platform copies on the far side: ~80 MB of renderer heap per chunk, over and
         * over. That is the WebView's render process being killed — the app rebuilds itself and the
         * screen "reloads" mid-sweep, with nothing thrown and nothing logged. The upload path has
         * always bounded this (Android chunks at 4 MB deliberately); the download path took whatever
         * it was given, so a file uploaded from a desktop was the one a phone could not receive.
         *
         * The wire chunk stays as it is — it is content-addressed and cannot be re-cut — but it
         * reaches the disk in pieces the platform sized itself. Costs nothing when they already
         * agree, which is every same-platform transfer. */
        const _piece = (fs.chunkBytes || 4 * 1024 * 1024);
        const _write = async (off, bytes) => {
          if(!bytes || bytes.length <= _piece) return fs.writePart(o.id, path, off, bytes);
          for(let at = 0; at < bytes.length; at += _piece){
            if(stopping && stopping()) throw new Error(STOPPED);
            await fs.writePart(o.id, path, off + at, bytes.subarray(at, Math.min(bytes.length, at + _piece)));
          }
        };
        await io.getParts(chunks, (off, bytes) => {
          if(stopping && stopping()) throw new Error(STOPPED);
          got = Math.max(got, off + ((bytes && bytes.length) || 0));
          if(total && onPercent) onPercent(Math.round(got / total * 100));
          return _write(off, bytes);
        }, entry.size, from, entry.cs || 0);
      };
      /* A PART FILE THAT IS ALREADY WHOLE MUST NOT BE FETCHED AGAIN. Resume works in whole chunks
       * — `have % cs === 0` — and a COMPLETE file almost never satisfies that, because the last
       * chunk is short. So a download that finished and then failed only its verification came back
       * here with the entire file on disk, failed the modulo, computed skip=0 and pulled all of it
       * a second time. On a 2 GB file that is the difference between a verification retry and an
       * unbounded loop. Nothing is trusted by skipping the pull: the verify below still has to
       * pass, and it is what decides whether these bytes are usable. */
      if(!(entry.size && have >= entry.size)) await pull(have);
      try{
        await verifyPart(fs, o, path, entry);
      }catch(e){
        /* AN UNANSWERED HASH IS NOT A FAILED ONE, and here the difference is 2 GB. `hashPart` reads
         * the whole part file back — on Android through SAF, minutes of I/O that can throw or be
         * killed with the renderer. Treated as a checksum failure it discards the part file and
         * downloads everything again, which on a large file never terminates: the bigger the file,
         * the likelier the hash fails, and the more there is to re-fetch. So an unanswered verify
         * keeps the bytes and reports; the next sweep finds a complete part file and (per the block
         * above) only has to hash it. */
        if(e && e.unverified) throw e;
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
      if(got !== entry.csum) throw new Error(BAD_COPY);
    }
    if(beforeCommit) await beforeCommit();
    return await fs.write(o.id, path, bytes, entry.mtime || 0);
  }

  /* "Could not compute the hash" and "the hash was wrong" are different answers with opposite
   * repairs: one keeps the bytes and asks again, the other throws them away. They were the same
   * exception. */
  const UNVERIFIED = 'downloaded, but this device could not read it back to check it — the bytes '
                   + 'are kept and the next sync will verify them';
  async function verifyPart(fs, o, path, entry){
    if(!entry.csum || typeof fs.hashPart !== 'function') return;
    let got = null, asked = true;
    try{ got = await fs.hashPart(o.id, path); }catch(_){ asked = false; }
    if(!asked || !got){ const e = new Error(UNVERIFIED); e.unverified = true; throw e; }
    /* THE HASH WE MEASURED TRAVELS WITH THE FAILURE. Without it the holder is asked "is your copy
     * bad, or was your checksum wrong?" and has nothing to answer with — see the heal path. */
    if(got !== entry.csum){ const e = new Error(BAD_COPY); e.got = got; throw e; }
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

    /* BREATHE. This walks every record in the folder, hashing each file through the platform
     * bridge — thousands of round trips with no await that ever yields to the page. On Android that
     * is a renderer holding its own thread flat out for minutes while the app looks frozen, and
     * Chromium's response to a renderer under that kind of pressure is to KILL it: the WebView is
     * rebuilt and the user sees the UI reload, in the middle of an operation, with nothing in any
     * log ("Deep Check, Verify, cause tablet to reload UI"). One macrotask yield every few files
     * costs nothing measurable against a hash and gives the page its thread back. */
    const breathe = () => new Promise(r => setTimeout(r, 0));
    const paths = Object.keys(state).filter(p => state[p] && !state[p].deletedAt);
    let i = 0;
    for(const p of paths){
      const entry = state[p];
      if((i % 16) === 0) await breathe();
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
      /* NO SILENT CAP. Checking the first 5,000 of 11,939 records reads as "covered everything"
       * and leaves the tail's missing bytes undiscovered — a Verify that then reports success is
       * the exact lie this feature keeps paying for. A HEAD is milliseconds on a LAN; the whole
       * folder is a minute or two, and a caller that truly wants a bound passes one. */
      const want = o.blobLimit ? paths.slice(0, o.blobLimit) : paths;
      /* A FEW AT A TIME, because this is twelve thousand round trips and every one of them is
       * mostly waiting. Serially it is long enough for a phone to lose its renderer before the
       * answer arrives — and the answer is what the whole screen is for. A HEAD changes nothing, so
       * there is no ordering to preserve; the only reason to bound it at all is the same one the
       * transfer lanes have, which is not drowning a node that is also serving everything else. */
      const LANE = 6;
      let j = 0, at = 0;
      const one = async (p) => {
        const e = state[p];
        const ids = e.chunks && e.chunks.length ? e.chunks : (e.sha ? [e.sha] : []);
        tick({ phase: 'checking the store', path: p, i: ++j, n: want.length });
        for(const id of ids){
          let there = null;
          try{ there = await io.hasBlob(id); }catch(_){ there = null; }
          if(there === false){ out.missingBytes.push(p); return; }
          if(there === null){ out.unverified.push(p); return; }
        }
      };
      await Promise.all(Array.from({ length: Math.min(LANE, want.length) }, async () => {
        for(;;){
          const k = at++;
          if(k >= want.length) return;
          await one(want[k]);
          if((k % 16) === 0) await breathe();
        }
      }));
    }
    return out;
  }

  const API = { sweep, verify, scan, idOf, SCAN_PAGE, LANES };
  root.PCSyncExec = API;
  if(typeof module !== 'undefined' && module.exports) module.exports = API;
})(typeof globalThis !== 'undefined' ? globalThis : this);
