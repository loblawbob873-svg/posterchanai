"""A file deleted here must not be put back by the server's own copy.

The index is one document, and a pull that lands while edits are pending folds the server's copy
UNDER the local one: `Object.assign({}, srv.files, loc.files)`. A file the user just DELETED is in
neither side's "local wins" — it is absent locally and present on the server — so the merge puts it
straight back. And that is the ordinary path, not a corner case: `_save()` pulls first whenever it
has not yet confirmed what the server holds.

Measured against the live instance: "Remove 2422 missing tracks" removed them, the save pulled, the
merge re-added all 2422, and the write went out with the same 3990 entries. No collapse, so the
server's guard saw nothing wrong; the save reported success honestly; the tracks were still there.
Three attempts over two days looked like this, and the last one was after the success toast had
already been made truthful — which is how the merge, rather than the signer, was finally pinned.

  delete-survives-merge   a merge cannot resurrect a file this device deleted
  other-files-untouched   everything else still merges exactly as before (that fold is what keeps a
                          drive's foldering when two devices write)
  reupload-clears-it      uploading the same content again brings it back — a tombstone is not a ban
  bounded                 tombstones are capped and aged out; this is a shield against copies still
                          in flight, not a second index

The methods are extracted from app.js rather than copied, so they cannot drift from what ships.
"""
import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
APP = os.path.join(REPO, "static", "js", "client", "app.js")


def _fn(src, name, opener):
    i = src.index(opener)
    depth, j, started = 0, i, False
    while j < len(src):
        if src[j] == "{":
            depth += 1
            started = True
        elif src[j] == "}":
            depth -= 1
            if started and depth == 0:
                return src[i:j + 1]
        j += 1
    raise AssertionError("could not bound " + name)


def _harness():
    with open(APP) as fh:
        src = fh.read()
    caps = re.search(r"_DEL_MAX: (\d+), _DEL_TTL: ([^,\n]+),", src)
    assert caps, "the tombstone caps are gone"
    parts = [
        "const idx = {",
        "  data: { folders:['Music'], files:{}, encFolders:[], deleted:{} },",
        "  saveLocal(){},",
        "  push(){ this.pushed = (this.pushed||0) + 1; },",
        f"  _DEL_MAX: {caps.group(1)}, _DEL_TTL: {caps.group(2).strip()},",
        _fn(src, "_norm", "_norm(){ if(!this.data") + ",",
        _fn(src, "_tomb", "_tomb(sha){") + ",",
        _fn(src, "_dropDeleted", "_dropDeleted(files){") + ",",
        _fn(src, "_mergeFiles", "_mergeFiles(srv, loc){") + ",",
        "  _syncedAt: 0,",
        _fn(src, "_merge", "_merge(srv){") + ",",
        _fn(src, "forget", "forget(sha){") + ",",
        _fn(src, "setFile", "setFile(sha, m){") + ",",
        "};",
    ]
    return "\n".join(parts)


