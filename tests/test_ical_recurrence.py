"""Recurrence expansion — run the SHIPPED parser (static/js/client/ical.js) under node.

Run: venv-unified/bin/python -m unittest tests.test_ical_recurrence

Why a node harness rather than assertions about strings: the month grid only ever placed DTSTART, so
59 of one real calendar's 707 events — every weekly delivery, every birthday — drew exactly once and
the calendar looked half-empty with nothing in any log. Recurrence is all edge cases (a "last
Friday", a series that stopped in 2024, an occurrence dragged to another hour), and the only way to
know it is right is to expand real rules and check the dates that come out.

The rules below are taken verbatim from that calendar.
"""
import json
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ICAL = ROOT / "static" / "js" / "client" / "ical.js"


def _node(script: str):
    """Run a snippet with PCIcal loaded; return whatever it JSON-prints."""
    src = f"const I = require({json.dumps(str(ICAL))});\n{script}"
    out = subprocess.run(["node", "-e", src], capture_output=True, timeout=60)
    if out.returncode != 0:
        raise AssertionError(out.stderr.decode()[-2000:])
    return json.loads(out.stdout.decode() or "null")


def _expand(ics: str, frm: str, to: str):
    """Expand one stored resource over [frm, to) and return the occurrence day keys."""
    return _node(f"""
      const res = I.parseResource({{ uid:'u', cal:'c', ics: {json.dumps(ics)} }});
      const occ = I.occurrences(res, new Date({json.dumps(frm)} + 'T00:00:00'),
                                     new Date({json.dumps(to)} + 'T00:00:00'));
      console.log(JSON.stringify(occ.map(o => [o.key, o.title])));
    """)


def _wrap(*body):
    return "BEGIN:VCALENDAR\r\nVERSION:2.0\r\n" + "".join(body) + "END:VCALENDAR\r\n"


EV = ("BEGIN:VEVENT\r\nUID:{uid}\r\nDTSTART{dtp}:{dt}\r\nSUMMARY:{sum}\r\n{extra}END:VEVENT\r\n")


def ev(uid="e1", dt="20260803T090000Z", dtp="", summary="Standup", **kw):
    extra = "".join(f"{k.upper().replace('_', '-')}:{v}\r\n" for k, v in kw.items() if v)
    return EV.format(uid=uid, dt=dt, dtp=dtp, sum=summary, extra=extra)


@unittest.skipIf(shutil.which("node") is None, "node not installed")
class WeeklyTests(unittest.TestCase):
    """`FREQ=WEEKLY;WKST=SU;BYDAY=FR` — the user's "Food Delivery @ YummyThai", running since 2025.

    Before this, it drew on 16 Sep 2025 and never again.
    """
    ICS = _wrap(ev(dt="20250916T110000Z", summary="Food Delivery",
                   rrule="FREQ=WEEKLY;WKST=SU;BYDAY=FR"))

    def test_it_lands_on_every_friday_of_the_window(self):
        got = [k for k, _ in _expand(self.ICS, "2026-08-01", "2026-09-01")]
        self.assertEqual(got, ["2026-08-07", "2026-08-14", "2026-08-21", "2026-08-28"])

    def test_it_does_not_start_before_the_series_does(self):
        self.assertEqual(_expand(self.ICS, "2025-08-01", "2025-09-30"),
                         [["2025-09-19", "Food Delivery"], ["2025-09-26", "Food Delivery"]])

    def test_a_series_that_ended_stops(self):
        # "Pepsi Delivery": FREQ=WEEKLY;BYDAY=WE;UNTIL=20230329T130000Z
        ics = _wrap(ev(dt="20220420T070000Z", summary="Pepsi",
                       rrule="FREQ=WEEKLY;BYDAY=WE;UNTIL=20230329T130000Z"))
        self.assertEqual(_expand(ics, "2026-08-01", "2026-09-01"), [])
        self.assertTrue(_expand(ics, "2023-03-01", "2023-04-01"))

    def test_the_last_occurrence_is_included_and_the_next_is_not(self):
        ics = _wrap(ev(dt="20220420T070000Z", rrule="FREQ=WEEKLY;BYDAY=WE;UNTIL=20230329T130000Z"))
        got = [k for k, _ in _expand(ics, "2023-03-01", "2023-04-30")]
        self.assertEqual(got[-1], "2023-03-29")

    def test_interval_skips_weeks(self):
        ics = _wrap(ev(dt="20260803T090000Z", rrule="FREQ=WEEKLY;INTERVAL=2;BYDAY=MO"))
        self.assertEqual([k for k, _ in _expand(ics, "2026-08-01", "2026-09-15")],
                         ["2026-08-03", "2026-08-17", "2026-08-31", "2026-09-14"])

    def test_several_days_a_week(self):
        ics = _wrap(ev(dt="20260803T090000Z", rrule="FREQ=WEEKLY;BYDAY=MO,WE,FR"))
        self.assertEqual([k for k, _ in _expand(ics, "2026-08-03", "2026-08-10")],
                         ["2026-08-03", "2026-08-05", "2026-08-07"])


