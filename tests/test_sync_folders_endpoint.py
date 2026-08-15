"""/client/sync-folders — the account-level list of synced folders.

Run: venv-unified/bin/python -m unittest tests.test_sync_folders_endpoint

A folder pair lives on the devices that sync it, in each one's own localStorage, so nothing
enumerated them: a device that does not sync "Documents" had no way to learn Documents exists. This
endpoint reads the manifests' own d-tags (`pcai:sync:<key>`) rather than keeping a second list —
an index of folders would be one more replaceable document that a single empty read could wipe, and
this feature has already paid that bill twice.

The cases below are the ways this endpoint can be quietly wrong:

  * an unreadable relay answering like an empty account. `_ws_query` returns [] for BOTH "no
    documents" and "I could not ask", and the client draws the first as an empty sidebar. Telling
    someone their synced folders are gone because a socket blinked is the reading to avoid, so a
    failed read is a 503 and never {"folders": []}.
  * counting a manifest written before the paths were sealed. Those documents keep their paths in
    the clear and carry no `n`, so the count has to fall back to counting them — and must not count
    tombstones, which are entries with a deletedAt and no bytes behind them.
  * a folder key that escapes its namespace. The d-tag keyspace is SHARED with notes, calendars,
    contacts and the files index; the prefix is what keeps a folder from addressing one of them.
"""
import asyncio
import unittest
from unittest import mock

from app.routers import client as C


class _FakeUser:
    id = 1
    nostr_npub = "npub1fake"


class _FakeDB:
    def __init__(self, user=None):
        self._user = user

    def query(self, *a, **k):
        return self

    def filter(self, *a, **k):
        return self

    def first(self):
        return self._user


def _call(docs=None, raises=None):
    """Drive sync_folders with the relay and the account stubbed out.

    The endpoint does `from app.services import nostr_store as store` at call time, so the real
    module's attributes are patched — swapping sys.modules would not be seen, because the name is
    resolved off the already-imported package.
    """
    import json
    from app.services import nostr_store as ns

    req = C.SyncFoldersReq(pubkey="a" * 64, auth="x")
    listing = (mock.AsyncMock(side_effect=raises) if raises is not None
               else mock.AsyncMock(return_value=docs or {}))
    with mock.patch.object(ns, "list_docs", listing), \
            mock.patch.object(ns, "user_storage_seckey", lambda db, u: b"\x01" * 32), \
            mock.patch.object(C.nostr_service, "to_pubkey_hex", lambda p: "a" * 64), \
            mock.patch.object(C.nostr_service, "npub_of", lambda p: "npub1fake"), \
            mock.patch.object(C, "_verify_self_auth", lambda a, p: True), \
            mock.patch.object(C, "_setting", lambda db, k, d=None: "3052"):
        resp = asyncio.run(C.sync_folders(req, db=_FakeDB(_FakeUser())))
        # strict=True is what makes the 503 below possible at all: without it an unreachable relay
        # answers {} and this endpoint reports an empty account.
        if raises is None:
            assert listing.await_args.kwargs.get("strict") is True, \
                "the folder list must be read strictly, or a dead relay reads as 'no folders'"
    return resp.status_code, json.loads(bytes(resp.body))


