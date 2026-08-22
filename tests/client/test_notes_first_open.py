"""Notes and Terminal must survive ordinary desktop focus/mount transitions."""
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


class CoreAppsOpenReliably(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = (ROOT / "static/js/client/app.js").read_text(encoding="utf-8")
        cls.notes = (ROOT / "static/js/client/notes.js").read_text(encoding="utf-8")
        cls.term = (ROOT / "static/js/client/term.js").read_text(encoding="utf-8")

    def test_notes_route_reloads_a_missing_module_and_renders_it(self):
        route = self.app[self.app.index("if (VIEW==='notes'){"):]
        route = route[:route.index("if (VIEW==='texts')")]
        self.assertIn("_withNotes", route)
        self.assertIn("if(VIEW==='notes') m.render()", route)
        self.assertIn("_withModule('notes.js', 'PCNotes'", self.app)

    def test_notes_paints_the_shell_before_decrypting_the_cache(self):
        load = self.notes[self.notes.index("async function _loadCache()"):]
        load = load[:load.index("async function refresh()")]
        self.assertLess(load.index("_paint();"), load.index("await _absorb"))
        self.assertIn("n === 1", load)

    def test_background_focus_cannot_resize_the_terminal_pty(self):
        fit = self.term[self.term.index("function _fit()"):]
        fit = fit[:fit.index("function _send(")]
        guard = "frame && !frame.classList.contains('focused')"
        self.assertIn(guard, fit)
        self.assertLess(fit.index(guard), fit.index("fit.fit()"))


if __name__ == "__main__":
    unittest.main()
