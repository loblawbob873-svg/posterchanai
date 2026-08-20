"""The computer's own files, in the Files screen — the bridge, run against a real directory.

Files browses the encrypted drive and a synced folder's manifest. On PosterChanOS there is an
obvious third source: the disk you are sitting in front of.

WHAT LIMITS THIS IS THE UNIX ACCOUNT, and there is deliberately no path allowlist — the Terminal on
the same desktop is a real PTY running as the session user, so anything this bridge reaches is
already reachable by typing `ls`. A file manager is strictly less capability than a shell. What the
tests below cover is the other kind of danger: the operations that LOSE somebody's files.
"""
import json
import os
import shutil
import subprocess
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MOD = os.path.join(ROOT, "desktop", "hostfs.js")
NODE = shutil.which("node") or shutil.which("nodejs")


@unittest.skipIf(not NODE, "needs node")
class HostFs(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="hostfs-")

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def js(self, body, env=None):
        src = ("const H = require(%s);\nconst D = %s;\nconst out = {};\n%s\n"
               "process.stdout.write(JSON.stringify(out));" % (json.dumps(MOD), json.dumps(self.d), body))
        e = dict(os.environ)
        e.update(env or {})
        r = subprocess.run([NODE, "-e", src], capture_output=True, text=True, timeout=60, env=e)
        self.assertEqual(r.returncode, 0, r.stderr[-1200:])
        return json.loads(r.stdout)

    # ---- paths ---------------------------------------------------------------------------------
    def test_a_relative_path_never_becomes_one_relative_to_wherever_we_started(self):
        """A file manager resolves `..` constantly, so refusing the segment would break the parent
        button. What must not happen is a RELATIVE path quietly resolving against the Electron
        process's working directory, which is somewhere nobody has ever looked."""
        out = self.js("out.a = H.clean('/tmp/../etc/./passwd');"
                      "out.b = H.clean('not/absolute');"
                      "out.c = H.clean('');")
        self.assertEqual(out["a"], "/etc/passwd")
        self.assertTrue(out["b"].startswith("/"), "a relative path was accepted as-is")
        self.assertEqual(out["c"], "")

    def test_the_up_button_stops_at_the_root(self):
        out = self.js("out.a = H.parentOf('/'); out.b = H.parentOf('/home/x');")
        self.assertIsNone(out["a"], "the parent of / is not null, so 'up' has nowhere to stop")
        self.assertEqual(out["b"], "/home")

    # ---- listing -------------------------------------------------------------------------------
    def test_a_directorys_size_is_not_reported_as_4096(self):
        """A directory's `size` is the size of its record — 4096 on most filesystems — and means
        nothing to anybody. Reported as such, a sort by size interleaves every folder in the middle
        of the files."""
        os.mkdir(os.path.join(self.d, "sub"))
        open(os.path.join(self.d, "f.txt"), "w").write("hello")
        out = self.js("out.e = H.list(D).entries.map(x => [x.name, x.dir, x.size]).sort();")
        self.assertIn(["f.txt", False, 5], out["e"])
        self.assertIn(["sub", True, 0], out["e"])

    def test_a_dangling_symlink_is_shown_and_marked(self):
        """Deleting it is usually why somebody is looking at it. Dropped from the listing, it is a
        file they can see in a terminal and not in the file manager."""
        os.symlink(os.path.join(self.d, "nope"), os.path.join(self.d, "dead"))
        out = self.js("out.e = H.list(D).entries.find(x => x.name === 'dead');")
        self.assertIsNotNone(out["e"], "a broken symlink vanished from the listing")
        self.assertTrue(out["e"]["link"])
        self.assertTrue(out["e"]["broken"])

    def test_dotfiles_are_marked_not_dropped(self):
        """The explorer decides whether to show them — every file manager has that switch — and a
        bridge that drops them makes it impossible to offer."""
        open(os.path.join(self.d, ".hidden"), "w").write("x")
        out = self.js("out.e = H.list(D).entries.find(x => x.name === '.hidden');")
        self.assertTrue(out["e"]["hidden"])

    def test_an_unreadable_directory_THROWS_rather_than_looking_empty(self):
        """"I could not read that" and "there is nothing in it" are different facts, and a file
        manager that confuses them shows somebody an empty folder full of their files."""
        out = self.js("try { H.list(D + '/nope'); out.threw = false; }"
                      "catch (e) { out.threw = true; }")
        self.assertTrue(out["threw"])

    # ---- deletion, which is the dangerous one --------------------------------------------------
    def test_delete_goes_to_the_desktop_trash_with_a_record(self):
        """A file in the bin without a .trashinfo is one every other trash tool on the machine
        refuses to restore — it no longer knows where it came from. The record is written FIRST, so
        an interruption cannot leave exactly that."""
        home = os.path.join(self.d, "home")
        os.makedirs(home)
        target = os.path.join(self.d, "bye.txt")
        open(target, "w").write("data")
        out = self.js("out.r = H.trash(D + '/bye.txt', { HOME: D + '/home' });"
                      "out.gone = !require('fs').existsSync(D + '/bye.txt');")
        self.assertTrue(out["gone"], "the file is still where it was")
        trash = os.path.join(home, ".local", "share", "Trash")
        self.assertTrue(os.path.exists(os.path.join(trash, "files", "bye.txt")),
                        "the file is not in the machine's trash")
        info = open(os.path.join(trash, "info", "bye.txt.trashinfo")).read()
        self.assertIn("[Trash Info]", info)
        self.assertIn("Path=" + target, info)
        self.assertIn("DeletionDate=", info)
        self.assertNotIn("Z", info.split("DeletionDate=")[1],
                         "the date is UTC with a Z — the spec asks for local time with no zone, "
                         "which is not what toISOString() produces")

    def test_two_files_of_the_same_name_do_not_overwrite_each_other_in_the_bin(self):
        """Delete `notes.txt` from two folders and the second must not silently replace the first —
        that is somebody's file gone from the one place they would look for it."""
        os.makedirs(os.path.join(self.d, "home"))
        for sub in ("a", "b"):
            os.makedirs(os.path.join(self.d, sub))
            open(os.path.join(self.d, sub, "notes.txt"), "w").write(sub)
        out = self.js("out.a = H.trash(D + '/a/notes.txt', { HOME: D + '/home' });"
                      "out.b = H.trash(D + '/b/notes.txt', { HOME: D + '/home' });")
        files = sorted(os.listdir(os.path.join(self.d, "home", ".local", "share", "Trash", "files")))
        self.assertEqual(files, ["notes.2.txt", "notes.txt"], repr(files))
        self.assertNotEqual(out["a"]["trashed"], out["b"]["trashed"])

    def test_the_root_of_the_filesystem_cannot_be_deleted(self):
        out = self.js("try { H.trash('/', { HOME: D }); out.ok = true; } catch (e) { out.why = e.message; }")
        self.assertNotIn("ok", out)
        self.assertIn("root", out["why"])

    # ---- rename and mkdir ----------------------------------------------------------------------
    def test_rename_refuses_to_overwrite_somebody_elses_file(self):
        """POSIX rename overwrites silently, and in a file manager that is another file gone with no
        dialog and no undo."""
        open(os.path.join(self.d, "a.txt"), "w").write("a")
        open(os.path.join(self.d, "b.txt"), "w").write("b")
        out = self.js("try { H.rename(D + '/a.txt', 'b.txt'); out.ok = true; }"
                      "catch (e) { out.why = e.message; }")
        self.assertNotIn("ok", out, "rename silently destroyed the other file")
        self.assertEqual(open(os.path.join(self.d, "b.txt")).read(), "b")

    def test_a_new_folder_takes_a_NAME_not_a_PATH(self):
        """`../../etc` typed into a "new folder" box is a directory created somewhere nobody asked
        for. The separator is refused rather than resolved."""
        out = self.js("try { H.mkdir(D, '../escaped'); out.ok = true; } catch (e) { out.why = e.message; }"
                      "try { H.rename(D, '../x'); out.ok2 = true; } catch (e) { out.why2 = e.message; }")
        self.assertNotIn("ok", out)
        self.assertNotIn("ok2", out)
        self.assertFalse(os.path.exists(os.path.join(os.path.dirname(self.d), "escaped")))

    # ---- the places to start from --------------------------------------------------------------
    def test_home_is_offered_first_and_the_root_last(self):
        """Home is where somebody's own files are; `/` is offered because this is the machine's own
        file manager and hiding its root would be pretending."""
        out = self.js("out.r = H.roots({ HOME: D, USER: 'someone' });")
        self.assertEqual(out["r"][0]["kind"], "home")
        self.assertEqual(out["r"][0]["path"], self.d)
        self.assertEqual(out["r"][-1]["path"], "/")


if __name__ == "__main__":
    unittest.main()