@unittest.skipIf(shutil.which("node") is None, "node not installed")
class YearlyTests(unittest.TestCase):
    """"Ryan's Birthday": FREQ=YEARLY;BYMONTHDAY=24;BYMONTH=11;UNTIL=20241124…"""

    def test_a_birthday_repeats_once_a_year(self):
        ics = _wrap(ev(dt="20141124T090000Z", summary="Ryan's Birthday",
                       rrule="FREQ=YEARLY;BYMONTHDAY=24;BYMONTH=11"))
        self.assertEqual([k for k, _ in _expand(ics, "2026-01-01", "2027-01-01")], ["2026-11-24"])

    def test_bymonth_keeps_it_out_of_other_months(self):
        ics = _wrap(ev(dt="20141124T090000Z", rrule="FREQ=YEARLY;BYMONTHDAY=24;BYMONTH=11"))
        self.assertEqual(_expand(ics, "2026-10-01", "2026-11-01"), [])

    def test_a_birthday_that_stopped_stays_stopped(self):
        ics = _wrap(ev(dt="20141124T090000Z",
                       rrule="FREQ=YEARLY;BYMONTHDAY=24;BYMONTH=11;UNTIL=20241124T235959Z"))
        self.assertEqual(_expand(ics, "2026-01-01", "2027-01-01"), [])


@unittest.skipIf(shutil.which("node") is None, "node not installed")
class MonthlyTests(unittest.TestCase):
    def test_the_last_friday_of_the_month(self):
        ics = _wrap(ev(dt="20260828T090000Z", rrule="FREQ=MONTHLY;BYDAY=-1FR"))
        self.assertEqual([k for k, _ in _expand(ics, "2026-08-01", "2026-11-01")],
                         ["2026-08-28", "2026-09-25", "2026-10-30"])

    def test_the_second_tuesday(self):
        ics = _wrap(ev(dt="20260811T090000Z", rrule="FREQ=MONTHLY;BYDAY=2TU"))
        self.assertEqual([k for k, _ in _expand(ics, "2026-08-01", "2026-10-01")],
                         ["2026-08-11", "2026-09-08"])

    def test_a_31st_skips_short_months(self):
        # RFC 5545: no 31st in September, so the occurrence is skipped, not moved to the 30th.
        ics = _wrap(ev(dt="20260831T090000Z", rrule="FREQ=MONTHLY"))
        self.assertEqual([k for k, _ in _expand(ics, "2026-08-01", "2026-11-01")],
                         ["2026-08-31", "2026-10-31"])

    def test_count_stops_the_series(self):
        ics = _wrap(ev(dt="20260803T090000Z", rrule="FREQ=MONTHLY;COUNT=3"))
        self.assertEqual([k for k, _ in _expand(ics, "2026-01-01", "2027-06-01")],
                         ["2026-08-03", "2026-09-03", "2026-10-03"])


