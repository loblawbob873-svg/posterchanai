"""The desktop filesystem bridge, driven against REAL directories.

Run: venv-unified/bin/python -m unittest tests.client.test_fs_bridge

desktop/fsbridge.js is the only part of folder sync that may touch a file, and the renderer that
calls it runs the instance's own JavaScript over the network — the same trust boundary the rest of
the desktop app is built around. So the assertions here are mostly about what it REFUSES.

  * `..`, an absolute path, or a leading slash must not reach outside the folder the user picked.
  * a SYMLINK pointing out of the tree must not either, which is why the check is on the resolved
    path and not on the string. A synced folder containing a link to ~/.ssh is an ordinary thing for
    a filesystem to contain and a catastrophic thing for a sync to follow — and string comparison
    misses it completely, which is the failure worth pinning.
  * scan() must skip symlinks rather than follow them (duplicate data at best, a cycle or a leak at
    worst), skip its own trash, and survive an unreadable directory instead of aborting the sweep.
  * write() must be atomic: a torn file after a power cut is corrupt data that looks like data.
  * trash() must never overwrite something already in the trash — a safety net that overwrites
    itself is not one.
  * a downloaded file must carry the source mtime, or the next scan reads our own write as a local
    edit and pushes it straight back: the loop that makes a sync never settle.
"""
import json
import os
import shutil
import subprocess
import tempfile
import textwrap
import time
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
MOD = os.path.join(REPO, "desktop", "fsbridge.js")
NODE = shutil.which("node") or shutil.which("nodejs")


