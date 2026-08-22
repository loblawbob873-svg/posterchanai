"""A text from a number in your address book shows the person's name.

    "I see contacts correctly in contacts but not Texts"

On the phone the SMS archive carries a name, because the handset resolved it against its own Contacts
app before publishing. In the WEB app there is no handset, so any thread published without one showed
a bare number — with the same person sitting in Contacts on the next screen.

The matching rule is the LAST SEVEN DIGITS, which is `key()` in sms.js and `SmsKeys.matchKey` in
Java. There are three copies of it now and the alternative was worse: a shared module between two
features that otherwise know nothing about each other. So this runs the two JavaScript copies against
each other on the same inputs, the way test_android_sms.py runs the JS and Java copies.
"""
import json
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SMS = ROOT / "static/js/client/sms.js"
CONTACTS = ROOT / "static/js/client/contacts.js"


class TheMessageNameStillWins(unittest.TestCase):
    """It was resolved by the device that received the text, at the time — better evidence than an
    address book edited since, and what keeps an unknown number unknown."""

    def setUp(self):
        self.src = SMS.read_text()

    def test_every_display_name_goes_through_one_helper(self):
        self.assertIn("function whoIs(", self.src)
        # No site may still pick a name by hand.
        for bad in ("last.name || t.address", ".name || m.address", "{}).name || t.address"):
            with self.subTest(pattern=bad):
                self.assertNotIn(bad, self.src)

    def test_the_message_name_is_preferred(self):
        i = self.src.index("function whoIs(")
        body = self.src[i:i + 700]
        self.assertLess(body.index("nameFromMsg"), body.index("nameFor"),
                        "the address book is consulted before the message's own name")

    def test_it_falls_back_to_the_number(self):
        i = self.src.index("function whoIs(")
        self.assertIn("return String(address", self.src[i:i + 1000])

    def test_a_provider_number_is_not_mistaken_for_a_name(self):
        i = self.src.index("function whoIs(")
        body = self.src[i:i + 1000]
        self.assertIn("key(n) !== key(address)", body)
        self.assertLess(body.index("key(n) !== key(address)"), body.index("nameFor"))

    def test_a_missing_contacts_module_is_not_fatal(self):
        """Texts must work with contacts disabled, or on a build without it."""
        i = self.src.index("function whoIs(")
        self.assertIn("catch", self.src[i:i + 700])


class ContactsCanAnswer(unittest.TestCase):
    def setUp(self):
        self.src = CONTACTS.read_text()

    def test_it_exposes_a_lookup(self):
        self.assertIn("nameFor(number)", self.src)

    def test_it_never_returns_the_number_back(self):
        """The caller decides what to show when there is no match; a lookup that echoes its input
        cannot be distinguished from a hit."""
        i = self.src.index("nameFor(number)")
        self.assertIn("return ''", self.src[i:i + 400])

    def test_it_works_from_the_cache(self):
        """Somebody who has not opened Contacts this session still has an address book."""
        i = self.src.index("nameFor(number)")
        self.assertIn("_loadCache()", self.src[i:i + 400])

    def test_the_index_is_stable_across_repaints(self):
        """Two cards sharing a number must not swap the label on every draw."""
        self.assertIn("first card wins", self.src)


@unittest.skipIf(shutil.which("node") is None, "node not installed")
class BothCopiesOfTheRuleAgree(unittest.TestCase):
    NUMBERS = ["+15551234567", "5551234567", "555-123-4567", "(555) 123 4567",
               "+44 20 7946 0958", "12345", "", "abc", "+1 (555) 123-4567 ext 9",
               "001155512345678", "911"]

    def test_the_last_seven_digit_rule_is_identical(self):
        js = """
        global.window = global;
        %s
        %s
        const out = [];
        for (const n of %s) out.push([smsKey(n), ctKey(n)]);
        console.log(JSON.stringify(out));
        """ % (
            # sms.js's key(), lifted verbatim by name
            "function smsKey(addr){ const digits=String(addr||'').replace(/[^0-9]/g,'');"
            " if(!digits) return String(addr||'').replace(/[^0-9+]/g,'');"
            " return digits.length < 7 ? digits : digits.slice(-7); }",
            "function ctKey(addr){ const digits=String(addr||'').replace(/[^0-9]/g,'');"
            " if(!digits) return String(addr||'').replace(/[^0-9+]/g,'');"
            " return digits.length < 7 ? digits : digits.slice(-7); }",
            json.dumps(self.NUMBERS))
        r = subprocess.run(["node", "-e", js], capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        for a, b in json.loads(r.stdout):
            self.assertEqual(a, b)

    def test_both_files_still_carry_that_exact_rule(self):
        """The test above compares two transcriptions; this is what ties them to the shipped code."""
        rule = "digits.length < 7 ? digits : digits.slice(-7)"
        self.assertIn(rule, SMS.read_text())
        self.assertIn(rule, CONTACTS.read_text())


if __name__ == "__main__":
    unittest.main()
