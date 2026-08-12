"""The vCard reader/writer the Contacts screen edits through — run under node.

Run: venv-unified/bin/python -m unittest tests.test_vcard

The property under test is PRESERVATION. A real addressbook (the one these cases are taken from: 50
cards written by DAVx5) carries base64 photos, Apple-style grouped properties where `item1.EMAIL` is
labelled by `item1.X-ABLABEL`, a PRODID naming the app that wrote it, and X-* fields nobody else
understands. This app has form fields for about eight properties. If saving a phone number rebuilt
the card from those fields, every other property would be dropped — the contact would still look
right here and lose its photo everywhere else, with nothing to indicate it had happened.
"""
import json
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VCARD = ROOT / "static" / "js" / "client" / "vcard.js"

CARD = ("BEGIN:VCARD\r\nVERSION:3.0\r\n"
        "PRODID:+//IDN bitfire.at//DAVx5/4.5.9-ose\r\n"
        "UID:a109a067\r\nFN:Fire DEPARTMENT\r\nN:DEPARTMENT;Fire;;;\r\n"
        "TEL;TYPE=cell:7192758666\r\nEND:VCARD\r\n")

PHOTO = ("BEGIN:VCARD\r\nVERSION:3.0\r\nUID:p1\r\nFN:Has Photo\r\n"
         "PHOTO;ENCODING=b;TYPE=JPEG:/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAAgGBgcGBQgHBwcJ\r\n"
         " CQkKDBQNDAsLDBkSEw8UHRofHh0aHBwgJC4nICIsIxwcKDcpLDAxNDQ0Hyc5PTgyPC4zNDL/\r\n"
         "END:VCARD\r\n")

GROUPED = ("BEGIN:VCARD\r\nVERSION:3.0\r\nUID:g1\r\nFN:Labelled\r\n"
           "item1.EMAIL;TYPE=INTERNET:someone@example.com\r\n"
           "item1.X-ABLABEL:School\r\n"
           "X-WEIRD-THING:keep me\r\nEND:VCARD\r\n")


def _node(script: str):
    src = f"const V = require({json.dumps(str(VCARD))});\n{script}"
    out = subprocess.run(["node", "-e", src], capture_output=True, timeout=60)
    if out.returncode != 0:
        raise AssertionError(out.stderr.decode()[-2000:])
    return json.loads(out.stdout.decode() or "null")


def _roundtrip(vcf: str, edit_js: str = ""):
    """Parse, optionally edit, serialize, re-parse. Returns the re-serialized text."""
    return _node(f"""
      const c = V.parse({json.dumps(vcf)});
      {edit_js}
      console.log(JSON.stringify(V.serialize(c)));
    """)


@unittest.skipIf(shutil.which("node") is None, "node not installed")
class ParseTests(unittest.TestCase):
    def test_the_basics(self):
        got = _node(f"""
          const c = V.parse({json.dumps(CARD)});
          console.log(JSON.stringify([c.uid, c.fn, c.n.family, c.n.given,
                                      c.tels.length, c.tels[0].value, c.tels[0].type]));
        """)
        self.assertEqual(got, ["a109a067", "Fire DEPARTMENT", "DEPARTMENT", "Fire",
                               1, "7192758666", "cell"])

    def test_a_folded_photo_becomes_something_an_img_can_show(self):
        got = _node(f"console.log(JSON.stringify(V.parse({json.dumps(PHOTO)}).photo.slice(0, 30)));")
        self.assertEqual(got, "data:image/jpeg;base64,/9j/4AA")

    def test_a_name_is_derived_when_fn_is_missing(self):
        got = _node("""
          console.log(JSON.stringify(V.displayName(
            V.parse('BEGIN:VCARD\\r\\nN:Smith;Jane;;;\\r\\nEND:VCARD'))));
        """)
        self.assertEqual(got, "Jane Smith")

    def test_a_nameless_card_falls_back_to_a_contact_detail(self):
        got = _node("""
          console.log(JSON.stringify(V.displayName(
            V.parse('BEGIN:VCARD\\r\\nTEL:555\\r\\nEND:VCARD'))));
        """)
        self.assertEqual(got, "555")


