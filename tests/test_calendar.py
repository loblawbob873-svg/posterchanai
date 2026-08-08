"""The bundled CalDAV server — the wiring that decides whether a phone syncs or silently doesn't.

Run: venv-unified/bin/python -m unittest tests.test_calendar

No Radicale server, no relay, no database: these cover the parts that fail QUIETLY, each of which
already bit once while this was being built.

  * The auth plugin overrides `_login`, not `login`. Radicale marks `login()` @final and dispatches
    to `_login`; overriding the public one imports cleanly and then throws "takes 3 positional
    arguments but 4 were given" on the FIRST request — every CalDAV call 500s while the server looks
    perfectly healthy.
  * A password is stored as a PBKDF2 hash and compared in constant time, and a wrong one is refused.
  * The .ics helpers, which are the whole of import/export: splitting a file into components,
    finding UIDs, and NOT nesting a VCALENDAR inside a VCALENDAR (an item is stored as the client
    PUT it, i.e. already wrapped — exporting those verbatim produced a file some programs import as
    one broken entry and others refuse).
  * The Radicale configuration never sets anything under [server]: those options configure a
    listener that does not run here, and `hosts: ""` made Radicale refuse to build AT ALL, which
    took the whole app down with it.
"""
import unittest
from pathlib import Path

from app.services import caldav_store as CS
from app.services.caldav import auth as CA

ROOT = Path(__file__).resolve().parents[1]


class AuthTests(unittest.TestCase):
    def test_the_plugin_overrides_the_method_radicale_calls(self):
        """`login` is @final in Radicale; a plugin implements `_login`. Getting this wrong is a 500
        on every request, not an import error."""
        self.assertTrue(hasattr(CA.Auth, "_login"))
        self.assertIn("_login", CA.Auth.__dict__,
                      "Auth must define _login itself — overriding login() 500s on the first request")
        self.assertNotIn("login", CA.Auth.__dict__,
                         "login() is @final in Radicale and must not be overridden")

    def test_a_password_round_trips_and_a_wrong_one_does_not(self):
        h = CA.hash_password("correct horse battery staple")
        self.assertTrue(h.startswith("pbkdf2_sha256$"))
        self.assertNotIn("correct horse", h, "the password itself must not be recoverable")
        self.assertTrue(CA.verify_password("correct horse battery staple", h))
        self.assertFalse(CA.verify_password("Correct horse battery staple", h))
        self.assertFalse(CA.verify_password("", h))
        self.assertFalse(CA.verify_password("x", "not-a-hash"))

    def test_two_hashes_of_one_password_differ(self):
        """Salted, so a stolen settings table doesn't reveal that two accounts share a password."""
        self.assertNotEqual(CA.hash_password("same"), CA.hash_password("same"))


class IcsTests(unittest.TestCase):
    FILE = """BEGIN:VCALENDAR\r
VERSION:2.0\r
PRODID:-//Radicale//EN\r
BEGIN:VEVENT\r
UID:one@example\r
DTSTART:20260810T140000Z\r
SUMMARY:Dentist\r
END:VEVENT\r
BEGIN:VEVENT\r
UID:two@example\r
DTSTART:20260811T090000Z\r
SUMMARY:Standup\r
END:VEVENT\r
BEGIN:VTODO\r
UID:three@example\r
SUMMARY:Buy milk\r
END:VTODO\r
END:VCALENDAR\r
"""

    def test_a_whole_file_splits_into_its_components(self):
        parts = CS.split_ics(self.FILE)
        self.assertEqual(len(parts), 3)
        self.assertEqual([CS.uid_of(p) for p in parts],
                         ["one@example", "two@example", "three@example"])
        self.assertEqual([CS.component_of(p) for p in parts], ["VEVENT", "VEVENT", "VTODO"])

    def test_a_component_without_a_uid_is_not_addressable(self):
        self.assertEqual(CS.uid_of("BEGIN:VEVENT\nSUMMARY:x\nEND:VEVENT"), "")

    def test_export_never_nests_a_calendar_inside_a_calendar(self):
        """Items are stored exactly as a CalDAV client PUT them — which is a whole VCALENDAR each. An
        export that wraps those again is a file some programs import as one broken entry."""
        stored = [CS.wrap_ics([p]) for p in CS.split_ics(self.FILE)]      # what the store holds
        out = CS.wrap_ics(stored, "Work")
        self.assertEqual(out.count("BEGIN:VCALENDAR"), 1)
        self.assertEqual(out.count("END:VCALENDAR"), 1)
        self.assertEqual(out.count("BEGIN:VEVENT"), 2)
        self.assertEqual(out.count("BEGIN:VTODO"), 1)
        self.assertIn("X-WR-CALNAME:Work", out)

    def test_a_round_trip_keeps_every_component(self):
        again = CS.split_ics(CS.wrap_ics(CS.split_ics(self.FILE)))
        self.assertEqual(len(again), 3)
        self.assertIn("SUMMARY:Dentist", "\n".join(again))


