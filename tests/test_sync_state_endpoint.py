"""Folder sync v3's server half — /client/sync-state — against an in-memory relay.

The server is the only choke point every client build passes through, so the rules that stop a
broken client from destroying a folder live HERE and are tested HERE: per-file compare-and-swap
under the pair lock, the era that makes a retired folder's records unspeakable, the tombstone
backstop, and paged listing that cannot silently truncate a 12,000-file folder.
"""
import asyncio
import json
import time
import unittest
from unittest import mock

from app.routers import client as client_router
from app.routers.client import SyncStateReq, sync_state, _fs_list_all
from app.services import nostr_store


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


class _FakeRelay:
    """put_doc/get_doc/get_docs/list_docs over a dict, honouring since/until/limit like the relay."""

    def __init__(self):
        self.docs = {}          # d_tag -> (payload dict, created_at)
        self.clock = 1_000_000

    async def put_doc(self, port, sk, d, payload, encrypt=True, **kw):
        self.clock += 1
        self.docs[d] = (json.loads(json.dumps(payload)), self.clock)
        return True

    async def get_doc(self, port, d, seckey=None, strict=False, encrypt=True, **kw):
        got = self.docs.get(d)
        return json.loads(json.dumps(got[0])) if got else None

    async def get_docs(self, port, d_tags, seckey=None, strict=False, encrypt=True, **kw):
        out = {}
        for d in d_tags:
            got = self.docs.get(d)
            if got:
                out[d] = json.loads(json.dumps(got[0]))
        return out

    async def list_docs(self, port, prefix, seckey=None, strict=False, encrypt=True,
                        with_meta=False, until=None, since=None, limit=5000, **kw):
        rows = [(d, doc, at) for d, (doc, at) in self.docs.items() if d.startswith(prefix)]
        if until:
            rows = [r for r in rows if r[2] <= until]
        if since:
            rows = [r for r in rows if r[2] >= since]
        rows.sort(key=lambda r: -r[2])
        rows = rows[:limit]
        return {d: (json.loads(json.dumps(doc)), at) for d, doc, at in rows}


class _Db:
    class _Q:
        def __init__(self, user):
            self._u = user

        def filter(self, *a, **k):
            return self

        def first(self):
            return self._u

    def __init__(self, user):
        self._u = user

    def query(self, *a):
        return _Db._Q(self._u)


class _User:
    id = 1
    nostr_npub = "npub1test"


D1 = "a" * 24
D2 = "b" * 24


