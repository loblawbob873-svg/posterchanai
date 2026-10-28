"""The server half of "Concord talks only to this node": `/ws/concord-relay` (app/services/concord_tunnel.py,
app/routers/concord_tunnel.py). Each rule here is a reason the endpoint is not an open proxy, driven through
the shipped router with only the outbound socket faked."""
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.routers import concord_tunnel as router_mod
from app.services import concord_tunnel as tun

PK = "ab" * 32


TOR1, TOR2 = "socks5h://127.0.0.1:9052", "socks5h://127.0.0.1:9062"
TORS = (TOR1, TOR2)


@pytest.fixture(autouse=True)
def fixed_key(monkeypatch):
    monkeypatch.setattr(tun, "_key", lambda: b"k" * 32)
    monkeypatch.setattr(tun, "_open", {})
    monkeypatch.setattr(tun, "_total", 0)
    monkeypatch.setattr(tun, "_good", {})
    monkeypatch.setattr(tun, "_unreachable", {}, raising=False)
    monkeypatch.setattr(tun, "tor_hops", lambda: list(TORS))


def test_a_ticket_names_its_member_and_expires():
    t = tun.issue(PK)
    assert tun.verify(t) == PK
    assert tun.verify(t, now=int(time.time()) + tun.TICKET_TTL + 5) is None, "an expired ticket was accepted"
    pk, exp, sig = t.split(".")
    assert tun.verify(f"{'cd' * 32}.{exp}.{sig}") is None, "a ticket was moved to another member"
    assert tun.verify(f"{pk}.{int(exp) + 9999}.{sig}") is None, "a ticket's expiry was extended"
    assert tun.verify("") is None and tun.verify("garbage") is None


@pytest.mark.parametrize("url", ["ws://127.0.0.1:3052", "wss://localhost/x", "ws://192.168.0.2:8080",
                                 "ws://10.0.0.1", "http://relay.example.com", "wss://poster.place/relay",
                                 "javascript:alert(1)", ""])
def test_private_own_and_non_relay_targets_are_refused(url):
    assert tun.check_target(url)[0] is False, url


def test_a_public_relay_is_allowed(monkeypatch):
    import socket
    monkeypatch.setattr(socket, "gethostbyname", lambda h: "93.184.216.34")
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: [(2, 1, 6, "", ("93.184.216.34", 443))])
    assert tun.check_target("wss://jskitty.com/nostr") == (True, "")


class _Upstream:
    """A far relay that echoes what it is sent, as an async-iterable websocket."""
    def __init__(self):
        import asyncio
        self.q = asyncio.Queue()
        self.sent = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def send(self, data):
        self.sent.append(data)
        await self.q.put('["NOTICE","echo:' + data.replace('"', "'") + '"]')

    def __aiter__(self):
        return self

    async def __anext__(self):
        return await self.q.get()


@pytest.fixture
def client(monkeypatch):
    import socket
    monkeypatch.setattr(socket, "gethostbyname", lambda h: "93.184.216.34")
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: [(2, 1, 6, "", ("93.184.216.34", 443))])
    monkeypatch.setattr(tun, "proxy_url", lambda: "http://127.0.0.1:8119")
    seen = []

    def connect(url, proxy):
        seen.append((url, proxy))
        return _Upstream()
    monkeypatch.setattr(tun, "connect", connect)
    app = FastAPI()
    app.include_router(router_mod.ws_router)
    c = TestClient(app)
    c.seen = seen
    return c


def test_frames_pass_both_ways_through_the_fallback_proxy(client):
    t = tun.issue(PK)
    with client.websocket_connect(f"/ws/concord-relay?u=wss://jskitty.com/nostr&t={t}") as ws:
        ws.send_text('["REQ","x",{"kinds":[1059]}]')
        assert "echo:" in ws.receive_text()
    assert client.seen == [("wss://jskitty.com/nostr", TOR1)], "not dialled through Tor 1 first"
    assert tun._open == {} and tun._total == 0, "a closed tunnel was not released"


def test_no_ticket_no_tunnel(client):
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/ws/concord-relay?u=wss://jskitty.com/nostr&t=forged") as ws:
            ws.receive_text()
    assert client.seen == [], "a relay was dialled for a forged ticket"