@unittest.skipIf(shutil.which("node") is None, "node not installed")
class DailyTests(unittest.TestCase):
    def test_daily_fills_the_window(self):
        ics = _wrap(ev(dt="20260801T090000Z", rrule="FREQ=DAILY"))
        self.assertEqual(len(_expand(ics, "2026-08-01", "2026-08-08")), 7)

    def test_a_daily_series_from_years_ago_is_still_cheap(self):
        # Stepping from DTSTART would be ~5500 iterations per repaint, per event.
        ics = _wrap(ev(dt="20110101T090000Z", rrule="FREQ=DAILY;INTERVAL=3"))
        got = [k for k, _ in _expand(ics, "2026-08-01", "2026-08-10")]
        self.assertEqual(len(got), 3)
        self.assertTrue(all(k.startswith("2026-08-") for k in got))


@unittest.skipIf(shutil.which("node") is None, "node not installed")
class ExceptionAndOverrideTests(unittest.TestCase):
    """EXDATE and RECURRENCE-ID: the two ways a single occurrence differs from its series."""

    def test_an_excluded_occurrence_does_not_draw(self):
        ics = _wrap(ev(dt="20260803T090000Z", rrule="FREQ=WEEKLY;BYDAY=MO",
                       exdate="20260810T090000Z"))
        self.assertEqual([k for k, _ in _expand(ics, "2026-08-01", "2026-08-25")],
                         ["2026-08-03", "2026-08-17", "2026-08-24"])

    def test_an_edited_occurrence_uses_its_own_title(self):
        ics = _wrap(ev(uid="w", dt="20260803T090000Z", summary="Standup",
                       rrule="FREQ=WEEKLY;BYDAY=MO"),
                    ev(uid="w", dt="20260810T103000Z", summary="Standup (moved)",
                       recurrence_id="20260810T090000Z"))
        got = dict(_expand(ics, "2026-08-01", "2026-08-18"))
        self.assertEqual(got["2026-08-10"], "Standup (moved)")
        self.assertEqual(got["2026-08-03"], "Standup")

    def test_an_occurrence_moved_to_another_day_moves(self):
        ics = _wrap(ev(uid="w", dt="20260803T090000Z", rrule="FREQ=WEEKLY;BYDAY=MO"),
                    ev(uid="w", dt="20260812T090000Z", summary="Moved to Wednesday",
                       recurrence_id="20260810T090000Z"))
        keys = [k for k, _ in _expand(ics, "2026-08-09", "2026-08-16")]
        self.assertIn("2026-08-12", keys)
        self.assertNotIn("2026-08-10", keys)

    def test_the_master_does_not_draw_twice(self):
        ics = _wrap(ev(uid="w", dt="20260803T090000Z", rrule="FREQ=WEEKLY;BYDAY=MO"),
                    ev(uid="w", dt="20260810T103000Z", recurrence_id="20260810T090000Z"))
        keys = [k for k, _ in _expand(ics, "2026-08-10", "2026-08-11")]
        self.assertEqual(keys, ["2026-08-10"])


