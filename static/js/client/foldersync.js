/* Folder sync — the decision engine. Documents/Pictures kept in step across devices, in encrypted
 * Blossom, the way Notes and Music already are.
 *
 * DELIBERATELY PURE. This file performs no I/O, knows nothing about Nostr, Blossom, Electron or
 * Android, and never touches the DOM: it takes three snapshots and returns a PLAN of what should
 * happen. Everything that can destroy a file lives behind a platform adapter that executes the plan,
 * and everything that can get the ANSWER wrong lives here, where tests/test_folder_sync.py can run it
 * under node against thousands of generated scenarios. (Same reason ical.js and joplin.js are shaped
 * this way — those are parsers, and this one decides whether your documents get deleted.)
 *
 * THE THREE SNAPSHOTS, and why two is not enough:
 *
 *   local   what is on this device now          {path: {size, mtime, sha?}}
 *   remote  the shared manifest                 {path: {sha, size, mtime, deletedAt?, device?}}
 *   base    what this device last agreed with   {path: {sha, size, mtime, deletedAt?}}
 *
 * `base` is the whole game. Without it a file present locally and absent remotely is ambiguous — it
 * could be new here, or deleted there — and a two-way sync that guesses will either resurrect
 * everything you delete or delete everything you add. FilesIdx learned this the expensive way and
 * carries `_syncedAt` for exactly this reason; a folder needs it per PATH, not per drive.
 *
 * CONFLICTS ARE NEVER RESOLVED BY PICKING A WINNER. Two devices editing the same document is a
 * normal Tuesday, not an error, and there is no correct automatic merge for arbitrary bytes. Both
 * survive: the incoming version takes the real path, the local one is renamed to
 * `name (conflict from <device>, <date>).ext`. A sync that silently overwrites work is worse than
 * one that occasionally leaves you two files.
 *
 * DELETE LOSES TO EDIT. If one device deleted a file while another edited it, the edit wins and the
 * file comes back. The asymmetry is deliberate: resurrecting a file you meant to delete costs you
 * one more delete, and the other way costs you the file.
 *
 * NOTHING IS DELETED IN PLACE. Every local deletion is a MOVE into `.pc-trash/<date>/`, so a bad
 * manifest, a clock skew or a bug in this file is recoverable with a file manager instead of a
 * backup. The adapter is what enforces it; `deleteLocal` actions carry the destination.
 */