class NestedComponentTests(unittest.TestCase):
    """A reminder is a VALARM INSIDE a VEVENT, and a timezone is VSTANDARD inside a VTIMEZONE.

    Counting every BEGIN:V… while closing only on the outer name left the parser's depth stuck above
    zero, so split_ics returned NOTHING: an import of any normal calendar file stored 0 events and an
    export came back header-only. Both looked like the data had been erased, with no error.
    """
    ICS = ("BEGIN:VCALENDAR\nVERSION:2.0\n"
           "BEGIN:VTIMEZONE\nTZID:Europe/London\nBEGIN:STANDARD\nDTSTART:19701025T020000\n"
           "END:STANDARD\nEND:VTIMEZONE\n"
           "BEGIN:VEVENT\nUID:with-alarm\nDTSTART:20260810T140000Z\nSUMMARY:Dentist\n"
           "BEGIN:VALARM\nTRIGGER:-PT15M\nACTION:DISPLAY\nEND:VALARM\nEND:VEVENT\n"
           "BEGIN:VEVENT\nUID:plain\nDTSTART:20260811T090000Z\nSUMMARY:Standup\nEND:VEVENT\n"
           "END:VCALENDAR\n")

    def test_an_event_with_a_reminder_still_parses(self):
        parts = CS.split_ics(self.ICS)
        uids = [CS.uid_of(p) for p in parts if CS.uid_of(p)]
        self.assertEqual(uids, ["with-alarm", "plain"])

    def test_the_reminder_stays_inside_its_event(self):
        parts = CS.split_ics(self.ICS)
        ev = next(p for p in parts if CS.uid_of(p) == "with-alarm")
        self.assertIn("BEGIN:VALARM", ev)
        self.assertIn("END:VALARM", ev)
        self.assertEqual(ev.count("BEGIN:VEVENT"), 1)

    def test_export_keeps_the_reminder_and_stays_flat(self):
        stored = [CS.wrap_ics([p]) for p in CS.split_ics(self.ICS)]
        out = CS.wrap_ics(stored, "Work")
        self.assertEqual(out.count("BEGIN:VCALENDAR"), 1)
        self.assertEqual(out.count("BEGIN:VEVENT"), 2)
        self.assertEqual(out.count("BEGIN:VALARM"), 1)


