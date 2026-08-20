"""`zoom` and `--zf` are ONE fact, and a rule that sets only one of them cuts the top off the page.

The client shrinks the whole document with `body{zoom}` on desktop. Viewport units are NOT rescaled
by zoom, so every full-height container is written `calc(100dvh / var(--zf))` — the variable exists
purely to undo the zoom. They are therefore the same fact written twice, and the moment a rule
changes one without the other, every one of those containers is sized for a zoom the page is not
using.

MEASURED, on a Windows laptop: the high-DPI override sets `zoom:1` and nothing else, so a screen
that also matched the 1600px tier kept `--zf:.72`. The desktop's own height is
`calc((100dvh - 60px) / var(--zf, 1))` — 139% of the window — so it grew taller than the viewport
and its top went off screen. Reported as "windows are getting cut off at the top all the time now"
and "i have to resize the entire windows app to see the desktop normally": narrowing the window
below 1400px drops the override, the two agree again, and it looks fixed.

`var(--zf, 1)`'s fallback does not save this. The variable IS set — to the wrong number.
"""
import re
import unittest
from pathlib import Path

CSS = Path(__file__).resolve().parents[2] / "static" / "css" / "client.css"


class ZoomAndZfAgree(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.src = CSS.read_text()
        # Strip comments: they quote `body{zoom:.67-.77}` all over, as prose.
        cls.code = re.sub(r"/\*.*?\*/", "", cls.src, flags=re.S)

    def test_every_rule_that_sets_zoom_also_sets_zf_to_the_same_value(self):
        blocks = re.findall(r"body\s*\{([^}]*)\}", self.code)
        seen = 0
        for b in blocks:
            mz = re.search(r"(?<![-\w])zoom\s*:\s*([0-9.]+)", b)
            if not mz:
                continue
            seen += 1
            mf = re.search(r"--zf\s*:\s*([0-9.]+)", b)
            self.assertIsNotNone(
                mf, "a body rule sets zoom:%s and no --zf — every container written "
                    "calc(100dvh / var(--zf)) is now sized for a zoom the page is not using: %r"
                    % (mz.group(1), b.strip()))
            self.assertEqual(
                float(mz.group(1)), float(mf.group(1)),
                "zoom and --zf disagree in one rule (%s vs %s) — they are the same fact: %r"
                % (mz.group(1), mf.group(1), b.strip()))
        self.assertGreaterEqual(seen, 4, "the zoom tiers moved — re-read this test")

    def test_the_desktop_is_one_of_the_containers_that_depends_on_it(self):
        """Named so the cost of getting this wrong is written next to the rule, not just in a
        commit message: this is the container whose top went off screen."""
        self.assertIn("var(--zf, 1)", self.src)
        self.assertRegex(self.src, r"height:calc\(\(100dvh - 60px\) / var\(--zf, 1\)\)")
