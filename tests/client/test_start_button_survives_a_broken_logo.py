"""The start button exists even when its image does not.

    "PosterChan logo now missing from start button"  — then, twice later,
    "ah posterchan icon missing from start button again!"

The mark is the INSTANCE's logo, read live off `.brand-logo` so a deployment with a custom one gets
its own. That means the src can be an instance URL, and an instance that is slow, unreachable over
Tor, down, or not configured yet leaves a broken image where the start button should be — nothing to
click and nothing on screen to say why. I dismissed the first report as the image not having loaded
during a restart; it came back, so it was not.

Two fallbacks. The bundled PNG is not a second opinion about which logo is right — it is the one that
cannot 404, because it ships inside the app. The sprite flower after it needs no network and no file
at all, which covers a bundle built before build-www.sh copied the images (a real state: that copy
was missing once already).
"""
import unittest
from pathlib import Path

from tests.client.test_native_window_follows_its_frame import body, strip_comments

ROOT = Path(__file__).resolve().parents[2]
OS_JS = ROOT / "static/js/client/os.js"
SPRITE = ROOT / "static/js/client/sprite.js"
CSS = ROOT / "static/css/client.css"


class TheButtonOutlivesItsImage(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.raw = OS_JS.read_text()
        cls.src = strip_comments(cls.raw)

    def test_the_guard_is_installed_when_the_bar_is_drawn(self):
        # Raw, not stripped: strip_comments blanks string literals, and this call is mostly one.
        self.assertIn("_keepStartVisible($('#os-start img', bar))", self.raw)

    def test_it_falls_back_to_the_bundled_file(self):
        fn = body(self.raw, "function _keepStartVisible")
        self.assertIn("/static/posterchan-relay.png", fn)
        self.assertIn("addEventListener('error'", fn)

    def test_and_then_to_something_that_needs_no_file(self):
        self.assertIn("function _startGlyph", self.src)
        self.assertIn("iconSvg('flower')", body(self.raw, "function _startGlyph"))

    def test_the_glyph_it_draws_exists_in_the_sprite(self):
        """An icon named but not defined renders as blank space — which is the bug this fixes,
        one layer down."""
        self.assertIn('id="i-flower"', SPRITE.read_text())

    def test_each_step_fires_once(self):
        """A fallback that also fails must not re-enter its own handler for ever."""
        fn = body(self.src, "function _keepStartVisible")
        self.assertGreaterEqual(fn.count("once: true"), 2)

    def test_an_image_already_broken_is_caught(self):
        """An <img> that failed BEFORE the listener was bound fires no further event; `complete`
        with a zero natural width is the only way to see it."""
        fn = body(self.src, "function _keepStartVisible")
        self.assertIn("img.complete", fn)
        self.assertIn("naturalWidth", fn)

    def test_it_does_not_re_bind_on_every_repaint(self):
        """drawBar runs on a timer; a listener per draw is a leak."""
        fn = body(self.src, "function _keepStartVisible")
        self.assertIn("dataset.guarded", fn)

    def test_the_replacement_is_sized(self):
        """So the taskbar does not reflow at the moment the logo fails."""
        css = CSS.read_text()
        self.assertIn(".os-start-ic{", css)
        i = css.index(".os-start-ic{")
        self.assertIn("width", css[i:i + 200])


if __name__ == "__main__":
    unittest.main()
