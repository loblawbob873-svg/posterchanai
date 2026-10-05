"""Publishing a kind-0 must not drop the NIP-30 emoji in the display name.

A kind-0 REPLACES the whole document, and its custom-emoji definitions live in the TAGS. Store keeps
them on its own record rather than on `meta` — deliberately, so that editing a profile cannot
pollute the publishable content (store.js says so at the point it strips them). The consequence
nobody followed through: every `publish(0, JSON.stringify(meta), [])` in this client publishes an
EMPTY tag array, so somebody whose display name contains a custom emoji loses it from every client
on the network the first time they change anything — silently, with no way to restore it from the UI.

Every call site goes through one rule. (instance-access.js had one, a "Use <address>" button that
overwrote the profile's nip05 with this instance's; it is gone -- membership no longer reads the
profile, so there is nothing there to fix -- and that module must not grow a kind-0 publish again.)
"""
import re
import unittest
from pathlib import Path
from tests.client_source import client_source

ROOT = Path(__file__).resolve().parents[2] / "static" / "js" / "client"


def _code_only(src):
    out, i, n = [], 0, len(src)
    while i < n:
        two = src[i:i + 2]
        if two == "/*":
            j = src.find("*/", i + 2); j = n if j < 0 else j + 2
            out.append(" " * (j - i)); i = j
        elif two == "//":
            j = src.find("\n", i); j = n if j < 0 else j
            out.append(" " * (j - i)); i = j
        else:
            out.append(src[i]); i += 1
    return "".join(out)


class Kind0KeepsEmoji(unittest.TestCase):
    def setUp(self):
        self.app = _code_only(client_source())
        self.ia = _code_only((ROOT / "instance-access.js").read_text())

    def test_no_kind0_is_published_with_an_empty_tag_array(self):
        """The regression, stated as a rule rather than per call site."""
        bad = re.findall(r"publish\(\s*0\s*,[^;]*?,\s*\[\s*\]", self.app + self.ia)
        self.assertEqual(bad, [],
                         "a kind-0 is still published with empty tags — name emoji are destroyed: "
                         + str(bad[:2]))

    def test_every_kind0_publish_carries_the_emoji_tags(self):
        sites = re.findall(r"publish\(\s*0\s*,[^;]{0,160}", self.app + self.ia)
        self.assertGreaterEqual(len(sites), 2, "expected the profile editor's and the quiet re-publish's kind-0 sites")
        for site in sites:
            self.assertIn("kind0Tags", site,
                          "a kind-0 publish does not carry the emoji tags: " + site[:110])

    def test_the_rule_lives_in_one_place(self):
        self.assertIn("function _kind0Tags(", self.app,
                      "no shared helper — the next call site will pass [] again")

    def test_it_reads_the_tags_store_actually_keeps(self):
        m = re.search(r"function _kind0Tags\(pk\)\{.*?\n  \}", self.app, re.S)
        self.assertIsNotNone(m)
        body = m.group(0)
        self.assertIn("profileEmojis", body,
                      "the helper does not read the emoji map Store keeps off meta")
        self.assertIn("'emoji'", body, "the tags are not NIP-30 emoji tags")

    def test_it_is_exported_for_the_separate_modules(self):
        """A separate module can only see the shared surface."""
        self.assertIn("kind0Tags:", self.app,
                      "the helper is not on the shared surface, so a separate module cannot use it")

    def test_the_app_gate_never_rewrites_the_profile(self):
        """Keeping your own NIP-05 is the point: the gate must not publish a kind-0 at all."""
        self.assertNotRegex(self.ia, r"publish\(\s*0\s*,", "the app gate publishes a profile again")

    def test_no_emoji_is_not_an_error(self):
        m = re.search(r"function _kind0Tags\(pk\)\{.*?\n  \}", self.app, re.S).group(0)
        self.assertIn("return []", m, "the helper has no empty-case answer")
        self.assertIn("catch", m, "a Store failure would break publishing a profile")


if __name__ == "__main__":
    unittest.main()
