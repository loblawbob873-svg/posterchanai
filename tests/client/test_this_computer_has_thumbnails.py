"""A PICTURE ON THIS COMPUTER CAN SHOW ITSELF.

"0 thumbnails loaded in File Manager" was not a failure on this pane -- it was an absence. The
drive's tiles have had thumbnails for a long time; This Computer had no thumbnail code at all.

It costs nothing now that a local file has an ADDRESS: `pcHost.fileUrl` points at the shell's own
`__hostfile` route, so the tile hands the browser a URL and the browser decodes and scales it. No
read through the IPC bridge, no bytes in the renderer's heap -- which is what made this expensive
before and is the same reason local video is streamed rather than slurped.
"""
from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[2]
HOST = (ROOT / "static/js/client/hostfiles.js").read_text(encoding="utf-8")
CSS = (ROOT / "static/css/client.css").read_text(encoding="utf-8")


class TestTheTileAsksForAPicture(unittest.TestCase):
    def test_a_tile_carries_a_thumbnail_when_it_can(self):
        self.assertIn("thumbAttr(e, ext)", HOST, "tiles never ask for a thumbnail")
        self.assertIn("pcHost.fileUrl", HOST, "it reads bytes instead of addressing the file")

    def test_only_images(self):
        fn = HOST[HOST.index("const THUMB_EXT"):]
        fn = fn[: fn.index("const cells")]
        self.assertRegex(fn, r"THUMB_EXT\s*=\s*/\^\(\?:png\|jpe\?g")
        for not_image in ("mp4", "pdf", "zip", "txt"):
            self.assertNotIn(not_image, fn.split("THUMB_EXT")[1].split("\n")[0])

    def test_a_large_file_is_left_alone(self):
        """A thumbnail is worth a decode; a 40MB RAW is not."""
        fn = HOST[HOST.index("const THUMB_MAX"):]
        self.assertRegex(fn[:80], r"THUMB_MAX\s*=\s*\d+\s*\*\s*1024\s*\*\s*1024")

    def test_a_folder_never_gets_one(self):
        fn = HOST[HOST.index("const thumbAttr"):]
        fn = fn[: fn.index("const cells")]
        self.assertIn("e.dir", fn)
        self.assertIn("e.broken", fn)

    def test_the_web_and_the_apk_are_unaffected(self):
        """`pcHost` exists only in the desktop shell; everywhere else keeps the glyph it had."""
        fn = HOST[HOST.index("const thumbAttr"):]
        fn = fn[: fn.index("const cells")]
        self.assertIn("window.pcHost && pcHost.fileUrl", fn)
        self.assertIn("return ''", fn)


class TestTheGlyphGetsOutOfTheWay(unittest.TestCase):
    def test_the_stylesheet_paints_the_thumbnail(self):
        m = re.search(r"\.file-icon\[data-thumb-host\]\{([^}]*)\}", CSS)
        self.assertIsNotNone(m, "nothing draws the thumbnail the tile asked for")
        self.assertIn("background-size:cover", m.group(1))

    def test_the_glyph_is_replaced_not_covered(self):
        """A paperclip behind a photo is what '0 thumbnails' looked like from the other side."""
        self.assertRegex(CSS, r"\.file-icon\[data-thumb-host\]\s*>\s*\*\{display:none\}")


if __name__ == "__main__":
    unittest.main()
