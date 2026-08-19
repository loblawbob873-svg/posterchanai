"""Neither direction of a bulk change happens without a person — and it used to be only one.

THE FAILURE THIS FILE EXISTS FOR, measured on a real three-device folder: 59 stale tombstones over a
1,000-file pair. Undoing them (59 `resurrect` sends from the device that still held every file)
tripped the absolute resurrect floor at 20 and was REFUSED on every sweep — "NOT republished, your
other devices deleted these". Applying them (59 trashes) passed the ratio guard (59 is far short of
1,000 kept) and passed an absolute cap of 100, so it ran SILENTLY on the laptop, then on the tablet,
then on the desktop's own deep scan. The guard written to protect the files is exactly what
guaranteed the deletions won: the one device holding the copies was the only one forbidden to act,
and every sweep drove the folder further towards deleted.

So the two floors are the same number, and this proves it in the shape it was reported in. It also
proves the way OUT: a path named by a person goes through `resend`, and a named path is not an
inference, so the resurrect floor does not apply to it.

Every check here is verified to FAIL against the pre-fix rules (see test_z_the_old_rules_fail).
"""
import json
import os
import shutil
import subprocess
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CLIENT = os.path.join(ROOT, "static", "js", "client")
STATE = os.path.join(CLIENT, "syncstate.js")
FOLDERSYNC = os.path.join(CLIENT, "foldersync.js")
EXEC = os.path.join(CLIENT, "syncexec.js")
NODE = shutil.which("node") or shutil.which("nodejs")

# The folder as it was reported: 1,000 files everyone agrees on, 59 carrying tombstones this device
# never applied, and this device holding a copy of all 59 (restored from a NAS backup, so a fresh
# mtime that no incremental scan can tell from an edit).
WORLD = """
  const S = require(%s);
  function world(o){
    const state = {}, disk = {}, index = {};
    for(let i = 0; i < 1000; i++){
      const p = 'docs/f' + i + '.txt', c = 'c' + i;
      state[p] = { v:3, by:'desk', size:10, mtime:1000, csum:c, sha:'blob-' + c };
      disk[p]  = { size:10, mtime:1000, csum: o.hash ? c : undefined };
      index[p] = { v:3, by:'desk', size:10, mtime:1000, csum:c, sha:'blob-' + c,
                   local:{ size:10, mtime:1000, csum:c } };
    }
    for(let i = 0; i < 59; i++){
      const p = 'important/keep' + i + '.docx', c = 'k' + i;
      state[p] = { v:5, by:'tablet', deletedAt:5000, size:20, mtime:2000, csum:c, sha:'blob-' + c };
      index[p] = { v: o.applied ? 5 : 4, by:'desk', size:20, mtime:2000, csum:c, sha:'blob-' + c,
                   local: o.applied ? null : { size:20, mtime:2000, csum:c } };
      if(o.holds) disk[p] = { size:20, mtime: o.restored ? 8888 : 2000,
                              csum: o.hash ? c : undefined };
    }
    return { state, disk, index };
  }
""" % (json.dumps(STATE),)


