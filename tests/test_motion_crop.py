"""A zoom-IN motion crops the frame, and overlays baked in BEFORE it are what get sliced.

At zoom z a centred zoompan keeps 1/z of the frame, losing (1 - 1/z) split evenly between the two
edges. `pulse` ran 1.00..1.24 — 9.7% off every edge at each peak — while a corner character is
composited at a 2.5-3% margin and captions sit near the edges too. Effects that BAKE those overlays
into their own frames (character.py's composites, text.py's captions) therefore had them clipped
twice a second, whereas the SAME overlays applied after the modifier were untouched. That asymmetry
is why it looked intermittent.

This pins the budget rather than the exact expression: the filter may be retuned, but not back to a
peak that eats a corner overlay's margin.
"""
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# The margin a corner character is drawn at (media_service.overlay_corner_character / character.py).
CHARACTER_MARGIN_FRAC = 0.025


def _peak_zoom(expr: str) -> float:
    """Peak of `a+b*sin(...)` — the most zoomed-in the pulse ever gets."""
    m = re.match(r"([0-9.]+)\+([0-9.]+)\*sin", expr)
    assert m, f"unrecognised pulse expression: {expr}"
    return float(m.group(1)) + float(m.group(2))


class PulseCrop(unittest.TestCase):
    def _expr(self):
        with open(os.path.join(ROOT, "app/services/media_service.py"), encoding="utf-8") as f:
            src = f.read()
        body = src.split("def _pulse_vf_video")[1].split("\ndef ")[0]
        m = re.search(r'z = "([^"]+)"', body)
        self.assertIsNotNone(m, "could not find the pulse zoom expression")
        return m.group(1)

    def test_pulse_never_zooms_out_below_the_frame(self):
        """The trough must be >= 1.0 or the frame shows background at the bottom of each beat."""
        e = self._expr()
        m = re.match(r"([0-9.]+)\+([0-9.]+)\*sin", e)
        self.assertGreaterEqual(round(float(m.group(1)) - float(m.group(2)), 6), 1.0)

    def test_peak_crop_stays_under_a_corner_overlay_margin(self):
        """The whole bug: a peak that crops MORE than the margin an overlay is drawn at will eat it."""
        peak = _peak_zoom(self._expr())
        per_edge = (1.0 - 1.0 / peak) / 2.0
        self.assertLess(per_edge, 0.06,
                        f"peak zoom {peak} crops {per_edge*100:.1f}% an edge; a corner overlay lives "
                        f"at {CHARACTER_MARGIN_FRAC*100:.1f}% and gets sliced")

    def test_zoom_ends_showing_the_whole_frame(self):
        """`zoom` is the counter-example that must stay true: it pulls back TO 1.0, so the clip ends
        with nothing cropped. If it ever ends above 1.0 it has the same defect."""
        with open(os.path.join(ROOT, "app/services/media_service.py"), encoding="utf-8") as f:
            body = f.read().split("def _zoom_vf_video")[1].split("\ndef ")[0]
        self.assertIn("max(1.0,", body, "zoom must floor at 1.0, i.e. finish on the full frame")


if __name__ == "__main__":
    unittest.main(verbosity=1)
