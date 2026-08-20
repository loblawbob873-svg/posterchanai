/* Folder sync — the third engine, and the reason it is the third.
 *
 * THE FOLDER IS ONE RECORD PER FILE. Not a document per device, not a merge of views, not a shared
 * manifest: every file has exactly one versioned record, the server refuses a write that is not
 * strictly newer than what it holds, and every device compares that record set against its own
 * disk. That is the whole design — the one every mature sync tool (Nextcloud, Dropbox, Syncthing's
 * global index) converges on, and the one the user finally ordered after two document-shaped
 * engines each found a new way for one stale read to speak for thousands of files at once.
 *
 * What per-file records make STRUCTURAL rather than guarded:
 *
 *   - NO READ CAN EMPTY THE FOLDER. There is no document whose absence, emptiness or staleness
 *     describes more than one file. A record that fails to load is one file the sweep does not
 *     touch, in the safe direction (nothing).
 *   - A DELETION IS A RECORD, NEVER AN ABSENCE. Deleting publishes a tombstone record at a higher
 *     version, carrying the file's last address so any device can restore it. A path with no
 *     record is a path nobody has said anything about.
 *   - TWO DEVICES CANNOT SILENTLY OVERWRITE EACH OTHER. Every write is a compare-and-swap on that
 *     one file's version, decided by the server under a lock. The loser is REFUSED, sees the
 *     winner's record next sweep, and resolves it as a conflict — both copies survive.
 *   - A DEVICE'S PAST LIFE CANNOT HAUNT IT. Re-adding a folder starts a new ERA (a plain integer on
 *     the pair); records from before the era are dead the moment it changes. The remove-and-re-add
 *     ghosts — 373 conflicts on a fresh phone — cannot be expressed.
 *
 * PURE. No I/O, no DOM, no network. It takes the record set, the disk scan and this device's
 * journal, and returns a PLAN; the executor moves bytes and decides nothing. Everything that can
 * get the ANSWER wrong is here, where it runs under node against generated folders; everything
 * that can destroy a FILE is in the executor, behind the platform adapter.
 *
 * Rules carried over because they were never the bug, each with its own tests:
 *   - conflicts keep both copies, delete loses to edit both ways, nothing is deleted in place;
 *   - a change is detected against the JOURNAL (what this device last applied), never against a
 *     published mtime — SAF hands downloads its own timestamps;
 *   - a journal that answers for less than half the folder forces a content hash (the dirty join:
 *     identical bytes settle, only real divergence conflicts);
 *   - the plan is CHECKED before it runs, in one place the executor cannot skip.
 */
