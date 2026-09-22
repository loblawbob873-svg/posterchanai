"""The default desktop/OS wallpaper is the mascot's desk at 4K, and client.css shows it full-bleed.

PosterChanOS's session IS the windowed desktop (static/js/client/os.js), so one file is both the
OS wallpaper and desktop-mode's default: static/os-wallpaper-bg.webp. It must be 3840x2160 so it is
crisp on a 4K monitor, and client.css must scale it with `cover` (fills any aspect ratio, no
letterboxing). os/plymouth/generate_wallpaper.py regenerates it deterministically in size.

The picture itself is square and the screen is not, so the composition is load-bearing rather than
decorative and is measured here, not eyeballed: the ICON CORNER must stay dark (os.js lays desktop
icons out from the top left, and a label is white text with a shadow — over a bright out-of-focus
neon sign it is unreadable, which is exactly what a centred or left-set picture produces), the
artwork must actually be in the file (a blur-only render is a plausible bug and looks like a
deliberately moody wallpaper), and the join between the sharp picture and its blurred extension must
not read as a vertical seam.
"""
import os
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WP = ROOT / "static/os-wallpaper-bg.webp"
CSS = (ROOT / "static/css/client.css").read_text(encoding="utf-8")


def _luma(box=None, size=(384, 216)):
    from PIL import Image
    with Image.open(WP) as im:
        g = im.convert("L").resize(size, Image.LANCZOS)
    return g.crop(box) if box else g


def _cols(g):
    w, h = g.size
    return [sum(g.crop((x, 0, x + 1, h)).getdata()) / h for x in range(w)]


class TestWallpaper(unittest.TestCase):
    def test_the_wallpaper_is_4k(self):
        from PIL import Image
        self.assertTrue(WP.exists(), "the default wallpaper is missing")
        with Image.open(WP) as im:
            self.assertEqual(im.format, "WEBP")
            self.assertEqual(im.size, (3840, 2160), "the wallpaper must be 4K so it is crisp on a 4K monitor")

    def test_css_shows_it_full_bleed_and_cover(self):
        self.assertIn("/static/os-wallpaper-bg.webp", CSS, "client.css no longer references the wallpaper")
        # cover (not a fixed vmin emblem) is what makes it fill a 4K screen and every aspect ratio.
        i = CSS.index("os-wallpaper-bg.webp")
        rule = CSS[CSS.rindex(".os-desk{", 0, i):i + 40]
        self.assertIn("cover", rule, "the desk wallpaper must be `cover` for 4K / any aspect ratio")

    def test_the_icon_corner_stays_dark_enough_for_labels(self):
        """os.js draws icons from the top left and their labels are white; the composition puts the
        dimmed, blurred side of the picture there on purpose. Measured here so moving the artwork
        (or brightening the ambient fill) cannot quietly cost every label its contrast."""
        corner = _luma((0, 0, 77, 118))          # the left 20% x top 55% — where the icon grid lives
        px = list(corner.getdata())
        self.assertLess(sum(px) / len(px), 45, "the icon corner is too bright for white labels")
        self.assertLess(max(px), 120, "a bright detail landed under the icon grid")

    def test_the_artwork_is_in_it_and_on_the_busy_side(self):
        """A blur-only render (art missing, ambient fill alone) is a real failure mode and looks
        intentional. The right-hand side must carry both detail and colour."""
        from PIL import Image
        right = _luma((172, 0, 384, 216))
        px = list(right.getdata())
        mean = sum(px) / len(px)
        sd = (sum((v - mean) ** 2 for v in px) / len(px)) ** 0.5
        self.assertGreater(sd, 25, "the right-hand side has no detail — the artwork is not in it")
        with Image.open(WP) as im:
            rgb = im.convert("RGB").resize((384, 216), Image.LANCZOS).crop((172, 0, 384, 216))
        sat = [max(p) - min(p) for p in rgb.getdata()]
        self.assertGreater(sum(sat) / len(sat), 25, "the picture side is nearly grey")

    def test_there_is_no_visible_seam(self):
        """The sharp picture fades into its own blur over FEATHER px. If that fade is dropped (or
        narrowed) the join is a vertical line down the desktop — obvious to a person and invisible
        to every other assertion here. Column MEANS do not see it (a 2160px average of two views of
        the same scene barely differs across the join), so this measures the per-column mean
        horizontal STEP and holds the join to the picture's own detail: a seam is a step sharper
        than anything the artwork contains. Measured: 3.0 as shipped, 11.0 with the fade removed."""
        import numpy as np
        from PIL import Image
        with Image.open(WP) as im:
            a = np.asarray(im.convert("L"), dtype=float)
        step = np.abs(np.diff(a, axis=1)).mean(axis=0)
        w = a.shape[1]
        joint = step[int(w * 0.22):int(w * 0.42)].max()     # the band the fade lives in
        detail = float(np.median(step[int(w * 0.45):]))     # the artwork's own edges, for scale
        self.assertLess(joint, 2 * detail,
                        "a vertical step where the picture meets its blurred extension")

    def test_the_shipped_file_is_what_the_generator_makes(self):
        """The wallpaper is a build product of os/plymouth/wallpaper-art.webp. Hand-editing the webp
        works until the next regeneration silently reverts it."""
        from PIL import Image, ImageChops
        self.assertTrue((ROOT / "os/plymouth/wallpaper-art.webp").exists(), "the source art is missing")
        out = ROOT / "tests" / "_wp_match.webp"
        try:
            env = dict(os.environ, PC_WALLPAPER_OUT=str(out))
            r = subprocess.run(["python", str(ROOT / "os/plymouth/generate_wallpaper.py")],
                               env=env, capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            with Image.open(WP) as a, Image.open(out) as b:
                a = a.convert("L").resize((96, 54), Image.LANCZOS)
                b = b.convert("L").resize((96, 54), Image.LANCZOS)
            d = list(ImageChops.difference(a, b).getdata())
            self.assertLess(sum(d) / len(d), 4, "the shipped wallpaper is not what the generator renders")
        finally:
            out.unlink(missing_ok=True)

    def test_generator_reproduces_a_4k_image(self):
        from PIL import Image
        out = ROOT / "tests" / "_wp_probe.webp"
        try:
            env = dict(os.environ, PC_WALLPAPER_OUT=str(out))
            r = subprocess.run(["python", str(ROOT / "os/plymouth/generate_wallpaper.py")],
                               env=env, capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            with Image.open(out) as im:
                self.assertEqual(im.size, (3840, 2160))
        finally:
            out.unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
