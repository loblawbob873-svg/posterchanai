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


def _node(script: str, tz: str = ""):
    """Run the shipped file under node. `tz` pins the RUNNER's own zone, which the clock reads as
    "here" — without it, "is it tomorrow in Tokyo" is answered against whatever zone the machine
    running the tests is in, and the same assertion passes in London and fails in Denver."""
    import os
    env = dict(os.environ)
    if tz:
        env["TZ"] = tz
    out = subprocess.run(["node", "-e", BOOT + script], capture_output=True, timeout=60, env=env)
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


class PerformanceWidgetSource(WidgetSizing):
    """The useful part of a performance widget is history, not a progress bar snapshot."""

    def test_cpu_ram_and_network_have_numeric_line_charts(self):
        src = OS_JS.read_text()
        self.assertIn('class="wgt-perf-graph"', src)
        self.assertIn('class="wgt-perf-line primary"', src)
        self.assertIn("h.a.length>60", src)
        self.assertIn("' logical CPUs'", src)
        self.assertIn("'Total  '+_sysBytes(s.memory.total)", src)
        self.assertIn("'\u2193 '+_sysBytes(a)+'/s'", src)
        self.assertIn("'\u2191 '+_sysBytes(b)+'/s'", src)

    def test_network_has_separate_receive_and_send_lines(self):
        src = OS_JS.read_text()
        self.assertIn("kind==='network'?_perfPoints(h.b,max):''", src)
        self.assertIn("max=Math.max(1024,...h.a,...h.b)*1.12", src)

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


def _ms(y, mo, d, h, mi=0):
    """A fixed instant, in epoch ms. Every clock assertion below is against a KNOWN moment: a test
    that formats `now` proves nothing twice a year."""
    from datetime import datetime, timezone
    return int(datetime(y, mo, d, h, mi, tzinfo=timezone.utc).timestamp() * 1000)


@unittest.skipUnless(shutil.which("node"), "node not installed")
class Clock(unittest.TestCase):
    """The clock, as it decides what to print — the shipped `_clockFace` under node.

    The taskbar already carries HH:MM, so this widget earns its place on the cities: what it is asked
    is "what time is it in Tokyo, and is it tomorrow there". Every way that can be wrong is silent —
    a clock does not fail, it shows a different time — and the three that actually bite are all
    offset arithmetic somebody was tempted to do by hand: a half-hour zone, a zone whose DST is not
    ours, and the date line. None of them are done by hand here (it is all Intl, against the tz
    database the browser ships), and these pin that it stays that way.

    Times are asserted with h12 forced off, because the runner's own locale decides the rest.
    """

    def face(self, ms, tz, cfg=None):
        cfg = dict(cfg or {})
        cfg.setdefault("h12", 0)
        # TZ=UTC: "tomorrow" is relative to the READER's day, so the runner's own zone is part of the
        # question being asked. Pinned, or this suite means something different in every timezone.
        return _node("console.log(JSON.stringify(PCOS.__clockFace(new Date(%d), %s, %s)))"
                     % (ms, json.dumps(tz), json.dumps(cfg)), tz="UTC")

    def test_a_city_reads_its_own_time(self):
        # 16:00 UTC is 01:00 the NEXT day in Tokyo (+9).
        f = self.face(_ms(2026, 1, 15, 16), "Asia/Tokyo")
        self.assertTrue(f["ok"])
        self.assertEqual(f["time"], "01:00")

    def test_a_day_ahead_says_tomorrow(self):
        """"01:00" under a city's name is half an answer; the useful half is WHICH day."""
        f = self.face(_ms(2026, 1, 15, 16), "Asia/Tokyo")
        self.assertEqual(f["dayNote"], "tomorrow")

    def test_a_day_behind_says_yesterday(self):
        f = self.face(_ms(2026, 1, 15, 2), "America/Los_Angeles")
        self.assertEqual(f["time"], "18:00")
        self.assertEqual(f["dayNote"], "yesterday")

    def test_a_half_hour_zone_is_not_rounded_to_an_hour(self):
        """India is +5:30. Anything that reasons in whole hours is 30 minutes wrong here, all year."""
        f = self.face(_ms(2026, 1, 15, 4), "Asia/Kolkata")
        self.assertEqual(f["time"], "09:30")

    def test_a_zone_follows_its_own_summer_time_not_ours(self):
        """Same city, same clock, six months apart: London is +0 in January and +1 in July. A stored
        offset (the obvious way to keep a city) is right for half the year."""
        self.assertEqual(self.face(_ms(2026, 1, 15, 12), "Europe/London")["time"], "12:00")
        self.assertEqual(self.face(_ms(2026, 7, 15, 12), "Europe/London")["time"], "13:00")

    def test_the_local_face_never_claims_another_day(self):
        f = self.face(_ms(2026, 1, 15, 12), "")
        self.assertTrue(f["ok"])
        self.assertEqual(f["dayNote"], "")
        self.assertTrue(f["date"], "the date under the numeral is the other half of the widget")

    def test_a_zone_this_browser_cannot_resolve_says_so(self):
        """The dangerous failure is not an error, it is THIS one falling back to local time under
        another city's name — a clock that is confidently, silently wrong for whoever added it."""
        f = self.face(_ms(2026, 1, 15, 12), "Mars/Olympus")
        self.assertFalse(f["ok"])
        self.assertEqual(f["time"], "--:--")

    def test_seconds_are_off_until_they_are_asked_for(self):
        self.assertRegex(self.face(_ms(2026, 1, 15, 12, 34), "")["time"], r"^\d{1,2}:\d{2}$")
        self.assertRegex(self.face(_ms(2026, 1, 15, 12, 34), "", {"sec": 1})["time"],
                         r"^\d{1,2}:\d{2}:\d{2}$")

    def test_the_am_pm_marker_is_kept_out_of_the_numeral(self):
        """It is drawn small beside a 34px figure. Formatted INTO the string, "10:45 PM" is one size
        and the panel reads as a line of text rather than as a clock."""
        f = self.face(_ms(2026, 1, 15, 22, 45), "", {"h12": 1})
        self.assertTrue(f["ampm"], "a 12-hour clock with no marker cannot say which 10:45 it is")
        self.assertNotIn(f["ampm"].lower(), f["time"].lower())
        self.assertEqual(self.face(_ms(2026, 1, 15, 22, 45), "", {"h12": 0})["ampm"], "")

    def zones(self, cfg):
        return _node("console.log(JSON.stringify(PCOS.__clockZones(%s)))" % json.dumps(cfg))

    def test_the_city_list_is_bounded_and_tolerant(self):
        self.assertEqual(self.zones({"zones": " America/Denver ,Asia/Tokyo,, "}),
                         ["America/Denver", "Asia/Tokyo"])
        self.assertEqual(len(self.zones({"zones": ",".join(["Asia/Tokyo"] * 9)})), 4)
        self.assertEqual(self.zones({}), [])
        self.assertEqual(self.zones(None), [])