(function(root){
  'use strict';

  const P = (root.PCFolderSync) || (typeof require === 'function' ? require('./foldersync.js') : null);
  if(!P) throw new Error('syncstate.js needs foldersync.js');

  const { same, excluder, conflictPath, MTIME_SLOP } = P;

  const num = (x) => (typeof x === 'number' && isFinite(x)) ? x : 0;
  const live = (e) => !!e && !e.deletedAt;
  const gone = (e) => !e || !!e.deletedAt;
  const versionOf = (e) => e ? num(e.v) : 0;

  /* Did the file on disk change since this device last applied something to it? Verbatim from the
   * second engine — this rule was never wrong. Against `index.local`, never a published entry,
   * because a downloaded file gets whatever last-modified the platform hands out. */
  function diskChanged(L, idx){
    const had = idx && idx.local;
    if(!L && !had) return false;
    if(!L) return !!(idx && !idx.deletedAt);        // it was here and is not: deleted locally
    if(!had) return true;                           // here, and nothing applied: new to this device
    if(L.csum && had.csum) return L.csum !== had.csum;
    return L.size !== had.size || Math.abs(num(L.mtime) - num(had.mtime)) > MTIME_SLOP;
  }

  /* Did the folder's record move past what this device applied? STRICTLY AHEAD, never merely
   * different: the journal legitimately runs ahead of the record set for the length of one publish
   * (a sweep that uploaded and then failed to publish, a crash between the two), and reading that
   * as "changed elsewhere" is the silent revert the second engine had to learn the hard way.
   * What the journal knows and the folder does not is OURS TO PUBLISH, never theirs to teach us. */
  const recordAhead = (R, idx) => !!R && versionOf(R) > versionOf(idx);

  /** The version to publish for a path: past everything either side has seen. */
  function bump(R, idx){ return Math.max(versionOf(R), versionOf(idx)) + 1; }

  /* ---- the plan --------------------------------------------------------------------------------
   *
   * state:  {path: record} — the folder, one record per file, tombstones included. THE record set:
   *         the caller guarantees it was read strictly (a failed read throws before we run).
   * disk:   {path: {size, mtime, csum?}} from the scan.
   * index:  {path: entry+local} — this device's journal of what it applied.
   */
  function plan(opts){
    const o = opts || {};
    const disk = o.disk || {}, index = o.index || {}, state = o.state || {};
    const me = o.device || 'this device';
    const now = num(o.now);

    const out = { fetch:[], send:[], remove:[], tombstone:[], keepBoth:[], settle:[],
                  unchanged:0, excluded:0, settledGone:0 };
    const isExcluded = excluder(o.excludes || []);
    const paths = new Set([...Object.keys(disk), ...Object.keys(state), ...Object.keys(index)]);

    for(const path of [...paths].sort()){
      if(isExcluded(path)){ out.excluded++; continue; }

      const L = disk[path] || null;
      const R = state[path] || null;
      const idx = index[path] || null;

      /* AN ADDRESS-LESS RECORD, HELD HERE, IS A SEND — an upload that died between the record and
       * its bytes leaves every other device asking "where is this stored" for ever. Whoever holds
       * a local copy re-publishes it, address included. */
      if(L && live(R) && !R.sha && !(R.chunks && R.chunks.length)){
        out.send.push({ path, v: bump(R, idx), stat: L,
                        why: 'the shared record names no storage — re-publishing from this copy' });
        continue;
      }

      /* A RECORD THE FOLDER LOST IS RESTORED BY WHOEVER HOLDS THE FILE. The journal says this
       * device applied it, the disk still has it, and the folder no longer says anything — under
       * per-device views a lost document was put back whole; per file, the record is put back per
       * file. Absent this, the path sits "unchanged" here for ever while no other device can learn
       * it exists. (A lost TOMBSTONE record needs no restoring: a path with no record and no file
       * is a path nobody claims.) */
      if(!R && idx && !idx.deletedAt && L && !diskChanged(L, idx)){
        out.send.push({ path, v: bump(null, idx), stat: L,
                        why: 'the folder has no record of this file — restoring it from this copy' });
        continue;
      }

      const here = diskChanged(L, idx);
      const there = recordAhead(R, idx);

      if(!here && !there){ out.unchanged++; if(R && !live(R)) out.settledGone++; continue; }

      /* ---- the folder moved and this device did not: apply it. */
      if(there && !here){
        /* Bytes we already hold are ADOPTED, not downloaded. This is our own publish coming back
         * (the record runs one version ahead of the journal for the length of a checkpoint) and
         * it is another device uploading a file we already have — both settle by content. */
        if(live(R) && L && same(L, R)){
          out.settle.push({ path, v: versionOf(R), entry: R, why: 'same content both sides' });
        } else if(live(R)){
          out.fetch.push({ path, v: versionOf(R), entry: R, from: R.by,
                           why: idx ? 'changed elsewhere' : 'new elsewhere' });
        } else if(L){
          out.remove.push({ path, v: versionOf(R), entry: R, why: 'deleted elsewhere' });
        } else {
          out.settle.push({ path, v: versionOf(R), entry: R, why: 'already gone here' });
        }
        continue;
      }

      /* ---- this device moved and the folder did not: publish it. */
      if(here && !there){
        if(L) out.send.push({ path, v: bump(R, idx), stat: L,
                              why: idx ? 'changed here' : 'new here' });
        else out.tombstone.push({ path, v: bump(R, idx), why: 'deleted here' });
        continue;
      }

      /* ---- both moved. The first two are not conflicts at all. */
      if(!L && gone(R)){
        out.settle.push({ path, v: versionOf(R), entry: R, why: 'deleted on both' });
        continue;
      }
      /* The same edit twice, or the same file copied onto both devices — what stops a photo
       * library seeded from one camera duplicating itself on its first sweep. same(), which uses
       * content when a checksum exists on both sides and size+mtime otherwise. */
      if(L && live(R) && same(L, R)){
        out.settle.push({ path, v: versionOf(R), entry: R, why: 'same content both sides' });
        continue;
      }
      // Delete loses to edit, both ways.
      if(!L && live(R)){
        out.fetch.push({ path, v: versionOf(R), entry: R, from: R.by,
                         why: 'deleted here but edited elsewhere — keeping the edit' });
        continue;
      }
      if(L && gone(R)){
        /* A JOINING DEVICE'S UNCHANGED COPY OBEYS THE DELETION. Tombstones keep the deleted
         * content's csum (for restore), and a journal-less join hashes its scan — so when this
         * local copy IS the bytes that were deliberately deleted, the deletion applies here too,
         * instead of resurrecting on every device that ever held the file. Only an actual edit
         * (different content, or content we cannot compare) wins over a delete. */
        if(R && R.csum && L.csum && L.csum === R.csum){
          out.remove.push({ path, v: versionOf(R), entry: R,
                            why: 'deleted elsewhere — this copy is the deleted version' });
        } else {
          out.send.push({ path, v: bump(R, idx), stat: L, resurrect: true,
                          why: 'deleted elsewhere but edited here — keeping the edit' });
        }
        continue;
      }
      // Divergent bytes: keep both, the incoming copy takes the name, ours is renamed beside it.
      out.keepBoth.push({ path, v: versionOf(R), entry: R,
                          keepAs: conflictPath(path, R.by || 'another device',
                                               num(R.mtime) || num(R.deletedAt) || now),
                          why: 'edited on both — the incoming copy takes the name, yours is renamed' });
    }
    return out;
  }

  /* ---- the check, which is what makes it safe ---------------------------------------------------
   * Verbatim guards from the second engine, minus the per-device-view ones (there are no views to
   * be partial: a record set that could not be read throws before plan() runs). Advisory verdicts
   * except `fatal`, which nothing may override. */
  /* ---- WHAT IS LEFT OF THE GUARDS, AND WHY SO LITTLE -------------------------------------------
   *
   * There used to be a floor, a ratio and a cap in each direction: no more than N deletions, never
   * more than survives, never more than the server's backstop, and a dialog whenever any of them
   * fired. Every one was added after a real loss and every one was locally correct. Together they
   * were a system nobody could predict — including the person who wrote them, who kept finding the
   * failures by measuring the relay afterwards. They also failed in the way guards fail: the bands
   * BETWEEN them were silent, they were asymmetric (the device holding the only good copies was the
   * one forbidden to act), and a dialog that fires often enough is a dialog people confirm.
   *
   * They are gone, because the thing they were approximating is now checked directly. A deletion
   * removes the local copy only when the STORE IS CONFIRMED TO HOLD THOSE BYTES (see the executor),
   * and the bytes stay there — one account-wide trash on the server, restorable to every device.
   * So a wrong bulk deletion costs a restore, not a file, and a number no longer has to stand in
   * for "is this safe".
   *
   * ONE RULE SURVIVES, and it is not a floor: a device that can see NONE of the files it knows
   * about has lost sight of the folder — a revoked grant, an unmounted volume, a folder picked at
   * the wrong path — and that is a different statement from "somebody emptied it". It is fatal
   * rather than confirmable, because there is no answer a person could give that makes it right. */
  const FLOOR = 20;       // kept only for the resurrect rule below and for the card's wording
  const MASS_CAP = 100;   // the SERVER's own tombstone backstop, exported so one number is quoted

  function check(plan, ctx){
    const c = ctx || {}, p = plan || {}, out = [];

    const settled = p.settle.filter(s => s.why === 'same content both sides').length;
    // LIVE survivors only — tombstones everyone agrees on are ballast, not kept files.
    const keep = num(p.unchanged) - num(p.settledGone)
               + p.fetch.length + p.send.length + p.keepBoth.length + settled;

    /* A DEVICE HOLDING NOTHING MAY NOT DELETE THE FOLDER. FATAL — never offered, never confirmable.
     *
     * Every other guard here asks. This one refuses, because there is no answer a person could give
     * that makes it right: a scan that found NOTHING while the journal knows about hundreds of files
     * is not a folder somebody emptied, it is a device that has lost sight of one — a revoked grant,
     * an unmounted volume, a folder picked at the wrong path, a phone whose copy was cleared. The
     * moment such a device is allowed to speak, one tap deletes the folder everywhere.
     *
     * Reported twice in one evening, from two emptied devices, offering to delete 966 files and then
     * 107 — against files that were sitting on the desktop the whole time. The proof-of-absence
     * probe would have held most of them back, and the mass floor would have asked first, but ASKING
     * is the bug: it puts a destructive default one tap away and hands the user a decision the
     * evidence cannot support. Nothing survives the sweep, so there is nothing to weigh it against.
     *
     * Deliberately `keep === 0`, not a ratio: a device that still holds SOMETHING has a real folder
     * and a real opinion about it, and the proportional rule above already covers "removes more than
     * it keeps". This is only the case where the device's own evidence is empty. */
    if(p.tombstone.length >= FLOOR && keep === 0){
      out.push({ kind:'massTombstone', rule:'emptyDevice', fatal:true, n: p.tombstone.length, keep,
                 why: 'this device can see none of the ' + p.tombstone.length + ' files it knows '
                    + 'about — that is a folder it has lost sight of, not one you emptied, so it '
                    + 'will not tell your other devices to delete anything' });
    }
    // An absolute floor on resurrections: a restored backup arrives beside thousands of ordinary
    // uploads, so no ratio can see it. This is the ONE remaining count-based rule, and it survives
    // for a reason the deletion rules did not: putting files back is not made safe by the store
    // holding a copy — the files are already safe — so there is nothing here for a direct check to
    // replace it with, and republishing a whole restored backup over everybody's folders is a real
    // event somebody should mean.
    const res = p.send.filter(s => s.resurrect).length;
    if(res >= FLOOR) out.push({ kind:'massResurrect', rule:'floor', n: res,
                                why: 'this sweep would republish ' + res
                                     + ' files your other devices deleted' });

    // A path that another live record sits under cannot be written as a file on any device.
    const g = c.state || {};
    for(const a of p.fetch){
      const pre = a.path + '/';
      for(const q in g){
        if(q !== a.path && live(g[q]) && q.indexOf(pre) === 0){
          out.push({ kind:'blocked', fatal:true, path: a.path,
                     why: '“' + a.path + '” is a file here and a folder on another device' });
          break;
        }
      }
    }

    /* TWO NAMES, ONE FILE, ON A FOLDING FILESYSTEM. `Photo.jpg` and `photo.jpg` are two records —
     * legitimate, distinct files on Linux — and ONE file on Windows, macOS and most of Android. A
     * device that folds and fetches both writes them over each other: each sweep then reads the
     * survivor as "changed here" for one record, republishes, and the two records climb versions
     * against each other for ever — the flip-flop every mixed-platform sync system has had to
     * learn about (macOS adds NFC/NFD normalisation to the same trap, so the fold normalises
     * first). On a folding device only the WINNER (highest version, then first name — the same
     * determinism rule as everything else) may be written; the rest are refused fatally and NAMED,
     * because the fix is a human renaming a file, not an engine guessing which twin to destroy. */
    if(c.caseFolds !== false){
      const fold = (x) => { try{ return String(x).normalize('NFC').toLowerCase(); }
                            catch(_){ return String(x).toLowerCase(); } };
      const groups = {};
      for(const q in g) if(live(g[q])) (groups[fold(q)] = groups[fold(q)] || []).push(q);
      const writes = new Set(p.fetch.concat(p.keepBoth).map(a => a.path));
      for(const f in groups){
        const twins = groups[f];
        if(twins.length < 2) continue;
        twins.sort((a, b) => (versionOf(g[b]) - versionOf(g[a])) || (a < b ? -1 : 1));
        for(const q of twins.slice(1)){
          if(!writes.has(q)) continue;
          out.push({ kind:'blocked', fatal:true, path: q,
                     why: '“' + q + '” and “' + twins[0] + '” are the same name on this device — '
                        + 'rename one of them where it was created' });
        }
      }
    }
    return out;
  }

  /* The plan with what a refusal forbids taken out — and nothing else. REFUSING SUPPRESSES ONE
   * KIND OF ACTION, NEVER THE SWEEP: a guard that aborts everything is the same bug with its sign
   * flipped (it is what stopped the contacts sweep syncing at all). */
  function apply(plan, verdicts, allowed){
    const ok = new Set(allowed || []);
    let out = Object.assign({}, plan);
    for(const v of verdicts || []){
      if(!v.fatal && ok.has(v.kind)) continue;
      if(v.kind === 'massTombstone') out.tombstone = [];
      else if(v.kind === 'massResurrect') out.send = out.send.filter(s => !s.resurrect);
      else if(v.kind === 'blocked'){
        out.fetch = out.fetch.filter(f => f.path !== v.path);
        out.keepBoth = out.keepBoth.filter(f => f.path !== v.path);
      }
    }
    return out;
  }

  /* HOW MANY TIMES A REPAIR HAS BEEN TRIED, and it is a decision rather than bookkeeping — which is
   * why it lives here with the rest of them.
   *
   * A refused copy is remembered by its STORAGE ADDRESS, so a fresh upload lifts the refusal by
   * itself. That is right when the re-sent bytes are good and a trap when they are not: a device
   * whose checksum of its own file is wrong agrees with itself, finds no fault on being asked to
   * verify, re-sends the same bytes under a new address, and clears every other device's memory of
   * the last one. Download, fail, flag, re-send, download — measured at sixteen rounds in ninety
   * minutes on one multi-gigabyte file, with nothing in the design able to end it.
   *
   * So the ROUNDS are counted across addresses. A NEW address failing the same checksum is round
   * n+1; anything else starts again at one, because it is a different kind of failure and says
   * nothing about the last. Three rounds is a statement about the sender, not the bytes.
   *
   * Both shapes are in the wild — an older build stored a bare address string — and a string is
   * simply round one, so an upgrade cannot read as an exhausted repair. */
  const BAD_ROUNDS = 3;
  function mergeBadFetch(cur, add){
    const out = Object.assign({}, cur || {});
    for(const p in (add || {})){
      const was = out[p], now = add[p];
      if(was === now) continue;
      const wasId = (was && typeof was === 'object') ? was.id : was;
      const nowId = (now && typeof now === 'object') ? now.id : now;
      /* Three cases, and the middle one is the one that is easy to get wrong. A NEW address failing
       * the same way is the next round. The SAME address failing again is the same round — it is one
       * copy, re-read — and resetting the count there would let a device that fails twice per sweep
       * hold the counter at one for ever. A different KIND of failure starts again: bytes the store
       * has lost say nothing about anybody's checksum, and counting them together would abandon a
       * file over a media server having one bad minute. */
      const bothChecksum = !!(now && now.why === 'checksum'
                              && (!was || typeof was !== 'object' || was.why === 'checksum'));
      const had = (was && typeof was === 'object' && +was.rounds) || (was ? 1 : 0);
      const rounds = !bothChecksum ? 1
                   : !wasId ? 1
                   : (wasId === nowId ? Math.max(1, had) : had + 1);
      out[p] = (now && typeof now === 'object') ? Object.assign({}, now, { rounds }) : now;
    }
    return out;
  }
  /** Has this path exhausted its repair? Never for a person who pressed the button themselves. */
  const repairExhausted = (v, manual) => !!(v && typeof v === 'object' && v.why === 'checksum'
                                            && (+v.rounds || 0) >= BAD_ROUNDS && !manual);

  const API = { plan, check, apply, versionOf, bump, diskChanged, recordAhead,
                mergeBadFetch, repairExhausted, BAD_ROUNDS, FLOOR, MASS_CAP };
  root.PCSyncState = API;
  if(typeof module !== 'undefined' && module.exports) module.exports = API;
})(typeof globalThis !== 'undefined' ? globalThis : this);
