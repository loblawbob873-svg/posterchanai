"""PosterChan Code is reachable from the surfaces it is for.

Run: venv-unified/bin/python -m pytest tests/client/test_code_is_reachable.py

It had NO SIDEBAR ROW. It existed in exactly one place — the mobile "More" sheet — so on the web it
could not be found at all, and on the PosterChanOS desktop it was not in the start menu or the app
grid either, because os.js builds those by reading the sidebar:

    $$('.sidebar .nav .nav-item[data-view]')

A view with no row there exists on a phone and nowhere else, which is the exact opposite of what an
editor for a NODE is for. Reported as "I don't even see it on the web version".

So the rule this pins is the general one, not a spelling: any view the desktop shell is expected to
launch must have a sidebar row, because that list IS the shell's app list.
"""
import os
import re
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TPL = os.path.join(ROOT, "templates", "client.html")
APP = os.path.join(ROOT, "static", "js", "client", "app.js")
OS_JS = os.path.join(ROOT, "static", "js", "client", "os.js")


def _read(p):
    with open(p, encoding="utf-8") as fh:
        return fh.read()


class CodeIsReachable(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tpl = _read(TPL)
        cls.app = _read(APP)
        cls.os = _read(OS_JS)

    def test_the_desktop_app_list_is_still_built_from_the_sidebar(self):
        """If this stops being true the rest of this file is measuring the wrong thing."""
        self.assertIn(".sidebar .nav .nav-item[data-view]", self.os,
                      "os.js no longer reads the sidebar for its app list — re-read this test")

    def test_code_has_a_sidebar_row(self):
        self.assertIn('data-view="code"', self.tpl,
                      "PosterChan Code has no sidebar row, so it is missing from the web sidebar, "
                      "the desktop start menu and the desktop app grid all at once")

    def test_code_is_not_in_the_mobile_more_sheet(self):
        """A node's file editor is not a phone screen.

        MEASURED ON THE SHEET'S OWN LIST, not on the whole file. This read `'PosterChan Code'`
        anywhere in a 2 MB app.js, which is a different question and started answering it wrongly
        the moment the open-chooser grew a button by that name \u2014 a sheet entry and a label in a
        dialog are not the same claim."""
        items = self.app[self.app.index("function moreMenu("):]
        items = items[items.index("const items=["):]
        items = items[:items.index("\n")]
        self.assertNotIn("PosterChan Code", items,
                         "Code is still listed in the mobile More sheet")
        self.assertIn("'Terminal'", items, "the More sheet list moved \u2014 re-point this test")

    def test_it_is_gated_like_the_terminal_not_more_loosely(self):
        """The jail defaults to the app's OWN checkout, so write access there is write access to the
        code this node runs. It shares the terminal's gate deliberately; widening one and not the
        other is how a privilege ends up quietly bigger than anybody meant."""
        code = _read(os.path.join(ROOT, "app", "routers", "code.py"))
        self.assertIn("user_allowed", code,
                      "the editor no longer shares the terminal's gate")

    def test_every_sidebar_view_is_one_the_client_can_render(self):
        """The desktop turns each of these into an app icon. A row naming a view renderView cannot
        route is a launcher entry that opens a dead screen."""
        rows = set(re.findall(r'nav-item"?\s+data-view="([a-z0-9_]+)"', self.tpl))
        self.assertIn("code", rows)
        handled = set(re.findall(r"VIEW===\s*'([a-z0-9_]+)'", self.app))
        handled |= set(re.findall(r"renderModuleView\('([a-z0-9_]+)'", self.app))
        missing = sorted(r for r in rows if r not in handled)
        self.assertEqual(missing, [], f"sidebar rows nothing renders: {missing}")


if __name__ == "__main__":
    unittest.main()