@unittest.skipIf(shutil.which("node") is None, "node not installed")
class PlainEventTests(unittest.TestCase):
    def test_a_one_off_appears_once(self):
        ics = _wrap(ev(dt="20260810T140000Z", summary="Dentist"))
        self.assertEqual(_expand(ics, "2026-08-01", "2026-09-01"), [["2026-08-10", "Dentist"]])

    def test_an_all_day_event_stays_on_its_date(self):
        ics = _wrap(ev(dt="20260810", dtp=";VALUE=DATE", summary="Holiday"))
        self.assertEqual(_expand(ics, "2026-08-01", "2026-09-01"), [["2026-08-10", "Holiday"]])

    def test_a_zoned_time_is_read_in_its_own_zone(self):
        # DTSTART;TZID=America/Denver:20220109T100000 is 10:00 in Denver, whatever the browser's zone.
        got = _node("""
          const d = I.fromZoned(2022, 1, 9, 10, 0, 0, 'America/Denver');
          console.log(JSON.stringify(d.toISOString()));
        """)
        self.assertEqual(got, "2022-01-09T17:00:00.000Z")      # MST is UTC-7 in January

    def test_an_unknown_zone_falls_back_instead_of_throwing(self):
        # 3 of the user's events carry TZID:GMT-0600, which their source file never defines and
        # which is not an IANA name. Intl throws on it; the event must still draw.
        ics = _wrap(ev(dt="20260810T140000", dtp=";TZID=GMT-0600", summary="Flight"))
        self.assertEqual([k for k, _ in _expand(ics, "2026-08-01", "2026-09-01")], ["2026-08-10"])

    def test_a_description_mentioning_tzid_is_not_a_reference(self):
        got = _node("""
          console.log(JSON.stringify(I.props(
            'BEGIN:VEVENT\\nDESCRIPTION:we discussed TZID=Europe/Oslo\\nEND:VEVENT')
            .find(p => p.name === 'DESCRIPTION').value));
        """)
        self.assertEqual(got, "we discussed TZID=Europe/Oslo")

    def test_a_folded_line_is_unfolded_before_parsing(self):
        ics = _wrap("BEGIN:VEVENT\r\nUID:f\r\nDTSTART:20260810T140000Z\r\n"
                    "SUMMARY:A very long title that the\r\n  exporter wrapped\r\nEND:VEVENT\r\n")
        self.assertEqual(_expand(ics, "2026-08-01", "2026-09-01"),
                         [["2026-08-10", "A very long title that the exporter wrapped"]])


if __name__ == "__main__":
    unittest.main()


CAL_JS = ROOT / "static" / "js" / "client" / "calendar.js"


def _build(stored_ics, form):
    """Run calendar.js's OWN rawSeries + buildIcs over a stored item and a form submission.

    calendar.js is an IIFE that needs window/__PC, so the two functions are lifted out of the source
    by name and evaluated on their own. Extracting them keeps this honest: the strings under test are
    the shipped ones, not a copy that can drift.
    """
    src = CAL_JS.read_text()

    def grab(start_marker, end_marker):
        i = src.index(start_marker)
        j = src.index(end_marker, i)
        return src[i:j]

    raw_fn = grab("    const MANAGED = [", "\n    /* The occurrences of every calendar")
    build_fn = grab("    function buildIcs(ev){", "\n    /* Reading iCalendar is PCIcal's job")
    helpers = """
      const pad = n => String(n).padStart(2, '0');
      const icsUtc = d => `${d.getUTCFullYear()}${pad(d.getUTCMonth()+1)}${pad(d.getUTCDate())}T`
                        + `${pad(d.getUTCHours())}${pad(d.getUTCMinutes())}00Z`;
      const icsDate = d => `${d.getFullYear()}${pad(d.getMonth()+1)}${pad(d.getDate())}`;
      const icsText = s => String(s||'').replace(/\\\\/g,'\\\\\\\\').replace(/;/g,'\\\;')
                                        .replace(/,/g,'\\\\,').replace(/\\r?\\n/g,'\\\\n');
      const crypto = { randomUUID: () => 'generated-uid' };
    """
    return _node(f"""
      globalThis.window = {{ PCIcal: I }};
      {helpers}
      {raw_fn}
      {build_fn}
      const raw = rawSeries({json.dumps(stored_ics)});
      const form = Object.assign({json.dumps(form)}, {{ raw }});
      console.log(JSON.stringify(buildIcs(form).ics));
    """)