@unittest.skipIf(not NODE, "no node on this node")
class FloorSymmetryTests(unittest.TestCase):
    def _run(self, body):
        js = "require(%s);\n%s\n%s" % (json.dumps(FOLDERSYNC), WORLD, body)
        r = subprocess.run([NODE, "-e", js], capture_output=True, text=True, timeout=120)
        self.assertEqual(r.returncode, 0, r.stderr[-2000:])
        return json.loads(r.stdout)

    def _sweep_plan(self, **opts):
        return self._run("""
          const w = world(%s);
          const p = S.plan({ state:w.state, disk:w.disk, index:w.index, device:'me', now:9000 });
          const v = S.check(p, { state: w.state });
          const a = S.apply(p, v, []);                 // an automatic sweep confirms nothing
          process.stdout.write(JSON.stringify({
            planTrash: p.trash.length, planSend: p.send.length,
            resurrect: p.send.filter(x => x.resurrect).length,
            kinds: v.map(x => x.kind),
            doesTrash: a.trash.length,
            doesSend: a.send.length }));
        """ % (json.dumps(opts),))

    # ---- the deleting direction, which is the one that ran silently -------------------------

    def test_a_stale_tombstone_wave_is_not_applied_unattended(self):
        """The laptop and the tablet, each holding all 59: 59 trashes against 1,000 kept files."""
        out = self._sweep_plan(holds=True, applied=False)
        self.assertEqual(out["planTrash"], 59, "setup: the engine should plan 59 trashes")
        self.assertIn("massTrash", out["kinds"],
                      "59 deletions on a 1,000-file folder passed every guard and ran silently")
        self.assertEqual(out["doesTrash"], 0, "an unattended sweep still moved 59 files to trash")

    def test_the_deep_scan_takes_the_same_answer(self):
        """With hashes the restored copies compare equal to the tombstones' csum, so the desktop
        plans to trash its own restored backup. Same wave, same floor."""
        out = self._sweep_plan(holds=True, applied=False, hash=True)
        self.assertEqual(out["planTrash"], 59)
        self.assertIn("massTrash", out["kinds"])
        self.assertEqual(out["doesTrash"], 0)

    def test_an_ordinary_handful_of_deletions_still_just_happens(self):
        """The floor must not turn every deletion into a dialog — below it, nothing is questioned."""
        out = self._run("""
          const state = {}, disk = {}, index = {};
          for(let i = 0; i < 500; i++){ const p = 'a' + i, c = 'c' + i;
            state[p] = { v:1, by:'x', size:1, mtime:1, csum:c, sha:'b' + c };
            disk[p] = { size:1, mtime:1 };
            index[p] = { v:1, by:'x', size:1, mtime:1, csum:c, sha:'b' + c,
                         local:{ size:1, mtime:1, csum:c } }; }
          for(let i = 0; i < 19; i++){ const p = 'd' + i;
            state[p] = { v:2, by:'x', deletedAt:5, csum:'z' + i, sha:'bz' + i };
            disk[p] = { size:1, mtime:1 };
            index[p] = { v:1, by:'x', size:1, mtime:1, csum:'z' + i, sha:'bz' + i,
                         local:{ size:1, mtime:1, csum:'z' + i } }; }
          const p = S.plan({ state, disk, index, device:'me', now:9000 });
          const v = S.check(p, { state });
          process.stdout.write(JSON.stringify({ planTrash: p.trash.length,
            kinds: v.map(x => x.kind), doesTrash: S.apply(p, v, []).trash.length }));
        """)
        self.assertEqual(out["planTrash"], 19)
        self.assertEqual(out["kinds"], [], "19 deletions must not need a person — the floor is 20")
        self.assertEqual(out["doesTrash"], 19)

    def test_confirming_it_carries_it_out(self):
        """A refusal is a question, not a veto: the answer has to be able to be yes."""
        out = self._run("""
          const w = world({ holds:true, applied:false });
          const p = S.plan({ state:w.state, disk:w.disk, index:w.index, device:'me', now:9000 });
          const v = S.check(p, { state: w.state });
          const a = S.apply(p, v, ['massTrash']);      // the person said yes
          process.stdout.write(JSON.stringify({ doesTrash: a.trash.length }));
        """)
        self.assertEqual(out["doesTrash"], 59)

    def test_a_refusal_suppresses_one_bucket_and_never_the_sweep(self):
        """The other half of the contacts lesson: a guard that aborts everything is the same bug
        with its sign flipped."""
        out = self._run("""
          const w = world({ holds:true, applied:false });
          w.disk['docs/new.txt'] = { size:5, mtime:7000 };          // an ordinary new file
          const p = S.plan({ state:w.state, disk:w.disk, index:w.index, device:'me', now:9000 });
          const v = S.check(p, { state: w.state });
          const a = S.apply(p, v, []);
          process.stdout.write(JSON.stringify({ doesTrash: a.trash.length,
            uploadsAnyway: a.send.filter(x => !x.resurrect).map(x => x.path) }));
        """)
        self.assertEqual(out["doesTrash"], 0)
        self.assertEqual(out["uploadsAnyway"], ["docs/new.txt"],
                         "the refused deletion also stopped an unrelated upload")

    # ---- the two floors are one number ------------------------------------------------------

    def test_both_directions_stop_at_the_same_count(self):
        """The whole bug in one assertion: whichever way a bulk change points, it needs the same
        evidence. Before this, deleting needed 101 and undoing needed 20."""
        out = self._run("""
          const mk = (n, kind) => {           // kind: 'trash' | 'resurrect'
            const state = {}, disk = {}, index = {};
            for(let i = 0; i < 3000; i++){ const p = 'q' + i, c = 'c' + i;
              state[p] = { v:1, by:'x', size:1, mtime:1, csum:c, sha:'b' + c };
              disk[p] = { size:1, mtime:1 };
              index[p] = { v:1, by:'x', size:1, mtime:1, csum:c, sha:'b' + c,
                           local:{ size:1, mtime:1, csum:c } }; }
            for(let i = 0; i < n; i++){ const p = 'm' + i;
              state[p] = { v:2, by:'x', deletedAt:5, csum:'z' + i, sha:'bz' + i };
              disk[p] = { size:1, mtime: kind === 'trash' ? 1 : 9999 };
              index[p] = { v:1, by:'x', size:1, mtime:1, csum:'z' + i, sha:'bz' + i,
                           local:{ size:1, mtime:1, csum:'z' + i } }; }
            const p = S.plan({ state, disk, index, device:'me', now:9000 });
            return S.check(p, { state }).map(x => x.kind);
          };
          const at = (kind, want) => { let lo = 0;
            for(let n = 1; n <= 40; n++) if(mk(n, kind).indexOf(want) >= 0){ lo = n; break; }
            return lo; };
          process.stdout.write(JSON.stringify({
            trashAt: at('trash', 'massTrash'), resurrectAt: at('resurrect', 'massResurrect') }));
        """)
        self.assertEqual(out["trashAt"], out["resurrectAt"],
                         "the floors are different, which is how the asymmetry got in")
        self.assertEqual(out["trashAt"], 20)

    # ---- the way out ------------------------------------------------------------------------

    def test_a_named_resend_is_not_an_inference_and_is_not_floored(self):
        """Pressing "put them back everywhere" names the paths. The engine flags a `resurrect` send
        because it cannot tell a restored backup from an edit — but a person naming the files IS the
        answer to that question, so the executor must strip the flag rather than let the verdict
        sweep them straight back out of the plan."""
        out = self._run("""
          const X = require(%s);
          (async () => {
          const paths = [];
          for(let i = 0; i < 59; i++) paths.push('important/keep' + i + '.docx');
          const w = world({ holds:true, applied:false, restored:true });
          const put = [];
          const fs = {
            scanPage: async (id, so, off, n) => {
              const all = Object.keys(w.disk).sort().slice(off, off + n), files = {};
              for(const p of all) files[p] = w.disk[p];
              return { files, done: off + all.length >= Object.keys(w.disk).length };
            },
            read: async (id, p) => new Uint8Array(w.disk[p].size),   // exactly what the scan saw
            confirmGone: async () => ({ gone:false, parentAlive:true }),
          };
          const io = {
            index: async () => JSON.parse(JSON.stringify(w.index)),
            state: async () => ({ state: JSON.parse(JSON.stringify(w.state)), flagged:{} }),
            saveIndex: async () => {},
            hashBytes: async () => 'h',
            putBlob: async () => ({ sha:'fresh' }),
            putState: async (k, recs) => { for(const r of recs) put.push(r.path);
              return { ok: recs.map(r => r.path), stale:[], failed:[] }; },
          };
          const rep = await X.sweep(fs, io, { id:'f', key:'k', device:'me', now:9000,
                                              manual:true, resend: paths });
          process.stdout.write(JSON.stringify({
            refused: (rep.refused || []).map(x => x.kind),
            uploaded: (rep.uploaded || []).length,
            restoring: rep.restoring || 0,
            published: put.filter(p => p.indexOf('important/') === 0).length }));
          })().catch(e => { console.error(e && e.stack || e); process.exit(1); });
        """ % (json.dumps(EXEC),))
        self.assertEqual(out["restoring"], 59, "the named paths kept their resurrect flag")
        self.assertEqual(out["uploaded"], 59,
                         "the guard swept the user's own explicit restore out of the plan")
        self.assertEqual(out["published"], 59)

    def test_the_person_is_asked_once_per_kind_not_once_per_rule(self):
        """Two rules raise `massTrash` and both fire on exactly the sweep that matters most. Asked
        per verdict that is the same dialog twice about the same files, which is how somebody learns
        to click through the one that counts — and one Yes has always covered both, because
        `apply()` keys on `kind`."""
        out = self._run("""
          const X = require(%s);
          (async () => {
          // 59 trashes and only 30 survivors: the ratio AND the floor both speak.
          const state = {}, disk = {}, index = {};
          for(let i = 0; i < 30; i++){ const p = 'k' + i, c = 'c' + i;
            state[p] = { v:1, by:'x', size:1, mtime:1, csum:c, sha:'b' + c };
            disk[p] = { size:1, mtime:1 };
            index[p] = { v:1, by:'x', size:1, mtime:1, csum:c, sha:'b' + c,
                         local:{ size:1, mtime:1, csum:c } }; }
          for(let i = 0; i < 59; i++){ const p = 'd' + i;
            state[p] = { v:2, by:'x', deletedAt:5, csum:'z' + i, sha:'bz' + i };
            disk[p] = { size:1, mtime:1 };
            index[p] = { v:1, by:'x', size:1, mtime:1, csum:'z' + i, sha:'bz' + i,
                         local:{ size:1, mtime:1, csum:'z' + i } }; }
          const kinds = [];
          const fs = {
            scanPage: async (id, so, off, n) => {
              const all = Object.keys(disk).sort().slice(off, off + n), files = {};
              for(const p of all) files[p] = disk[p];
              return { files, done: off + all.length >= Object.keys(disk).length };
            },
            trash: async (id, p) => '.pc-trash/' + p,
            confirmGone: async () => ({ gone:false, parentAlive:true }),
          };
          const io = {
            index: async () => JSON.parse(JSON.stringify(index)),
            state: async () => ({ state: JSON.parse(JSON.stringify(state)), flagged:{} }),
            saveIndex: async () => {},
            putState: async (k, recs) => ({ ok: recs.map(r => r.path), stale:[], failed:[] }),
          };
          const rep = await X.sweep(fs, io, { id:'f', key:'k', device:'me', now:9000, manual:true,
            confirm: async (v) => { kinds.push(v.kind + ':' + (v.rule || '')); return true; } });
          process.stdout.write(JSON.stringify({ asked: kinds, trashed: (rep.trashed||[]).length }));
          })().catch(e => { console.error(e && e.stack || e); process.exit(1); });
        """ % (json.dumps(EXEC),))
        self.assertEqual(out["asked"], ["massTrash:shortList"],
                         "the same question was asked more than once, or with the milder wording")
        self.assertEqual(out["trashed"], 59, "one Yes must cover both rules")

    # ---- restore from trash must not be undone by the sweep that follows it ------------------

    def test_a_named_resend_is_taken_out_of_the_trash_list(self):
        """RESTORE, SWEEP, BACK IN THE TRASH — 172 files, every round reporting success.

        Putting a file back was left entirely implicit: the bytes returned to the disk and nothing
        else changed, so the next sweep re-derived the intent from versions and timestamps — and it
        derives the opposite. The restored bytes ARE the bytes the tombstone describes, so wherever
        this device's journal entry is missing (struck by a lost compare-and-swap, cleared by an
        era change, never there) a hashed scan reads "deleted elsewhere, and this copy is the
        deleted version" and trashes the lot again.

        `resend` is the way to say it outright — and it dropped a named path from settle, fetch and
        keepBoth while leaving `trash` alone, so a sweep could be told "send this file" and move it
        to .pc-trash in the same pass. Both halves are asserted: the path leaves the trash list, and
        it is actually sent."""
        out = self._run("""
          const X = require(%s);
          (async () => {
          const paths = [];
          const state = {}, disk = {}, index = {};
          for(let i = 0; i < 5; i++){        // BELOW the floor on purpose: past it the
            const p = 'restored/f' + i + '.docx', c = 'k' + i;   // mass guard answers first
                                                                 // and this would be measuring that
            paths.push(p);
            // The folder says deleted, at a version this device never applied…
            state[p] = { v:9, by:'other', deletedAt:5000, size:20, mtime:2000, csum:c, sha:'blob-' + c };
            // …this device's journal knows nothing (struck / cleared)…
            // …and the file is back on disk, hashed, byte-identical to what was deleted.
            disk[p] = { size:20, mtime:7777, csum:c };
          }
          const trashed = [], sent = [];
          const fs = {
            scanPage: async (id, so, off, n) => {
              const all = Object.keys(disk).sort().slice(off, off + n), files = {};
              for(const p of all) files[p] = disk[p];
              return { files, done: off + all.length >= Object.keys(disk).length };
            },
            read: async (id, p) => new Uint8Array(disk[p].size),
            hashFile: async (id, p) => disk[p].csum,
            trash: async (id, p) => { trashed.push(p); return '.pc-trash/' + p; },
            confirmGone: async () => ({ gone:false, parentAlive:true }),
          };
          const io = {
            index: async () => JSON.parse(JSON.stringify(index)),
            state: async () => ({ state: JSON.parse(JSON.stringify(state)), flagged:{} }),
            saveIndex: async () => {},
            hashBytes: async () => 'h',
            putBlob: async () => ({ sha:'fresh' }),
            putState: async (k, recs) => { for(const r of recs) sent.push(r.path);
              return { ok: recs.map(r => r.path), stale:[], failed:[] }; },
          };
          // Without saying anything: the sweep undoes the restore.
          const before = await X.sweep(fs, io, { id:'f', key:'k', device:'me', now:9000 });
          const undone = trashed.length;
          trashed.length = 0; sent.length = 0;
          // Naming them is the whole difference.
          const after = await X.sweep(fs, io, { id:'f', key:'k', device:'me', now:9000,
                                               manual:true, resend: paths });
          process.stdout.write(JSON.stringify({
            undoneWithoutSaying: undone,
            trashedAnyway: trashed.length,
            uploaded: (after.uploaded || []).length,
            published: sent.length }));
          })().catch(e => { console.error(e && e.stack || e); process.exit(1); });
        """ % (json.dumps(EXEC),))
        self.assertEqual(out["undoneWithoutSaying"], 5,
                         "setup: an unannounced restore should be undone — that IS the report")
        self.assertEqual(out["trashedAnyway"], 0,
                         "the sweep trashed files it had been told, by name, to send")
        self.assertEqual(out["uploaded"], 5)
        self.assertEqual(out["published"], 5)

    # ---- a tombstone that names nothing is a deletion nobody can undo ------------------------

    def test_a_tombstone_takes_its_address_from_whichever_side_still_has_one(self):
        """`index[p] || state[p]` reads as "prefer what we applied", and it is how the address went
        missing: a journal entry that had lost its own address SHADOWED a record that still had one,
        so the published tombstone named no bytes. Two things break at once and neither says so —
        "Deleted on every device" cannot offer the file (it lists only addressed tombstones), and a
        device still holding the file can never settle against it, because delete-loses-to-edit
        compares csums and an absent csum always reads as an edit: it republishes for ever."""
        out = self._run("""
          const X = require(%s);
          (async () => {
          // The journal remembers applying it and nothing more — a struck CAS write, an era change,
          // a row from an older build. The shared record still carries the address.
          const idx  = { 'gone.txt': { v:2, by:'me', local:{ size:9, mtime:10, csum:'c1' } } };
          const rec  = { 'gone.txt': { v:2, by:'other', sha:'s1', csum:'c1', size:9, mtime:10 } };
          let published = null;
          const fs = { scanPage: async () => ({ files:{}, done:true }),
                       confirmGone: async () => ({ gone:true, parentAlive:true }) };
          const io = {
            index: async () => JSON.parse(JSON.stringify(idx)),
            state: async () => ({ state: JSON.parse(JSON.stringify(rec)), flagged:{} }),
            saveIndex: async () => {},
            putState: async (k, recs) => { published = published || {};
              for(const r of recs) published[r.path] = r.entry;
              return { ok: recs.map(r => r.path), stale:[], failed:[] }; },
          };
          await X.sweep(fs, io, { id:'f', key:'k', device:'me', now:9000 });
          const t = (published || {})['gone.txt'] || {};
          process.stdout.write(JSON.stringify({ deleted: !!t.deletedAt, sha: t.sha || '',
                                                csum: t.csum || '' }));
          })().catch(e => { console.error(e && e.stack || e); process.exit(1); });
        """ % (json.dumps(EXEC),))
        self.assertTrue(out["deleted"], "setup: the sweep should have announced the deletion")
        self.assertEqual(out["sha"], "s1",
                         "the tombstone named no bytes — nothing can restore it account-wide")
        self.assertEqual(out["csum"], "c1",
                         "without a csum, a device holding the file republishes it for ever")

    # ---- and the proof that each rule can fail ----------------------------------------------

    def test_z_the_old_rules_fail(self):
        """Re-run the pre-fix guard over the reported world. If this passes, the check above is
        measuring nothing."""
        out = self._run("""
          const w = world({ holds:true, applied:false });
          const p = S.plan({ state:w.state, disk:w.disk, index:w.index, device:'me', now:9000 });
          // The rules exactly as they were: a ratio, and an absolute cap of 100.
          const settled = p.settle.filter(s => s.why === 'same content both sides').length;
          const keep = p.unchanged - p.settledGone + p.fetch.length + p.send.length
                     + p.keepBoth.length + settled;
          const old = (p.trash.length >= 20 && p.trash.length > keep) || p.trash.length > 100;
          const res = p.send.filter(s => s.resurrect).length >= 20;
          process.stdout.write(JSON.stringify({ oldWouldAsk: old, oldWouldRefuseTheUndo: res,
                                                trash: p.trash.length, keep }));
        """)
        self.assertFalse(out["oldWouldAsk"],
                         "the old rules already caught this, so the fix is not the fix")
        self.assertEqual(out["trash"], 59)