@unittest.skipUnless(shutil.which("node"), "node not installed")
class Headlines(unittest.TestCase):
    """The rotation. It wraps, because a feed is not always longer than the panel."""

    def window(self, n_items, off, rows):
        items = [{"title": "t%d" % i} for i in range(n_items)]
        return _node("console.log(JSON.stringify(PCOS.__newsWindow(%s, %d, %d).map(x => x.title)))"
                     % (json.dumps(items), off, rows))

    def test_it_wraps_round_the_end(self):
        self.assertEqual(self.window(5, 3, 3), ["t3", "t4", "t0"])

    def test_a_short_feed_never_repeats_itself(self):
        """Three headlines in a panel with room for five is three headlines, not "t0 t1 t2 t0 t1"."""
        self.assertEqual(self.window(3, 0, 5), ["t0", "t1", "t2"])

    def test_an_offset_past_the_end_still_lands_inside(self):
        self.assertEqual(self.window(4, 9, 2), ["t1", "t2"])
        self.assertEqual(self.window(4, -1, 2), ["t3", "t0"])

    def test_no_items_is_not_an_error(self):
        self.assertEqual(_node("console.log(JSON.stringify(PCOS.__newsWindow([], 2, 3)))"), [])


@unittest.skipUnless(shutil.which("node"), "node not installed")
class CommunityCounters(unittest.TestCase):
    """All five, always, including the zeroes — the decision netStatsHtml already made and had to
    make twice: a cell that disappears when it is zero reads as a feature that is missing, not as a
    quiet network."""

    def cells(self, st):
        return _node("console.log(JSON.stringify(PCOS.__statCells(%s)))" % json.dumps(st))

    def test_a_silent_network_still_shows_five_counters(self):
        c = self.cells({"users": 0, "online": 0, "relay": 0, "streams": 0, "calls": 0})
        self.assertEqual(len(c), 5)
        self.assertEqual([x["label"] for x in c],
                         ["WoT", "online", "on relay", "live", "in call"])
        self.assertEqual([x["n"] for x in c], [0, 0, 0, 0, 0])

    def test_the_live_cell_only_lights_when_something_is_live(self):
        self.assertFalse(any(x.get("live") for x in
                             self.cells({"users": 9, "online": 3, "relay": 2, "streams": 0, "calls": 1})))
        lit = [x for x in self.cells({"streams": 2}) if x.get("live")]
        self.assertEqual([x["label"] for x in lit], ["live"])

    def test_junk_counts_as_none_rather_than_NaN(self):
        c = self.cells({"users": "many", "online": -4, "relay": None, "streams": 1.7, "calls": 3})
        self.assertEqual([x["n"] for x in c], [0, 0, 0, 1.7, 3])

    def test_a_missing_payload_does_not_throw(self):
        self.assertEqual(len(self.cells(None)), 5)


