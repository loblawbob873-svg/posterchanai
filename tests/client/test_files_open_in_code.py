"""A document in Files opens in PosterChan Code, and saving puts it back.

Run: venv-unified/bin/python -m pytest tests/client/test_files_open_in_code.py

Files → Blossom holds content-addressed BLOBS, not paths. PosterChan Code opens files by workspace
path (`/api/code/file?path=…`), so there was no way to get a document from the drive into the editor
at all — Office had one (the 📝 button) and Code had none.

The round trip is deliberately split: the editor holds the text and knows nothing about Blossom;
app.js owns the drive index, the encryption and the folder, so it owns the save. Content addressing
means an edit is a NEW blob — the index is re-pointed and the old blob is left recoverable, exactly
as the office save does.
"""
import os
import re
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
APP = os.path.join(ROOT, "static", "js", "client", "app.js")
CODE = os.path.join(ROOT, "static", "js", "client", "code.js")


def _read(p):
    with open(p, encoding="utf-8") as fh:
        return fh.read()


def _decomment(js):
    js = re.sub(r"/\*.*?\*/", "", js, flags=re.S)
    return re.sub(r"(?m)^\s*//.*$", "", js)


def _fn(src, head):
    i = src.index(head)
    j = src.index("{", i)
    depth, k = 0, j
    while k < len(src):
        if src[k] == "{":
            depth += 1
        elif src[k] == "}":
            depth -= 1
            if depth == 0:
                return src[i:k + 1]
        k += 1
    raise AssertionError(f"{head} never closes")


