"""Every widget this app ships can be made smaller, so a phone can fit it.

    "widgets need support to fit on mobile phone screen"
    "widgets look great on tablet"
    "still can't add widget to phone, says too big"

That is one sentence three times. A tablet's home grid is 5-7 columns by 6-8 rows and a phone's is 4
by 3-6, so the same widget asking for the same rectangle lands on one and is refused by the other.

The refusal is not really about the grid, though — it is about what the widget SAYS. A provider that
declares `minWidth`/`minHeight` and no `minResizeWidth`/`minResizeHeight` has told every launcher,
including this app's own, that the size it asked for IS its floor. `resizeMode` then promises a
resize that nothing can actually perform, and `Desk.addShrinking` — which walks from the requested
size down to the declared floor — has nowhere to walk to. Ours declared none of them.

`targetCellWidth`/`targetCellHeight` (API 31+) do not stand in for it: they are what the widget wants,
not the least it will accept, and a launcher on an older platform never reads them at all.
"""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
XML = ROOT / "mobile/android/app/src/main/res/xml"

PROVIDERS = sorted(XML.glob("*_widget_info.xml"))


def attrs(path):
    src = path.read_text()
    return dict(re.findall(r'android:(\w+)="([^"]*)"', src))


def dp(v):
    m = re.match(r"^(\d+)dp$", v or "")
    return int(m.group(1)) if m else None


class EveryWidgetDeclaresItsFloor(unittest.TestCase):

    def test_there_are_widgets_to_check(self):
        """A glob that matches nothing passes every test below it."""
        self.assertGreaterEqual(len(PROVIDERS), 3, [p.name for p in PROVIDERS])

    def test_each_one_says_how_small_it_can_go(self):
        for p in PROVIDERS:
            with self.subTest(widget=p.name):
                a = attrs(p)
                for k in ("minResizeWidth", "minResizeHeight"):
                    self.assertIn(k, a,
                                  "%s declares no %s, so every launcher treats the size it asked "
                                  "for as its floor and a phone grid that cannot spare those cells "
                                  "refuses it outright" % (p.name, k))

    def test_the_floor_is_actually_lower_than_the_ask(self):
        """A floor equal to the request is no floor at all — it is the same refusal spelled twice,
        and it silently makes the launcher's shrink-to-fit search a no-op."""
        for p in PROVIDERS:
            with self.subTest(widget=p.name):
                a = attrs(p)
                for lo, hi in (("minResizeWidth", "minWidth"), ("minResizeHeight", "minHeight")):
                    l, h = dp(a.get(lo)), dp(a.get(hi))
                    self.assertIsNotNone(l, "%s %s is not a dp value" % (p.name, lo))
                    self.assertIsNotNone(h, "%s %s is not a dp value" % (p.name, hi))
                    self.assertLessEqual(l, h, "%s: %s is larger than %s" % (p.name, lo, hi))
                if dp(a["minResizeWidth"]) == dp(a["minWidth"]) \
                        and dp(a["minResizeHeight"]) == dp(a["minHeight"]):
                    self.fail("%s cannot be made smaller in either direction, so `resizeMode` "
                              "promises a resize nothing can perform" % p.name)

    def test_it_fits_a_phone_grid(self):
        """A phone is 4 columns of roughly 90dp and at least 3 rows of roughly 90dp. A widget whose
        FLOOR needs more than that cannot be placed on any phone, whatever the launcher does."""
        CELL, COLS, ROWS = 90, 4, 3
        for p in PROVIDERS:
            with self.subTest(widget=p.name):
                a = attrs(p)
                w = -(-dp(a["minResizeWidth"]) // CELL)
                h = -(-dp(a["minResizeHeight"]) // CELL)
                self.assertLessEqual(w, COLS,
                                     "%s needs %d of a phone's %d columns at its smallest"
                                     % (p.name, w, COLS))
                self.assertLessEqual(h, ROWS,
                                     "%s needs %d of a phone's %d rows at its smallest"
                                     % (p.name, h, ROWS))

    def test_a_resizable_widget_is_the_only_kind_this_applies_to(self):
        """Stated rather than assumed: all three are resizable, so all three must say how far."""
        for p in PROVIDERS:
            with self.subTest(widget=p.name):
                self.assertIn("resizeMode", attrs(p), p.name)
