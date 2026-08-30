"""An unreachable relay must be a REFUSAL, never an empty document.

Run: venv-unified/bin/python -m pytest tests/test_doc_endpoints_refuse_an_unreachable_relay.py

`nostr_store._ws_query` answers the same empty result for "there is no such document" and for "I
could not reach the relay". Every endpoint that serves a kind-30078 document therefore has to choose
which one an empty read means, and for a REPLACEABLE document the two answers are a version of the
library and no library at all.

Where that goes wrong is not obvious from the endpoint, because the clients here are careful: the
drive index client refuses to save until a pull has SUCCEEDED. What defeats it is the server
answering `{"ok": true, "index": {}}` — app.js reads that on the branch commented "server has no
index at all — a fresh drive, safe to save", sets `_pullOk`, and the next save replaces a full drive
with an empty default. The guard was never reached, because the server told it a fresh-account
story. `scripts/restore_files_index.py` exists because of that wipe.

So this is a behavioural test, not a lint: it drives the real endpoint functions against a relay
that is DOWN, and asserts they answer 503 rather than 200-with-nothing. The fake honours `strict`
the way the real `_ws_query` does — raising only when asked to — so an endpoint that drops
`strict=True` fails here rather than quietly passing.

`tests/test_replaceable_doc_reads_are_strict.py` is the static half: it stops a NEW read-modify-write
shipping loose. This is the half that proves the answer these endpoints actually give.
"""
import asyncio
import json
import unittest
from unittest import mock

from app.routers import client as client_router
from app.routers.client import DraftsReq, FilesIndexReq, drafts_sync, files_index
from app.services import nostr_store


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


class _DeadRelay:
    """Every read fails the way an unreachable relay does: strict RAISES, loose answers empty.

    That asymmetry IS the bug under test. A fake that raised unconditionally would make a loose read
    fail too, and the test would pass against code that never asked for strict.
    """

    def __init__(self):
        self.writes = []

    async def get_doc(self, port, d, seckey=None, strict=False, encrypt=True, **kw):
        if strict:
            raise ConnectionError("relay unreachable")
        return None

    async def get_docs(self, port, d_tags, seckey=None, strict=False, **kw):
        if strict:
            raise ConnectionError("relay unreachable")
        return {}

    async def list_docs(self, port, prefix, seckey=None, strict=False, **kw):
        if strict:
            raise ConnectionError("relay unreachable")
        return {}

    async def put_doc(self, port, sk, d, payload, encrypt=True, **kw):
        self.writes.append((d, json.loads(json.dumps(payload))))
        return True


class _LiveRelay(_DeadRelay):
    """Reachable, and genuinely holding nothing — the case that must still be allowed through."""

    async def get_doc(self, port, d, seckey=None, strict=False, encrypt=True, **kw):
        return None

    async def get_docs(self, port, d_tags, seckey=None, strict=False, **kw):
        return {}

    async def list_docs(self, port, prefix, seckey=None, strict=False, **kw):
        return {}


class _Db:
    class _Q:
        def __init__(self, u):
            self._u = u

        def filter(self, *a, **k):
            return self

        def first(self):
            return self._u

    def __init__(self, u):
        self._u = u

    def query(self, *a):
        return _Db._Q(self._u)


class _User:
    id = 1
    nostr_npub = "npub1test"


class _Base(unittest.TestCase):
    RELAY = _DeadRelay

    def setUp(self):
        self.relay = self.RELAY()
        self.patches = [
            mock.patch.object(nostr_store, "get_doc", self.relay.get_doc),
            mock.patch.object(nostr_store, "get_docs", self.relay.get_docs),
            mock.patch.object(nostr_store, "list_docs", self.relay.list_docs),
            mock.patch.object(nostr_store, "put_doc", self.relay.put_doc),
            mock.patch.object(nostr_store, "user_storage_seckey", lambda db, u: b"\x01" * 32),
            mock.patch.object(client_router, "_verify_self_auth", lambda a, p: True),
            mock.patch.object(client_router.nostr_service, "to_pubkey_hex", lambda x: "f" * 64),
            mock.patch.object(client_router.nostr_service, "npub_of", lambda x: "npub1test"),
            mock.patch.object(client_router, "_setting", lambda db, k, dflt=None: "3052"),
        ]
        for p in self.patches:
            p.start()
        self.db = _Db(_User())

    def tearDown(self):
        for p in self.patches:
            p.stop()

    def drafts(self, **kw):
        r = _run(drafts_sync(DraftsReq(pubkey="f" * 64, auth="x", **kw), self.db))
        return r.status_code, json.loads(r.body)

    def index(self, **kw):
        r = _run(files_index(FilesIndexReq(pubkey="f" * 64, auth="x", **kw), self.db))
        return r.status_code, json.loads(r.body)


class WhenTheRelayIsDown(_Base):
    RELAY = _DeadRelay

    def test_saving_drafts_refuses_instead_of_replacing_them(self):
        """The read-modify-write. Merging onto an empty read publishes only THIS device's drafts
        over a replaceable document, deleting every draft the others had."""
        code, j = self.drafts(drafts=[{"id": "d1", "ts": 10, "text": "hello"}])
        self.assertEqual(503, code,
                         "the drafts save answered %s with the relay down — it merged onto an "
                         "empty read and replaced the document" % code)
        self.assertFalse(j.get("ok"))
        self.assertEqual([], self.relay.writes,
                         "it WROTE with the relay unreachable: %s" % self.relay.writes)

    def test_loading_drafts_says_it_could_not_ask(self):
        code, j = self.drafts()
        self.assertEqual(503, code)
        self.assertFalse(j.get("ok"))

    def test_loading_the_drive_index_does_not_report_a_fresh_account(self):
        """The exact shape of the wipe. `{"ok": true, "index": {}}` is what app.js reads as
        "server has no index at all — a fresh drive, safe to save"."""
        code, j = self.index()
        self.assertEqual(503, code,
                         "the drive index answered %s with the relay down; the client reads an ok "
                         "empty answer as a fresh account and permits the save that overwrites it"
                         % code)
        self.assertFalse(j.get("ok"))
        self.assertNotIn("index", j)

    def test_saving_the_drive_index_refuses(self):
        """Already guarded before this file existed — asserted here so the set is complete and a
        regression in one endpoint cannot hide behind the others."""
        code, j = self.index(index={"files": {}, "folders": []})
        self.assertEqual(503, code)
        self.assertEqual([], self.relay.writes)


class WhenTheRelayIsUpAndEmpty(_Base):
    """The other half, and the reason this cannot just be "always 503": a genuinely new account
    must still be able to read and to make its first save. A fix that refuses here would break
    every first-time user, which is a worse failure than the one being prevented."""

    RELAY = _LiveRelay

    def test_a_new_account_can_read_drafts(self):
        code, j = self.drafts()
        self.assertEqual(200, code)
        self.assertTrue(j["ok"])
        self.assertEqual([], j["drafts"])

    def test_a_new_account_can_save_its_first_draft(self):
        code, j = self.drafts(drafts=[{"id": "d1", "ts": 10, "text": "hello"}])
        self.assertEqual(200, code)
        self.assertTrue(j["ok"])
        self.assertEqual(1, len(self.relay.writes))
        self.assertEqual("pcai:drafts", self.relay.writes[0][0])

    def test_a_new_account_reads_an_empty_drive_index(self):
        code, j = self.index()
        self.assertEqual(200, code)
        self.assertTrue(j["ok"])
        self.assertEqual({}, j["index"])


if __name__ == "__main__":
    unittest.main()
