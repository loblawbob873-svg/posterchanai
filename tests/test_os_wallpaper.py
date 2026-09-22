"""The default desktop/OS wallpaper is the mascot's desk at 4K, and client.css shows it full-bleed.

PosterChanOS's session IS the windowed desktop (static/js/client/os.js), so one file is both the
OS wallpaper and desktop-mode's default: static/os-wallpaper-bg.webp. It must be 3840x2160 so it is
crisp on a 4K monitor, and client.css must scale it with `cover` (fills any aspect ratio, no
letterboxing). os/plymouth/generate_wallpaper.py renders it from os/plymouth/wallpaper-art.webp.

WHY THE COMPOSITION IS MEASURED HERE AND NOT EYEBALLED: os.js lays desktop icons out from the TOP
LEFT and draws their labels in white, and this artwork's top left is a paper poster and a lit
lantern — 45/255 mean and 254 peak in the source, i.e. white text on white paper. The generator
darkens that side in proportion to each pixel's own brightness, and client.css lays its `::before`
scrim over the same corner at display time. Three ways that goes wrong silently, one test each:

  * the corner ends up bright and every label loses its contrast — on the one screen where a label
    is the only thing identifying an app;
  * it is "fixed" by dimming the whole picture, which costs the wallpaper and nobody notices,
    because the corner measurement it was aimed at passes;
  * the art is fitted rather than covered, which letterboxes a 4K screen.

The readability test composites the REAL scrim parsed out of client.css, because the wallpaper and
the scrim are partners: judged alone, either one can look wrong while what a person sees is fine
(and a scrim weakened later would silently undo the wash the generator applied).
"""
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WP = ROOT / "static/os-wallpaper-bg.webp"
ART = ROOT / "os/plymouth/wallpaper-art.webp"
CSS = (ROOT / "static/css/client.css").read_text(encoding="utf-8")
ICONS = (0.0, 0.0, 0.20, 0.55)      # the slice of the desktop the icon grid is laid out into
BUSY = (0.45, 0.0, 1.0, 1.0)        # the half the picture has to survive in


def _grey(path, box=None, size=(384, 216)):
    import numpy as np
    from PIL import Image
    with Image.open(path) as im:
        g = im.convert("L").resize(size, Image.LANCZOS)
    a = np.asarray(g, dtype=float)
    if box:
        x0, y0, x1, y1 = box
        h, w = a.shape
        a = a[int(h * y0):int(h * y1), int(w * x0):int(w * x1)]
    return a


def _scrim():
    """The `.os-desk::before` gradient as (position, alpha) stops, read from the shipped CSS."""
    i = CSS.index(".os-desk::before")
    rule = CSS[i:CSS.index("}", i)]
    stops = re.findall(r"rgba\([^)]*?,\s*([\d.]+)\)\s+([\d.]+)%", rule)
    assert stops, "the readability scrim is gone from .os-desk::before"
    return [(float(pos) / 100.0, float(alpha)) for alpha, pos in stops]


