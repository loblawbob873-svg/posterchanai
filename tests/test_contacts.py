"""Contacts — the CardDAV half of the bundled server.

Run: venv-unified/bin/python -m unittest tests.test_contacts

Calendars and addressbooks share one namespace and one storage plugin, which is what makes hydration
a single pass — and what makes the two ways of getting it wrong silent:

  * A collection written before addressbooks existed has no `kind` field. If that defaulted to
    anything but a calendar, EVERY existing calendar would vanish from the calendar UI at once, with
    the data still on the relay and nothing in any log.
  * The reconcile has to pick the file extension and the Radicale tag from the collection's kind. A
    vCard written into `<uid>.ics` inside a collection announcing itself as a VCALENDAR gives a phone
    an addressbook with no contacts and a calendar it cannot parse.

The vCard helpers are the whole of import/export, and the property that matters is that a card
round-trips byte for byte: a real addressbook carries base64 photos, Apple-style grouped labels and
X-* fields nobody else understands, and an import that quietly normalises them loses them.
"""
import unittest
from pathlib import Path

from app.services import caldav_store as CS
from app.services.caldav import storage as ST

ROOT = Path(__file__).resolve().parents[1]


CARD = ("BEGIN:VCARD\r\n"
        "VERSION:3.0\r\n"
        "PRODID:+//IDN bitfire.at//DAVx5/4.5.9-ose ez-vcard/0.12.1\r\n"
        "UID:a109a067-8f7e-479e-957c-7fc271adf531\r\n"
        "FN:Fire DEPARTMENT\r\n"
        "N:DEPARTMENT;Fire;;;\r\n"
        "REV:2026-02-14T07:30:33Z\r\n"
        "TEL;TYPE=cell:7192758666\r\n"
        "END:VCARD\r\n")

PHOTO_CARD = ("BEGIN:VCARD\r\nVERSION:3.0\r\nUID:with-photo\r\nFN:Has Photo\r\n"
              "PHOTO;ENCODING=b;TYPE=JPEG:/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAAgGBgcGBQgHBwcJ\r\n"
              " CQkKDBQNDAsLDBkSEw8UHRofHh0aHBwgJC4nICIsIxwcKDcpLDAxNDQ0Hyc5PTgyPC4zNDL/\r\n"
              " 2wBDAQkJCQwLDBgNDRgyIRwhMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIy\r\n"
              "END:VCARD\r\n")

GROUPED = ("BEGIN:VCARD\r\nVERSION:3.0\r\nUID:grouped\r\nFN:Labelled\r\n"
           "item1.EMAIL;TYPE=INTERNET:someone@example.com\r\n"
           "item1.X-ABLABEL:School\r\n"
           "END:VCARD\r\n")


class KindTests(unittest.TestCase):
    def test_a_collection_with_no_kind_is_a_calendar(self):
        # Every calendar that existed before addressbooks did. Getting this wrong hides all of them.
        self.assertEqual(CS.kind_of({"id": "main", "displayname": "Main"}), CS.KIND_CALENDAR)
        self.assertEqual(CS.kind_of({}), CS.KIND_CALENDAR)
        self.assertEqual(CS.kind_of(None), CS.KIND_CALENDAR)

    def test_an_addressbook_is_recognised_however_it_is_cased(self):
        self.assertEqual(CS.kind_of({"kind": "vaddressbook"}), CS.KIND_ADDRESSBOOK)
        self.assertEqual(CS.kind_of({"kind": "VADDRESSBOOK"}), CS.KIND_ADDRESSBOOK)

    def test_an_unknown_kind_is_a_calendar_rather_than_nothing(self):
        # Fail towards the type whose UI can display it; a collection in neither list is invisible.
        self.assertEqual(CS.kind_of({"kind": "VJOURNAL"}), CS.KIND_CALENDAR)


class ReconcilePropsTests(unittest.TestCase):
    def test_a_calendar_gets_ics_files_and_a_vcalendar_tag(self):
        props, ext = ST.collection_props({"displayname": "Main"}, "main")
        self.assertEqual(ext, ".ics")
        self.assertEqual(props["tag"], "VCALENDAR")
        self.assertEqual(props["D:displayname"], "Main")

    def test_an_addressbook_gets_vcf_files_and_a_vaddressbook_tag(self):
        props, ext = ST.collection_props({"displayname": "Contacts", "kind": "VADDRESSBOOK"},
                                         "contacts")
        self.assertEqual(ext, ".vcf")
        self.assertEqual(props["tag"], "VADDRESSBOOK")

    def test_the_id_stands_in_for_a_missing_name(self):
        props, _ = ST.collection_props({}, "contacts")
        self.assertEqual(props["D:displayname"], "contacts")

    def test_a_colour_is_carried_but_never_invented(self):
        props, _ = ST.collection_props({"color": "#879843"}, "c")
        self.assertEqual(props["ICAL:calendar-color"], "#879843")
        self.assertNotIn("ICAL:calendar-color", ST.collection_props({}, "c")[0])


