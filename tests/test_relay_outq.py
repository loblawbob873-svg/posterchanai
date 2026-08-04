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

from app.services.nostr_relay.server import _OutQ, RelayServer, _client_ip


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


def test_eviction_takes_the_oldest_event_and_spares_queued_control_frames():
    """A control frame already waiting in the queue is just as un-droppable as the incoming one —
    the victim has to be the oldest EVENT, not simply the head of the queue."""
    q = _OutQ(16)
    q.push(OK, False)                           # sits at the head, must survive
    for i in range(15):
        q.push(ev(i), True)
    q.push(EOSE, False)
    assert OK in q.dq
    assert ev(0) not in q.dq                    # oldest event went
    assert ev(1) in q.dq
    assert q.dq[-1] == EOSE


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
    srv._conn_ips = {}      # __new__ skips __init__, and the first drop logs the connection's IP
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


def test_falling_behind_warns_exactly_once(caplog):
    """The close log arrives when the session ends, which can be hours after it started struggling.
    This warning is the live signal — but it must fire ONCE, not once per dropped frame, or a slow
    client turns the journal into its own denial of service."""
    srv = RelayServer.__new__(RelayServer)
    q = _OutQ(16)
    srv._outq = {"conn": q}
    srv._conn_ips = {"conn": "41.0.0.1"}
    with caplog.at_level("WARNING"):
        for i in range(200):                    # 16 fit, 184 are dropped
            srv._send("conn", ["EVENT", "s1", {"id": f"{i:064x}"}])
    warnings = [r for r in caplog.records if "not keeping up" in r.getMessage()]
    assert len(warnings) == 1
    assert "41.0.0.1" in warnings[0].getMessage()
    assert q.dropped == 184


# --- client IP resolution ---------------------------------------------------------------------
# The logged IP is the whole point of the session log (it's how a reported "it keeps disconnecting"
# gets matched to a connection), and the same value dedups the "online people" count — so it has to
# be the client's REAL address and not one the client chose for us.


class _Hdrs(dict):
    def get(self, key, default=""):
        return dict.get(self, key, default)


class _Conn:
    def __init__(self, peer):
        self.remote_address = (peer, 1234)


def test_x_real_ip_beats_a_client_supplied_forwarded_for():
    """nginx resolves the true client via set_real_ip_from + real_ip_header CF-Connecting-IP and
    passes it as X-Real-IP, overwriting anything the client sent. XFF's first element is the
    client's own claim (nginx APPENDS with $proxy_add_x_forwarded_for), so it must not win."""
    hdrs = _Hdrs({"X-Real-IP": "41.90.1.2",
                  "X-Forwarded-For": "1.2.3.4, 41.90.1.2, 192.168.0.1"})
    assert _client_ip(hdrs, _Conn("192.168.0.1")) == "41.90.1.2"


def test_falls_back_through_cf_then_xff_then_the_socket():
    assert _client_ip(_Hdrs({"CF-Connecting-IP": "41.90.1.2", "X-Forwarded-For": "9.9.9.9"}),
                      _Conn("192.168.0.1")) == "41.90.1.2"
    assert _client_ip(_Hdrs({"X-Forwarded-For": "41.90.1.2, 192.168.0.1"}),
                      _Conn("192.168.0.1")) == "41.90.1.2"
    assert _client_ip(_Hdrs({}), _Conn("192.168.0.55")) == "192.168.0.55"   # direct / turnkey
    assert _client_ip(_Hdrs({"X-Real-IP": "2a00:11b1:10a2:672e:a2b2:807:41ef:9200"}),
                      _Conn("::1")).startswith("2a00:")


def test_non_ip_header_values_are_discarded():
    """A header can't carry CRLF, but it can carry enough printable text to make a log line lie."""
    assert _client_ip(_Hdrs({"X-Real-IP": "1.1.1.1 dur=999s dropped=0",
                             "X-Forwarded-For": "41.90.1.2"}), _Conn("192.168.0.1")) == "41.90.1.2"
    assert _client_ip(_Hdrs({"X-Real-IP": "x" * 200}), _Conn("192.168.0.9")) == "192.168.0.9"


def test_a_duplicated_header_cannot_blank_the_logged_ip():
    """websockets' Headers.get RAISES MultipleValuesError on a repeated header — and that is not a
    KeyError, so the usual .get(name, "") guard doesn't catch it. Sending X-Real-IP twice therefore
    used to record the connection as ip=?, letting a client opt out of the log added to find it.
    Uses the real Headers type on purpose: this is a contract of that class, not of our own code."""
    from websockets.datastructures import Headers

    hdrs = Headers()
    hdrs["X-Real-IP"] = "1.1.1.1"
    hdrs["X-Real-IP"] = "2.2.2.2"                     # tampered → skip this source
    hdrs["X-Forwarded-For"] = "41.90.1.2, 192.168.0.1"
    assert _client_ip(hdrs, _Conn("192.168.0.1")) == "41.90.1.2"

    only_dupes = Headers()
    only_dupes["X-Real-IP"] = "1.1.1.1"
    only_dupes["X-Real-IP"] = "2.2.2.2"
    assert _client_ip(only_dupes, _Conn("192.168.0.7")) == "192.168.0.7"   # socket peer: unforgeable

    assert _client_ip(Headers(), _Conn("192.168.0.8")) == "192.168.0.8"


def test_only_public_clients_count_as_remote():
    """Which sessions are worth an INFO line. Measured on the live relay: ~13 connections close
    every 90s and every one has the proxy as its TCP peer — the app, the router.lan bridge, the
    bots and the node agents all open short-lived LAN sockets. Excluding only loopback would bury
    real user reports under our own machinery, so the line is LAN, not localhost."""
    internal = RelayServer._is_internal
    assert internal("127.0.0.1") and internal("::1") and internal("localhost")
    assert internal("192.168.0.1") and internal("10.0.0.5") and internal("172.16.3.9")
    assert internal("169.254.1.1") and internal("fe80::1") and internal("[fe80::1%eth0]")
    assert not internal("41.90.1.2") and not internal("2a00:11b1:10a2::1")
    # Unknown counts as remote, in both spellings: our own machinery always has a LAN peer to
    # report, so an address we can't place is genuinely odd and worth the line.
    assert not internal("") and not internal("?") and not internal("not-an-ip")
