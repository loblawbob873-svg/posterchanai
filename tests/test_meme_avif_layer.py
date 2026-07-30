"""An AVIF/HEIC still must survive a render, not abort it.

Run: venv-unified/bin/python -m unittest tests.test_meme_avif_layer

`-loop 1` is an option of ffmpeg's IMAGE2 demuxer. AVIF and HEIC are ISOBMFF, so they are demuxed by
mov,mp4,m4a,3gp,3g2,mj2 instead — which has no `loop` option and does NOT ignore it: ffmpeg exits
with "Option loop not found" before decoding a single frame, so ONE .avif layer (what you get from
saving an image in a current browser, or from a phone in the HEIC case) failed the WHOLE meme.

The tests are deliberately end-to-end and pixel-based, because the obvious near-miss fix — dropping
`-loop` for these formats — also "passes" a string assertion while being wrong: a single-frame mov
input covers only its own frame duration, so the layer would appear for a few frames and then
vanish. Sampling the last frame is what tells the two apart.
"""
import os
import shutil
import subprocess
import tempfile
import unittest

from app.services import media_service, meme_builder_service


def _have(binary: str) -> bool:
    return bool(shutil.which(binary))


def _pillow_ok() -> bool:
    try:
        from PIL import Image
        Image.new("RGB", (8, 8)).save(os.path.join(tempfile.gettempdir(), "_pc_probe.avif"))
        return True
    except Exception:
        return False


class TestLoopableStill(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="pctest-still-")
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def _still(self, ext: str, fmt: str = None):
        from PIL import Image
        try:
            import pillow_heif
            pillow_heif.register_heif_opener()
        except Exception:
            pass
        p = os.path.join(self.tmp, f"in{ext}")
        Image.new("RGB", (64, 48), (10, 20, 220)).save(p, fmt)
        return p

    def test_ordinary_formats_are_passed_through_untouched(self):
        """The conversion must be reserved for the formats that need it — re-encoding every PNG/JPEG
        layer would cost a decode per layer and silently drop a GIF's animation."""
        for ext, fmt in ((".png", "PNG"), (".jpg", "JPEG"), (".gif", "GIF"), (".webp", "WEBP")):
            p = self._still(ext, fmt)
            self.assertEqual(media_service.loopable_still(p), p, ext)

    @unittest.skipUnless(_pillow_ok(), "Pillow has no AVIF support")
    def test_isobmff_stills_become_a_real_png(self):
        for ext in (".avif", ".heic"):
            p = self._still(ext)
            out = media_service.loopable_still(p)
            self.assertNotEqual(out, p, ext)
            with open(out, "rb") as fh:
                self.assertEqual(fh.read(8), b"\x89PNG\r\n\x1a\n", f"{ext} -> not a PNG")

    def test_unreadable_source_falls_back_to_the_original_path(self):
        """A truncated/garbage .avif is the caller's error to report. Returning a path that does not
        exist would turn a clear ffmpeg failure into a confusing one."""
        p = os.path.join(self.tmp, "junk.avif")
        with open(p, "wb") as fh:
            fh.write(b"not an image")
        self.assertEqual(media_service.loopable_still(p), p)


@unittest.skipUnless(_have("ffmpeg") and _have("ffprobe") and _pillow_ok(),
                     "needs ffmpeg/ffprobe and Pillow AVIF support")
class TestMemeRenderWithAvifLayer(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="pctest-meme-")
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_avif_layer_renders_and_lasts_the_whole_slot(self):
        from PIL import Image
        src = os.path.join(self.tmp, "layer.avif")
        Image.new("RGB", (400, 300), (10, 20, 220)).save(src)

        dur = 1.0
        edit = {"w": 640, "h": 480, "fps": 20, "duration": dur, "bg": "#000000",
                "layers": [{"type": "image", "src": "u1", "x": 0, "y": 0, "w": 640, "h": 480,
                            "start": 0, "dur": dur, "fit": "cover"}]}
        mp4 = os.path.join(self.tmp, "out.mp4")
        data, ctype = meme_builder_service.render(edit, {"u1": src})
        self.assertEqual(ctype, "video/mp4")
        with open(mp4, "wb") as fh:
            fh.write(data)

        # Whole slot covered: the layer is still there on the LAST frame, not just the first.
        for t in (0.05, dur - 0.1):
            png = os.path.join(self.tmp, f"f{t}.png")
            subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", f"{t:.3f}", "-i", mp4,
                            "-frames:v", "1", png], check=True, timeout=120)
            r, g, b = Image.open(png).convert("RGB").getpixel((320, 240))
            self.assertGreater(b, 120, f"layer missing at t={t:.2f} (got {(r, g, b)})")
            self.assertLess(r, 90, f"unexpected colour at t={t:.2f} (got {(r, g, b)})")


if __name__ == "__main__":
    unittest.main()