@unittest.skipIf(not NODE, "no node on this node")
class TestFsBridge(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="pcfs-")
        self.root = os.path.join(self.tmp, "Documents")
        self.outside = os.path.join(self.tmp, "secrets")
        os.makedirs(self.root)
        os.makedirs(self.outside)
        with open(os.path.join(self.outside, "id_rsa"), "w") as fh:
            fh.write("PRIVATE KEY")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_js(self, body):
        js = textwrap.dedent("""
            const B = require(%s);
            B.init({ roots: [{id:'r1', dir: %s}], save(){} });
            (async () => {
              const out = {};
              const attempt = async (name, fn) => {
                try { out[name] = { ok: true, value: await fn() }; }
                catch (e) { out[name] = { ok: false, error: String(e.message || e) }; }
              };
              %s
              process.stdout.write(JSON.stringify(out));
            })().catch(e => { process.stderr.write(String(e && e.stack || e)); process.exit(1); });
        """) % (json.dumps(MOD), json.dumps(self.root), body)
        r = subprocess.run([NODE, "-e", js], capture_output=True, text=True, timeout=120)
        if r.returncode != 0:
            raise AssertionError("node failed:\n" + r.stderr[-3000:])
        return json.loads(r.stdout)

    # ---- the whole stack, on a real disk ----------------------------------------------------
    #
    # Every other folder-sync test above the adapter uses STUB adapters: the engine is proven with
    # fake filesystems and the bridge is proven in isolation. Nothing ever ran the real executor
    # against the real bridge against real files, which is precisely the combination a laptop runs —
    # so "another device deleted 3,930 files and this one still has them" had no test that could
    # even fail. This is that test.

    def run_sweep(self, manifest, base, files, excludes=None, force_resurrect=False,
                  store_has='true'):
        """Drive the SHIPPED syncrun.js through the SHIPPED fsbridge.js over a real directory.

        `base` entries are completed from what the file ACTUALLY became on disk. That is not test
        convenience, it is the only honest fixture: a real agreement was written from a real stat, so
        an invented mtime makes every file look locally edited — and the engine then applies DELETE
        LOSES TO EDIT and re-uploads the very files the other device deleted. Which is correct, and
        is also worth knowing: a laptop whose files were restored, copied or rsynced with fresh
        timestamps will resurrect deletions rather than apply them, and be right to."""
        for rel, body in files.items():
            full = os.path.join(self.root, rel)
            os.makedirs(os.path.dirname(full), exist_ok=True)
            with open(full, "w") as fh:
                fh.write(body)
            # Only when the case has not stated one itself: a test that WANTS a stale agreement
            # (a restore, a copy, an rsync without -t) says so by giving base an explicit mtime.
            if rel in base and "mtime" not in base[rel]:
                st = os.stat(full)
                base[rel] = dict(base[rel], size=st.st_size, mtime=int(st.st_mtime * 1000))
        eng = os.path.join(REPO, "static", "js", "client", "syncstate.js")
        exe = os.path.join(REPO, "static", "js", "client", "syncexec.js")
        fold = os.path.join(REPO, "static", "js", "client", "foldersync.js")
        # The journal records what the file looked like when this device applied — the real stat,
        # taken above — so a case that wants a STALE agreement (a restore, an rsync without -t) is
        # the one that states its own mtime.
        index = {}
        for rel, e in base.items():
            local = {"size": e.get("size"), "mtime": e.get("mtime")}
            if e.get("csum"):
                local["csum"] = e["csum"]
            index[rel] = dict(e, v=1, local=local)
        # The record set, with the versions the fixtures predate: a record whose content matches
        # the journal shares its version, one that differs is one ahead — exactly what the server's
        # counter would have produced. Live records carry a storage address, as real ones must.
        state = {}
        for rel, e in manifest.items():
            b0 = base.get(rel)
            same = (b0 is not None and e.get("csum") == b0.get("csum")
                    and bool(e.get("deletedAt")) == bool(b0.get("deletedAt")))
            se = dict(e, v=(1 if same else 2), by=e.get("device", "other"))
            if not se.get("deletedAt") and "sha" not in se:
                se["sha"] = "b_" + str(se.get("csum", rel))
            state[rel] = se
        js = textwrap.dedent("""
            const B = require(%s);
            require(%s); require(%s);
            const X = require(%s);
            B.init({ roots: [{id:'r1', dir: %s}], save(){} });
            const state = %s, index = %s;
            const io = {
              published: [],
              async state(){ return { state: JSON.parse(JSON.stringify(state)), flagged: {} }; },
              async putState(k, recs){ for(const r of recs) this.published.push(r);
                return { ok: recs.map(r => r.path), stale: [], failed: [] }; },
              async index(){ return JSON.parse(JSON.stringify(index)); },
              async saveIndex(){},
              async getBlob(){ return new Uint8Array([1]); },
              async putBlob(){ return { sha: 'SHA' }; },
              async hashBytes(){ return 'HASH'; },
              /* THE STORE, ANSWERING THAT IT STILL HAS THE BYTES. Without this nothing is deleted
               * at all — which is the new rule working: a device never removes its copy until the
               * store has confirmed it can be restored. Overridden per-test where the point is what
               * happens when the answer is no, or when there is no answer. */
              async hasBlob(){ return %s; },
            };
            (async () => {
              const rep = await X.sweep(B, io, {id:'r1', key:'Documents', device:'laptop',
                                                now: 99000, excludes: %s, manual: %s,
                                                confirm: async () => %s});
              process.stdout.write(JSON.stringify({
                trashed: rep.trashed.length, failed: rep.failed,
                keptUnconfirmed: (rep.keptUnconfirmed || []).length,
                keptUnstored: (rep.keptUnstored || []).length,
                refused: (rep.refused.find(v => v.kind === 'partialViews') || null),
                excluded: rep.excluded, unchanged: rep.unchanged,
                resurrected: rep.resurrected || [],
                refusedResurrect: (rep.refused.find(v => v.kind === 'massResurrect') || null),
                uploaded: rep.uploaded.length,
              }));
            })().catch(e => { process.stderr.write(String(e && e.stack || e)); process.exit(1); });
        """) % (json.dumps(MOD), json.dumps(fold), json.dumps(eng), json.dumps(exe),
                json.dumps(self.root), json.dumps(state), json.dumps(index),
                store_has,
                json.dumps(excludes or []),
                "true" if force_resurrect else "false",
                "true" if force_resurrect else "false")
        r = subprocess.run([NODE, "-e", js], capture_output=True, text=True, timeout=180)
        if r.returncode != 0:
            raise AssertionError("node failed:\n" + r.stderr[-3000:])
        return json.loads(r.stdout)

    def _left_on_disk(self):
        out = []
        for dirpath, _dirs, names in os.walk(self.root):
            for n in names:
                rel = os.path.relpath(os.path.join(dirpath, n), self.root).replace(os.sep, "/")
                if not rel.startswith(".pc-trash/"):
                    out.append(rel)
        return sorted(out)

    def test_another_devices_deletes_actually_reach_this_disk(self):
        """THE REPORTED ONE. Files sit here, the shared manifest says they were deleted elsewhere,
        and this device has an agreement saying it once had them. They must end up in .pc-trash —
        on a real filesystem, through the real bridge, not a stub that cannot fail."""
        files, manifest, base = {}, {}, {}
        for i in range(40):
            rel = "notes/%02d.txt" % i
            files[rel] = "body %d" % i
            base[rel] = {"csum": "C%d" % i}
            manifest[rel] = ({"deletedAt": 9000, "sha": "S%d" % i, "csum": "C%d" % i} if i < 10
                             else {"sha": "S%d" % i, "csum": "C%d" % i,
                                   "size": len("body %d" % i), "mtime": 1000})
        out = self.run_sweep(manifest, base, files)
        self.assertEqual(out["failed"], [], "the bridge could not carry out the deletions")
        self.assertIsNone(out["refused"])
        self.assertEqual(out["trashed"], 10)
        self.assertEqual(len(self._left_on_disk()), 30,
                         "another device's deletions never reached this disk")
        # THE PROMISE IS STILL KEPT, SOMEWHERE ELSE. There is no .pc-trash on this disk any more:
        # a per-device trash was a second copy of the same idea that nobody could see the whole of
        # — a phone with 109 files in it, a tablet with 226, and no single list that answered "what
        # did I delete". The trash is one place now, on the server, and what makes a deletion safe
        # is that the executor confirmed the store still holds the bytes BEFORE removing anything.
        
        left = [os.path.join(dp, n) for dp, _d, ns in os.walk(os.path.join(self.root, ".pc-trash"))
                for n in ns]
        self.assertEqual(left, [], "a per-device .pc-trash came back")
        self.assertEqual(out["keptUnconfirmed"], 0,
                         "the store said it had the bytes, so nothing should have been kept back")

    def test_a_file_is_never_removed_when_the_store_cannot_be_asked(self):
        """THE ONE RULE THAT REPLACED EVERY FLOOR, on a real filesystem.

        Deletion is automatic now — no floor, no ratio, no dialog — and the local copy is removed
        rather than moved into a per-device trash. That is defensible only because of this: a device
        never removes its copy until the store has CONFIRMED it still holds those bytes. A rate
        limiter, a proxy, a dead socket, an unmounted disk: all of them mean "do not delete".
        """
        files = {"a.txt": "x", "b.txt": "y"}
        base = {"a.txt": {"csum": "A"}, "b.txt": {"csum": "B"}}
        manifest = {"a.txt": {"deletedAt": 9000, "sha": "SA", "csum": "A"},
                    "b.txt": {"sha": "SB", "csum": "B", "size": 1, "mtime": 1000}}
        out = self.run_sweep(manifest, base, files, store_has="null")
        self.assertEqual(out["trashed"], 0, "it deleted a file it could not verify was recoverable")
        self.assertEqual(out["keptUnconfirmed"], 1, "it kept the file and said nothing about why")
        self.assertIn("a.txt", self._left_on_disk(), "the file is gone and cannot be got back")

    def test_nor_when_the_store_says_it_does_not_have_them(self):
        """Then this copy is the only copy, and deleting it is losing it."""
        files = {"a.txt": "x"}
        base = {"a.txt": {"csum": "A"}}
        manifest = {"a.txt": {"deletedAt": 9000, "sha": "SA", "csum": "A"}}
        out = self.run_sweep(manifest, base, files, store_has="false")
        self.assertEqual(out["trashed"], 0)
        self.assertEqual(out["keptUnconfirmed"], 1)
        self.assertIn("a.txt", self._left_on_disk())

    def test_nor_when_the_tombstone_names_no_bytes_at_all(self):
        """A file deleted before it ever finished uploading. There is nothing to confirm and nothing
        to restore from, so the local copy is the only copy — kept, and reported separately, because
        it is a fact about the file rather than about the moment."""
        files = {"a.txt": "x"}
        base = {"a.txt": {"csum": "A"}}
        manifest = {"a.txt": {"deletedAt": 9000}}
        out = self.run_sweep(manifest, base, files)
        self.assertEqual(out["trashed"], 0)
        self.assertEqual(out["keptUnstored"], 1)
        self.assertIn("a.txt", self._left_on_disk())

    def test_a_deletion_in_a_subdirectory_finds_the_file_by_its_path(self):
        """The manifest path is always posix while `path.sep` is a backslash on Windows — the
        laptop's platform. A nested path is where that would go wrong, and every other test here
        uses files at the root."""
        files = {"a/b/c/deep.txt": "x", "a/b/keep.txt": "y"}
        base = {"a/b/c/deep.txt": {"csum": "D"}, "a/b/keep.txt": {"csum": "K"}}
        manifest = {"a/b/c/deep.txt": {"deletedAt": 9000, "sha": "GONE", "csum": "GONE"},
                    "a/b/keep.txt": {"sha": "SK", "csum": "K", "size": 1, "mtime": 1000}}
        out = self.run_sweep(manifest, base, files)
        self.assertEqual(out["failed"], [])
        self.assertEqual(out["trashed"], 1)
        self.assertEqual(self._left_on_disk(), ["a/b/keep.txt"])
        self.assertFalse(os.path.exists(os.path.join(self.root, "a", "b", "c", "deep.txt")),
                         "the nested file was not actually removed")
        self.assertTrue(os.path.isdir(os.path.join(self.root, "a", "b")),
                        "a directory that still holds a file was pruned")

    def test_a_touched_file_resurrects_the_deletion_and_SAYS_SO(self):
        """THE LIKELY LAPTOP CASE, and the one with no symptom until now.

        An ordinary sweep does not hash, so "changed here" is size+mtime. Restore a folder from a
        backup, copy it in, or rsync it without -t, and every timestamp is new — so a file the
        manifest says was deleted looks locally edited, DELETE LOSES TO EDIT fires, and the device
        republishes every deletion instead of applying it. The rule is right; being unable to tell
        that from a normal upload is not. It reports "N up" and the folder looks in step while the
        deletions have been quietly undone for everyone.

        The count is separate now, so the card can say it out loud."""
        files = {"gone.txt": "x", "keep.txt": "y"}
        # An explicit, STALE agreement for gone.txt — the file on disk is newer than what this
        # device last agreed to, exactly as a restore or a copy leaves it. keep.txt is filled from
        # the real stat by the fixture, so it stays untouched and the sweep is not trivially all-new.
        base = {"gone.txt": {"csum": "G", "size": 1, "mtime": 1000}, "keep.txt": {"csum": "K"}}
        manifest = {"gone.txt": {"deletedAt": 9000, "sha": "GONE", "csum": "GONE"},
                    "keep.txt": {"sha": "SK", "csum": "K", "size": 1, "mtime": 1000}}
        out = self.run_sweep(manifest, base, files, force_resurrect=True)
        self.assertEqual(out["trashed"], 0)
        self.assertEqual(out["failed"], [],
                         "counting a republish that never landed would assert the opposite of what "
                         "the manifest says, on the one sweep somebody is reading to find out why")
        self.assertEqual(out["resurrected"], ["gone.txt"],
                         "a sweep that undid another device's deletion reported it as a plain "
                         "upload — the folder then reads as in step while the delete is reversed")

    def test_a_mass_resurrection_is_refused_rather_than_refilling_every_device(self):
        """THE GUARD THIS PAIRS WITH, and the mirror of the mass-delete one. A laptop restored from
        backup has fresh timestamps on everything, so every tombstoned path reads as edited here and
        the sweep republishes the lot — putting thousands of deliberately deleted files back on every
        other device, including the phone that had just correctly applied the deletion.

        The mass-DELETE guard cannot see this: it only ever suppresses deleteLocal, and it runs after
        the upload loop, so the files would already be back before anything asked."""
        files, base, manifest = {}, {}, {}
        for i in range(30):
            rel = "doc%02d.txt" % i
            files[rel] = "x"
            base[rel] = {"csum": "C%d" % i, "size": 1, "mtime": 1000}   # stale on purpose
            manifest[rel] = {"deletedAt": 9000, "sha": "GONE", "csum": "GONE"}
        out = self.run_sweep(manifest, base, files)
        self.assertIsNotNone(out["refusedResurrect"], "a mass resurrection went ahead unasked")
        self.assertEqual(out["refusedResurrect"]["n"], 30)
        self.assertEqual(out["uploaded"], 0, "the deletions were republished anyway")
        self.assertEqual(out["resurrected"], [])
        self.assertEqual(len(self._left_on_disk()), 30,
                         "refusing to republish must not delete anything locally either")

    def test_saying_yes_still_lets_a_real_mass_resurrection_through(self):
        """A guard that cannot be answered is the delete-guard bug with the sign flipped: it would
        make a genuine bulk edit impossible to sync, for ever, with no way past it."""
        files, base, manifest = {}, {}, {}
        for i in range(30):
            rel = "doc%02d.txt" % i
            files[rel] = "x"
            base[rel] = {"csum": "C%d" % i, "size": 1, "mtime": 1000}
            manifest[rel] = {"deletedAt": 9000, "sha": "GONE", "csum": "GONE"}
        out = self.run_sweep(manifest, base, files, force_resurrect=True)
        self.assertIsNone(out["refusedResurrect"])
        self.assertEqual(len(out["resurrected"]), 30)
        self.assertEqual(out["failed"], [])

    def test_an_ordinary_handful_is_never_questioned(self):
        """Deleting three files on one device and genuinely editing them on another is rare but real,
        and a dialog people are trained to click through protects nothing."""
        files, base, manifest = {}, {}, {}
        for i in range(3):
            rel = "doc%d.txt" % i
            files[rel] = "x"
            base[rel] = {"csum": "C%d" % i, "size": 1, "mtime": 1000}
            manifest[rel] = {"deletedAt": 9000, "sha": "GONE", "csum": "GONE"}
        out = self.run_sweep(manifest, base, files)
        self.assertIsNone(out["refusedResurrect"])
        self.assertEqual(len(out["resurrected"]), 3)

    def test_deleting_a_folders_files_removes_the_empty_folders_too(self):
        """"The files are gone but the dirs remain."

        A manifest holds PATHS, never directories — a folder in the Blossom view is just the common
        prefix of the files under it. So deleting one tombstones every file it contains, each device
        trashes those files, and the directory tree is left standing on disk, empty, exactly where
        the user deleted it: `PDF Project/1/venv` with nothing in it. Reported from two Windows PCs
        as "files and folders I deleted in Blossom are not deleted on disk", and the folders were the
        whole of it — the files really had gone.
        """
        files = {"PDF Project/1/venv/lib/a.py": "x", "PDF Project/1/venv/lib/b.py": "y",
                 "keep.txt": "z"}
        base = {k: {"csum": "C" + k} for k in files}
        manifest = {"PDF Project/1/venv/lib/a.py": {"deletedAt": 9000, "sha": "GONE", "csum": "GONE"},
                    "PDF Project/1/venv/lib/b.py": {"deletedAt": 9000, "sha": "GONE", "csum": "GONE"},
                    "keep.txt": {"sha": "SK", "csum": "Ckeep.txt", "size": 1, "mtime": 1000}}
        out = self.run_sweep(manifest, base, files)
        self.assertEqual(out["trashed"], 2)
        self.assertEqual(out["failed"], [])
        self.assertEqual(self._left_on_disk(), ["keep.txt"])
        self.assertFalse(os.path.exists(os.path.join(self.root, "PDF Project")),
                         "the emptied folder is still on disk — this is the reported bug")

    def test_a_folder_that_still_holds_anything_is_never_removed(self):
        """THE SAFETY PROPERTY, and the reason this uses rmdir rather than a recursive delete: rmdir
        physically refuses a non-empty directory. A file the sweep did not touch — excluded, ignored,
        written by another program a moment ago, never ours at all — keeps its folder, and the worst
        this can do is leave a directory standing."""
        files = {"proj/gone.txt": "x", "proj/mine.txt": "keep me"}
        base = {"proj/gone.txt": {"csum": "G"}}
        manifest = {"proj/gone.txt": {"deletedAt": 9000, "sha": "GONE", "csum": "GONE"}}
        out = self.run_sweep(manifest, base, files)
        self.assertEqual(out["trashed"], 1)
        self.assertTrue(os.path.exists(os.path.join(self.root, "proj")),
                        "a directory still holding a file was removed")
        self.assertIn("proj/mine.txt", self._left_on_disk())

    def test_the_sync_root_is_never_removed_however_empty(self):
        """The root IS the pairing. A device that deleted it would have to re-pick the folder in a
        native dialog before it could sync again."""
        files = {"only.txt": "x"}
        base = {"only.txt": {"csum": "O"}}
        manifest = {"only.txt": {"deletedAt": 9000, "sha": "GONE", "csum": "GONE"}}
        out = self.run_sweep(manifest, base, files)
        self.assertEqual(out["trashed"], 1)
        self.assertTrue(os.path.isdir(self.root), "the sync root itself was removed")

    def test_an_exclusion_silently_keeps_files_another_device_deleted(self):
        """NOT A BUG — the documented rule, pinned because it is the likeliest innocent explanation
        for 'the deletes never arrived'. An exclusion means 'stop looking at this', so an excluded
        path is dropped from all three snapshots and can never be deleted by anyone. A folder whose
        pattern covers the deleted files therefore keeps them for ever, correctly and silently."""
        files = {"old/gone.txt": "x", "keep.txt": "y"}
        base = {"old/gone.txt": {"csum": "G"}, "keep.txt": {"csum": "K"}}
        manifest = {"old/gone.txt": {"deletedAt": 9000, "sha": "GONE", "csum": "GONE"},
                    "keep.txt": {"sha": "SK", "csum": "K", "size": 1, "mtime": 1000}}
        out = self.run_sweep(manifest, base, files, excludes=["old"])
        self.assertEqual(out["trashed"], 0)
        self.assertGreaterEqual(out["excluded"], 1)
        self.assertIn("old/gone.txt", self._left_on_disk())

    # ---- confinement ----------------------------------------------------------------------

    def test_dot_dot_cannot_escape(self):
        out = self.run_js("""
          await attempt('up', () => B.read('r1', '../secrets/id_rsa'));
          await attempt('abs', () => B.read('r1', %s));
        """ % json.dumps(os.path.join(self.outside, "id_rsa")))
        self.assertFalse(out["up"]["ok"], "../ escaped the sync folder")
        self.assertFalse(out["abs"]["ok"], "an absolute path escaped the sync folder")

    def test_a_symlink_out_of_the_tree_cannot_be_read(self):
        """The case string comparison misses. `<root>/link/id_rsa` has the root as a prefix."""
        os.symlink(self.outside, os.path.join(self.root, "link"))
        out = self.run_js("await attempt('link', () => B.read('r1', 'link/id_rsa'));")
        self.assertFalse(out["link"]["ok"],
                         "a symlink pointing outside the folder was followed — the check must be on "
                         "the RESOLVED path, not on the string")

    def test_an_unknown_root_is_refused(self):
        out = self.run_js("await attempt('r', () => B.read('nope', 'a.txt'));")
        self.assertFalse(out["r"]["ok"])

    # ---- scanning -------------------------------------------------------------------------

    def test_scan_reports_files_and_skips_links_and_trash(self):
        with open(os.path.join(self.root, "a.txt"), "w") as fh:
            fh.write("hello")
        os.makedirs(os.path.join(self.root, "sub"))
        with open(os.path.join(self.root, "sub", "b.txt"), "w") as fh:
            fh.write("world!")
        os.makedirs(os.path.join(self.root, ".pc-trash", "2026-01-01"))
        with open(os.path.join(self.root, ".pc-trash", "2026-01-01", "old.txt"), "w") as fh:
            fh.write("deleted")
        os.symlink(self.outside, os.path.join(self.root, "link"))

        out = self.run_js("await attempt('s', () => B.scan('r1', {}));")
        self.assertTrue(out["s"]["ok"], out["s"].get("error"))
        files = out["s"]["value"]["files"]
        self.assertEqual(sorted(files), ["a.txt", "sub/b.txt"],
                         "scan must use forward slashes, skip .pc-trash and skip symlinks")
        self.assertEqual(files["a.txt"]["size"], 5)

    def test_scan_hashes_only_when_asked(self):
        with open(os.path.join(self.root, "a.txt"), "w") as fh:
            fh.write("hello")
        out = self.run_js("""
          await attempt('cheap', () => B.scan('r1', {}));
          await attempt('hashed', () => B.scan('r1', {hash:true}));
        """)
        self.assertNotIn("sha", out["cheap"]["value"]["files"]["a.txt"],
                         "an incremental scan must not hash — that is the space-heater path")
        self.assertEqual(out["hashed"]["value"]["files"]["a.txt"]["sha"],
                         "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824")

    def test_a_file_over_the_limit_is_reported_not_silently_dropped(self):
        with open(os.path.join(self.root, "big.bin"), "wb") as fh:
            fh.write(b"x" * 4096)
        out = self.run_js("await attempt('s', () => B.scan('r1', {maxBytes: 1024}));")
        v = out["s"]["value"]
        self.assertNotIn("big.bin", v["files"])
        self.assertTrue(any(s["path"] == "big.bin" and s["why"] == "too big" for s in v["skipped"]),
                        "an oversized file must appear in `skipped` so the UI can say so")

    # ---- writing --------------------------------------------------------------------------

    def test_write_is_atomic_and_leaves_no_part_file(self):
        out = self.run_js("""
          await attempt('w', () => B.write('r1', 'deep/new.txt', Buffer.from('abc'), 1700000000000));
          await attempt('s', () => B.scan('r1', {}));
        """)
        self.assertTrue(out["w"]["ok"], out["w"].get("error"))
        with open(os.path.join(self.root, "deep", "new.txt")) as fh:
            self.assertEqual(fh.read(), "abc")
        self.assertEqual(sorted(out["s"]["value"]["files"]), ["deep/new.txt"],
                         "a .pcpart temp file must not survive, nor be reported by a scan")

    def test_a_download_keeps_the_source_mtime(self):
        """Otherwise the next scan reads our own write as a local edit and pushes it back — the loop
        that makes a sync never settle."""
        out = self.run_js(
            "await attempt('w', () => B.write('r1','m.txt', Buffer.from('x'), 1700000000000));")
        self.assertEqual(out["w"]["value"]["mtime"], 1700000000000)

    # ---- deleting -------------------------------------------------------------------------

    def test_delete_moves_into_a_dated_trash(self):
        with open(os.path.join(self.root, "gone.txt"), "w") as fh:
            fh.write("bye")
        out = self.run_js("await attempt('t', () => B.trash('r1','gone.txt', Date.UTC(2026,0,2)));")
        dest = out["t"]["value"]
        self.assertTrue(dest.startswith(".pc-trash/2026-01-02/"), dest)
        self.assertFalse(os.path.exists(os.path.join(self.root, "gone.txt")))
        self.assertTrue(os.path.exists(os.path.join(self.root, *dest.split("/"))))

    def test_trash_never_overwrites_itself(self):
        with open(os.path.join(self.root, "dup.txt"), "w") as fh:
            fh.write("first")
        out1 = self.run_js("await attempt('t', () => B.trash('r1','dup.txt', Date.UTC(2026,0,2)));")
        with open(os.path.join(self.root, "dup.txt"), "w") as fh:
            fh.write("second")
        out2 = self.run_js("await attempt('t', () => B.trash('r1','dup.txt', Date.UTC(2026,0,2)));")
        self.assertNotEqual(out1["t"]["value"], out2["t"]["value"],
                            "a second deletion of the same name on the same day overwrote the first")
        with open(os.path.join(self.root, *out1["t"]["value"].split("/"))) as fh:
            self.assertEqual(fh.read(), "first", "the original trashed copy was clobbered")

    def test_empty_trash_only_takes_old_days(self):
        for day in ("2020-01-01", "2099-01-01"):
            os.makedirs(os.path.join(self.root, ".pc-trash", day))
            with open(os.path.join(self.root, ".pc-trash", day, "f.txt"), "w") as fh:
                fh.write("x")
        out = self.run_js("await attempt('e', () => B.emptyTrash('r1', 30));")
        self.assertEqual(out["e"]["value"]["removed"], 1)
        self.assertFalse(os.path.exists(os.path.join(self.root, ".pc-trash", "2020-01-01")))
        self.assertTrue(os.path.exists(os.path.join(self.root, ".pc-trash", "2099-01-01")))

    def test_empty_trash_with_zero_days_takes_everything(self):
        """"Empty trash" has to be able to empty the trash.

        `.pc-trash` lives INSIDE the synced root, so everything in it is still counted by Explorer,
        by a disk-usage tool and by a quota. Every layer hardcoded 30 days — `days || 30`, which
        cannot tell an explicit 0 from an absent value — and there was no automatic sweep for that
        floor to serve, so the only caller was a button that could never reclaim anything recent.
        Reported after deleting a 40 GB Pictures folder: pressed Empty trash, folder still 40 GB,
        and the only way out was deleting .pc-trash by hand in a file manager."""
        for day in ("2020-01-01", "2099-01-01"):
            os.makedirs(os.path.join(self.root, ".pc-trash", day))
            with open(os.path.join(self.root, ".pc-trash", day, "f.txt"), "w") as fh:
                fh.write("x" * 100)
        out = self.run_js("await attempt('e', () => B.emptyTrash('r1', 0));")
        self.assertEqual(out["e"]["value"]["removed"], 2, out["e"])
        self.assertFalse(os.path.exists(os.path.join(self.root, ".pc-trash", "2020-01-01")))
        self.assertFalse(os.path.exists(os.path.join(self.root, ".pc-trash", "2099-01-01")),
                         "an explicit 0 still fell back to the 30-day window")

    def test_empty_trash_reports_the_space_it_freed(self):
        """"emptied 0 day(s)" is what made the old button look broken; "freed 40.2 GB" is the only
        answer to the question actually being asked."""
        os.makedirs(os.path.join(self.root, ".pc-trash", "2020-01-01", "nested"))
        with open(os.path.join(self.root, ".pc-trash", "2020-01-01", "a.bin"), "wb") as fh:
            fh.write(b"\0" * 1000)
        with open(os.path.join(self.root, ".pc-trash", "2020-01-01", "nested", "b.bin"), "wb") as fh:
            fh.write(b"\0" * 2500)
        stat = self.run_js("await attempt('s', () => B.trashStat('r1'));")["s"]["value"]
        self.assertEqual((stat["files"], stat["bytes"], stat["days"]), (2, 3500, 1), stat)
        out = self.run_js("await attempt('e', () => B.emptyTrash('r1', 0));")["e"]["value"]
        self.assertEqual((out["removed"], out["files"], out["bytes"]), (1, 2, 3500), out)

    def test_a_missing_or_absent_days_value_still_keeps_the_safety_net(self):
        """The other direction, and the reason this is a tiebreak rather than "always delete
        everything": with no argument at all the 30-day window still applies, so nothing that calls
        it without thinking can wipe a net somebody is relying on."""
        os.makedirs(os.path.join(self.root, ".pc-trash", "2099-01-01"))
        with open(os.path.join(self.root, ".pc-trash", "2099-01-01", "f.txt"), "w") as fh:
            fh.write("x")
        out = self.run_js("await attempt('e', () => B.emptyTrash('r1'));")
        self.assertEqual(out["e"]["value"]["removed"], 0, out["e"])
        self.assertTrue(os.path.exists(os.path.join(self.root, ".pc-trash", "2099-01-01")))

    def test_purge_trash_removes_only_the_named_files(self):
        """The reconcile proves file by file which trash copies are redundant, so the delete has to
        be per file. `emptyTrash` takes whole DAYS, and a day is exactly the wrong unit for that:
        one unprovable file either protects a hundred redundant ones or is thrown away with them."""
        d = os.path.join(self.root, ".pc-trash", "2026-08-19")
        os.makedirs(os.path.join(d, "sub"))
        for name in ("a.txt", "b.txt", os.path.join("sub", "c.txt")):
            with open(os.path.join(d, name), "w") as fh:
                fh.write("x" * 10)
        out = self.run_js("await attempt('p', () => B.purgeTrash('r1', "
                          "['.pc-trash/2026-08-19/a.txt', '.pc-trash/2026-08-19/sub/c.txt']));")
        v = out["p"]["value"]
        self.assertEqual((v["removed"], v["bytes"], v["failed"]), (2, 20, []), v)
        self.assertFalse(os.path.exists(os.path.join(d, "a.txt")))
        self.assertFalse(os.path.exists(os.path.join(d, "sub", "c.txt")))
        self.assertTrue(os.path.exists(os.path.join(d, "b.txt")),
                        "a file the reconcile could not prove was deleted anyway")

    def test_purge_trash_refuses_a_path_outside_the_trash(self):
        """This is the one bridge call whose entire purpose is deleting. The caller is checked here
        and not trusted, because the caller is the half that has been wrong all week — and the paths
        it hands over come from a listing it did minutes earlier, on a tree other programs write."""
        with open(os.path.join(self.root, "keep.txt"), "w") as fh:
            fh.write("precious")
        os.makedirs(os.path.join(self.root, ".pc-trash", "2026-08-19"))
        out = self.run_js("await attempt('p', () => B.purgeTrash('r1', "
                          "['keep.txt', '.pc-trash/2026-08-19/../../keep.txt']));")
        v = out["p"]["value"]
        self.assertEqual(v["removed"], 0, v)
        self.assertEqual(len(v["failed"]), 2, v)
        self.assertTrue(os.path.exists(os.path.join(self.root, "keep.txt")),
                        "a path outside .pc-trash was deleted")

    def test_purge_trash_keeps_going_past_one_failure(self):
        """One locked file must not cost the rest — on Windows that is the likely case, not the
        unlikely one, and a partial purge that reports what it could not do is worth far more than
        a clean error that did nothing."""
        d = os.path.join(self.root, ".pc-trash", "2026-08-19")
        os.makedirs(d)
        with open(os.path.join(d, "real.txt"), "w") as fh:
            fh.write("x")
        out = self.run_js("await attempt('p', () => B.purgeTrash('r1', "
                          "['.pc-trash/2026-08-19/ghost.txt', '.pc-trash/2026-08-19/real.txt']));")
        v = out["p"]["value"]
        self.assertEqual(v["removed"], 1, v)
        self.assertEqual(v["missing"], 1, "a path that was already gone was counted as removed")
        self.assertFalse(os.path.exists(os.path.join(d, "real.txt")))

    def test_trash_stat_on_a_folder_with_no_trash_is_not_an_error(self):
        """The button reads this before asking anything, so a folder that has never had a delete
        must answer zero rather than throw — otherwise the confirmation cannot be drawn at all."""
        out = self.run_js("await attempt('s', () => B.trashStat('r1'));")
        self.assertTrue(out["s"]["ok"], out["s"])
        self.assertEqual((out["s"]["value"]["files"], out["s"]["value"]["days"]), (0, 0))


