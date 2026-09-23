"""Files → Synced folders: Move to… moves a selection into another folder, in one write, safely.

Reported as "File Manager is missing basic file operations like cut, copy, paste, move" -- from a
synced folder, which could rename one path at a time and could not move anything: its rename refused
a "/" and said to "drag it on a device". The engine's `rename` already took a full path; `move` is
the same record edit for a selection, with every one of rename's refusals.

The SHIPPED `edit.move`, its helpers and a manifest writer with the real put/drop/merge-check shape
are RUN under node. The button is checked in the shipped files.js.
"""
import json
import os
import re
import shutil
import subprocess
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SYNC = open(os.path.join(ROOT, "static", "js", "client", "sync.js"), encoding="utf-8").read()
FILES = open(os.path.join(ROOT, "static", "js", "client", "files.js"), encoding="utf-8").read()
NODE = shutil.which("node") or shutil.which("nodejs")


def _fn(src, header):
    """A function or method body by brace matching, from its header."""
    i = src.index(header)
    j = src.index("{", i + len(header) - 1)
    depth = 0
    while True:
        c = src[j]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return src[i:j + 1]
        j += 1


HARNESS = r"""
%(reserved)s
const deviceName = () => 'laptop';
%(liveUnder)s
%(blockedBy)s
let MANIFEST, FRESH_EXTRA = {};
async function _mutate(key, build, verify){
  const paths = JSON.parse(JSON.stringify(MANIFEST));
  const touched = {};
  build({ paths, put: (p, e) => { paths[p] = e; touched[p] = 1; },
                 drop: (p) => { paths[p] = { deletedAt: 1 }; touched[p] = 1; } });
  const fresh = Object.assign(JSON.parse(JSON.stringify(MANIFEST)), FRESH_EXTRA);
  if (verify) verify(fresh);
  MANIFEST = paths;
  return { touched: Object.keys(touched) };
}
const edit = { %(move)s };
const live = () => Object.keys(MANIFEST).filter(p => !MANIFEST[p].deletedAt).sort();
const START = { 'a.jpg': {sha:'1'}, 'b.jpg': {sha:'2'}, 'Trip/c.jpg': {sha:'3'},
                'Trip/day1/d.jpg': {sha:'4'}, 'Docs/a.jpg': {sha:'5'}, 'old.jpg': {deletedAt: 3} };
const run = async (froms, dir, extra) => {
  MANIFEST = JSON.parse(JSON.stringify(START)); FRESH_EXTRA = extra || {};
  try { await edit.move('Pictures', froms, dir); return { live: live(), ok: true }; }
  catch (e) { return { live: live(), why: e.message }; }
};
(async () => {
  const out = {};
  out.one = await run(['b.jpg'], 'Trip');
  out.many = await run(['b.jpg', 'Trip/day1'], 'Docs');
  out.up = await run(['Trip/c.jpg'], '');
  out.clash = await run(['a.jpg'], 'Docs');
  out.inside = await run(['Trip'], 'Trip/day1');
  out.intoFile = await run(['b.jpg'], 'a.jpg');
  out.same = await run(['a.jpg'], '');
  out.raced = await run(['b.jpg'], 'Trip', { 'Trip/b.jpg': { sha: 'other' } });
  out.newdir = await run(['a.jpg'], '/2026/New/');
  out.keeps = (await run(['b.jpg'], 'Trip'), MANIFEST['Trip/b.jpg']);
  process.stdout.write(JSON.stringify(out));
})();
"""


@unittest.skipIf(not NODE, "needs node")
class SyncedMove(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        move = _fn(SYNC, "    async move(key, froms, dir){")
        js = HARNESS % {
            "liveUnder": _fn(SYNC, "  function _liveUnder(paths, path){"),
            "blockedBy": _fn(SYNC, "  function _blockedBy(paths, path){"),
            "move": move.strip(),
            "reserved": re.search(r"  const RESERVED = .*;", SYNC).group(0),
        }
        r = subprocess.run([NODE, "-e", js], capture_output=True, text=True, timeout=60)
        assert r.returncode == 0, r.stderr[-2000:]
        cls.out = json.loads(r.stdout)

    def test_a_file_moves_into_a_folder(self):
        self.assertTrue(self.out["one"]["ok"], self.out["one"])
        self.assertIn("Trip/b.jpg", self.out["one"]["live"])
        self.assertNotIn("b.jpg", self.out["one"]["live"])

    def test_a_folder_moves_with_everything_in_it_in_one_go(self):
        live = self.out["many"]["live"]
        self.assertIn("Docs/b.jpg", live)
        self.assertIn("Docs/day1/d.jpg", live)
        self.assertNotIn("Trip/day1/d.jpg", live)

    def test_moving_to_the_top_level_is_allowed(self):
        self.assertIn("c.jpg", self.out["up"]["live"])

    def test_a_collision_is_refused_and_nothing_changes(self):
        self.assertIn("already exists", self.out["clash"]["why"])
        self.assertIn("a.jpg", self.out["clash"]["live"])
        self.assertEqual(self.out["clash"]["live"].count("Docs/a.jpg"), 1)

    def test_a_folder_cannot_go_inside_itself_or_a_file(self):
        self.assertIn("inside itself", self.out["inside"]["why"])
        self.assertIn("is a file", self.out["intoFile"]["why"])

    def test_already_there_is_said_not_written(self):
        self.assertIn("already in that folder", self.out["same"]["why"])

    def test_a_file_created_elsewhere_meanwhile_is_not_overwritten(self):
        self.assertIn("another device", self.out["raced"]["why"])
        self.assertIn("b.jpg", self.out["raced"]["live"])

    def test_slashes_around_the_destination_are_ignored(self):
        self.assertIn("2026/New/a.jpg", self.out["newdir"]["live"])

    def test_the_moved_entry_keeps_its_bytes(self):
        self.assertEqual(self.out["keeps"]["sha"], "2")
        self.assertEqual(self.out["keeps"]["device"], "laptop")

    def test_there_is_one_move_and_the_bar_offers_it(self):
        self.assertEqual(SYNC.count("async move(key, froms, dir){"), 1,
                         "two edit.move methods -- the later one silently shadows the first")
        self.assertIn('id="ss-move"', FILES)
        handler = FILES[FILES.index("const mv = $('#ss-move', grid);"):]
        handler = handler[:handler.index("if(_syncSelOn){")]
        self.assertIn("PCSync.edit.move(", handler)
        self.assertIn("_syncFolderList(paths)", handler)


if __name__ == "__main__":
    unittest.main()
