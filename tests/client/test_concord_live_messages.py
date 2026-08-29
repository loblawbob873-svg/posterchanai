"""Messages arrive; they are not fetched for.

The live tick runs every four seconds, but the query it makes carries `minInterval:60000` — so on
any community whose relays are not already in the shared pool, a message could take a FULL MINUTE
to appear. Tightening the timer cannot fix that: polling a relay that is perfectly capable of
pushing is the wrong shape, and `p.relaySubscribe` already existed — discovery was the only thing
using it.

The runtime beside this drives the real startChatLive/flushChatLive with a fake subscription whose
query methods THROW, so a test that passed by polling could not pass at all.
"""
import os
import shutil
import subprocess
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
RUNTIME = os.path.join(ROOT, "tests", "client", "concord_live_messages_runtime.mjs")
CONCORD = os.path.join(ROOT, "static", "js", "client", "concord.js")


@unittest.skipIf(shutil.which("node") is None, "node not installed")
class MessagesArriveLive(unittest.TestCase):
    def test_a_pushed_message_reaches_the_channel_without_a_poll(self):
        r = subprocess.run(["node", RUNTIME], capture_output=True, text=True, timeout=120)
        self.assertEqual(r.returncode, 0, r.stdout[-2000:] + r.stderr[-4000:])

    def test_the_live_tick_arms_the_subscription(self):
        """The runtime drives startChatLive directly, so this is what holds the WIRING. Nothing
        else calls it: unarmed, the subscription is dead code and the channel is back to polling
        once a minute with every test still green."""
        src = open(CONCORD, encoding="utf-8").read()
        tick = src[src.index("async function refreshActiveChannel("):]
        tick = tick[:tick.index("async function refreshRoomMetadata(")]
        self.assertIn("startChatLive(p,room,channel)", tick,
                      "the live tick no longer arms the message subscription")

    def test_leaving_the_view_closes_the_stream(self):
        """A subscription nobody closes is a socket that keeps delivering into a store the reader
        has left — and on a room switch, into the wrong room's store."""
        src = open(CONCORD, encoding="utf-8").read()
        self.assertIn("function stopLiveSync()", src)
        stop = src[src.index("function stopLiveSync()"):]
        stop = stop[:stop.index("function startLiveSync")]
        self.assertIn("stopChatLive()", stop)
