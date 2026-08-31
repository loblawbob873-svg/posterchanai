"""Clicking Save downloaded a document nothing had been asked to write.

Reported as "another office bug, clicking save when opening a blossom file did nothing".

`#office-save` waited 700ms and then fetched the WOPI session's document. The only thing that ever
writes that document is Collabora's own PutFile, which it sends when IT decides to — an autosave
tick, or closing. Click Save promptly after typing and the fetch returned the bytes the session had
opened with: the upload succeeded, the drive index moved to a new hash, the toast said "document
saved", and the file was byte-for-byte unchanged. The same handler serves a synced folder and
Download, so all three were saving nothing.

Two halves, both covered here: the editor is now ASKED (`Action_Save`, waiting for its
acknowledgement), and a result identical to what was opened reports itself instead of being
uploaded under a false success.

The runtime half runs the shipped `askEditorToSave` against a fake editor frame rather than reading
it, because the interesting failures are behavioural — a leaked listener, an unbounded wait, and
accepting a postMessage from any frame on the page.
"""
import re
import subprocess
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
APP = (HERE.parent.parent / "static" / "js" / "client" / "app.js").read_text(encoding="utf-8")


class TheEditorIsAskedToSave(unittest.TestCase):
    def test_the_shipped_handshake_behaves(self):
        out = subprocess.run(["node", "--unhandled-rejections=strict",
                              str(HERE / "office_save_handshake_runtime.mjs")],
                             capture_output=True, text=True, timeout=120)
        self.assertEqual(out.returncode, 0,
                         "the save handshake runtime failed:\n" + (out.stderr or "")[-3000:])

    def _save_handler(self) -> str:
        start = APP.index("$('#office-save',root).onclick=async e=>{")
        return APP[start:start + 2200]

    def test_save_asks_before_it_reads(self):
        body = self._save_handler()
        ask = body.find("askEditorToSave(root)")
        read = body.find("/contents?access_token=")
        self.assertGreaterEqual(ask, 0, "Save no longer asks the editor to save — this is the bug")
        self.assertLess(ask, read, "the document is read before the editor was asked to write it")

    def test_an_unchanged_document_is_not_uploaded_under_a_success_toast(self):
        body = self._save_handler()
        self.assertIn("_sameBytes(bytes, origBytes)", body)
        same = body.index("_sameBytes(bytes, origBytes)")
        back = body.index("await saveBack(")
        self.assertLess(same, back, "the unchanged check must come before the upload, not after")
        self.assertIn("no changes to save", body)

    def test_the_comparison_is_a_real_byte_comparison(self):
        fn = APP[APP.index("function _sameBytes("):APP.index("async function _officeSession(")]
        # Length alone would call a one-character edit "unchanged" and silently drop a save.
        self.assertIn("x[i] !== y[i]", fn)
        self.assertIn("x.length !== y.length", fn)

    def test_save_as_pdf_is_ours_and_saves_through_the_one_path_that_works(self):
        """CODE's own File > Download as > PDF hands the browser a download from inside a
        cross-origin iframe, which the desktop shell and the APK both refuse — so the click did
        nothing, silently, having actually run the conversion."""
        self.assertIn('id="office-pdf"', APP)
        start = APP.index("if(pdfBtn) pdfBtn.onclick")
        body = APP[start:start + 1400]
        self.assertIn("askEditorToSave(root)", body, "the PDF is converted from unsaved bytes")
        self.assertIn("/export/pdf?access_token=", body)
        self.assertIn("_officeSaveCopy(", body,
                      "PDF export must use the same destination-aware save path as Save As")
        self.assertNotIn("<a download", body)


class ALocalDocumentCanBeOpenedInOffice(unittest.TestCase):
    """"I am in the Home folder in Files and click on .odt file and can't open it in office? WTF!"

    A document on the drive and a document in a synced folder each had an Office button. A document
    on THIS COMPUTER had a chooser offering PosterChan Code — which refuses a .odt as binary — and
    "hand it to the machine". There was no Office path at all, so the answer to clicking an .odt in
    Home was a dead end, on the OS whose whole point is that it ships an office suite.
    """
    def _chooser(self) -> str:
        # hostfiles carries metadata on the callable `openHere`, preserving the four-argument
        # opener contract shared by Preview and Code.
        start = APP.index("openFile: async (path, name, openHere, mime) => {")
        return APP[start:start + 3500]

    def test_the_chooser_offers_office_for_a_document(self):
        body = self._chooser()
        self.assertIn("id:'office'", body)
        self.assertIn("_officeable(name || path, mime)", body,
                      "the Office choice must be gated on the same set the server accepts")
        self.assertIn("_officeSession(", body, "it must reuse the one office session machinery")

    def test_it_saves_back_as_bytes_and_never_as_a_string(self):
        """An .odt is a ZIP. Round-tripping it through `writeText` saves a file LibreOffice then
        refuses to open, with nothing said at the time it was destroyed."""
        body = self._chooser()
        office = body[body.index("id:'office'"):body.index("id:'code'")]
        self.assertIn("pcHost.writeBytes(", office)
        self.assertNotIn("writeText(", office)

    def test_the_bridge_that_button_calls_actually_exists(self):
        """A choice that reaches a method no bridge exposes is a dead button, and it looks exactly
        like the bug it was added to fix. All three layers are asserted here, in one place."""
        root = HERE.parent.parent
        preload = (root / "desktop" / "preload.js").read_text(encoding="utf-8")
        main = (root / "desktop" / "main.js").read_text(encoding="utf-8")
        hostfs = (root / "desktop" / "hostfs.js").read_text(encoding="utf-8")
        self.assertIn("writeBytes:", preload)
        self.assertIn("pc:host:writeBytes", preload)
        self.assertIn("ipcMain.handle('pc:host:writeBytes'", main)
        self.assertIn("function writeBytes(", hostfs)
        self.assertRegex(hostfs, r"module\.exports\s*=\s*\{[^}]*writeBytes")
        # Same atomic rename + compare-and-swap the text writer has; writing in place is how an
        # editor destroys the thing it was editing.
        fn = hostfs[hostfs.index("function writeBytes("):]
        fn = fn[:fn.index("\n}\n") + 3]
        self.assertIn("fs.renameSync(tmp, abs)", fn)
        self.assertIn("changed-on-disk", fn)
        self.assertIn("BYTES_MAX", fn)


if __name__ == "__main__":
    unittest.main()
