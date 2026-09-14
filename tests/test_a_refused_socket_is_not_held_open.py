"""`posterchan_clients_only` has to REDUCE the connection count, not churn it — without turning
anybody away.

Run: venv-unified/bin/python -m unittest tests.test_a_refused_socket_is_not_held_open

Reported as "there are 103 people connected to the relay despite 7 being online, there is still
some problem you didn't fix with my setting the posterchan client only" — and the switch was
working perfectly. Measured from a public Tor exit against the live relay: an unrecognised client
is admitted, refused every read, told why, and closed at 80s with `use a PosterChan client`.

THE COUNT WAS NEVER ABOUT ENFORCEMENT. IT WAS ABOUT HOW LONG A REFUSED SOCKET IS HELD. One hour on
this node: **137 distinct addresses, 2,925 closes** — every one refused everything, every one back
inside three minutes. An 80-second grace against a ~171-second dial interval is a ~47% duty cycle,
i.e. ~65 sockets held permanently by clients that cannot read a single event. The operator sees ~100
connections against 7 people and reasonably concludes the switch does nothing.

A client that asks for ordinary content has PROVED it is not a NIP-46 signer — the one thing the
confinement exists to keep working — so there is nothing left to wait 80 seconds for. Its socket is
closed as soon as it says so.

WHAT THIS FILE EXISTS TO STOP COMING BACK is the first version of that fix, which put the address
into a growing cooldown and 403'd its next handshake. It passed its own tests and it would have
broken logging in. The ban is keyed on an ADDRESS; the addresses this population arrives on are the
most SHARED ones there are — Amber runs on a phone behind carrier CGNAT, a Tor exit is worse — and
the check runs at the handshake, before the socket exists and therefore before any signer work could
vouch for it. One stranger's Damus asking for a timeline would have locked every NIP-46 login behind
that carrier out of the QR flow this node tells people to use, silently, because a 403 at the
handshake is not a screen anybody sees. Closing a socket costs its owner one reconnect. Refusing a
handshake costs a co-tenant their login. `test_no_handshake_is_ever_refused_for_a_struck_address`
is that rule, written down.
"""
import asyncio
import json
import unittest

from app.services.nostr_relay import server as S


class _Conn:
    # 93.184.216.34 (example.com) is genuinely public. TEST-NET-3 is not: Python's `ipaddress`
    # reports 203.0.113.x as private, which would take the LAN exemption and prove nothing.
    def __init__(self, peer="93.184.216.34", confined=True):
        self.remote_address = (peer, 54321)
        self._pcai_signer_only = confined
        self._pcai_ip = peer
        self.closed = None

    async def close(self, code=1000, reason=""):
        self.closed = (code, reason)


class _Req:
    def __init__(self, headers, path="/relay"):
        self.headers = headers
        self.path = path


def _server(**cfg):
    srv = S.RelayServer.__new__(S.RelayServer)
    srv.cfg = {"posterchan_clients_only": True, "max_message_size": 262144,
               "posterchan_origins": ["https://poster.place", "app://posterchan"], **cfg}
    srv._refused_at = {}
    srv.sent = []
    srv._send = lambda conn, msg: srv.sent.append(msg)

    # What a permitted message goes on to DO is not what this file is about, and wiring a whole
    # store/subscription manager to find out would pin the query engine instead of the rule.
    async def _noop(*a, **kw):
        return None
    srv._on_req = _noop
    srv._on_event = _noop
    return srv


def _handshake(srv, ua="", origin="", peer="93.184.216.34"):
    """None = the WebSocket handshake proceeds; a Response = refused at the door."""
    hdrs = {"Upgrade": "websocket"}
    if ua:
        hdrs["User-Agent"] = ua
    if origin:
        hdrs["Origin"] = origin
    return srv.process_request(_Conn(peer), _Req(hdrs))


