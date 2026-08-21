"""Holding the SMS role IS being the messages app, when nothing else is named.

Measured on a real Samsung, from the app's own diagnostic:

    rows: 587   threads: 41
    role: true   store names: (none)
    this app: place.poster.app   sim: true

Android had granted PosterChan the SMS ROLE and the message store's default-app row was EMPTY. They
are separate tables on Android 10+ and OEM builds do not always write both.

`HasRole.sms()` asked only the store, so it answered FALSE while Android said we held the role. That
one answer is behind a full day of reports: "the checkbox in settings never works" (it worked; the
store never recorded it), "posterchan is set as the messaging app" while the app insisted it was not,
and every gate keyed on it quietly doing nothing.

The rule has three cases and only one of them is new:
  * the store names US            → we are the messages app
  * the store names SOMEBODY ELSE → we are not, even holding the role: messages really are being
                                    delivered there, and writing into a store another app owns or
                                    reporting sends it performed would both be lies
  * the store names NOBODY        → the role decides, because there is no other candidate
"""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HASROLE = ROOT / "mobile/android/app/src/main/java/place/poster/app/sms/HasRole.java"


def method(src, decl):
    i = src.index(decl)
    j = src.index("{", i)
    depth, k = 0, j
    while True:
        if src[k] == "{":
            depth += 1
        elif src[k] == "}":
            depth -= 1
            if depth == 0:
                return src[i:k + 1]
        k += 1


def strip_comments(src):
    out, i, n = [], 0, len(src)
    while i < n:
        if src.startswith("//", i):
            i = src.find("\n", i)
            if i < 0:
                break
        elif src.startswith("/*", i):
            i = src.find("*/", i)
            i = n if i < 0 else i + 2
        else:
            out.append(src[i]); i += 1
    return "".join(out)


class TheRoleCountsWhenNothingIsNamed(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.body = method(strip_comments(HASROLE.read_text()), "static boolean sms(")

    def test_it_consults_the_role_at_all(self):
        self.assertIn("roleHeld(ctx)", self.body,
                      "sms() asks only the message store, so a phone that granted the role and did "
                      "not write the row is told it is not the messages app")

    def test_a_named_package_still_decides(self):
        """The store naming another app is the case where messages really are delivered elsewhere."""
        self.assertIn("cur.equals(mine)", self.body)
        i = self.body.index("cur.equals(mine)")
        j = self.body.index("roleHeld(ctx)")
        self.assertLess(i, j, "the role is consulted before the store's own answer")

    def test_the_role_is_only_the_fallback(self):
        """Holding the role must NOT override a store that names somebody else — that would have us
        writing into a store another app owns and reporting sends it performed."""
        self.assertRegex(self.body, r"cur != null && !cur\.isEmpty\(\)")

    def test_it_never_throws(self):
        self.assertIn("catch", self.body)


class TheTwoAnswersAreStillReportedSeparately(unittest.TestCase):
    """Collapsing them is what hid this for a day; the panel must keep showing both."""

    def test_role_held_is_its_own_measurement(self):
        self.assertIn("static boolean roleHeld", HASROLE.read_text())

    def test_the_plugin_reports_both(self):
        p = ROOT / "mobile/android/app/src/main/java/place/poster/app/sms/SmsPlugin.java"
        src = p.read_text()
        self.assertIn('o.put("roleHeld"', src)
        self.assertIn('o.put("defaultPackage"', src)


if __name__ == "__main__":
    unittest.main()
