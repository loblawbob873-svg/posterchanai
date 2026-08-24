"""The start menu finds files, not only apps — the drive's and the machine's.

    "search in the start menu should search blossom files, local files as well"
    "for posterchanOS" / "and i guess regular posterchan desktop"

A start menu that finds apps and not documents is half a search box.

The two halves are fetched very differently, and that is deliberate rather than inconsistent. The
DRIVE is an index this page already holds decrypted, so it is filtered synchronously on every
keystroke exactly like the app list. The MACHINE's files are a disk walk in another process, so they
are asked for once the typing settles and painted when they arrive — a menu that blocks on readdir
stutters, and one that reorders under the cursor is worse than one that is slightly late.

Most of what is checked here is the quiet-failure surface this codebase keeps meeting: a function
that is defined but not on the PC surface, an icon that is named but not in the sprite, and a late
answer painted under a query it does not belong to.
"""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
APP = ROOT / "static/js/client/app.js"
OS_JS = ROOT / "static/js/client/os.js"
SPRITE = ROOT / "static/js/client/sprite.js"
HOSTFS = ROOT / "desktop/hostfs.js"
PRELOAD = ROOT / "desktop/preload.js"
MAIN = ROOT / "desktop/main.js"


class TheDriveSearchIsReachable(unittest.TestCase):
    """`PC._fmtBytes is not a function` and `PC.openMenuPopover` were both this: defined in app.js,
    never added to the surface, called from a sub-module."""

    def setUp(self):
        self.src = APP.read_text()
        # THE WHOLE OBJECT, BY MATCHING BRACES — never a fixed slice. It was `[i:i + 6000]`, and
        # adding two entries near the top of the surface pushed `driveSearch` past the end: the test
        # then reported that a function which had been exported for months was missing. A guard that
        # fails when somebody writes a paragraph is a guard people learn to edit rather than believe.
        i = self.src.index("window.__PC = {")
        j = self.src.index("{", i)
        depth, k = 0, j
        while k < len(self.src):
            if self.src[k] == "{":
                depth += 1
            elif self.src[k] == "}":
                depth -= 1
                if depth == 0:
                    break
            k += 1
        self.surface = self.src[i:k + 1]

    def test_both_functions_exist(self):
        self.assertIn("function driveSearch(", self.src)
        self.assertIn("function driveReveal(", self.src)

    def test_both_are_on_the_pc_surface(self):
        self.assertRegex(self.surface, r"\bdriveSearch\b")
        self.assertRegex(self.surface, r"\bdriveReveal\b")

    def test_it_does_not_touch_the_network(self):
        """It runs on every keystroke. A fetch, a relay read or a signer call here is a menu that
        stalls while somebody types."""
        i = self.src.index("function driveSearch(")
        body = self.src[i:self.src.index("\n  }", i)]
        for bad in ("await", "fetch(", "publish(", "sign(", "Relay"):
            with self.subTest(forbidden=bad):
                self.assertNotIn(bad, body)

    def test_a_single_character_is_not_a_search(self):
        i = self.src.index("function driveSearch(")
        self.assertIn("s.length < 2", self.src[i:i + 600])

    def test_it_ranks_before_it_truncates(self):
        """Object key order is arbitrary, so stopping at the cap returns an arbitrary handful of the
        matches rather than the best ones."""
        i = self.src.index("function driveSearch(")
        body = self.src[i:self.src.index("\n  }", i)]
        self.assertIn("hits.sort(", body)
        self.assertLess(body.index("hits.sort("), body.index("slice("))


class TheLocalSearchIsBounded(unittest.TestCase):
    def setUp(self):
        self.src = HOSTFS.read_text()
        i = self.src.index("function search(")
        self.body = self.src[i:self.src.index("\nmodule.exports", i)]

    def test_it_is_exported(self):
        self.assertRegex(self.src, r"module\.exports = \{[^}]*\bsearch\b")

    def test_every_bound_is_present(self):
        """Each one alone has a case that defeats it: a deadline for a stale network mount, a scan
        cap for a flat directory of 200,000 files, a depth cap, and a result cap."""
        for bound in ("until", "limit", "maxScan", "maxDepth"):
            with self.subTest(bound=bound):
                self.assertIn(bound, self.body)

    def test_it_is_breadth_first(self):
        """Depth-first disappears into node_modules and spends the whole budget there, so the answer
        depends on which folder sorts first."""
        self.assertIn("queue.shift()", self.body,
                      "a pop() here would make this depth-first")

    def test_symlinks_are_not_followed(self):
        """A link back up the tree is an infinite walk that only the deadline would notice."""
        self.assertIn("isSymbolicLink()", self.body)

    def test_the_bridge_carries_it(self):
        self.assertIn("pc:host:search", MAIN.read_text())
        self.assertIn("search:", PRELOAD.read_text())


class TheMenuPaintsBoth(unittest.TestCase):
    def setUp(self):
        self.src = OS_JS.read_text()

    def test_both_sections_are_rendered(self):
        self.assertIn("driveSearch", self.src)
        self.assertIn("pcHost.search", self.src)

    def test_every_icon_it_names_is_in_the_sprite(self):
        """An icon named but not defined renders as blank space with no error — the same bug as the
        mail row, and as `icon: 'window'` on the desktop launcher."""
        sprite = SPRITE.read_text()
        i = self.src.index("Your files")
        for name in sorted(set(re.findall(r"iconSvg\('([a-z0-9-]+)'\)", self.src[i - 400:i + 1600]))):
            with self.subTest(icon=name):
                self.assertIn('id="i-%s"' % name, sprite)

    def test_a_late_answer_is_keyed_to_its_query(self):
        """A slow disk walk that lands after the box has moved on must be discarded, not painted
        under a different word."""
        i = self.src.index("function _askLocal(")
        body = self.src[i:i + 1200]
        self.assertIn("_localQ = q", body)
        self.assertIn("=== q", body)

    def test_the_typing_is_debounced(self):
        i = self.src.index("function _askLocal(")
        self.assertIn("setTimeout", self.src[i:i + 1200])

    def test_a_late_answer_redraws_the_list_not_the_menu(self):
        """Rebuilding the menu takes focus and the caret out of the search box mid-word."""
        self.assertIn("_repaintStart = paint", self.src)

    def test_nothing_matches_is_not_shown_when_something_did(self):
        """The empty-state used to be decided before these two sections existed."""
        self.assertIn("const nothing = !list.length", self.src)


if __name__ == "__main__":
    unittest.main()
