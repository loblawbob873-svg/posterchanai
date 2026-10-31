"""A TUNNEL IS NOT A NEW CONNECTION TO THE FAR RELAY -- the member's next tunnel to the same relay reuses the last one.

Measured on poster.place 2026-10-30: jskitty.com began refusing this node's Concord mirror at 02:00 ("restricted: this
relay serves people's own clients; proxies, mirrors and aggregators are not permitted"), so every member's client read
it ITSELF through `/ws/concord-relay` -- and the client opens one websocket per read and per live subscription. One
tunnel was one connection to jskitty: 12,165 an hour (2,064 in ten minutes, 2,059 of them shorter than 15 s; the app's
Tor sockets lived ~2.5 s). That is the "churning connections/websockets" jskitty blocked this node for once before.
jskitty itself was measured holding those exact subscriptions open for 90 s, so the client ends them -- and installed
APKs and desktop builds cannot be changed from the server. So the server keeps the far connection a little while
(concord_tunnel.park) and hands it to the same member's next tunnel to the same relay.

Driven through the shipped router with one TestClient event loop (as production has one), only the far relay faked.
On the code before this change the first test dials 30 times.
"""
import asyncio
import json
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.routers import concord_tunnel as router_mod
from app.services import concord_tunnel as tun

PK, OTHER = "ab" * 32, "cd" * 32
URL = "wss://jskitty.com/nostr"


class FarRelay:
    """A relay: answers a REQ with one stored event and EOSE, keeps it open, remembers CLOSEs; says AUTH at connect."""

    def __init__(self, challenge=""):
        self.dials, self.sockets, self.challenge = 0, [], challenge

    def connect(self, url, proxy):
        relay = self

        class Cm:
            async def __aenter__(self):
                relay.dials += 1
                self.ws = FarSocket(relay)
                relay.sockets.append(self.ws)
                return self.ws

            async def __aexit__(self, *a):
                self.ws.close_code = 1000
                await self.ws.q.put(None)
                return False
        return Cm()


class FarSocket:
    def __init__(self, relay):
        self.relay, self.q, self.close_code, self.loop = relay, asyncio.Queue(), None, asyncio.get_running_loop()
        self.open_subs, self.closed_subs = set(), []
        if relay.challenge:
            self.q.put_nowait(json.dumps(["AUTH", relay.challenge]))

    async def send(self, data):
        msg = json.loads(data)
        if msg[0] == "REQ":
            self.open_subs.add(msg[1])
            await self.q.put(json.dumps(["EVENT", msg[1], {"id": "e-" + msg[1]}]))
            await self.q.put(json.dumps(["EOSE", msg[1]]))
        elif msg[0] == "CLOSE":
            self.open_subs.discard(msg[1])
            self.closed_subs.append(msg[1])

    def push(self, m):
        """From the test's thread: the relay says `m` (None: it closes)."""
        self.loop.call_soon_threadsafe(self.q.put_nowait, m)

    def __aiter__(self):
        return self

    async def __anext__(self):
        m = await self.q.get()
        if m is None:
            raise StopAsyncIteration
        return m


@pytest.fixture
def far(monkeypatch):
    import socket
    monkeypatch.setattr(socket, "gethostbyname", lambda h: "93.184.216.34")
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: [(2, 1, 6, "", ("93.184.216.34", 443))])
    monkeypatch.setattr(tun, "_key", lambda: b"k" * 32)
    for name, value in (("_open", {}), ("_total", 0), ("_good", {}), ("_unreachable", {}), ("_strikes", {}),
                        ("_reached", {}), ("_dialing", {}), ("_parked", {}), ("_reused", {})):
        monkeypatch.setattr(tun, name, value, raising=False)
    monkeypatch.setattr(tun, "tor_hops", lambda: ["socks5h://127.0.0.1:9052"])
    monkeypatch.setattr(tun, "proxy_url", lambda: "http://127.0.0.1:8119")
    relay = FarRelay()
    monkeypatch.setattr(tun, "connect", relay.connect)
    return relay


def _app():
    app = FastAPI()
    app.include_router(router_mod.ws_router)
    return app


def _until(cond, seconds=3.0):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.01)
    return cond()


def _read(client, sid, pk=PK, close_sub=False, first=None):
    """One short tunnel the way Concord's queryFrom makes it: REQ, EVENT, EOSE, close. Returns what it received."""
    got = []
    with client.websocket_connect(f"/ws/concord-relay?u={URL}&t={tun.issue(pk)}") as ws:
        if first is not None:
            got.append(json.loads(ws.receive_text()))
        ws.send_text(json.dumps(["REQ", sid, {"kinds": [1059], "authors": ["a" * 64], "limit": 1000}]))
        while True:
            m = json.loads(ws.receive_text())
            got.append(m)
            if m[0] == "EOSE":
                break
        if close_sub:
            ws.send_text(json.dumps(["CLOSE", sid]))
    _until(lambda: not tun._open)                     # released: the router has parked it (or closed it) by then
    return got


