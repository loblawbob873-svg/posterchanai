"""A room is not one serial queue, and one bad channel is not a bad room.

Reported as Concord being slow, with "could not refresh room history" toasts, worst on the big
Armada rooms. Both come out of the same loop:

  * every channel waited on the previous channel's relay round trip, so a ten-channel community
    cost ten of them before the room was usable;
  * and a single throw abandoned every channel behind it, surfaced as that toast, and left
    `hydrated` unset — so the whole room was fetched again on the next click, which made the next
    failure likelier and the room slower still.

The channel on screen is still fetched first and alone, and is still allowed to throw: failing to
load the conversation somebody just opened is worth saying out loud. Everything after it is
prefetch, runs four at a time, and a channel that will not load is left to the live tick, which
fetches whatever channel is actually open.
"""
import os
import shutil
import subprocess
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
RUNTIME = os.path.join(ROOT, "tests", "client", "concord_hydrate_parallel_runtime.mjs")


@unittest.skipIf(shutil.which("node") is None, "node not installed")
class HydrationIsParallelAndFaultTolerant(unittest.TestCase):
    def test_channels_load_together_and_one_failure_does_not_take_the_room(self):
        r = subprocess.run(["node", RUNTIME], capture_output=True, text=True, timeout=180)
        self.assertEqual(r.returncode, 0, r.stdout[-2000:] + r.stderr[-4000:])
