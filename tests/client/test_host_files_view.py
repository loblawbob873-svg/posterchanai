"""The Files screen's third source — the machine's own disk — as the client shapes it.

`tests/test_host_fs.py` covers the bridge (the operations that lose files). This covers the half
that decides what a folder LOOKS like, which fails differently: nothing throws, and a directory of a
thousand items is simply unusable.
"""
import json
import os
import shutil
import subprocess
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
MOD = os.path.join(ROOT, "static", "js", "client", "hostfiles.js")
APP = os.path.join(ROOT, "static", "js", "client", "app.js")
NODE = shutil.which("node") or shutil.which("nodejs")


@unittest.skipIf(not NODE, "needs node")
class HostFilesView(unittest.TestCase):
    def js(self, body):
        src = ("const F = require(%s);\nconst out = {};\n%s\n"
               "process.stdout.write(JSON.stringify(out));" % (json.dumps(MOD), body))
        r = subprocess.run([NODE, "-e", src], capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr[-900:])
        return json.loads(r.stdout)

    def test_folders_come_first_whatever_the_sort_is(self):
        """Every file manager does this, and the reason is navigation: the folders are what you are
        moving THROUGH. Interleaved with files by date, a directory of a thousand items cannot be
        walked. It is applied on top of the shared comparator rather than inside it, because neither
        of the other two sources has folders in its list at all."""
        out = self.js("""
          const rows = [
            { name: 'zeta',  dir: true,  mtime: 1, size: 0 },
            { name: 'alpha', dir: false, mtime: 9, size: 10 },
            { name: 'beta',  dir: true,  mtime: 5, size: 0 },
            { name: 'gamma', dir: false, mtime: 2, size: 20 },
          ];
          const byName = (a, b) => String(a.name).localeCompare(String(b.name));
          const byNewest = (a, b) => b.mtime - a.mtime;
          out.name = F.order(rows, byName).map(r => r.name);
          out.newest = F.order(rows, byNewest).map(r => r.name);
        """)
        self.assertEqual(out["name"], ["beta", "zeta", "alpha", "gamma"])
        self.assertEqual(out["newest"], ["beta", "zeta", "alpha", "gamma"])

    def test_dotfiles_are_hidden_until_asked_for(self):
        out = self.js("""
          const rows = [{ name: 'a', hidden: false }, { name: '.b', hidden: true }];
          out.off = F.order(rows, null).map(r => r.name);
          out.on  = F.order(rows, null, { hidden: true }).map(r => r.name);
        """)
        self.assertEqual(out["off"], ["a"])
        self.assertEqual(sorted(out["on"]), [".b", "a"])

    def test_a_folder_never_sorts_by_its_size(self):
        """`keyOf` gives a directory -1 rather than its record size, so a sort by size cannot
        interleave folders through the files by accident."""
        out = self.js("out.d = F.keyOf({ dir: true, size: 4096 }, 'size');"
                      "out.f = F.keyOf({ dir: false, size: 4096 }, 'size');"
                      "out.t = F.keyOf({ dir: false, name: 'a.TXT' }, 'type');")
        self.assertEqual(out["d"], -1)
        self.assertEqual(out["f"], 4096)
        self.assertEqual(out["t"], "txt")

    def test_every_ancestor_in_the_path_is_clickable(self):
        """The way back up is the most used control in a file manager, and a text field is not it."""
        out = self.js("out.c = F.crumbs('/home/x/Documents');out.r = F.crumbs('/');")
        self.assertEqual([c["label"] for c in out["c"]], ["/", "home", "x", "Documents"])
        self.assertEqual([c["path"] for c in out["c"]],
                         ["/", "/home", "/home/x", "/home/x/Documents"])
        self.assertEqual(out["r"], [{"label": "/", "path": "/"}])

    def test_a_home_path_is_shortened_but_never_the_one_used(self):
        """`/home/npub1fdtthaq…/Documents` is unreadable and its leading two thirds never change."""
        out = self.js("out.a = F.pretty('/home/u/Documents', '/home/u');"
                      "out.b = F.pretty('/etc', '/home/u');"
                      "out.c = F.pretty('/home/uu/x', '/home/u');")
        self.assertEqual(out["a"], "~/Documents")
        self.assertEqual(out["b"], "/etc")
        self.assertEqual(out["c"], "/home/uu/x", "a different user's home was shortened as ours")

    def test_the_delete_prompt_says_it_is_reversible(self):
        """The most important part of that sentence, and the part a generic "Are you sure?" leaves
        out — this delete goes to the machine's own bin."""
        out = self.js("""
          out.one = F.deletePrompt([{ name: 'a.txt', dir: false }]);
          out.many = F.deletePrompt([{ name: 'a', dir: true }, { name: 'b', dir: false }]);
          out.none = F.deletePrompt([]);
        """)
        self.assertIn("a.txt", out["one"])
        self.assertIn("trash", out["one"].lower())
        self.assertIn("put", out["one"].lower())
        self.assertIn("2 items", out["many"])
        self.assertIn("1 folder", out["many"])
        self.assertEqual(out["none"], "")

    def test_it_is_absent_where_there_is_no_filesystem(self):
        """A browser tab and the APK have no pcHost at all, and the Files screen must simply not
        offer the chip rather than offer one that throws."""
        out = self.js("out.a = F.available();")
        self.assertFalse(out["a"])

    def test_the_files_screen_actually_reaches_it(self):
        """The module could be complete, loaded and precached and called by nothing — which is
        exactly what happened to termhist.js and is invisible from every angle but this one."""
        src = open(APP, encoding="utf-8").read()
        self.assertIn("PCHostFiles", src, "app.js never reaches for the host source")
        # The DEFINITION is not the wiring — a renderer nothing calls is the termhist.js shape
        # exactly. The branch is what makes the chip do anything.
        self.assertIn("if(_hostOn) return _renderHostRoot", src.replace("  ", ""),
                      "nothing routes the Files screen to the host source, so the chip is inert")
        self.assertIn("data-host", src, "there is no way to get to it from the Files screen")
        # …and choosing another source must leave it, or the chip is a one-way door.
        self.assertIn("_hostOn=false", src.replace(" ", ""),
                      "picking a drive folder or a synced folder does not leave this source")


if __name__ == "__main__":
    unittest.main()
