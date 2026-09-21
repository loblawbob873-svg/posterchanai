"""Meme Builder as a photo editor: the exported file has the size and format that were asked for.

Run: venv-unified/bin/python -m pytest tests/test_meme_export_size.py

"Export at 1234x567 as a JPEG" is a promise about the FILE, so every assertion here opens the bytes
the renderer returned and reads the real pixel dimensions and container back — a filter-string test
would pass against a scale that ffmpeg silently rounded, or a JPEG that is really a PNG.

  * a still is exported at EXACTLY the numbers asked for, odd ones included (h264's even rule does not
    apply to a picture)
  * JPEG and WebP are what they claim to be, and the quality knob changes the file
  * the resample is applied to the FINISHED composite, so a layer that filled the canvas still fills
    the exported picture edge to edge (it was not scaled once more on its own)
  * an MP4 honours the size too, rounded to even
  * nonsense sizes clamp rather than fail, and an unknown format is refused by name
"""
import io
import os
import subprocess
import tempfile
import unittest

from PIL import Image

from app.services import meme_builder_service as mb


class TestExportSize(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not mb.media_service.resolve_ffmpeg():
            raise unittest.SkipTest("ffmpeg is not available on this node")

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="pcmemesize-")
        # Left half red, right half blue: position survives a resample, a uniform colour would not.
        im = Image.new("RGB", (400, 300), (220, 20, 20))
        im.paste((20, 20, 220), (200, 0, 400, 300))
        self.src = os.path.join(self.tmp, "a.png")
        im.save(self.src)

    def tearDown(self):
        for f in os.listdir(self.tmp):
            os.unlink(os.path.join(self.tmp, f))
        os.rmdir(self.tmp)

    def _edit(self, **extra):
        e = {"w": 400, "h": 300, "fps": 10, "bg": "#00ff00", "duration": 1.0, "fmt": "png",
             "layers": [{"type": "image", "src": "S", "start": 0, "dur": 1.0,
                         "x": 0, "y": 0, "w": 400, "h": 300, "opacity": 1, "fit": "cover"}]}
        e.update(extra)
        return e

    def _render(self, **extra):
        return mb.render(self._edit(**extra), {"S": self.src})

    def test_png_is_exactly_the_size_asked_for_odd_numbers_included(self):
        data, ctype = self._render(out_w=333, out_h=517)
        self.assertEqual(ctype, "image/png")
        im = Image.open(io.BytesIO(data))
        self.assertEqual(im.format, "PNG")
        self.assertEqual(im.size, (333, 517))

    def test_no_size_means_the_canvas(self):
        data, _ = self._render()
        self.assertEqual(Image.open(io.BytesIO(data)).size, (400, 300))

    def test_upscale_beyond_the_canvas_cap(self):
        # The canvas is capped at MAX_DIM; an EXPORT may go past it (a print-size still).
        data, _ = self._render(out_w=2400, out_h=1800)
        self.assertEqual(Image.open(io.BytesIO(data)).size, (2400, 1800))

    def test_jpeg_is_a_jpeg_of_that_size_and_quality_matters(self):
        hi, ctype = self._render(fmt="jpeg", out_w=801, out_h=601, quality=95)
        self.assertEqual(ctype, "image/jpeg")
        im = Image.open(io.BytesIO(hi))
        self.assertEqual((im.format, im.size), ("JPEG", (801, 601)))
        lo, _ = self._render(fmt="jpg", out_w=801, out_h=601, quality=10)
        self.assertEqual(Image.open(io.BytesIO(lo)).format, "JPEG")
        self.assertLess(len(lo), len(hi), "quality 10 should be a smaller file than quality 95")

    def test_webp_is_a_webp_of_that_size(self):
        data, ctype = self._render(fmt="webp", out_w=250, out_h=190, quality=80)
        self.assertEqual(ctype, "image/webp")
        im = Image.open(io.BytesIO(data))
        self.assertEqual((im.format, im.size), ("WEBP", (250, 190)))

    def test_the_whole_composite_is_resampled_not_the_layer(self):
        # A full-canvas layer must still reach every edge after a 2x export — the canvas background
        # (green) must not appear anywhere, and left/right halves keep their colours.
        data, _ = self._render(out_w=800, out_h=600)
        im = Image.open(io.BytesIO(data)).convert("RGB")
        for xy in ((2, 2), (797, 2), (2, 597), (797, 597)):
            r, g, b = im.getpixel(xy)
            self.assertLess(g, 100, "the canvas background shows at %r: %r" % (xy, (r, g, b)))
        self.assertGreater(im.getpixel((100, 300))[0], 150)   # red on the left
        self.assertGreater(im.getpixel((700, 300))[2], 150)   # blue on the right

    def test_mp4_honours_the_size_rounded_to_even(self):
        data, ctype = self._render(fmt="mp4", out_w=641, out_h=361)
        self.assertEqual(ctype, "video/mp4")
        p = os.path.join(self.tmp, "o.mp4")
        with open(p, "wb") as fh:
            fh.write(data)
        out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                              "stream=width,height", "-of", "csv=p=0", p],
                             capture_output=True, text=True, timeout=30).stdout.strip()
        self.assertEqual(out, "640,360")

    def test_sizes_clamp_rather_than_fail(self):
        self.assertEqual(mb.export_size({"out_w": 0, "out_h": -5}, 400, 300, "png"), (1, 1))
        self.assertEqual(mb.export_size({"out_w": 99999, "out_h": "x"}, 400, 300, "png"),
                         (mb.MAX_EXPORT_DIM, 300))
        self.assertEqual(mb.export_size({"out_w": 7, "out_h": 9}, 400, 300, "mp4"), (16, 16))

    def test_unknown_format_is_refused(self):
        with self.assertRaises(ValueError):
            self._render(fmt="bmp")


if __name__ == "__main__":
    unittest.main()
