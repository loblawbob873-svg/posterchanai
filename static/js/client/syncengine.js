/* Folder sync — the reconciler. Ordinary two-way file sync, the shape Syncthing and Nextcloud use.
 *
 * PURE. No I/O, no DOM, no Nostr, no Blossom: it takes what the devices have published, what is on
 * this disk, and what this device has already applied, and returns a PLAN. Everything that can get
 * the ANSWER wrong is here, where it runs under node against thousands of generated cases;
 * everything that can destroy a FILE is in the executor, behind a platform adapter.
 *
 * ---- THERE IS NO SHARED DOCUMENT -------------------------------------------------------------
 *
 * Every device publishes its OWN view of the folder, and nothing else ever writes it:
 *
 *     pcai:sync:<pair>:<device>        one writer, for ever
 *
 * The folder is the MERGE of those views. That is the whole design, and it is what makes the two
 * things that kept going wrong impossible rather than guarded:
 *
 *   - TWO DEVICES SYNCING AT ONCE cannot lose each other's work. The old shape was one document
 *     every device read, edited and wrote back, which is last-writer-wins on the record of whether
 *     your files exist; it needed a merge on save, a re-read per checkpoint, and a server-side
 *     collapse guard, and it still lost writes. A single-writer document needs none of that.
 *   - NO ONE DOCUMENT CAN EMPTY THE FOLDER. A view that is missing, unreadable or wrong is one
 *     device's opinion, not the truth — the others still assert their files, and a delete requires a
 *     positive tombstone from the device that made it.
 *
 * ---- VERSIONS, NOT TIMESTAMPS ------------------------------------------------------------------
 *
 * Each entry carries `v`, a counter a device raises when it publishes a change to that path, and
 * `by`, the device that did. A delete is an ordinary version that happens to be a tombstone. Two
 * devices publishing the same version for a path is exactly what a concurrent edit looks like, and
 * it is resolved the same way on every device, by rule, without anybody coordinating.
 *
 * This is what removes the clock heuristics. "Was the deletion later than this copy" cannot be asked
 * of two machines' clocks and reliably answered; the times it was answered wrong it either emptied a
 * folder or refilled one.
 *
 * Entries written before versions existed carry none: `v` is 0 on both sides and the comparison
 * falls back to content, exactly as before, so an old pair keeps working and upgrades itself one
 * publish at a time.
 *
 * ---- AND THE RULES THAT ARE NOT NEGOTIABLE -----------------------------------------------------
 *
 *   - conflicts are never resolved by picking a winner: both copies survive;
 *   - delete loses to edit, in both directions;
 *   - nothing is deleted in place — a local delete is a move into `.pc-trash/<date>/`;
 *   - an exclusion means "stop looking at this", never "delete it";
 *   - a plan is CHECKED before it runs, in one place the executor cannot skip, because every rule
 *     above decides ONE path and a bad input produces ten thousand identical decisions.
 */
