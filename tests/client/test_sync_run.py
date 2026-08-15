"""The folder-sync executor, driven with fake adapters under node.

Run: venv-unified/bin/python -m unittest tests.client.test_sync_run

foldersync.js decides WHAT should happen; syncrun.js decides in what ORDER, and what to do when a
step fails. That is where a sync actually loses data, and none of it needs a filesystem to test —
the adapters are injected, so the fakes here can fail on demand, which a real disk will not do to
order.

Each case is one of the four ordering rules, or the thing that breaks if it is ignored:

  * a conflict renames the local copy BEFORE writing the incoming one. The other order means a crash
    in between has overwritten an edit that then exists nowhere.
  * `base` advances PER FILE and only for files that moved. Advancing the whole plan at the end turns
    one failed upload into a file silently deleted on the next sweep — base says synced, the scan
    says gone, so the engine reads it as "deleted here".
  * one failure is not a failed sweep. A single locked file must not block the folder forever.
  * an oversized file is REPORTED, and does not record agreement. A file that never syncs and never
    says so is the worst outcome available.
"""
import json
import os
import shutil
import subprocess
import textwrap
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
RUN = os.path.join(REPO, "static", "js", "client", "syncrun.js")
NODE = shutil.which("node") or shutil.which("nodejs")

HARNESS = r"""
const R = require(%s);

function makeFs(files, failures){
  const calls = [];
  const F = failures || {};
  return {
    calls,
    async scan(){ return { files: JSON.parse(JSON.stringify(files)), skipped: [] }; },
    async read(id, p){
      calls.push(['read', p]);
      if(F.read && F.read[p]) throw new Error(F.read[p]);
      return new Uint8Array([1,2,3]);
    },
    async write(id, p, bytes, mtime){
      calls.push(['write', p]);
      if(F.write && F.write[p]) throw new Error(F.write[p]);
      files[p] = { size: bytes.length, mtime: mtime || 1 };
      return { size: bytes.length, mtime: mtime || 1 };
    },
    async move(id, a, b){
      calls.push(['move', a, b]);
      if(F.move && F.move[a]) throw new Error(F.move[a]);
      files[b] = files[a]; delete files[a]; return true;
    },
    async trash(id, p){
      calls.push(['trash', p]);
      if(F.trash && F.trash[p]) throw new Error(F.trash[p]);
      delete files[p]; return '.pc-trash/x/' + p;
    },
  };
}

function makeStore(manifest, base, failures){
  const F = failures || {};
  const saved = [];
  return {
    saved,
    async manifest(){ return JSON.parse(JSON.stringify(manifest)); },
    async base(){ return JSON.parse(JSON.stringify(base)); },
    async getBlob(sha){ if(F.get && F.get[sha]) throw new Error(F.get[sha]); return new Uint8Array([9]); },
    async putBlob(){ if(F.put) throw new Error(F.put); return 'SHA_NEW'; },
    async save(id, s){ saved.push(JSON.parse(JSON.stringify(s))); },
  };
}
"""


