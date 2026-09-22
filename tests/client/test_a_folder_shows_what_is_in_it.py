"""A folder in the attach picker must show what is IN it, not what fits in the listing window.

The picker's BUD-02 listing is deliberately bounded (`?limit=2000`) because a full listing on this
deployment is 37,483 blobs / 9.7 MB and awaiting it made the sheet a permanent spinner. But the grid
then filtered that WINDOW by folder, so any folder whose files are older than the newest 2000
rendered "Nothing in this folder." Measured on a real account at the time of the report:

    Memes    79 files ->  0 shown        Anime  75 ->  0        Blacks 60 -> 0
    Jews     60       ->  0              Notes 1146 ->  0       Music 2444 -> 0
    Messages 2903     -> 89              Posts    50 -> 48

Almost every folder in the sheet was empty — reported as "i am trying to attach an image to post
from blossom but nothing in this folder?". That is worse than the spinner it replaced: a hang looks
broken, this looks like the files are gone.

This RUNS the shipped `_folderRows` out of app.js under node against a stub index and a stub window.
"""
import json
import re
import subprocess
import unittest
from pathlib import Path
from tests.client_source import client_source

APP = Path(__file__).resolve().parents[2] / "static" / "js" / "client" / "app.js"


def _run(window, index_files, folder, caller_filter="null"):
    src = client_source()
    keep = re.search(r"      const _keep = b => \{.*?\n      \};", src, re.S)
    rows = re.search(r"      const _folderRows = f => \{.*?\n      \};", src, re.S)
    if not keep:
        raise AssertionError("_keep not found in blossomPicker — the hygiene filter was renamed")
    if not rows:
        raise AssertionError("_folderRows not found — a folder is being drawn from the window again")

    js = """
const server = 'https://media.example';
const opts = { filter: %s };
const mimeForName = n => /\\.(jpe?g|png|gif|webp)$/i.test(n||'') ? 'image/jpeg'
                       : /\\.(mp3|m4a|flac|wav)$/i.test(n||'') ? 'audio/mpeg' : '';
const _MIME_EXT = {};
const INDEX = %s;
const FilesIdx = {
  _lastIndexSha: 'ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff',
  _norm(){ return { files: INDEX }; },
  meta(sha){ return INDEX[sha] || null; },
  folderOf(sha){ return (INDEX[sha]||{}).folder || ''; },
  isEncFolder(n){ return n === 'Music' || n === 'Notes'; },
};
let list = %s;
%s
list = list.filter(_keep);
%s
const out = _folderRows(%s);
console.log(JSON.stringify(out.map(b => ({sha: b.sha256, name: b.name, type: b.type, size: b.size}))));
""" % (caller_filter, json.dumps(index_files), json.dumps(window),
       keep.group(0), rows.group(0), json.dumps(folder))

    p = subprocess.run(["node", "-e", js], capture_output=True, text=True, timeout=60)
    if p.returncode != 0:
        raise AssertionError("node failed: " + (p.stderr or "")[:600])
    return json.loads(p.stdout.strip().splitlines()[-1])


def _sha(n):
    return ("%064x" % n)


class FolderShowsItsFiles(unittest.TestCase):
    def test_a_file_outside_the_window_is_still_shown(self):
        """The regression, in one case."""
        window = [{"sha256": _sha(1), "url": "u1", "size": 10, "type": "image/png", "name": "new.png"}]
        index = {
            _sha(1): {"folder": "Memes", "name": "new.png", "mime": "image/png", "size": 10, "ts": 200},
            _sha(2): {"folder": "Memes", "name": "old.png", "mime": "image/png", "size": 20, "ts": 100},
        }
        got = _run(window, index, "Memes")
        shas = {r["sha"] for r in got}
        self.assertIn(_sha(2), shas,
                      "a file in the folder but outside the listing window is still missing")
        self.assertEqual(len(got), 2)

    def test_a_whole_folder_outside_the_window_is_not_empty(self):
        """The measured shape: Memes 79 -> 0, Anime 75 -> 0, Blacks 60 -> 0."""
        window = [{"sha256": _sha(900), "url": "u", "size": 1, "type": "image/png", "name": "other.png"}]
        index = {_sha(900): {"folder": "Recent", "name": "other.png", "mime": "image/png", "size": 1}}
        for i in range(1, 80):
            index[_sha(i)] = {"folder": "Memes", "name": "m%d.png" % i, "mime": "image/png", "size": i}
        got = _run(window, index, "Memes")
        self.assertEqual(len(got), 79,
                         "the folder drew %d of 79 files" % len(got))

    def test_a_synthesised_row_carries_a_usable_address(self):
        """A tile with no url is a file you cannot pick."""
        index = {_sha(3): {"folder": "Memes", "name": "x.png", "mime": "image/png", "size": 5}}
        got = _run([], index, "Memes")
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]["type"], "image/png")
        self.assertEqual(got[0]["name"], "x.png")

    def test_the_callers_own_filter_still_applies(self):
        """Hygiene must not be skipped just because a row came from the index."""
        index = {
            _sha(4): {"folder": "Memes", "name": "pic.png", "mime": "image/png", "size": 1},
            _sha(5): {"folder": "Memes", "name": "song.mp3", "mime": "audio/mpeg", "size": 1},
        }
        got = _run([], index, "Memes",
                   caller_filter="b => /^image\\//.test(b.type||'')")
        self.assertEqual([r["name"] for r in got], ["pic.png"],
                         "the caller's narrowing was not applied to index-derived rows")

    def test_an_encrypted_folder_is_still_refused_by_default(self):
        """Music/Notes are encrypted; a URL-publishing caller must not be offered ciphertext."""
        index = {_sha(6): {"folder": "Music", "name": "t.mp3", "mime": "audio/mpeg", "size": 1, "enc": True}}
        got = _run([], index, "Music")
        self.assertEqual(got, [], "an encrypted blob leaked into a picker that did not opt in")

    def test_all_files_still_uses_the_bounded_window(self):
        """'All' must stay instant — it must NOT start enumerating the whole index."""
        window = [{"sha256": _sha(7), "url": "u", "size": 1, "type": "image/png", "name": "a.png"}]
        index = {_sha(7): {"folder": "", "name": "a.png", "mime": "image/png", "size": 1},
                 _sha(8): {"folder": "", "name": "b.png", "mime": "image/png", "size": 1}}
        got = _run(window, index, "")
        self.assertEqual(len(got), 1,
                         "the All view is no longer the bounded window")


if __name__ == "__main__":
    unittest.main()
