"""A room read must return, or the Concord listener stops for ever and says nothing.

Run: venv-unified/bin/python -m unittest tests.test_a_concord_room_read_always_comes_back

MEASURED ON THIS DEPLOYMENT, and this is what it looked like from outside: a bot joined its room at
01:58:18, printed "Starting Concord listener...", and then did nothing for SIX AND A HALF HOURS. The
process was up. The thread was alive. `py-spy` put it at `concordListener.py:259` — the room read —
and `ps` reported **zero CPU seconds over 25 seconds**. Its seen-ids file had not been touched since
the previous evening. Nothing was logged, because nothing failed.

`relay.query` does carry a 12s per-relay deadline, but that deadline is armed INSIDE the socket,
between the connect and the close. A stall in the opening or closing handshake is outside it, and
`asyncio.run` then waits for ever. The `except Exception` around the call cannot help: A HANG IS NOT
AN EXCEPTION. And a listener that reads nothing is indistinguishable from a quiet room, which is why
this reads to an operator as "the bots never joined".

So the whole call gets a wall-clock ceiling. A read that times out returns nothing and the next poll
tries again — exactly what already happens for a relay that answered nothing.
"""
import asyncio
import os
import sys
import threading
import time
import unittest
from pathlib import Path

# The bots run with cwd=botframework, so `concordListener` imports `concord` root-relatively.
BOTS = Path(__file__).resolve().parents[1] / "botframework"
if str(BOTS) not in sys.path:
    sys.path.insert(0, str(BOTS))


class ARoomReadIsBounded(unittest.TestCase):
    def setUp(self):
        import concordListener as cl  # noqa: F401  (import cost stays in the test)
        self.cl = cl

    def test_a_query_that_never_answers_gives_up_and_returns_nothing(self):
        from app.services.nostr import nostr_service as svc

        async def never(*a, **kw):
            await asyncio.Event().wait()          # the exact shape: awaiting, no timer, no CPU

        original = svc.relay.query
        svc.relay.query = never
        os.environ["CONCORD_QUERY_TIMEOUT"] = "1"
        try:
            wire = self.cl.live_wire()
            # THE TEST NEEDS ITS OWN CEILING, or a regression does not FAIL — it HANGS, taking the
            # whole gate with it. That is the very failure being tested, and a suite that wedges is
            # worse than one that goes red: verified, the unbounded version pins this test for ever.
            box = {}

            def call():
                began = time.monotonic()
                box["out"] = wire.query(["wss://example.invalid"], [{"kinds": [1059]}])
                box["took"] = time.monotonic() - began

            worker = threading.Thread(target=call, daemon=True)
            worker.start()
            worker.join(timeout=20)
            self.assertFalse(worker.is_alive(),
                             "the room read never came back — it is unbounded, which is what wedged "
                             "a bot for six and a half hours at zero CPU")
            out, took = box.get("out"), box.get("took", 999)
        finally:
            svc.relay.query = original
            os.environ.pop("CONCORD_QUERY_TIMEOUT", None)

        self.assertEqual(out, [], "a timed-out read must answer 'nothing', not raise")
        self.assertLess(took, 8, f"the read took {took:.1f}s against a 1s ceiling — it is unbounded, "
                                 "which is what wedged a bot for six and a half hours")

    def test_a_normal_answer_is_passed_straight_through(self):
        """The ceiling must not change what a working read returns."""
        from app.services.nostr import nostr_service as svc

        async def fine(*a, **kw):
            return [{"id": "abc", "kind": 1059}]

        original = svc.relay.query
        svc.relay.query = fine
        try:
            out = self.cl.live_wire().query(["wss://example.invalid"], [{"kinds": [1059]}])
        finally:
            svc.relay.query = original
        self.assertEqual(out, [{"id": "abc", "kind": 1059}])

    def test_a_raising_query_is_still_swallowed_into_nothing(self):
        from app.services.nostr import nostr_service as svc

        async def boom(*a, **kw):
            raise RuntimeError("relay said no")

        original = svc.relay.query
        svc.relay.query = boom
        try:
            self.assertEqual(self.cl.live_wire().query(["wss://x"], [{}]), [])
        finally:
            svc.relay.query = original


if __name__ == "__main__":
    unittest.main()
