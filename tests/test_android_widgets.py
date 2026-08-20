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


class EveryWidgetDeclaresItsCeiling(unittest.TestCase):
    """"the weather widget is just too gigantic on phones" — and it stayed gigantic after the
    arithmetic that produced the giant span was fixed, because a PLACED widget is stored with a SPAN
    and nothing ever re-derives it. The old density-inflated maths made a 180dp card ask for six of a
    four-column grid, the cap turned that into the full width, and there it stayed on every draw of
    every later build. Removing it was the only way out, and removing it was broken too.

    `maxResizeWidth`/`maxResizeHeight` are the widget SAYING how big it wants to get, so putting a
    stored span back inside them is not the launcher overruling a person — it is the launcher
    stopping overruling the widget. A provider that declares nothing gets no opinion, which is every
    third-party widget on the phone.
    """

    def test_each_one_says_how_big_it_wants_to_get(self):
        for p in PROVIDERS:
            with self.subTest(widget=p.name):
                a = attrs(p)
                for k in ("maxResizeWidth", "maxResizeHeight"):
                    self.assertIn(k, a, "%s declares no %s, so nothing can ever bring a span the "
                                        "old arithmetic invented back down" % (p.name, k))

    def test_the_ceiling_is_above_the_floor_and_above_the_ask(self):
        for p in PROVIDERS:
            with self.subTest(widget=p.name):
                a = attrs(p)
                for hi, lo in (("maxResizeWidth", "minWidth"), ("maxResizeHeight", "minHeight")):
                    self.assertGreaterEqual(dp(a[hi]), dp(a[lo]),
                                            "%s: %s is below %s, so the widget can never be placed "
                                            "at the size it asks for" % (p.name, hi, lo))

    def test_the_ceiling_is_not_simply_the_whole_phone(self):
        """A ceiling of four phone columns is no ceiling: a phone cell is roughly 90dp, and the
        report is about a widget occupying every one of them."""
        CELL, COLS = 90, 4
        wide = [p.name for p in PROVIDERS if dp(attrs(p)["maxResizeWidth"]) // CELL >= COLS]
        self.assertNotIn("weather_widget_info.xml", wide,
                         "the weather widget may still take a phone's whole width, which is the "
                         "report")


class ACeilingNeverPinsAWidget(unittest.TestCase):
    """A ceiling that lands on the same cell count as the floor is not a ceiling — it is a lock.

        "now I can't resize the weather widget wtf are you doing to it"

    That was self-inflicted, one commit after the ceilings were introduced to bring a full-width
    widget back down. Weather's floor is 110dp and its ceiling was 260dp; on the phone the emulator
    actually measures — 261x256px cells at density 2.75 — those work out to `ceil(303/261) = 2` and
    `floor(715/261) = 2`. Two is two, so the widget could only ever be exactly two cells wide, the
    handles moved nothing, and the fix for "too gigantic" became "cannot be resized at all".

    The arithmetic here is the launcher's own (`Widgets.spanFor` for the floor, a whole-cell divide
    for the ceiling) run against REAL phone metrics, and what it asserts is that every axis a widget
    declares resizable has more than one answer available. A number that satisfies this on paper and
    not on a phone is the entire failure being guarded against.
    """

    # The device measured these: 1080x2340 at 440dpi is a 4x6 grid of 261x256px cells. The second
    # is a smaller, denser phone, because a ceiling can pin on one size and not on another.
    PHONES = [("1080x2340 @440dpi", 2.75, 261, 256, 4, 6),
              ("1080x1920 @480dpi", 3.00, 270, 288, 4, 5)]

    def _range(self, lo_dp, hi_dp, cell_px, density, grid):
        import math
        lo = max(1, math.ceil(int(lo_dp * density) / cell_px))       # Widgets.spanFor
        hi = max(1, int(hi_dp * density) // cell_px)                 # HomeActivity.maxCells
        hi = max(lo, min(grid, hi))
        return lo, hi

    def test_every_resizable_axis_has_more_than_one_answer(self):
        for p in PROVIDERS:
            a = attrs(p)
            mode = a.get("resizeMode", "")
            for axis, lo_k, hi_k, cell_i, grid_i, flag in (
                    ("width", "minResizeWidth", "maxResizeWidth", 2, 4, "horizontal"),
                    ("height", "minResizeHeight", "maxResizeHeight", 3, 5, "vertical")):
                if flag not in mode:
                    continue                     # not resizable that way; nothing is promised
                for phone in self.PHONES:
                    with self.subTest(widget=p.name, axis=axis, phone=phone[0]):
                        lo, hi = self._range(dp(a[lo_k]), dp(a[hi_k]),
                                             phone[cell_i], phone[1], phone[grid_i])
                        self.assertGreater(
                            hi, lo,
                            "%s can only ever be %d cells %s on %s — its %s and its %s land on the "
                            "same cell count, so the resize handles move nothing"
                            % (p.name, lo, axis, phone[0], lo_k, hi_k))

    def test_the_weather_widget_is_still_never_the_whole_phone(self):
        """Both halves at once, because they pull against each other: it must have room to grow AND
        must not be able to take every column, which is the report this all started from."""
        a = attrs([p for p in PROVIDERS if "weather" in p.name][0])
        for phone in self.PHONES:
            with self.subTest(phone=phone[0]):
                lo, hi = self._range(dp(a["minResizeWidth"]), dp(a["maxResizeWidth"]),
                                     phone[2], phone[1], phone[4])
                self.assertGreater(hi, lo, "pinned on %s" % phone[0])
                self.assertLess(hi, phone[4],
                                "it can still take all %d columns on %s" % (phone[4], phone[0]))
