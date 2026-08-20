"""One broken thing on a profile must cost exactly that thing.

The profile page ends in a straight run of bindings — the tab row, the tip buttons, Copy npub — and
a throw anywhere in it used to silently truncate the page at that point. The header is already on
screen and looks perfect, so it does not read as one failure: it reads as three unrelated bug
reports ("no posts", "the hamburger menu isn't showing", "copying the npub does nothing"), none of
which names a cause.

A try/catch around the run was added for that, and it is NOT the same as independence: it stops the
exception escaping, it does not run the statements after the throw. `hydrate` is the largest thing
in there — every avatar, name and badge on the page — so it is both the likeliest to throw and the
one that takes the most down with it. This asserts the small bindings survive it.

Read out of the shipped source rather than driven through a browser: the property is structural
(does the binding stand on its own?) and a browser test would only ever exercise the happy path.
"""
import os
import re
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
APP = os.path.join(ROOT, "static", "js", "client", "app.js")


class ProfileBindings(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(APP, encoding="utf-8") as fh:
            cls.src = fh.read()

    def test_copy_npub_is_bound_through_its_own_guard(self):
        """The single most reported casualty of everything above it."""
        m = re.search(r"_bind\(\s*'Copy npub'\s*,([\s\S]{0,300}?)\}\)\;", self.src)
        self.assertIsNotNone(m, "Copy npub is not bound through an independent guard")
        self.assertIn("copy-npub", m.group(1))
        self.assertIn("copyValue", m.group(1))

    def test_it_is_bound_before_the_things_most_likely_to_throw(self):
        """Order is the cheap half of the fix: a one-line binding that cannot fail should not be
        queued behind the page's heaviest call."""
        cn = self.src.index("_bind('Copy npub'")
        tabs = self.src.index("$$('.prof-tab',feed).forEach(t=> t.onclick=")
        self.assertLess(cn, tabs, "Copy npub is still bound after the tab row")

    def test_hydrate_cannot_take_the_rest_of_the_page_with_it(self):
        """It touches every avatar, name and badge on the page, so it is the likeliest to throw."""
        m = re.search(r"_bind\('the avatars and names',\s*\(\)\s*=>\s*hydrate\(feed\)\)", self.src)
        self.assertIsNotNone(m, "hydrate is not isolated — a throw in it still truncates the page")

    def test_what_broke_is_named_not_just_counted(self):
        """"Part of this profile didn't load" with no subject is a message nobody can act on."""
        self.assertIn("_profBroke", self.src)
        m = re.search(r"_bind = \(what, fn\) =>[\s\S]{0,400}?push\(what \+ ", self.src)
        self.assertIsNotNone(m, "a failed binding is recorded without saying which one it was")

    def test_the_warning_is_shown_only_when_something_really_broke(self):
        """A banner on every profile teaches people to ignore it."""
        m = re.search(r"if\(_profBroke && _profBroke\.length\)\{[\s\S]{0,400}?didn.t load", self.src)
        self.assertIsNotNone(m, "the warning is not gated on there being something to warn about")


if __name__ == "__main__":
    unittest.main()
