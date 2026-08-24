"""A file card with nothing to show does not reserve the space a photograph needs.

Run: venv-unified/bin/python -m pytest tests/client/test_file_tiles_are_dense.py

`.file-icon` is 140px tall, which is right for an image or a video frame and wrong for a 42px glyph
floating in an empty box. A drive of photographs never shows it; a SYNCED folder — documents, code,
archives — is a grid of empty boxes, reported as "synced folders is mostly wasted icon space".

`noart` is the same rule in both views, which is the other half of the request ("make it look like
the regular blossom"): one tile design, and the difference between them is what the FILES are, not
which screen you are on.

`has-thumb` must win over it. A synced preview is decrypted lazily and arrives after the card is
drawn, so a card that starts compact has to be able to become a picture.
"""
import os
import re
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
APP = os.path.join(ROOT, "static", "js", "client", "app.js")
CSS = os.path.join(ROOT, "static", "css", "client.css")


def _read(p):
    with open(p, encoding="utf-8") as fh:
        return fh.read()


def _rules(css, sel_contains):
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    return {sel.strip(): body for sel, body in re.findall(r"([^{}]*)\{([^{}]*)\}", css)
            if sel_contains in sel}


class TilesAreDenseWhenThereIsNothingToShow(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = _read(APP)
        cls.css = _read(CSS)

    def test_the_compact_rule_exists_and_is_shorter(self):
        r = _rules(self.css, ".file-icon")
        base = next((v for k, v in r.items() if k.strip() == ".file-icon"), None)
        self.assertTrue(base, "the base .file-icon rule moved — re-point this test")
        noart = next((v for k, v in r.items() if k.strip() == ".file-icon.noart"), None)
        self.assertTrue(noart, "no compact rule: every card still reserves a photograph's height")
        def h(body, what):
            """A MISSING height is not a height of zero. Read as 0 it satisfies every comparison
            below — the rule would be inheriting 140px and the test would call it compact."""
            m = re.search(r"height:(\d+)px", body)
            self.assertTrue(m, f"{what} sets no height, so it inherits the full one")
            return int(m.group(1))
        hb, hn = h(base, ".file-icon"), h(noart, ".file-icon.noart")
        self.assertGreater(hb, hn, "the 'compact' card is not shorter than the normal one")
        self.assertGreaterEqual(hb - hn, 40, "the saving is too small to be worth a second rule")

    def test_a_late_thumbnail_re_expands_the_card(self):
        """Synced previews are decrypted lazily; a card that starts compact must become a picture."""
        r = _rules(self.css, ".file-icon")
        both = next((v for k, v in r.items() if "noart" in k and "has-thumb" in k), None)
        self.assertTrue(both, "a decrypted preview would be squeezed into the compact height")
        self.assertIn("height:140px", both)

    def test_both_views_use_the_same_rule(self):
        """One tile design. The difference between the drive and a synced folder should be what the
        FILES are, not which screen you are on."""
        self.assertIn('file-icon noart', self.app, "the drive's non-preview cards are not compact")
        self.assertIn("const artc = canThumb ? '' : ' noart'", self.app,
                      "a synced folder decides density some other way than the drive does")

    def test_a_previewable_file_is_never_marked_compact(self):
        """An image must keep the space its thumbnail needs, or the drive looks worse than before."""
        i = self.app.index("function blobThumb(")
        body = self.app[i:self.app.index("\n  //", i)]
        for line in body.splitlines():
            if "ithumb" in line or "vthumb" in line:
                self.assertNotIn("noart", line, "an image/video tile was marked compact")


if __name__ == "__main__":
    unittest.main()
