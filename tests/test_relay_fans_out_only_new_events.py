"""The relay fans out an event to live subscriptions ONCE -- when it is new to the store -- never again for a copy
it already holds.

Measured on poster.place 2026-10-18 ("entire timeline seems sluggish, only 1 fedi event in 30 seconds"): of 506 live
events a following-feed subscription received in 45 s, 500 had been stored BEFORE the subscription opened. The
store's add_event answers True for a duplicate (ON CONFLICT DO NOTHING semantics), and every ingest path fanned out
on that answer -- so the firehose re-delivering the last hour after a restart, and our own outbox's posts echoing
back from the relays it sent them to, were pushed to every open timeline as if new, burying the real ones.
"""
import ast
import asyncio
import json
import tempfile
from pathlib import Path

from tests.os_relay_harness import Instance, OWNER_SK, FRIEND_SK, ev

ROOT = Path(__file__).resolve().parents[1]


def test_the_store_says_whether_an_event_is_new():
    with tempfile.TemporaryDirectory() as tmp:
        inst = Instance(tmp).start()
        try:
            e = ev(OWNER_SK, content="hello once")
            run = lambda c: asyncio.run_coroutine_threadsafe(c, inst.loop).result(20)
            assert run(inst.store.add_event_new(e, origin="wot")) == (True, True)
            assert run(inst.store.add_event_new(e, origin="wot")) == (True, False), "a held copy reported as new"
            assert run(inst.store.add_event(e, origin="wot")) is True, "add_event's own answer must not change"
        finally:
            inst.close()


def test_a_republished_copy_is_not_sent_to_live_subscribers_again():
    with tempfile.TemporaryDirectory() as tmp:
        inst = Instance(tmp).start()
        try:
            from websockets.asyncio.client import connect
            e = ev(FRIEND_SK, content="posted once, echoed twice")

            async def go():
                async with connect(inst.relay_url) as sub, connect(inst.relay_url) as pub:
                    await sub.recv()
                    await pub.recv()
                    await sub.send(json.dumps(["REQ", "live", {"kinds": [1], "limit": 0}]))
                    got = []

                    async def reader():
                        while True:
                            m = json.loads(await sub.recv())
                            if m[0] == "EVENT":
                                got.append(m[2]["id"])
                    task = asyncio.create_task(reader())
                    await asyncio.sleep(0.3)
                    for _ in range(3):                     # the original, then two echoes of it
                        await pub.send(json.dumps(["EVENT", e]))
                        while True:
                            m = json.loads(await pub.recv())
                            if m[0] == "OK":
                                assert m[2] is True, m      # a duplicate is still an OK to its publisher
                                break
                    await asyncio.sleep(0.5)
                    task.cancel()
                    return got
            got = asyncio.run(go())
            assert got == [e["id"]], "live subscribers got %d copies of one event" % len(got)
        finally:
            inst.close()


def test_no_ingest_path_fans_out_on_add_events_answer():
    """add_event says True for a duplicate; a fan-out gated on it re-broadcasts. Every fan-out must be gated on
    add_event_new's `new` (or follow its own has_event check). Walks the relay package's AST."""
    bad = []
    for f in sorted((ROOT / "app/services/nostr_relay").glob("*.py")):
        tree = ast.parse(f.read_text())
        for node in ast.walk(tree):
            if not isinstance(node, ast.If):
                continue
            t = node.test
            if isinstance(t, ast.Await) and isinstance(t.value, ast.Call) and \
                    getattr(t.value.func, "attr", "") == "add_event":
                if any(isinstance(n, ast.Call) and getattr(n.func, "attr", "") == "fanout" for n in ast.walk(node)):
                    bad.append("%s:%d" % (f.name, node.lineno))
    assert not bad, "fan-out gated on add_event (True for duplicates): %s" % bad


def test_the_concord_mirror_does_not_forward_a_delivered_wrap_twice():
    """A member's app re-publishing a wrap the mirror already delivered must not send it to the community's relays
    again; a forward that FAILED is retried."""
    from app.services.nostr_relay.concord_mirror import ConcordMirror

    class Link:
        def __init__(self, ok):
            self.ok, self.sent = ok, []

        async def publish(self, ev):
            self.sent.append(ev["id"])
            return self.ok

    m = ConcordMirror.__new__(ConcordMirror)
    good, bad = Link(True), Link(False)
    m.links = {"wss://a": good}
    m._relays_for = lambda author: ["wss://a"]
    wrap = {"id": "1" * 64, "pubkey": "2" * 64, "kind": 1059}
    for _ in range(3):
        assert asyncio.run(m.forward(wrap)) is True
    assert good.sent == [wrap["id"]], "an already-delivered wrap was sent again: %d sends" % len(good.sent)
    m.links = {"wss://a": bad}
    other = dict(wrap, id="3" * 64)
    asyncio.run(m.forward(other)); asyncio.run(m.forward(other))
    assert bad.sent == [other["id"], other["id"]], "a failed forward must be retried"
