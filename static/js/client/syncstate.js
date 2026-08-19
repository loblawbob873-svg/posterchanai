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

  const { same, excluder, conflictPath, trashPath, MTIME_SLOP } = P;

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

    const out = { fetch:[], send:[], trash:[], tombstone:[], keepBoth:[], settle:[],
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
          out.trash.push({ path, v: versionOf(R), entry: R, to: trashPath(path, now),
                           why: 'deleted elsewhere' });
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
          out.trash.push({ path, v: versionOf(R), entry: R, to: trashPath(path, now),
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
  const FLOOR = 20;
  const MASS_CAP = 100;   // absolute per-sweep trash ceiling for unattended sweeps

  function check(plan, ctx){
    const c = ctx || {}, p = plan || {}, out = [];

    const settled = p.settle.filter(s => s.why === 'same content both sides').length;
    // LIVE survivors only — tombstones everyone agrees on are ballast, not kept files.
    const keep = num(p.unchanged) - num(p.settledGone)
               + p.fetch.length + p.send.length + p.keepBoth.length + settled;

    // A SHORT LIST IS A DELETE ORDER: never trash more than survives the sweep.
    if(p.trash.length >= FLOOR && p.trash.length > keep){
      out.push({ kind:'massTrash', n: p.trash.length, keep,
                 why: 'this sweep would move ' + p.trash.length + ' files to the trash and keep ' + keep });
    }
    // And an absolute cap, because proportional is not enough on a big folder.
    if(!c.allowMassTrash && p.trash.length > MASS_CAP){
      out.push({ kind:'massTrash', n: p.trash.length, keep,
                 why: 'this sweep would move ' + p.trash.length + ' files to the trash — more than '
                    + MASS_CAP + ' needs a deliberate delete, not an unattended sweep' });
    }
    // The same question pointing outwards: this device telling every other one to delete.
    if(p.tombstone.length >= FLOOR && p.tombstone.length > keep){
      out.push({ kind:'massTombstone', n: p.tombstone.length, keep,
                 why: 'this sweep would tell your other devices to delete ' + p.tombstone.length
                      + ' files and keep ' + keep });
    }
    if(!c.allowMassTrash && p.tombstone.length > MASS_CAP){
      out.push({ kind:'massTombstone', n: p.tombstone.length, keep,
                 why: 'this sweep would tell your other devices to delete ' + p.tombstone.length
                    + ' files — more than ' + MASS_CAP + ' needs a deliberate delete' });
    }
    // An absolute floor on resurrections: a restored backup arrives beside thousands of ordinary
    // uploads, so no ratio can see it.
    const res = p.send.filter(s => s.resurrect).length;
    if(res >= FLOOR) out.push({ kind:'massResurrect', n: res,
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
      if(v.kind === 'massTrash') out.trash = [];
      else if(v.kind === 'massTombstone') out.tombstone = [];
      else if(v.kind === 'massResurrect') out.send = out.send.filter(s => !s.resurrect);
      else if(v.kind === 'blocked'){
        out.fetch = out.fetch.filter(f => f.path !== v.path);
        out.keepBoth = out.keepBoth.filter(f => f.path !== v.path);
      }
    }
    return out;
  }

  const API = { plan, check, apply, versionOf, bump, diskChanged, recordAhead, FLOOR, MASS_CAP };
  root.PCSyncState = API;
  if(typeof module !== 'undefined' && module.exports) module.exports = API;
})(typeof globalThis !== 'undefined' ? globalThis : this);