@unittest.skipUnless(shutil.which("node"), "node not installed")
class TheSharedTimer(unittest.TestCase):
    """ONE interval for every widget, at the rate of the fastest one mounted.

    It was a flat 15s, which is right for everything that reads a network and makes a CLOCK wrong by
    up to fifteen seconds — on the one panel whose whole job is to be right, and in the way somebody
    notices immediately (this against their phone). The property being kept is that there is still
    exactly one timer, and that it is stopped when nothing is watching; the period is what moves.
    """

    def period(self, everies):
        return _node("console.log(JSON.stringify(PCOS.__wgtPeriodOf(%s)))" % json.dumps(everies))

    def test_a_deskful_of_slow_panels_keeps_the_slow_tick(self):
        self.assertEqual(self.period([90000, 600000, 300000]), 15000)
        self.assertEqual(self.period([]), 15000)
        self.assertEqual(self.period([None, 0, False]), 15000)

    def test_a_clock_speeds_the_whole_timer_up(self):
        self.assertEqual(self.period([90000, 1000, 600000]), 1000)

    def due_in(self, every):
        return _node("console.log(JSON.stringify(PCOS.__wgtDueIn(%d)))" % every)

    def test_a_widget_whose_interval_IS_the_tick_fires_on_every_tick(self):
        """The deadline was `Date.now() + every` read at refresh time — i.e. with that tick's jitter
        baked in — so the next tick had to be later by more jitter than the last one. A coin flip:
        the clock skipped roughly every other second (:01 → :03) and the Community panel refreshed
        every ~30s against its declared 15. The deadline is set SHORT to absorb that."""
        self.assertLess(self.due_in(1000), 1000)
        self.assertLess(self.due_in(15000), 15000)

    def test_but_a_slow_widget_still_does_not_run_early(self):
        """The slack has to be small against the interval, or a 90s ticker becomes an 80s one."""
        self.assertGreater(self.due_in(90000), 89000)
        self.assertGreater(self.due_in(600000), 599000)

    def test_and_nothing_can_take_it_below_a_second(self):
        """`every` is a widget's own declaration; a typo in one would otherwise become the desktop's
        timer for as long as it is on screen."""
        self.assertEqual(self.period([50]), 1000)


@unittest.skipUnless(shutil.which("node"), "node not installed")
class HeadlineLinks(unittest.TestCase):
    """A row that cannot go anywhere must not be a link.

    `'#'` in an anchor with `target="_blank"` resolves to the CURRENT document, so a feed item with
    no link opened a second full copy of the client in a new tab — own relay sockets, own
    subscriptions — instead of an article."""

    def safe(self, u):
        return _node("console.log(JSON.stringify(PCOS.__safeHttp(%s)))" % json.dumps(u))

    def test_a_real_link_survives(self):
        self.assertEqual(self.safe("https://example.com/a?b=c#d"), "https://example.com/a?b=c#d")
        self.assertEqual(self.safe("HTTP://example.com/"), "HTTP://example.com/")

    def test_everything_else_becomes_nothing_at_all_not_a_hash(self):
        for bad in ("", None, "#", "mailto:a@b.c", "/relative", "javascript:alert(1)", "ftp://x/y"):
            self.assertEqual(self.safe(bad), "", f"{bad!r} still renders as a link")


