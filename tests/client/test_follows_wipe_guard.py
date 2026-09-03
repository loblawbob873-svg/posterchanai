"""The follows list that kept resetting to 2, and the ratchet that made it permanent.

    "the people I follow keeps getting reset to only 2 despite following them back over and over"

The WRITE side has refused a near-empty republish for a long time. The READ side had no guard at
all: `fetchFollows` adopted whatever kind-3 came back, replaced the whole set with it, and then
`_persistFollows` wrote that length into `followsCount` — the very number `publish()`'s guard
measures against. So one short read did two things: it shrank the list, and it disarmed the guard
that exists to stop the next one. Re-following rebuilt the list; the next short read flattened it
again. That is the "over and over".

Measured on the live relay while diagnosing: the node held ONE kind-3 for this account carrying 239
p-tags, published minutes earlier. The good list was never the thing at fault — a client kept
publishing a short one over it, and nothing stopped it.

Both halves are checked here, including the case the guard must NOT catch: an ordinary unfollow.
The rule is deliberately identical to the write guard's, so the two cannot disagree about what
counts as a wipe.
"""
import json
import subprocess
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
APP = HERE.parent.parent / "static" / "js" / "client" / "app.js"


class FollowsSurviveAShortRead(unittest.TestCase):
    def test_a_short_read_neither_shrinks_the_list_nor_disarms_the_guard(self):
        p = subprocess.run(["node", str(HERE / "follows_wipe_runtime.mjs")],
                           text=True, capture_output=True, timeout=120)
        self.assertEqual(p.returncode, 0, p.stdout[-2000:] + p.stderr[-4000:])
        got = json.loads(p.stdout)

        self.assertEqual(got["adoptedHealthy"], 40, "a normal list is still adopted")
        self.assertEqual(got["cachedAfterHealthy"], 40)

        # The bug, in one line each.
        self.assertEqual(got["afterShortRead"], 40,
                         "a relay answering with 2 replaced the whole follow list")
        self.assertEqual(got["cachedAfterShortRead"], 40,
                         "the short read wrote itself into followsCount — this is the ratchet: the "
                         "write guard now measures against 2 and lets every wipe through")
        self.assertEqual(got["said"], 1,
                         "the refusal must be said out loud exactly once; silent is how the "
                         "original loss went unnoticed, per-refresh is spam")

        # …and the thing it must not break.
        self.assertEqual(got["afterOrdinaryShrink"], 38,
                         "an ordinary unfollow from another device is no longer adopted — the "
                         "guard is about wipes, not about every list that got shorter")
        self.assertEqual(got["afterOrdinaryReload"], 38,
                         "the recovery high-water mark resurrected legitimate unfollows on reload")
        self.assertEqual(got["safetyCount"], 40,
                         "a normal shrink ratcheted down the independent recovery snapshot")
        self.assertEqual(got["afterPoisonedReload"], 40,
                         "a poisoned mutable cache defeated recovery on the next cold start")
        self.assertEqual(got["ownProfile"], 40,
                         "our profile bypassed protected follow state and displayed the short relay answer")
        self.assertEqual(got["otherProfile"], 2,
                         "protecting our own profile contaminated another person's published following list")
        self.assertEqual(got["fromStoredHistory"], 55,
                         "the retained older kind-3 was ignored, so a cleared/poisoned localStorage "
                         "still made the newest short list authoritative")

    def test_the_write_guard_does_not_trust_localstorage_alone(self):
        """A device with no localStorage (fresh profile, cleared WebView, new install) read 0 for
        `known`, which is under the floor — so the guard was OFF exactly where the client is least
        sure of itself. It consults the Store, which is a local relay and holds what was seen."""
        src = APP.read_text(encoding="utf-8")
        guard = src[src.index("// Replaceable-list wipe guard"):]
        guard = guard[:guard.index("if(known>=8 && outP < Math.floor(known/2))")]
        self.assertIn("Store.query([{authors:[ME.pubkey],kinds:[kind],limit:1}])", guard,
                      "the shrink guard trusts localStorage alone again — a cleared profile "
                      "publishes a wipe unopposed")
        self.assertIn("const safe=_followSafetyMembers()", guard,
                      "the central publish guard lost its non-ratcheting follow witness")
        self.assertIn("ClientSettings.get('followsSafetyCache',[])", src,
                      "the recovery witness is no longer persisted independently of current follows")


if __name__ == "__main__":
    unittest.main()
