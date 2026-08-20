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

    def _menu(self):
        """The menu builder, found by MATCHING BRACES rather than by taking a fixed slice.

        It used to be `self.src[i:i + 1800]`, and a comment explaining a new row pushed the handler
        past the end — so the test reported "the menu offers 'check' and nothing handles it", about
        a row that had been handled for months. A guard that fails when somebody writes a paragraph
        is a guard people learn to edit rather than believe.
        """
        i = self.src.index("const more = card.querySelector('.sync-more')")
        j = self.src.index("{", self.src.index("more.onclick", i))
        depth, k = 0, j
        while k < len(self.src):
            if self.src[k] == "{":
                depth += 1
            elif self.src[k] == "}":
                depth -= 1
                if depth == 0:
                    return self.src[i:k + 1]
            k += 1
        raise AssertionError("the menu builder's braces do not close — re-read this test")

    def test_the_menu_offers_only_actions_that_exist(self):
        block = self._menu()
        offered = set(re.findall(r"items\.push\(\['([a-z]+)'", block)) \
            | set(re.findall(r"\[\['([a-z]+)'", block))
        self.assertTrue(offered, "the menu builder changed shape — re-read this test")
        for a in sorted(offered):
            self.assertIn("if(a === '%s')" % a, block,
                          "the menu offers '%s' and nothing handles it — a dead menu row" % a)

    def test_every_menu_action_calls_a_function_that_is_defined(self):
        block = self._menu()
        for fn in sorted(set(re.findall(r"return (_do[A-Za-z]+)\(\)", block))):
            self.assertIn("const %s = " % fn, self.src,
                          "the menu calls %s(), which is not defined — the row throws on click" % fn)

    def test_the_urgent_actions_stay_on_the_card(self):
        """A recovery buried in a menu is a recovery nobody finds, and this feature has spent its
        whole life being unable to tell people what it did. The primary action, the two conditional
        rescues and Stop syncing are the four that must not move."""
        for c in ("sync-now", "sync-putback", "sync-restore", "sync-forget", "sync-more"):
            self.assertIn(c, self._classes_in_markup(), "%s left the card" % c)


class EveryPickIsRecordedAsAGrant(unittest.TestCase):
    """"still says to point Documents after adding it."

    `granted` is fetched when the screen paints, so a folder attached afterwards is not in it and
    the next repaint draws "Point at the folder again…" over a folder that was just pointed at. The
    ADD path has recorded the grant for a while. The ATTACH path never did — and attach is the path
    for a folder the account ALREADY syncs, which is every folder being re-established on a second
    device. So the banner appeared on the path people use most.
    """

    @classmethod
    def setUpClass(cls):
        with open(SRC, encoding="utf-8") as fh:
            cls.src = fh.read()

    def _handler(self, marker):
        """To the end of the handler, not a byte count — the add path runs to ~4 KB of code and
        comment, and a fixed window is how three other tests in this repo ended up asserting
        against somebody else's code today."""
        i = self.src.index(marker)
        end = self.src.find("\n    const add = document.getElementById", i + 1)
        if end < 0:
            end = self.src.find("\n    feed.querySelectorAll('.sync-card')", i + 1)
        return self.src[i:end if end > i else i + 6000]

    def test_the_attach_path_records_the_grant(self):
        seg = self._handler("feed.querySelectorAll('.sync-attach')")
        self.assertIn("granted.push(", seg,
                      "attaching a folder does not record the grant, so the card claims the folder "
                      "needs pointing at until something else proves it")

    def test_the_add_path_still_does(self):
        seg = self._handler("const add = document.getElementById('sync-add')")
        self.assertIn("granted.push(", seg)

    def test_both_guard_against_a_missing_grant_list(self):
        """`granted` is null while the platform is being asked and null again if that failed —
        pushing into either is a throw inside a click handler, which reads as the button doing
        nothing at all."""
        for marker in ("feed.querySelectorAll('.sync-attach')",
                       "const add = document.getElementById('sync-add')"):
            seg = self._handler(marker)
            i = seg.index("granted.push(")
            self.assertIn("Array.isArray(granted)", seg[:i],
                          "%s pushes without checking the list exists" % marker)


if __name__ == "__main__":
    unittest.main()
