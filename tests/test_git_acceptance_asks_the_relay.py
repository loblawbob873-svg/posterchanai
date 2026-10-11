"""The git host's acceptance policy asks the RELAY whether an author is in its web of trust -- not Postgres.

#161: `git_host_main` read `SELECT 1 FROM wot` and `FROM users` from the relay's Postgres. A PosterChanDB node has
neither table (nas.lan, which hosts git, has no DATABASE_URL at all), so the check failed closed and every new
repository was refused. The relay now answers `/_wot/<pubkey>` on loopback from the same gate it admits writes
with; this runs a real RelayServer and asks it through the git host's own lookup.
"""
import asyncio
import json
import os
import socket
import sys
import threading
import urllib.error
import urllib.request

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

MEMBER, STRANGER = "a" * 64, "b" * 64


class _Gate:
    def is_member(self, pk):
        return pk == MEMBER

    def is_operator(self, pk):
        return False

    def is_blocked(self, pk):
        return False

    def is_puppet_event(self, ev):
        return False


@pytest.fixture
def relay():
    import websockets
    from app.services.nostr_relay.server import RelayServer
    ready, box = threading.Event(), {}

    def run():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        async def main():
            srv = RelayServer(None, _Gate(), {"wot_enabled": True, "node_pubkey": "c" * 64, "operator": []})
            ws = await websockets.serve(srv.handle, "127.0.0.1", 0, process_request=srv.process_request)
            box["port"] = ws.sockets[0].getsockname()[1]
            box["stop"] = asyncio.Event()
            ready.set()
            await box["stop"].wait()
            ws.close()
        box["loop"] = loop
        loop.run_until_complete(main())
    t = threading.Thread(target=run, daemon=True)
    t.start()
    assert ready.wait(20)
    yield box["port"]
    box["loop"].call_soon_threadsafe(box["stop"].set)
    t.join(5)


def _get(port, path, headers=None):
    req = urllib.request.Request("http://127.0.0.1:%d%s" % (port, path), headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode() or "{}")


def test_the_git_host_asks_the_relays_gate(relay, monkeypatch):
    import git_host_main as gh
    monkeypatch.setattr(gh, "_CONFIG", {"relay_port": relay}, raising=False)
    assert gh._relay_wot_member(MEMBER) is True
    assert gh._relay_wot_member(STRANGER) is False


def test_membership_is_not_published_through_a_proxy(relay):
    assert _get(relay, "/_wot/" + MEMBER)[0] == 200
    for h in ("X-Forwarded-For", "X-Real-IP", "Forwarded", "CF-Connecting-IP"):
        status, body = _get(relay, "/_wot/" + MEMBER, {h: "203.0.113.9"})
        assert status == 403 and "member" not in body, h


def test_a_repeated_proxy_header_does_not_slip_through(relay):
    """`_one_header` reads a repeated header as absent; the probe must not."""
    import http.client
    c = http.client.HTTPConnection("127.0.0.1", relay, timeout=5)
    c.putrequest("GET", "/_wot/" + MEMBER)
    c.putheader("X-Forwarded-For", "203.0.113.9")
    c.putheader("X-Forwarded-For", "198.51.100.7")
    c.endheaders()
    r = c.getresponse()
    assert r.status == 403 and b"member" not in r.read()


def test_a_malformed_key_is_refused(relay):
    assert _get(relay, "/_wot/not-a-key")[0] == 400
    assert _get(relay, "/_wot/" + "A" * 63)[0] == 400


def test_a_relay_that_cannot_be_asked_raises_and_the_host_refuses(monkeypatch):
    import git_host_main as gh
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    dead = s.getsockname()[1]
    s.close()
    monkeypatch.setattr(gh, "_CONFIG", {"relay_port": dead}, raising=False)
    with pytest.raises(Exception):
        gh._relay_wot_member(MEMBER)
    h = object.__new__(gh._Handler)
    assert h._is_wot_member(MEMBER) is False
