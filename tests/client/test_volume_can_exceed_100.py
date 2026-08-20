"""The volume sliders reach where the backend already allowed, and stop exactly there.

    "volume mixer needs that louder thing where you can go past 100 percent"

100% is the loudest the hardware is being ASKED for, not the loudest it can be; above it PipeWire
scales the samples in software, which is what a quiet recording or a laptop speaker actually needs.
`desktop/audio.js` has allowed it all along — `MAX = 1.5` — so the ceiling was three copies of the
number 100 in the markup rather than a decision anybody made.

Two things are checked, and the second is the one that bites: the sliders go to 150, AND they agree
with `audio.js`. A slider that asks for more than `clamp` allows lands somewhere other than where it
was dropped, which is worse than one that stops. Brightness must NOT follow — 100% is the panel at
full power and there is nothing above it.
"""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SHELL = ROOT / "static" / "js" / "client" / "osshell.js"
AUDIO = ROOT / "desktop" / "audio.js"
CSS = ROOT / "static" / "css" / "client.css"


class TheSlidersReachTheBackendsCeiling(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.src = SHELL.read_text()

    def test_the_ui_ceiling_matches_audio_js(self):
        backend = re.search(r"const MAX\s*=\s*([\d.]+)\s*;", AUDIO.read_text())
        self.assertIsNotNone(backend, "audio.js no longer declares MAX — re-read this test")
        ui = re.search(r"var VOL_MAX\s*=\s*(\d+)\s*;", self.src)
        self.assertIsNotNone(ui, "osshell.js no longer declares VOL_MAX")
        self.assertEqual(int(ui.group(1)), round(float(backend.group(1)) * 100),
                         "the slider and the clamp disagree, so the knob lands somewhere other "
                         "than where it was dropped")

    def test_every_volume_slider_uses_it(self):
        """Three of them: quick settings, the volume popover, and each mixer stream. One left at
        100 is a mixer where the master boosts and an application cannot."""
        vol = [m for m in re.findall(r'<input class="os-qs-range[^"]*"[^>]*?(?:data-qs="vol"|data-mixvol)[^>]*?max="([^"]+)"', self.src)]
        self.assertEqual(len(vol), 2, "expected the quick-settings and mixer sliders")
        for m in vol:
            self.assertEqual(m, "${VOL_MAX}")
        self.assertIn("a.setVolume(n, 'sink'), VOL_MAX)", self.src,
                      "the volume popover does not ask for the boosted range")

    def test_brightness_is_left_alone(self):
        """There is nothing above a panel at full power."""
        self.assertIn('data-qs="bright" type="range" min="1" max="100"', self.src)
        m = re.search(r"sliderPop\(anchor, 'Brightness'[^;]*;", self.src, re.S)
        self.assertIsNotNone(m)
        self.assertNotIn("VOL_MAX", m.group(0))

    def test_a_slider_without_a_max_still_stops_at_100(self):
        """`sliderPop`'s parameter is optional; a caller that forgets it must not silently boost."""
        self.assertIn("const top = max || 100;", self.src)


class ThereIsAMarkAtTheSafeCeiling(unittest.TestCase):
    def test_the_boostable_class_is_styled(self):
        css = CSS.read_text()
        self.assertIn(".os-boostable{", css,
                      "a slider that goes to 150 with no mark makes 'loud' and 'boosted' the same "
                      "gesture — there is no way to feel where the hardware ends")

    def test_the_mark_sits_where_100_actually_is(self):
        """100/150 = 66.667%. A mark drawn at 50% would be a confident lie."""
        css = CSS.read_text()
        i = css.index(".os-boostable{")
        self.assertIn("66.667%", css[i:i + 700])

    def test_vendor_pseudo_elements_are_not_in_one_selector_list(self):
        """An unknown pseudo-element poisons the ENTIRE selector list, so
        `::-webkit-slider-runnable-track,::-moz-range-track` applies in neither engine and the native
        track paints over the mark. Invalid CSS fails silently — nothing logs it."""
        css = CSS.read_text()
        for line in css.splitlines():
            if "-webkit-slider-runnable-track" in line:
                with self.subTest(line=line.strip()[:60]):
                    self.assertNotIn("-moz-", line)

    def test_the_mark_cannot_swallow_a_drag(self):
        """Decoration painted over a control has to be inert."""
        css = CSS.read_text()
        i = css.index(".os-boostable{")
        self.assertIn("background-image", css[i:i + 700])
        self.assertNotIn("position:absolute", css[i:i + 700])


if __name__ == "__main__":
    unittest.main()