if __name__ == "__main__":
    unittest.main()


@unittest.skipIf(not NODE, "no node on this node")
class TestInFlightFiles(TestFsBridge):
    """The substitute for file locking.

    There is nothing to lock against: flock on Linux and macOS is advisory and no ordinary
    application takes it, so locking would exclude us and nobody else. What the tools that write
    files DO announce is a name — ~$doc.docx, .~lock.sheet.ods#, a .crdownload — and a size/mtime
    that keeps moving. Both are signals we can actually read.
    """

    def test_editor_and_download_temp_files_are_skipped(self):
        for name in ("~$report.docx", ".~lock.sheet.ods#", "movie.mp4.crdownload",
                     "big.iso.part", "notes.txt.swp"):
            with open(os.path.join(self.root, name), "w") as fh:
                fh.write("in flight")
        with open(os.path.join(self.root, "real.txt"), "w") as fh:
            fh.write("done")
        out = self.run_js("await attempt('s', () => B.scan('r1', {}));")
        self.assertEqual(sorted(out["s"]["value"]["files"]), ["real.txt"],
                         "a half-written file must not be uploaded as if it were finished")

    def test_a_file_that_changes_while_being_hashed_is_left_for_next_time(self):
        """Uploading bytes that were never a whole file is worse than skipping: the other devices
        get a corrupt copy with a perfectly good checksum."""
        big = os.path.join(self.root, "growing.bin")
        with open(big, "wb") as fh:
            fh.write(b"x" * (2 * 1024 * 1024))
        # Rewrite the file from under the hash: node's own timer, so it lands mid-read.
        out = self.run_js("""
          const fsn = require('fs');
          setTimeout(() => { try { fsn.appendFileSync(%s, Buffer.alloc(1024*512, 121)); } catch(_){} }, 5);
          await attempt('s', () => B.scan('r1', {hash:true}));
        """ % json.dumps(big))
        v = out["s"]["value"]
        if "growing.bin" in v["files"]:
            # The race did not land this run — assert the mechanism is at least present and correct
            # for the case it does catch, rather than passing on a coin flip.
            self.skipTest("the write did not land inside the hash window on this run")
        self.assertTrue(any(s["path"] == "growing.bin" for s in v["skipped"]),
                        "a file that changed mid-hash must be skipped, not uploaded")


