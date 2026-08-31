"""Opening a folder is one action, and it was written twice with different bodies.

Reported over and over: "Once I am in home, I can't click to any other folder from Blossom or Synced
folders", "file manager is completely non-functional now", "can't even see synced folders from
android Files list".

The sidebar CHIP did:

    _fxRemember(); _syncRoot=''; _syncPath=''; _hostOn=false;
    _fxMobileSource='blossom'; _filesFolder=…; renderBlossom();

and the HOME TILE did the same thing MINUS the first and fifth statements. `_fxMobileSource` is what
puts `mobile-on` on a pane — it decides WHICH PANE IS VISIBLE on a narrow layout — so the tile moved
the state and left the screen showing home. On a phone that is the entire file manager: press a
folder, nothing happens, nothing logs, the app looks frozen. `_fxRemember` was missing too, so Back
had nothing to return to.

Nothing caught it because `check_files_explorer.py` builds its own `.fx-home-tile` markup by hand
and never runs the real render, binds a handler, or clicks anything — it is a layout check, and the
one thing a person does on that screen was exercised by nothing.

The fix is not "add the two missing statements": it is that there is now ONE function both call.
Two call sites for one action drift again the moment either is edited, and this file exists to make
a second one fail rather than ship.
"""
import re
import unittest
from pathlib import Path

APP = (Path(__file__).resolve().parents[2] / "static" / "js" / "client" / "app.js").read_text(
    encoding="utf-8")


class OneWayToOpenAFolder(unittest.TestCase):
    def test_the_navigator_exists(self):
        for fn in ("_fxOpenFolder", "_fxOpenSynced"):
            self.assertIn("function %s(" % fn, APP, "%s is gone" % fn)

    def test_it_sets_everything_that_makes_the_view_change(self):
        body = APP[APP.index("function _fxOpenFolder("):APP.index("function _fxOpenSynced(")]
        for needed, why in (
                ("_fxRemember()", "Back has nothing to return to"),
                ("_fxMobileSource='blossom'", "the visible pane never switches on a phone"),
                ("_filesFolder=name", "the folder is never selected"),
                ("renderBlossom()", "nothing repaints")):
            self.assertIn(needed, body, "%s missing — %s" % (needed, why))

    def test_synced_navigation_sets_its_own_source(self):
        body = APP[APP.index("function _fxOpenSynced("):APP.index("function _fxOpenSynced(") + 400]
        self.assertIn("_fxMobileSource='synced'", body,
                      "a synced folder opens without switching the visible pane")
        self.assertIn("_fxRemember()", body)

    def test_both_the_chip_and_the_tile_go_through_it(self):
        for sel, fn in ((".folder-chip[data-folder]", "_fxOpenFolder"),
                        (".folder-chip[data-synckey]", "_fxOpenSynced"),
                        (".fx-home-tile[data-folder]", "_fxOpenFolder"),
                        (".fx-home-tile[data-synckey]", "_fxOpenSynced")):
            with self.subTest(selector=sel):
                # ONLY THE BINDING THAT ASSIGNS onclick. The same selector is also used by
                # `_fxBindChipDrop`, which wires ondragover/ondrop and correctly navigates nowhere —
                # and taking the FIRST occurrence found that one and reported the navigation as
                # missing from code that was right.
                clicks = [m.start() for m in re.finditer(re.escape("$$('%s'" % sel), APP)
                          if "onclick" in APP[m.start():m.start() + 300]]
                self.assertTrue(clicks, "%s has no click binding at all" % sel)
                for at in clicks:
                    self.assertIn(fn, APP[at:at + 300],
                                  "the %s click at app.js:%d does not use %s — it has its own copy "
                                  "of the navigation, which is how the home tile came to be missing "
                                  "_fxMobileSource"
                                  % (sel, APP[:at].count("\n") + 1, fn))

    def test_no_handler_still_assigns_the_state_by_hand(self):
        """A second inline copy is the bug returning; the navigator is the only place these move."""
        hand = []
        for m in re.finditer(r"\$\$\('\.(?:folder-chip|fx-home-tile)\[[^']+'\s*,", APP):
            near = APP[m.start():m.start() + 300]
            if "onclick" not in near:
                continue                      # the drag/drop binder, which navigates nowhere
            if "_filesFolder=" in near or "_syncRoot=" in near:
                hand.append("app.js:%d" % (APP[:m.start()].count("\n") + 1))
        self.assertEqual([], hand,
                         "these bindings set the navigation state inline instead of calling the "
                         "navigator, so they can drift from it again: %s" % ", ".join(hand))


if __name__ == "__main__":
    unittest.main()
