"""The desktop's folder mapping must not fail to save in silence.

Reported as "why do I have to keep pointing Pictures to the Pictures folder on Desktop!" — every
launch, with nothing in any log.

`saveCfg()` swallowed every error (`catch (_) {}`), and that file holds the SYNC ROOTS: the mapping
from a folder pair to a directory on this disk. A failed write means the pick works, the sweep works
for that session, and on the next launch the bridge has no roots at all — the folder is still listed
by the page (that lives in localStorage and survives), its handle resolves to nothing, and the app
asks the user to point at it again.

Three properties, and the third is the one that makes it fixable rather than merely visible:
  * the failure is recorded and logged, not swallowed;
  * the write is READ BACK, because a save that "succeeded" and stored nothing is the same outcome;
  * the pick still SUCCEEDS and says so — refusing would trade a recurring annoyance for a feature
    that cannot be used at all.
"""
import os
import re
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MAIN = os.path.join(ROOT, "desktop", "main.js")
SYNC = os.path.join(ROOT, "static", "js", "client", "sync.js")


def _src(p):
    return open(p, encoding="utf-8").read()


def _fn(src, header):
    at = src.index(header)
    i = src.index("{", at)
    depth = 0
    while i < len(src):
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
            if depth == 0:
                break
        i += 1
    return src[at:i + 1]


class ConfigPersistTests(unittest.TestCase):
    def test_a_failed_save_is_not_swallowed(self):
        body = _fn(_src(MAIN), "function saveCfg()")
        self.assertNotIn("catch (_) {}", body,
                         "saveCfg still swallows every error; the sync roots live in this file")
        self.assertIn("cfgSaveFailed", body, "the failure is not recorded anywhere")
        self.assertIn("console.error", body, "the failure is not logged")

    def test_the_roots_are_read_back_after_writing(self):
        """A save that raises nothing and stores nothing is the same outcome to the user."""
        src = _src(MAIN)
        at = src.index("fsbridge.init({")
        block = src[at:at + 1400]
        self.assertIn("readFileSync", block, "nothing verifies the roots actually landed")
        self.assertIn("kept !== roots.length", block, "the read-back does not compare anything")

    def test_the_pick_reports_whether_it_will_survive_a_restart(self):
        src = _src(MAIN)
        at = src.index("ipcMain.handle('pc:fs:pick'")
        block = src[at:at + 900]
        self.assertIn("persisted", block, "the picker does not report whether the mapping stuck")

    def test_the_pick_still_succeeds_when_it_cannot_persist(self):
        """Refusing would trade a recurring annoyance for a folder that cannot be synced at all."""
        src = _src(MAIN)
        at = src.index("ipcMain.handle('pc:fs:pick'")
        block = src[at:at + 900]
        self.assertIn("persisted: false", block, "it does not report the failure at all")
        self.assertNotIn("if (cfgSaveFailed) return", block,
                         "a failed save now aborts the pick, so the folder cannot be used at all")

    def test_the_client_tells_the_user(self):
        src = _src(SYNC)
        # sync.js picks in more than one place (add, re-attach, Files); this is the ADD handler, which
        # is the one that pushes a new folder into the list.
        at = src.index("const list2 = folders();")
        block = src[max(0, at - 1200):at]
        self.assertIn("persisted === false", block, "the client ignores the flag")
        self.assertIn("ask again after a restart", block,
                      "the message does not say what will actually happen")
