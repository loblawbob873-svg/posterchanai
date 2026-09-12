"""A targeted firehose stream asks about EVERY registered user, and that question outgrew a REQ.

The DM-inbox and DVM streams filter on `#p` = the operator set, which is three keys per registered
user. At 2,090 users that is 4,180 keys and a 284 KB REQ; measured on 2026-09-11 against all 30
configured upstreams, 29 refused it ("total filter items too large" / "message too large
(284303 > 262144)") and thirteen of those killed the socket without a word. The relay went on
logging "firehose connected" and the WoT stream (no `#p`) went on delivering ~36k events a day, so
the only visible symptom was that NIP-17 gift wraps stopped arriving entirely — no inbound DMs and
no Concord room traffic from other relays — from 2026-09-09 10:54 onwards.

These run the SHIPPED `_run_one` against a real websocket server that enforces a real filter cap,
because the bug is in what goes ON THE WIRE. A fixture that accepted any REQ would agree with it.
"""
import asyncio
import json
import logging
import unittest

import websockets

from app.services.nostr_relay import firehose


# What a real relay does, measured: refuse the REQ past a cap and say so (strfry), rather than
# answering a smaller version of the question.
CAP = firehose._MAX_FILTER_ITEMS


class _FakeRelay:
    """Serves EVENTs for a REQ it accepts and `NOTICE`s one it does not."""

    def __init__(self, cap=CAP, events=None):
        self.cap, self.events = cap, events or []
        self.reqs = []          # every filter this server was actually asked
        self.refused = 0

    async def _handle(self, ws):
        async for raw in ws:
            msg = json.loads(raw)
            if msg[0] != "REQ":
                continue
            sub, flt = msg[1], msg[2]
            self.reqs.append(flt)
            biggest = max((len(v) for k, v in flt.items()
                           if isinstance(v, list) and k != "kinds"), default=0)
            if biggest > self.cap:
                self.refused += 1
                await ws.send(json.dumps(["NOTICE", "ERROR: bad req: total filter items too large"]))
                continue
            wanted = set(flt.get("#p") or [])
            for ev in self.events:
                if not wanted or any(t[0] == "p" and t[1] in wanted for t in ev["tags"]):
                    await ws.send(json.dumps(["EVENT", sub, ev]))

    async def __aenter__(self):
        self._srv = await websockets.serve(self._handle, "127.0.0.1", 0)
        self.url = "ws://127.0.0.1:%d" % self._srv.sockets[0].getsockname()[1]
        return self

    async def __aexit__(self, *a):
        self._srv.close()
        await self._srv.wait_closed()


def _keys(n, start=0):
    return ["%064x" % (start + i) for i in range(n)]


def _wrap(pk):
    return {"id": "%064x" % 0xABC, "kind": 1059, "pubkey": "%064x" % 1,
            "tags": [["p", pk]], "content": "", "sig": ""}


def _go(coro):
    """A FRESH loop per case, never `get_event_loop()`.

    These first used `asyncio.get_event_loop().run_until_complete(...)`, which passes when the file
    is run alone and raises "There is no current event loop in thread 'MainThread'" once anything
    earlier in the session has closed the implicit loop — so the suite would have gone red for a
    reason that has nothing to do with the rule under test. A test that only passes in isolation
    guards nothing."""
    loop = asyncio.new_event_loop()
    try:
        asyncio.set_event_loop(loop)
        return loop.run_until_complete(coro)
    finally:
        asyncio.set_event_loop(None)
        loop.close()


async def _drain(relay, kinds, extra, expect, timeout=8.0):
    """Run the shipped stream until `expect` events land (or time out), then stop it."""
    got, stop = [], asyncio.Event()

    async def on_event(ev):
        got.append(ev)
        if len(got) >= expect:
            stop.set()

    task = asyncio.create_task(firehose._run_one(
        relay.url, kinds, on_event, stop, True, extra=extra, label=" (test)"))
    try:
        await asyncio.wait_for(stop.wait(), timeout=timeout)
    except asyncio.TimeoutError:
        stop.set()
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    return got


