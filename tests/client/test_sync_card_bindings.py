"""Every control on the Folder Sync card is reachable, and none of them can kill the others.

`bindCard` wires the card's controls one after another in a single function. A line written as
`card.querySelector('.sync-x').onclick = …` throws the moment `.sync-x` is not in the markup — and
because they share one function, that throw takes EVERY control below it with it. The card still
draws perfectly and nothing reaches any log, so it surfaces as several unrelated dead buttons.

That is not hypothetical here: moving the secondary actions behind a "⋯ More" menu removed four
buttons from the markup whose handlers were bound exactly that way, and Stop syncing was below all
four of them.

So: an unguarded binding must name a class the markup actually contains, and every action the menu
offers must exist. Source-read, because the failure is structural and a rendered check would need a
filesystem bridge this box does not have.
"""
import os
import re
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SRC = os.path.join(ROOT, "static", "js", "client", "sync.js")


class SyncCardBindings(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(SRC, encoding="utf-8") as fh:
            cls.src = fh.read()

    def _classes_in_markup(self):
        return set(re.findall(r'class="[^"]*?\b(sync-[a-z0-9-]+)\b', self.src))

    def test_no_unguarded_binding_names_a_button_that_is_not_there(self):
        unguarded = set(re.findall(r"card\.querySelector\('\.([a-z0-9-]+)'\)\.on", self.src))
        self.assertTrue(unguarded, "the binding style changed — re-read this test before trusting it")
        missing = sorted(c for c in unguarded if c not in self._classes_in_markup())
        self.assertEqual(missing, [], "these are bound without a null check and are not in the "
                                      "markup, so bindCard throws and every control after them "
                                      "silently stops working")

    def test_the_menu_offers_only_actions_that_exist(self):
        i = self.src.index("const more = card.querySelector('.sync-more')")
        block = self.src[i:i + 1800]
        offered = set(re.findall(r"items\.push\(\['([a-z]+)'", block)) \
            | set(re.findall(r"\[\['([a-z]+)'", block))
        self.assertTrue(offered, "the menu builder changed shape — re-read this test")
        for a in sorted(offered):
            self.assertIn("if(a === '%s')" % a, block,
                          "the menu offers '%s' and nothing handles it — a dead menu row" % a)

    def test_every_menu_action_calls_a_function_that_is_defined(self):
        i = self.src.index("const more = card.querySelector('.sync-more')")
        block = self.src[i:i + 1800]
        for fn in sorted(set(re.findall(r"return (_do[A-Za-z]+)\(\)", block))):
            self.assertIn("const %s = " % fn, self.src,
                          "the menu calls %s(), which is not defined — the row throws on click" % fn)

    def test_the_urgent_actions_stay_on_the_card(self):
        """A recovery buried in a menu is a recovery nobody finds, and this feature has spent its
        whole life being unable to tell people what it did. The primary action, the two conditional
        rescues and Stop syncing are the four that must not move."""
        for c in ("sync-now", "sync-putback", "sync-restore", "sync-forget", "sync-more"):
            self.assertIn(c, self._classes_in_markup(), "%s left the card" % c)


if __name__ == "__main__":
    unittest.main()
