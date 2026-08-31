"""macOS app tiles are coloured by the APP, never by where it sits in the Dock.

The macOS desktop had a menu bar, a Dock and traffic lights, and then drew the same flat sprite
glyphs the PosterChan desktop uses — "I don't see macOS icons" was a fair description of it.

The tiles are real app artwork now, and the colour comes from `appHue(key)` over the app's durable
key. What that replaces is `:nth-child(4n+2|3|4)`: a four-colour rainbow cycled by POSITION, so
opening one window re-coloured every tile after it and one app was blue, then purple, then orange,
on a single desktop. Colour is how a person finds an app in a Dock; an identity that changes when a
neighbour opens is not an identity.
"""
import json
import re
import subprocess
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
RUNTIME = HERE / "mac_app_tint_runtime.mjs"
CSS = (ROOT / "static" / "css" / "client.css").read_text(encoding="utf-8")
OS_JS = (ROOT / "static" / "js" / "client" / "os.js").read_text(encoding="utf-8")


class AppColourIsTheAppsOwn(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        out = subprocess.run(["node", str(RUNTIME)], capture_output=True, text=True, timeout=120)
        if out.returncode:
            raise AssertionError("appHue runtime failed: " + (out.stderr or "")[-2000:])
        cls.got = json.loads(out.stdout.strip().splitlines()[-1])

    def test_the_same_app_always_gets_the_same_colour(self):
        self.assertTrue(self.got["stable"], "appHue is not a pure function of the key")
        self.assertTrue(self.got["inRange"],
                        "a hue outside 0-359 lands outside the colour wheel: %s" % self.got["hues"])

    def test_short_names_do_not_collapse_onto_each_other(self):
        self.assertTrue(self.got["anagram"],
                        "'notes' and 'stone' share a colour — that is the signature of a char-code "
                        "SUM, and a launcher is full of short names")
    def test_no_two_colours_are_almost_the_same(self):
        """SEPARATION, not uniqueness — and this is the assertion that changed after looking at a
        real desktop.

        The first version hashed to a raw degree in 0..359 and asserted the hues were nearly all
        distinct, which they were. On the actual machine seventeen of thirty-seven apps still landed
        between 175 and 260: every value different, one indistinguishable blue band, reported as
        "the desktop icons look the same". Adjacent hues are not different colours to a person
        scanning a grid, so near-uniqueness was measuring the wrong thing.

        The hash picks a slot in a ring of well-separated hues now. Two apps therefore share a
        colour or differ visibly, never sit 4 degrees apart — and repeats are correct: macOS has
        several blue apps too."""
        used = sorted(set(self.got["hues"].values()))
        close = [(a, b) for i, a in enumerate(used) for b in used[i + 1:] if abs(a - b) < 18]
        self.assertEqual([], close,
                         "these hues are too close to tell apart: %s" % close)

    def test_no_single_colour_swallows_the_desktop(self):
        counts = {}
        for h in self.got["hues"].values():
            counts[h] = counts.get(h, 0) + 1
        worst = max(counts.values())
        self.assertLessEqual(worst, max(4, self.got["total"] // 4),
                             "%d of %d apps share one colour (%s) — the ring is not spreading them"
                             % (worst, self.got["total"], counts))


class DockDoesNotColourByPosition(unittest.TestCase):
    """Source-only, and deliberately in its own class with NO setUpClass.

    Sharing the runtime's fixture meant that against pre-fix code every test here died at
    extraction — "appHue moved" — which is a harness complaint, not the finding. These are the
    assertions that actually catch the old stylesheet, so they must be able to run without it."""

    def test_the_dock_no_longer_colours_by_position(self):
        mac = CSS[CSS.index(".os-root.os-style-mac{"):]
        # Report the MATCH, never the haystack: assertNotRegex prints the whole subject, and the
        # subject here is the rest of a 9,000-line stylesheet — 676KB into a CI log for a one-line
        # finding.
        hits = re.findall(r"\.os-task:nth-child\([^)]*\)>\.ic", mac)
        self.assertEqual([], hits,
                         "the Dock still tints tiles by position (%s) — opening a window re-colours "
                         "every tile after it, and the same app changes colour depending on its "
                         "neighbours" % ", ".join(hits))

    def test_every_tile_surface_takes_the_apps_hue(self):
        """Desktop, start menu and Dock must read ONE value, or the same app is three colours."""
        self.assertGreaterEqual(CSS.count("var(--app-h"), 2,
                                "the tiles no longer read --app-h")
        for selector in (".os-root.os-style-mac .os-icon>.ic",
                         ".os-root.os-style-mac .os-task>.ic"):
            self.assertIn(selector, CSS, "%s lost its app tile" % selector)

    def test_the_hue_is_stamped_from_a_durable_key_not_a_window_id(self):
        """A window id is new on every reopen and a title carries the open document; either would
        recolour the app as you use it, which is the bug this replaced."""
        self.assertIn("const tint = (key) =>", OS_JS, "the tint stamp is gone")
        self.assertIn("${tint(a.view)}", OS_JS, "desktop icons are no longer tinted by their view")
        self.assertNotIn("tint(w.id)", OS_JS,
                         "a Dock tile is tinted by its WINDOW id — a fresh one on every reopen, so "
                         "the app changes colour each time it is closed and opened")


if __name__ == "__main__":
    unittest.main()