@unittest.skipIf(not NODE, "no node on this node")
class TestSyncRun(unittest.TestCase):
    def run_js(self, body):
        js = (HARNESS % json.dumps(RUN)) + textwrap.dedent(body)
        r = subprocess.run([NODE, "-e", js], capture_output=True, text=True, timeout=90)
        if r.returncode != 0:
            raise AssertionError("node failed:\n" + r.stderr[-3000:])
        return json.loads(r.stdout)

    def test_a_conflict_renames_before_it_writes(self):
        out = self.run_js("""
          (async () => {
            const fs = makeFs({'doc.txt': {sha:'LOCAL', size:5, mtime:2000}});
            const store = makeStore({'doc.txt': {sha:'REMOTE', csum:'REMOTE', size:5, mtime:3000, device:'phone'}},
                                    {'doc.txt': {csum:'OLD', size:5, mtime:1000}});
            const rep = await R.sweep(fs, store, {id:'r1', device:'laptop', now:5000});
            process.stdout.write(JSON.stringify({calls: fs.calls, rep}));
          })();
        """)
        kinds = [c[0] for c in out["calls"]]
        self.assertEqual(kinds[0], "move",
                         "the local copy must be renamed BEFORE the incoming one is written — the "
                         "other order loses the edit if it crashes in between")
        self.assertEqual(kinds[1], "write")
        self.assertEqual(len(out["rep"]["conflicted"]), 1)

    def test_a_failed_upload_does_not_record_agreement(self):
        """The one that silently deletes files. If base says a file is synced and the next scan
        cannot find it, the engine reads that as 'deleted here' and removes it everywhere."""
        out = self.run_js("""
          (async () => {
            const fs = makeFs({'a.txt': {sha:'A', size:3, mtime:1}, 'b.txt': {sha:'B', size:3, mtime:1}},
                              {read: {'a.txt': 'EACCES'}});
            const store = makeStore({}, {});
            const rep = await R.sweep(fs, store, {id:'r1', device:'laptop', now:5000});
            process.stdout.write(JSON.stringify({rep, saved: store.saved}));
          })();
        """)
        rep, saved = out["rep"], out["saved"]
        self.assertEqual(rep["uploaded"], ["b.txt"])
        self.assertEqual([f["path"] for f in rep["failed"]], ["a.txt"])
        self.assertNotIn("a.txt", saved[-1]["base"],
                         "base recorded a file whose upload failed — the next sweep will read it as "
                         "deleted and remove it from every device")
        self.assertIn("b.txt", saved[-1]["base"])

    def test_one_failure_does_not_abort_the_sweep(self):
        out = self.run_js("""
          (async () => {
            const fs = makeFs({}, {write: {'x.txt': 'EBUSY'}});
            const store = makeStore({'x.txt': {sha:'X', size:1, mtime:1},
                                     'y.txt': {sha:'Y', size:1, mtime:1}}, {});
            const rep = await R.sweep(fs, store, {id:'r1', device:'laptop', now:5000});
            process.stdout.write(JSON.stringify(rep));
          })();
        """)
        self.assertEqual(out["downloaded"], ["y.txt"],
                         "a locked file must not block every other file in the folder")
        self.assertEqual(len(out["failed"]), 1)
        self.assertFalse(out["ok"])

    def test_an_oversized_file_is_reported_and_not_agreed(self):
        out = self.run_js("""
          (async () => {
            const fs = makeFs({'big.bin': {sha:'BIG', size:999, mtime:1}});
            const store = makeStore({}, {});
            const rep = await R.sweep(fs, store, {id:'r1', device:'laptop', now:5000, maxBytes:100});
            process.stdout.write(JSON.stringify({rep, saved: store.saved}));
          })();
        """)
        self.assertEqual(out["rep"]["uploaded"], [])
        self.assertTrue(any(s["path"] == "big.bin" and s["why"] == "too big"
                            for s in out["rep"]["skipped"]),
                        "an oversized file must be reported, never silently dropped")
        for s in out["saved"]:
            self.assertNotIn("big.bin", s["base"])

    def test_a_local_delete_becomes_a_remote_tombstone(self):
        out = self.run_js("""
          (async () => {
            const fs = makeFs({});
            const store = makeStore({'g.txt': {sha:'G', size:1, mtime:1}},
                                    {'g.txt': {sha:'G', size:1, mtime:1}});
            const rep = await R.sweep(fs, store, {id:'r1', device:'laptop', now:5000});
            process.stdout.write(JSON.stringify({rep, saved: store.saved}));
          })();
        """)
        self.assertEqual(out["rep"]["removedRemote"], ["g.txt"])
        self.assertEqual(out["saved"][-1]["manifest"]["g.txt"]["deletedAt"], 5000,
                         "the manifest must carry a tombstone so other devices learn of the delete")

    def test_a_remote_delete_goes_to_the_trash_not_to_unlink(self):
        out = self.run_js("""
          (async () => {
            const fs = makeFs({'g.txt': {sha:'G', size:1, mtime:1}});
            const store = makeStore({'g.txt': {deletedAt: 4000}},
                                    {'g.txt': {sha:'G', size:1, mtime:1}});
            const rep = await R.sweep(fs, store, {id:'r1', device:'laptop', now:5000});
            process.stdout.write(JSON.stringify({calls: fs.calls, rep}));
          })();
        """)
        self.assertEqual([c[0] for c in out["calls"]], ["trash"])
        self.assertEqual(out["rep"]["trashed"][0]["path"], "g.txt")

    def test_a_settled_sweep_writes_nothing_and_the_next_one_is_quiet(self):
        """The manifest already carries the checksum, so there is nothing left to learn or record."""
        out = self.run_js("""
          (async () => {
            const scan = {'a.txt': {sha:'A', size:3, mtime:1000}};          // a scan reports the FILE's hash
            const entry = {'a.txt': {csum:'A', size:3, mtime:1000}};        // an entry carries it as csum
            const fs = makeFs(JSON.parse(JSON.stringify(scan)));
            const store = makeStore(JSON.parse(JSON.stringify(entry)), JSON.parse(JSON.stringify(entry)));
            const rep = await R.sweep(fs, store, {id:'r1', device:'laptop', now:5000});
            process.stdout.write(JSON.stringify({rep, calls: fs.calls, saves: store.saved.length}));
          })();
        """)
        self.assertEqual(out["rep"]["unchanged"], 1)
        self.assertEqual(out["calls"], [], "a settled folder must touch no files")
        self.assertEqual(out["saves"], 0, "a settled folder must not rewrite the manifest")

    def test_a_folder_missing_checksums_records_them_ONCE(self):
        """The repair is a write, and it has to be a one-off.

        An entry with no `csum` cannot be compared by content, so every device that hashes falls back
        to size+mtime and conflicts — which is why a sweep records what it hashed. But a repair that
        did not converge would rewrite the whole manifest on every sweep for ever, and each rewrite
        of a large folder is a fresh multi-megabyte blob.
        """
        out = self.run_js("""
          (async () => {
            const scan  = {'a.txt': {sha:'A', size:3, mtime:1000}};
            const entry = {'a.txt': {size:3, mtime:1000}};                  // legacy: no identity at all
            const fs = makeFs(JSON.parse(JSON.stringify(scan)));
            const store = makeStore(JSON.parse(JSON.stringify(entry)), JSON.parse(JSON.stringify(entry)));
            const first = await R.sweep(fs, store, {id:'r1', device:'laptop', now:5000});
            // The store now holds what the first sweep wrote — the same thing a real one would read.
            const stored = store.saved[store.saved.length - 1].manifest;
            const store2 = makeStore(JSON.parse(JSON.stringify(stored)), JSON.parse(JSON.stringify(stored)));
            const second = await R.sweep(makeFs(JSON.parse(JSON.stringify(scan))), store2,
                                         {id:'r1', device:'laptop', now:6000});
            process.stdout.write(JSON.stringify({
              firstSaves: store.saved.length, repaired: first.repaired || 0,
              csum: stored['a.txt'].csum || null,
              secondSaves: store2.saved.length, secondRepaired: second.repaired || 0 }));
          })();
        """)
        self.assertEqual(out["repaired"], 1, "the sweep should record the hash it computed")
        self.assertEqual(out["csum"], "A", "and record it as the content identity")
        self.assertEqual(out["firstSaves"], 1)
        self.assertEqual(out["secondRepaired"], 0, "nothing left to repair once it is recorded")
        self.assertEqual(out["secondSaves"], 0, "so the second sweep writes nothing at all")

    def test_stopping_halts_the_sweep_that_is_running(self):
        """Pause used to set a flag the POLICY reads — which decides whether a sweep may START. So
        pressing it during a sweep of several hundred files did nothing for as long as that sweep
        took, and the button plainly lied. It has to stop THIS run: the file in flight finishes,
        what has been agreed is stored, and the next run resumes from there."""
        out = self.run_js("""
          (async () => {
            const files = {};
            for(let i = 0; i < 8; i++) files['f' + i + '.txt'] = {sha:'S' + i, size:3, mtime:1};
            const fs = makeFs(files);
            const store = makeStore({}, {});
            let seen = 0;
            const rep = await R.sweep(fs, store, {id:'r1', device:'laptop', now:5000,
              shouldStop: () => (++seen > 3)});      // asked to stop partway through
            process.stdout.write(JSON.stringify({ stopped: !!rep.stopped,
              uploaded: rep.uploaded.length, saves: store.saved.length }));
          })();
        """)
        self.assertTrue(out["stopped"], "the report must say it was stopped")
        self.assertLess(out["uploaded"], 8, "it must not have finished the whole plan")
        self.assertGreaterEqual(out["saves"], 1,
                                "what it DID agree has to be stored, or the work is thrown away")

    def test_dry_run_touches_nothing(self):
        out = self.run_js("""
          (async () => {
            const fs = makeFs({'a.txt': {sha:'A', size:3, mtime:1}});
            const store = makeStore({}, {});
            const rep = await R.sweep(fs, store, {id:'r1', device:'laptop', now:5000, dryRun:true});
            process.stdout.write(JSON.stringify({rep, calls: fs.calls, saves: store.saved.length}));
          })();
        """)
        self.assertEqual(out["calls"], [])
        self.assertEqual(out["saves"], 0)
        self.assertEqual(len(out["rep"]["plan"]["upload"]), 1,
                         "a dry run must still say what it WOULD do")

    def test_excluded_paths_never_reach_the_filesystem(self):
        out = self.run_js("""
          (async () => {
            const fs = makeFs({'a.jpg': {sha:'A', size:3, mtime:1}, 'Old/x.jpg': {sha:'X', size:3, mtime:1}});
            const store = makeStore({}, {});
            const rep = await R.sweep(fs, store, {id:'r1', device:'laptop', now:5000, excludes:['Old']});
            process.stdout.write(JSON.stringify({rep, calls: fs.calls}));
          })();
        """)
        self.assertEqual(out["rep"]["uploaded"], ["a.jpg"])
        self.assertEqual([c[1] for c in out["calls"]], ["a.jpg"])
        self.assertEqual(out["rep"]["excluded"], 1)

    def test_excluding_a_folder_leaves_it_intact_in_the_manifest(self):
        """Exclude a folder on the phone; the laptop must still have it.

        The engine already refuses to PLAN a deletion for an excluded path. This is the last link:
        the manifest written back must still CONTAIN those paths. If the executor rebuilt the manifest
        from what it just saw locally instead of from what was already there, excluding a folder on
        one device would erase it from every other one — the same outcome, one step later.

        There is a NEW local file in this scenario on purpose. Without one nothing is dirty, no
        manifest is written at all, and the assertions below pass vacuously — which is exactly what
        the first version of this test did, and it survived a mutation that emptied the manifest.
        """
        out = self.run_js("""
          (async () => {
            const fs = makeFs({'keep.jpg': {sha:'K', size:3, mtime:1},
                               'new.jpg':  {sha:'N', size:3, mtime:1}});   // forces a save
            const store = makeStore({'keep.jpg': {sha:'K', size:3, mtime:1},
                                     'Old/a.jpg': {sha:'A', size:9, mtime:1},
                                     'Old/deep/b.jpg': {sha:'B', size:9, mtime:1}},
                                    {'keep.jpg': {sha:'K', size:3, mtime:1},
                                     'Old/a.jpg': {sha:'A', size:9, mtime:1},
                                     'Old/deep/b.jpg': {sha:'B', size:9, mtime:1}});
            const rep = await R.sweep(fs, store, {id:'r1', device:'phone', now:5000, excludes:['Old']});
            process.stdout.write(JSON.stringify({rep, saved: store.saved, calls: fs.calls}));
          })();
        """)
        rep, saved = out["rep"], out["saved"]
        self.assertEqual(rep["removedRemote"], [],
                         "excluding a folder proposed removing it from the other devices")
        self.assertEqual(rep["trashed"], [])
        self.assertEqual([c[1] for c in out["calls"]], ["new.jpg"],
                         "an excluded folder must not be touched on disk either")
        self.assertTrue(saved, "the harness must produce a manifest write, or this test proves nothing")
        m = saved[-1]["manifest"]
        self.assertIn("Old/a.jpg", m, "the excluded folder was dropped from the shared manifest — every "
                                      "other device would read that as 'deleted elsewhere'")
        self.assertIn("Old/deep/b.jpg", m)
        self.assertFalse(m["Old/a.jpg"].get("deletedAt"), "it was tombstoned, which is a delete")

    def test_deleting_an_excluded_folder_locally_is_safe(self):
        """The reason someone excludes a folder on a phone is to get the space back. Deleting the
        local copy afterwards must still not touch anyone else's."""
        out = self.run_js("""
          (async () => {
            const fs = makeFs({});                                  // the phone has nothing left
            const store = makeStore({'Old/a.jpg': {sha:'A', size:9, mtime:1}},
                                    {'Old/a.jpg': {sha:'A', size:9, mtime:1}});
            const rep = await R.sweep(fs, store, {id:'r1', device:'phone', now:5000, excludes:['Old']});
            process.stdout.write(JSON.stringify({rep, saved: store.saved}));
          })();
        """)
        self.assertEqual(out["rep"]["removedRemote"], [])
        self.assertEqual(out["saved"], [], "nothing changed, so the manifest should not be rewritten")

    # ---- the sweep that emptied a Pictures folder ------------------------------------------

    def _tombstoned_folder(self, n, extra=""):
        """N files on this disk, N tombstones in the shared manifest, and NO agreement — the exact
        state a real Pictures folder was in: the manifest held ~10k paths and every one of them was
        marked deleted (`n=0` live on the server), while the files were all still here.

        `base` is empty because the folder had just been re-added, which is the whole reason the
        engine ends up guessing: with an agreement, an untouched file compares equal to it and the
        delete is a fact rather than an inference.
        """
        return """
          (async () => {
            const N = %d;
            const files = {}, manifest = {};
            for(let i=0;i<N;i++){
              files['p'+i+'.jpg'] = { sha:'C'+i, size:10, mtime:1000 };
              manifest['p'+i+'.jpg'] = { deletedAt: 9000 };     // tombstoned AFTER the local mtime
            }
            const fs = makeFs(files);
            const store = makeStore(manifest, {});
            const rep = await R.sweep(fs, store, {id:'r1', device:'windows', now:99000%s});
            process.stdout.write(JSON.stringify({
              trashed: rep.trashed.length, refused: rep.refusedTrash || null,
              left: Object.keys(files).length,
              agreed: store.saved.length ? Object.keys(store.saved[store.saved.length-1].base||{}).length : 0,
              saves: store.saved.length,
            }));
          })();
        """ % (n, extra)

    def test_a_sweep_that_would_empty_the_folder_trashes_nothing(self):
        out = self.run_js(self._tombstoned_folder(500))
        self.assertEqual(out["trashed"], 0,
                         "the sweep moved the whole folder to the trash without being asked — this "
                         "is the Pictures wipe")
        self.assertEqual(out["left"], 500, "every file must still be on the disk")
        self.assertIsNotNone(out["refused"], "a refusal that says nothing is a silent failure")
        self.assertEqual(out["refused"]["n"], 500)

    def test_the_refusal_is_re_asked_every_sweep_not_recorded_once(self):
        """A refused delete must NOT advance `base`. If it did, the next sweep would compare against
        an agreement saying those files are deleted, decide there is nothing to do, and the question
        would never be asked again — a guard that silently gives up is worse than no guard, because
        the folder then never syncs and never says why."""
        out = self.run_js(self._tombstoned_folder(500))
        self.assertEqual(out["agreed"], 0,
                         "base recorded the deletions the sweep refused to make")

    def test_saying_yes_lets_a_real_mass_delete_through(self):
        """The other half, and the one the contacts sweep learned by breaking: a guard that cannot
        be answered turns 'it deleted everything' into 'it syncs nothing, for ever'. Deleting 500
        photos on your phone has to be able to reach this device."""
        out = self.run_js(self._tombstoned_folder(500, ", confirmTrash: async () => true"))
        self.assertEqual(out["trashed"], 500)
        self.assertEqual(out["left"], 0)
        self.assertIsNone(out["refused"])

    def test_without_the_guard_the_whole_folder_goes_to_the_trash(self):
        """PROOF THE CHECK ABOVE IS NOT VACUOUS. `forceTrash` is byte-for-byte the behaviour that
        shipped, so this asserts the scenario really does produce 500 deletions — otherwise the
        three tests above would pass just as happily against a sweep that had nothing to refuse."""
        out = self.run_js(self._tombstoned_folder(500, ", forceTrash: true"))
        self.assertEqual(out["trashed"], 500)
        self.assertEqual(out["left"], 0)

    def test_an_automatic_sweep_is_never_allowed_to_ask(self):
        """No confirmTrash at all — the watcher, a resume, the heartbeat. There is nobody in front of
        a background sweep, so it must fail CLOSED rather than block on a dialog nobody answers."""
        out = self.run_js(self._tombstoned_folder(100))
        self.assertEqual(out["trashed"], 0)
        self.assertIsNotNone(out["refused"])

    def test_a_confirm_that_throws_is_a_no(self):
        out = self.run_js(self._tombstoned_folder(100, ", confirmTrash: async () => { throw new Error('x'); }"))
        self.assertEqual(out["trashed"], 0, "a broken dialog must not read as consent")
        self.assertIsNotNone(out["refused"])

    def test_an_ordinary_small_delete_is_never_questioned(self):
        """Three files deleted on another device, 40 still here. This is the feature working, and it
        must not raise a dialog — a guard people are trained to click through protects nothing."""
        out = self.run_js("""
          (async () => {
            const files = {}, manifest = {}, base = {};
            for(let i=0;i<40;i++){
              files['k'+i+'.jpg'] = { sha:'K'+i, csum:'K'+i, size:10, mtime:1000 };
              manifest['k'+i+'.jpg'] = { sha:'K'+i, csum:'K'+i, size:10, mtime:1000 };
              base['k'+i+'.jpg'] = { csum:'K'+i, size:10, mtime:1000 };
            }
            for(let i=0;i<3;i++){
              files['d'+i+'.jpg'] = { sha:'D'+i, csum:'D'+i, size:10, mtime:1000 };
              manifest['d'+i+'.jpg'] = { deletedAt: 9000 };
              base['d'+i+'.jpg'] = { csum:'D'+i, size:10, mtime:1000 };
            }
            const fs = makeFs(files);
            const store = makeStore(manifest, base);
            const rep = await R.sweep(fs, store, {id:'r1', device:'windows', now:99000});
            process.stdout.write(JSON.stringify({trashed: rep.trashed.length,
                                                 refused: rep.refusedTrash || null,
                                                 left: Object.keys(files).length}));
          })();
        """)
        self.assertEqual(out["trashed"], 3)
        self.assertIsNone(out["refused"], "an everyday 3-file delete must not ask")
        self.assertEqual(out["left"], 40)


if __name__ == "__main__":
    unittest.main()
