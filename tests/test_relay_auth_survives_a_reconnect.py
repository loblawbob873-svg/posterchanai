"""A client that reconnects and answers NIP-42 with the challenge of the socket it just closed is signed in.

Measured on poster.place 2026-10-18: ~3,000 `AUTH refused (invalid: AUTH challenge does not match)` an hour,
every one "the challenge of a socket closed 7s ago", all from ONE address holding ~98 keys, in bursts of ~98 a
second every few minutes. The client reconnects and answers with the challenge it already holds for this relay
URL before it reads the new one. Each of those keys then had no authenticated socket: no NIP-78 docs, no private
reads, and a private repo announcement that "this node's relay hasn't answered".

The challenge is what stops a captured AUTH event being replayed. A challenge THIS relay issued, to a socket from
the SAME address, closed moments ago, with a fresh signature, gives an attacker nothing (they would need the
signed event, which only ever travelled over that client's TLS). So it is accepted -- once per AUTH event, inside
a short window, from the same address only. Everything else is still refused, and still says why.

Driven against a REAL relay over real websockets (tests/os_relay_harness.Instance), because every individual
check here passes against a stubbed `_on_auth` and the bug only exists across two sockets.
"""
import asyncio
import json
import tempfile
import time
import unittest

from tests.os_relay_harness import Instance, OWNER_SK
from app.services.nostr.event import build_event


async def _open(url, ip):
    from websockets.asyncio.client import connect
    ws = await connect(url, additional_headers={"X-Real-IP": ip}, open_timeout=10)
    first = json.loads(await asyncio.wait_for(ws.recv(), 5))
    assert first[0] == "AUTH", first
    return ws, first[1]


async def _auth(ws, url, challenge, sk=OWNER_SK, ev=None):
    ev = ev or build_event(sk, 22242, "", [["relay", url], ["challenge", challenge]])
    await ws.send(json.dumps(["AUTH", ev]))
    while True:
        m = json.loads(await asyncio.wait_for(ws.recv(), 5))
        if m[0] == "OK" and m[1] == ev["id"]:
            return m[2], m[3], ev


class AuthSurvivesAReconnect(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.inst = Instance(cls.tmp.name).start()

    @classmethod
    def tearDownClass(cls):
        cls.inst.close()
        cls.tmp.cleanup()

    def run_(self, coro):
        return asyncio.run(coro)

    def test_the_previous_sockets_challenge_signs_in_after_a_reconnect(self):
        url = self.inst.relay_url

        async def go():
            a, old = await _open(url, "203.0.113.7")
            await a.close()
            await asyncio.sleep(0.3)                     # the relay's handler has run its `finally`
            b, new = await _open(url, "203.0.113.7")
            self.assertNotEqual(old, new)
            ok, why, _ = await _auth(b, url, old)
            await b.close()
            return ok, why
        ok, why = self.run_(go())
        self.assertTrue(ok, "a client answering with the challenge of the socket it just closed was refused: %r" % why)

    def test_the_same_auth_event_is_not_accepted_twice(self):
        url = self.inst.relay_url

        async def go():
            a, old = await _open(url, "203.0.113.8")
            await a.close()
            await asyncio.sleep(0.3)
            b, _ = await _open(url, "203.0.113.8")
            ok1, _, ev = await _auth(b, url, old)
            c, _ = await _open(url, "203.0.113.8")
            ok2, why2, _ = await _auth(c, url, old, ev=ev)
            await b.close(); await c.close()
            return ok1, ok2, why2
        ok1, ok2, why2 = self.run_(go())
        self.assertTrue(ok1)
        self.assertFalse(ok2, "one signed AUTH for a closed socket's challenge signed in TWO sockets")
        self.assertIn("challenge does not match", why2)

    def test_a_closed_sockets_challenge_does_not_work_from_another_address(self):
        url = self.inst.relay_url

        async def go():
            a, old = await _open(url, "203.0.113.9")
            await a.close()
            await asyncio.sleep(0.3)
            b, _ = await _open(url, "198.51.100.20")
            ok, why, _ = await _auth(b, url, old)
            await b.close()
            return ok, why
        ok, why = self.run_(go())
        self.assertFalse(ok, "a closed socket's challenge signed in a DIFFERENT address")
        self.assertIn("challenge does not match", why)

    def test_an_open_sockets_challenge_and_an_invented_one_are_still_refused(self):
        url = self.inst.relay_url

        async def go():
            a, chal_a = await _open(url, "203.0.113.10")
            b, _ = await _open(url, "203.0.113.10")
            ok1, _, _ = await _auth(b, url, chal_a)          # a's challenge, a still open
            ok2, _, _ = await _auth(b, url, "never-issued")
            await a.close(); await b.close()
            return ok1, ok2
        ok1, ok2 = self.run_(go())
        self.assertFalse(ok1, "another OPEN socket's challenge was accepted")
        self.assertFalse(ok2, "a challenge this relay never issued was accepted")

    def test_an_old_closed_challenge_is_refused(self):
        url = self.inst.relay_url
        srv = self.inst.server

        async def go():
            a, old = await _open(url, "203.0.113.11")
            await a.close()
            await asyncio.sleep(0.3)
            # Age it past the window rather than sleeping it out.
            for k, v in list(srv._past_challenges.items()):
                if k == old:
                    srv._past_challenges[k] = (v[0] - srv.PAST_CHALLENGE_S - 1,) + tuple(v[1:])
            b, _ = await _open(url, "203.0.113.11")
            ok, _, _ = await _auth(b, url, old)
            await b.close()
            return ok
        self.assertFalse(self.run_(go()), "a challenge from a socket closed long ago still signs in")


if __name__ == "__main__":
    unittest.main()


class ConfinedSocketsSayWhatTheyAre(unittest.TestCase):
    """The refusals above were attributable to an ADDRESS and nothing else. A confined socket's close line now
    names the software family (Origin scheme+host, User-Agent product) and what it was refused -- types and kinds,
    never content."""

    def test_the_family_names_software_not_people(self):
        from app.services.nostr_relay import server as S
        from websockets.datastructures import Headers
        h = Headers({"Origin": "https://example.app/some/path?u=alice", "User-Agent": "okhttp/4.12.0"})
        self.assertEqual(S._client_family(h), "origin=https://example.app ua=okhttp")
        self.assertEqual(S._client_family(Headers({})), "origin=none ua=none")
        self.assertIn("ua=android-webview", S._client_family(Headers({
            "User-Agent": "Mozilla/5.0 (Linux; Android 14; wv) AppleWebKit/537.36 Chrome/131 Mobile Safari/537.36"})))

    def test_the_summary_tallies_refusals_by_kind(self):
        from app.services.nostr_relay import server as S

        class C:
            pass
        c = C()
        c._pcai_client = "origin=none ua=okhttp"
        S._note_confined_refusal(c, "REQ", ["REQ", "s", {"kinds": [1, 7], "#p": ["x" * 64]}])
        S._note_confined_refusal(c, "REQ", ["REQ", "t", {"kinds": [7, 1]}])
        S._note_confined_refusal(c, "EVENT", ["EVENT", {"kind": 30617, "content": "SECRET"}])
        line = S._confined_summary(c)
        self.assertIn("REQ:1,7x2", line)
        self.assertIn("EVENT:30617x1", line)
        self.assertNotIn("SECRET", line)
        self.assertEqual(S._confined_summary(C()), "", "an unconfined socket's line must not change")