@unittest.skipUnless(shutil.which("node"), "node not installed")
class TheSearchBarIsLean(unittest.TestCase):
    """"Reduce the border width around the text input so it's thinner and leaner" — the frame around
    a one-line control was a 96px panel, which is not a border, it is a widget with a search box
    somewhere inside it."""

    def test_a_bar_is_the_height_of_what_it_holds(self):
        h = _node("console.log(JSON.stringify(PCOS.__wgtBox('m', 1600, 900, {bar:true}).h))")
        self.assertLessEqual(h, 60, "the bar is a panel with an input in it again")
        self.assertGreaterEqual(h, 44, "…and now there is no room for the input")


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

    def spot(self, widgets, type_="clock", size="m", w=2494, h=1350):
        """The default size is this machine's REAL layout box: a 1920x1080 panel under the desktop
        scaling tier (body{zoom:.77}) lays out at 2494x1403 CSS px, less the taskbar. Measuring at
        1920x1000 is measuring a screen nobody has, and it is why the first version of this test
        passed against both rules."""
        return _node("console.log(JSON.stringify(PCOS.__nextWidgetSpot(%s, %s, %s, %d, %d)))"
                     % (json.dumps(widgets), json.dumps(type_), json.dumps(size), w, h))

    def _grow(self, n, rule, w=2494, h=1350):
        """Add `n` widgets one at a time, each placed by `rule`, and return where they ended up."""
        rows = []
        for i in range(n):
            y = (min(1, i * 0.22) if rule == "count" else self.spot(rows, w=w, h=h)["y"])
            rows.append({"id": "w%d" % i, "type": "clock", "x": 1, "y": y, "size": "m"})
        return self.place(rows, w, h)

    def test_widgets_do_not_open_with_growing_gaps_between_them(self):
        """"widgets open in weird places".

        The rule was `y = (how many you already have) x 0.22` — a fraction of the free height, so on
        a big desk each step is much taller than a widget. MEASURED at this machine's real layout
        size (2494x1350), adding six clock panels one at a time put them at

            count rule:  5, 261, 517, 773, 1029, 1164
            free slot :  10, 201, 392,  583,  774,  965

        A widget is 176px tall plus a 10px gap, so the count rule leaves 70px of dead space after
        the first, and 255px by the fifth — and then the sixth is CLAMPED to the bottom edge,
        because 5 x 0.22 is already past 1. Every widget after the fifth asks for the bottom of the
        screen no matter what is there.

        The free-slot rule puts each one under the last, which is where a person would have put it.
        """
        rows = self._grow(6, "count")
        gaps = [rows[i + 1]["y"] - (rows[i]["y"] + rows[i]["h"]) for i in range(len(rows) - 1)]
        self.assertTrue(any(g > 40 for g in gaps),
                        "the count rule stopped leaving gaps — has it been changed? %r" % (gaps,))

        rows = self._grow(6, "free")
        gaps = [rows[i + 1]["y"] - (rows[i]["y"] + rows[i]["h"]) for i in range(len(rows) - 1)]
        self.assertEqual(self._overlaps(rows), [], "widgets overlap")
        for g in gaps:
            self.assertLessEqual(g, 20, "a new widget opened well below the last one: %r" % (gaps,))
            self.assertGreaterEqual(g, 0, "widgets are touching or overlapping: %r" % (gaps,))

    def test_nothing_piles_up_on_the_bottom_edge(self):
        """The other half of the same rule: past the fifth widget `n * 0.22` exceeds 1 and clamps,
        so the sixth, seventh and eighth all ask for the very bottom of the desk."""
        want = [min(1, i * 0.22) for i in range(8)]
        self.assertEqual(want[5:], [1, 1, 1], "the count rule no longer clamps — re-check this test")
        rows = self._grow(8, "free")
        ys = [r["y"] for r in rows]
        self.assertEqual(len(set(ys)), len(ys), "two widgets were given the same position: %r" % (ys,))
        self.assertEqual(self._overlaps(rows), [])

    def test_it_fills_a_gap_left_by_a_removed_widget(self):
        """Appending after the last one for ever leaves a hole in the middle of the column that
        nothing ever uses — the same "weird place" complaint from the other direction."""
        rows = [{"id": "top", "type": "clock", "x": 1, "y": 0, "size": "m"},
                {"id": "far", "type": "clock", "x": 1, "y": 0.95, "size": "m"}]
        spot = self.spot(rows)
        self.assertLess(spot["y"], 0.5, "the gap between the two was skipped")

    def test_a_full_column_is_left_to_placeWidgets(self):
        """On a short desk the column really does fill up. Then the honest answer is the bottom, and
        the overflow rule — which already knows how to start a column — takes it from there."""
        rows = [{"id": "w%d" % i, "type": "clock", "x": 1, "y": i / 9.0, "size": "m"}
                for i in range(9)]
        spot = self.spot(rows, w=1280, h=700)
        self.assertEqual(spot["y"], 1)

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

    def test_what_is_coming_is_offered_alongside_today(self):
        """"Desktop widget only showing today, can it show today and tomorrow events?" — and it is
        the more useful shape anyway: what you want from a glance is whether the next thing is in an
        hour or on Thursday. Bounded, because the panel is a glance and not an agenda."""
        occ = ([{"title": "standup", "start": "2026-03-04T09:00:00"}]
               + [{"title": f"e{i}", "start": f"2026-03-0{i}T09:00:00"} for i in range(5, 9)])
        r = self.split(occ, "2026-03-04T08:00:00")
        self.assertEqual([t for t, _ in r["today"]], ["standup"])
        self.assertEqual(r["later"], ["e5", "e6", "e7", "e8"],
                         "today's events no longer suppress what is coming")

    def test_the_upcoming_list_is_bounded(self):
        occ = [{"title": f"e{i}", "start": "2026-03-%02dT09:00:00" % i} for i in range(5, 20)]
        r = self.split(occ, "2026-03-04T09:00:00")
        self.assertLessEqual(len(r["later"]), 6, "a busy fortnight would fill the whole desktop")

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


