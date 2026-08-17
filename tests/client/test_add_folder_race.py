"""Adding a folder writes against a FRESH list, not one read before the name prompt.

Reported as "when you add the folder, you have to do the point again so it sticks".

The handler read the folder list, then awaited `PC.uiPrompt` for the pair name — seconds of somebody
typing — and then pushed and saved that snapshot. Every other writer of that list works the same way
(a sweep recording lastSyncAt, the watcher, a repaint), so in that window one of two things happens:
this write loses their change, or theirs lands after and loses the folder that was just added. The
second is what people see: the folder appears, then quietly is not there.

Structural, and honest about it: proving the race by running it would need the real DOM, the real
prompt and a concurrent sweep. What can be pinned is the ORDER — the list used for the write is read
after the awaits, not before.
"""
import os
import re
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SYNC = os.path.join(ROOT, "static", "js", "client", "sync.js")


class AddFolderTests(unittest.TestCase):
    def setUp(self):
        src = open(SYNC, encoding="utf-8").read()
        at = src.index("const add = document.getElementById('sync-add')")
        self.block = src[at:src.index("feed.querySelectorAll('.sync-card')", at)]

    def test_the_saved_list_is_read_after_the_prompt(self):
        prompt = self.block.index("PC.uiPrompt(")
        save = self.block.index("saveFolders(")
        fresh = self.block.rindex("= folders();", 0, save)
        self.assertGreater(fresh, prompt,
                           "the list written to storage was read BEFORE the name prompt, so anything "
                           "that writes it while the user types is lost — or loses this folder")

    def test_the_write_and_the_read_use_the_same_variable(self):
        """A fresh read that is then ignored would be worse than none: it reads like a fix."""
        save = self.block.index("saveFolders(")
        saved = re.search(r"saveFolders\((\w+)\)", self.block[save:]).group(1)
        # the LAST read before the write is the one that matters; an earlier snapshot may legitimately
        # still exist for the duplicate check.
        reads = [m.group(1) for m in re.finditer(r"const (\w+) = folders\(\);", self.block[:save])]
        self.assertTrue(reads, "nothing reads the folder list any more")
        self.assertEqual(reads[-1], saved,
                         "it re-reads the list and then saves a different, older one")

    def test_the_duplicate_check_still_happens_before_anything_is_written(self):
        self.assertIn("already syncing", self.block, "the duplicate check is gone")
        self.assertLess(self.block.index("already syncing"), self.block.index("saveFolders("))
