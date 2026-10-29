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
    monkeypatch.setattr(tun, "_strikes", {}, raising=False)
    monkeypatch.setattr(tun, "_reached", {}, raising=False)
    monkeypatch.setattr(tun, "_dialing", {}, raising=False)
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
    _open_one(client, "wss://relay.primal.net")
    _open_one(client, "wss://relay.primal.net")
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


def test_a_relay_that_stays_unreachable_is_asked_less_and_less_often(monkeypatch):
    """Measured on poster.place 2026-10-27: asia.vectorapp.io unreachable through all four Tor daemons and direct, and
    the flat two-minute pause still walked the whole chain ~19 times an hour. Each full failure in a row doubles the
    pause, up to UNREACHABLE_MAX; reaching the host by any hop starts it over."""
    clock = [1000.0]
    monkeypatch.setattr(tun.time, "monotonic", lambda: clock[0])
    url = "wss://asia.vectorapp.io/nostr"
    pauses = []
    for _ in range(7):
        assert not tun.unreachable(url)
        tun.all_hops_failed(url)
        pauses.append(tun._unreachable["asia.vectorapp.io"] - clock[0])
        clock[0] += pauses[-1] + 1
    assert pauses == [120, 240, 480, 960, 1800, 1800, 1800], pauses
    tun.hop_result(url, None, True)                      # this node's own address got through
    tun.all_hops_failed(url)
    assert tun._unreachable["asia.vectorapp.io"] - clock[0] == 120, "a host that was reached kept its long pause"
    assert not tun.unreachable("wss://jskitty.com/nostr"), "one host's failures paused another"


def test_every_dial_is_counted_and_reported_per_host(monkeypatch, caplog):
    """jskitty blocked this node for churning websockets (2026-10-28); the count is what tells us whether we still do."""
    monkeypatch.setattr(tun, "_dials", {})
    monkeypatch.setattr(tun, "_dials_since", time.monotonic() - tun.DIAL_REPORT_EVERY - 1)
    import logging
    with caplog.at_level(logging.INFO, logger=tun.logger.name):
        tun.dialled("wss://jskitty.com/nostr")
    assert any("jskitty.com 1" in r.getMessage() for r in caplog.records), caplog.text
    assert tun._dials == {}


def test_a_short_tunnel_is_reported_by_what_it_asked_for_never_by_its_values(client, monkeypatch, caplog):
    """The dial count says THAT we churn a far relay; this says WHICH client path does it: tunnels that close within
    seconds of their dial, grouped by the verb and kinds of their first frame. A filter value (an author, a tag) and
    any content never reach the log."""
    import logging
    monkeypatch.setattr(tun, "_dials", {})
    monkeypatch.setattr(tun, "_short", {})
    monkeypatch.setattr(tun, "_shapes", {})
    monkeypatch.setattr(tun, "_dials_since", time.monotonic())
    t = tun.issue(PK)
    with client.websocket_connect("/ws/concord-relay?u=wss://jskitty.com/nostr&t=" + t) as ws:
        ws.send_text('["REQ","s1",{"kinds":[1059],"#p":["%s"]},{"kinds":[10009]}]' % ("ab" * 32))
        ws.receive_text()
    for _ in range(50):
        if tun._shapes:
            break
        time.sleep(0.02)
    monkeypatch.setattr(tun, "_dials_since", time.monotonic() - tun.DIAL_REPORT_EVERY - 1)
    with caplog.at_level(logging.INFO, logger=tun.logger.name):
        tun.dialled("wss://relay.ditto.pub")
    assert "jskitty.com 1 (1 short)" in caplog.text, caplog.text
    assert "jskitty.com REQ 1059,10009 x1" in caplog.text, caplog.text   # no authors, no since, no limit
    assert "ab" * 32 not in caplog.text