class VcardTests(unittest.TestCase):
    def test_a_file_splits_into_cards(self):
        self.assertEqual(len(CS.split_vcards(CARD + PHOTO_CARD + GROUPED)), 3)

    def test_a_card_is_addressable_by_its_uid(self):
        self.assertEqual(CS.uid_of_vcard(CARD), "a109a067-8f7e-479e-957c-7fc271adf531")
        self.assertEqual(CS.fn_of_vcard(CARD), "Fire DEPARTMENT")

    def test_a_card_without_a_uid_says_so(self):
        self.assertEqual(CS.uid_of_vcard("BEGIN:VCARD\r\nFN:Nobody\r\nEND:VCARD\r\n"), "")

    def test_a_grouped_property_is_not_mistaken_for_the_uid(self):
        # `item1.UID` is still a UID; `item1.X-ABLABEL` is not.
        self.assertEqual(CS.uid_of_vcard("BEGIN:VCARD\r\nitem1.UID:xyz\r\nEND:VCARD\r\n"), "xyz")
        self.assertEqual(CS.uid_of_vcard(GROUPED), "grouped")

    def test_a_folded_photo_stays_with_its_card(self):
        cards = CS.split_vcards(PHOTO_CARD)
        self.assertEqual(len(cards), 1)
        self.assertEqual(cards[0].count("\n"), PHOTO_CARD.strip().count("\n"))
        self.assertIn("2wBDAQkJCQwLDBgNDRgyIRwhMjIyMjIy", cards[0])

    def test_the_round_trip_is_byte_for_byte(self):
        src = CARD + PHOTO_CARD + GROUPED
        again = CS.split_vcards(CS.wrap_vcards(CS.split_vcards(src)))
        norm = lambda s: s.replace("\r\n", "\n").strip()
        self.assertEqual([norm(c) for c in again], [norm(c) for c in CS.split_vcards(src)])

    def test_an_export_is_a_bare_concatenation(self):
        # A .vcf has no envelope. Wrapping it in anything produces a file no client reads.
        out = CS.wrap_vcards(CS.split_vcards(CARD + GROUPED))
        self.assertTrue(out.startswith("BEGIN:VCARD"))
        self.assertEqual(out.count("BEGIN:VCARD"), 2)
        self.assertNotIn("VCALENDAR", out)

    def test_an_empty_book_exports_an_empty_file_not_a_broken_one(self):
        self.assertEqual(CS.wrap_vcards([]), "")

    def test_a_card_already_wrapped_is_not_nested(self):
        self.assertEqual(CS.wrap_vcards([CARD + PHOTO_CARD]).count("BEGIN:VCARD"), 2)



class IdSpaceTests(unittest.TestCase):
    """Calendars and addressbooks share ONE id space, because the id is the directory name under the
    CalDAV root. Every "is this id free?" check therefore has to ask about both kinds.

    Asking about only its own kind finds no collision, and the metadata write that follows replaces
    the other collection's `pcai:calmeta:<id>` document — converting an addressbook into a calendar
    (it vanishes from Contacts, its vCards become calendar items, and deleting that calendar deletes
    every contact in it), or the reverse.
    """
    def test_a_free_id_is_used_as_is(self):
        self.assertEqual(CS.free_id("contacts", {}), "contacts")

    def test_a_taken_id_is_suffixed(self):
        self.assertEqual(CS.free_id("contacts", {"contacts": CS.KIND_CALENDAR}), "contacts-2")

    def test_it_keeps_counting_past_a_taken_suffix(self):
        taken = {"c": CS.KIND_CALENDAR, "c-2": CS.KIND_ADDRESSBOOK, "c-3": CS.KIND_CALENDAR}
        self.assertEqual(CS.free_id("c", taken), "c-4")

    def test_it_accepts_a_plain_set_too(self):
        self.assertEqual(CS.free_id("c", {"c"}), "c-2")


class RouterIdChecksTests(unittest.TestCase):
    """The routers must ask `collection_kinds` (both kinds), never `list_calendars`/`list_addressbooks`.

    Read as source rather than exercised, because the failure is a collision that is NOT detected —
    there is nothing to observe at the call site, and the damage only appears later as a collection
    that changed type.
    """
    def _src(self, name):
        return (ROOT / "app" / "routers" / name).read_text()

    def test_the_calendar_router_checks_both_kinds(self):
        src = self._src("calendar.py")
        self.assertIn("collection_kinds", src)
        self.assertNotIn("list_calendars(db, current_user, strict=True)", src)

    def test_the_contacts_router_checks_both_kinds(self):
        src = self._src("contacts.py")
        self.assertIn("collection_kinds", src)
        self.assertNotIn("list_addressbooks(db, user, strict=True)", src)

    def test_the_storage_hydrate_reconciles_both_kinds(self):
        # list_calendars here meant no addressbook was ever written to disk (CardDAV discovery
        # returned nothing) and _drop_missing deleted the directory a phone had created.
        src = (ROOT / "app" / "services" / "caldav" / "storage.py").read_text()
        self.assertIn("caldav_store.list_collections(db, user, strict=True)", src)
        self.assertNotIn("caldav_store.list_calendars(", src)


if __name__ == "__main__":
    unittest.main()