(function(root){
  'use strict';

  const P = (root.PCFolderSync) || (typeof require === 'function' ? require('./foldersync.js') : null);
  if(!P) throw new Error('syncengine.js needs foldersync.js');

  /* Carried over verbatim and deliberately: content identity, the exclusion matcher, conflict and
   * trash naming, the battery policy. None of them has ever been the bug and each has its own
   * tests — rewriting them would be risk taken for nothing. */
  const { same, excluder, conflictPath, trashPath, MTIME_SLOP } = P;
  const live = (e) => !!e && !e.deletedAt;      // an entry that names actual bytes
  const gone = (e) => !e || !!e.deletedAt;      // absent, or a tombstone

  const num = (x) => (typeof x === 'number' && isFinite(x)) ? x : 0;
  const versionOf = (e) => e ? num(e.v) : 0;
  const stampOf = (e) => e ? (num(e.deletedAt) || num(e.mtime)) : 0;

  /* Which of two claims about one path is the later one.
   *
   * Version first. Then the entry's own timestamp, which is what orders the entries of a pair that
   * has not published a version yet. Then the device id, which decides nothing meaningful and is
   * here so that every device reaches the SAME answer — a merge that is not deterministic is a
   * folder that flickers between two states as the devices take turns. */
  function later(a, b){
    const va = versionOf(a.entry), vb = versionOf(b.entry);
    if(va !== vb) return va > vb ? a : b;
    const sa = stampOf(a.entry), sb = stampOf(b.entry);
    if(sa !== sb) return sa > sb ? a : b;
    return String(a.by) > String(b.by) ? a : b;
  }

  /* The folder, as the devices between them describe it.
   *
   * `views` is {device: {path: entry}}. The result is one entry per path plus, where two devices
   * published the SAME version with different content, the losing claim — which is what a
   * concurrent edit looks like and is the input the conflict rule needs.
   */
  function merge(views){
    const global = {}, rivals = {}, by = {};
    const devices = Object.keys(views || {}).sort();
    for(const dev of devices){
      const view = views[dev] || {};
      for(const path in view){
        const claim = { entry: view[path], by: dev };
        const cur = global[path] ? { entry: global[path], by: by[path] } : null;
        if(!cur){ global[path] = claim.entry; by[path] = dev; continue; }
        const win = later(cur, claim);
        const lose = win === cur ? claim : cur;
        global[path] = win.entry; by[path] = win.by;
        /* A RIVAL IS A CONCURRENT EDIT, not merely an older copy. Same version, different content:
         * two devices changed this path without either seeing the other. An entry that simply lost
         * on version is not a rival — it is out of date, and it has already been superseded. */
        if(versionOf(win.entry) === versionOf(lose.entry) && !same(win.entry, lose.entry)){
          rivals[path] = { entry: lose.entry, by: lose.by };
        }
      }
    }
    return { global, rivals, by, devices };
  }

  /* Did the file on disk change since this device last applied something to it?
   *
   * Against `index.local` — what the file looked like when we applied — and never against a
   * published entry, because a downloaded file gets whatever last-modified the platform gives it
   * (Android's SAF assigns its own), so comparing the two reports every downloaded file as edited on
   * every sweep, for ever.
   *
   * `csum` when both sides have one; size and mtime otherwise, which is what makes an ordinary sweep
   * cost a stat per file rather than a read of the whole folder. */
  function diskChanged(L, idx){
    const had = idx && idx.local;
    if(!L && !had) return false;
    if(!L) return !!(idx && !idx.deletedAt);        // it was here and is not: deleted locally
    if(!had) return true;                           // here, and nothing applied: new to this device
    if(L.csum && had.csum) return L.csum !== had.csum;
    return L.size !== had.size || Math.abs(num(L.mtime) - num(had.mtime)) > MTIME_SLOP;
  }

  /* Did the folder's own record change since this device last applied it?
   *
   * ABSENCE IS NOT NEWS, and that is the rule the whole per-device design rests on. A path nobody
   * currently claims means exactly that: no device published anything about it. It does NOT mean
   * deleted — a deletion is a positive tombstone, published by the device that made it, and it
   * survives in that device's view until every device has seen it.
   *
   * The old shape could not tell those apart, because there was one document and a path missing from
   * it was the only way a delete could look. Which is why a document that failed to load, or came
   * back empty, or lost a write to a concurrent save, read as "every file you have was deleted" —
   * and the folder went into the trash, correctly, per path, by rules that were each right.
   */
  function viewChanged(R, idx){
    if(!R) return false;                       // nobody said anything about this path
    const vr = versionOf(R), vi = versionOf(idx);
    if(vr || vi) return vr !== vi;
    if(!idx) return true;                      // an entry we have never applied is news
    return !same(R, idx);
  }

  /** The version to publish for a path: past everything either side has seen. */
  function bump(R, idx){ return Math.max(versionOf(R), versionOf(idx)) + 1; }

  /* ---- the reconcile --------------------------------------------------------------------------- */
  function reconcile(opts){
    const o = opts || {};
    const disk = o.disk || {}, index = o.index || {};
    const global = o.global || {}, rivals = o.rivals || {}, by = o.by || {};
    const me = o.device || 'this device';
    const now = num(o.now);

    const plan = { fetch:[], send:[], trash:[], tombstone:[], keepBoth:[], settle:[],
                   unchanged:0, excluded:0 };
    const isExcluded = excluder(o.excludes || []);
    const paths = new Set([...Object.keys(disk), ...Object.keys(global), ...Object.keys(index)]);

    for(const path of [...paths].sort()){
      if(isExcluded(path)){ plan.excluded++; continue; }

      const L = disk[path] || null;
      const R = global[path] || null;
      const idx = index[path] || null;
      const rival = rivals[path] || null;

      /* A CONCURRENT EDIT BY TWO OTHER DEVICES IS RESOLVED THE SAME WAY EVERYWHERE.
       *
       * Both claims are content-addressed and both sets of bytes are stored, so every device can
       * carry out the identical repair without asking anyone: the winner keeps the name, the loser
       * is written beside it under a conflict name. Deterministic, so three devices do not each
       * pick a different winner and then argue about it for ever. */
      if(rival && live(R) && live(rival.entry)){
        plan.keepBoth.push({ path, v: versionOf(R), entry: R, rival: rival.entry,
                             keepAs: conflictPath(path, rival.by, stampOf(rival.entry) || now),
                             why: 'two devices changed this at the same time — both copies kept' });
        continue;
      }

      const here = diskChanged(L, idx);
      const there = viewChanged(R, idx);

      if(!here && !there){ plan.unchanged++; continue; }

      /* ---- the folder moved and this device did not: apply it. */
      if(there && !here){
        if(live(R)) plan.fetch.push({ path, v: versionOf(R), entry: R, from: by[path],
                                      why: idx ? 'changed elsewhere' : 'new elsewhere' });
        else if(L) plan.trash.push({ path, v: versionOf(R), entry: R, to: trashPath(path, now),
                                     why: 'deleted elsewhere' });
        else plan.settle.push({ path, v: versionOf(R), entry: R, why: 'already gone here' });
        continue;
      }

      /* ---- this device moved and the folder did not: publish it. */
      if(here && !there){
        if(L) plan.send.push({ path, v: bump(R, idx), stat: L,
                               why: idx ? 'changed here' : 'new here' });
        else plan.tombstone.push({ path, v: bump(R, idx), why: 'deleted here' });
        continue;
      }

      /* ---- both moved. The first two are not conflicts at all. */

      if(!L && gone(R)){
        plan.settle.push({ path, v: versionOf(R), entry: R, why: 'deleted on both' });
        continue;
      }

      /* The same edit twice, or the same file copied onto both devices. This is what stops a photo
       * library seeded from one camera duplicating itself on its first sweep. same(), never a bare
       * hash comparison: an ordinary sweep does not hash, so a rule that needs one decides
       * "divergent" for every path it sees. */
      if(L && live(R) && same(L, R)){
        plan.settle.push({ path, v: versionOf(R), entry: R, why: 'same content both sides' });
        continue;
      }

      // Delete loses to edit, both ways.
      if(!L && live(R)){
        plan.fetch.push({ path, v: versionOf(R), entry: R, from: by[path],
                          why: 'deleted here but edited elsewhere — keeping the edit' });
        continue;
      }
      if(L && gone(R)){
        plan.send.push({ path, v: bump(R, idx), stat: L, resurrect: true,
                         why: 'deleted elsewhere but edited here — keeping the edit' });
        continue;
      }

      // Divergent bytes: keep both, the incoming copy takes the name.
      /* Named after the device that WROTE the entry when it says so, and after the document it came
       * from otherwise. The entry's own claim is the better label: it survives being copied between
       * views, and it is what somebody reads off a filename months later. */
      plan.keepBoth.push({ path, v: versionOf(R), entry: R,
                           keepAs: conflictPath(path, R.by || R.device || by[path] || 'another device',
                                                stampOf(R) || now),
                           why: 'edited on both — the incoming copy takes the name, yours is renamed' });
    }
    return plan;
  }

  /* ---- the check, which is what makes it safe ---------------------------------------------------
   *
   * Advisory verdicts, except `fatal`, which nothing may override. The caller may allow a
   * non-fatal one when a person is standing in front of it and said yes.
   */
  const FLOOR = 20;        // below this, deleting or republishing a few files is ordinary work

  function check(plan, ctx){
    const c = ctx || {}, p = plan || {}, out = [];

    const settled = p.settle.filter(s => s.why === 'same content both sides').length;
    const keep = num(p.unchanged) + p.fetch.length + p.send.length + p.keepBoth.length + settled;

    /* A VIEW THAT COULD NOT BE READ IS NOT A DEVICE WITH NOTHING. It is a device that did not
     * answer, and the difference is every file it holds: read as empty, its files are absent from
     * the merge, and absent from the merge is indistinguishable from deleted. Uploading and
     * downloading are still fine — they add — so only the removals are refused. */
    if(c.missingViews){
      if(p.trash.length) out.push({ kind:'partialViews', fatal:true, n: p.trash.length,
                                    why: c.missingViews + ' of your devices could not be read — '
                                         + 'nothing will be deleted this sweep' });
      if(p.tombstone.length) out.push({ kind:'partialViewsOut', fatal:true, n: p.tombstone.length,
                                        why: c.missingViews + ' of your devices could not be read — '
                                             + 'not publishing any deletions this sweep' });
    }

    // A SHORT LIST IS A DELETE ORDER: never trash more than survives the sweep.
    if(p.trash.length >= FLOOR && p.trash.length > keep){
      out.push({ kind:'massTrash', n: p.trash.length, keep,
                 why: 'this sweep would move ' + p.trash.length + ' files to the trash and keep ' + keep });
    }

    // The same question pointing outwards: this device telling every other one to delete.
    if(p.tombstone.length >= FLOOR && p.tombstone.length > keep){
      out.push({ kind:'massTombstone', n: p.tombstone.length, keep,
                 why: 'this sweep would tell your other devices to delete ' + p.tombstone.length
                      + ' files and keep ' + keep });
    }

    /* An absolute floor, not a ratio: a restored backup makes every file look edited, so the
     * resurrections arrive beside thousands of ordinary uploads a ratio counts as kept — measured,
     * 3,930 beside 11,884, which sails past any ratio there is. */
    const res = p.send.filter(s => s.resurrect).length;
    if(res >= FLOOR) out.push({ kind:'massResurrect', n: res,
                                why: 'this sweep would republish ' + res
                                     + ' files your other devices deleted' });

    /* A folder has directories and a manifest does not. A path that another live entry sits under
     * cannot be written as a file on any device: it fails on every sweep, for ever. */
    const g = c.global || {};
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
    return out;
  }

  /* The plan with what a refusal forbids taken out — and nothing else.
   *
   * REFUSING SUPPRESSES ONE KIND OF ACTION, NEVER THE SWEEP. A guard that aborts everything is the
   * same bug with its sign flipped, which is exactly what happened to the contacts sweep: it stopped
   * syncing altogether rather than stopping the deletions. */
  function apply(plan, verdicts, allowed){
    const ok = new Set(allowed || []);
    let out = Object.assign({}, plan);
    for(const v of verdicts || []){
      if(!v.fatal && ok.has(v.kind)) continue;
      if(v.kind === 'massTrash' || v.kind === 'partialViews') out.trash = [];
      else if(v.kind === 'massTombstone' || v.kind === 'partialViewsOut') out.tombstone = [];
      else if(v.kind === 'massResurrect') out.send = out.send.filter(s => !s.resurrect);
      else if(v.kind === 'blocked') out.fetch = out.fetch.filter(f => f.path !== v.path);
    }
    return out;
  }

  const API = { merge, reconcile, check, apply, versionOf, bump, diskChanged, viewChanged,
                later, FLOOR };
  root.PCSyncEngine = API;
  if(typeof module !== 'undefined' && module.exports) module.exports = API;
})(typeof globalThis !== 'undefined' ? globalThis : this);