class TestSyncFolders(unittest.TestCase):
    def test_lists_the_pair_keys_with_their_counts(self):
        status, body = _call(docs={
            "pcai:sync:Documents": ({"n": 412, "sealed": "…"}, 1786000000),
            "pcai:sync:Pictures": ({"n": 8213, "sealed": "…"}, 1786000100),
        })
        self.assertEqual(status, 200)
        self.assertEqual([f["key"] for f in body["folders"]], ["Documents", "Pictures"])
        self.assertEqual([f["n"] for f in body["folders"]], [412, 8213])
        self.assertEqual(body["folders"][0]["updated_at"], 1786000000)

    def test_an_unreadable_relay_is_never_an_empty_account(self):
        """The one that matters. [] and "I could not ask" must not look the same to the client."""
        status, body = _call(raises=RuntimeError("relay unreachable"))
        self.assertEqual(status, 503, "a failed read reported as success would draw an empty "
                                      "sidebar and read as 'my synced folders are gone'")
        self.assertFalse(body.get("ok"))
        self.assertNotIn("folders", body)

    def test_a_pre_seal_manifest_is_counted_not_dropped(self):
        """Manifests written before the paths were sealed carry no `n`; they are still readable and
        still real folders, and a tombstone in one is not a file."""
        status, body = _call(docs={
            "pcai:sync:Notes": ({"paths": {"a.txt": {"sha": "1"}, "b.txt": {"sha": "2"},
                                           "gone.txt": {"deletedAt": 123}}}, 1780000000),
        })
        self.assertEqual(status, 200)
        self.assertEqual(body["folders"][0], {"key": "Notes", "n": 2, "updated_at": 1780000000})

    def test_the_folder_key_cannot_address_another_document(self):
        """The d-tag namespace is shared with notes, calendars, contacts and the files index."""
        for evil in ["pcai:note:abcd", "../files-index", "*", "a:b", "..", "abc", "z" * 80]:
            key = C._sync_folder_key(evil)
            self.assertTrue(key is None or key.startswith("pcai:sync:"), evil)

    def test_no_account_is_an_empty_list_not_an_error(self):
        req = C.SyncFoldersReq(pubkey="a" * 64, auth="x")
        with mock.patch.object(C.nostr_service, "to_pubkey_hex", lambda p: "a" * 64), \
                mock.patch.object(C.nostr_service, "npub_of", lambda p: "npub1fake"), \
                mock.patch.object(C, "_verify_self_auth", lambda a, p: True):
            resp = asyncio.run(C.sync_folders(req, db=_FakeDB(None)))
        import json
        self.assertEqual(json.loads(bytes(resp.body)), {"ok": True, "folders": []})

    def test_ownership_is_proven_before_anything_is_read(self):
        req = C.SyncFoldersReq(pubkey="a" * 64, auth="x")
        with mock.patch.object(C.nostr_service, "to_pubkey_hex", lambda p: "a" * 64), \
                mock.patch.object(C, "_verify_self_auth", lambda a, p: False):
            resp = asyncio.run(C.sync_folders(req, db=_FakeDB(_FakeUser())))
        self.assertEqual(resp.status_code, 403)


class TestSyncManifestUnknownAccount(unittest.TestCase):
    """/client/sync-manifest handing out an EMPTY manifest as a success.

    This endpoint exists for one reason — an empty read written back over a full manifest does not
    lose a setting, it loses the folder, and every device then reads the missing paths as "deleted
    elsewhere" and moves its local copies to the trash. Every other exit was already careful about
    that: an unreadable relay is a 503, a refused shrink is a 409.

    The account lookup was not. A signed request whose npub has no `User` row answered
    {"ok": true, "manifest": {}} — the wipe itself, wearing a 200. It is reachable by a device that
    has been syncing for months: a restore onto a fresh node, an instance re-pointed at a different
    server, an npub that changed. "I do not know who you are" is a refusal, never an empty folder.
    """

    def _call(self, user):
        import json
        req = C.SyncManifestReq(pubkey="a" * 64, auth="x", folder="Pictures")
        with mock.patch.object(C.nostr_service, "to_pubkey_hex", lambda p: "a" * 64), \
                mock.patch.object(C.nostr_service, "npub_of", lambda p: "npub1fake"), \
                mock.patch.object(C, "_verify_self_auth", lambda a, p: True):
            resp = asyncio.run(C.sync_manifest(req, db=_FakeDB(user)))
        return resp.status_code, json.loads(bytes(resp.body))

    def test_an_unknown_account_is_refused_never_an_empty_manifest(self):
        status, body = self._call(None)
        self.assertEqual(status, 403)
        self.assertFalse(body.get("ok"))
        self.assertNotIn("manifest", body,
                         "an empty manifest handed out as a success is the folder wipe this "
                         "endpoint exists to prevent")