class RestoreSaysWhatHappened(unittest.TestCase):
    """"restore 172 files from trash did nothing … says already restored", three times.

    Every row skipped means every destination already holds the file — a previous restore DID put
    them back and the trash copies survived it, which on Android is what an optional `moveDocument`
    does when the provider satisfies it by COPYING. From the outside that is indistinguishable from
    a dead button, and the count never drops, so it is pressed again and again.

    The reassurance ("your files are back, the trash holds duplicates") is only safe if it is TRUE,
    so it is measured — a sample of pairs hashed on both sides — and the opposite case gets the
    opposite advice."""

    @classmethod
    def setUpClass(cls):
        with open(os.path.join(CLIENT, "sync.js"), encoding="utf-8") as fh:
            cls.sync = fh.read()

    def _seg(self):
        i = self.sync.index("if(!done && skipped && typeof fs2.hashFile")
        return self.sync[i:i + 2000]

    def test_it_does_not_assert_the_copies_are_duplicates(self):
        seg = self._seg()
        self.assertIn("hashFile(folderId, r.at)", seg)
        self.assertIn("hashFile(folderId, r.to)", seg)
        self.assertIn("same && !diff", seg,
                      "the reassuring sentence must need every sampled pair to match")

    def test_differing_bytes_get_the_opposite_advice(self):
        seg = self._seg()
        self.assertIn("DIFFERENT contents", seg,
                      "an older version in the trash must not be described as a duplicate")
        i_ok, i_bad = seg.index("Empty trash reclaims"), seg.index("DIFFERENT contents")
        self.assertNotEqual(i_ok, i_bad)

    def test_it_only_speaks_when_nothing_was_restored(self):
        """A partial restore is its own story; this sentence is for the all-skipped case only."""
        self.assertIn("if(!done && skipped", self.sync)