def test_the_first_frame_shape_carries_no_values():
    assert tun.frame_shape('["EVENT",{"kind":1059,"content":"secret","pubkey":"x"}]') == "EVENT 1059"
    assert tun.frame_shape('["REQ","x",{"authors":["a"]}]') == "REQ any a1"
    assert tun.frame_shape('["REQ","x",{"kinds":[1059],"authors":["%s","%s"],"since":1793000000}]' % ("a" * 64, "b" * 64)) \
        == "REQ 1059 a2 since"
    assert tun.frame_shape('["REQ","x",{"kinds":[1059],"authors":["a"],"limit":1000}]') == "REQ 1059 a1 limit"
    assert tun.frame_shape('["REQ","x",{"kinds":[1,2,3,4,5,6]}]') == "REQ 1,2,3,4+"
    assert tun.frame_shape("not json") == "?"
    assert tun.frame_shape('["AUTH",{"kind":22242}]') == "AUTH"


def test_a_long_tunnel_is_reported_by_who_ended_it(monkeypatch, caplog):
    """Most of jskitty's dials were LONG tunnels (subscriptions) re-dialled again and again; who ends them -- the
    member's client or the far relay -- and after how long is what says why."""
    import logging
    for name in ("_dials", "_short", "_shapes", "_long"):
        monkeypatch.setattr(tun, name, {})
    monkeypatch.setattr(tun, "_dials_since", time.monotonic() - tun.DIAL_REPORT_EVERY - 1)
    tun.closed("wss://jskitty.com/nostr", '["REQ","x",{"kinds":[1059]}]', 40.0, "client")
    tun.closed("wss://jskitty.com/nostr", '["REQ","y",{"kinds":[1059]}]', 90.0, "client")
    tun.closed("wss://jskitty.com/nostr", '["REQ","z",{"kinds":[1059]}]', 300.0, "relay")
    with caplog.at_level(logging.INFO, logger=tun.logger.name):
        tun.dialled("wss://jskitty.com/nostr")
    assert "jskitty.com by client REQ 1059 x2 (90s)" in caplog.text, caplog.text
    assert "jskitty.com by relay REQ 1059 x1 (300s)" in caplog.text, caplog.text


def test_the_pipe_says_the_client_ended_it(client, monkeypatch):
    seen = []
    monkeypatch.setattr(tun, "closed", lambda url, frame, secs, who, client="": seen.append((frame, who)))
    with client.websocket_connect("/ws/concord-relay?u=wss://jskitty.com/nostr&t=" + tun.issue(PK)) as ws:
        ws.send_text('["REQ","s",{"kinds":[1059]}]')
        ws.receive_text()
    for _ in range(50):
        if seen:
            break
        time.sleep(0.02)
    assert seen == [('["REQ","s",{"kinds":[1059]}]', "client")], seen


def test_the_report_names_the_kind_of_client_never_the_device():
    assert tun.client_kind("Mozilla/5.0 (Linux; Android 16; SM-X400 Build/x; wv) AppleWebKit/537.36 Chrome/141 Safari/537.36") == "apk"
    assert tun.client_kind("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 PosterChan/1.0 Chrome/141 Electron/38.2.0 Safari/537.36") == "desktop"
    assert tun.client_kind("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/141 Safari/537.36") == "browser"
    assert tun.client_kind("") == "?"
    # The desktop app strips Electron/ from its UA; its ORIGIN says what it is (and the APK's).
    chrome = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/141 Safari/537.36"
    assert tun.client_kind(chrome, "app://posterchan") == "desktop"
    assert tun.client_kind(chrome, "https://localhost") == "apk"
    assert tun.client_kind(chrome, "https://poster.place") == "browser"


def test_a_long_tunnel_is_reported_with_its_client_kind(monkeypatch, caplog):
    import logging
    for name in ("_dials", "_short", "_shapes", "_long"):
        monkeypatch.setattr(tun, name, {})
    monkeypatch.setattr(tun, "_dials_since", time.monotonic() - tun.DIAL_REPORT_EVERY - 1)
    tun.closed("wss://jskitty.com/nostr", '["REQ","x",{"kinds":[1059],"authors":["a"],"since":1}]', 60.0, "client", "apk")
    with caplog.at_level(logging.INFO, logger=tun.logger.name):
        tun.dialled("wss://jskitty.com/nostr")
    assert "jskitty.com by client REQ 1059 a1 since [apk] x1 (60s)" in caplog.text, caplog.text


