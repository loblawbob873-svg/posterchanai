"""Every folder-sync failure that was reported in the field, as a scenario against the real engine.

`scenarios_sim.js` runs them; this puts them in the suite and states what each one is for, so a
scenario that goes missing is a failing test rather than a quiet gap.

The pattern these exist to break: each individual bug had a test, and fixing it moved the failure to
a place none of those tests were looking. A scenario suite fails on the MOVE.
"""
import os
import re
import subprocess
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SIM = os.path.join(ROOT, "tests", "client", "exec_sim.js")

# Each entry is a real report. Removing a scenario has to be a deliberate act, not an omission.
REQUIRED = [
    "a fresh pair",                        # "i want to add the folder to be synced and be good to go"
    "two hosts updating at the same time",  # "need to support 2 hosts or more updating at the same time"
    "three hosts, all writing",
    "a server that cannot be asked",         # "UPDATE APK DURING SYNC ... SENDING EVERYTHING TO TRASH"
    "the store was emptied by hand",        # "i cleared out the Pictures in blossom"
    "the folder handle is gone",            # a device that cannot see its own folder
    "a reinstall lost the journal",         # "why is the phone always downloading 1/32"
    "an interrupted sweep resumes",         # "needs to be able to resume where it left off"
    "corrupt bytes are refused",            # "checksumming"
    "a corrupt large file is refused too",
    "the consistency check finds corruption",   # "consistency check on the server and clients"
    "a delete on one device reaches the other",
    "a restored backup",                    # 3,930 files republished by an rsync without -t
    "big files go one at a time",           # the Electron/WebView memory ceiling
    "a settled folder is quiet",
    "a journal that cannot be read",
    "a copy that fails its checksum is not fetched again",
    "the preview and the sweep agree",
    "records the folder lost are put back",
    "the scale that killed the desktop",    # "make sure electron desktop apps don't shit themselves"
    "the CAS race",                         # two devices editing one file at once — both survive
    "remove-and-re-add cannot haunt",       # the era; "instantly has 373 conflicts"
    "the receipts",                         # the torn store copy that healed itself, end to end
]


class ScenarioTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.out = subprocess.run(["node", SIM], capture_output=True, text=True, timeout=1800)

    def test_every_scenario_passes(self):
        self.assertEqual(self.out.returncode, 0, self.out.stdout + self.out.stderr)

    def test_no_scenario_has_gone_missing(self):
        for name in REQUIRED:
            self.assertIn(name, self.out.stdout, "the scenario for “%s” is no longer run" % name)

    def test_they_all_actually_ran(self):
        ran = len(re.findall(r"^ok    ", self.out.stdout, re.M))
        self.assertGreaterEqual(ran, len(REQUIRED),
                                "only %d scenarios ran, expected at least %d" % (ran, len(REQUIRED)))