class TestSupersededManifestBlobs(unittest.TestCase):
    """Every manifest save past ~45 KB uploads a whole new encrypted blob, so a first sync of a big
    folder leaves one per checkpoint and every later change leaves another. They are `keep` blobs,
    and the cleanup sweep skips those unconditionally — that exemption is what stops an admin turning
    on a TTL from eating an encrypted drive — so nothing else was ever going to collect them.

    The document therefore carries a one-deep chain (`pathsSha` + `prevSha`) and the server releases
    the generation behind it, ownership-checked. The check is the load-bearing part: the sha comes
    out of a document the client wrote, so without it a crafted manifest would have this node delete
    somebody else's bytes.
    """

    def test_an_explicit_expiry_is_honoured_on_a_keep_blob(self):
        """The whole mechanism rests on this. `keep` blobs are exempt from the admin's blanket age
        TTL — that exemption is what stops turning the setting on from eating an encrypted drive —
        but an expiry stamped one blob at a time, by code that proved those bytes are referenced by
        nothing, has to actually fire. While it did not, every superseded index and manifest leaked
        for ever while the code looked like it was reclaiming them."""
        import inspect

        from app.services import blossom_service

        src = " ".join(inspect.getsource(blossom_service._cleanup_once).split())
        self.assertIn("conds = [explicit]", src,
                      "an explicit per-blob expiry must apply to every blob")
        self.assertIn("and_(BlossomBlob.keep.is_(False), BlossomBlob.created_at", src,
                      "the ADMIN's age rule must still never touch a keep blob")

    def test_release_is_gated_on_ownership(self):
        """A crafted manifest naming somebody else's sha must not delete their bytes."""
        from app.services import blossom_service as bs

        seen = {}

        def _owner(db, sha, pk):
            seen["asked"] = True
            return False

        def _release(*a, **k):
            raise AssertionError("released a blob this user does not own")

        with mock.patch.object(bs, "is_owner", _owner), \
                mock.patch.object(bs, "release_owner", _release):
            asyncio.run(C._release_sync_blob(_FakeDB(), "a" * 64, "b" * 64))
        self.assertTrue(seen.get("asked"), "ownership was never checked")

    def test_the_bytes_go_only_when_the_last_owner_releases(self):
        """Blossom dedups, so one set of bytes can be referenced by several accounts."""
        from app.services import blossom_service as bs

        expired = mock.MagicMock()
        with mock.patch.object(bs, "is_owner", lambda *a: True), \
                mock.patch.object(bs, "release_owner", lambda *a: 1), \
                mock.patch.object(bs, "expire_blob_in", expired):
            asyncio.run(C._release_sync_blob(mock.MagicMock(), "a" * 64, "b" * 64))
        expired.assert_not_called()

    def test_the_last_owner_leaves_a_week_of_grace(self):
        """When nobody else references them the bytes go — but on a TTL, not on the spot, so letting
        go of the wrong thing is recoverable for a week rather than instantly final."""
        from app.services import blossom_service as bs

        got = {}
        with mock.patch.object(bs, "is_owner", lambda *a: True), \
                mock.patch.object(bs, "release_owner", lambda *a: 0), \
                mock.patch.object(bs, "expire_blob_in",
                                  lambda db, sha, days: got.update(sha=sha, days=days)):
            asyncio.run(C._release_sync_blob(mock.MagicMock(), "a" * 64, "b" * 64))
        self.assertEqual(got.get("sha"), "a" * 64)
        self.assertGreaterEqual(got.get("days", 0), 1, "no recovery window at all")

    def test_a_failure_never_raises_into_the_save(self):
        """Collecting a superseded blob is housekeeping — it must never turn a stored manifest into
        a failed one."""
        from app.services import blossom_service as bs

        def _boom(*a, **k):
            raise RuntimeError("db gone")

        with mock.patch.object(bs, "is_owner", _boom):
            asyncio.run(C._release_sync_blob(mock.MagicMock(), "a" * 64, "b" * 64))   # must not raise


if __name__ == "__main__":
    unittest.main()
