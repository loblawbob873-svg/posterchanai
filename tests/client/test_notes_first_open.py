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
        cls.sms = (ROOT / "static/js/client/sms.js").read_text(encoding="utf-8")

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

    def test_network_backfill_progressively_reveals_the_library(self):
        refresh = self.notes[self.notes.index("async function refresh()"):
                             self.notes.index("function _stamp()")]
        self.assertIn("await _absorb(_lib, live, n =>", refresh)
        self.assertIn("n % 12 === 0", refresh)

    def test_background_focus_cannot_resize_the_terminal_pty(self):
        fit = self.term[self.term.index("function _fit()"):]
        fit = fit[:fit.index("function _send(")]
        guard = "frame && !frame.classList.contains('focused')"
        self.assertIn(guard, fit)
        self.assertLess(fit.index(guard), fit.index("fit.fit()"))
        self.assertIn("_fitPixels===px && _sentSize", fit)
        self.assertLess(fit.index("_fitPixels===px"), fit.index("fit.fit()"))

    def test_every_module_backed_app_heals_its_first_open(self):
        for view, file_name, global_name, method in (
            ('news','news.js','PCNews','render'), ('websearch','websearch.js','PCWebSearch','render'),
            ('terminal','term.js','PCTerm','render'), ('calendar','calendar.js','PCCalendar','render'),
            ('contacts','contacts.js','PCContacts','render'), ('markets','markets.js','PCMarkets','render'),
            ('meme','meme.js','PCMeme','render'), ('stats','stats.js','PCStats','render'),
            ('budget','budget.js','PCBudget','render'), ('sync','sync.js','PCSync','paint'),
            ('vault','vault.js','PCVault','render'), ('xdc','webxdc.js','PCWebxdc','gallery')):
            call = "renderModuleView(%r,%r,%r,%r)" % (view, file_name, global_name, method)
            self.assertIn(call, self.app, '%s can remain on a spinner after a cold first click' % view)

    def test_texts_paints_newest_messages_before_draining_the_archive(self):
        load = self.sms[self.sms.index("async function load(force)"):]
        load = load[:load.index("let _refreshing")]
        self.assertIn("cached.splice(0, 32)", load)
        self.assertLess(load.index("S.ready = true"), load.index("while(cached.length)"))
        self.assertIn("cached.splice(0, 128)", load)


if __name__ == "__main__":
    unittest.main()
