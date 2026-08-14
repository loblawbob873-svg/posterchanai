"""A draggable panel must not contain a scroller that steals the gesture.

THE BUG THIS PINS, and it took a user's A/B to find: desktop ICONS dragged on a tablet and WIDGETS
did not, on the same screen, in the same session. Both paths gate on the pointer correctly, both
attach their listeners to `document`, and `.os-wgt` already carried `touch-action:none`.

What differs is structure. `.os-wgt-body` is `overflow:auto`, and a touch that STARTS on a scrollable
descendant is handed to that scroller — `touch-action:none` on the ancestor does not save it. The
browser then fires `pointercancel` on the first move, which `startWgtDrag` rightly treats as a
release, so the widget never moves a pixel. An icon has no scrollable child, which is the whole
difference.

Measured with real touch input rather than reasoned about: two identical panels, one whose body
carried `touch-action:none` and one without. The first completed the drag; the second cancelled.

This reads the stylesheet rather than driving a browser, because the property is the whole fix and a
CSS assertion cannot go stale the way a rendered measurement can. `scripts/check_os_desktop.py`
covers the drag itself.
"""
import re
import unittest
from pathlib import Path

CSS = Path(__file__).resolve().parents[2] / "static" / "css" / "client.css"

# Scrollable and NOT inside a draggable panel: the widget picker is a list you choose from.
ALLOWED_SCROLLERS = {".os-wgt-grid"}


def _rules(css):
    """(selector, body) for every top-level rule whose selector starts a widget class."""
    for m in re.finditer(r"^(\.(?:os-wgt|wgt)[a-zA-Z0-9_-]*(?:\s[^{,]+)?)\{([^}]*)\}", css, re.M):
        yield m.group(1).strip(), m.group(2)


class WidgetDragSurvivesTouch(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.css = CSS.read_text(encoding="utf-8")

    def test_the_widget_panel_itself_owns_the_gesture(self):
        body = next((b for s, b in _rules(self.css) if s == ".os-wgt"), None)
        self.assertIsNotNone(body, ".os-wgt rule not found")
        self.assertIn("touch-action:none", body.replace(" ", ""),
                      "a widget must own the drag gesture or the page scrolls instead")

    def test_no_scroller_inside_a_draggable_widget_can_steal_the_drag(self):
        """The one that was actually broken. A new scrollable list added to a widget lands here."""
        stealing = [
            sel for sel, body in _rules(self.css)
            if re.search(r"overflow(-y)?:\s*(auto|scroll)", body)
            and "touch-action" not in body
            and sel not in ALLOWED_SCROLLERS
        ]
        self.assertEqual(
            stealing, [],
            "these scroll inside a draggable widget and will cancel its drag on touch — give each "
            "`touch-action:none`, or add it to ALLOWED_SCROLLERS if it is not inside a widget: "
            + ", ".join(stealing))

    def test_the_widget_picker_is_still_allowed_to_scroll(self):
        """The exemption is real, not a way of silencing the rule: 'Add a widget' is a list you pick
        from, is not draggable, and would be unusable if it could not scroll."""
        body = next((b for s, b in _rules(self.css) if s == ".os-wgt-grid"), None)
        self.assertIsNotNone(body, ".os-wgt-grid rule not found")
        self.assertRegex(body, r"overflow(-y)?:\s*(auto|scroll)",
                         "the picker is exempt because it scrolls; if it no longer does, drop the "
                         "exemption rather than leaving a stale one")


if __name__ == "__main__":
    unittest.main()