class TimezoneCarryTests(unittest.TestCase):
    """A VTIMEZONE has no UID, so the import dropped every one of them.

    Measured on a real 707-event Radicale export: 577 events referenced a TZID and NOT ONE of the
    definitions survived the import. `DTSTART;TZID=America/Denver:20220109T100000` with no matching
    VTIMEZONE is an invalid resource — a strict client refuses it and a lenient one reads the time as
    floating, shifting the appointment by the UTC offset. Nothing errored; the calendar just quietly
    became wrong.
    """
    FILE = ("BEGIN:VCALENDAR\nVERSION:2.0\n"
            "BEGIN:VTIMEZONE\nTZID:America/Denver\nBEGIN:STANDARD\nDTSTART:20071104T030000\n"
            "TZOFFSETFROM:-0600\nTZOFFSETTO:-0700\nEND:STANDARD\nEND:VTIMEZONE\n"
            "BEGIN:VTIMEZONE\nTZID:Europe/Helsinki\nBEGIN:STANDARD\nDTSTART:19701025T040000\n"
            "TZOFFSETFROM:+0300\nTZOFFSETTO:+0200\nEND:STANDARD\nEND:VTIMEZONE\n"
            "BEGIN:VEVENT\nUID:zoned\nDTSTART;TZID=America/Denver:20220109T100000\n"
            "SUMMARY:Vineyard Cafe\nEND:VEVENT\n"
            "BEGIN:VEVENT\nUID:utc\nDTSTART:20260811T090000Z\nSUMMARY:Standup\nEND:VEVENT\n"
            "END:VCALENDAR\n")

    def test_the_definitions_are_found(self):
        self.assertEqual(sorted(CS.timezones_of(self.FILE)), ["America/Denver", "Europe/Helsinki"])

    def test_a_reference_is_a_parameter_not_prose(self):
        self.assertEqual(CS.tzids_in("BEGIN:VEVENT\nDTSTART;TZID=America/Denver:20220109T100000\n"
                                     "DESCRIPTION:we discussed TZID=Europe/Oslo at length\n"
                                     "END:VEVENT"), {"America/Denver"})

    def test_a_folded_parameter_is_still_a_reference(self):
        # A long DTSTART wraps mid-parameter; a line-by-line scan never sees the TZID.
        self.assertEqual(CS.tzids_in("BEGIN:VEVENT\r\nDTSTART;TZ\r\n ID=America/Denver:20220109T100000"
                                     "\r\nEND:VEVENT"), {"America/Denver"})

    def test_the_stored_resource_carries_the_timezone_it_uses(self):
        tzs = CS.timezones_of(self.FILE)
        uid, comp, parts = next(r for r in CS.group_resources(self.FILE) if r[0] == "zoned")
        body = CS.wrap_ics(parts, "Main", timezones=tzs)
        self.assertIn("TZID:America/Denver", body)
        self.assertEqual(body.count("BEGIN:VEVENT"), 1)

    def test_it_carries_only_the_timezone_it_uses(self):
        # Attaching every definition to every event turns a 700-event export into a wall of
        # duplicated VTIMEZONEs.
        tzs = CS.timezones_of(self.FILE)
        uid, comp, parts = next(r for r in CS.group_resources(self.FILE) if r[0] == "zoned")
        self.assertNotIn("Europe/Helsinki", CS.wrap_ics(parts, "Main", timezones=tzs))

    def test_an_event_that_needs_no_timezone_gets_none(self):
        tzs = CS.timezones_of(self.FILE)
        uid, comp, parts = next(r for r in CS.group_resources(self.FILE) if r[0] == "utc")
        self.assertNotIn("BEGIN:VTIMEZONE", CS.wrap_ics(parts, "Main", timezones=tzs))

    def test_an_export_carries_each_definition_once(self):
        tzs = CS.timezones_of(self.FILE)
        stored = [CS.wrap_ics(p, "Main", timezones=tzs) for _, _, p in CS.group_resources(self.FILE)]
        out = CS.wrap_ics(stored, "Main")
        self.assertEqual(out.count("BEGIN:VTIMEZONE"), 1)          # deduped, not one per event
        self.assertIn("TZID:America/Denver", out)
        self.assertEqual(out.count("BEGIN:VEVENT"), 2)


