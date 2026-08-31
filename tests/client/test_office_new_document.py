"""Files offers a way to CREATE a document, and it reuses the paths that already work.

"a huge office gap, no way to create a new document by type" — and it was exactly that: the app
could open a document off the drive and had nothing that made one, so starting a spreadsheet needed
a spreadsheet you already had.

The risk in adding it is duplication, not absence: a second upload path that forgets a folder's
encryption writes somebody's document to the relay in the clear, and a second save path drifts from
the one openOfficeFile already implements. So this asserts the wiring reuses the existing pieces
rather than growing parallel ones.
"""
import re
import unittest
from pathlib import Path

APP = (Path(__file__).resolve().parents[2] / "static" / "js" / "client" / "app.js").read_text(
    encoding="utf-8")


def _fn(name, until):
    start = APP.index(name)
    return APP[start:APP.index(until, start)]


class NewDocumentIsReachableAndReusesTheDrive(unittest.TestCase):
    def test_the_button_exists_and_is_bound(self):
        self.assertIn('id="bl-newdoc"', APP, "Files has no New document button")
        self.assertIn("$('#bl-newdoc',pane); if(nd) nd.onclick=_newDocumentModal;", APP,
                      "the New document button is drawn but wired to nothing — the shape that made "
                      "'Copy npub does nothing' a bug report")

    def test_all_three_types_are_offered(self):
        kinds = _fn("const _DOC_KINDS", "async function _createOfficeDocument")
        for k in ("text", "spreadsheet", "presentation"):
            self.assertIn("'%s'" % k, kinds, "%s is not offered" % k)

    def test_an_encrypted_folder_still_encrypts(self):
        """The one mistake here that cannot be undone: a second upload path that ignores the
        folder's encryption publishes a private document to a relay in the clear."""
        create = _fn("async function _createOfficeDocument", "function _newDocumentModal")
        self.assertIn("FilesIdx.isEncFolder(folder)", create,
                      "the new document ignores whether its folder is encrypted")
        self.assertIn("uploadEncFile(file, folder", create,
                      "an encrypted folder must use the same encrypting upload as every other file")

    def test_it_opens_through_the_existing_office_path(self):
        """openOfficeFile owns decrypt-open-save-reindex. A second copy of that would drift."""
        modal = _fn("function _newDocumentModal", "async function openOfficeFile")
        self.assertIn("openOfficeFile(d)", modal,
                      "the new document is not opened through openOfficeFile, so its save path is "
                      "a second implementation of the one that already works")

    def test_the_name_cannot_escape_its_folder(self):
        create = _fn("async function _createOfficeDocument", "function _newDocumentModal")
        self.assertRegex(create, r"replace\(/\[\\\\/\]\+/g",
                         "a typed name with a slash in it goes into the drive index verbatim")
        self.assertIn("|| 'Untitled'", create, "an empty name must still produce a file")


if __name__ == "__main__":
    unittest.main()