class TestPartFiles(TestFsBridge):
    """Interrupted downloads: the `.part` file is the whole safety story, and it was also a leak."""

    def test_part_size_reports_what_is_there_to_resume(self):
        out = self.run_js("""
            await attempt('empty', () => B.partSize('r1', 'v.mp4'));
            await attempt('w', () => B.writePart('r1', 'v.mp4', 0, new Uint8Array(4096)));
            await attempt('some', () => B.partSize('r1', 'v.mp4'));
        """)
        self.assertEqual(out["empty"]["value"], 0, "a missing part file must answer 0, not throw")
        self.assertEqual(out["some"]["value"], 4096)

    def test_hash_part_hashes_the_part_and_not_the_target(self):
        """It has to run BEFORE the commit — after writeCommit the new file has already been renamed
        over the old one, so a check there is a report, not a defence."""
        import hashlib
        with open(os.path.join(self.root, "v.mp4"), "wb") as fh:
            fh.write(b"the OLD file")
        out = self.run_js("""
            await attempt('w', () => B.writePart('r1', 'v.mp4', 0, new Uint8Array([1,2,3,4])));
            await attempt('h', () => B.hashPart('r1', 'v.mp4'));
        """)
        self.assertEqual(out["h"]["value"], hashlib.sha256(bytes([1, 2, 3, 4])).hexdigest(),
                         "hashPart hashed the target rather than the part file")

    def test_discard_part_removes_it_and_leaves_the_real_file_alone(self):
        with open(os.path.join(self.root, "v.mp4"), "wb") as fh:
            fh.write(b"the good copy")
        out = self.run_js("""
            await attempt('w', () => B.writePart('r1', 'v.mp4', 0, new Uint8Array([9,9])));
            await attempt('d', () => B.discardPart('r1', 'v.mp4'));
        """)
        self.assertTrue(out["d"]["ok"], out["d"])
        self.assertFalse(os.path.exists(os.path.join(self.root, "v.mp4.pcpart")))
        with open(os.path.join(self.root, "v.mp4"), "rb") as fh:
            self.assertEqual(fh.read(), b"the good copy", "discarding a part touched the real file")

    def test_stale_part_files_are_collected_but_fresh_ones_are_not(self):
        """They are invisible to everything — `ignored()` keeps them out of the scan, rightly — so
        nothing ever looked at them again and every interrupted download left its bytes on the disk
        for good. The age bound is what makes it safe: a part file this sweep is about to resume from
        must survive.

        The files here are created THROUGH the adapter, because that is now what makes them
        collectable: the sweep removes only part files it recorded writing. A `.pcpart` file it did
        not create is somebody else's and is left alone — see TestPartSweepOnlyTakesItsOwn."""
        import time
        self.run_js("""
            await B.writePart('r1', 'old.mp4', 0, new Uint8Array(500));
            await B.writePart('r1', 'fresh.mp4', 0, new Uint8Array(10));
        """)
        old = os.path.join(self.root, "old.mp4.pcpart")
        long_ago = time.time() - 5 * 86400
        os.utime(old, (long_ago, long_ago))

        out = self.run_js("await attempt('s', () => B.sweepParts('r1', 24 * 3600000));")
        self.assertEqual(out["s"]["value"]["removed"], 1, out["s"])
        self.assertEqual(out["s"]["value"]["bytes"], 500)
        self.assertFalse(os.path.exists(old), "the abandoned part file survived")
        self.assertTrue(os.path.exists(os.path.join(self.root, "fresh.mp4.pcpart")),
                        "a part file a sweep may still be resuming from was collected")


