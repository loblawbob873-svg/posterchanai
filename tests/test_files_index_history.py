"""Retained Files-index versions are usable recovery, not dead backup records."""

import asyncio
import json
from unittest import mock

from app.routers import client as C


class _User:
    nostr_npub = "npub1fake"


class _DB:
    def query(self, *_a, **_k): return self
    def filter(self, *_a, **_k): return self
    def first(self): return _User()


class _HistoryStore:
    APP_KIND = 30078

    async def _ws_query(self, _port, filters, strict=False):
        assert strict is True
        assert set(filters[0]["#d"]) == {
            "pcai:files-index-bak:1", "pcai:files-index-bak:2",
            "pcai:files-index-bak:3", "pcai:files-index-bak:4",
            "pcai:files-index-bak:5",
        }
        return [
            {"created_at": 20, "tags": [["d", "pcai:files-index-bak:2"]]},
            {"created_at": 10, "tags": [["d", "pcai:files-index-bak:1"]]},
        ]

    async def get_docs(self, _port, tags, **kwargs):
        assert kwargs.get("strict") is True
        assert set(tags) == {"pcai:files-index-bak:1", "pcai:files-index-bak:2"}
        return {
            "pcai:files-index-bak:1": {"n": 5968, "indexSha": "old"},
            "pcai:files-index-bak:2": {"n": 5977, "indexSha": "new"},
        }


def test_history_is_newest_first_and_exposes_no_keys_or_blob_addresses():
    got = asyncio.run(C._files_index_history(_HistoryStore(), 3052, b"\x01" * 32))
    assert got == [
        {"slot": 2, "created_at": 20, "n": 5977},
        {"slot": 1, "created_at": 10, "n": 5968},
    ]
    assert "mk" not in json.dumps(got)
    assert "indexSha" not in json.dumps(got)


def test_restore_keeps_the_live_version_elsewhere_and_preserves_the_canonical_key():
    from app.services import nostr_store as store

    current = {"n": 19, "indexSha": "current", "mk": "canonical"}
    chosen = {"n": 42, "indexSha": "chosen", "mk": "stale"}
    get_docs = mock.AsyncMock(return_value={
        "pcai:files-index": current,
        "pcai:files-index-bak:3": chosen,
    })
    put_doc = mock.AsyncMock(return_value=True)
    backup = mock.AsyncMock(return_value=None)
    req = C.FilesIndexReq(pubkey="a" * 64, auth="proof", restore=3)
    with mock.patch.object(store, "get_docs", get_docs), \
            mock.patch.object(store, "put_doc", put_doc), \
            mock.patch.object(store, "user_storage_seckey", lambda *_: b"\x01" * 32), \
            mock.patch.object(C, "_files_index_backup", backup), \
            mock.patch.object(C, "_expire_unreferenced_index"), \
            mock.patch.object(C.nostr_service, "to_pubkey_hex", lambda _p: "a" * 64), \
            mock.patch.object(C.nostr_service, "npub_of", lambda _p: "npub1fake"), \
            mock.patch.object(C, "_verify_self_auth", lambda *_: True), \
            mock.patch.object(C, "_setting", lambda *_: "3052"):
        response = asyncio.run(C.files_index(req, db=_DB()))

    body = json.loads(bytes(response.body))
    assert response.status_code == 200
    assert body == {"ok": True, "restored": 3, "n": 42}
    backup.assert_awaited_once_with(store, 3052, b"\x01" * 32, current, True,
                                    exclude={"pcai:files-index-bak:3"}, strict=True)
    restored = put_doc.await_args.args[3]
    assert restored == {"n": 42, "indexSha": "chosen", "mk": "canonical"}


def test_restore_rejects_an_invalid_slot_before_reading_the_relay():
    from app.services import nostr_store as store

    read = mock.AsyncMock()
    req = C.FilesIndexReq(pubkey="a" * 64, auth="proof", restore=99)
    with mock.patch.object(store, "get_docs", read), \
            mock.patch.object(store, "user_storage_seckey", lambda *_: b"\x01" * 32), \
            mock.patch.object(C.nostr_service, "to_pubkey_hex", lambda _p: "a" * 64), \
            mock.patch.object(C.nostr_service, "npub_of", lambda _p: "npub1fake"), \
            mock.patch.object(C, "_verify_self_auth", lambda *_: True), \
            mock.patch.object(C, "_setting", lambda *_: "3052"):
        response = asyncio.run(C.files_index(req, db=_DB()))
    assert response.status_code == 400
    read.assert_not_awaited()


def test_restore_never_replaces_live_state_if_safeguarding_it_fails():
    from app.services import nostr_store as store

    get_docs = mock.AsyncMock(return_value={
        "pcai:files-index": {"n": 19, "indexSha": "current", "mk": "key"},
        "pcai:files-index-bak:2": {"n": 42, "indexSha": "chosen", "mk": "key"},
    })
    put_doc = mock.AsyncMock(return_value=True)
    req = C.FilesIndexReq(pubkey="a" * 64, auth="proof", restore=2)
    with mock.patch.object(store, "get_docs", get_docs), \
            mock.patch.object(store, "put_doc", put_doc), \
            mock.patch.object(store, "user_storage_seckey", lambda *_: b"\x01" * 32), \
            mock.patch.object(C, "_files_index_backup", mock.AsyncMock(side_effect=RuntimeError("no ack"))), \
            mock.patch.object(C.nostr_service, "to_pubkey_hex", lambda _p: "a" * 64), \
            mock.patch.object(C.nostr_service, "npub_of", lambda _p: "npub1fake"), \
            mock.patch.object(C, "_verify_self_auth", lambda *_: True), \
            mock.patch.object(C, "_setting", lambda *_: "3052"):
        response = asyncio.run(C.files_index(req, db=_DB()))
    assert response.status_code == 503
    put_doc.assert_not_awaited()