def test_a_private_target_is_never_dialled(client):
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(f"/ws/concord-relay?u=ws://127.0.0.1:3052&t={tun.issue(PK)}") as ws:
            ws.receive_text()
    assert client.seen == []


def test_a_member_cannot_hold_more_than_their_share(client, monkeypatch):
    monkeypatch.setattr(tun, "_open", {PK: tun.PER_MEMBER})
    monkeypatch.setattr(tun, "_total", tun.PER_MEMBER)
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(f"/ws/concord-relay?u=wss://jskitty.com/nostr&t={tun.issue(PK)}") as ws:
            ws.receive_text()
    assert client.seen == []


def test_a_member_holds_as_many_tunnels_as_concord_opens():
    """Concord keeps a live subscription per relay per room; 24 was full on one real account and every send after it
    was refused. A member with dozens open still gets the next one."""
    tun._open.clear()
    tun._total = 0
    try:
        for _ in range(100):
            assert tun.acquire(PK)
        assert tun.acquire(PK), "the 101st tunnel of one member was refused"
    finally:
        tun._open.clear()
        tun._total = 0


def test_no_proxy_means_no_tunnel(client, monkeypatch):
    monkeypatch.setattr(tun, "proxy_url", lambda: "")
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(f"/ws/concord-relay?u=wss://jskitty.com/nostr&t={tun.issue(PK)}") as ws:
            ws.receive_text()
    assert client.seen == [], "the server dialled a relay directly, without the proxy"


def _dialer(seen, works):
    """connect() that records each hop and succeeds only on the hops in `works`."""
    def connect(url, proxy):
        seen.append(proxy)
        if proxy not in works:
            class Refused:
                async def __aenter__(self):
                    raise TimeoutError("timed out during opening handshake")

                async def __aexit__(self, *a):
                    return False
            return Refused()
        return _Upstream()
    return connect


def _open_one(client, url):
    with client.websocket_connect(f"/ws/concord-relay?u={url}&t={tun.issue(PK)}") as ws:
        ws.send_text('["REQ","x",{}]')
        assert "echo:" in ws.receive_text()


def test_tor1_then_tor2_then_direct(client, monkeypatch):
    """"make sure our concord uses the http proxy correctly! tor1 tor2 then default" (2026-10-27). The proxy listener
    fails over only on a CONNECT error, so a relay that timed out over one exit went straight to this node's own
    address; jskitty.com and asia.vectorapp.io then 403'd this IP with any User-Agent. Every Tor daemon is a whole
    handshake attempt, in the proxy's order; direct only after all of them."""
    seen = []
    monkeypatch.setattr(tun, "connect", _dialer(seen, {None}))
    _open_one(client, "wss://asia.vectorapp.io/nostr")
    assert seen == [TOR1, TOR2, None], seen
    assert tun._open == {} and tun._total == 0


def test_a_tor1_failure_goes_to_tor2_never_to_direct(client, monkeypatch):
    seen = []
    monkeypatch.setattr(tun, "connect", _dialer(seen, {TOR2, None}))
    _open_one(client, "wss://asia.vectorapp.io/nostr")
    assert seen == [TOR1, TOR2], ("direct was dialled while a Tor circuit still worked", seen)


def test_the_circuit_that_worked_goes_first_and_direct_never_does(client, monkeypatch):
    seen = []
    monkeypatch.setattr(tun, "connect", _dialer(seen, {TOR2, None}))
    _open_one(client, "wss://asia.vectorapp.io/nostr")
    _open_one(client, "wss://asia.vectorapp.io/nostr")
    assert seen == [TOR1, TOR2, TOR2], seen
    # A host that only answered DIRECT is still tried over Tor first the next time.
    seen.clear()
    monkeypatch.setattr(tun, "connect", _dialer(seen, {None}))
    _open_one(client, "wss://relay.damus.io")
    _open_one(client, "wss://relay.damus.io")
    assert seen == [TOR1, TOR2, None, TOR1, TOR2, None], ("a host was dialled direct first", seen)
    # ...and a circuit that stops working loses its place.
    seen.clear()
    monkeypatch.setattr(tun, "connect", _dialer(seen, {TOR1}))
    _open_one(client, "wss://asia.vectorapp.io/nostr")
    assert seen == [TOR2, TOR1], seen