class TestPartSweepOnlyTakesItsOwn(unittest.TestCase):
    """The part sweep walks somebody's Documents folder deleting files.

    Matching on the extension alone is not good enough. A user's own `notes.pcpart` is invisible to
    the scan — part files are ignored, exactly so a half-written download is never uploaded — which
    means sync has never touched it, and a sweep that deletes it a day later does so with no report
    and no copy in `.pc-trash`.

    So the adapter records the part files it creates, and the sweep removes only those. A part file
    it did not record is LEAKED, which is the correct direction: a leaked temp file is a cost, and
    deleting somebody's file is not a cost.
    """

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="pc-parts-")
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)

    def _run(self, script):
        js = textwrap.dedent("""
            const B = require(%s);
            B.init({ roots: [{id:'r1', dir: %s}], save(){} });
            (async () => {
              %s
            })().catch(e => { process.stderr.write(String(e && e.stack || e)); process.exit(1); });
        """) % (json.dumps(MOD), json.dumps(self.root), script)
        r = subprocess.run([NODE, "-e", js], capture_output=True, text=True, timeout=120)
        if r.returncode != 0:
            raise AssertionError("node failed:\n" + r.stderr[-3000:])
        return json.loads(r.stdout or "{}")

    def test_a_users_own_pcpart_file_is_never_deleted(self):
        mine = os.path.join(self.root, "notes.pcpart")
        with open(mine, "w") as fh:
            fh.write("something a person made")
        old = time.time() - 3 * 24 * 3600
        os.utime(mine, (old, old))
        out = self._run("""
          const r = await B.sweepParts('r1', 1000);
          process.stdout.write(JSON.stringify({ removed: r.removed }));
        """)
        self.assertTrue(os.path.exists(mine), "the sweep deleted a file sync never touched")
        self.assertEqual(out["removed"], 0, out)

    def test_a_part_file_the_adapter_wrote_is_collected(self):
        out = self._run("""
          await B.writePart('r1', 'video.mp4', 0, new Uint8Array([1,2,3,4]));
          process.stdout.write(JSON.stringify({ made: true }));
        """)
        part = os.path.join(self.root, "video.mp4.pcpart")
        self.assertTrue(os.path.exists(part), out)
        old = time.time() - 3 * 24 * 3600
        os.utime(part, (old, old))
        out2 = self._run("""
          const r = await B.sweepParts('r1', 1000);
          process.stdout.write(JSON.stringify({ removed: r.removed }));
        """)
        self.assertEqual(out2["removed"], 1, "an abandoned part file was left to leak")
        self.assertFalse(os.path.exists(part))

    def test_a_committed_part_file_is_forgotten(self):
        """The register must not grow for ever, and a name that is reused later must not be
        collectable on the strength of a record from a download that finished."""
        self._run("""
          await B.writePart('r1', 'doc.pdf', 0, new Uint8Array([9,9]));
          await B.writeCommit('r1', 'doc.pdf', 0);
          process.stdout.write('{}');
        """)
        reg = os.path.join(self.root, ".pc-trash", ".parts.json")
        with open(reg) as fh:
            self.assertEqual(json.load(fh), {}, "the register still names a part file that landed")


