"""The icon view looks like an icon view: one cell size, art that fills its box.

Run: venv-unified/bin/python -m pytest tests/client/test_file_tiles_are_dense.py

Two things were wrong and the first fix made the second worse.

WASTED SPACE: `.file-icon` was 140px tall holding a 42px glyph — a billboard with a dot in the
middle. In a folder of documents (which is what a SYNCED folder is) every cell looked empty.

UNIFORMITY: the fix for that gave non-preview cards a SHORT cell, so a folder holding both a
photograph and a PDF drew two different tile heights. No file manager does that, and it read as
worse than the problem it solved — "you made it shit again".

So: one height for everything, and a smaller one, with the glyph scaled up to fill it the way an OS
icon does. A photograph and a document occupy the same cell.
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


def _rule(css, selector):
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    for sel, body in re.findall(r"([^{}]*)\{([^{}]*)\}", css):
        if sel.strip() == selector:
            return body
    return None


def _px(body, prop):
    m = re.search(rf"(?:^|;)\s*{prop}:(\d+)px", body or "")
    return int(m.group(1)) if m else None


class TheIconViewLooksLikeOne(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = _read(APP)
        cls.css = _read(CSS)

    def test_a_glyph_and_a_photograph_get_the_SAME_cell(self):
        """The one thing no OS does is draw two tile heights in one folder."""
        glyph = _px(_rule(self.css, ".file-icon"), "height")
        art = _px(_rule(self.css, ".file-card img,.file-card video"), "height")
        self.assertIsNotNone(glyph, "the .file-icon rule moved — re-point this test")
        self.assertIsNotNone(art, "the image/video rule moved — re-point this test")
        self.assertEqual(glyph, art,
                         f"a document cell is {glyph}px and a photo cell is {art}px, so a folder "
                         "holding both draws a ragged grid")

    def test_nothing_makes_a_cell_a_different_height(self):
        """Every earlier attempt at density did it by shortening SOME cells. That is the bug."""
        self.assertNotIn("noart", self.css, "a per-file height override is back")
        self.assertNotIn("noart", self.app)
        heights = {sel.strip(): _px(body, "height")
                   for sel, body in re.findall(r"([^{}]*)\{([^{}]*)\}",
                                               re.sub(r"/\*.*?\*/", "", self.css, flags=re.S))
                   if ".file-icon" in sel and _px(body, "height")}
        self.assertEqual(len(set(heights.values())), 1,
                         f"more than one .file-icon height is defined: {heights}")

    def test_the_art_fills_its_box_rather_than_floating_in_it(self):
        """140px around a 42px glyph is the 'wasted icon space'. An OS icon fills its cell."""
        body = _rule(self.css, ".file-icon")
        h, f = _px(body, "height"), _px(body, "font-size")
        self.assertLessEqual(h, 110, "the icon cell is still billboard-sized")
        self.assertGreaterEqual(f / h, 0.45,
                                f"a {f}px glyph in a {h}px box is a dot in an empty square")

    def test_no_type_caption_under_the_icon(self):
        """No OS prints 'PDF' beneath a document. It cost a line of every tile and said nothing the
        glyph and the filename did not."""
        self.assertIn(".files-grid:not(.details) .file-icon span", self.css)

    def test_there_is_no_box_around_a_file(self):
        """A bordered, filled card per item is a web design. A file manager draws the icon and the
        name on the background and nothing else — asked for in as many words: "i don't want a box
        around folders and files"."""
        body = _rule(self.css, ".file-card")
        self.assertIsNotNone(body, "the .file-card rule moved — re-point this test")
        self.assertNotIn("background:var(", body,
                         "every file still sits on a filled panel")
        self.assertNotIn("border:1px solid var(", body,
                         "every file still sits inside a drawn border")
        self.assertIn("border:1px solid transparent", body,
                      "the border must stay reserved and transparent, or hover shifts every tile "
                      "in the row by a pixel")

    def test_the_glyph_has_no_panel_behind_it_either(self):
        """A 96px dark rectangle behind the icon is the same box, one element in."""
        self.assertNotIn("background:#120c24", _rule(self.css, ".file-icon") or "",
                         "the icon still has its own filled box")

    def test_encryption_does_not_put_the_boxes_back(self):
        """Most real drives are encrypted. A clean generic tile rule is meaningless if `.enc`
        overrides every tile back to a bordered, filled card."""
        card = _rule(self.css, ".files-grid:not(.details) .file-card.enc") or ""
        icon = _rule(self.css, ".files-grid:not(.details) .file-card.enc .file-icon") or ""
        self.assertIn("border-color:transparent", card)
        self.assertIn("background:none", icon)

    def test_selection_and_hover_are_what_draw_a_surface(self):
        """Which is exactly when an OS draws one. Without a fill, a selected file is
        indistinguishable from a hovered one."""
        sel = _rule(self.css, ".file-card.selected") or ""
        self.assertIn("background:", sel, "a selected file gets no fill")
        self.assertIn("border-color:", sel)
        self.assertIn(".files-grid:not(.details) .file-card:hover", self.css)

    def test_the_details_view_keeps_its_own_icon_size(self):
        """The list view has its own small icon (.fx-ic); this must not have touched it."""
        self.assertIsNotNone(_rule(self.css, ".fx-ic") or _rule(self.css, ".fx-ic.has-thumb"),
                             "the details-view icon rule vanished")


if __name__ == "__main__":
    unittest.main()
