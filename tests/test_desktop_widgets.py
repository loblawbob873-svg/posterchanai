"""Desktop widgets, as the DOCUMENT sees them — the shipped `_normDoc` under node.

Run: venv-unified/bin/python -m unittest tests.test_desktop_widgets

Widgets live in the same encrypted `pcai:desktop` document as the icon arrangement, and `_normDoc` is
the one place its invariants are enforced. Everything it decides fails silently on screen: a widget
that stops appearing, one that draws off the edge of a smaller laptop, a document a newer client
wrote that a older one has to survive reading.

The rules asserted here are decisions, not accidents:

  * POSITIONS ARE FRACTIONS, not pixels. Icons store pixels and clamp, which keeps them on screen but
    not where you put them — a panel against the right edge of a 2560px monitor belongs against the
    right edge of the laptop that opens the same account, not 1500px into the middle of it. This is
    what "widgets should resize going from a tablet to a desktop and back" actually requires.
  * SIZE IS A NAME, so the real width comes from the screen. A fixed pixel width is either cramped on
    a desktop or covers a tablet.
  * An UNKNOWN TYPE is dropped rather than drawn: an empty frame nothing can fill and nothing
    explains is worse than the widget being absent. This is also the forward-compatibility rule — a
    document written by a newer client must not be able to break the desktop of an older one.
  * `cfg` is a small flat bag, bounded in count and length. It is where a widget remembers its city;
    it is not a place to store documents, and it is read on every draw.
  * The whole list is capped, because this document is the one thing here a half-finished write or a
    future version could put anything in.

The rendering half (drawing, dragging, the shared timer) is scripts/check_os_desktop.py's job.
"""
import json
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OS_JS = ROOT / "static" / "js" / "client" / "os.js"

BOOT = """
global.window = {};
global.document = { addEventListener(){}, querySelector(){ return null; },
                    querySelectorAll(){ return []; } };
global.getComputedStyle = () => ({ zoom: '1' });
require(%s);
const PCOS = window.PCOS;
""" % json.dumps(str(OS_JS))


def _node(script: str):
    out = subprocess.run(["node", "-e", BOOT + script], capture_output=True, timeout=60)
    if out.returncode != 0:
        raise AssertionError(out.stderr.decode()[-2000:])
    return json.loads(out.stdout.decode() or "null")


def norm(doc):
    return _node(f"console.log(JSON.stringify(PCOS.__normDoc({json.dumps(doc)})))")


