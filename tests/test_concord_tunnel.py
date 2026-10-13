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


@pytest.fixture(autouse=True)
def fixed_key(monkeypatch):
    monkeypatch.setattr(tun, "_key", lambda: b"k" * 32)
    monkeypatch.setattr(tun, "_open", {})
    monkeypatch.setattr(tun, "_total", 0)


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
    assert client.seen == [("wss://jskitty.com/nostr", "http://127.0.0.1:8119")], "not dialled through the proxy"
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


def test_a_relay_that_refuses_tor_is_reached_on_the_direct_hop(client, monkeypatch):
    """Measured 2026-10-13 after deploy 217: through the fallback proxy relay.damus.io answered HTTP 503 (it refuses
    Tor exits) and asia.vectorapp.io -- one of the four CORD relays -- timed out, while both answered DIRECT. The
    proxy's own Tor -> Tor -> direct chain falls back only on a CONNECT error, never on a refusal after it, so the
    tunnel takes that last hop itself."""
    seen = []

    def connect(url, proxy):
        seen.append(proxy)
        if proxy:
            class Refused:
                async def __aenter__(self):
                    raise TimeoutError("timed out during opening handshake")

                async def __aexit__(self, *a):
                    return False
            return Refused()
        return _Upstream()
    monkeypatch.setattr(tun, "connect", connect)
    with client.websocket_connect(f"/ws/concord-relay?u=wss://asia.vectorapp.io/nostr&t={tun.issue(PK)}") as ws:
        ws.send_text('["REQ","x",{}]')
        assert "echo:" in ws.receive_text()
    assert seen == ["http://127.0.0.1:8119", None], seen
    assert tun._open == {} and tun._total == 0


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