@unittest.skipUnless(shutil.which("node"), "node not installed")
class StickyNoteRefresh(unittest.TestCase):
    """The sticky note showed whatever it said when it was drawn, for ever.

    Reported as "windows app not updating note contents on the desktop widget. same for tablet and
    laptop" — every device, because it was never a platform bug: `mount` reads the text once and
    `refresh` was an empty function. Edit the note in the Notes app, or on another device, and the
    paper kept the old text until the page was reloaded.

    These are source assertions: the widget's refresh needs a live DOM, a Notes library and a desktop
    document to be worth driving, and what regresses here is the RULE.
    """

    def setUp(self):
        self.os_js = OS_JS.read_text(encoding="utf-8")
        self.notes = (ROOT / "static" / "js" / "client" / "notes.js").read_text(encoding="utf-8")

    def _note_widget(self):
        i = self.os_js.index("    note: {")
        return self.os_js[i:self.os_js.index("\n    },\n", self.os_js.index("refresh(el, w){", i))]

    def test_it_refreshes_at_all(self):
        w = self._note_widget()
        self.assertNotIn("refresh(){}", w, "the sticky note still never refreshes")
        self.assertIn("every: 20000", w,
                      "it has no interval, so refresh would only run when something else redrew it")

    def test_it_prefers_the_real_note_over_its_own_copy(self):
        """The Notes library is where another device's edit actually lands."""
        w = self._note_widget()
        self.assertIn("N.get(w.cfg.noteId)", w)
        self.assertIn("PCNotes", w)
        self.assertIn("get(id){", self.notes, "PCNotes exposes no way to read one note")

    def test_reading_a_note_does_not_hydrate_the_whole_notebook(self):
        """The caller is a square of paper on a screen that has nothing to do with Notes."""
        i = self.notes.index("get(id){")
        body = self.notes[i:i + 400]
        self.assertIn("if(!_lib", body, "get() would load the library to answer")
        self.assertNotIn("await", body)

    def test_a_refresh_never_eats_what_is_being_typed(self):
        """A refresh landing mid-sentence that replaced the textarea with the last SAVED text would
        eat whatever is inside the 1.2s debounce."""
        w = self._note_widget()
        self.assertIn("document.activeElement === ta", w)
        self.assertIn("ta.dataset.typing === '1'", w)
        # …and the flag has to be SET while a write is pending, or the guard is decorative.
        self.assertIn("ta.dataset.typing = '1'", self.os_js)
        self.assertIn("delete ta.dataset.typing;", self.os_js)

    def test_a_later_row_is_labelled_with_its_day(self):
        """Sitting under today's rows, an unlabelled one reads as today's."""
        src = OS_JS.read_text(encoding="utf-8")
        i = src.index("const laterRows = later.map")
        assert "_calDayLabel(o.start, now)" in src[i:i + 400]

    def test_tomorrow_is_called_tomorrow(self):
        """"Thu" is ambiguous the moment it is more than a week out, and useless for the day
        everybody actually asks about."""
        out = _node("""
        const now = new Date(2026, 2, 4, 9, 0);
        const d = (n, h) => new Date(2026, 2, 4 + n, h || 9);
        console.log(JSON.stringify([0, 1, 3, 9].map(n => PCOS.__calDayLabel(d(n), now))));""")
        self.assertEqual(out[0], "Tomorrow", "today's own label should not appear, but must not throw")
        self.assertEqual(out[1], "Tomorrow")
        self.assertNotIn(out[2], ("Tomorrow",))
        self.assertNotEqual(out[3], out[2], "a date nine days out reads as a weekday")
