"""A Concord room whose subscription cannot be CONFIRMED must still work.

Run: venv-unified/bin/python -m pytest tests/client/test_concord_unconfirmed_subscription.py

`startChatLive` races two "did it open?" gates and used to call `stopChatLive()` when both
rejected. Both reject on a perfectly healthy room:

  * `waitForSubscription(pooled, urls)` resolves true only if the pooled REQ reached one of the
    ROOM's relay urls. A room's relays come from its invite bundle and are usually not among the
    signed-in user's account relays, so the managed pool correctly never sends there — and the gate
    reports false after its 8-second timeout, about a subscription that is alive.
  * `external.ready` reports only what THIS browser managed to dial. Measured from the node, all
    four of a real room's relays answer in under a second; Firefox logged "can't establish a
    connection" for three of them at the same time.

So the live stream was destroyed a few seconds after every room open, and because `stopChatLive()`
clears `chatSubKey` the four-second tick re-armed it — reopening a pooled REQ plus sockets to up to
eight relays, waiting up to 8s, and tearing them down again, for as long as the room was on screen.
Reported as Concord being "major slow" and "not showing room messages".

The existing `concord_live_messages_runtime.mjs` cannot see this: its external gate RESOLVES, so
`Promise.any` settles and the teardown branch is never reached. This drives the case that actually
happens — both gates reject — and asserts the subscription survives AND still delivers.
"""
import os
import shutil
import subprocess
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
RUNTIME = os.path.join(HERE, "concord_unconfirmed_subscription_runtime.mjs")
CONCORD = os.path.join(ROOT, "static", "js", "client", "concord.js")


@unittest.skipIf(shutil.which("node") is None, "node not installed")
class AnUnconfirmedSubscriptionStaysOpen(unittest.TestCase):

    def test_neither_gate_failing_tears_the_room_down(self):
        r = subprocess.run(["node", RUNTIME], capture_output=True, text=True, timeout=120)
        self.assertEqual(r.returncode, 0, r.stdout[-3000:] + "\n" + r.stderr[-4000:])

    def test_the_gate_handler_does_not_close_the_stream(self):
        """Named at the source too, because the runtime can only observe the handler it manages to
        provoke — and this is a one-line branch that is easy to reintroduce while reading as a
        tidy-up."""
        with open(CONCORD, encoding="utf-8") as f:
            src = f.read()
        i = src.index("function startChatLive(")
        body = src[i:src.index("async function flushChatLive(")]
        self.assertIn("Promise.any(gates)", body, "the gate race is gone from startChatLive")
        gate = body[body.index("Promise.any(gates)"):]
        gate = gate[:gate.index("\n", gate.index("catch"))] if "catch" in gate else gate
        self.assertNotIn("stopChatLive()", gate,
                         "startChatLive closes its own subscription when the gates cannot confirm "
                         "it. Neither gate can prove a subscription is dead — the managed pool does "
                         "not carry a room's invite relays, and external.ready only reports what "
                         "this browser dialled — so this kills live rooms and makes the 4s tick "
                         "reopen and destroy sockets for as long as the room is on screen.")


if __name__ == "__main__":
    unittest.main()
