"""Relay anti-spam: per-author rate and duplicate limits, set well above how people really post.

"we need to come up with some relay anti-spam features/protections ... this is about prevention".
Measured over a week on this relay: the busiest minute any author published directly here was 31
events, the most repeats of one note in an hour 7. Defaults are 120/min (1200/h) and 20 identical/h.
"""
import asyncio
from unittest import mock

from app.services.nostr.event import build_event
from app.services.nostr_relay import server as srvmod
from app.services.nostr_relay.server import RelayServer

ALICE = bytes.fromhex("a1" * 32)


class Gate:
    def is_member(self, _pk): return True
    def is_operator(self, _pk): return False
    def is_puppet_event(self, _ev): return False
    def is_blocked(self, _pk): return False


def _server(**cfg):
    srv = RelayServer(object(), Gate(), {"wot_enabled": False, **cfg})
    srv.sent = []
    srv._send = lambda conn, obj: srv.sent.append((conn, obj))
    return srv


def _ev(kind=7, content="+", sk=ALICE):
    return {"kind": kind, "pubkey": "a1" * 32, "content": content, "id": "x", "tags": []}


def _limits(srv, conn, evs, start=1000.0, step=0.0):
    out = []
    with mock.patch.object(srvmod.time, "time") as t:
        for i, e in enumerate(evs):
            t.return_value = start + i * step
            out.append(srv._rate_limited(conn, e, e["kind"]))
    return out


def test_a_burst_past_the_minute_limit_is_refused_and_it_refills():
    srv = _server()
    srv._conn_ips["c"] = "8.8.4.4"
    res = _limits(srv, "c", [_ev() for _ in range(121)])
    assert res[:120] == [""] * 120, "a normal burst was refused"
    assert res[120].startswith("rate-limited:"), res[120]
    # Thirty seconds later half a minute's worth is back.
    later = _limits(srv, "c", [_ev() for _ in range(61)], start=1030.0)
    assert later[:59].count("") == 59 and later[-1].startswith("rate-limited:"), later[-3:]


def test_our_own_machines_and_high_volume_kinds_are_never_limited():
    srv = _server(rate_per_min=5)
    srv._conn_ips["lan"] = "192.168.0.85"
    assert set(_limits(srv, "lan", [_ev() for _ in range(50)])) == {""}
    srv._conn_ips["c"] = "8.8.4.4"
    assert set(_limits(srv, "c", [_ev(kind=1059) for _ in range(50)] + [_ev(kind=30078) for _ in range(50)])) == {""}


def test_the_same_post_flooded_is_refused_but_different_posts_are_not():
    srv = _server(rate_per_min=0)
    srv._conn_ips["c"] = "8.8.4.4"
    same = "buy my coin now at the best price ever!!"
    res = _limits(srv, "c", [_ev(kind=1, content=same) for _ in range(21)])
    assert res[:20] == [""] * 20 and res[20].startswith("rate-limited:"), res[-2:]
    diff = _limits(srv, "c", [_ev(kind=1, content=f"note number {i} with enough text") for i in range(40)])
    assert set(diff) == {""}
    short = _limits(srv, "c", [_ev(kind=1, content="gm") for _ in range(40)])
    assert set(short) == {""}, "a short greeting repeated is not spam"


def test_zero_turns_each_limit_off():
    srv = _server(rate_per_min=0, dup_per_hour=0)
    srv._conn_ips["c"] = "8.8.4.4"
    assert set(_limits(srv, "c", [_ev(kind=1, content="x" * 40) for _ in range(500)])) == {""}


def test_the_client_is_told_rate_limited_through_the_real_event_path():
    srv = _server(rate_per_min=2)
    srv._conn_ips["c"] = "8.8.4.4"
    events = [build_event(ALICE, 7, "+", [["e", "b" * 64]]) for _ in range(3)]
    async def run():
        srv._store_and_fanout = None
        for e in events[:2]:
            srv._rate_limited("c", e, 7)                      # spend the two tokens
        await srv._on_event("c", events[2])
    with mock.patch.object(srvmod.time, "time", return_value=float(events[2]["created_at"])):
        asyncio.new_event_loop().run_until_complete(run())
    oks = [m for _c, m in srv.sent if isinstance(m, list) and m and m[0] == "OK"]
    assert oks and oks[-1][2] is False and oks[-1][3].startswith("rate-limited:"), srv.sent
