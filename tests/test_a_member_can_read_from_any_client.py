"""With "Allow PosterChan clients only" ON, a MEMBER of this instance can still read the relay from
another app by signing in (NIP-42); strangers stay refused.

Reported: "amethyst checks your relays. I get 0 events from it". The switch admitted Amethyst,
refused every read with a NOTICE Amethyst never acts on, and closed the socket the moment it asked
for content — before it could answer the AUTH challenge sent at connect. Now a refusal says
`auth-required:` (the NIP-42 prefix clients act on), a socket gets AUTH_GRACE seconds to sign in, and
an AUTH as a member lifts the confinement.
"""
import asyncio
import json
import time
import unittest

from app.services.nostr.event import build_event
from app.services.nostr_relay import server as S

MEMBER_SK = bytes.fromhex("31" * 32)
STRANGER_SK = bytes.fromhex("32" * 32)


class _Conn:
    def __init__(self, opened_ago=0.0):
        self.remote_address = ("93.184.216.34", 54321)
        self._pcai_signer_only = True
        self._pcai_ip = "93.184.216.34"
        self._pcai_opened = time.time() - opened_ago
        self.closed = None

    async def close(self, code=1000, reason=""):
        self.closed = (code, reason)


def _server(member_pk):
    srv = S.RelayServer.__new__(S.RelayServer)
    srv.cfg = {"posterchan_clients_only": True, "max_message_size": 262144,
               "nip05": {"names": {"alice": member_pk}}, "preserve": []}
    srv._refused_at = {}
    srv.sent = []
    srv._send = lambda conn, msg: srv.sent.append(msg)
    srv._auth_challenges, srv._auth_pubkeys, srv._relay_urls = {}, {}, {}
    srv.served = []

    async def _on_req(conn, sid, filters):
        srv.served.append(sid)
    srv._on_req = _on_req

    async def _noop(*a, **kw):
        return None
    srv._on_event = _noop
    return srv


def _auth(srv, conn, sk):
    srv._auth_challenges[conn] = "chal"
    srv._relay_urls[conn] = ""
    ev = build_event(sk, 22242, "", [["challenge", "chal"], ["relay", "wss://poster.place/relay"]])
    srv._on_auth(conn, ev)
    return ev["pubkey"]


def _run(srv, conn, msg):
    async def go():
        await srv._dispatch(conn, json.dumps(msg))
        await asyncio.sleep(0.4)
    asyncio.run(go())


class AMemberCanReadFromAnyClient(unittest.TestCase):

    def setUp(self):
        self.member_pk = build_event(MEMBER_SK, 1, "", [])["pubkey"]

    def test_a_refused_read_says_auth_required(self):
        srv, conn = _server(self.member_pk), _Conn()
        _run(srv, conn, ["REQ", "feed", {"kinds": [1], "limit": 20}])
        closed = [m for m in srv.sent if m[0] == "CLOSED"]
        self.assertTrue(closed and closed[0][1] == "feed" and closed[0][2].startswith("auth-required:"),
                        f"Amethyst only signs in when told auth-required: {srv.sent}")
        self.assertIsNone(conn.closed, "a fresh socket was closed before it could answer the AUTH challenge")

    def test_a_member_who_signs_in_reads_everything(self):
        srv, conn = _server(self.member_pk), _Conn()
        _auth(srv, conn, MEMBER_SK)
        self.assertFalse(conn._pcai_signer_only, "a member's AUTH did not lift the confinement")
        _run(srv, conn, ["REQ", "feed", {"kinds": [1], "limit": 20}])
        self.assertEqual(srv.served, ["feed"], "a signed-in member still got nothing")
        self.assertIsNone(conn.closed)

    def test_a_stranger_who_signs_in_is_still_a_stranger(self):
        srv, conn = _server(self.member_pk), _Conn()
        _auth(srv, conn, STRANGER_SK)
        self.assertTrue(conn._pcai_signer_only, "any key that signs the challenge got in")
        _run(srv, conn, ["REQ", "feed", {"kinds": [1], "limit": 20}])
        self.assertEqual(srv.served, [], "a stranger read the relay by signing any AUTH")

    def test_an_old_confined_socket_is_still_closed_early(self):
        srv, conn = _server(self.member_pk), _Conn(opened_ago=60)
        _run(srv, conn, ["REQ", "feed", {"kinds": [1], "limit": 20}])
        self.assertIsNotNone(conn.closed, "the connection-count fix regressed: old refused sockets stay open")


if __name__ == "__main__":
    unittest.main()