class BigFilesCanActuallyLand(unittest.TestCase):
    """A 2 GB file could never finish on a phone, and the two reasons were both size-blind.

    "ANDROID ISSUES WRITING >2GB FILE  DOWNLOAD STOPPED MOVING WILL TRY AGAIN", then "tablet keeps
    reloading UI when doing folder sync".

    1. RESUME WAS DEAD. The executor has always called `io.getParts(chunks, write, expect, have, cs)`
       and sync.js's stall-guard wrapper declared `(chunks, write)` — three arguments dropped on the
       floor. Without `have`/`cs` the resume arithmetic cannot run, so `skip` stays 0 and every retry
       restarts at byte 0; without `expect` the "rebuilt N bytes, expected M" check never fires at
       all. Invisible on a small file, fatal on a large one: the three-minute stall guard trips, the
       sweep retries, and the retry throws away every byte.
    2. THE BRIDGE PAYLOAD WAS THE UPLOADER'S CHOICE. `cs` is set once by whoever stored the file — a
       desktop picks 16 MB — and each chunk crosses Capacitor as base64 held as UTF-16, so a phone
       was asked to hold ~80 MB of renderer heap per chunk. That is the render process being killed
       and the UI "reloading" mid-sweep."""

    @classmethod
    def setUpClass(cls):
        with open(os.path.join(CLIENT, "sync.js"), encoding="utf-8") as fh:
            cls.sync = fh.read()
        with open(EXEC, encoding="utf-8") as fh:
            cls.exc = fh.read()

    def _run(self, body):
        js = "require(%s);\n%s" % (json.dumps(FOLDERSYNC), body)
        r = subprocess.run([NODE, "-e", js], capture_output=True, text=True, timeout=120)
        self.assertEqual(r.returncode, 0, r.stderr[-2000:])
        return json.loads(r.stdout)

    def test_the_transfer_wrappers_forward_every_argument(self):
        for name in ("getParts", "putParts"):
            i = self.sync.index("    %s: PC.syncBlobs" % name)
            seg = self.sync[i:i + 1900]
            self.assertIn("...rest", seg,
                          "%s drops the arguments the executor passes — resume and the size check "
                          "silently stop working" % name)
            self.assertGreaterEqual(seg.count("...rest"), 2,
                                    "%s must both ACCEPT and FORWARD the rest" % name)

    def test_the_executor_hands_the_store_what_resume_needs(self):
        """The contract the wrapper was breaking, RUN: a chunked download must arrive at the store
        with the expected size, how much is already on disk, and the chunk size — or resume cannot
        be computed and every retry starts at byte 0."""
        out = self._run("""
          const X = require(%s);
          (async () => {
          const entry = { v:1, by:'other', size:9000, mtime:1, csum:'k',
                          chunks:['a','b','c'], cs:4000 };
          let seen = null;
          const fs = {
            chunkBytes: 1000,
            scanPage: async () => ({ files:{}, done:true }),
            partSize: async () => 4000,                 // one whole chunk already on disk
            hashPart: async () => 'k',
            writePart: async () => {},
            writeCommit: async () => ({ size:9000, mtime:2 }),
            confirmGone: async () => ({ gone:false, parentAlive:true }),
          };
          const io = {
            index: async () => ({}),
            state: async () => ({ state: { 'big.jex': JSON.parse(JSON.stringify(entry)) }, flagged:{} }),
            saveIndex: async () => {},
            putState: async (k, r) => ({ ok:r.map(x=>x.path), stale:[], failed:[] }),
            getParts: async (chunks, write, expect, have, cs) => {
              seen = { expect, have, cs, chunks: chunks.length };
              await write(0, new Uint8Array(1));
              return 9000;
            },
          };
          await X.sweep(fs, io, { id:'f', key:'k', device:'me', now:1 });
          process.stdout.write(JSON.stringify(seen || {}));
          })().catch(e => { console.error(e && e.stack || e); process.exit(1); });
        """ % (json.dumps(EXEC),))
        self.assertEqual(out.get("expect"), 9000, "the store cannot check the rebuilt size")
        self.assertEqual(out.get("have"), 4000,
                         "the store never learns what is already on disk — every retry restarts at "
                         "byte 0, which is why a 2 GB file could never finish")
        self.assertEqual(out.get("cs"), 4000, "without the chunk size resume cannot be computed")

    def test_a_big_incoming_chunk_reaches_the_disk_in_platform_sized_pieces(self):
        i = self.exc.index("const _piece = (fs.chunkBytes")
        seg = self.exc[i:i + 700]
        self.assertIn("bytes.subarray(", seg, "a 16 MB chunk still crosses the bridge whole")
        self.assertIn("bytes.length <= _piece", seg,
                      "it must stay a single call when the sizes already agree")