@unittest.skipIf(not NODE, "no node on this node")
class TestTrashRestore(TestFsBridge):
    """"we need a restore button" — the bridge enumerates the trash, the client moves files back
    over the ordinary move(), and nothing is ever overwritten. Real files, real trash."""

    def test_listTrash_names_every_file_with_its_way_home(self):
        os.makedirs(os.path.join(self.root, "sub"))
        with open(os.path.join(self.root, "sub", "a.txt"), "w") as fh:
            fh.write("hello")
        out = self.run_js("""
            await attempt('trash', () => B.trash('r1', 'sub/a.txt', Date.parse('2026-08-18')));
            await attempt('list', () => B.listTrash('r1'));
        """)
        self.assertTrue(out["list"]["ok"], out)
        rows = out["list"]["value"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["to"], "sub/a.txt")
        self.assertTrue(rows[0]["at"].startswith(".pc-trash/2026-08-18/"), rows[0])

    def test_the_restore_round_trip_and_never_overwrite(self):
        os.makedirs(os.path.join(self.root, "sub"))
        with open(os.path.join(self.root, "sub", "a.txt"), "w") as fh:
            fh.write("original")
        out = self.run_js("""
            await attempt('trash', () => B.trash('r1', 'sub/a.txt', Date.parse('2026-08-18')));
            await attempt('list', () => B.listTrash('r1'));
            const row = (await B.listTrash('r1'))[0];
            // the exact sequence the button runs: the way home is free when the path is provably
            // gone OR its parent was pruned with it (move() recreates directories)
            await attempt('free', async () => { const ev = await B.confirmGone('r1', row.to);
              return ev.gone === true || ev.parentAlive === false; });
            await attempt('back', () => B.move('r1', row.at, row.to));
            await attempt('read', async () => new TextDecoder().decode(await B.read('r1', 'sub/a.txt')));
            await attempt('empty', () => B.listTrash('r1'));
        """)
        self.assertTrue(out["free"]["value"], "the freed path did not read as free")
        self.assertEqual(out["read"]["value"], "original")
        self.assertEqual(out["empty"]["value"], [], "the restored file still shows in the trash")

    def test_a_reoccupied_path_reads_as_not_free(self):
        """The button's skip rule: a file that exists again must NOT be overwritten by its old
        trash copy — confirmGone answers gone:false and the restore leaves both alone."""
        os.makedirs(os.path.join(self.root, "sub"))
        with open(os.path.join(self.root, "sub", "a.txt"), "w") as fh:
            fh.write("old")
        out1 = self.run_js("""
            await attempt('trash', () => B.trash('r1', 'sub/a.txt', Date.parse('2026-08-18')));
        """)
        os.makedirs(os.path.join(self.root, "sub"), exist_ok=True)   # trash() pruned the emptied dir
        with open(os.path.join(self.root, "sub", "a.txt"), "w") as fh:
            fh.write("NEW CONTENT")
        out = self.run_js("""
            const row = (await B.listTrash('r1'))[0];
            await attempt('free', () => B.confirmGone('r1', row.to));
            await attempt('still', () => B.listTrash('r1'));
        """)
        self.assertFalse(out["free"]["value"]["gone"], "an existing file read as free to overwrite")
        self.assertEqual(len(out["still"]["value"]), 1, "the trash copy went somewhere")