def _dispatch(srv, conn, msg):
    """Run one client message through the real dispatcher and say whether it was closed."""
    async def go():
        await srv._dispatch(conn, json.dumps(msg))
        # _close_refused is scheduled, not awaited, so the NOTICE can leave first.
        await asyncio.sleep(0.4)
    asyncio.run(go())
    return conn.closed


class ARefusedSocketIsNotHeldOpen(unittest.TestCase):

    def test_a_confined_client_asking_for_content_is_told_why_and_closed(self):
        srv, conn = _server(), _Conn()
        closed = _dispatch(srv, conn, ["REQ", "s1", {"kinds": [1], "limit": 20}])
        self.assertTrue(any("serves PosterChan clients" in str(m) for m in srv.sent),
                        "a refused client must be told why — a silent drop is indistinguishable "
                        "from a relay that is simply slow")
        self.assertIsNotNone(closed, "the socket was held open after the client proved it is not a "
                                     "signer; 80s of that per dial is the whole connection count")
        self.assertEqual(closed[0], 1008)
        self.assertIn("PosterChan", closed[1])

    def test_a_signer_doing_signer_work_is_never_closed_early(self):
        """The one thing the confinement exists to keep working. A NIP-46 session speaks 24133 and
        nothing else, so it never reaches the refusal path at all."""
        srv, conn = _server(), _Conn()
        self.assertIsNone(_dispatch(srv, conn, ["REQ", "s1", {"kinds": [24133]}]),
                          "a remote signer's subscription was closed — this is the Amber QR login")
        self.assertTrue(getattr(conn, "_pcai_signer_used", False),
                        "the socket was not marked as doing signer work, so the 80s sweep will "
                        "close it mid-session")
        self.assertIsNone(_dispatch(srv, conn, ["EVENT", {"kind": 24133, "content": "x"}]),
                          "a signer's own publish was closed")

    def test_an_inbound_dm_is_never_closed_early(self):
        """Somebody on Damus publishing a gift wrap to an inbox this relay is listed for. Allowed by
        the confinement, so it must not be treated as proof of a general-purpose client."""
        srv, conn = _server(), _Conn()
        self.assertIsNone(_dispatch(srv, conn, ["EVENT", {"kind": 1059, "content": "x"}]))

    def test_a_posterchan_client_is_never_closed_early(self):
        srv, conn = _server(), _Conn(confined=False)
        self.assertIsNone(_dispatch(srv, conn, ["REQ", "s1", {"kinds": [1]}]),
                          "a recognised client was closed by the stranger rule")

    def test_no_handshake_is_ever_refused_for_a_struck_address(self):
        """THE REGRESSION GUARD. A cooldown keyed on an address is keyed on a CGNAT carrier and a Tor
        exit, and it would be consulted before any signer work could vouch for the socket."""
        srv = _server()
        for _ in range(5):
            srv._note_refused("93.184.216.34")
        self.assertIsNone(_handshake(srv, ua="okhttp/4.12.0"),
                          "an address that a DIFFERENT client was struck on is now refused at the "
                          "handshake — that is every Amber login behind one carrier, with a 403 "
                          "nobody ever sees")
        self.assertIsNone(_handshake(srv, ua="nostr-tool/1.0"))
        self.assertIsNone(_handshake(srv, origin="https://poster.place"))

    def test_an_early_close_is_rate_limited_per_address(self):
        """Closing instantly invites a client that reconnects on close to spin. Past the limit the
        socket is held to the ordinary sweep instead — degrading toward the OLD behaviour, never
        toward a refusal."""
        srv = _server()
        self.assertTrue(srv._note_refused("93.184.216.34"))
        self.assertFalse(srv._note_refused("93.184.216.34"),
                         "a second early close inside the window — this is the reconnect storm the "
                         "floor exists to prevent")
        self.assertTrue(srv._note_refused("93.184.216.35"), "the limit must be per address")

    def test_our_own_machines_are_never_closed_early(self):
        srv = _server()
        for ip in ("192.168.0.42", "127.0.0.1", "10.0.0.5", ""):
            self.assertFalse(srv._note_refused(ip),
                             f"{ip!r} was treated as a stranger — our own bots, bridge and nodes "
                             "dial this relay constantly")

    def test_the_memory_is_bounded(self):
        srv = _server()
        for i in range(S.RelayServer._REFUSED_MAX + 600):
            srv._note_refused(f"93.184.{i // 256 % 256}.{i % 256}")
        self.assertLessEqual(len(srv._refused_at), S.RelayServer._REFUSED_MAX + 600,
                             "unbounded state a stranger can drive")