class StoreCountIsEvidence(unittest.TestCase):
    """"the counter for local files so we can compare what is on the blossom server."

    Three numbers, and the third is the only one that says whether the BYTES exist: "N here" is this
    disk, "N in the folder" is what the devices agreed, and neither implies that a new device could
    actually fetch anything. This is the difference between a folder that can be restored and one
    that only looks like it can.

    The rule it must not break is the one this feature has broken before, twice (the drive check's
    upload-shortcut probe called 497 present files lost; the admin scan had to learn the same): a
    listing that could not be READ is not a listing of nothing."""

    @classmethod
    def setUpClass(cls):
        with open(os.path.join(CLIENT, "sync.js"), encoding="utf-8") as fh:
            cls.sync = fh.read()

    def _seg(self):
        i = self.sync.index("async function _storeCount(")
        return self.sync[i:i + 1800]

    def test_an_unreadable_listing_counts_nothing_as_missing(self):
        seg = self._seg()
        self.assertIn("if(!Array.isArray(list)) return null;", seg,
                      "anything other than a real listing must answer null, not an empty set — an "
                      "empty set makes every file in the folder look absent from the store")
        self.assertIn("catch(_){ list = null; }", seg)
        self.assertIn("catch(_){ return null; }", seg,
                      "a record set that could not be read must not be counted either")

    def test_a_file_needs_every_chunk_to_count_as_held(self):
        seg = self._seg()
        self.assertIn("ids.every(id => have.has(id))", seg,
                      "one missing chunk is one file nobody can fetch; counting it as held is how a "
                      "'complete' folder turns out not to be")
        self.assertIn("if(!ids.length){ missing++; continue; }", seg,
                      "a record naming no storage at all is not held")

    def test_tombstones_are_not_counted_as_files(self):
        self.assertIn("if(!e || e.deletedAt) continue;", self._seg(),
                      "folding deletions in is what made 8,132 tombstones read as 8,132 files")

    def test_the_paint_never_waits_on_the_network(self):
        """A screen that awaits a request is the failure this codebase keeps paying for."""
        i = self.sync.index("function _storeAsk(")
        seg = self.sync[i:i + 700]
        self.assertNotIn("await ", seg, "_storeAsk must kick the request, not await it")
        self.assertIn("paint()", seg, "…and repaint once the answer lands")