class SyncStateEndpoint(unittest.TestCase):
    def setUp(self):
        self.relay = _FakeRelay()
        self.patches = [
            mock.patch.object(nostr_store, "put_doc", self.relay.put_doc),
            mock.patch.object(nostr_store, "get_doc", self.relay.get_doc),
            mock.patch.object(nostr_store, "get_docs", self.relay.get_docs),
            mock.patch.object(nostr_store, "list_docs", self.relay.list_docs),
            mock.patch.object(nostr_store, "user_storage_seckey", lambda db, u: b"\x01" * 32),
            mock.patch.object(client_router, "_verify_self_auth", lambda a, p: True),
            mock.patch.object(client_router.nostr_service, "to_pubkey_hex", lambda x: "f" * 64),
            mock.patch.object(client_router.nostr_service, "npub_of", lambda x: "npub1test"),
            mock.patch.object(client_router, "_setting", lambda db, k, dflt=None: "3052"),
        ]
        for p in self.patches:
            p.start()
        client_router._fs_locks.clear()
        client_router._fs_tomb_log.clear()
        self.db = _Db(_User())

    def tearDown(self):
        for p in self.patches:
            p.stop()

    def call(self, **kw):
        req = SyncStateReq(pubkey="f" * 64, auth="x", pair=kw.pop("pair", "TestPair"), **kw)
        resp = _run(sync_state(req, self.db))
        return resp.status_code, json.loads(resp.body)

    def rec(self, d, v, t=0, by="dev1"):
        r = {"d": d, "v": v, "by": by, "ct": "ct-" + d[:4] + "-" + str(v)}
        if t:
            r["t"] = 1
        return r

    # ---- CAS ------------------------------------------------------------------------------------
    def test_put_then_stale_then_newer(self):
        code, j = self.call(put=[self.rec(D1, 1)], era=0)
        self.assertEqual(code, 200)
        self.assertTrue(j["results"][0].get("ok"))
        self.assertEqual(j["n"], 1)

        # The same version again is REFUSED — the loser of a race must never overwrite the winner.
        code, j = self.call(put=[self.rec(D1, 1, by="dev2")], era=0)
        self.assertEqual(code, 200)
        self.assertTrue(j["results"][0].get("stale"))
        env = self.relay.docs["pcai:fs:TestPair:" + D1][0]
        self.assertEqual(env["by"], "dev1", "a stale write must not land")

        # Strictly newer wins.
        code, j = self.call(put=[self.rec(D1, 2, by="dev2")], era=0)
        self.assertTrue(j["results"][0].get("ok"))
        self.assertEqual(self.relay.docs["pcai:fs:TestPair:" + D1][0]["by"], "dev2")

    def test_tombstone_counts_down_not_out(self):
        self.call(put=[self.rec(D1, 1), self.rec(D2, 1)], era=0)
        code, j = self.call(put=[self.rec(D1, 2, t=1)], era=0)
        self.assertEqual(j["n"], 1, "a tombstone decrements the live count")
        # A tombstone is a RECORD — it must come back from a list, not vanish.
        code, j = self.call()
        tomb = [r for r in j["records"] if r["d"] == D1]
        self.assertEqual(len(tomb), 1)
        self.assertEqual(tomb[0].get("t"), 1)

    # ---- era ------------------------------------------------------------------------------------
    def test_forget_all_is_one_write_and_kills_the_world(self):
        self.call(put=[self.rec(D1, 5)], era=0)
        code, j = self.call(forgetAll=True)
        self.assertEqual(j["era"], 1)
        # The record row still exists, but the new world cannot see it…
        code, j = self.call()
        self.assertEqual(j["records"], [])
        self.assertEqual(j["era"], 1)
        # …a put from the old world is refused with the new era…
        code, j = self.call(put=[self.rec(D1, 6)], era=0)
        self.assertEqual(code, 409)
        self.assertTrue(j.get("eraChanged"))
        # …and a fresh join starts at v1 with no ghost of v5.
        code, j = self.call(put=[self.rec(D1, 1)], era=1)
        self.assertTrue(j["results"][0].get("ok"))

    # ---- backstop -------------------------------------------------------------------------------
    def test_mass_tombstone_backstop(self):
        recs = [self.rec(("%024x" % i), 1) for i in range(120)]
        self.call(put=recs[:500], era=0)
        tombs = [self.rec(("%024x" % i), 2, t=1) for i in range(120)]
        code, j = self.call(put=tombs, era=0)
        self.assertEqual(code, 409)
        self.assertTrue(j.get("backstop"))
        for i in range(120):
            self.assertNotIn("t", self.relay.docs["pcai:fs:TestPair:" + ("%024x" % i)][0],
                             "a refused batch must land NOTHING")
        # The deliberate flow passes.
        code, j = self.call(put=tombs, era=0, confirmed=True)
        self.assertEqual(code, 200)

    def test_a_restore_is_never_blocked_by_the_memory_of_a_delete(self):
        """'this would tell every device to delete 0 files' — the rolling deletion counter gated
        EVERY write, so the account-wide RESTORE was refused minutes after the delete it undoes.
        A batch with zero tombstones passes whatever the counter remembers."""
        recs = [self.rec(("%024x" % i), 1) for i in range(120)]
        self.call(put=recs, era=0)
        tombs = [self.rec(("%024x" % i), 2, t=1) for i in range(120)]
        self.call(put=tombs, era=0, confirmed=True)
        restore = [self.rec(("%024x" % i), 3) for i in range(120)]
        code, j = self.call(put=restore, era=0)
        self.assertEqual(code, 200, j)
        self.assertEqual(sum(1 for x in j["results"] if x.get("ok")), 120)

    def test_backstop_rolls_across_batches(self):
        recs = [self.rec(("%024x" % i), 1) for i in range(120)]
        self.call(put=recs, era=0)
        a = [self.rec(("%024x" % i), 2, t=1) for i in range(60)]
        b = [self.rec(("%024x" % i), 2, t=1) for i in range(60, 120)]
        code, _ = self.call(put=a, era=0)
        self.assertEqual(code, 200)
        code, j = self.call(put=b, era=0)
        self.assertEqual(code, 409, "batch-splitting must not walk around the cap")

    # ---- list / delta ---------------------------------------------------------------------------
    def test_delta_list(self):
        self.call(put=[self.rec(D1, 1)], era=0)
        code, j = self.call()
        self.assertTrue(j["full"])
        cursor = self.relay.docs["pcai:fs:TestPair:" + D1][1]
        self.call(put=[self.rec(D2, 1)], era=0)
        code, j = self.call(since=cursor + 1, era=0)
        self.assertFalse(j["full"])
        self.assertEqual([r["d"] for r in j["records"]], [D2], "delta returns only the news")
        # A stale era can never take the delta shortcut — it must see the whole (new) world.
        code, j = self.call(since=cursor + 1, era=99)
        self.assertTrue(j["full"])

    def test_paged_listing_survives_a_big_folder(self):
        relay, sk = self.relay, b"\x01" * 32
        for i in range(12000):
            relay.docs["pcai:fs:Big:%024x" % i] = ({"v": 1, "by": "x", "era": 0, "ct": "c"},
                                                   1_000_000 + i)
        got = _run(_fs_list_all(3052, sk, "pcai:fs:Big:"))
        self.assertEqual(len(got), 12000, "a folder past the 5000-doc window must not truncate")

    # ---- flags ----------------------------------------------------------------------------------
    def test_flag_marks_without_touching_version(self):
        self.call(put=[self.rec(D1, 3)], era=0)
        code, j = self.call(flag=[{"d": D1, "bad": "blob-abc"}], era=0)
        self.assertEqual(j["flagged"], 1)
        env = self.relay.docs["pcai:fs:TestPair:" + D1][0]
        self.assertEqual(env["bad"], "blob-abc")
        self.assertEqual(env["v"], 3, "a flag is an annotation, never a version")
        # A holder's re-send at v4 clears it (a fresh envelope carries no flag).
        self.call(put=[self.rec(D1, 4)], era=0)
        self.assertNotIn("bad", self.relay.docs["pcai:fs:TestPair:" + D1][0])

    # ---- refusals -------------------------------------------------------------------------------
    def test_no_account_is_a_refusal_not_an_empty_folder(self):
        db = _Db(None)
        req = SyncStateReq(pubkey="f" * 64, auth="x", pair="TestPair")
        resp = _run(sync_state(req, db))
        self.assertEqual(resp.status_code, 403)

    def test_malformed_records_are_refused(self):
        for bad in ([{"d": "ZZZ", "v": 1, "by": "d", "ct": "c"}],
                    [{"d": D1, "v": 0, "by": "d", "ct": "c"}],
                    [{"d": D1, "v": 1, "by": "d", "ct": ""}]):
            code, _ = self.call(put=bad, era=0)
            self.assertEqual(code, 400)

    def test_a_device_token_replaces_the_signer(self):
        """'if I have to wake up the signer, that is a problem' — one signed call mints a token;
        every later call authenticates with it and never consults the signer. A bad token is a 401
        naming tokenInvalid, so the client knows to sign exactly once more."""
        req = SyncStateReq(pubkey="f" * 64, auth="signed", pair="mint", mintToken=True)
        resp = _run(sync_state(req, self.db))
        j = json.loads(resp.body)
        self.assertTrue(j.get("ok") and j.get("token"))
        tok = j["token"]
        # The token authenticates a real call with NO auth at all.
        with mock.patch.object(client_router, "_verify_self_auth",
                               side_effect=AssertionError("the signer was consulted")):
            req2 = SyncStateReq(pubkey="f" * 64, token=tok, pair="TestPair",
                                put=[{"d": D1, "v": 1, "by": "dev", "ct": "c"}])
            resp2 = _run(sync_state(req2, self.db))
        self.assertEqual(resp2.status_code, 200)
        # A wrong token is refused BY NAME, never treated as an empty anything.
        req3 = SyncStateReq(pubkey="f" * 64, token="nope", pair="TestPair")
        resp3 = _run(sync_state(req3, self.db))
        self.assertEqual(resp3.status_code, 401)
        self.assertTrue(json.loads(resp3.body).get("tokenInvalid"))

    def test_pairs_listing(self):
        self.call(put=[self.rec(D1, 1)], era=0, pair="Pictures")
        self.call(put=[self.rec(D1, 1)], era=0, pair="Documents")
        self.call(forgetAll=True, pair="Documents")
        code, j = self.call(pairs=True)
        keys = [f["key"] for f in j["folders"]]
        self.assertIn("Pictures", keys)
        self.assertNotIn("Documents", keys, "a retired pair leaves the list")


if __name__ == "__main__":
    unittest.main()
