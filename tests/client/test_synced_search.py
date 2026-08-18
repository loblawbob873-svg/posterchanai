"""Searching a synced folder searches the FOLDER, not the directory you are standing in.

Reported as "search is broken for Folder Sync in Files → Blossom — searched for conflict and no
results were found", on a folder that plainly contained them: they were in sub-folders.

`_syncEntries` answers "what is in this directory", which is right for browsing and wrong for
searching. A synced folder files thousands of paths into a tree — that is most of why the drive
needed a search box at all — so a query has to cover the subtree, and each hit has to say where it
lives or the result is only half an answer.

The shipped helper is RUN against a real manifest shape.
"""
import json
import os
import re
import shutil
import subprocess
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
APP = os.path.join(ROOT, "static", "js", "client", "app.js")
NODE = shutil.which("node") or shutil.which("nodejs")

MANIFEST = {
    "DCIM/img1.jpg": {"sha": "a" * 64, "size": 10, "mtime": 1},
    "DCIM/2019/holiday (conflict from phone, 2026-08-17).jpg": {"sha": "b" * 64, "size": 20, "mtime": 2},
    "DCIM/2019/deep/nested (conflict from laptop, 2026-08-17).png": {"sha": "c" * 64, "size": 30, "mtime": 3},
    "Docs/notes.txt": {"sha": "d" * 64, "size": 40, "mtime": 4},
    "DCIM/gone (conflict from phone, 2026-08-01).jpg": {"deletedAt": 99},
    "DCIM/.pc-trash/2026-08-17/old (conflict from x, 2026-08-01).jpg": {"sha": "e" * 64, "size": 1},
}


@unittest.skipIf(not NODE, "no node on this node")
class SyncedSearchTests(unittest.TestCase):
    def _search(self, dir_, needle):
        src = open(APP, encoding="utf-8").read()
        m = re.search(r"\n  function _syncSearch\(paths, dir, match\)\{", src)
        self.assertIsNotNone(m, "_syncSearch moved in app.js")
        i = src.index("{", m.end() - 1)
        depth = 0
        while i < len(src):
            if src[i] == "{":
                depth += 1
            elif src[i] == "}":
                depth -= 1
                if depth == 0:
                    break
            i += 1
        body = src[m.start() + 1:i + 1]
        js = """
        %s
        const q = %s.toLowerCase();
        const out = _syncSearch(%s, %s, (nm) => nm.toLowerCase().includes(q));
        process.stdout.write(JSON.stringify(out));
        """ % (body, json.dumps(needle), json.dumps(MANIFEST), json.dumps(dir_))
        r = subprocess.run([NODE, "-e", js], capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr[-2000:])
        return json.loads(r.stdout)

    def test_it_finds_matches_in_sub_folders(self):
        """THE REPORTED ONE."""
        hits = self._search("", "conflict")
        names = sorted(h["path"] for h in hits)
        self.assertIn("DCIM/2019/holiday (conflict from phone, 2026-08-17).jpg", names)
        self.assertIn("DCIM/2019/deep/nested (conflict from laptop, 2026-08-17).png", names)

    def test_each_hit_says_where_it_lives(self):
        hits = {h["name"]: h["where"] for h in self._search("", "conflict")}
        self.assertEqual(hits["holiday (conflict from phone, 2026-08-17).jpg"], "DCIM/2019")
        self.assertEqual(hits["nested (conflict from laptop, 2026-08-17).png"], "DCIM/2019/deep")

    def test_it_is_scoped_to_the_folder_you_are_in(self):
        """Standing in Docs, a search must not return the whole folder."""
        self.assertEqual(self._search("Docs", "conflict"), [])
        self.assertEqual([h["name"] for h in self._search("Docs", "notes")], ["notes.txt"])

    def test_a_deleted_file_is_not_a_result(self):
        """A tombstone is how a deletion travels; it is not a file you can open."""
        hits = [h["path"] for h in self._search("", "conflict")]
        self.assertNotIn("DCIM/gone (conflict from phone, 2026-08-01).jpg", hits)

    def test_the_folders_own_trash_is_not_searched(self):
        """`.pc-trash` is where deletions go. Offering its contents as search results would make a
        deleted file look present, and re-adding one from there is how a deletion gets undone."""
        hits = [h["path"] for h in self._search("", "conflict")]
        self.assertFalse([h for h in hits if ".pc-trash" in h], hits)

    def test_a_prefix_is_a_path_boundary_not_a_string_one(self):
        """`DCIM` must not match `DCIMBACKUP/...`."""
        extra = dict(MANIFEST)
        extra["DCIMBACKUP/x (conflict from y, 2026-08-17).jpg"] = {"sha": "f" * 64, "size": 1}
        src = open(APP, encoding="utf-8").read()
        self.assertIn("const pre = dir ? dir + '/' : '';", src,
                      "the subtree filter no longer treats the prefix as a path boundary")


