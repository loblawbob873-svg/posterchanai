"""Puts state_sim.js — the reconciler's own suite — INTO the suite.

Found in a code review, not by a failure: every other sim here has a wrapper
(exec_sim → test_scenarios, sync_store_sim → test_sync_store_scale, sync_tick_sim → test_sync_tick,
forget_sim → test_forget_sim) and this one — merge determinism, "no single view can empty the
folder", the whole state table, every guard: the tests the rewrite's safety case rests on — had
none. pytest collects .py files, so `node state_sim.js` passing on a laptop meant nothing about any
deploy: the engine could have regressed on the exact properties that ended three days of data loss
and ./test.sh would have stayed green.
"""
import os
import re
import shutil
import subprocess
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SIM = os.path.join(ROOT, "tests", "client", "state_sim.js")
NODE = shutil.which("node") or shutil.which("nodejs")


@unittest.skipIf(not NODE, "no node on this node")
class EngineSimTests(unittest.TestCase):
    def test_the_reconciler_suite_passes(self):
        r = subprocess.run([NODE, SIM], capture_output=True, text=True, timeout=120)
        self.assertEqual(r.returncode, 0, (r.stdout + r.stderr)[-3000:])
        self.assertIn("all", r.stdout)
        self.assertIn("passed", r.stdout)

    def test_no_case_has_gone_missing(self):
        """The sim's own harness only reports what still exists — a T() deleted in a refactor is
        invisible to it. 21 is the count at wiring time; raising it is free, lowering it is a
        deliberate act with this line as the receipt."""
        with open(SIM, encoding="utf-8") as fh:
            n = len(re.findall(r"\bok\(", fh.read()))
        self.assertGreaterEqual(n, 40, "state_sim.js lost test cases (%d left)" % n)

    def test_the_harness_can_actually_fail(self):
        """A suite whose failure path is broken is a green light wired to nothing. Run the sim with
        one assertion forced false and demand a nonzero exit."""
        with open(SIM, encoding="utf-8") as fh:
            src = fh.read()
        at = src.index("console.log(failures")
        broken = src[:at] + "ok(false, 'sentinel');\n" + src[at:]
        r = subprocess.run([NODE, "-e", broken], capture_output=True, text=True, timeout=120,
                           cwd=os.path.dirname(SIM))
        self.assertNotEqual(r.returncode, 0, "a failing case did not fail the run")
        self.assertIn("sentinel", r.stdout + r.stderr)


if __name__ == "__main__":
    unittest.main()
