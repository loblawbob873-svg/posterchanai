"""The relay's per-connection outbound queue (`_OutQ`).

The queue exists so one slow client can't stall the firehose: when it's full, something has to go.
The rule these tests pin down is WHICH thing goes. An EVENT is re-pullable — dropping one costs the
client a stale feed until its next query. An EOSE/OK/CLOSED is not: the client's `query()` waits out
its own timeout and the zombie detector answers that by tearing the socket down and reconnecting,
which is the "slow, keeps disconnecting, content missing" report from a link too slow to drain the
queue. It never reproduces on a LAN, where the queue never fills — so it has to be a test.
"""
import asyncio
import json

import pytest

from app.services.nostr_relay.server import _OutQ, RelayServer


def ev(i):
    return json.dumps(["EVENT", "s1", {"id": f"{i:064x}"}])


EOSE = json.dumps(["EOSE", "s1"])
OK = json.dumps(["OK", "abc", True, ""])


def test_events_are_dropped_when_full():
    q = _OutQ(16)
    for i in range(16):
        assert q.push(ev(i), True) is True
    assert q.push(ev(99), True) is False        # full → the new event is the one that goes
    assert q.dropped == 1
    assert len(q.dq) == 16


def test_control_frame_evicts_the_oldest_event_instead_of_being_dropped():
    q = _OutQ(16)
    for i in range(16):
        q.push(ev(i), True)
    assert q.push(EOSE, False) is True          # EOSE must survive a full queue
    assert q.dq[-1] == EOSE
    assert ev(0) not in q.dq                    # ...at the cost of the OLDEST event
    assert ev(1) in q.dq
    assert len(q.dq) == 16
    assert q.dropped == 1


def test_control_frame_dropped_only_when_nothing_is_evictable():
    """A queue of nothing but control frames has no event to sacrifice — drop rather than grow
    without bound (an unbounded queue on a dead-slow client is how a relay OOMs)."""
    q = _OutQ(16)
    for _ in range(16):
        q.push(OK, False)
    assert q.push(EOSE, False) is False
    assert len(q.dq) == 16
    assert q.dropped == 1


def test_bad_sizes_cannot_produce_a_drop_everything_queue():
    """outq_size is operator config. Unset/0 falls back to the default; a silly-small value is
    floored — either way the queue can still hold a page of results plus its EOSE."""
    assert _OutQ(0).maxlen == 8192
    assert _OutQ(None).maxlen == 8192
    assert _OutQ(2).maxlen == 16
    assert _OutQ(100_000).maxlen == 100_000


def test_pop_is_fifo_and_waits_for_a_frame():
    async def go():
        q = _OutQ(8)
        q.push(ev(1), True)
        q.push(EOSE, False)
        assert await q.pop() == ev(1)
        assert await q.pop() == EOSE
        # empty now: pop() must block rather than spin or raise
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(q.pop(), 0.05)
        # ...and wake up when a frame arrives
        task = asyncio.ensure_future(q.pop())
        await asyncio.sleep(0)
        q.push(OK, False)
        assert await asyncio.wait_for(task, 1) == OK

    asyncio.run(go())


def test_send_classifies_frames():
    """_send is the only caller: it must mark EVENT frames as droppable and everything else not."""
    srv = RelayServer.__new__(RelayServer)      # no store/gate needed for the queue path
    q = _OutQ(16)
    srv._outq = {"conn": q}
    for i in range(16):
        srv._send("conn", ["EVENT", "s1", {"id": f"{i:064x}"}])
    srv._send("conn", ["EVENT", "s1", {"id": "late"}])   # droppable: queue stays as it was
    assert len(q.dq) == 16 and all(json.loads(m)[0] == "EVENT" for m in q.dq)
    srv._send("conn", ["EOSE", "s1"])                    # not droppable: evicts the oldest event
    assert json.loads(q.dq[-1])[0] == "EOSE"
    assert len(q.dq) == 16


def test_send_on_an_unknown_conn_is_a_noop():
    srv = RelayServer.__new__(RelayServer)
    srv._outq = {}
    srv._send("gone", ["EOSE", "s1"])           # teardown races fanout — must not raise