@unittest.skipUnless(shutil.which("node"), "node not installed")
class WidgetDocument(unittest.TestCase):
    def test_a_widget_survives_a_round_trip(self):
        d = norm({"widgets": [{"id": "w1", "type": "crypto", "x": 0.5, "y": 0.25, "size": "l",
                               "cfg": {"place": "Home"}}]})
        self.assertEqual(d["widgets"], [{"id": "w1", "type": "crypto", "x": 0.5, "y": 0.25,
                                         "size": "l", "cfg": {"place": "Home"}}])

    def test_an_unknown_type_is_KEPT_not_dropped(self):
        """This sanitiser runs on read AND on every write, so dropping a row here does not hide a
        widget — it PUBLISHES the deletion to every device. Arrange widgets on an up-to-date desktop,
        then let a cached PWA or an older APK move one icon, and that client's save would wipe them
        for everyone with nothing said. `order` and `hidden` already keep keys they do not recognise;
        this is the same problem. drawWidgets skips what it cannot draw."""
        d = norm({"widgets": [{"id": "a", "type": "stockmarket3000"},
                              {"id": "b", "type": "weather"}]})
        self.assertEqual([w["type"] for w in d["widgets"]], ["stockmarket3000", "weather"])

    def test_a_typeless_row_is_still_dropped(self):
        """"Unknown" is a type this client has not heard of; a row with no type at all is junk."""
        d = norm({"widgets": [{"id": "a"}, {"id": "b", "type": "!!!"}, {"id": "c", "type": "crypto"}]})
        self.assertEqual([w["type"] for w in d["widgets"]], ["crypto"])

    def test_positions_are_fractions_and_are_clamped(self):
        """Off-scale values are the ones that put a panel where it cannot be reached — and 1.0 has to
        survive intact, because that is 'against the right edge', the most useful position there is."""
        d = norm({"widgets": [
            {"id": "a", "type": "crypto", "x": 1, "y": 0},
            {"id": "b", "type": "crypto", "x": 4.5, "y": -3},
            {"id": "c", "type": "crypto", "x": "nonsense", "y": None},
        ]})
        self.assertEqual([[w["x"], w["y"]] for w in d["widgets"]], [[1, 0], [1, 0], [0, 0]])

    def test_an_unknown_size_becomes_the_middle_one(self):
        d = norm({"widgets": [{"id": "a", "type": "crypto", "size": "enormous"},
                              {"id": "b", "type": "crypto"},
                              {"id": "c", "type": "crypto", "size": "s"}]})
        self.assertEqual([w["size"] for w in d["widgets"]], ["m", "m", "s"])

    def test_two_widgets_cannot_share_an_id(self):
        """Every mutation finds its row by id; a duplicate means removing one removes both, and a cfg
        write lands on whichever came first."""
        d = norm({"widgets": [{"id": "same", "type": "crypto"}, {"id": "same", "type": "weather"}]})
        self.assertEqual(len(d["widgets"]), 1)
        self.assertEqual(d["widgets"][0]["type"], "crypto")

    def test_a_widget_with_no_id_still_gets_one(self):
        d = norm({"widgets": [{"type": "crypto"}, {"type": "weather"}]})
        ids = [w["id"] for w in d["widgets"]]
        self.assertTrue(all(ids), f"a widget with no id is unaddressable: {ids}")
        self.assertEqual(len(set(ids)), 2)

    def test_the_list_is_capped(self):
        d = norm({"widgets": [{"id": f"w{i}", "type": "crypto"} for i in range(60)]})
        self.assertLessEqual(len(d["widgets"]), 12)
        self.assertGreater(len(d["widgets"]), 0)

    def test_cfg_is_bounded_in_both_directions(self):
        """It is read on every draw of the desktop, and it is caller-written."""
        big = {"text": "x" * 9000}
        big.update({f"k{i}": i for i in range(40)})
        big["nested"] = {"no": "objects"}
        big["arr"] = [1, 2, 3]
        d = norm({"widgets": [{"id": "a", "type": "note", "cfg": big}]})
        cfg = d["widgets"][0]["cfg"]
        self.assertLessEqual(len(cfg), 12)
        for v in cfg.values():
            self.assertIn(type(v), (str, int, float, bool), f"cfg kept a {type(v)}")
        # The note's own copy: 400 was chosen for a city name and silently ate a sticky note on the
        # NEXT load, which is the worst place to discover it. Bounded, but generously.
        self.assertIn("text", cfg)
        self.assertEqual(len(cfg["text"]), 4000)

    def test_a_document_with_no_widgets_key_is_fine(self):
        """Every desktop arranged before this feature existed has exactly that shape."""
        d = norm({"order": ["home"], "folders": [], "hidden": [], "pos": {}})
        self.assertEqual(d["widgets"], [])

    def test_junk_in_place_of_the_list_does_not_throw(self):
        for bad in ("nope", 42, {"not": "an array"}, None):
            self.assertEqual(norm({"widgets": bad})["widgets"], [], f"widgets={bad!r}")

    def test_widgets_reach_computeLayout(self):
        """_normDoc alone is not enough — the drawing code reads them off the computed layout, and a
        rule that stops there would leave the desktop bare with the document intact."""
        got = _node("""
          const lay = PCOS.__layout([{view:'home',label:'Home',icon:'#i-home'}],
                                    { widgets: [{ id:'w1', type:'weather', x:0.2, y:0.8, size:'s' }] });
          console.log(JSON.stringify(lay.widgets || null));
        """)
        self.assertEqual(got, [{"id": "w1", "type": "weather", "x": 0.2, "y": 0.8,
                                "size": "s", "cfg": {}}])