def _as_seen(box):
    """The wallpaper with the scrim over it — what a person actually looks at."""
    import numpy as np
    a = _grey(WP, box)
    stops = _scrim()
    h, w = a.shape
    x0, y0, x1, y1 = box
    # a 135deg gradient: progress runs from the top-left corner to the bottom-right one
    yy, xx = np.mgrid[0:h, 0:w]
    t = ((x0 + (xx + 0.5) / w * (x1 - x0)) + (y0 + (yy + 0.5) / h * (y1 - y0))) / 2
    alpha = np.zeros_like(t)
    for (p0, a0), (p1, a1) in zip(stops, stops[1:]):
        m = (t >= p0) & (t <= p1)
        alpha[m] = a0 + (a1 - a0) * ((t[m] - p0) / (p1 - p0 or 1))
    alpha[t >= stops[-1][0]] = stops[-1][1]
    return a * (1 - alpha) + 10 * alpha      # the scrim's own colour is #0a0a0f


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
        rule = CSS[CSS.rindex(".os-desk{", 0, i):CSS.index("}", i)]
        self.assertIn("cover", rule, "the desk wallpaper must be `cover` for 4K / any aspect ratio")

    def test_something_paints_before_the_photograph_arrives(self):
        """"The background image takes a while to load" — it is a 4K photograph, so it does.

        A ~400-byte copy travels INSIDE client.css as a data: URI, costs no request, and cannot
        arrive late; the browser scales it to fill the screen until the real one lands. It must be
        generated FROM the shipped wallpaper, or it becomes a wash of the previous picture — a lie
        that only shows for the half-second nobody is watching, and the reason it is not a
        hand-pasted string. It must also be the SECOND layer, so the real picture covers it."""
        import base64
        import io

        import numpy as np
        from PIL import Image
        m = re.search(r"pc-lqip \*/url\('data:image/webp;base64,([^']+)'\)", CSS)
        self.assertTrue(m, "the instant placeholder is gone from .os-desk")
        raw = base64.b64decode(m.group(1))
        self.assertLess(len(raw), 2048, "the placeholder is meant to be a few hundred bytes")
        i = CSS.index("os-wallpaper-bg.webp")
        self.assertLess(i, m.start(), "the placeholder must be UNDER the real wallpaper, not over it")
        with Image.open(io.BytesIO(raw)) as tiny:
            self.assertEqual(tiny.format, "WEBP")
            a = np.asarray(tiny.convert("L").resize((20, 11), Image.LANCZOS), dtype=float)
        b = _grey(WP, size=(20, 11))
        self.assertLess(np.abs(a - b).mean(), 18,
                        "the placeholder is a wash of a DIFFERENT picture than the one that follows")

    def test_an_icon_label_has_something_to_stand_on(self):
        """Wallpaper + the shipped scrim, over the corner the icon grid is laid out into. Measured:
        the source art alone is 45 mean / 254 peak there — a white label on a white poster."""
        seen = _as_seen(ICONS)
        self.assertLess(seen.mean(), 40, "the icon corner is too bright for white labels")
        self.assertLess(seen.max(), 130, "a bright detail (the poster, the lantern) survived under the icons")

    def test_the_wash_is_local_and_the_picture_survives_it(self):
        """The cheap way to pass the test above is to dim the whole wallpaper, which costs the
        picture and passes silently. So the busy half must keep the source's own brightness."""
        src = _grey(ART, BUSY).mean()
        out = _grey(WP, BUSY).mean()
        self.assertGreater(out, src * 0.88, f"the whole picture was dimmed, not just the icon side ({out:.1f} vs {src:.1f})")

    def test_the_artwork_is_in_it(self):
        """A render carrying no artwork (a fill or a wash alone) is a real failure mode and looks
        like a deliberately moody wallpaper. The picture side must carry detail AND colour."""
        from PIL import Image
        self.assertGreater(_grey(WP, BUSY).std(), 25, "the picture side has no detail — the artwork is not in it")
        with Image.open(WP) as im:
            rgb = im.convert("RGB").resize((384, 216), Image.LANCZOS).crop((172, 0, 384, 216))
        sat = [max(p) - min(p) for p in rgb.getdata()]
        self.assertGreater(sum(sat) / len(sat), 25, "the picture side is nearly grey")

    def test_it_covers_rather_than_fits(self):
        """`cover` crops the overflow; `fit` would letterbox a 16:9 screen with flat bars that no
        size assertion can see — the file is still 3840x2160."""
        top, bottom = _grey(WP)[0], _grey(WP)[-1]
        self.assertGreater(top.std(), 5, "the top row is a flat bar — the art was fitted, not covered")
        self.assertGreater(bottom.std(), 5, "the bottom row is a flat bar — the art was fitted, not covered")

    def test_the_shipped_file_is_what_the_generator_makes(self):
        """The wallpaper is a build product of os/plymouth/wallpaper-art.webp. Hand-editing the webp
        works until the next regeneration silently reverts it."""
        import numpy as np
        from PIL import Image
        self.assertTrue(ART.exists(), "the source art is missing")
        with tempfile.TemporaryDirectory() as tmp:      # never into the working tree: the checks run
            out = os.path.join(tmp, "wp.webp")          # concurrently against a live deployment
            r = subprocess.run([sys.executable, str(ROOT / "os/plymouth/generate_wallpaper.py")],
                               env=dict(os.environ, PC_WALLPAPER_OUT=out), capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            with Image.open(out) as im:
                self.assertEqual(im.size, (3840, 2160))
            d = np.abs(_grey(WP, size=(96, 54)) - _grey(out, size=(96, 54)))
            self.assertLess(d.mean(), 4, "the shipped wallpaper is not what the generator renders")


if __name__ == "__main__":
    unittest.main()
