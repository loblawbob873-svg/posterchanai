"""Queries to upstream relays reuse their connection instead of a TLS handshake per query.

server1, 2026-10-18: every `relay.query` opened a websocket to every upstream relay and closed it after EOSE. The sync
sweep asks about the web of trust in author batches, so the relay made ~1,200 outbound connections a minute (1,250
sockets in TIME-WAIT) and TLS was ~17% of its CPU (py-spy) -- the largest single term. Runs against a real websocket
server that counts its connections.
"""
import asyncio
import json

import pytest
import websockets

from app.services.nostr import relay

_REAL_CONN_KW = relay._conn_kw            # the autouse fixture below stubs it; the compression test needs the real one


class Upstream:
    def __init__(self, mode="answer"):
        self.connections = 0
        self.reqs = 0
        self.mode = mode

    async def handler(self, ws):
        self.connections += 1
        async for raw in ws:
            msg = json.loads(raw)
            if msg[0] != "REQ":
                continue
            self.reqs += 1
            sub = msg[1]
            if self.mode == "close-after-one" and self.reqs > 1 and self.reqs % 2 == 0:
                await ws.close()                       # a relay that drops idle connections
                return
            if self.mode == "refuse":
                await ws.send(json.dumps(["CLOSED", sub, "rate-limited: slow down"]))
                continue
            if self.mode == "silent":
                continue
            ev = {"id": "%064x" % self.reqs, "pubkey": "a" * 64, "created_at": self.reqs, "kind": 1, "tags": [],
                  "content": "x", "sig": "0" * 128}
            await ws.send(json.dumps(["EVENT", sub, ev]))
            await ws.send(json.dumps(["EOSE", sub]))


async def _serve(up):
    srv = await websockets.serve(up.handler, "127.0.0.1", 0)
    return srv, "ws://127.0.0.1:%d/" % srv.sockets[0].getsockname()[1]


@pytest.fixture(autouse=True)
def _remote(monkeypatch):
    # treat the test server as a remote relay (loopback relays skip the breaker, nothing else differs)
    monkeypatch.setattr(relay, "_is_local", lambda r: False)
    monkeypatch.setattr(relay, "_conn_kw", lambda r, d: {"proxy": None})
    monkeypatch.setattr(relay, "_relay_paused", lambda r: False)


def test_twenty_queries_to_one_relay_use_one_connection():
    async def go():
        up = Upstream()
        srv, url = await _serve(up)
        got = [await relay.query([url], [{"kinds": [1], "limit": 1}], timeout=5) for _ in range(20)]
        srv.close()
        return up, got
    up, got = asyncio.run(go())
    assert all(len(g) == 1 for g in got), "a pooled query lost its answer"
    assert up.reqs == 20
    assert up.connections == 1, "%d connections for 20 queries" % up.connections


def test_a_connection_the_relay_dropped_is_replaced_and_the_query_still_answers():
    async def go():
        up = Upstream("close-after-one")
        srv, url = await _serve(up)
        got = [await relay.query([url], [{"kinds": [1], "limit": 1}], timeout=5) for _ in range(6)]
        srv.close()
        return got
    got = asyncio.run(go())
    assert all(len(g) == 1 for g in got), [len(g) for g in got]


def test_a_refused_or_unanswered_query_does_not_keep_its_connection():
    async def go(mode, timeout):
        up = Upstream(mode)
        srv, url = await _serve(up)
        for _ in range(3):
            assert await relay.query([url], [{"kinds": [1]}], timeout=timeout) == []
        srv.close()
        return up
    assert asyncio.run(go("refuse", 5)).connections == 3, "a rate-limited connection was reused"
    assert asyncio.run(go("silent", 0.3)).connections == 3, "a connection still streaming was reused"


def test_a_connection_to_this_machines_relay_is_not_compressed(monkeypatch):
    """Deflating answers that never leave the host cost the relay ~8% of its CPU (py-spy 2026-10-18): the push
    service, the notification poller and the bots connect through this module, and negotiated permessage-deflate."""
    monkeypatch.setattr(relay, "_is_local", lambda r: True)
    monkeypatch.setattr(relay, "_conn_kw", _REAL_CONN_KW)

    async def go():
        async def h(ws):
            await ws.wait_closed()
        srv = await websockets.serve(h, "127.0.0.1", 0)       # offers permessage-deflate, as the relay does
        url = "ws://127.0.0.1:%d/" % srv.sockets[0].getsockname()[1]
        ws = await relay._open(url, False)
        ext = [type(e).__name__ for e in ws.protocol.extensions]
        await ws.close()
        srv.close()
        return ext
    assert asyncio.run(go()) == [], "a loopback connection negotiated compression"