@unittest.skipUnless(shutil.which("node"), "node not installed")
class WidgetSizing(unittest.TestCase):
    """`wgtBox` is what makes a widget fit the screen it is on rather than the one it was placed on."""

    def box(self, size, w, h, defn=None):
        return _node(f"console.log(JSON.stringify(PCOS.__wgtBox({json.dumps(size)}, {w}, {h}, "
                     f"{json.dumps(defn)})))")

    def test_a_bar_widget_is_wide_and_one_line_tall(self):
        """A search box wants width, not a panel's worth of dead space around one input."""
        bar = self.box("m", 1600, 900, {"bar": True})
        panel = self.box("m", 1600, 900)
        self.assertGreater(bar["w"], panel["w"])
        self.assertLess(bar["h"], panel["h"])

    def test_a_bar_still_fits_a_small_desk(self):
        bar = self.box("l", 700, 420, {"bar": True})
        self.assertLessEqual(bar["w"], 700)

    def test_a_big_desktop_gets_the_full_size(self):
        self.assertEqual(self.box("l", 2560, 1400), {"w": 380, "h": 250})

    def test_a_small_desktop_shrinks_it(self):
        """A tablet must not be covered by one panel — nor left with a widget too small to read."""
        b = self.box("l", 700, 420)
        self.assertLess(b["w"], 380)
        self.assertLessEqual(b["w"], 700 * 0.46 + 1)
        self.assertGreaterEqual(b["w"], 150)
        self.assertGreaterEqual(b["h"], 96)

    def test_it_never_exceeds_the_desk_even_when_tiny(self):
        b = self.box("l", 240, 200)
        self.assertLessEqual(b["w"], 240)
        self.assertLessEqual(b["h"], 200)


if __name__ == "__main__":
    unittest.main()


@unittest.skipUnless(shutil.which("node"), "node not installed")
class WeatherUnits(unittest.TestCase):
    """°C on a widget in America is the failure this exists to stop.

    A node in one country serves readers in another, so a SERVER-side default is wrong for somebody by
    construction. The default comes from the browser's own locale, and an explicit choice wins over it
    for ever after."""

    def units(self, cfg, locale="en-US", measurement=None):
        return _node("""
          const loc = %s, ms = %s;
          global.Intl = { DateTimeFormat: () => ({ resolvedOptions: () => (
            ms ? { locale: loc, measurementSystem: ms } : { locale: loc }) }) };
          global.navigator = { language: loc };
          console.log(JSON.stringify(PCOS.__wxUnits({ cfg: %s })));
        """ % (json.dumps(locale), json.dumps(measurement), json.dumps(cfg)))

    def test_an_american_reader_gets_fahrenheit(self):
        self.assertEqual(self.units({}, "en-US"), "imperial")

    def test_everyone_else_gets_celsius(self):
        self.assertEqual(self.units({}, "en-GB"), "metric")
        self.assertEqual(self.units({}, "de-DE"), "metric")

    def test_the_engine_s_own_answer_wins_when_it_has_one(self):
        """Intl reports the measurement system directly on modern engines; the region list is only the
        fallback for the ones that do not."""
        self.assertEqual(self.units({}, "en-GB", "us"), "imperial")
        self.assertEqual(self.units({}, "en-US", "metric"), "metric")

    def test_a_chosen_unit_beats_the_locale_for_ever(self):
        self.assertEqual(self.units({"units": "metric"}, "en-US"), "metric")
        self.assertEqual(self.units({"units": "imperial"}, "en-GB"), "imperial")

    def test_a_junk_stored_value_falls_back_rather_than_sticking(self):
        self.assertEqual(self.units({"units": "kelvin"}, "en-US"), "imperial")


