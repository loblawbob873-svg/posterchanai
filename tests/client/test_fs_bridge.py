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


if __name__ == "__main__":
    unittest.main()
