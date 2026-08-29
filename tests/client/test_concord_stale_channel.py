"""A stale channel name must not read as a refused membership, on a four-second loop.

Reported verbatim, with the whole stack, from a live tick:

    Concord live sync failed Error: channel is not readable with this membership
        inspectChat cord-reader.js
        refreshActiveChannel concord.js
        liveTimer concord.js

`room.channels` is saved to localStorage and `applyControl` replaces it only when a control fetch
yields something, so a room can keep a channel the community has since renamed or removed — or one
a partial fetch once produced. The live tick picks that channel BY NAME and hands its id to the
reader, whose readable set comes from the control events and from nothing else. It refuses,
correctly, with a message about MEMBERSHIP; the actual fault is bookkeeping. On a timer it never
stops, so the room never syncs for the whole visit.
"""
import os
import shutil
import subprocess
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
RUNTIME = os.path.join(ROOT, "tests", "client", "concord_stale_channel_runtime.mjs")
CONCORD = os.path.join(ROOT, "static", "js", "client", "concord.js")


@unittest.skipIf(shutil.which("node") is None, "node not installed")
class StaleChannelIsRepaired(unittest.TestCase):
    def test_the_live_tick_repairs_a_stale_channel_instead_of_failing_for_ever(self):
        r = subprocess.run(["node", RUNTIME], capture_output=True, text=True, timeout=120)
        self.assertEqual(r.returncode, 0, r.stdout[-2000:] + r.stderr[-4000:])

    def test_the_live_tick_actually_goes_through_the_repair(self):
        """The runtime drives `readChat` directly, so this is what holds the WIRING: a live tick
        that called the reader itself would be green in there and broken on the screen."""
        src = open(CONCORD, encoding="utf-8").read()
        tick = src[src.index("async function refreshActiveChannel("):]
        tick = tick[:tick.index("async function refreshRoomMetadata(")]
        self.assertIn("readChat(p,reader,bundle,controlWraps,room,channel", tick,
                      "the live tick no longer goes through the repair")
        self.assertNotIn("reader.inspectChat(", tick,
                         "the live tick calls the reader directly again — the repair is bypassed")