@unittest.skipIf(shutil.which("node") is None, "node not installed")
class EditorPreservationTests(unittest.TestCase):
    """Saving an edit must not delete the parts of an event this form has no field for.

    The editor has eight fields; a real event carries far more. An earlier version kept only the
    repeat rule, so changing a title silently deleted VALARM reminders (200 of one real 707-event
    calendar have one), ATTENDEE/ORGANIZER, STATUS, CATEGORIES, URL and every X- property — and
    rewrote a VTODO as a VEVENT. It still looked right on this screen and lost half of itself on
    every synced device.
    """
    STORED = ("BEGIN:VCALENDAR\r\nVERSION:2.0\r\n"
              "BEGIN:VTIMEZONE\r\nTZID:America/Denver\r\nBEGIN:STANDARD\r\n"
              "DTSTART:20071104T030000\r\nTZOFFSETFROM:-0600\r\nTZOFFSETTO:-0700\r\n"
              "END:STANDARD\r\nEND:VTIMEZONE\r\n"
              "BEGIN:VEVENT\r\nUID:party\r\nDTSTART;TZID=America/Denver:20260810T100000\r\n"
              "DTEND;TZID=America/Denver:20260810T110000\r\nSUMMARY:Party\r\n"
              "RRULE:FREQ=WEEKLY;BYDAY=MO\r\nEXDATE:20260817T100000\r\n"
              "ORGANIZER;CN=Someone:mailto:someone@example.com\r\n"
              "ATTENDEE;CN=Guest:mailto:guest@example.com\r\n"
              "STATUS:CONFIRMED\r\nCATEGORIES:Personal\r\nURL:https://example.com/party\r\n"
              "X-MICROSOFT-CDO-BUSYSTATUS:BUSY\r\n"
              "BEGIN:VALARM\r\nTRIGGER:-PT15M\r\nACTION:DISPLAY\r\nDESCRIPTION:Reminder\r\n"
              "END:VALARM\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n")
    FORM = {"uid": "party", "title": "Party (moved)", "date": "2026-08-11",
            "allDay": False, "start": "12:00", "end": "13:00", "location": "", "notes": ""}

    def setUp(self):
        self.out = _build(self.STORED, self.FORM)

    def test_the_edit_is_applied(self):
        self.assertIn("SUMMARY:Party (moved)", self.out)

    def test_the_reminder_survives(self):
        self.assertIn("BEGIN:VALARM", self.out)
        self.assertIn("TRIGGER:-PT15M", self.out)
        self.assertIn("END:VALARM", self.out)

    def test_the_repeat_rule_and_its_exception_survive(self):
        self.assertIn("RRULE:FREQ=WEEKLY;BYDAY=MO", self.out)
        self.assertIn("EXDATE:20260817T100000", self.out)

    def test_the_people_survive(self):
        self.assertIn("ORGANIZER;CN=Someone:mailto:someone@example.com", self.out)
        self.assertIn("ATTENDEE;CN=Guest:mailto:guest@example.com", self.out)

    def test_the_odds_and_ends_survive(self):
        for line in ("STATUS:CONFIRMED", "CATEGORIES:Personal", "URL:https://example.com/party",
                     "X-MICROSOFT-CDO-BUSYSTATUS:BUSY"):
            self.assertIn(line, self.out)

    def test_the_timezone_table_survives(self):
        self.assertIn("TZID:America/Denver", self.out)

    def test_the_old_times_are_replaced_not_duplicated(self):
        # The form owns DTSTART/DTEND; keeping the originals too would give the event two of each.
        self.assertEqual(self.out.count("DTSTART"), 1 + self.out.count("BEGIN:VTIMEZONE"))
        self.assertEqual(self.out.count("DTEND"), 1)
        self.assertIn("SUMMARY:Party (moved)", self.out)
        self.assertNotIn("SUMMARY:Party\r", self.out)

    def test_a_todo_stays_a_todo(self):
        stored = ("BEGIN:VCALENDAR\r\nVERSION:2.0\r\nBEGIN:VTODO\r\nUID:t1\r\n"
                  "DTSTART:20260810T090000Z\r\nSUMMARY:Renew passport\r\nPERCENT-COMPLETE:40\r\n"
                  "END:VTODO\r\nEND:VCALENDAR\r\n")
        out = _build(stored, dict(self.FORM, uid="t1", title="Renew passport now"))
        self.assertIn("BEGIN:VTODO", out)
        self.assertNotIn("BEGIN:VEVENT", out)
        self.assertIn("PERCENT-COMPLETE:40", out)
        self.assertIn("DUE:", out)          # a VTODO has DUE, never DTEND
        self.assertNotIn("DTEND", out)