class ResourceGroupingTests(unittest.TestCase):
    """Components sharing a UID are ONE resource.

    A recurring event with an edited occurrence is a master VEVENT plus a VEVENT carrying
    RECURRENCE-ID under the SAME UID. Stored one document per UID as separate items, the second write
    silently overwrites the first: the master disappears and the calendar shows a lone stray
    occurrence.
    """
    FILE = ("BEGIN:VCALENDAR\nVERSION:2.0\n"
            "BEGIN:VEVENT\nUID:weekly\nDTSTART:20260803T090000Z\nRRULE:FREQ=WEEKLY\n"
            "SUMMARY:Standup\nEND:VEVENT\n"
            "BEGIN:VEVENT\nUID:weekly\nRECURRENCE-ID:20260810T090000Z\nDTSTART:20260810T103000Z\n"
            "SUMMARY:Standup (moved)\nEND:VEVENT\n"
            "BEGIN:VTODO\nUID:task\nSUMMARY:Renew passport\nEND:VTODO\n"
            "END:VCALENDAR\n")

    def test_one_resource_per_uid(self):
        self.assertEqual([(u, k, len(p)) for u, k, p in CS.group_resources(self.FILE)],
                         [("weekly", "VEVENT", 2), ("task", "VTODO", 1)])

    def test_the_override_travels_with_its_master(self):
        _, _, parts = CS.group_resources(self.FILE)[0]
        body = CS.wrap_ics(parts, "Main")
        self.assertIn("RRULE:FREQ=WEEKLY", body)
        self.assertIn("RECURRENCE-ID:20260810T090000Z", body)
        self.assertEqual(body.count("BEGIN:VEVENT"), 2)

    def test_a_todo_is_a_resource_too(self):
        # 10 of the user's 707 items are VTODOs; keying only on VEVENT would drop them.
        self.assertIn("VTODO", [k for _, k, _ in CS.group_resources(self.FILE)])

    def test_a_timezone_is_not_a_resource(self):
        self.assertNotIn("VTIMEZONE", [k for _, k, _ in CS.group_resources(TimezoneCarryTests.FILE)])


class ImportGuardTests(unittest.TestCase):
    """A read that FAILED must never be read as "there is nothing there"."""

    def test_the_scans_can_be_strict(self):
        import inspect
        for fn in (CS.list_calendars, CS.get_items):
            self.assertIn("strict", inspect.signature(fn).parameters,
                          f"{fn.__name__} must be able to raise instead of answering empty")

    def test_the_namespace_scan_does_not_share_the_default_window(self):
        """The keyspace is shared with chat messages, and a half-read calendar is worse than a slow
        one: the reconcile treats what it did not see as deleted."""
        self.assertGreater(CS._SCAN_LIMIT, 5000)


class ConfigTests(unittest.TestCase):
    def test_nothing_configures_radicales_own_listener(self):
        """[server] configures a listener that never runs (we are mounted as WSGI), and `hosts: ""`
        makes Radicale refuse to build — which took the entire app down when it was first written."""
        src = (ROOT / "app/services/caldav/server.py").read_text()
        # The KEYS, not the prose: the comment above them explains exactly this and must stay.
        self.assertNotIn('"server":', src)
        self.assertNotIn('"hosts":', src)

    def test_a_calendar_problem_cannot_stop_the_app_from_starting(self):
        """The mount is wrapped, and the handler must use a logger that EXISTS at import time — the
        first version called an undefined `logger`, so the guard raised NameError and every request
        502'd."""
        src = (ROOT / "app/main.py").read_text()
        self.assertIn("except Exception as _caldav_err", src)
        self.assertIn("logging.getLogger(__name__).warning", src)

    def test_the_server_is_off_until_an_admin_turns_it_on(self):
        from app.schemas import SettingsResponse
        self.assertIn("caldav_enabled", SettingsResponse.model_fields)
        self.assertIs(SettingsResponse.model_fields["caldav_enabled"].default, False)


if __name__ == "__main__":
    unittest.main()