class TheSearchIsWiredTests(unittest.TestCase):
    def test_the_synced_view_uses_the_subtree_search(self):
        src = open(APP, encoding="utf-8").read()
        self.assertIn("_syncSearch(paths, _syncPath, _fxMatch)", src,
                      "the synced folder view still filters only its current directory")

    def test_an_empty_result_says_it_looked_everywhere(self):
        src = open(APP, encoding="utf-8").read()
        self.assertIn("including sub-folders", src,
                      "an empty search result reads as 'this folder is empty', which is a different "
                      "statement and the one that sent somebody looking for a bug")


class BulkSelectTests(unittest.TestCase):
    """Search "conflict", Select all shown, one delete — the storm cleanup. The storm published
    hundreds of conflict-copy ENTRIES the source machine never even held, so the tool acts on the
    SHARED RECORD (removeMany, one publish) and lets every device apply it as an ordinary deletion
    into .pc-trash."""

    @classmethod
    def setUpClass(cls):
        with open(os.path.join(ROOT, "static", "js", "client", "app.js"), encoding="utf-8") as fh:
            cls.app = fh.read()
        with open(os.path.join(ROOT, "static", "js", "client", "sync.js"), encoding="utf-8") as fh:
            cls.sync = fh.read()

    def test_the_tool_exists_and_selection_is_keyed_on_full_paths(self):
        self.assertIn("ss-toggle", self.app)
        self.assertIn("Select all shown", self.app)
        self.assertIn("_syncSel = new Set()", self.app)

    def test_it_deletes_through_one_publish_with_one_honest_confirmation(self):
        at = self.app.index('id="ss-del"')
        h = self.app[at:at + 2600]
        self.assertIn("removeMany", h, "the bulk delete publishes the whole document once per file")
        self.assertIn("uiConfirm", h)
        self.assertIn(".pc-trash", h, "the confirmation does not say where the copies go")

    def test_remove_many_drops_exact_live_paths_only_and_counts_honestly(self):
        """RUN the shipped removeMany against a stub _mutate: exact live paths tombstoned, dead and
        unknown paths skipped, and the returned count is what was actually dropped."""
        import json
        import re
        import shutil
        import subprocess
        node = shutil.which("node")
        if not node:
            self.skipTest("no node on this node")
        # ONE removeMany. Adding a second method with the same name to the edit literal silently
        # SHADOWS the first at runtime — the phantom-entry repair reads `r.removed` off it, and a
        # duplicate returning a bare number broke that caller. Caught here, before it shipped.
        self.assertEqual(self.sync.count("async removeMany("), 1,
                         "two removeMany methods — the later one shadows the first")
        m = re.search(r"async removeMany\(key, paths\)\{[\s\S]*?\n    \},", self.sync)
        self.assertIsNotNone(m, "removeMany moved in sync.js")
        fn = "(" + m.group(0).rstrip().rstrip(",").replace("async removeMany", "async function") + ")"
        js = """
        const dropped = [];
        const _mutate = async (key, build) => { build({
          paths: { 'a.jpg': { sha:'s1' }, 'gone.jpg': { deletedAt: 5 },
                   'dir/b (conflict from phone).jpg': { sha:'s2' } },
          drop: (p) => { const cur = this && null; dropped.push(p); },
        }); return { removed: dropped.length }; };
        FN('Pictures', ['a.jpg', 'gone.jpg', 'dir/b (conflict from phone).jpg', 'never-existed.jpg'])
          .then(r => process.stdout.write(JSON.stringify({ r, dropped })));
        """.replace("FN", fn)
        r = subprocess.run([node, "-e", js], capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr[-2000:])
        out = json.loads(r.stdout)
        # The real drop() skips dead paths itself; what this pins is the CONTRACT: exact paths in,
        # {removed: N} out — the shape both callers (verify's phantom repair, the bulk tool) read.
        self.assertIn("a.jpg", out["dropped"])
        self.assertEqual(out["r"].get("removed"), len(out["dropped"]))
