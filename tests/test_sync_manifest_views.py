"""/client/sync-manifest — the three branches the rewrite added, RUN rather than read.

Run: venv-unified/bin/python -m unittest tests.test_sync_manifest_views

The folder is no longer ONE document. Each device publishes `pcai:sync:<pair>:<device>` and the
folder is the merge of all of them, which is what makes an empty read survivable: absence is no
longer a deletion, because a deletion has to be a positive tombstone in some device's own view.

That moves the danger into this endpoint, and specifically into three new branches:

  * `views`       hands back EVERY device's document. A device that cannot be READ must be counted
                  as unreadable, never omitted — omitted, it merges as "that device holds nothing",
                  which is the empty-read wipe wearing a new hat.
  * `forgetDevice` retires ONE device's record. It is a write, so the name it writes to has to be
                  sanitised: the d-tag namespace is shared with notes, calendars, contacts and the
                  files index.
  * `forgetAll`   retires every device's record for a pair — and must not reach a DIFFERENT folder
                  whose name merely starts the same way ("Pics" vs "Pictures2"), nor report success
                  when some of the writes failed.

None of these had a test that ran them. They are the server half of the feature that has twice
deleted somebody's files, so "it reads correct" is not the standard.
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


def _call(req, *, docs=None, raises=None, put=None, user=True):
    """Drive sync_manifest with the relay and the account stubbed.

    `docs` is what list_docs returns: {d_tag: (doc_or_None, created_at)}. `put` is called for every
    write so a test can watch which d-tags were touched, and can refuse one.
    """
    from app.services import nostr_store as ns

    writes = []

    async def _put(port, sk, key, doc):
        writes.append(key)
        return True if put is None else put(key)

    listing = (mock.AsyncMock(side_effect=raises) if raises is not None
               else mock.AsyncMock(return_value=docs or {}))
    with mock.patch.object(ns, "list_docs", listing), \
            mock.patch.object(ns, "put_doc", _put), \
            mock.patch.object(ns, "user_storage_seckey", lambda db, u: b"\x01" * 32), \
            mock.patch.object(C.nostr_service, "to_pubkey_hex", lambda p: "a" * 64), \
            mock.patch.object(C.nostr_service, "npub_of", lambda p: "npub1fake"), \
            mock.patch.object(C, "_verify_self_auth", lambda a, p: True), \
            mock.patch.object(C, "_setting", lambda db, k, d=None: "3052"):
        resp = asyncio.run(C.sync_manifest(req, db=_FakeDB(_FakeUser() if user else None)))
    import json
    return resp.status_code, json.loads(bytes(resp.body).decode()), writes


def _req(**kw):
    kw.setdefault("pubkey", "a" * 64)
    kw.setdefault("auth", "x")
    kw.setdefault("folder", "Pictures")
    return C.SyncManifestReq(**kw)


class TheKeyTests(unittest.TestCase):
    """The d-tag is shared with every other private document this account owns."""

    def test_a_device_name_cannot_reach_another_document(self):
        self.assertEqual(C._sync_folder_key("Pictures", "laptop-a1"), "pcai:sync:Pictures:laptop-a1")
        # A colon would let a device address `pcai:sync:Pictures:x:pcai:note:...`-shaped keys, and a
        # wildcard would let it match them.
        self.assertEqual(C._sync_folder_key("Pictures", "a:b*c"), "pcai:sync:Pictures:abc")

    def test_a_device_name_that_sanitises_to_nothing_is_refused(self):
        self.assertIsNone(C._sync_folder_key("Pictures", ":::"))

    def test_a_folder_name_that_sanitises_too_short_is_refused(self):
        self.assertIsNone(C._sync_folder_key("a:"))

    def test_the_split_is_the_exact_inverse(self):
        for pair, dev in (("Pictures", "laptop-a1"), ("Pictures", None)):
            key = C._sync_folder_key(pair, dev)
            self.assertEqual(C._sync_split_key(key), (pair, dev))


class ViewsTests(unittest.TestCase):
    def test_it_returns_one_entry_per_device(self):
        code, j, _ = _call(_req(views=True), docs={
            "pcai:sync:Pictures:laptop": ({"n": 3}, 1),
            "pcai:sync:Pictures:phone": ({"n": 5}, 2),
        })
        self.assertEqual(code, 200)
        self.assertEqual(sorted(j["views"].keys()), ["laptop", "phone"])
        self.assertEqual(j["unreadable"], 0)

    def test_a_device_that_cannot_be_read_is_counted_not_dropped(self):
        """Dropped, it merges as "that device holds nothing" — and every path only that device
        published then looks deleted to everybody else."""
        code, j, _ = _call(_req(views=True), docs={
            "pcai:sync:Pictures:laptop": ({"n": 3}, 1),
            "pcai:sync:Pictures:phone": (None, 2),
        })
        self.assertEqual(list(j["views"].keys()), ["laptop"])
        self.assertEqual(j["unreadable"], 1)
        self.assertEqual(j["cannot"], ["phone"], "it does not say WHICH device to retire")

    def test_an_unreadable_relay_is_a_503_not_an_empty_folder(self):
        code, j, _ = _call(_req(views=True), raises=RuntimeError("relay down"))
        self.assertEqual(code, 503)
        self.assertNotIn("views", j)

    def test_a_folder_whose_name_merely_starts_the_same_is_not_folded_in(self):
        """`list_docs` matches on a prefix, so "Pictures" would otherwise collect "Pictures2"."""
        code, j, _ = _call(_req(views=True), docs={
            "pcai:sync:Pictures:laptop": ({"n": 1}, 1),
            "pcai:sync:Pictures2:laptop": ({"n": 99}, 1),
        })
        self.assertEqual(list(j["views"].keys()), ["laptop"])
        self.assertEqual(j["views"]["laptop"]["n"], 1, "it merged a different folder's document")

    def test_the_pre_rewrite_shared_document_is_reported_separately(self):
        """A device that has not updated still writes the shared one. It is offered as `legacy` so
        the merge can include it, and never as a device."""
        code, j, _ = _call(_req(views=True), docs={
            "pcai:sync:Pictures": ({"n": 7}, 1),
            "pcai:sync:Pictures:laptop": ({"n": 1}, 2),
        })
        self.assertEqual(j["legacy"], {"n": 7})
        self.assertEqual(list(j["views"].keys()), ["laptop"])

    def test_an_account_this_node_does_not_know_is_refused_not_answered_empty(self):
        code, j, _ = _call(_req(views=True), docs={}, user=False)
        self.assertEqual(code, 403)


class ForgetDeviceTests(unittest.TestCase):
    def test_it_clears_exactly_one_device(self):
        code, j, writes = _call(_req(forgetDevice="phone"))
        self.assertEqual(code, 200)
        self.assertEqual(writes, ["pcai:sync:Pictures:phone"])

    def test_a_device_name_that_sanitises_to_nothing_writes_nothing(self):
        code, j, writes = _call(_req(forgetDevice=":::"))
        self.assertEqual(code, 400)
        self.assertEqual(writes, [])

    def test_a_write_that_did_not_land_is_reported_as_a_failure(self):
        """Otherwise the screen says the device was retired and its stale view is still in the merge
        on the next sweep."""
        code, j, _ = _call(_req(forgetDevice="phone"), put=lambda k: False)
        self.assertEqual(code, 503)


class ForgetAllTests(unittest.TestCase):
    def test_it_clears_every_device_of_that_pair_and_nothing_else(self):
        code, j, writes = _call(_req(forgetAll=True), docs={
            "pcai:sync:Pictures:laptop": ({"n": 1}, 1),
            "pcai:sync:Pictures:phone": ({"n": 2}, 1),
            "pcai:sync:Pictures2:laptop": ({"n": 3}, 1),
        })
        self.assertEqual(code, 200)
        self.assertEqual(sorted(writes),
                         ["pcai:sync:Pictures:laptop", "pcai:sync:Pictures:phone"])
        self.assertEqual(j["cleared"], 2)

    def test_it_clears_the_pre_rewrite_shared_document_too(self):
        """Left behind, it is still in every updated device's merge — so "forget this folder"
        would leave the folder's whole contents in play."""
        code, j, writes = _call(_req(forgetAll=True), docs={
            "pcai:sync:Pictures": ({"n": 9}, 1),
        })
        self.assertEqual(writes, ["pcai:sync:Pictures"])

    def test_a_partial_clear_is_not_reported_as_success(self):
        code, j, _ = _call(_req(forgetAll=True), docs={
            "pcai:sync:Pictures:laptop": ({"n": 1}, 1),
            "pcai:sync:Pictures:phone": ({"n": 2}, 1),
        }, put=lambda k: k.endswith("laptop"))
        self.assertEqual(code, 503)
        self.assertEqual(j["cleared"], 1)
        self.assertEqual(j["failed"], 1)

    def test_an_unreadable_relay_clears_nothing(self):
        """It cannot know what it would be clearing, and a partial forget leaves a folder half in
        and half out of the merge."""
        code, j, writes = _call(_req(forgetAll=True), raises=RuntimeError("relay down"))
        self.assertEqual(code, 503)
        self.assertEqual(writes, [])


if __name__ == "__main__":
    unittest.main()