class FilesOpenInCode(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = _read(APP)
        cls.code = _read(CODE)

    # ---- the way in -------------------------------------------------------------------------
    def test_there_is_a_button_and_it_is_bound(self):
        self.assertIn('class="codebtn"', self.app, "no way to open a Files document in Code")
        self.assertIn("$$('.codebtn',grid)", self.app, "the button is drawn but nothing binds it")
        self.assertIn("openCodeFile(a.dataset)", self.app)

    def _code_ext(self):
        """The SHIPPED regex, compiled and run against real names. Splitting the alternation on `|`
        and looking for `yml` fails against `ya?ml`, which is a correct pattern — the test has to
        ask the same question the code does."""
        m = re.search(r"const _CODE_EXT = (/[^\n]*?/i);", self.app)
        self.assertTrue(m, "_CODE_EXT moved — re-point this test")
        body = m.group(1)[1:-2]                      # strip the / … /i
        return re.compile(body, re.I)

    def test_it_does_not_offer_to_edit_a_picture(self):
        """A .png opened as text is a screenful of mojibake and a corrupted file once saved."""
        rx = self._code_ext()
        for binary in ("shot.png", "a.jpg", "a.jpeg", "a.gif", "a.webp", "clip.mp4",
                       "song.mp3", "doc.pdf", "bundle.zip", "game.xdc"):
            self.assertIsNone(rx.search(binary), f"Code offers to edit {binary} as text")
        for text in ("notes.md", "a.json", "s.py", "a.js", "run.sh", "conf.yml",
                     "conf.yaml", "a.css", "a.txt"):
            self.assertIsNotNone(rx.search(text), f"Code will not open {text}")

    def test_a_spreadsheet_belongs_to_office_not_to_code(self):
        self.assertIsNone(self._code_ext().search("sheet.csv"),
                          "csv is in _OFFICE_EXT; offering both makes the two buttons fight")

    def test_binary_is_refused_by_its_BYTES_not_its_name(self):
        """A .txt that is really a zip must not open, whatever it is called."""
        body = _decomment(_fn(self.app, "async function openCodeFile("))
        self.assertIn("bytes.indexOf(0)", body,
                      "nothing checks for a NUL byte, so a mislabelled binary opens as mojibake")
        self.assertIn("_CODE_MAX", body, "no size limit on a buffer held in localStorage")

    def test_an_encrypted_file_is_decrypted_first(self):
        body = _decomment(_fn(self.app, "async function openCodeFile("))
        self.assertIn("encFileUrl", body, "an encrypted drive file would open as ciphertext")

    # ---- the way back -----------------------------------------------------------------------
    def test_the_editor_does_not_know_about_blossom(self):
        """It holds text. The index, the encryption and the folder live in app.js, so the save does."""
        body = _decomment(_fn(self.code, "async function saveDoc("))
        self.assertIn("saveBlobDoc", body, "code.js does not route a Files document back to Files")
        for leak in ("FilesIdx", "uploadEncFile", "uploadBlob"):
            self.assertNotIn(leak, self.code, f"code.js reaches into {leak} — that belongs in app.js")

    def test_the_saver_is_on_the_pc_surface(self):
        """The recurring `PC.x is not a function`: defined in app.js, never exported, called from a
        sub-module."""
        i = self.app.index("window.__PC = {")
        j = self.app.index("\n  };", i)
        self.assertIn("saveBlobDoc", self.app[i:j])

    def test_saving_re_points_the_index_and_keeps_the_old_blob(self):
        body = _decomment(_fn(self.app, "async function saveBlobDoc("))
        self.assertIn("FilesIdx.forget(desc.sha)", body,
                      "the old index entry survives, so the file appears twice in Files")
        self.assertIn("newSha !== desc.sha", body,
                      "an unchanged hash would forget the entry it just wrote")
        self.assertIn("uploadEncFile", body, "an encrypted file would be saved back in the clear")

    def test_a_restored_blob_buffer_never_asks_the_workspace_for_it(self):
        """`/api/code/file?path=<a file name>` resolves against the jail and 400s — the tab would
        come back as an error about a file that was never on this node."""
        body = _decomment(_fn(self.code, "async function hydrate("))
        self.assertIn("d.blob", body)
        self.assertLess(body.index("d.blob"), body.index("api("),
                        "hydrate reaches the workspace fetch before it checks for a blob buffer")


class SyncedFoldersOpenInCodeToo(unittest.TestCase):
    """A file in a SYNCED folder opens in Code, and saving reaches every device.

    A synced folder's rows offered Download, Save-a-copy-to-drive, Rename and Delete-everywhere —
    everything except opening the thing. And the save has to go back to the FOLDER: a copy quietly
    landing on the drive instead would look like the edit worked and change nothing anywhere else,
    which is the worst of the three possible outcomes.
    """

    @classmethod
    def setUpClass(cls):
        cls.app = _read(APP)

    def test_a_synced_file_can_be_opened(self):
        self.assertIn('class="codesync"', self.app, "no way to open a synced file in Code")
        self.assertIn("openSyncCodeFile(b.dataset)", self.app, "the button is drawn but not bound")

    def test_it_reuses_the_same_fetch_the_download_uses(self):
        """Blossom by sha OR chunk list, decrypted with the drive key. A file over ~16 MB has no
        `sha` of its own, so a path that only knows about sha silently cannot open the common case."""
        body = _decomment(_fn(self.app, "async function openSyncCodeFile("))
        self.assertIn("_syncFileBlob", body)
        self.assertIn("chunks", body, "a chunked file would appear to have no bytes")

    def test_the_buffer_knows_it_came_from_a_folder(self):
        body = _decomment(_fn(self.app, "async function openSyncCodeFile("))
        self.assertIn("sync: { key:", body,
                      "without the descriptor the save falls through to the drive path and the "
                      "edit reaches no other device")

    def test_saving_writes_back_to_the_folder_not_the_drive(self):
        body = _decomment(_fn(self.app, "async function saveBlobDoc("))
        self.assertIn("desc.sync", body)
        self.assertIn("PCSync.edit.uploadMany", body,
                      "a synced file is saved through some other path than the folder's own writer")
        # …and it must take that branch BEFORE the drive upload.
        self.assertLess(body.index("desc.sync"), body.index("uploadBlob"),
                        "the drive upload runs first, so a synced edit lands on the drive")

    def test_it_invalidates_the_manifest_it_just_changed(self):
        """The view must never redraw from the copy the write invalidated."""
        body = _decomment(_fn(self.app, "async function saveBlobDoc("))
        self.assertIn("_syncManifests.delete", body)

    def test_binary_and_size_are_refused_here_too(self):
        body = _decomment(_fn(self.app, "async function openSyncCodeFile("))
        self.assertIn("bytes.indexOf(0)", body)
        self.assertIn("_CODE_MAX", body)


if __name__ == "__main__":
    unittest.main()
