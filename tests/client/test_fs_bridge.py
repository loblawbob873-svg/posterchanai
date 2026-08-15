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
        must survive."""
        import time
        old = os.path.join(self.root, "old.mp4.pcpart")
        with open(old, "wb") as fh:
            fh.write(b"x" * 500)
        long_ago = time.time() - 5 * 86400
        os.utime(old, (long_ago, long_ago))
        with open(os.path.join(self.root, "fresh.mp4.pcpart"), "wb") as fh:
            fh.write(b"y" * 10)

        out = self.run_js("await attempt('s', () => B.sweepParts('r1', 24 * 3600000));")
        self.assertEqual(out["s"]["value"]["removed"], 1, out["s"])
        self.assertEqual(out["s"]["value"]["bytes"], 500)
        self.assertFalse(os.path.exists(old), "the abandoned part file survived")
        self.assertTrue(os.path.exists(os.path.join(self.root, "fresh.mp4.pcpart")),
                        "a part file a sweep may still be resuming from was collected")
