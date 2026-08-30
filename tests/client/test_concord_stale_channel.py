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
        # The channel read moved into absorbChatWraps when the live subscription was added, so
        # both arrivals share one merge. This checks the function that actually reads the channel,
        # whichever one that is — pinned to refreshActiveChannel it would have gone green on an
        # extraction that quietly reintroduced a direct reader call next door.
        tick = src[src.index("async function absorbChatWraps("):]
        tick = tick[:tick.index("async function refreshRoomMetadata(")]
        self.assertIn("readChat(p,reader,bundle,controlWraps,room,channel", tick,
                      "the live tick no longer goes through the repair")
        self.assertNotIn("reader.inspectChat(", tick,
                         "the live tick calls the reader directly again — the repair is bypassed")

    def test_hydration_goes_through_the_repair_too(self):
        """THE LIVE TICK WAS FIXED AND HYDRATION WAS NOT, WHICH IS WHY IT CAME BACK.

        Reported as two sentences that are one bug:

            "every time I choose a room i am a member of: could not refresh community"
            "room history is not readbale with this membership"

        `hydrateRoomStreams` replays the ON-DISK envelope cache before it opens a socket, and its
        `applyChannel` called `reader.inspectChat` directly — the only read left in the file that
        skipped `readChat`, i.e. the only one with no id-repair. A saved channel the control set
        does not carry threw straight out of the whole job, and because the source is a cache the
        throw is deterministic: same failure on every open, for ever.
        """
        src = open(CONCORD, encoding="utf-8").read()
        hydrate = src[src.index("async function hydrateRoomStreams("):]
        hydrate = hydrate[:hydrate.index("async function publishCordNative(")]
        self.assertIn("readChat(p,reader,bundle,controlWraps", hydrate,
                      "hydration no longer goes through the repair")
        self.assertNotIn("reader.inspectChat(", hydrate,
                         "hydration calls the reader directly again — the repair is bypassed")

    def test_one_unreadable_cached_channel_does_not_abort_the_room(self):
        """The repair cannot help when the control set genuinely lacks the channel, and then the
        question is what the ROOM does. The network half already records such a channel and moves
        on; the cached half threw, and its own rescue below could not catch it because that rescue
        needs `cachedHistoryRendered` — set on the very line that threw."""
        src = open(CONCORD, encoding="utf-8").read()
        hydrate = src[src.index("async function hydrateRoomStreams("):]
        hydrate = hydrate[:hydrate.index("async function publishCordNative(")]
        # The replay is `replayCached` now — it was lifted out of the serial loop when the cached
        # half stopped waiting for every channel in the room (test_concord_cached_prefetch.py).
        # Anchor on the function, not on the loop that used to hold it.
        replay = hydrate[hydrate.index("const replayCached=async channel=>{"):]
        replay = replay[:replay.index("const [cachedHead,...cachedRest]=ordered;")]
        self.assertIn("not readable with this membership", replay,
                      "the cached replay no longer tolerates an unreadable channel — one stale "
                      "channel id takes the whole room again, on every open")
        self.assertIn("cachedStale.push(channel.name)", replay,
                      "the unreadable channel is no longer recorded, so a half-empty room cannot "
                      "say which channel it could not read")
