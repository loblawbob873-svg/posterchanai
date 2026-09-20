"""The default desktop/OS wallpaper is the boot-splash mascot at 4K, and client.css shows it full-bleed.

PosterChanOS's session IS the windowed desktop (static/js/client/os.js), so one file is both the
OS wallpaper and desktop-mode's default: static/os-wallpaper-bg.webp. It must be 3840x2160 so it is
crisp on a 4K monitor, and client.css must scale it with `cover` (fills any aspect ratio, no
letterboxing). os/plymouth/generate_wallpaper.py regenerates it deterministically in size.
"""
import os
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WP = ROOT / "static/os-wallpaper-bg.webp"
CSS = (ROOT / "static/css/client.css").read_text(encoding="utf-8")


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