class CheckDoesNotWedgeThePage(unittest.TestCase):
    """The check reads every file and asks about every record — and it must give the page its
    thread back while it does, or Android kills the renderer and the UI reloads mid-operation."""

    @classmethod
    def setUpClass(cls):
        with open(EXEC, encoding="utf-8") as fh:
            cls.exc = fh.read()
        with open(os.path.join(CLIENT, "sync.js"), encoding="utf-8") as fh:
            cls.sync = fh.read()

    def test_the_hashing_loop_yields(self):
        i = self.exc.index("const paths = Object.keys(state).filter")
        seg = self.exc[i:i + 900]
        self.assertIn("await breathe()", seg,
                      "the per-file hash loop never yields — a phone holds its renderer flat out "
                      "for minutes and Chromium reclaims it")

    def test_the_store_pass_is_not_one_request_at_a_time(self):
        i = self.exc.index("checking the store")
        seg = self.exc[max(0, i - 1200):i + 1200]
        self.assertIn("Promise.all", seg,
                      "twelve thousand serial HEADs is long enough to lose the renderer before the "
                      "answer arrives")

    def test_a_check_holds_the_processor_and_always_gives_it_back(self):
        i = self.sync.index("async function verifyFolder(f)")
        seg = self.sync[i:i + 3000]
        self.assertIn("wakeBegin", seg, "a check can run for minutes with no wake lock at all")
        self.assertIn("finally", seg)
        self.assertIn("wakeEnd", seg, "a lock that is never released is a flat battery")


if __name__ == "__main__":
    unittest.main()
