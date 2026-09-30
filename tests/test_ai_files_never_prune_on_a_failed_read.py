"""AI chat files: Prune deletes only what NOTHING references, and a read that failed or stopped short
deletes nothing.

Found auditing "we can't have our core apps not working if network outage" (the calendar drew 0
events while a deploy restarted the relay). `/client/ai-files-prune` built its keep-set from two
LOOSE reads: an unreachable relay answered {} for both, no blob was referenced, and EVERY private
upload and generated image of the account was deleted; a chat history past list_docs' 5000 lost the
images of its oldest messages the same way. The routes run here against a fake relay with the real
semantics (newest first, `limit`, the `#d~` prefix, the (created_at, id) cursor, strict raising).
"""
import asyncio
import json
from types import SimpleNamespace
from unittest import mock

import pytest

from app.routers import client as C
from app.services import nostr_store as store

SK = bytes(range(1, 33))
PK = "ab" * 32
SHA_OLD, SHA_UP, SHA_ORPHAN = "1" * 64, "2" * 64, "3" * 64


class Relay:
    def __init__(self, n_msgs, down=False):
        self.down, self.events = down, []
        for i in range(n_msgs):
            # The OLDEST message is the one that references SHA_OLD.
            body = {"content": f"![img](/x/enc_{SHA_OLD}.png)"} if i == 0 else {"content": f"message {i}"}
            self.events.append({"id": f"{i:064x}", "created_at": 1_000_000 + i,
                                "tags": [["d", f"{store.NS_MSG}conv:{i}"]], "content": json.dumps(body)})
        self.events.append({"id": "f" * 64, "created_at": 5_000_000,
                            "tags": [["d", f"{store.NS_UPLOAD}conv:up"]], "content": json.dumps({"sha256": SHA_UP, "name": "a.png"})})

    async def query(self, port, filters, strict=False, auth_seckey=None):
        if self.down:
            if strict:
                raise ConnectionError("relay not listening")
            return []
        f = filters[0]
        prefix = (f.get("#d~") or [""])[0]
        evs = [e for e in self.events if e["tags"][0][1].startswith(prefix)]
        evs.sort(key=lambda e: (e["created_at"], e["id"]), reverse=True)
        if f.get("_cursor"):
            cur = (int(f["_cursor"][0]), str(f["_cursor"][1]))
            evs = [e for e in evs if (e["created_at"], e["id"]) < cur]
        return evs[: f.get("limit", 500)]


def _run(route, relay, blobs, sha=""):
    deleted = []

    async def delete_blob(db, sha):
        deleted.append(sha)
        return True
    user = SimpleNamespace(id=1, nostr_npub="npub1x")
    db = mock.MagicMock()
    db.query.return_value.filter.return_value.first.return_value = user
    with mock.patch.object(store, "_ws_query", relay.query), \
         mock.patch.object(store, "_decode", lambda c, sk, enc: json.loads(c) if c else None), \
         mock.patch.object(store, "user_storage_seckey", lambda db, u: SK), \
         mock.patch.object(store, "delete_doc", mock.AsyncMock(return_value=True)), \
         mock.patch.object(store, "put_doc", mock.AsyncMock(return_value=True)), \
         mock.patch.object(C, "_verify_self_auth", lambda a, pk: True), \
         mock.patch.object(C, "_setting", lambda db, k, d="": "3052"), \
         mock.patch("app.services.blossom_service.list_for_pubkey", lambda db, pub, include_private=False: blobs), \
         mock.patch("app.services.artifact_store.delete_blob", delete_blob):
        resp = asyncio.run(getattr(C, route)(C.AiFileReq(pubkey=PK, auth="x", sha=sha), db))
    return resp.status_code, json.loads(resp.body), deleted


BLOBS = [SimpleNamespace(sha256=s, private=True, size=10) for s in (SHA_OLD, SHA_UP, SHA_ORPHAN)]


def test_prune_deletes_only_the_unreferenced_blob():
    code, body, deleted = _run("ai_files_prune", Relay(40), BLOBS)
    assert code == 200 and deleted == [SHA_ORPHAN], (body, deleted)


def test_an_unreachable_relay_deletes_nothing():
    code, body, deleted = _run("ai_files_prune", Relay(40, down=True), BLOBS)
    assert deleted == [], "every file was deleted because the relay could not be asked what is in use"
    assert code == 503 and "nothing was deleted" in body["error"]


def test_a_long_chat_history_is_read_to_its_oldest_message():
    """6000 messages: the oldest one's picture is still in use."""
    code, body, deleted = _run("ai_files_prune", Relay(6000), BLOBS)
    assert SHA_OLD not in deleted, "an image of an old message was deleted because the read stopped at 5000"
    assert code == 200 and deleted == [SHA_ORPHAN]


def test_the_listing_says_it_could_not_read_rather_than_offering_everything_for_pruning():
    code, body, _ = _run("ai_files", Relay(40, down=True), BLOBS)
    assert code == 503 and body["ok"] is False


def test_deleting_one_file_reads_its_references_first():
    code, body, deleted = _run("ai_file_delete", Relay(40, down=True), BLOBS, sha=SHA_UP)
    assert code == 503 and deleted == [], "the bytes were deleted and the card left pointing at nothing"
    code, body, deleted = _run("ai_file_delete", Relay(40), BLOBS, sha=SHA_UP)
    assert code == 200 and deleted == [SHA_UP]
