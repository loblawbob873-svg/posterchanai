"""/f/<sha> — the page a stranger opens.

Run: venv-unified/bin/python -m unittest tests.test_sharelink_route

scripts/check_sharelink.py proves the CRYPTO end to end in a real browser. This covers the parts a
browser check cannot see, and every one of them is a property of a page that is deliberately
unauthenticated and is handed to people over SMS.

The end-to-end proof and this file are complementary on purpose: one shows that the right person can
open the file, the other that the page cannot be turned into something else.
"""
import os
import re
import unittest

from fastapi.testclient import TestClient

import app.main as M
from app.routers import sharelink as S

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEMPLATE = os.path.join(ROOT, "templates", "sharelink.html")
SHA = "a" * 64


class TheRoute(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.c = TestClient(M.app)

    def test_a_real_sha_renders_the_page_for_that_blob(self):
        r = self.c.get("/f/" + SHA)
        self.assertEqual(r.status_code, 200)
        self.assertIn("/blossom/" + SHA, r.text, "the page was not pointed at this blob")

    def test_it_needs_no_login(self):
        """THE WHOLE POINT. The recipient has a phone, a browser and no account here. An auth gate
        would make the feature deliver a sign-in screen to somebody who was sent a photo."""
        r = self.c.get("/f/" + SHA)
        self.assertEqual(r.status_code, 200)
        self.assertNotIn("get_current_user", open(
            os.path.join(ROOT, "app", "routers", "sharelink.py"), encoding="utf-8").read(),
            "the page acquired an authentication dependency")

    def test_anything_that_is_not_a_sha_is_a_404(self):
        for bad in ("notasha", "../etc/passwd", "a" * 63, "a" * 65, "g" * 64, ""):
            with self.subTest(path=bad):
                self.assertEqual(self.c.get("/f/" + bad).status_code, 404)

    def test_a_sha_is_normalised_rather_than_rejected(self):
        """An uppercase hash and a blob's own extension both ride along in real links; refusing them
        would be a dead link for a reason nobody could see."""
        self.assertEqual(self.c.get("/f/" + SHA.upper()).status_code, 200)
        r = self.c.get("/f/" + SHA + ".enc")
        self.assertEqual(r.status_code, 200)
        self.assertIn("/blossom/" + SHA, r.text)

    def test_the_page_is_not_cached_and_leaks_no_referrer(self):
        """A one-off transfer page has no business in a shared cache, and the URL it would leak in a
        Referer is the URL that carries the key."""
        h = self.c.get("/f/" + SHA).headers
        self.assertEqual(h.get("cache-control"), "no-store")
        self.assertEqual(h.get("referrer-policy"), "no-referrer")


class ThePage(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(TEMPLATE, encoding="utf-8") as f:
            cls.src = f.read()

    def test_it_loads_nothing_from_anywhere(self):
        """It is opened on a stranger's phone, on an unknown connection, and it holds a decryption
        key in its URL. Every third party it pulled in would be one more party in a position to see
        that URL — and one more way for the page to simply not work."""
        for pat in (r'src\s*=\s*["\']https?://', r'href\s*=\s*["\']https?://',
                    r'src\s*=\s*["\']//', r'@import', r'fonts\.googleapis'):
            with self.subTest(pattern=pat):
                self.assertIsNone(re.search(pat, self.src, re.I),
                                  "the page pulls in an external resource")

    def test_the_key_is_never_put_into_a_request(self):
        """The fragment is the key. It must be read and used LOCALLY — never appended to a URL, sent
        in a body, or beaconed, any of which hands it to the server the page came from."""
        # The one legitimate read.
        self.assertIn("location.hash", self.src)
        body = self.src
        for bad in ("XMLHttpRequest", "sendBeacon", "new Image(", "navigator.sendBeacon"):
            with self.subTest(api=bad):
                self.assertNotIn(bad, body)
        # A fetch must not be built out of the hash.
        for m in re.finditer(r"fetch\(([^)]*)\)", body):
            self.assertNotIn("hash", m.group(1),
                             "a request was constructed from the fragment: " + m.group(0)[:120])

    def test_it_says_what_it_cannot_promise(self):
        """The link IS the secret and SMS is not a confidential channel. A page that implied more
        than that would be the dishonest part of an otherwise honest feature."""
        self.assertIn("anyone with this link", self.src.lower())

    def test_every_failure_has_its_own_sentence(self):
        """A truncated link, a wrong key and a deleted file need three different things from the
        person. One generic error makes all three unactionable."""
        low = self.src.lower()
        for phrase in ("incomplete", "could not unlock", "no longer available"):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, low)

    def test_a_blob_hosted_elsewhere_must_name_this_same_file(self):
        """`u` in the fragment lets a blob on another media server be fetched. Unchecked, this page
        becomes a request-anywhere gadget aimed at whoever opens the link."""
        self.assertIn("meta.u", self.src)
        self.assertIn("indexOf({{ sha|tojson }})", self.src,
                      "the alternate blob address is not tied to this link's own sha")
        self.assertIn("/^https?:", self.src)


class TheModule(unittest.TestCase):
    def test_the_sha_pattern_is_anchored(self):
        """Unanchored, `[0-9a-f]{64}` matches inside a longer string and the jail below it is moot."""
        self.assertTrue(S._SHA.pattern.startswith("^") and S._SHA.pattern.endswith("$"))
        self.assertIsNone(S._SHA.match("x" + "a" * 64))
        self.assertIsNone(S._SHA.match("a" * 64 + "x"))


if __name__ == "__main__":
    unittest.main()