def test_the_tor_list_is_the_proxys_own(monkeypatch):
    from app.services import http_proxy_service as hp
    monkeypatch.undo()
    monkeypatch.setattr(hp, "socks_backends", lambda ss=None: [(9052, "us"), (9062, "ca"), (9072, "us3")])
    assert tun.tor_hops() == ["socks5h://127.0.0.1:9052", "socks5h://127.0.0.1:9062", "socks5h://127.0.0.1:9072"]
    assert tun.egress_hops("wss://jskitty.com/nostr")[-1] is None


def test_the_handshake_does_not_wait_for_the_far_relay(client, monkeypatch):
    """A browser lets ONE websocket per host sit in CONNECTING (RFC 6455 4.1); the rest queue. The tunnel used to
    hold its handshake open until the far relay answered -- up to ~25s over Tor, plus a direct retry -- so every
    Concord dial queued the app's OWN relay socket behind it: 2026-10-13 the laptop's wss://poster.place/relay sat
    in CONNECTING for minutes ("connected to local relay but not connecting to posterchan relay"). Accept first,
    then dial; what the client sends meanwhile waits in the socket and is delivered once the far relay is up."""
    import time

    class _Slow(_Upstream):
        async def __aenter__(self):
            import asyncio
            await asyncio.sleep(2.0)
            return self
    monkeypatch.setattr(tun, "connect", lambda url, proxy: _Slow())
    t = tun.issue(PK)
    t0 = time.monotonic()
    with client.websocket_connect(f"/ws/concord-relay?u=wss://jskitty.com/nostr&t={t}") as ws:
        opened = time.monotonic() - t0
        ws.send_text('["REQ","x",{"kinds":[1059]}]')
        assert "echo:" in ws.receive_text(), "a frame sent before the far relay answered was lost"
    assert opened < 1.0, "the handshake waited %.1fs for the far relay" % opened


def test_a_far_relay_nobody_can_reach_closes_the_open_tunnel(client, monkeypatch):
    class _Dead(_Upstream):
        async def __aenter__(self):
            raise OSError("unreachable")
    monkeypatch.setattr(tun, "connect", lambda url, proxy: _Dead())
    t = tun.issue(PK)
    with pytest.raises(WebSocketDisconnect) as e:
        with client.websocket_connect(f"/ws/concord-relay?u=wss://jskitty.com/nostr&t={t}") as ws:
            ws.receive_text()
    assert e.value.code == 4502
    assert tun._open == {} and tun._total == 0


def test_tor_gets_seconds_not_the_clients_whole_budget():
    """Measured 2026-10-14: a Tor attempt at the full 20 s outlived the client's 10 s control read. A healthy Tor dial
    opens in 2-5 s (asia.vectorapp.io's one working circuit: 4.0 s, 2026-10-27), and the circuit that worked is tried
    first next time, so a remembered host fits the read."""
    assert tun.open_timeout(TOR1) + 1 < 10
    assert tun.open_timeout(None) == tun.OPEN_TIMEOUT


def test_a_relay_no_hop_reached_is_not_dialled_again_for_a_while(client, monkeypatch):
    """Each retry of an unreachable relay is every Tor circuit plus this node's address; every page of every member
    retries every few seconds. For UNREACHABLE_FOR the answer is given without dialling anything."""
    seen = []
    monkeypatch.setattr(tun, "connect", _dialer(seen, set()))
    for _ in range(3):
        with pytest.raises(WebSocketDisconnect) as e:
            with client.websocket_connect(f"/ws/concord-relay?u=wss://asia.vectorapp.io/nostr&t={tun.issue(PK)}") as ws:
                ws.receive_text()
        assert e.value.code == 4502
    assert seen == [TOR1, TOR2, None], ("an unreachable relay was dialled again inside its window", seen)
    # Another relay is unaffected, and once the window passes the whole chain is tried again.
    monkeypatch.setattr(tun, "connect", _dialer(seen, {TOR1}))
    _open_one(client, "wss://jskitty.com/nostr")
    monkeypatch.setitem(tun._unreachable, "asia.vectorapp.io", 0.0)
    seen.clear()
    _open_one(client, "wss://asia.vectorapp.io/nostr")
    assert seen == [TOR1], seen
    assert tun._open == {} and tun._total == 0
