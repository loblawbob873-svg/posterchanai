"""Runs reconcile_algo.mjs (the timeline reconcile algorithm) under node.

Run: venv-unified/bin/python -m unittest tests.client.test_reconcile
     (skips itself if node isn't on the box)

The assertions live in the .mjs file so the algorithm under test can be a verbatim copy of the one in
app.js — see the header there for what each case is for and why identity-reuse is the property that matters.
"""
import os
import shutil
import subprocess
import unittest

SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "reconcile_algo.mjs")
NODE = shutil.which("node")


@unittest.skipIf(not NODE, "no node on this box")
class TestTimelineReconcile(unittest.TestCase):
    def test_reconcile_cases(self):
        p = subprocess.run([NODE, SCRIPT], capture_output=True, text=True, timeout=60)
        self.assertEqual(p.returncode, 0, "\n" + p.stdout + p.stderr)
        self.assertIn("FAILURES: 0", p.stdout, p.stdout)
        # Guard the guard: an .mjs that threw before running anything would also print no failures.
        self.assertGreaterEqual(p.stdout.count("PASS"), 14, p.stdout)


if __name__ == "__main__":
    unittest.main()