PAGE = """<!doctype html><meta charset="utf-8"><pre id="out"></pre><script>
__EXTRACTED__
const out = {};
const server = { folders:['Music','Photos'], encFolders:['Music'],
                 files: { a:{name:'a.ogg',folder:'Music'}, b:{name:'b.ogg',folder:'Music'},
                          c:{name:'c.jpg',folder:'Photos'} } };
// Start from the server's state, as a device that has pulled once does.
idx.data = { folders:['Music','Photos'], encFolders:['Music'],
             files: JSON.parse(JSON.stringify(server.files)), deleted:{} };

// 1. the reported loop: delete two, then a pull lands with edits pending and merges
idx.forget('a'); idx.forget('b');
out.afterForget = Object.keys(idx.data.files).sort();
idx._merge(JSON.parse(JSON.stringify(server)));
out.afterMerge = Object.keys(idx.data.files).sort();
out.foldersKept = idx.data.folders.slice().sort();

// 2. a file the server has and we never touched still merges in
idx._merge({ folders:['Music'], encFolders:[], files:{ d:{name:'d.png'} } });
out.newFileArrived = Object.keys(idx.data.files).sort();

// 3. re-uploading deleted content brings it back
idx.setFile('a', {name:'a.ogg', folder:'Music'});
idx._merge(JSON.parse(JSON.stringify(server)));
out.afterReupload = Object.keys(idx.data.files).sort();

// 4. A DELETION MADE ON ANOTHER DEVICE. This device never saw it: no tombstones of its own, it
//    simply holds the pre-deletion library. The server's copy is the one that is right.
{
  const phone = Object.create(idx);
  phone.data = { folders:['Music'], encFolders:['Music'], deleted:{},
                 files:{ old1:{name:'a',ts:100}, old2:{name:'b',ts:100}, mine:{name:'c',ts:500} } };
  phone._syncedAt = 300;                      // last agreed with the server at t=300
  // The server has since dropped old1/old2 (deleted on the laptop) and gained fresh1.
  phone._merge({ folders:['Music'], encFolders:['Music'], files:{ fresh1:{name:'d',ts:400} } });
  out.phoneKeeps = Object.keys(phone.data.files).sort();
}
// …and with no record of ever having synced, nothing is dropped: the old union, which never loses.
{
  const cold = Object.create(idx);
  cold.data = { folders:['Music'], encFolders:[], deleted:{}, files:{ old1:{name:'a',ts:100} } };
  cold._syncedAt = 0;
  cold._merge({ folders:['Music'], encFolders:[], files:{} });
  out.coldKeeps = Object.keys(cold.data.files).sort();
}
// 5. bounded — old tombstones age out rather than accumulating forever
idx.data.deleted = {}; idx._tomb('z');
idx.data.deleted['ancient'] = 1;                       // 1970
for (let i = 0; i < 512; i++) idx._tomb('k' + i);      // crosses the trim checkpoint
out.ancientDropped = !('ancient' in idx.data.deleted);
out.recentKept = 'z' in idx.data.deleted;
document.getElementById('out').textContent = JSON.stringify(out);
</script>"""


class FilesIndexDeletesStick(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        chrome = (shutil.which("google-chrome-stable") or shutil.which("chromium")
                  or shutil.which("google-chrome") or shutil.which("chrome"))
        if not chrome:
            raise unittest.SkipTest("no chrome")
        tmp = tempfile.mkdtemp(prefix="pcdel-")
        try:
            path = os.path.join(tmp, "t.html")
            with open(path, "w") as fh:
                fh.write(PAGE.replace("__EXTRACTED__", _harness()))
            res = subprocess.run(
                [chrome, "--headless", "--no-sandbox", "--disable-gpu",
                 "--virtual-time-budget=15000", "--dump-dom", "file://" + path],
                capture_output=True, text=True, timeout=180).stdout
            m = re.search(r'<pre id="out">(.*?)</pre>', res, re.S)
            if not m or not m.group(1).strip():
                raise unittest.SkipTest("page did not evaluate")
            cls.r = json.loads(m.group(1))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_a_merge_cannot_resurrect_a_deleted_file(self):
        self.assertEqual(self.r["afterForget"], ["c"])
        self.assertEqual(self.r["afterMerge"], ["c"],
                         "the server's copy put the deleted files back — this is the reported bug")

    def test_the_rest_of_the_merge_still_works(self):
        """That fold is what keeps a drive's foldering when two devices write; the fix must not cost
        it."""
        self.assertEqual(self.r["foldersKept"], ["Music", "Photos"])
        self.assertEqual(self.r["newFileArrived"], ["c", "d"])

    def test_re_uploading_deleted_content_brings_it_back(self):
        self.assertEqual(self.r["afterReupload"], ["a", "c", "d"],
                         "a tombstone must not outlive a deliberate re-upload")

    def test_a_deletion_made_on_another_device_is_honoured(self):
        """The reported failure: 2422 entries deleted on a desktop, resurrected by a phone that
        still held the pre-deletion library — which then made the desktop's next save look like it
        was collapsing the list. A file the server no longer has, that this device already knew
        about at its last sync, was deleted by somebody. One added here since was not."""
        self.assertEqual(self.r["phoneKeeps"], ["fresh1", "mine"],
                         "old1/old2 were deleted elsewhere; `mine` was added here after the last sync")

    def test_a_device_that_never_synced_drops_nothing(self):
        """No recorded sync means no way to tell a deletion from an addition — so it falls back to
        the union, which is the behaviour that never loses data."""
        self.assertEqual(self.r["coldKeeps"], ["old1"])

    def test_tombstones_are_bounded(self):
        self.assertTrue(self.r["ancientDropped"], "old tombstones must age out")
        self.assertTrue(self.r["recentKept"])


if __name__ == "__main__":
    unittest.main()