@unittest.skipUnless(shutil.which("node"), "node not installed")
class WidgetPlacement(unittest.TestCase):
    """Fractions preserve the EDGE a panel was put against; they cannot preserve the space BETWEEN two
    of them. Widgets keep a minimum readable size while the desk shrinks, so a tablet has
    proportionally less free area and an arrangement that is spread out on a monitor lands on top of
    itself — reported as "on tablet, they overlap"."""

    def place(self, widgets, w, h):
        return _node("console.log(JSON.stringify(PCOS.__placeWidgets(%s, %d, %d)))"
                     % (json.dumps(widgets), w, h))

    @staticmethod
    def _overlaps(rects):
        bad = []
        for i, a in enumerate(rects):
            for b in rects[i + 1:]:
                if (a["x"] < b["x"] + b["w"] and a["x"] + a["w"] > b["x"]
                        and a["y"] < b["y"] + b["h"] and a["y"] + a["h"] > b["y"]):
                    bad.append((a["id"], b["id"]))
        return bad

    def _stack(self, n=4, size="m"):
        # Exactly what addWidget produces: down the right-hand edge in 0.22 steps.
        return [{"id": f"w{i}", "type": "crypto", "x": 1, "y": min(1, i * 0.22), "size": size, "cfg": {}}
                for i in range(n)]

    def test_four_widgets_do_not_overlap_on_a_tablet(self):
        r = self.place(self._stack(4), 1024, 640)
        self.assertEqual(self._overlaps(r), [], f"panels landed on each other: {r}")

    def test_nor_on_a_small_tablet_in_portrait(self):
        r = self.place(self._stack(4), 800, 1000)
        self.assertEqual(self._overlaps(r), [])

    def test_nor_when_the_desk_is_really_short(self):
        r = self.place(self._stack(3, "l"), 1100, 420)
        self.assertEqual(self._overlaps(r), [])

    def test_everything_stays_inside_the_desk(self):
        """Pushing panels apart must never push one off the edge — that is worse than the overlap."""
        for w, h in ((1024, 640), (800, 1000), (1400, 900), (700, 400)):
            for r in self.place(self._stack(5), w, h):
                self.assertGreaterEqual(r["x"], 0)
                self.assertGreaterEqual(r["y"], 0)
                self.assertLessEqual(r["x"] + r["w"], w + 1, f"off the right at {w}x{h}")
                self.assertLessEqual(r["y"] + r["h"], h + 1, f"off the bottom at {w}x{h}")

    def test_a_roomy_desktop_is_left_exactly_as_arranged(self):
        """The resolution must be invisible where nothing collides, or a deliberate arrangement stops
        being deliberate."""
        spread = [{"id": "a", "type": "crypto", "x": 0, "y": 0, "size": "m", "cfg": {}},
                  {"id": "b", "type": "crypto", "x": 1, "y": 1, "size": "m", "cfg": {}}]
        r = {p["id"]: p for p in self.place(spread, 2560, 1400)}
        self.assertEqual(r["a"]["x"], 5)
        self.assertGreater(r["b"]["x"], 2000, "the right-edge panel moved on a desk with room to spare")

    def test_the_same_document_always_lays_out_the_same_way(self):
        """Resolution order is reading order, not insertion order — otherwise the desktop reshuffles
        itself depending on which widget happened to be added first."""
        a = self.place(self._stack(4), 1024, 640)
        b = self.place(list(reversed(self._stack(4))), 1024, 640)
        self.assertEqual({p["id"]: (p["x"], p["y"]) for p in a},
                         {p["id"]: (p["x"], p["y"]) for p in b})

    def test_more_widgets_than_fit_still_produces_a_position_for_each(self):
        r = self.place(self._stack(12, "l"), 900, 500)
        self.assertEqual(len(r), 12)
        for p in r:
            self.assertGreaterEqual(p["x"], 0)
            self.assertGreaterEqual(p["y"], 0)