@unittest.skipIf(shutil.which("node") is None, "node not installed")
class PreservationTests(unittest.TestCase):
    """Everything this app has no field for must survive an edit."""

    def test_editing_a_phone_keeps_the_photo(self):
        out = _roundtrip(PHOTO, "c.tels = [{value:'0123', type:'cell'}];")
        self.assertIn("PHOTO", out)
        self.assertIn("2wBDAAgGBgcGBQgHBwcJ", out)
        self.assertIn("TEL", out)

    def test_editing_keeps_unknown_fields(self):
        out = _roundtrip(GROUPED, "c.org = 'New Employer';")
        self.assertIn("X-WEIRD-THING:keep me", out)
        self.assertIn("ORG:New Employer", out)

    def test_a_grouped_email_keeps_its_prefix_so_its_label_still_points_at_it(self):
        out = _roundtrip(GROUPED, "c.emails[0].value = 'new@example.com';")
        self.assertIn("item1.EMAIL", out)
        self.assertIn("item1.X-ABLABEL:School", out)
        self.assertIn("new@example.com", out)

    def test_the_prodid_of_the_app_that_wrote_it_is_not_stolen(self):
        self.assertIn("bitfire.at", _roundtrip(CARD, "c.org = 'x';"))

    def test_the_uid_never_changes(self):
        out = _roundtrip(CARD, "c.fn = 'Someone Else'; c.n = {family:'Else', given:'Someone'};")
        self.assertIn("UID:a109a067", out)

    def test_a_photo_line_is_refolded_rather_than_emitted_as_one_long_line(self):
        out = _roundtrip(PHOTO)
        longest = max(len(l) for l in out.split("\r\n"))
        self.assertLessEqual(longest, 75)


@unittest.skipIf(shutil.which("node") is None, "node not installed")
class WriteTests(unittest.TestCase):
    def test_a_new_card_has_a_uid_of_its_own(self):
        # Generated client-side: a re-import can only recognise this person by the handle written
        # the first time.
        got = _node("const b = V.blank(); console.log(JSON.stringify(b.uid.length > 8));")
        self.assertTrue(got)

    def test_text_is_escaped(self):
        out = _node("""
          console.log(JSON.stringify(V.serialize(Object.assign(V.blank(),
            {fn:'Smith; Jane, Dr', note:'line1\\nline2'}))));
        """)
        self.assertIn("FN:Smith\\; Jane\\, Dr", out)
        self.assertIn("NOTE:line1\\nline2", out)

    def test_an_empty_address_is_not_written(self):
        out = _node("""
          console.log(JSON.stringify(V.serialize(Object.assign(V.blank(),
            {adrs:[{po:'',ext:'',street:'',city:'',region:'',code:'',country:''}]}))));
        """)
        self.assertNotIn("ADR", out)

    def test_a_blank_phone_row_is_dropped(self):
        out = _node("""
          console.log(JSON.stringify(V.serialize(Object.assign(V.blank(),
            {fn:'X', tels:[{value:'',type:''},{value:'555',type:'home'}]}))));
        """)
        self.assertEqual(out.count("TEL"), 1)
        self.assertIn("TEL;TYPE=home:555", out)


@unittest.skipIf(shutil.which("node") is None, "node not installed")
class SearchTests(unittest.TestCase):
    def test_a_punctuated_number_matches_a_stored_one(self):
        got = _node(f"""
          const c = V.parse({json.dumps(CARD)});
          console.log(JSON.stringify([V.matches(c, '719-275-8666'), V.matches(c, '(719) 2758666'),
                                      V.matches(c, '7192758666'), V.matches(c, '5551234')]));
        """)
        self.assertEqual(got, [True, True, True, False])

    def test_search_covers_name_and_company(self):
        got = _node(f"""
          const c = V.parse({json.dumps(CARD)});
          console.log(JSON.stringify([V.matches(c, 'fire'), V.matches(c, 'DEPART'),
                                      V.matches(c, 'zzz')]));
        """)
        self.assertEqual(got, [True, True, False])

    def test_cards_sort_by_family_name(self):
        got = _node("""
          const a = V.parse('BEGIN:VCARD\\r\\nN:Zeta;Ann;;;\\r\\nFN:Ann Zeta\\r\\nEND:VCARD');
          const b = V.parse('BEGIN:VCARD\\r\\nN:Alpha;Bob;;;\\r\\nFN:Bob Alpha\\r\\nEND:VCARD');
          console.log(JSON.stringify([a,b].sort((x,y)=>V.sortKey(x).localeCompare(V.sortKey(y)))
                                          .map(V.displayName)));
        """)
        self.assertEqual(got, ["Bob Alpha", "Ann Zeta"])


