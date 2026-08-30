"""Opening a room may decrypt the channel you opened. It may not decrypt the other twelve first.

The cached replay in hydrateRoomStreams was a serial loop over every channel in the community,
decrypting a 300-envelope page each before the next began, with the whole network pass behind it.
That is NIP-44 on the main thread. Measured against Soapbox's real community: 13 channels, a
300-wrap page ~560ms, so five to seven seconds of decryption for conversations nobody had opened —
spent BEFORE the first relay was asked, and growing with the cache, which is why it got worse the
longer the app was used.

The network half of the same function already knew this ("THE REST IS PREFETCH, AND PREFETCH IS NOT
WORTH WAITING FOR"). This is that rule applied to the half that reads the disk.
"""
import json
import subprocess
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
RUNTIME = HERE / "concord_cached_prefetch_runtime.mjs"


class CachedReplayIsNotOnTheCriticalPath(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        out = subprocess.run(["node", "--unhandled-rejections=strict", str(RUNTIME)],
                             capture_output=True, text=True, timeout=180)
        if out.returncode:
            raise AssertionError("the room-open runtime failed: " + (out.stderr or "")[-2000:])
        cls.got = json.loads(out.stdout.strip().splitlines()[-1])

    def test_the_room_does_not_wait_for_most_of_its_channels(self):
        """Measured, not asserted exactly: with the fix the room waits on the open channel and
        whatever the background prefetch happened to finish while the network head was in flight —
        that overlap is correct behaviour, so pinning an exact list would be pinning a race. What
        cannot happen is waiting for the WHOLE room. Old code: 8 of 8 distinct channels decrypted
        before resolve. New: 2."""
        at = self.got["decryptedAtResolve"]
        distinct = sorted(set(at))
        total = self.got["channels"]
        self.assertIn(self.got["selected"], at,
                      "the channel actually on screen was not decrypted before the room opened: %s" % at)
        self.assertLessEqual(len(distinct), total // 2,
                             "opening a room waited on the cached history of %d of %d channels (%s). "
                             "Each is a NIP-44 page nobody asked to read, spent before the first "
                             "relay request goes out — and it grows with the cache, which is why "
                             "this got worse the longer the app was used."
                             % (len(distinct), total, distinct))

    def test_the_other_channels_are_still_replayed_afterwards(self):
        """Off the critical path, NOT dropped: switching channels must still be warm."""
        # DISTINCT channels: every channel is decrypted twice over a full open — once from the
        # cache and again from the relay answer — so counting the raw list counts the round trips,
        # not the coverage.
        seen = sorted(set(self.got["decryptedEventually"]))
        self.assertEqual(self.got["channels"], len(seen),
                         "the prefetch stopped replaying the rest of the room (%s of %d) — moving it "
                         "off the critical path must not turn it off"
                         % (seen, self.got["channels"]))

    def test_the_open_channel_is_replayed_first(self):
        self.assertEqual(self.got["selected"], self.got["decryptedEventually"][0],
                         "the channel on screen was not the first one decrypted")


if __name__ == "__main__":
    unittest.main()
