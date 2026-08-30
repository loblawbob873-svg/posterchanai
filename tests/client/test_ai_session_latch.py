"""A slow app-session must not become a permanent one.

Reported across two devices in one afternoon:

    "AI Chat just a circle"
    "PosterChan AI app on phone just spinning circle, even though it uses nsec"
    "posterchan ai window finally loaded on desktop"

The third line is the diagnosis. Nothing was broken; the session was SLOW — and it happened on a
LOCAL nsec as well as the built-in signer, which rules out "the signer was asked and did not
answer" as the whole story. What made slow indistinguishable from dead is `_aiAuthP`: it was set
BEFORE the attempt and cleared only in a `finally`, so an attempt that had not settled owned the
view, and the Retry button — which calls back into the same function — adopted the very promise it
was meant to escape. There was no way out of the spinner but a reload.

The fix keeps the dedupe (two callers arriving together must still cost ONE signer prompt) and adds
`force`, which is a person pressing "Start over": it declines to adopt and starts its own attempt.
The abandoned attempt's `finally` had to learn to retire only its own slot, or a late arrival would
clear the live one and the next caller would start a third — a third prompt on an external signer.

Drives the SHIPPED ensureAiSession, extracted from app.js, so it cannot drift from what deploys.
"""
import json
import subprocess
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent


class SlowSessionIsEscapable(unittest.TestCase):
    def test_a_stuck_session_can_be_restarted_without_a_second_signer_prompt(self):
        p = subprocess.run(["node", str(HERE / "ai_session_latch_runtime.mjs")],
                           text=True, capture_output=True, timeout=120)
        self.assertEqual(p.returncode, 0, p.stdout[-2000:] + p.stderr[-4000:])
        got = json.loads(p.stdout)

        # Dedupe still holds: two callers, one signature. This is the property `force` must not cost.
        self.assertEqual(got["signsAfterConcurrent"], 1,
                         "concurrent callers no longer share one attempt — an external signer "
                         "would prompt once per caller")
        # …and that shared attempt really is stuck, so the rest of this test means something.
        self.assertTrue(got["stuckWithoutForce"],
                        "the fixture's first sign answered after all; the latch is not under test")
        # Start over ESCAPES it. Before the fix this adopted the stuck promise and did nothing.
        self.assertEqual(got["forcedUsername"], "user1",
                         "force adopted the pending attempt instead of starting its own — this is "
                         "the Retry button that did nothing")
        self.assertEqual(got["signsAfterForce"], 2, "force did not actually re-ask the signer")
        # The abandoned attempt settling late must not cost a third login (a third prompt).
        self.assertEqual(got["posts"], 2,
                         "a late-settling abandoned attempt cleared the live slot, so the next "
                         "caller started another login")


if __name__ == "__main__":
    unittest.main()