@unittest.skipIf(shutil.which("node") is None, "node not installed")
class PhonebookTests(unittest.TestCase):
    """The TWO-WAY Android sync's decisions, which is where a contact gets eaten.

    ContactsContract is a thin adapter; everything that can get the ANSWER wrong is here, so it can
    be run without a device. Each of these is a way somebody loses a person, a number or a photo with
    nothing in any log:

      * a phone-side edit rebuilding the card from the eight fields a phone models — the photo, the
        Apple-style labels and every X-* field go, exactly as if the web editor had done it;
      * a deletion made on the phone read as "a card the phone is missing", which re-adds them, which
        makes the next pull delete them again, for ever;
      * a contact created on the phone getting a fresh uid on every sweep — one person, many cards;
      * a dirty row that says the same thing being written back anyway, which churns REV on every
        contact the user so much as opens;
      * both sides changing and the loser being dropped in silence.
    """

    RICH = ("BEGIN:VCARD\r\nVERSION:3.0\r\nPRODID:+//IDN bitfire.at//DAVx5\r\nUID:u1\r\n"
            "FN:Ann Zeta\r\nN:Zeta;Ann;;;\r\nTEL;TYPE=voice,cell:7192758666\r\n"
            "item1.EMAIL;TYPE=INTERNET:ann@example.com\r\nitem1.X-ABLABEL:School\r\n"
            "PHOTO;ENCODING=b;TYPE=JPEG:/9j/4AAQSkZJRgABAQAAAQABAAD\r\n"
            "X-WEIRD-THING:keep me\r\nREV:2026-08-01T10:00:00Z\r\nEND:VCARD\r\n")

    def _plan(self, rows_js, stored_js="({})"):
        return _node(f"console.log(JSON.stringify(V.phonePlan({rows_js}, {stored_js})));")

    def test_a_phone_edit_keeps_everything_the_phone_does_not_model(self):
        out = _node(f"""
          const mine = V.parse({json.dumps(self.RICH)});
          const phone = V.toPhone(mine);
          phone.tels[0].value = '5550000';           // the one thing the user changed
          const merged = V.applyPhone(mine, phone);
          console.log(JSON.stringify(V.serialize(merged)));
        """)
        self.assertIn("5550000", out)
        self.assertIn("PHOTO;ENCODING=b", out)          # the photo survives a phone-side edit
        self.assertIn("X-WEIRD-THING:keep me", out)
        self.assertIn("bitfire.at", out)
        self.assertIn("UID:u1", out)
        # …and the grouped label still points at its property.
        self.assertIn("item1.EMAIL", out)
        self.assertIn("item1.X-ABLABEL:School", out)

    def test_a_type_is_not_rewritten_when_it_means_the_same_thing(self):
        """ContactsContract has one type per row, so `TYPE=voice,cell` comes back as "cell".
        Taken verbatim it rewrites everybody's labels the first time one number is edited."""
        out = _node("""
          const mine = V.parse('BEGIN:VCARD\\r\\nUID:t1\\r\\nFN:T\\r\\n'
                             + 'TEL;TYPE=WORK,CELL:7192758666\\r\\nEND:VCARD');
          const phone = V.toPhone(mine);
          phone.tels[0].type = 'cell';               // what ContactsContract can say
          const same = V.applyPhone(mine, phone);
          const moved = V.applyPhone(mine, Object.assign({}, phone,
                          {tels:[{type:'home', value:'7192758666'}]}));
          console.log(JSON.stringify([mine.tels[0].type, same.tels[0].type, moved.tels[0].type]));
        """)
        self.assertEqual(out, ["work cell", "work cell", "home"])

    def test_a_phone_deletion_deletes_and_is_never_an_add(self):
        got = self._plan(
            "[{uid:'u1', rawId:7, version:3, deleted:true}]",
            "({u1:{book:'contacts', card:V.parse(%s)}})" % json.dumps(self.RICH))
        self.assertEqual([s["action"] for s in got], ["delete"])
        self.assertEqual(got[0]["book"], "contacts")
        # And one we no longer hold is simply acknowledged — never re-created.
        got = self._plan("[{uid:'gone', rawId:8, version:1, deleted:true}]")
        self.assertEqual([s["action"] for s in got], ["drop"])

    def test_a_contact_created_on_the_phone_makes_exactly_one_card(self):
        rows = ("[{uid:'pc-abc', rawId:9, version:2, deleted:false, updated:1000,"
                " card:{fn:'New Person', given:'New', family:'Person',"
                " tels:[{type:'mobile', value:'5551111'}], emails:[], adrs:[]}}]")
        got = self._plan(rows)
        self.assertEqual([s["action"] for s in got], ["create"])
        self.assertEqual(got[0]["card"]["uid"], "pc-abc")
        self.assertEqual(got[0]["uid"], "pc-abc")
        # Sweep it again with the card now stored under that uid: nothing new is created.
        again = self._plan(
            rows,
            "({'pc-abc':{book:'contacts', card:V.applyPhone(Object.assign(V.blank(),"
            "{uid:'pc-abc'}), %s[0].card)}})" % rows)
        self.assertEqual([s["action"] for s in again], ["clean"])

    def test_a_dirty_row_that_says_the_same_thing_writes_nothing(self):
        got = self._plan(
            "[{uid:'u1', rawId:1, version:4, deleted:false, updated:9e12,"
            " card:V.toPhone(V.parse(%s))}]" % json.dumps(self.RICH),
            "({u1:{book:'contacts', card:V.parse(%s)}})" % json.dumps(self.RICH))
        self.assertEqual([s["action"] for s in got], ["clean"])
        self.assertNotIn("card", got[0])

    def test_when_both_sides_changed_the_loser_is_kept_not_dropped(self):
        """Last writer wins on the two clocks available, and the other version becomes a copy —
        folder sync's rule. Neither clock is exact enough to delete somebody's only copy on."""
        stored = ("({u1:{book:'contacts', card:Object.assign(V.parse(%s), {org:'Edited here'})}})"
                  % json.dumps(self.RICH))
        phone_card = ("Object.assign(V.toPhone(V.parse(%s)), {org:'Edited on the phone'})"
                      % json.dumps(self.RICH))
        # The phone's edit is newer than the card's REV (2026-08-01) → the phone wins.
        newer = self._plan(
            "[{uid:'u1', rawId:1, version:2, deleted:false, updated:%d, pushed:'stale', card:%s}]"
            % (1786000000000, phone_card), stored)
        self.assertEqual(newer[0]["action"], "update")
        self.assertEqual(newer[0]["card"]["org"], "Edited on the phone")
        self.assertEqual(newer[0]["copy"]["org"], "Edited here")
        self.assertNotEqual(newer[0]["copy"]["uid"], "u1")
        self.assertIn("conflict copy", newer[0]["copy"]["fn"])
        # …and with the phone's clock behind the card's REV, ours wins and the PHONE's version is the
        # copy. Nothing is stored under the original uid — the push puts our version back.
        older = self._plan(
            "[{uid:'u1', rawId:1, version:2, deleted:false, updated:1, pushed:'stale', card:%s}]"
            % phone_card, stored)
        self.assertEqual(older[0]["action"], "keep")
        self.assertIsNone(older[0].get("card"))
        self.assertEqual(older[0]["copy"]["org"], "Edited on the phone")

    def test_only_the_phone_changing_is_not_a_conflict(self):
        """`pushed` is the hash of what we last put on the phone. Equal to the card's hash now means
        this side has not moved, so there is no conflict and no copy — one edit, one card."""
        got = _node(f"""
          const M = V.parse({json.dumps(self.RICH)});
          const H = V.toPhone(M).h;
          const row = {{ uid:'u1', rawId:1, version:2, deleted:false, updated:9e12, pushed:H,
                        card: Object.assign(V.toPhone(M), {{ note:'from the phone' }}) }};
          console.log(JSON.stringify(V.phonePlan([row], {{ u1:{{ book:'contacts', card:M }} }})));
        """)
        self.assertEqual(got[0]["action"], "update")
        self.assertIsNone(got[0]["copy"])
        self.assertEqual(got[0]["card"]["note"], "from the phone")


if __name__ == "__main__":
    unittest.main()