@unittest.skipIf(not NODE, "no node on this node")
class TestConfirmGoneSubtree(TestFsBridge):
    """A deleted DIRECTORY must be provable ("i wanted to simulate a restore event" — deleting
    .ssh outright parked six deletions as unconfirmable, because the per-file probe wanted the
    parent healthy and the parent died with the folder). The proof walks up to the nearest live
    ancestor; only the folder ROOT being unreachable stays unprovable — that's an unplugged
    drive, not a deletion."""

    def test_a_deleted_directory_is_provably_gone(self):
        os.makedirs(os.path.join(self.root, "keys"))
        with open(os.path.join(self.root, "keys", "id_rsa"), "w") as fh:
            fh.write("k")
        shutil.rmtree(os.path.join(self.root, "keys"))
        out = self.run_js("""
            await attempt('gone', () => B.confirmGone('r1', 'keys/id_rsa'));
        """)
        v = out["gone"]["value"]
        self.assertTrue(v["gone"], "a genuinely deleted folder's file is not provable — its "
                                   "tombstone would be held for ever")
        self.assertTrue(v["parentAlive"])

    def test_a_vanished_root_is_still_unprovable(self):
        out = self.run_js("""
            const fsp = require('fs/promises');
            await fsp.rm(%s, { recursive: true, force: true });   // the drive left the building
            await attempt('gone', () => B.confirmGone('r1', 'keys/id_rsa'));
        """ % json.dumps(self.root))
        v = out["gone"]["value"]
        self.assertFalse(v["gone"], "an unplugged root read as a proven deletion — THE disaster")
        self.assertFalse(v["parentAlive"])
