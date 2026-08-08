"""Calendar alarms → reminders.

Run: venv-unified/bin/python -m unittest tests.test_calendar_notify

A VALARM is a user saying "warn me 15 minutes before", and nothing was acting on them. What this
pins is the arithmetic between the calendar and the reminder row, plus the three ways a notifier
turns into a nuisance:

  * scheduling anything in the PAST — importing ten years of history must not deliver ten years of
    alarms;
  * scheduling the same alarm twice, which is what a poller does by definition unless it dedups;
  * reading an absolute TRIGGER as an offset, which fires at a wildly wrong time.
"""
import unittest
from datetime import datetime, timedelta, timezone

from app.services import calendar_notify_service as C


def ev(dtstart="20260810T140000Z", summary="Dentist", alarm="-PT15M", extra="", dtp=""):
    a = (f"BEGIN:VALARM\r\nTRIGGER:{alarm}\r\nACTION:DISPLAY\r\nEND:VALARM\r\n") if alarm else ""
    return (f"BEGIN:VEVENT\r\nUID:e1\r\nDTSTART{dtp}:{dtstart}\r\nSUMMARY:{summary}\r\n"
            f"{extra}{a}END:VEVENT\r\n")


class TriggerTests(unittest.TestCase):
    def test_a_lead_time_is_read_as_time_before_the_start(self):
        self.assertEqual(C._triggers(ev(alarm="-PT15M")), [timedelta(minutes=15)])
        self.assertEqual(C._triggers(ev(alarm="-PT1H")), [timedelta(hours=1)])
        self.assertEqual(C._triggers(ev(alarm="-P1D")), [timedelta(days=1)])
        self.assertEqual(C._triggers(ev(alarm="-PT1H30M")), [timedelta(hours=1, minutes=30)])

    def test_a_positive_trigger_is_after_the_start(self):
        self.assertEqual(C._triggers(ev(alarm="PT10M")), [timedelta(minutes=-10)])

    def test_an_absolute_trigger_is_ignored_rather_than_guessed_at(self):
        # Reading an instant as an offset fires the alarm years out. Better to say nothing.
        comp = ("BEGIN:VEVENT\r\nUID:x\r\nDTSTART:20260810T140000Z\r\nSUMMARY:X\r\n"
                "BEGIN:VALARM\r\nTRIGGER;VALUE=DATE-TIME:20260810T133000Z\r\nEND:VALARM\r\n"
                "END:VEVENT\r\n")
        self.assertEqual(C._triggers(comp), [])

    def test_several_alarms_on_one_event(self):
        comp = ("BEGIN:VEVENT\r\nUID:x\r\nDTSTART:20260810T140000Z\r\nSUMMARY:X\r\n"
                "BEGIN:VALARM\r\nTRIGGER:-PT15M\r\nEND:VALARM\r\n"
                "BEGIN:VALARM\r\nTRIGGER:-P1D\r\nEND:VALARM\r\nEND:VEVENT\r\n")
        self.assertEqual(sorted(C._triggers(comp)), [timedelta(minutes=15), timedelta(days=1)])

    def test_an_event_with_no_alarm_asks_for_nothing(self):
        self.assertEqual(C._triggers(ev(alarm="")), [])


class TimeTests(unittest.TestCase):
    def test_utc(self):
        self.assertEqual(C._parse_dt("20260810T140000Z", {}),
                         datetime(2026, 8, 10, 14, 0, tzinfo=timezone.utc))

    def test_a_named_zone_is_converted(self):
        # 10:00 in Denver in January is 17:00 UTC.
        self.assertEqual(C._parse_dt("20220109T100000", {"TZID": "America/Denver"}),
                         datetime(2022, 1, 9, 17, 0, tzinfo=timezone.utc))

    def test_an_unknown_zone_does_not_throw(self):
        # Real airline exports carry TZID:GMT-0600, which is not an IANA name. An event that cannot
        # be placed exactly must still be placed.
        self.assertIsNotNone(C._parse_dt("20260810T140000", {"TZID": "GMT-0600"}))

    def test_a_date_only_value_parses(self):
        self.assertIsNotNone(C._parse_dt("20260810", {"VALUE": "DATE"}))

    def test_nonsense_is_none_rather_than_an_exception(self):
        self.assertIsNone(C._parse_dt("not-a-date", {}))


class RecurrenceTests(unittest.TestCase):
    """Expansion is dateutil's, not a port of ical.js — these check the wiring, not dateutil."""
    FRM = datetime(2026, 8, 1, tzinfo=timezone.utc)
    TO = datetime(2026, 9, 1, tzinfo=timezone.utc)

    def _occ(self, comp):
        start = C._parse_dt(*C._prop(comp, "DTSTART"))
        return [d.strftime("%Y-%m-%d") for d in C._occurrences(comp, start, self.FRM, self.TO)]

    def test_a_one_off_inside_the_window(self):
        self.assertEqual(self._occ(ev()), ["2026-08-10"])

    def test_a_one_off_outside_the_window(self):
        self.assertEqual(self._occ(ev(dtstart="20270810T140000Z")), [])

    def test_a_weekly_series_expands(self):
        comp = ev(dtstart="20260803T090000Z", extra="RRULE:FREQ=WEEKLY;BYDAY=MO\r\n")
        self.assertEqual(self._occ(comp),
                         ["2026-08-03", "2026-08-10", "2026-08-17", "2026-08-24", "2026-08-31"])

    def test_an_exdate_cancels_one(self):
        comp = ev(dtstart="20260803T090000Z",
                  extra="RRULE:FREQ=WEEKLY;BYDAY=MO\r\nEXDATE:20260810T090000Z\r\n")
        self.assertNotIn("2026-08-10", self._occ(comp))

    def test_an_unreadable_rule_falls_back_to_the_single_start(self):
        comp = ev(extra="RRULE:FREQ=NONSENSE;;;\r\n")
        self.assertEqual(self._occ(comp), ["2026-08-10"])


class SchedulingWindowTests(unittest.TestCase):
    """The numbers that decide whether a notifier is useful or a nuisance."""

    def test_the_horizon_and_the_interval_are_a_pair(self):
        # The pass decrypts every event the user owns, so it must not run on a short interval — and
        # it does not need to, because it schedules far enough ahead for the reminder poller.
        self.assertGreaterEqual(C._POLL_SECONDS, 900,
                                "a frequent pass re-decrypts the whole calendar — the mail CPU bug")
        self.assertGreater(C._HORIZON.total_seconds(), C._POLL_SECONDS,
                           "the horizon must outrun the interval or alarms fall between passes")

    def test_nothing_in_the_past_is_ever_scheduled(self):
        self.assertGreater(C._MIN_LEAD.total_seconds(), 0)


if __name__ == "__main__":
    unittest.main()