def _slow_dialer(seen, works, delay=0.05):
    """connect() that takes `delay` seconds to answer -- long enough for a burst to overlap."""
    import asyncio

    def connect(url, proxy):
        seen.append(proxy)

        class Slow:
            async def __aenter__(self):
                await asyncio.sleep(delay)
                if proxy not in works:
                    raise TimeoutError("timed out during opening handshake")
                return _Upstream()

            async def __aexit__(self, *a):
                return False
        return Slow()
    return connect


def test_a_burst_of_tunnels_to_a_dead_relay_dials_the_chain_once(monkeypatch):
    """Measured on poster.place 2026-10-29: asia.vectorapp.io six full chains (four Tor daemons and this node's address,
    each) in 9 s, fifteen in 20 s -- Concord opens several tunnels to one relay at once, and every one of them walked
    the whole chain before the first could record the failure. One walks it; the rest get the answer."""
    import asyncio
    seen = []
    monkeypatch.setattr(tun, "connect", _slow_dialer(seen, set()))

    async def burst():
        return await asyncio.gather(*[tun.open_upstream("wss://asia.vectorapp.io/nostr") for _ in range(6)])
    got = asyncio.run(burst())
    assert all(up is None for _, up in got), got
    assert seen == [TOR1, TOR2, None], ("the burst dialled the chain more than once", seen)


def test_a_burst_to_a_relay_that_answers_still_opens_every_tunnel(monkeypatch):
    """The first tunnel to a host nothing has reached yet goes alone; once it got through, the rest dial in parallel
    (its working circuit first) -- a healthy relay is never made to wait in line."""
    import asyncio
    seen = []
    monkeypatch.setattr(tun, "connect", _slow_dialer(seen, {TOR2}))

    async def burst():
        return await asyncio.gather(*[tun.open_upstream("wss://nos.lol") for _ in range(5)])
    got = asyncio.run(burst())
    assert all(up is not None for _, up in got), got
    assert seen[:2] == [TOR1, TOR2] and seen[2:] == [TOR2] * 4, ("after the first got through, the circuit that worked "
                                                                  "should go first for the rest", seen)


def test_a_relay_reached_lately_is_dialled_in_parallel(monkeypatch):
    import asyncio
    seen = []
    monkeypatch.setattr(tun, "connect", _slow_dialer(seen, {TOR1}, delay=0.2))
    tun.hop_result("wss://nos.lol", TOR1, True)

    async def burst():
        t0 = asyncio.get_running_loop().time()
        await asyncio.gather(*[tun.open_upstream("wss://nos.lol") for _ in range(5)])
        return asyncio.get_running_loop().time() - t0
    took = asyncio.run(burst())
    assert took < 0.6, ("five dials to a known-good relay were serialized: %.2fs" % took)


def test_a_relay_that_answered_lately_and_died_walks_the_rest_of_the_chain_once(monkeypatch):
    """Measured on poster.place 2026-10-29, after the single-flight above shipped: asia.vectorapp.io -- reached by one
    Tor exit now and then, so "known to answer" for GOOD_FOR -- still took bursts of 30-35 failed dials a minute
    (16:50, 16:53, 16:57): five tunnels at once, each walking every Tor daemon and this node's address in parallel.
    Its best hop may be tried by each (it is the one that worked); the rest of the chain is walked by one tunnel."""
    import asyncio
    seen = []
    monkeypatch.setattr(tun, "connect", _slow_dialer(seen, set()))
    tun.hop_result("wss://asia.vectorapp.io/nostr", TOR2, True)        # reached through Tor 2 a while ago

    async def burst():
        return await asyncio.gather(*[tun.open_upstream("wss://asia.vectorapp.io/nostr") for _ in range(5)])
    got = asyncio.run(burst())
    assert all(up is None for _, up in got), got
    assert sorted(seen, key=str) == sorted([TOR2] * 5 + [TOR1, None], key=str), (
        "the rest of the chain was walked by more than one tunnel", len(seen), seen)
    later = len(seen)
    asyncio.run(burst())
    assert len(seen) == later, "inside its pause the dead relay was dialled again"