(function(root){
  'use strict';

  // A path is equal to another entry when its CONTENT is. sha wins whenever both sides have one —
  // it is the only comparison that survives a file being touched, copied or restored from a backup
  // with a fresh mtime. size+mtime is the fallback for a local scan that has not hashed yet, and it
  // is why a scan may leave `sha` undefined: hashing an unchanged 40GB Pictures folder on every
  // sweep is the difference between a background task and a space heater.
  const MTIME_SLOP = 2000;   // ms. FAT32/SMB/Android SAF round mtimes; exFAT to 2s. Anything tighter
                             // reports every file on a removable drive as changed, every sweep.
  function same(a, b){
    if(!a || !b) return !a && !b;
    if(a.deletedAt || b.deletedAt) return !!a.deletedAt === !!b.deletedAt;
    if(a.sha && b.sha) return a.sha === b.sha;
    return a.size === b.size && Math.abs((a.mtime||0) - (b.mtime||0)) <= MTIME_SLOP;
  }
  const live = e => !!e && !e.deletedAt;          // an entry that names actual bytes
  const gone = e => !e || !!e.deletedAt;          // absent, or a tombstone

  // `dir/name.ext` → `dir/name (conflict from laptop, 2026-08-09).ext`. The suffix goes before the
  // extension so the file still opens in whatever owns that type — a conflict copy called
  // `report.pdf (conflict…)` is a file nothing on the system will double-click.
  function conflictPath(path, device, when){
    const slash = path.lastIndexOf('/');
    const dir = slash < 0 ? '' : path.slice(0, slash + 1);
    const name = slash < 0 ? path : path.slice(slash + 1);
    const dot = name.lastIndexOf('.');
    const stem = dot > 0 ? name.slice(0, dot) : name;
    const ext = dot > 0 ? name.slice(dot) : '';
    const d = new Date(when || 0);
    const day = isNaN(d.getTime()) ? 'unknown date'
              : d.getUTCFullYear() + '-' + String(d.getUTCMonth() + 1).padStart(2, '0')
                + '-' + String(d.getUTCDate()).padStart(2, '0');
    return dir + stem + ' (conflict from ' + (device || 'another device') + ', ' + day + ')' + ext;
  }

  /* The plan. Every action names a path and says why, so a status panel can show the user sentences
   * instead of a spinner, and so a test can assert on the REASON rather than on the side effect. */
  function diff(opts){
    const local  = (opts && opts.local)  || {};
    const remote = (opts && opts.remote) || {};
    const base   = (opts && opts.base)   || {};
    const device = (opts && opts.device) || 'this device';
    const now    = (opts && opts.now)    || 0;

    const plan = { upload:[], download:[], deleteLocal:[], deleteRemote:[], conflicts:[],
                   unchanged:0, notes:[] };
    const paths = new Set([...Object.keys(local), ...Object.keys(remote), ...Object.keys(base)]);

    for(const path of [...paths].sort()){
      const L = local[path] || null, R = remote[path] || null, B = base[path] || null;
      const localChanged  = !same(L, B);
      const remoteChanged = !same(R, B);

      if(!localChanged && !remoteChanged){ plan.unchanged++; continue; }

      // Only one side moved — the easy majority, and the whole point of keeping `base`.
      if(localChanged && !remoteChanged){
        if(live(L)) plan.upload.push({ path, why: live(B) ? 'changed here' : 'new here' });
        else plan.deleteRemote.push({ path, why: 'deleted here' });
        continue;
      }
      if(remoteChanged && !localChanged){
        if(live(R)) plan.download.push({ path, sha: R.sha, why: live(B) ? 'changed elsewhere' : 'new elsewhere' });
        else if(live(L)) plan.deleteLocal.push({ path, why: 'deleted elsewhere' });
        continue;
      }

      // Both moved.
      if(gone(L) && gone(R)){ plan.notes.push({ path, why: 'deleted on both' }); continue; }

      // Converged by accident — the same edit made twice, or the same file copied in on both
      // devices. Nothing to do but agree, and NOT flagging it is what stops a photo library that
      // was seeded from the same camera producing thousands of conflict copies on first sync.
      if(live(L) && live(R) && L.sha && R.sha && L.sha === R.sha){
        plan.notes.push({ path, why: 'same content both sides' });
        continue;
      }

      // Delete loses to edit, in both directions.
      if(gone(L) && live(R)){
        plan.download.push({ path, sha: R.sha, why: 'deleted here but edited elsewhere — keeping the edit' });
        continue;
      }
      if(live(L) && gone(R)){
        plan.upload.push({ path, why: 'deleted elsewhere but edited here — keeping the edit' });
        continue;
      }

      // Genuinely divergent bytes. Keep both.
      const to = conflictPath(path, R.device || 'another device', (R.mtime || now));
      plan.conflicts.push({ path, keepAs: to, sha: R.sha,
                            why: 'edited on both — the incoming copy takes the name, yours is renamed' });
    }
    return plan;
  }

  /* Where a local deletion actually goes. Dated, so two deletions of the same name a week apart do
   * not collide and neither is silently lost inside the trash itself. */
  function trashPath(path, when){
    const d = new Date(when || 0);
    const day = isNaN(d.getTime()) ? 'unknown'
              : d.getUTCFullYear() + '-' + String(d.getUTCMonth() + 1).padStart(2, '0')
                + '-' + String(d.getUTCDate()).padStart(2, '0');
    return '.pc-trash/' + day + '/' + path;
  }

  /* Fold a completed plan back into the manifest that becomes the next run's `base`. Kept here, with
   * the rules it has to agree with, rather than in each adapter — two implementations of "what did we
   * just agree to" is how a sync loops forever, re-uploading what it just downloaded. */
  function advance(opts){
    const base = Object.assign({}, (opts && opts.base) || {});
    const done = (opts && opts.done) || {};        // path -> entry that is now true on BOTH sides
    const removed = (opts && opts.removed) || [];  // paths that are now deleted on both sides
    const now = (opts && opts.now) || 0;
    for(const path in done) base[path] = Object.assign({}, done[path]);
    for(const path of removed) base[path] = { deletedAt: now };
    return base;
  }

  /* Tombstones cannot live forever — a manifest that only grows is a manifest that eventually will
   * not fit in one blob. They can only be dropped once every device has certainly seen them, so this
   * is deliberately generous and deliberately NOT called automatically. A tombstone forgotten too
   * early is a deleted file that comes back from whichever device was offline. */
  function pruneTombstones(manifest, olderThanMs, now){
    const out = {};
    for(const path in manifest){
      const e = manifest[path];
      if(e && e.deletedAt && (now - e.deletedAt) > olderThanMs) continue;
      out[path] = e;
    }
    return out;
  }

  const API = { diff, advance, same, conflictPath, trashPath, pruneTombstones, MTIME_SLOP };
  root.PCFolderSync = API;
  if(typeof module !== 'undefined' && module.exports) module.exports = API;
})(typeof globalThis !== 'undefined' ? globalThis : this);
