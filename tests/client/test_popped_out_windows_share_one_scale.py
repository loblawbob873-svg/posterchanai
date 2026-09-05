"""EVERY POPPED-OUT WINDOW ON ONE SCREEN MUST RENDER AT ONE SCALE.

The stylesheet picks its scale from the VIEWPORT WIDTH -- .67 under 1366, .72 under 1600, .77 under
1920, then 1. That is right for a browser window on somebody's laptop and wrong for a frame the
desktop just placed on a 4K monitor: each popped-out window is its own viewport, so each landed in a
different tier purely because of the size `place()` chose for it.

MEASURED on the real desk, three frames on one screen:

    global      2058px wide   ->  zoom 1
    messages    1728px wide   ->  zoom .77
    News        ~1100px wide  ->  zoom .67

Reported as "not every window is sized and displayed right ... global has better zoom and wider
window decoration compared to Notifications and News".

The scale now comes from the desktop that opened the window -- its COMPUTED body zoom, so a 1366px
laptop whose desktop is at .67 does not get full-size windows, which is the same inconsistency with
the sign flipped.
"""
from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[2]
CSS = (ROOT / "static/css/client.css").read_text(encoding="utf-8")
OSWIN = (ROOT / "static/js/client/oswin.js").read_text(encoding="utf-8")


def _rule(selector):
    m = re.search(re.escape(selector) + r"\s*\{([^}]*)\}", CSS)
    return m.group(1) if m else None


class TestTheWindowIgnoresItsOwnWidth(unittest.TestCase):
    def test_a_popped_out_window_has_its_own_scale_rule(self):
        body = _rule("html.pc-oswin body")
        self.assertIsNotNone(body, "nothing overrides the width tiers for a popped-out window")
        self.assertIn("zoom:var(--ui-scale", body)
        self.assertIn("--zf:var(--ui-scale", body)

    def test_zoom_and_zf_agree(self):
        """They are one fact. A page whose containers are sized `calc(100dvh / var(--zf))` while
        `zoom` says something else is taller than its own window."""
        body = _rule("html.pc-oswin body")
        zoom = re.search(r"zoom:var\(--ui-scale,\s*([\d.]+)\)", body)
        zf = re.search(r"--zf:var\(--ui-scale,\s*([\d.]+)\)", body)
        self.assertIsNotNone(zoom, body)
        self.assertIsNotNone(zf, body)
        self.assertEqual(zoom.group(1), zf.group(1))

    def test_the_app_height_is_divided_by_the_same_factor(self):
        app = _rule("html.pc-oswin .app")
        self.assertIsNotNone(app)
        self.assertIn("calc(100dvh / var(--ui-scale", app)

    def test_it_comes_after_every_width_tier_or_it_loses(self):
        mine = CSS.index("html.pc-oswin body{ zoom:")
        for tier in ("@media (min-width:821px) and (max-width:1366px)",
                     "@media (min-width:1921px)",
                     "@media (min-width:3500px) and (min-height:1800px)"):
            self.assertLess(CSS.index(tier), mine,
                            f"the popped-out rule is declared before {tier} and cannot win")


class TestTheScaleComesFromTheDesktop(unittest.TestCase):
    def test_adopt_reads_the_opener(self):
        body = OSWIN[OSWIN.index("  function adopt(){"):]
        body = body[: body.index("\n  function installChrome")]
        self.assertIn("--ui-scale", body, "the window never learns what the desktop renders at")
        self.assertIn("getComputedStyle", body)

    def test_it_prefers_the_measured_zoom_over_the_configured_setting(self):
        """On a 1366px laptop the desktop itself is at .67; taking the SETTING would make windows
        the only thing on that screen at full size."""
        body = OSWIN[OSWIN.index("  function adopt(){"):]
        body = body[: body.index("\n  function installChrome")]
        self.assertLess(body.index("getComputedStyle"), body.index("uiScaleEffective"),
                        "the configured setting is consulted before the measured zoom")

    def test_an_unreadable_opener_changes_nothing(self):
        """No desktop said otherwise, so the stylesheet's own tiers decide exactly as before."""
        body = OSWIN[OSWIN.index("  function adopt(){"):]
        body = body[: body.index("\n  function installChrome")]
        guard = re.search(r"if\s*\(\s*z\s*>\s*0.*?\)\s*\n?\s*root\.document", body, re.S)
        self.assertIsNotNone(guard,
                             "a zero or unreadable scale is still written to the window")
        self.assertIn("isFinite", guard.group(0),
                      "a NaN from an unreadable opener would be written as a scale")


if __name__ == "__main__":
    unittest.main()
