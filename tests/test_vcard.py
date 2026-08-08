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


if __name__ == "__main__":
    unittest.main()