class FirehoseFilterSize(unittest.TestCase):

    def test_a_filter_bigger_than_a_relay_accepts_still_delivers_its_events(self):
        """THE BUG. 4,180 `#p` keys, a server that refuses more than `CAP` per REQ — the events for
        the LAST key in the list must still arrive. Before the split this delivered nothing at all."""
        ops = _keys(4180)
        async def go():
            async with _FakeRelay(events=[_wrap(ops[0]), _wrap(ops[-1])]) as r:
                got = await _drain(r, [4, 1059], {"#p": ops}, expect=2)
                return r, got
            
        r, got = _go(go())
        self.assertEqual(r.refused, 0, "a REQ was still too large for the relay")
        self.assertGreaterEqual(len(r.reqs), 2, "an oversized filter was not split")
        self.assertEqual(len(got), 2,
                         "the stream lost events — a key in the filter reached no subscription")

    def test_no_req_carries_more_items_than_a_relay_will_take(self):
        """The rule, stated as the wire fact: every `#p` we send is within the cap, and TOGETHER
        they still name every key — a split that drops keys is the silent version of this bug."""
        ops = _keys(4180)
        async def go():
            async with _FakeRelay() as r:
                await _drain(r, [1059], {"#p": ops}, expect=1, timeout=4.0)
                return r
        r = _go(go())
        self.assertTrue(r.reqs, "nothing was ever asked")
        for flt in r.reqs:
            self.assertLessEqual(len(flt["#p"]), CAP)
        asked = [k for flt in r.reqs for k in flt["#p"]]
        self.assertEqual(sorted(asked), sorted(ops), "the split lost or duplicated keys")

    def test_every_chunk_keeps_the_rest_of_the_filter(self):
        """`kinds`/`since` are the QUESTION, not a list to divide — every chunk must carry them, or
        a sub-REQ asks something else."""
        ops = _keys(1200)
        parts = firehose._split_filter({"kinds": [4, 1059], "since": 99, "#p": ops})
        self.assertEqual(len(parts), 3)
        for p in parts:
            self.assertEqual(p["kinds"], [4, 1059])
            self.assertEqual(p["since"], 99)

    def test_an_ordinary_filter_is_still_exactly_one_subscription(self):
        """The WoT stream carries no `#p` and must be untouched — one REQ, byte-identical shape."""
        flt = {"kinds": [1, 6, 7], "since": 1}
        self.assertEqual(firehose._split_filter(flt), [flt])
        self.assertEqual(firehose._split_filter({"kinds": [1], "#p": _keys(CAP)}),
                         [{"kinds": [1], "#p": _keys(CAP)}])

    def test_two_long_lists_are_both_bounded(self):
        """`authors` can grow the same way `#p` did. Both must end up within the cap — chunking only
        the longest field and calling it done would leave the other oversized and refused."""
        flt = {"kinds": [1059], "authors": _keys(900), "#p": _keys(700, start=10**6)}
        parts = firehose._split_filter(flt)
        for p in parts:
            self.assertLessEqual(len(p["authors"]), CAP)
            self.assertLessEqual(len(p["#p"]), CAP)
        # Every (author, recipient) pair is still asked about exactly once.
        pairs = {(a, q) for p in parts for a in p["authors"] for q in p["#p"]}
        self.assertEqual(len(pairs), 900 * 700)

    def test_a_filter_too_big_to_split_says_so(self):
        """Past `_MAX_SUBS` the stream cannot ask the whole question. It subscribes to what fits and
        WARNS — silence is what hid the original bug for two days."""
        ops = _keys(CAP * (firehose._MAX_SUBS + 5))
        with self.assertLogs("app.services.nostr_relay.firehose", level="WARNING") as log:
            parts = firehose._split_filter({"kinds": [1059], "#p": ops})
        self.assertEqual(len(parts), firehose._MAX_SUBS)
        self.assertIn("will NOT arrive", "\n".join(log.output))

    def test_a_relay_that_refuses_us_is_named_in_the_log(self):
        """The diagnosis that was available the whole time and never read. A stream whose REQ is
        refused must not look like a quiet one."""
        async def go():
            async with _FakeRelay(cap=10) as r:   # refuses even a split chunk
                with self.assertLogs("app.services.nostr_relay.firehose", level="WARNING") as log:
                    await _drain(r, [1059], {"#p": _keys(600)}, expect=1, timeout=4.0)
                return log
        log = _go(go())
        joined = "\n".join(log.output)
        self.assertIn("refused a subscription", joined)
        self.assertIn("total filter items too large", joined)


if __name__ == "__main__":
    unittest.main()
