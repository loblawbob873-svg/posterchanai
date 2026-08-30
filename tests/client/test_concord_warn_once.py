"""A wall of the same error is not a log.

refreshActiveChannel runs on a four-second timer, so a condition that persists — a control stream
the relay has not answered for yet, which is ordinary on a phone — printed the same line for ever.
Reported from Android as a wall of "channel is not readable with this membership".

The condition is worth reporting; repeating it is not, and a log nobody can read is a log nobody
reads when something new goes wrong. A DIFFERENT failure on the same channel still prints, and a
channel that recovers reports again if it breaks again.
"""
import os
import shutil
import subprocess
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
RUNTIME = os.path.join(ROOT, "tests", "client", "concord_warn_once_runtime.mjs")


@unittest.skipIf(shutil.which("node") is None, "node not installed")
class OnePersistentFailureIsReportedOnce(unittest.TestCase):
    def test_a_repeating_tick_does_not_repeat_its_error(self):
        r = subprocess.run(["node", RUNTIME], capture_output=True, text=True, timeout=120)
        self.assertEqual(r.returncode, 0, r.stdout[-2000:] + r.stderr[-3000:])