def test_thirty_short_tunnels_of_one_member_are_one_connection_to_the_far_relay(far):
    with TestClient(_app()) as client:
        for n in range(30):
            got = _read(client, "qf%d" % n)
            assert got == [["EVENT", "qf%d" % n, {"id": "e-qf%d" % n}], ["EOSE", "qf%d" % n]], (n, got)
    assert far.dials == 1, ("every tunnel was a new connection to the far relay", far.dials)
    assert tun._reused == {"jskitty.com": 29}, tun._reused


def test_a_reused_connection_carries_nothing_of_the_last_tunnel(far):
    """The last tunnel's live subscription is CLOSEd on the far relay when it is parked, and a reply to it that arrives
    late never reaches the next tunnel."""
    with TestClient(_app()) as client:
        _read(client, "live1")                        # left open, as a live subscription is when its socket goes
        assert _until(lambda: (PK, URL) in tun._parked)
        sock = far.sockets[0]
        assert _until(lambda: "live1" in sock.closed_subs), "the departed tunnel's subscription was left streaming"
        with client.websocket_connect(f"/ws/concord-relay?u={URL}&t={tun.issue(PK)}") as ws:
            assert _until(lambda: (PK, URL) not in tun._parked)
            sock.push(json.dumps(["EVENT", "live1", {"id": "late"}]))     # a late reply to the old one
            ws.send_text(json.dumps(["REQ", "qf2", {"kinds": [1059]}]))
            first = json.loads(ws.receive_text())
        assert first == ["EVENT", "qf2", {"id": "e-qf2"}], ("the next member tunnel was handed the last one's traffic", first)
    assert far.dials == 1


def test_the_relays_auth_challenge_is_said_to_the_tunnel_that_reuses_it(far):
    far.challenge = "chal-1"
    with TestClient(_app()) as client:
        got = _read(client, "a1", first=True)
        assert got[0] == ["AUTH", "chal-1"]
        got = _read(client, "a2", first=True)
        assert got[0] == ["AUTH", "chal-1"], ("a reused connection did not repeat the relay's AUTH challenge", got)
    assert far.dials == 1


def test_another_member_never_gets_the_connection(far):
    with TestClient(_app()) as client:
        _read(client, "x1", pk=PK)
        assert _until(lambda: (PK, URL) in tun._parked)
        _read(client, "x2", pk=OTHER)
    assert far.dials == 2, "one member's far connection (and any AUTH done on it) was handed to another member"


def test_a_connection_the_relay_closed_is_dialled_again(far):
    with TestClient(_app()) as client:
        _read(client, "c1")
        assert _until(lambda: (PK, URL) in tun._parked)
        sock = far.sockets[0]
        sock.close_code = 1001
        sock.push(None)                         # the far relay went away while it was parked
        assert _until(lambda: (PK, URL) not in tun._parked)
        _read(client, "c2")
    assert far.dials == 2


def test_a_parked_connection_is_closed_when_nobody_takes_it(far, monkeypatch):
    monkeypatch.setattr(tun, "PARK_FOR", 0.2)
    with TestClient(_app()) as client:
        _read(client, "p1")
        assert _until(lambda: (PK, URL) in tun._parked)
        assert _until(lambda: far.sockets[0].close_code is not None, 3), "a parked far connection was held for ever"
        assert (PK, URL) not in tun._parked


def test_a_member_parks_at_most_a_few_per_relay(far, monkeypatch):
    monkeypatch.setattr(tun, "PARK_PER", 2)

    async def burst():
        cms = []
        for _ in range(5):
            cm = far.connect(URL, None)
            ws = await cm.__aenter__()
            cms.append((cm, ws))
        kept = [await tun.park(PK, URL, cm, ws, {"ended_by": "client"}) for cm, ws in cms]
        for e in [e for lst in tun._parked.values() for e in lst]:
            e.taken = True
            e.task.cancel()
        return kept
    assert asyncio.run(burst()) == [True, True, False, False, False]


def test_a_relay_ended_tunnel_is_not_parked(far):
    async def go():
        cm = far.connect(URL, None)
        ws = await cm.__aenter__()
        return await tun.park(PK, URL, cm, ws, {"ended_by": "relay"})
    assert asyncio.run(go()) is False


def test_the_report_says_how_many_tunnels_reused_and_who_ended_the_short_ones(monkeypatch, caplog):
    """The 10-minute line is how this is checked in production: dials, reuses, and -- for the short tunnels, whose ender
    was never logged -- who ended them."""
    import logging
    for name in ("_dials", "_short", "_shapes", "_long", "_reused", "_short_by"):
        monkeypatch.setattr(tun, name, {})
    monkeypatch.setattr(tun, "_dials_since", time.monotonic())
    tun._reused["jskitty.com"] = 40
    tun.closed(URL, '["REQ","x",{"kinds":[1059],"authors":["a"],"since":1}]', 2.5, "client", "apk")
    monkeypatch.setattr(tun, "_dials_since", time.monotonic() - tun.DIAL_REPORT_EVERY - 1)
    with caplog.at_level(logging.INFO, logger=tun.logger.name):
        tun.dialled(URL)
    assert "jskitty.com 1 (1 short, 40 reused)" in caplog.text, caplog.text
    assert "short tunnels by who ended them: jskitty.com by client x1" in caplog.text, caplog.text
