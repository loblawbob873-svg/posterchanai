"""Meme Builder: a clip's slot may be LONGER than the clip's own footage.

Run: venv-unified/bin/python -m unittest tests.test_meme_slot_longer_than_source

Reported as "I keep changing the length of all the layers but the rendered never changes to the new
length", and as "renaming the project changes the length of the clip back to what it was before".

Two halves, both of which fail SILENTLY — nothing errors, you just get the old length back:

  * the EDITOR used to rewrite `dur` back to the source length whenever the slot ran past the end of the
    footage (bindTrim's ready()), and save() it. That fired on every inspector rebuild — renaming the
    project, adding a layer, an undo — so a deliberately-typed length was reverted with no message and no
    undo entry, however many times it was retyped. Covered by the guard in meme.js; what is asserted HERE
    is the half that made the rewrite look justified:
  * the RENDERER dropped the layer at the end of its footage (overlay eof_action=pass showed the composite
    through), so the tail of a long slot was background — while the editor's PREVIEW held the last frame,
    because an HTML video element clamps to its duration. Preview and export disagreeing is the thing
    that made a long slot look broken.

A filter-string assertion cannot catch either: the command is accepted and produces a valid file of the
right length, with the wrong PIXELS in the tail. So this renders and samples them.
"""
import os
import subprocess
import tempfile
import unittest

from PIL import Image

from app.services import meme_builder_service as mb


def _pixel(path, at, xy=(160, 120)):
    """The colour at time `at` of a rendered video."""
    d = os.path.dirname(path)
    frame = os.path.join(d, f"probe_{at}.png")
    subprocess.run([mb.media_service.resolve_ffmpeg(), "-y", "-v", "error", "-ss", str(at),
                    "-i", path, "-frames:v", "1", frame], check=True, capture_output=True, timeout=60)
    try:
        return Image.open(frame).convert("RGB").getpixel(xy)
    finally:
        try:
            os.unlink(frame)
        except OSError:
            pass


def _near(got, want, tol=24):
    """Colour comparison with room for the h264 round-trip."""
    return all(abs(a - b) <= tol for a, b in zip(got, want))


class SlotLongerThanSource(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not mb.media_service.resolve_ffmpeg():
            raise unittest.SkipTest("ffmpeg is not available on this node")

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="pcmemeslot-")
        # RED for the first second, BLUE for the second. Two colours so the held tail is identifiable as
        # "the last frame of the source" rather than merely "not the background".
        self.src = os.path.join(self.tmp, "src.mp4")
        ff = mb.media_service.resolve_ffmpeg()
        subprocess.run([ff, "-y", "-v", "error",
                        "-f", "lavfi", "-i", "color=c=red:s=320x240:r=30:d=1",
                        "-f", "lavfi", "-i", "color=c=blue:s=320x240:r=30:d=1",
                        "-filter_complex", "[0:v][1:v]concat=n=2:v=1[v]", "-map", "[v]",
                        "-c:v", "libx264", "-pix_fmt", "yuv420p", self.src],
                       check=True, capture_output=True, timeout=60)

    def tearDown(self):
        for f in os.listdir(self.tmp):
            try:
                os.unlink(os.path.join(self.tmp, f))
            except OSError:
                pass
        os.rmdir(self.tmp)

    def _render(self, dur):
        edit = {"w": 320, "h": 240, "fps": 30, "bg": "#00ff00", "duration": dur, "fmt": "mp4",
                "layers": [{"type": "video", "src": "S", "start": 0, "dur": dur, "trim": 0,
                            "x": 0, "y": 0, "w": 320, "h": 240, "opacity": 1,
                            "effect": "none", "fit": "cover", "speed": 1}]}
        out, _mime = mb.render(edit, {"S": self.src})
        p = os.path.join(self.tmp, "out.mp4")
        with open(p, "wb") as fh:
            fh.write(out)
        return p

    def test_source_duration_probe(self):
        self.assertAlmostEqual(mb._source_duration(self.src), 2.0, delta=0.15)
        # Unprobeable must mean "assume it fits" — a probe failure may never change a working render.
        self.assertEqual(mb._source_duration(os.path.join(self.tmp, "nope.mp4")), 0.0)

    def test_long_slot_keeps_its_length_and_holds_the_last_frame(self):
        """A 2s clip in a 6s slot: 6s out, and the tail is the clip's last frame — not the background."""
        out = self._render(6.0)
        probed = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                                 "-of", "default=nw=1:nk=1", out],
                                capture_output=True, text=True, timeout=30)
        self.assertAlmostEqual(float(probed.stdout.strip()), 6.0, delta=0.25)

        self.assertTrue(_near(_pixel(out, 0.5), (255, 0, 0)), "the clip's own first second should play")
        self.assertTrue(_near(_pixel(out, 1.5), (0, 0, 255)), "the clip's own second second should play")
        # The regression. Before the fix this sampled the GREEN project background, because the layer
        # simply ended and overlay=eof_action=pass let the composite show through for 4 seconds.
        tail = _pixel(out, 5.0)
        self.assertFalse(_near(tail, (0, 255, 0), tol=60),
                         f"the tail of the slot fell through to the background ({tail})")
        self.assertTrue(_near(tail, (0, 0, 255)),
                        f"the tail should hold the clip's last frame, got {tail}")

    def test_a_slot_that_fits_is_untouched(self):
        """The padding must not fire when the footage covers the slot — no tpad, no held frames."""
        out = self._render(1.5)
        self.assertTrue(_near(_pixel(out, 0.5), (255, 0, 0)))
        self.assertTrue(_near(_pixel(out, 1.2), (0, 0, 255)))


if __name__ == "__main__":
    unittest.main()
