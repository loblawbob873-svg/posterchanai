"""Meme Builder captions: the background box, the drop shadow, and one drawtext PER LINE.

Run: venv-unified/bin/python -m unittest tests.test_meme_caption_box

Why these are asserted on the FILTER STRING and not only on "the render succeeded": drawtext accepts a
misspelled option by failing the whole graph, and it accepts a *valid* option that does nothing visible
(a box with alpha 0) silently. The exact tokens are the contract between the client's preview — which
draws one inline-block span per line with a box-shadow, deliberately mirroring boxborderw — and what
ffmpeg actually paints. If `box=1` stops being emitted per line, the preview and the export disagree
and nothing else in the pipeline notices.

The multi-line assertions matter because the CLIENT now word-wraps captions (drawtext never wraps) and
sends the wrapped text down with newlines in it. That path used to be reachable only by typing a
newline by hand, so it was effectively untested; it is now the normal case for any long caption.
"""
import os
import subprocess
import tempfile
import unittest

from app.services import meme_builder_service as mb


class TestDrawtextBoxAndShadow(unittest.TestCase):
    def test_no_box_or_shadow_by_default(self):
        """A plain caption must not gain a box: `box=1` with the default black would black out the
        frame behind every line of every meme made before this option existed."""
        f = mb._drawtext({"text": "hi", "size": 60, "start": 0, "dur": 2}, 720, 1280)
        self.assertNotIn("box=1", f)
        self.assertNotIn("shadowx", f)

    def test_box_emits_colour_alpha_and_border(self):
        f = mb._drawtext({"text": "hi", "size": 60, "start": 0, "dur": 2,
                          "box": True, "boxColor": "#ffffff", "boxAlpha": 1}, 720, 1280)
        self.assertIn("box=1", f)
        self.assertIn("boxcolor=#ffffff@1.00", f)
        self.assertIn("boxborderw=12", f)          # size // 5

    def test_box_colour_is_validated_not_trusted(self):
        """The colour lands in an ffmpeg command line, so a non-hex value must fall back rather than
        travel — same rule as every other colour in this service."""
        f = mb._drawtext({"text": "hi", "size": 60, "start": 0, "dur": 2,
                          "box": True, "boxColor": "red;rm -rf /", "boxAlpha": 0.5}, 720, 1280)
        self.assertIn("boxcolor=black@0.50", f)
        self.assertNotIn("rm -rf", f)

    def test_box_alpha_is_clamped(self):
        f = mb._drawtext({"text": "hi", "size": 60, "start": 0, "dur": 2,
                          "box": True, "boxAlpha": 9}, 720, 1280)
        self.assertIn("@1.00", f)

    def test_shadow_offsets_scale_with_the_font(self):
        big = mb._drawtext({"text": "hi", "size": 180, "start": 0, "dur": 2, "shadow": True}, 720, 1280)
        self.assertIn("shadowx=10", big)           # 180 // 18
        self.assertIn("shadowy=10", big)
        self.assertIn("shadowcolor=black@0.65", big)
        # …and never smaller than a visible offset, however small the caption
        tiny = mb._drawtext({"text": "hi", "size": 9, "start": 0, "dur": 2, "shadow": True}, 720, 1280)
        self.assertIn("shadowx=2", tiny)

    def test_centre_flag_still_wins_over_a_measured_cx(self):
        """Adding options must not disturb the x expression — ffmpeg centring is what makes a centred
        caption exact, and it has to beat the client's measured centre when both are present."""
        f = mb._drawtext({"text": "hi", "size": 60, "start": 0, "dur": 2,
                          "align": "center", "cx": 123, "box": True}, 720, 1280)
        self.assertIn("x=(w-text_w)/2", f)


class TestWrappedCaptionRenders(unittest.TestCase):
    """A wrapped caption (what the client now sends) renders, with one drawtext per line."""

    @classmethod
    def setUpClass(cls):
        if not mb.media_service.resolve_ffmpeg():
            raise unittest.SkipTest("ffmpeg is not available on this node")

    def test_three_line_boxed_caption_renders(self):
        from PIL import Image
        tmp = tempfile.mkdtemp(prefix="pcmemetest-")
        try:
            src = os.path.join(tmp, "base.jpg")
            Image.new("RGB", (600, 400), (20, 90, 40)).save(src)
            edit = {
                "w": 480, "h": 640, "fps": 12, "bg": "#000000", "duration": 1.0,
                "layers": [
                    {"type": "image", "src": "S", "start": 0, "dur": 1,
                     "x": 0, "y": 0, "w": 480, "h": 640, "opacity": 1, "fit": "cover"},
                    # newlines are how the wrapped text arrives from the client
                    {"type": "text", "text": "line one\nline two\nline three",
                     "start": 0, "dur": 1, "x": 20, "y": 40, "size": 40,
                     "color": "#ffffff", "stroke": "#000000", "align": "center",
                     "box": True, "boxColor": "#ff0000", "boxAlpha": 0.6, "shadow": True},
                ],
            }
            data = mb.render(edit, {"S": src})
            self.assertGreater(len(data), 1000)
            out = os.path.join(tmp, "out.mp4")
            with open(out, "wb") as fh:
                fh.write(data)
            p = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0",
                                "-show_entries", "stream=width,height", "-of", "csv=p=0", out],
                               capture_output=True, text=True, timeout=30)
            self.assertIn("480,640", (p.stdout or "").strip())
        finally:
            for f in os.listdir(tmp):
                try:
                    os.unlink(os.path.join(tmp, f))
                except OSError:
                    pass
            os.rmdir(tmp)

    def test_a_blank_line_still_consumes_a_line_height(self):
        """"a\n\nb" must put b two lines down, not one — the spacing the user typed is content."""
        lines = "a\n\nb".split("\n")
        dys = [mb._drawtext({"text": ln, "size": 50, "start": 0, "dur": 1, "_line_dy": i}, 480, 640)
               for i, ln in enumerate(lines) if ln.strip()]
        self.assertEqual(len(dys), 2)
        self.assertIn("y=0", dys[0])
        self.assertIn("y=118", dys[1])          # 2 * round(50 * 1.18)


if __name__ == "__main__":
    unittest.main()