@unittest.skipUnless(shutil.which("node"), "node not installed")
class TodayWidget(unittest.TestCase):
    """The "Today" widget's DECISION — which occurrences it shows and how it marks them.

    Asked for as "new widget: calendar that shows you events for the day". Two things about that are
    easy to get wrong and invisible when you do:

      * "the day" is the CALENDAR DAY `now` falls in, not "the next 24 hours". At 23:50 the second
        reading puts tomorrow morning's 09:00 under *today*, which is exactly the hour you are most
        likely to be looking at a desktop clock and least likely to notice.
      * a repeating event has to be EXPANDED. The month grid shipped once placing only DTSTART, and
        59 of one real 707-event calendar — every weekly delivery, every birthday — drew exactly
        once. A widget with the same bug shows an empty day to somebody whose whole week repeats.

    So this runs the shipped `_calSplit` and `_calOccurrences` under node, against real iCalendar.
    """

    def split(self, occ, now_iso):
        return _node(
            "const occ = %s.map(o => Object.assign({}, o, {start: new Date(o.start)}));\n"
            "const r = PCOS.__calSplit(occ, new Date(%s));\n"
            "console.log(JSON.stringify({today: r.today.map(x => [x.title, !!x.gone]),\n"
            "                            later: r.later.map(x => x.title)}));"
            % (json.dumps(occ), json.dumps(now_iso)))

    def test_the_day_is_a_calendar_day_not_the_next_24_hours(self):
        occ = [{"title": "late tonight", "start": "2026-03-04T23:55:00"},
               {"title": "tomorrow 9am", "start": "2026-03-05T09:00:00"}]
        r = self.split(occ, "2026-03-04T23:50:00")
        self.assertEqual([t for t, _ in r["today"]], ["late tonight"])
        self.assertEqual(r["later"], ["tomorrow 9am"])

    def test_a_finished_appointment_is_dimmed_and_not_dropped(self):
        occ = [{"title": "standup", "start": "2026-03-04T09:00:00"},
               {"title": "review", "start": "2026-03-04T16:00:00"}]
        r = self.split(occ, "2026-03-04T11:00:00")
        self.assertEqual(r["today"], [["standup", True], ["review", False]])

    def test_an_all_day_item_is_never_past(self):
        """It is true for the whole day; dimming it at 00:01 would be wrong all day."""
        occ = [{"title": "Alice's birthday", "start": "2026-03-04T00:00:00", "allDay": True}]
        r = self.split(occ, "2026-03-04T18:00:00")
        self.assertEqual(r["today"], [["Alice's birthday", False]])

    def test_only_two_later_items_are_offered(self):
        occ = [{"title": f"e{i}", "start": f"2026-03-0{i}T09:00:00"} for i in range(5, 9)]
        r = self.split(occ, "2026-03-04T09:00:00")
        self.assertEqual(r["today"], [])
        self.assertEqual(r["later"], ["e5", "e6"])

    def test_a_repeating_event_is_expanded_not_placed_once(self):
        """The bug the month grid had. `occurrences` is PCIcal's, but the widget has to CALL it —
        a widget that read DTSTART would show an empty day to anybody whose week repeats."""
        ics = ("BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\nUID:x1\r\n"
               "DTSTART:20260302T090000\r\nSUMMARY:standup\r\n"
               "RRULE:FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n")
        # ical.js registers on globalThis; the browser's `window` IS globalThis and this harness's is
        # not, so the alias is the harness catching up with the page rather than a shim for it.
        out = _node(
            "require(%s); window.PCIcal = global.PCIcal;\n"
            "const occ = PCOS.__calOccurrences([{uid:'x1', cal:'c', ics: %s}],\n"
            "  new Date(2026, 2, 4), new Date(2026, 2, 5));\n"
            "console.log(JSON.stringify(occ.map(o => o.title)));"
            % (json.dumps(str(ROOT / "static" / "js" / "client" / "ical.js")), json.dumps(ics)))
        self.assertEqual(out, ["standup"], "a weekly event did not appear on a later week")
