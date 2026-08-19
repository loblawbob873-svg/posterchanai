"""The Sync screen must not rebuild itself while a sweep runs.

Run: venv-unified/bin/python -m unittest tests.client.test_sync_repaint

Reported from use, on a tablet pulling down a large Pictures folder: "the UI keeps refreshing during
sync". Three things compounded, and each one on its own is enough to cause it:

  1. A sweep's own downloads ARE filesystem changes, so the watcher fires once per file written. Each
     notification asked for a sweep, and each ask ran the battery/network policy — thousands of times
     during one download.
  2. Every one of those asks was refused (a sweep is already running, or one just finished), and the
     refusal was reported through the path that rebuilds the whole screen. A skip is one line of
     text; it does not need every card redrawn.
  3. Nothing coalesced repaints, so each of those rebuilds happened in full, one after another.

The third is also a data problem rather than a comfort one: the exclusions box is a textarea inside
those cards, so a rebuild mid-edit discards what was being typed.

Source assertions — sync.js cannot be imported here (it wants localStorage, a relay and a DOM), so
these prove the guards are present, not that they work. The behaviour they protect is covered by
tests/client/test_sync_store_scale.py at the store level.
"""

import os
import re
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SYNC = os.path.join(ROOT, "static", "js", "client", "sync.js")


def src():
    with open(SYNC, encoding="utf-8", errors="replace") as fh:
        return fh.read()


class TestSkipsDoNotRedrawTheScreen(unittest.TestCase):
    def test_a_declined_sweep_updates_only_its_own_line(self):
        body = src()
        # The branch, then the setStatus INSIDE it — not the two glued together on one line. They
        # stopped being adjacent when the decline grew its "· watching (…)" wording, and a regex
        # that wanted them adjacent has been failing (i.e. measuring nothing) ever since.
        i = body.index("if(!decision.run && !o.dryRun){")
        branch = body[i:body.index("return { skipped:true, why:decision.why };", i)]
        m = re.search(r"setStatus\(([^;]*)\);", branch)
        self.assertIsNotNone(m, "the decline branch has moved — check it still reports live-only")
        self.assertIn("true", m.group(1),
                      "a declined sweep must pass liveOnly; it is the commonest outcome there is, and "
                      "rebuilding every card for each one is what made the screen flicker")

    def test_starting_a_sweep_updates_only_its_own_line(self):
        self.assertIn("setStatus(f.id, o.dryRun ? 'checking…' : 'syncing…', null, true);", src())

    def test_the_final_report_still_repaints(self):
        """Deliberately NOT live-only: the summary carries a report, and the details it renders are
        part of the card, not the status line. Making this one live-only would silently stop showing
        what a sweep actually did."""
        self.assertIn("setStatus(f.id, summarise(rep, decision), rep);", src())


class TestTheWatcherIsCoalesced(unittest.TestCase):
    def test_change_notifications_are_debounced(self):
        body = src()
        i = body.index("fs.onChanged(")
        window = body[i:i + 700]
        self.assertIn("setTimeout", window,
                      "onChanged must coalesce: a sweep writing 1000 files notifies 1000 times")
        self.assertIn("clearTimeout", window)

    def test_the_dirty_flag_is_set_before_the_delay(self):
        """The delay may only defer the ASKING. Deferring the flag as well would lose the knowledge
        that something changed if the app is closed inside the window."""
        body = src()
        i = body.index("fs.onChanged(")
        window = body[i:i + 400]
        self.assertLess(window.index("_dirty = true"), window.index("setTimeout"),
                        "_dirty must be set immediately, not inside the debounce")


class TestRepaintsAreCoalesced(unittest.TestCase):
    def test_paint_is_not_a_direct_rebuild(self):
        body = src()
        self.assertIn("function _paintNow()", body)
        self.assertRegex(body, r"function paint\(\)\{[\s\S]*?_paintNow\(\)",
                         "paint() must go through the coalescer, not rebuild directly")

    def test_a_repaint_never_lands_under_a_cursor(self):
        body = src()
        self.assertIn("_editing()", body)
        i = body.index("function _editing()")
        window = body[i:i + 300]
        self.assertIn("TEXTAREA", window,
                      "the exclusions box is a textarea; a rebuild mid-edit throws away the text")

    def test_a_deferred_repaint_is_not_dropped(self):
        """Skipping a repaint while typing is only acceptable because it is re-queued — otherwise the
        screen would sit stale until something else happened to redraw it."""
        body = src()
        i = body.index("function paint()")
        # To the END of the function, not a fixed window — a comment added at the top of paint()
        # pushed the coalescing out of a 400-character view and made this red for prose.
        self.assertIn("_paintQ = true", body[i:body.index("\n  }", i)])


if __name__ == "__main__":
    unittest.main()
