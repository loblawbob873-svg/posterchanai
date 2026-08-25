"""Puts preview_sim.js into the suite, and pins the wiring the sim cannot see.

"basiucally i want the preview to app to handle videos, images when people click on them from
blossom". Blossom's only answer for a picture, a video or a PDF was "open in a new tab", which on
the encrypted drive means decrypting to a blob URL and handing it to the browser - you leave the
app and lose the folder you were in, and on the APK it does nothing useful at all.
"""
import os
import re
import shutil
import subprocess
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SIM = os.path.join(ROOT, "tests", "client", "preview_sim.js")
NODE = shutil.which("node") or shutil.which("nodejs")


def _read(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as fh:
        return fh.read()


@unittest.skipIf(not NODE, "no node on this node")
class PreviewSim(unittest.TestCase):
    def test_the_suite_passes(self):
        r = subprocess.run([NODE, SIM], capture_output=True, text=True, timeout=120)
        self.assertEqual(r.returncode, 0, (r.stdout + r.stderr)[-3000:])
        self.assertIn("preview holds", r.stdout)


class PreviewIsReachable(unittest.TestCase):
    """A viewer nothing opens is a file in a directory."""

    @classmethod
    def setUpClass(cls):
        cls.app = _read("static/js/client/app.js")
        cls.tpl = _read("templates/client.html")
        cls.sw = _read("static/js/client/sw.js")
        cls.css = _read("static/css/client.css")

    def test_the_chooser_offers_it(self):
        i = self.app.index("function _handlersFor(")
        block = self.app[i:self.app.index("return out;", i)]
        self.assertIn("id:'preview'", block, "clicking a photograph offers no way to look at it")

    def test_pdf_preview_is_offered_on_a_cold_start(self):
        """The module is lazy. Its missing global must not make Preview disappear before first use."""
        i = self.app.index("const _previewable")
        block = self.app[i:self.app.index(";", i) + 1]
        self.assertNotIn("PCPreview", block)
        self.assertIn("_PREVIEW_EXT", block)
        rx = re.search(r"const _PREVIEW_EXT = (/[^\n]*?/i);", self.app)
        self.assertTrue(rx)
        self.assertRegex("manual.pdf", re.compile(rx.group(1)[1:-2], re.I))

    def test_it_is_offered_before_the_editors(self):
        """Looking at a file is the lightest thing you can do with it, and it is what somebody
        clicking a photograph meant. An editor first is a chooser that leads with the wrong answer."""
        i = self.app.index("function _handlersFor(")
        block = self.app[i:self.app.index("return out;", i)]
        self.assertLess(block.index("id:'preview'"), block.index("id:'code'"))
        self.assertLess(block.index("id:'preview'"), block.index("id:'office'"))

    def test_every_source_can_feed_it(self):
        """The drive (plain AND encrypted) and a synced folder. A viewer wired to one of the three
        is a button that works in one folder and not the next."""
        i = self.app.index("async function _previewBytes(")
        body = self.app[i:self.app.index("async function openPreviewFile(", i)]
        self.assertIn("_syncFileBlob", body, "a synced file cannot be previewed")
        self.assertIn("encFileUrl", body, "an encrypted file cannot be previewed")
        self.assertIn("d.url", body, "a plain drive file cannot be previewed")

    def test_the_module_is_shipped_and_cached(self):
        """It is lazily loaded through _withModule, so a missing script tag is survivable - but a
        missing PRECACHE entry means the viewer is simply absent offline, which is exactly when
        somebody is looking through their own drive."""
        self.assertIn("client/preview.js", self.tpl, "the module is never loaded")
        self.assertIn("'/static/js/client/preview.js'", self.sw, "not precached; absent offline")

    def test_the_back_button_closes_the_picture_not_the_folder(self):
        """It is a full-screen sheet on a phone, opened FROM a screen the person still wants."""
        i = self.app.index("PCPreview.isOpen")
        self.assertGreater(i, 0, "Android Back does not close the preview")
        seg = self.app[i - 400:i + 200]
        self.assertIn("PCPreview.close()", seg)

    def test_it_is_styled_for_both_hosts(self):
        """A desktop window's slot and a full-screen sheet. Without the rules the media is sized by
        the viewport inside a window somebody just resized - the office editor's "tiny white box"."""
        for sel in (".pv-host{", ".pv-win{", ".pv-sheet{", ".pv-vid{", ".pv-img{"):
            self.assertIn(sel, self.css, f"{sel} has no rule")

    def test_the_download_never_uses_a_bare_anchor(self):
        """The APK's WebView ignores a programmatic download and the desktop's app:// origin refuses
        one, so `<a download>` is a button that silently does nothing on two of three platforms."""
        src = _read("static/js/client/preview.js")
        self.assertIn("saveBlobAs", src)
        self.assertNotIn("download=", src)

    def test_the_blob_url_is_released(self):
        """One leaked object URL per file means every picture you looked at is still in memory."""
        src = _read("static/js/client/preview.js")
        self.assertIn("revokeObjectURL", src)

    def test_closing_stops_the_sound(self):
        """A <video> detached from the document keeps playing in Chromium until it is collected."""
        src = _read("static/js/client/preview.js")
        self.assertIn(".pause()", src, "a closed window would keep talking")


if __name__ == "__main__":
    unittest.main()
