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
            planTrash: p.remove.length, planSend: p.send.length,
            resurrect: p.send.filter(x => x.resurrect).length,
            kinds: v.map(x => x.kind),
            doesTrash: a.remove.length,
            doesSend: a.send.length }));
        """ % (json.dumps(opts),))

    # ---- the deleting direction, which is no longer counted at all ---------------------------

    def test_the_tombstone_wave_now_simply_applies(self):
        """SAME FOLDER, OPPOSITE ANSWER, AND THAT IS THE FIX.

        The floors are gone. They were approximating "can this deletion be undone", and that is now
        answered directly by the executor: the local copy goes only once the STORE HAS CONFIRMED it
        still holds those bytes, and the bytes stay there in one account-wide trash on the server.

        So the engine plans all 59 and nothing suppresses them — which is right, because a deletion
        that has been proved recoverable is not a risk, and treating it as one is what produced the
        dialogs people learned to click through. What used to make this dangerous was never the
        number; it was that nobody had checked whether the files could come back.
        """
        out = self._sweep_plan(holds=True, applied=False)
        self.assertEqual(out["planTrash"], 59, "setup: the engine should plan 59 deletions")
        self.assertNotIn("massTrash", out["kinds"], "a count-based deletion guard came back")
        self.assertEqual(out["doesTrash"], 59, "something is still suppressing deletions by count")

    def test_the_deep_scan_takes_the_same_answer(self):
        """With hashes the restored copies compare equal to the tombstones' csum, so this device
        agrees the files were deleted. Same wave, same answer — and the same store check stands
        between it and any actual removal."""
        out = self._sweep_plan(holds=True, applied=False, hash=True)
        self.assertEqual(out["planTrash"], 59)
        self.assertNotIn("massTrash", out["kinds"])
        self.assertEqual(out["doesTrash"], 59)

    def test_an_ordinary_handful_of_deletions_still_just_happens(self):
        """It always did. What changed is that a large one does too, once it has been checked."""
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
          process.stdout.write(JSON.stringify({ planTrash: p.remove.length,
            kinds: v.map(x => x.kind), doesTrash: S.apply(p, v, []).remove.length }));
        """)
        self.assertEqual(out["planTrash"], 19)
        self.assertEqual(out["kinds"], [], "an ordinary handful of deletions raised a verdict")
        self.assertEqual(out["doesTrash"], 19)

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

    def test_a_verified_deletion_asks_nothing_at_all(self):
        """THE DIALOG IS GONE, AND ITS ABSENCE IS THE POINT.

        There used to be a question here — two rules could raise it, and it was carefully arranged
        so a person was asked once rather than twice. That was solving the wrong problem. A dialog
        that fires on ordinary work is a dialog people learn to confirm, and after four days of them
        the answer to "delete 122 files?" was yes; those were business receipts.

        Now nothing asks, because nothing is guessing: the executor has already established, file by
        file, that the store still holds the bytes and the deletion can be undone from one
        account-wide trash. 59 deletions against 30 survivors — the shape that used to raise BOTH
        the ratio and the floor — simply applies.
        """
        out = self._run("""
          const X = require(%s);
          (async () => {
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
            remove: async (id, p) => { delete disk[p]; return true; },
            confirmGone: async () => ({ gone:false, parentAlive:true }),
          };
          const io = {
            index: async () => JSON.parse(JSON.stringify(index)),
            state: async () => ({ state: JSON.parse(JSON.stringify(state)), flagged:{} }),
            saveIndex: async () => {},
            putState: async (k, recs) => ({ ok: recs.map(r => r.path), stale:[], failed:[] }),
            hasBlob: async () => true,          // the store confirms it can give them back
          };
          const rep = await X.sweep(fs, io, { id:'f', key:'k', device:'me', now:9000, manual:true,
            confirm: async (v) => { kinds.push(v.kind + ':' + (v.rule || '')); return true; } });
          process.stdout.write(JSON.stringify({ asked: kinds, trashed: (rep.trashed||[]).length }));
          })().catch(e => { console.error(e && e.stack || e); process.exit(1); });
        """ % (json.dumps(EXEC),))
        self.assertEqual(out["asked"], [],
                         "a verified deletion still stopped to ask: " + repr(out["asked"]))
        self.assertEqual(out["trashed"], 59, "it applied %r of 59 verified deletions" % out["trashed"])

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
        keepBoth while leaving the deletion list alone, so a sweep could be told "send this file"
        and delete it in the same pass. Both halves are asserted: the path leaves the deletion list,
        and it is actually sent."""
        out = self._run("""
          const X = require(%s);
          (async () => {
          const paths = [];
          const state = {}, disk = {}, index = {};
          for(let i = 0; i < 5; i++){
            const p = 'restored/f' + i + '.docx', c = 'k' + i;
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
            remove: async (id, p) => { trashed.push(p); delete disk[p]; return true; },
            confirmGone: async () => ({ gone:false, parentAlive:true }),
          };
          const io = {
            index: async () => JSON.parse(JSON.stringify(index)),
            state: async () => ({ state: JSON.parse(JSON.stringify(state)), flagged:{} }),
            saveIndex: async () => {},
            hashBytes: async () => 'h',
            hasBlob: async () => true,        // the store can give them back, so a delete may run
            putBlob: async () => ({ sha:'fresh' }),
            putState: async (k, recs) => { for(const r of recs) sent.push(r.path);
              return { ok: recs.map(r => r.path), stale:[], failed:[] }; },
          };
          // Without saying anything: the sweep undoes the restore.
          const before = await X.sweep(fs, io, { id:'f', key:'k', device:'me', now:9000 });
          const undone = trashed.length;
          trashed.length = 0; sent.length = 0;
          // Put them back again — which is literally what somebody does, and is exactly as silent
          // the second time. Naming them is the whole difference.
          for(const p of paths) disk[p] = { size:20, mtime:7777, csum: 'k' + paths.indexOf(p) };
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

    def test_a_device_can_declare_its_copy_correct_without_retiring_the_pair(self):
        """The folder says deleted; the files are sitting right here.

        Retiring the pair works and is a sledgehammer: it throws away every record for every file,
        so every other device re-reads the whole folder from nothing — offered for a situation that
        needs ONE thing said. "why do I have to remove and readd" is the right question.

        Only the paths the folder DISAGREES about are published (no record, or a tombstone); a file
        whose record is live and correct is left alone, so a 12,000-file folder republishes the
        handful actually in dispute. And because they are named, they come out of the trash list —
        a path somebody asked to publish is not a deletion candidate in the same sweep."""
        out = self._run("""
          const X = require(%s);
          (async () => {
          const state = {}, disk = {}, index = {};
          // 10 files everyone agrees about — these must NOT be republished.
          for(let i = 0; i < 10; i++){ const p = 'ok/f' + i, c = 'c' + i;
            state[p] = { v:1, by:'x', size:5, mtime:1, csum:c, sha:'b' + c };
            disk[p]  = { size:5, mtime:1, csum:c };
            index[p] = { v:1, by:'x', size:5, mtime:1, csum:c, sha:'b' + c,
                         local:{ size:5, mtime:1, csum:c } }; }
          // 30 the folder calls deleted, at a version this device never applied, held here with
          // the very bytes the tombstone describes — so the engine would obey the deletion.
          const doomed = [];
          for(let i = 0; i < 30; i++){ const p = 'restored/f' + i, c = 'k' + i;
            doomed.push(p);
            state[p] = { v:9, by:'phone', deletedAt:5000, size:9, mtime:2, csum:c, sha:'b' + c };
            index[p] = { v:4, by:'me', size:9, mtime:2, csum:c, sha:'b' + c,
                         local:{ size:9, mtime:2, csum:c } };
            disk[p]  = { size:9, mtime:2, csum:c }; }
          const trashed = [], put = [];
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
            putState: async (k, recs) => { for(const r of recs) put.push(r.path);
              return { ok: recs.map(r => r.path), stale:[], failed:[] }; },
          };
          const rep = await X.sweep(fs, io, { id:'f', key:'k', device:'me', now:9000,
                                              manual:true, resendAll:true });
          process.stdout.write(JSON.stringify({
            reclaiming: rep.reclaiming || 0,
            uploaded: (rep.uploaded || []).length,
            trashed: trashed.length,
            republishedAgreed: put.filter(p => p.indexOf('ok/') === 0).length }));
          })().catch(e => { console.error(e && e.stack || e); process.exit(1); });
        """ % (json.dumps(EXEC),))
        self.assertEqual(out["reclaiming"], 30, "it did not pick out the disputed files")
        self.assertEqual(out["uploaded"], 30, "the disputed files were not published")
        self.assertEqual(out["trashed"], 0,
                         "it trashed files it had just been told to publish")
        self.assertEqual(out["republishedAgreed"], 0,
                         "it republished files the folder already agrees about — on a real folder "
                         "that is re-encrypting and re-uploading everything")

    def test_a_device_that_can_see_nothing_may_not_delete_the_folder(self):
        """FATAL — never offered, never confirmable, and that is the difference that matters.

        Every other guard here asks. There is no answer a person could give that makes this one
        right: a scan that found NOTHING while the journal knows about hundreds of files is a device
        that has lost sight of a folder — a revoked grant, an unmounted volume, a phone whose copy
        was cleared — not a folder somebody emptied. Asking puts a destructive default one tap away
        and hands over a decision the evidence cannot support, because nothing survives the sweep to
        weigh it against.

        Reported twice in one evening from two emptied devices: "tell your other devices to delete
        966 files?" and then 107, against files that were on the desktop the whole time."""
        out = self._run("""
          const state = {}, disk = {}, index = {};
          for(let i = 0; i < 107; i++){ const p = 'p' + i, c = 'c' + i;
            state[p] = { v:1, by:'x', size:1, mtime:1, csum:c, sha:'b' + c };
            index[p] = { v:1, by:'x', size:1, mtime:1, csum:c, sha:'b' + c,
                         local:{ size:1, mtime:1, csum:c } }; }
          const p = S.plan({ state, disk, index, device:'emptied', now:9000 });
          const v = S.check(p, { state });
          const fatal = v.filter(x => x.fatal);
          // Even a caller that says yes to everything must not get past it.
          const a = S.apply(p, v, ['massTombstone', 'massTrash', 'massResurrect']);
          process.stdout.write(JSON.stringify({
            planned: p.tombstone.length,
            fatalKinds: fatal.map(x => x.kind + '/' + (x.rule || '')),
            doesTombstone: a.tombstone.length }));
        """)
        self.assertEqual(out["planned"], 107, "setup: the engine should plan 107 tombstones")
        self.assertIn("massTombstone/emptyDevice", out["fatalKinds"],
                      "an empty device is still merely ASKED, so one tap deletes the folder")
        self.assertEqual(out["doesTombstone"], 0,
                         "a confirmation got past it — this one must not be confirmable")

    def test_a_device_that_still_holds_something_keeps_its_voice(self):
        """The one surviving rule is `keep === 0`, and it is not a count: a device that can see NONE
        of the files it knows about has lost sight of the folder — a revoked grant, an unmounted
        volume, a folder picked at the wrong path. A device holding real files has a real opinion
        about them and is simply believed. Widening this would stop legitimate deletions
        propagating at all, and a guard that stops everything is the same bug with its sign
        flipped."""
        out = self._run("""
          const state = {}, disk = {}, index = {};
          for(let i = 0; i < 107; i++){ const p = 'gone' + i, c = 'c' + i;
            state[p] = { v:1, by:'x', size:1, mtime:1, csum:c, sha:'b' + c };
            index[p] = { v:1, by:'x', size:1, mtime:1, csum:c, sha:'b' + c,
                         local:{ size:1, mtime:1, csum:c } }; }
          for(let i = 0; i < 5; i++){ const p = 'here' + i, c = 'h' + i;
            state[p] = { v:1, by:'x', size:1, mtime:1, csum:c, sha:'b' + c };
            disk[p]  = { size:1, mtime:1, csum:c };
            index[p] = { v:1, by:'x', size:1, mtime:1, csum:c, sha:'b' + c,
                         local:{ size:1, mtime:1, csum:c } }; }
          const p = S.plan({ state, disk, index, device:'has-some', now:9000 });
          const v = S.check(p, { state });
          process.stdout.write(JSON.stringify({
            fatal: v.filter(x => x.fatal).length,
            asks: v.some(x => x.kind === 'massTombstone'),
            confirmable: S.apply(p, v, ['massTombstone']).tombstone.length }));
        """)
        self.assertEqual(out["fatal"], 0, "a device holding files was refused outright")
        self.assertFalse(out["asks"],
                         "a device that can still see its folder was questioned about deleting "
                         "from it — the counting guards are gone, and only the lost-folder rule "
                         "(keep === 0) survives")
        self.assertEqual(out["confirmable"], 107, "and the deletions must simply travel")

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

    def test_z_the_guards_could_never_have_caught_this(self):
        """WHY THE NUMBERS WERE REPLACED RATHER THAN TUNED.

        Re-run every guard that ever existed over the folder as it was reported, and watch each of
        them wave it through or make it worse:

          - the RATIO (more removed than kept) never fires — 59 deletions arrive beside 1,000
            untouched files, which is what every real folder looks like;
          - the CAP of 100 never fires, because 59 is less than 100;
          - the FLOOR of 20 fires, but on BOTH sides — so the device holding the only good copies is
            refused permission to put them back, while the devices with nothing to lose are merely
            asked, and after four days of dialogs the answer to a dialog is yes.

        There is no number that separates "59 files somebody deleted" from "59 files a device is
        about to lose", because the difference is not in the count. It is in whether the bytes can
        come back, which is a question with an actual answer — and asking it is what replaced all
        of this.
        """
        out = self._run("""
          const w = world({ holds:true, applied:false });
          const p = S.plan({ state:w.state, disk:w.disk, index:w.index, device:'me', now:9000 });
          const settled = p.settle.filter(s => s.why === 'same content both sides').length;
          const keep = p.unchanged - p.settledGone + p.fetch.length + p.send.length
                     + p.keepBoth.length + settled;
          process.stdout.write(JSON.stringify({
            remove: p.remove.length, keep,
            ratioWouldFire: p.remove.length >= 20 && p.remove.length > keep,
            capWouldFire: p.remove.length > 100,
            floorWouldFire: p.remove.length >= 20,
            floorWouldAlsoRefuseTheUndo: p.send.filter(s => s.resurrect).length >= 20 }));
        """)
        self.assertEqual(out["remove"], 59, "setup: the reported wave is 59 deletions")
        self.assertGreater(out["keep"], 900, "setup: beside a folder nobody touched")
        self.assertFalse(out["ratioWouldFire"],
                         "the proportional rule would have caught it, so it was not the problem")
        self.assertFalse(out["capWouldFire"], "nor the cap of 100")
        self.assertTrue(out["floorWouldFire"],
                        "the floor is the only one that spoke — by asking, on every device")


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
        # Wide enough to hold the whole branch including the offer it ends with — this reads SOURCE,
        # so the window is about prose length and not about the rule.
        return self.sync[i:i + 4200]

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

    def test_it_offers_to_clear_the_duplicates_and_only_after_proving_they_are_duplicates(self):
        """The state the user is stuck in cannot be escaped by restoring — the destinations are
        occupied by the files themselves, so every press reports "N already back in place" and the
        count never falls. `emptyTrash` is the only call that can remove them and it is in every
        build; what was missing was the confirmation that pressing it is safe, which is what the
        hashes establish. The offer must therefore sit INSIDE the all-matched branch."""
        seg = self._seg()
        i_ok = seg.index("same && !diff")
        i_offer = seg.index("emptyTrash")
        self.assertGreater(i_offer, i_ok,
                           "the offer to empty the trash must be inside the branch that PROVED the "
                           "copies are duplicates — never on the differing-bytes path")
        self.assertIn("uiConfirm", seg[i_ok:], "it must ask before deleting anything")
        self.assertIn("Remove the duplicates", seg)
        self.assertNotIn("emptyTrash", seg[:i_ok])

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


class MemoryIsMeasuredWhereItGoes(unittest.TestCase):
    """"tablet keeps reloading UI when doing folder sync" · "probably out of memory from other sync
    parts". The APK already distinguishes the two renderer deaths in a toast (reclaimed vs crashed);
    this is the other half — how close the sweep got, and to what.

    The sampling was in `step()`, which is called per FILE during transfers, so the peak it found
    was always a transfer. The two largest allocations happen before any of that and between two
    steps: the whole record set decrypted (every path, every checksum, and for a chunked file one
    hash per 4 MB — a 2 GB file alone is ~500 of them) and this device's journal, both live at once
    while the plan is built. Unsampled, the report blamed whatever moved next."""

    @classmethod
    def setUpClass(cls):
        with open(EXEC, encoding="utf-8") as fh:
            cls.exc = fh.read()
        with open(os.path.join(CLIENT, "sync.js"), encoding="utf-8") as fh:
            cls.sync = fh.read()

    def test_the_big_loads_are_sampled(self):
        for anchor in ("got0 = await io.state(", "index = (await io.index("):
            i = self.exc.index(anchor)
            self.assertIn("_mark(", self.exc[max(0, i - 400):i],
                          "%s is not sampled — the two biggest allocations in a sweep were "
                          "invisible to the peak" % anchor)

    def _details(self):
        """The DETAILS block's use of it. `summarise` has carried a peak-memory line for a long time
        and only past 1024 MB — a threshold a phone's renderer rarely survives to report — so
        anchoring on the first occurrence in the file measures that one instead of this."""
        i = self.sync.index('<b>Memory</b>')
        return self.sync[max(0, i - 300):i + 700]

    def test_the_peak_reaches_the_card(self):
        seg = self._details()
        self.assertIn("peakHeapMB", seg,
                      "the sweep has measured this all along and the details never showed it")
        self.assertIn("peakHeapPhase", seg, "a number with no phase does not say what to fix")

    def test_it_is_not_shouted_about_on_an_ordinary_sweep(self):
        self.assertIn(">= 200", self._details(),
                      "a healthy sweep must not carry a memory warning — it would be noise on every "
                      "report and then ignored on the one that mattered")


class AnUnansweredHashNeverMintsACopy(unittest.TestCase):
    """"phone now has conflict files", straight after a large file finished syncing.

    The hash in the conflict path is the only thing between a timestamp difference and a duplicated
    file — and on Android it reads the WHOLE file back through SAF. On a multi-gigabyte file that is
    minutes of I/O that can throw, be killed with the renderer, or answer nothing; every one of
    those landed as `h = null` and fell straight through to minting a second copy. "Could not
    compare" is not "different", and this is the one place in the sweep where getting that wrong
    duplicates a file rather than losing one."""

    def _run(self, hash_js):
        js = """
        require(%s); const X = require(%s);
        (async () => {
          const entry = { v:9, by:'other', sha:'s1', csum:'THEIRS', size:20, mtime:2000 };
          const disk = { 'Pictures/big.jex': { size:20, mtime:7777 } };
          const made = [];
          const fs = {
            scanPage: async (id, so, off) => (off ? { files:{}, done:true }
              : { files: JSON.parse(JSON.stringify(disk)), done:true }),
            %s
            move: async (id, from, to) => { made.push(to); },
            write: async () => ({ size:20, mtime:1 }),
            writeCommit: async () => ({ size:20, mtime:1 }),
            confirmGone: async () => ({ gone:false, parentAlive:true }),
          };
          const io = {
            index: async () => ({}),
            state: async () => ({ state: { 'Pictures/big.jex': JSON.parse(JSON.stringify(entry)) },
                                  flagged:{} }),
            saveIndex: async () => {},
            hashBytes: async () => 'THEIRS',
            getBlob: async () => new Uint8Array(20),
            putState: async (k, r) => ({ ok:r.map(x=>x.path), stale:[], failed:[] }),
          };
          const rep = await X.sweep(fs, io, { id:'f', key:'k', device:'me', now:9000 });
          process.stdout.write(JSON.stringify({
            copies: made, conflicted: (rep.conflicted||[]).length,
            uncompared: (rep.uncompared||[]).length, ok: rep.ok }));
        })().catch(e => { console.error(e && e.stack || e); process.exit(1); });
        """ % (json.dumps(FOLDERSYNC), json.dumps(EXEC), hash_js)
        r = subprocess.run([NODE, "-e", js], capture_output=True, text=True, timeout=90)
        self.assertEqual(r.returncode, 0, r.stderr[-1500:])
        return json.loads(r.stdout)

    def test_a_hash_that_throws_leaves_both_copies_alone(self):
        out = self._run("hashFile: async () => { throw new Error('read timed out'); },")
        self.assertEqual(out["copies"], [],
                         "a file was duplicated because this device could not read it back")
        self.assertEqual(out["conflicted"], 0)
        self.assertEqual(out["uncompared"], 1, "and it has to be reported, not swallowed")
        self.assertFalse(out["ok"], "an unresolved path is not a clean sweep")

    def test_a_hash_that_answers_nothing_leaves_both_copies_alone(self):
        out = self._run("hashFile: async () => null,")
        self.assertEqual(out["copies"], [])
        self.assertEqual(out["uncompared"], 1)

    def test_matching_bytes_still_settle_with_no_copy(self):
        out = self._run("hashFile: async () => 'THEIRS',")
        self.assertEqual(out["copies"], [])
        self.assertEqual(out["uncompared"], 0, "an answered, equal hash is not an unknown")

    def test_a_hash_that_really_differs_still_keeps_both(self):
        """The guard must not swallow a REAL conflict — that would lose an edit."""
        out = self._run("hashFile: async () => 'MINE',")
        self.assertEqual(out["uncompared"], 0)
        self.assertEqual(len(out["copies"]), 1,
                         "a genuinely divergent file must still keep both copies")


class ALargeDownloadFinishes(unittest.TestCase):
    """The two things that go wrong at the END of a multi-gigabyte download, both of which throw the
    whole file away and start again — so the bigger the file, the less likely it ever lands.

    1. Resume works in WHOLE chunks (`have % cs === 0`), and a COMPLETE part file almost never
       satisfies that because the last chunk is short. A download that finished and then failed only
       its verification therefore came back with the entire file on disk, failed the modulo, and
       pulled all of it a second time.
    2. `hashPart` reads the whole part file back — on Android through SAF, minutes of I/O that can
       throw or be killed with the renderer. Treated as a checksum FAILURE it discards the part file
       and re-downloads everything, which on a large file never terminates."""

    def _run(self, hashpart_js, have):
        js = """
        require(%s); const X = require(%s);
        (async () => {
          const CS = 1000, SIZE = 2400;                 // 3 chunks, last one short — the real shape
          const entry = { v:9, by:'other', csum:'GOOD', size:SIZE, cs:CS,
                          chunks:['a','b','c'] };
          const pulled = [];
          const fs = {
            chunkBytes: CS,
            scanPage: async () => ({ files:{}, done:true }),
            partSize: async () => %d,
            %s
            discardPart: async () => { pulled.push('DISCARD'); },
            writePart: async (id, p, off) => { pulled.push(off); },
            writeCommit: async () => ({ size:SIZE, mtime:2 }),
            confirmGone: async () => ({ gone:false, parentAlive:true }),
          };
          const io = {
            index: async () => ({}),
            state: async () => ({ state: { 'big.jex': JSON.parse(JSON.stringify(entry)) }, flagged:{} }),
            saveIndex: async () => {},
            putState: async (k, r) => ({ ok:r.map(x=>x.path), stale:[], failed:[] }),
            getParts: async (chunks, write, expect, have, cs) => {
              let off = 0, skip = 0;
              if(have > 0 && cs > 0 && have %% cs === 0){ skip = Math.floor(have / cs); off = skip * cs; }
              for(let i = skip; i < chunks.length; i++){
                const n = (i === chunks.length - 1) ? 400 : cs;
                await write(off, new Uint8Array(n)); off += n;
              }
              return off;
            },
          };
          const rep = await X.sweep(fs, io, { id:'f', key:'k', device:'me', now:1 });
          process.stdout.write(JSON.stringify({ pulled, downloaded: (rep.downloaded||[]).length,
                                                failed: (rep.failed||[]).map(f => f.error) }));
        })().catch(e => { console.error(e && e.stack || e); process.exit(1); });
        """ % (json.dumps(FOLDERSYNC), json.dumps(EXEC), have, hashpart_js)
        r = subprocess.run([NODE, "-e", js], capture_output=True, text=True, timeout=90)
        self.assertEqual(r.returncode, 0, r.stderr[-1500:])
        return json.loads(r.stdout)

    def test_a_complete_part_file_is_not_downloaded_again(self):
        """It is whole on disk and only needs hashing."""
        out = self._run("hashPart: async () => 'GOOD',", 2400)
        self.assertEqual(out["pulled"], [],
                         "a file already fully on disk was fetched again from byte 0")
        self.assertEqual(out["downloaded"], 1, "…and it must still be committed")

    def test_an_unanswered_hash_keeps_the_bytes(self):
        out = self._run("hashPart: async () => { throw new Error('read timed out'); },", 2400)
        self.assertNotIn("DISCARD", out["pulled"],
                         "2 GB was thrown away because this device could not read it back")
        self.assertEqual(out["pulled"], [], "and it must not re-fetch either")
        self.assertTrue(any("could not read it back" in f for f in out["failed"]),
                        "it has to be reported: %r" % (out["failed"],))

    def test_a_hash_that_really_differs_still_discards_and_refetches(self):
        """The guard must not protect a genuinely spliced part file — that is what it is for."""
        out = self._run("hashPart: async () => 'WRONG',", 1000)
        self.assertIn("DISCARD", out["pulled"],
                      "a part file that failed a real checksum must be thrown away")


class TheStallWindowKnowsWhatIsComing(unittest.TestCase):
    """"the download stopped moving will try again", repeatedly, on a file that was in fact moving.

    The guard bumps when a CHUNK lands, so between bumps an entire chunk has to download AND decrypt.
    Three minutes is right for the 4 MB a phone cuts and wrong for the 16 MB a desktop cuts: 16 MB
    inside three minutes demands ~90 KB/s sustained, which a phone on a poor link does not have. A
    transfer that was working was therefore declared dead every time — and before resume worked,
    each declaration threw away everything."""

    @classmethod
    def setUpClass(cls):
        with open(os.path.join(CLIENT, "sync.js"), encoding="utf-8") as fh:
            cls.sync = fh.read()

    def _seg(self):
        i = self.sync.index("getParts: PC.syncBlobs")
        return self.sync[i:i + 2200]

    def test_the_window_scales_with_the_incoming_chunk(self):
        seg = self._seg()
        self.assertIn("rest[2]", seg,
                      "the download guard never saw the chunk size, so it could not know how much "
                      "had to arrive before the next bump")
        self.assertIn("Math.max(_STALL_MS", seg,
                      "it must never go BELOW the old floor — a small chunk keeps the tight window")

    def test_the_arithmetic_gives_a_phone_a_chance(self):
        """A 16 MB chunk at the pessimistic floor must buy minutes, not seconds; a 4 MB one must not
        change from the three-minute default."""
        import re as _re
        m = _re.search(r"Math\.ceil\(_cs / (\d+)\) \* 1000", self._seg())
        self.assertIsNotNone(m, "the window is no longer derived from the chunk size")
        rate = int(m.group(1))
        big = -(-(16 * 1024 * 1024) // rate)          # seconds allowed for a 16 MB chunk
        small = -(-(4 * 1024 * 1024) // rate)
        self.assertGreater(big, 300, "16 MB still has to arrive inside five minutes")
        self.assertLessEqual(small, 180, "a 4 MB chunk must keep the original three-minute window")

    def test_the_upload_guard_is_left_alone(self):
        """Uploads bump per chunk READ and per progress report, so they were never the problem —
        and widening a guard that is working is how a dead socket goes unnoticed."""
        i = self.sync.index("putParts: PC.syncBlobs")
        self.assertNotIn("_cs", self.sync[i:i + 900])


class AWrongKeyBlobIsRepairable(unittest.TestCase):
    """"the bytes are intact but were sealed with a different key" … "never recover".

    A blob this account can no longer open is a THIRD kind of unusable copy and it was the only one
    with no repair. `hasBlob` says it is there, so none of the missing-bytes machinery applies, and
    the error's own advice — press "Send them again" on the device that HAS the file — pointed at a
    path that only ever covered blobs the store had LOST. It failed every sweep, for ever.

    It is deterministic like a checksum failure, so it is remembered by storage ADDRESS and FLAGGED
    on the record: whoever holds the plaintext re-uploads, which seals it under the current key and
    gives it a new address, and a fresh address lifts every device's memory of the old one."""

    def _run(self, err):
        js = """
        require(%s); const X = require(%s);
        (async () => {
          const entry = { v:3, by:'other', sha:'ab06adca', csum:'c1', size:9, mtime:10 };
          const flags = [];
          const fs = {
            scanPage: async () => ({ files:{}, done:true }),
            write: async () => ({ size:9, mtime:1 }),
            confirmGone: async () => ({ gone:false, parentAlive:true }),
          };
          const io = {
            index: async () => ({}),
            state: async () => ({ state: { 'a.jpg': JSON.parse(JSON.stringify(entry)) }, flagged:{} }),
            saveIndex: async () => {},
            hashBytes: async () => 'c1',
            getBlob: async () => { throw new Error(%s); },
            flagBad: async (k, batch) => { for(const b of batch) flags.push(b.path); },
            putState: async (k, r) => ({ ok:r.map(x=>x.path), stale:[], failed:[] }),
          };
          const rep = await X.sweep(fs, io, { id:'f', key:'k', device:'me', now:1 });
          process.stdout.write(JSON.stringify({
            flagged: flags,
            remembered: Object.keys(rep.badFetch || {}),
            wrongKey: rep.wrongKey || [] }));
        })().catch(e => { console.error(e && e.stack || e); process.exit(1); });
        """ % (json.dumps(FOLDERSYNC), json.dumps(EXEC), json.dumps(err))
        r = subprocess.run([NODE, "-e", js], capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr[-1500:])
        return json.loads(r.stdout)

    def test_a_wrong_key_blob_is_flagged_so_a_holder_repairs_it(self):
        out = self._run("this device\u2019s drive key does not open ab06adca — the bytes are "
                        "intact but were sealed with a different key")
        self.assertEqual(out["wrongKey"], ["a.jpg"], "a wrong-key blob was not recognised as one")
        self.assertEqual(out["flagged"], ["a.jpg"],
                         "nothing was flagged, so no device is ever asked to re-send it — that is "
                         "the 'never recover' loop")
        self.assertIn("a.jpg", out["remembered"],
                      "it will be re-fetched every sweep for ever")

    def test_an_ordinary_network_error_is_still_not_remembered(self):
        """A 5xx or a dead socket really is about the moment; remembering it strands a good copy."""
        out = self._run("the server did not answer in time — will try again")
        self.assertEqual(out["wrongKey"], [])
        self.assertEqual(out["remembered"], [],
                         "a transient failure was remembered as a permanently bad copy")


class AnotherEngineOnTheSameFolderIsNamed(unittest.TestCase):
    """Two authorities over one directory produce every symptom this feature has been accused of.

    Measured on a real folder after five days of unexplained losses: `.stversions` held
    PosterChan-named conflict copies from a fortnight earlier that Syncthing had archived away, and
    nothing had ever mentioned Syncthing was on the same tree. The scan already skips those
    directories — that stops us SYNCING them, a different problem — so the fact sat on the disk the
    whole time and was never said out loud.

    It must change no decision. Naming it is the whole job: the fix is a person choosing which
    engine owns the folder, and nothing here can choose for them."""

    def _run(self, present):
        js = """
        require(%s); const X = require(%s);
        (async () => {
          const HERE = %s;
          const fs = {
            scanPage: async () => ({ files:{}, done:true }),
            confirmGone: async (id, rel) => ({ gone: HERE.indexOf(rel) < 0, parentAlive: true }),
          };
          const io = { index: async () => ({}), state: async () => ({ state:{}, flagged:{} }),
                       saveIndex: async () => {},
                       putState: async (k, r) => ({ ok:r.map(x=>x.path), stale:[], failed:[] }) };
          const rep = await X.sweep(fs, io, { id:'f', key:'k', device:'me', now:1 });
          process.stdout.write(JSON.stringify({ engines: rep.otherEngines || [],
                                                failed: (rep.failed||[]).length, ok: rep.ok }));
        })().catch(e => { console.error(e && e.stack || e); process.exit(1); });
        """ % (json.dumps(FOLDERSYNC), json.dumps(EXEC), json.dumps(present))
        r = subprocess.run([NODE, "-e", js], capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr[-1500:])
        return json.loads(r.stdout)

    def test_syncthing_is_named(self):
        out = self._run([".stfolder"])
        self.assertEqual(out["engines"], ["Syncthing"])

    def test_a_clean_folder_says_nothing(self):
        out = self._run([])
        self.assertEqual(out["engines"], [],
                         "a folder with no other engine must not carry a warning — a banner on "
                         "every card is a banner nobody reads")

    def test_it_changes_no_decision(self):
        """It is a report, not a guard. A folder shared with another engine still syncs; refusing
        would be worse than the overlap, and the user may have arranged it deliberately."""
        out = self._run([".stfolder", ".dropbox"])
        self.assertEqual(sorted(out["engines"]), ["Dropbox", "Syncthing"])
        self.assertEqual(out["failed"], 0, "detecting another engine failed the sweep")
        self.assertTrue(out["ok"], "…or marked it unclean")


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


class ARewriteIsNotAnEdit(unittest.TestCase):
    """Identical bytes with a fresh timestamp must not be uploaded again.

    `diskChanged` compares a STAMP — size and mtime — because that is all a paged scan can afford to
    know. Everything that rewrites a file without changing a byte therefore reads as "changed here":
    a restore from backup, an rsync, a second sync engine on the same directory, a touch, a scanner
    that rewrites in place. Reported after a sweep had finished cleanly with every count in
    agreement — 11,939 here, 11,939 in the folder, 11,939 in the store — as "why is desktop
    uploading 2/19 files right now! sync was finished!".

    The fetch side has adopted by content since the engine was written ("same content both sides").
    The send side never did."""

    def _run(self, local_hash, resend="[]"):
        js = """
        require(%s); const X = require(%s);
        (async () => {
          const R = { v:4, by:'other', sha:'s1', csum:'CONTENT', size:5, mtime:1000 };
          const puts = [], published = [];
          const fs = {
            scanPage: async (id, so, off) => (off ? { files:{}, done:true }
              /* same size, NEW mtime, and no csum — exactly what a paged scan hands back after
               * something rewrote the file in place. */
              : { files: { 'a.txt': { size:5, mtime:9999 } }, done:true }),
            hashFile: async () => %s,
            read: async () => { puts.push('read'); return new Uint8Array(5); },
            confirmGone: async () => ({ gone:false, parentAlive:true }),
          };
          const io = {
            index: async () => ({ 'a.txt': Object.assign({}, R,
                                    { local: { size:5, mtime:1000, csum:'CONTENT' } }) }),
            state: async () => ({ state: { 'a.txt': JSON.parse(JSON.stringify(R)) }, flagged:{} }),
            saveIndex: async () => {},
            hashBytes: async () => 'CONTENT',
            putBlob: async () => { puts.push('putBlob'); return { sha:'s2' }; },
            putState: async (k, recs) => { for(const x of recs) published.push(x.path);
                                           return { ok:recs.map(x=>x.path), stale:[], failed:[] }; },
          };
          const rep = await X.sweep(fs, io, { id:'f', key:'k', device:'me', now:9000,
                                              resend: %s });
          process.stdout.write(JSON.stringify({
            puts, published, uploaded: rep.uploaded || [],
            settled: rep.settledByContent || [], ok: rep.ok }));
        })().catch(e => { console.error(e && e.stack || e); process.exit(1); });
        """ % (json.dumps(FOLDERSYNC), json.dumps(EXEC), local_hash, resend)
        r = subprocess.run([NODE, "-e", js], capture_output=True, text=True, timeout=90)
        self.assertEqual(r.returncode, 0, r.stderr[-1500:])
        return json.loads(r.stdout)

    def test_identical_bytes_are_not_uploaded(self):
        out = self._run("'CONTENT'")
        self.assertEqual(out["settled"], ["a.txt"])
        self.assertEqual(out["uploaded"], [], "a file the store already holds was uploaded again")
        self.assertNotIn("putBlob", out["puts"], "…and its bytes were pushed over the wire")
        self.assertTrue(out["ok"])

    def test_it_publishes_nothing(self):
        """This device learned nothing the folder did not already know. Publishing a new version
        would hand every other device a round of work for one local rewrite — one machine's rsync
        becoming a sync on all of them."""
        out = self._run("'CONTENT'")
        self.assertEqual(out["published"], [],
                         "a rewrite that changed no byte published a new version to every device")

    def test_different_bytes_still_upload(self):
        out = self._run("'EDITED'")
        self.assertEqual(out["settled"], [])
        self.assertEqual(out["uploaded"], ["a.txt"])
        self.assertIn("putBlob", out["puts"])

    def test_an_unreadable_file_still_uploads(self):
        """The shortcut may only ever REMOVE work. It cannot decide anything, so anything it cannot
        answer falls through to the upload that would have happened anyway."""
        out = self._run("null")
        self.assertEqual(out["uploaded"], ["a.txt"])
        out = self._run("(() => { throw new Error('busy'); })()")
        self.assertEqual(out["uploaded"], ["a.txt"])

    def test_a_repair_is_never_shortcut(self):
        """The one case where a matching checksum means the OPPOSITE. These paths are here because
        the STORE lost the bytes; the record still certifies the same content, so a naive shortcut
        would agree the file is safely stored and skip the repair — leaving a folder that reports
        itself in step while the bytes behind it stay missing, and nothing left to press."""
        out = self._run("'CONTENT'", resend="['a.txt']")
        self.assertEqual(out["settled"], [], "a resend named by hand was answered with a shortcut")
        self.assertEqual(out["uploaded"], ["a.txt"])
        self.assertIn("putBlob", out["puts"])


class OneFileHasOneSpellingOnEveryPlatform(unittest.TestCase):
    """The record's ADDRESS is sha256(path), so two spellings of one name are two records — and two
    records for one file is a duplication loop this engine has no defence against: each device
    downloads the other's spelling and the folder grows a second copy of everything with an accent
    in its name.

    macOS is where they diverge. HFS+ stored filenames decomposed and APFS still hands NFD back
    through some APIs, so a file Linux and Windows both call "café.txt" (U+00E9) arrives from a Mac
    as "cafe\u0301.txt" — visually identical, byte-different, a different record. There is no Mac
    here to measure on, which is the reason it is normalised at the ONE boundary a path enters the
    engine rather than at the call sites."""

    def _sweep(self, scan_paths, state="{}"):
        js = """
        require(%s); const X = require(%s);
        (async () => {
          const files = {};
          for(const p of %s) files[p] = { size: 5, mtime: 1000 };
          const sent = [];
          const fs = {
            scanPage: async (id, so, off) => (off ? { files:{}, done:true } : { files, done:true }),
            read: async () => new Uint8Array(5),
            confirmGone: async () => ({ gone:false, parentAlive:true }),
          };
          const io = {
            index: async () => ({}),
            state: async () => ({ state: %s, flagged:{} }),
            saveIndex: async () => {},
            hashBytes: async () => 'H',
            putBlob: async () => ({ sha:'s1' }),
            putState: async (k, recs) => { for(const r of recs) sent.push(r.path);
                                           return { ok:recs.map(r=>r.path), stale:[], failed:[] }; },
          };
          const rep = await X.sweep(fs, io, { id:'f', key:'k', device:'me', now:9000 });
          process.stdout.write(JSON.stringify({ sent, clash: rep.caseClash || [] }));
        })().catch(e => { console.error(e && e.stack || e); process.exit(1); });
        """ % (json.dumps(FOLDERSYNC), json.dumps(EXEC), json.dumps(scan_paths), state)
        r = subprocess.run([NODE, "-e", js], capture_output=True, text=True, timeout=90)
        self.assertEqual(r.returncode, 0, r.stderr[-1500:])
        return json.loads(r.stdout)

    def test_a_decomposed_name_is_published_composed(self):
        out = self._sweep(["cafe\u0301.txt"])
        self.assertEqual(out["sent"], ["caf\u00e9.txt"],
                         "a macOS-decomposed filename was published under its own spelling — every "
                         "other device will fetch it as a second file")

    def test_a_composed_name_is_left_exactly_as_it_is(self):
        """Windows and Linux never decompose, so this must be a no-op for them — otherwise the
        normalisation is itself a re-keying of every existing record."""
        out = self._sweep(["caf\u00e9.txt"])
        self.assertEqual(out["sent"], ["caf\u00e9.txt"])

    def test_both_spellings_collapse_to_one_record(self):
        """The proof that it is the same file afterwards, not merely tidier."""
        out = self._sweep(["cafe\u0301.txt", "caf\u00e9.txt"])
        self.assertEqual(out["sent"], ["caf\u00e9.txt"], out)

    def test_case_only_collisions_are_reported_and_not_touched(self):
        """`Foo.txt` and `foo.txt` are two files on Linux and ONE on macOS and Windows. There is no
        correct automatic answer — folding loses one of two files a Linux user legitimately holds,
        and leaving it makes a Windows device download each over the other for ever. Naming it is
        the whole job."""
        st = ('{"Foo.txt":{"v":1,"by":"a","csum":"H","sha":"s","size":5,"mtime":1000},'
              ' "foo.txt":{"v":1,"by":"a","csum":"H2","sha":"s2","size":5,"mtime":1000}}')
        out = self._sweep([], state=st)
        self.assertEqual(len(out["clash"]), 1, out)
        self.assertEqual(sorted(out["clash"][0]), ["Foo.txt", "foo.txt"])

    def test_a_folder_with_no_collision_reports_none(self):
        st = '{"a.txt":{"v":1,"by":"a","csum":"H","sha":"s","size":5,"mtime":1000}}'
        out = self._sweep([], state=st)
        self.assertEqual(out["clash"], [], "a banner on every sweep is a banner nobody reads")


class TheTransferBudgetFitsTheDevice(unittest.TestCase):
    """A desktop and a tablet run the same code and are not the same machine.

    Big transfers are bounded by BYTES rather than by a file count, which is what lets photographs
    overlap while a 100 MB video still goes alone. But the number itself was measured on a desktop,
    and a tablet's WebView is killed at a fraction of a desktop's ceiling — and killed by being
    RECREATED, which from the inside is the sweep ceasing to exist mid-file. Reported as "the tablet
    reloaded the screen again during sync"."""

    def setUp(self):
        with open(os.path.join(CLIENT, "syncexec.js"), encoding="utf-8") as fh:
            self.src = fh.read()

    def _budget(self, limit_js):
        js = """
        globalThis.performance = { memory: %s };
        require(%s); require(%s);
        process.stdout.write(String(require(%s).__budget || ''));
        """ % (limit_js, json.dumps(FOLDERSYNC), json.dumps(EXEC), json.dumps(EXEC))
        r = subprocess.run([NODE, "-e", js], capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr[-600:])
        return r.stdout.strip()

    def test_it_reads_the_runtime_ceiling_rather_than_a_fixed_number(self):
        self.assertIn("jsHeapSizeLimit", self.src,
                      "the budget is a desktop number applied to every device")

    def test_a_device_with_no_performance_memory_keeps_the_measured_desktop_figure(self):
        """performance.memory is Chromium-only. Absent, the safe answer is the value this was
        actually measured at, not zero and not unbounded."""
        i = self.src.index("const BIG_BUDGET")
        block = self.src[i:i + 700]
        self.assertIn("return DESK", block)
        self.assertIn("catch", block, "a throw reading performance.memory must not kill the sweep")

    def test_there_is_a_floor(self):
        """Scaled down without one, a device reporting a small ceiling would get a budget under a
        single file's cost — and then nothing would ever be allowed to start except by the
        nothing-in-flight escape, which is the serial behaviour all over again."""
        i = self.src.index("const BIG_BUDGET")
        self.assertIn("FLOOR", self.src[i:i + 700])
