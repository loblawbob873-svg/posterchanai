"""A client behind a reverse proxy on the SAME machine is a remote client: paced, and counted online.

Security + correctness reviews, 2026-10-18: the relay called a connection "this node's own process" whenever the
socket's own end was 127.*. docs/RELAY.md tells an installer to run nginx on the same box with
`proxy_pass http://127.0.0.1:3052`, so there EVERY public client arrived on a 127.0.0.1 socket -- exempt from the
per-connection answer budget (one client could flood again) and counted as nobody online. Our own processes connect
straight to the socket; a proxy always adds a forwarding header (the documented config sets X-Real-IP).
"""
from app.services.nostr_relay import server as S
from app.services.nostr_relay.server import RelayServer


class Conn:
    def __init__(self, local, proxied):
        self.local_address = (local, 3052)
        if proxied:
            self._pcai_proxied = True


def _srv(conns):
    srv = RelayServer.__new__(RelayServer)
    srv._conn_ips = dict(conns)
    srv._auth_pubkeys = {}
    srv._conns = len(conns)
    return srv


def test_a_client_through_a_same_host_proxy_is_remote_and_online():
    public = Conn("127.0.0.1", proxied=True)          # nginx on this machine -> 127.0.0.1:3052, X-Real-IP set
    srv = _srv({public: "203.0.113.5"})
    assert srv._req_origin(public, "sub-notes-1").startswith("R:")
    b = srv.online_breakdown()
    assert b["online"] == 1 and b["loopback_conns"] == 0, b


def test_our_own_processes_are_still_ours():
    app = Conn("127.0.0.1", proxied=False)           # the app / worker / bots, straight to the socket
    natted = Conn("127.0.0.1", proxied=False)        # server1's NAT shows a loopback peer as 192.168.0.2
    srv = _srv({app: "127.0.0.1", natted: "192.168.0.2"})
    assert srv._req_origin(app, "dt-x").startswith("L:")
    assert srv._req_origin(natted, "repo1").startswith("L:")
    b = srv.online_breakdown()
    assert b["online"] == 0 and b["loopback_conns"] == 2, b


def test_a_forwarding_header_never_makes_a_client_ours():
    spoof = Conn("192.168.0.10", proxied=True)       # "X-Forwarded-For: 127.0.0.1" from outside
    assert not S._own_process(spoof, "127.0.0.1")