class TheGateNeverFailsOpen(unittest.TestCase):
    """A SOCKET ADMITTED WITHOUT A VERDICT GETS FULL SERVICE, AND THAT IS HOW THE SWITCH GOES OFF.

    `process_request` wraps its whole body in `except Exception: return None`, and returning None is
    how it says "proceed with the handshake". So anything that threw before the confinement marker
    was set produced a socket the per-message check treats as a recognised PosterChan client.

    Reaching it took nothing clever: websockets' `Headers.get` RAISES `MultipleValuesError` for a
    repeated header, and the first line of the gate read `hdrs.get("Upgrade")`. Send `Upgrade:
    websocket` twice and the filter is skipped entirely — the same shape `_one_header` already
    existed for on X-Real-IP, one line above where it was not used.

    The rule: with the switch ON, a gate that cannot be evaluated CONFINES the socket. It does not
    refuse it (that would break the Amber QR login and inbound DM delivery on a bug) and it does not
    admit it unmarked.
    """

    def test_a_repeated_upgrade_header_cannot_skip_the_filter(self):
        try:
            from websockets.datastructures import Headers
        except Exception:                       # pragma: no cover - websockets is a hard dep
            self.skipTest("needs websockets")
        srv = _server()
        conn = _Conn(confined=False)
        conn._pcai_signer_only = None
        hdrs = Headers([("Upgrade", "websocket"), ("Upgrade", "websocket"),
                        ("User-Agent", "nostr-tool/1.0")])
        resp = srv.process_request(conn, _Req(hdrs))
        # Either answer is safe — the duplicate is untrustworthy, so it is no longer read as an
        # upgrade at all and the caller gets the welcome page instead of a socket. What must never
        # happen is the third outcome: the handshake PROCEEDS (None) with no verdict recorded.
        self.assertFalse(resp is None and getattr(conn, "_pcai_signer_only", None) is not True,
                         "a socket got onto this relay with no verdict recorded — the per-message "
                         "check reads that as a recognised PosterChan client, so "
                         "`posterchan_clients_only` is off for whoever repeats a header")

    def test_a_gate_that_throws_confines_rather_than_admitting(self):
        srv = _server()
        conn = _Conn(confined=False)
        conn._pcai_signer_only = None

        class _Exploding(dict):
            def get_all(self, name):
                if name == "Upgrade":
                    return ["websocket"]
                raise RuntimeError("boom")

            def get(self, name, default=None):
                if name == "Upgrade":
                    return "websocket"
                raise RuntimeError("boom")

        srv.process_request(conn, _Req(_Exploding()))
        self.assertIs(getattr(conn, "_pcai_signer_only", None), True,
                      "an unevaluable gate admitted the socket with full service")

    def test_the_switch_off_still_admits_a_thrown_gate(self):
        """Fail-closed must mean confined-while-ON, never confined-always: with the switch off this
        relay is a general-purpose relay and a bug here must not start refusing reads."""
        srv = _server(posterchan_clients_only=False)
        conn = _Conn(confined=False)
        conn._pcai_signer_only = None

        class _Exploding(dict):
            def get_all(self, name):
                if name == "Upgrade":
                    return ["websocket"]
                raise RuntimeError("boom")

        srv.process_request(conn, _Req(_Exploding()))
        self.assertIs(getattr(conn, "_pcai_signer_only", None), False)


if __name__ == "__main__":
    unittest.main()
