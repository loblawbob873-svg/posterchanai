"""DELETING A STREAM ALSO DELETES ITS RECORDING -- AND NOBODY ELSE'S FILE.

"I deleted stream from my profile page but I see it in blossom still" (2026-10-30): Delete stream published a kind 5
for the 30311 and the VOD stayed on the drive. DELETE /api/streams/vods/<sha> removes the caller's recording row and
releases the blob the way Blossom's own DELETE does: the bytes go only when no other owner references them.
"""
import asyncio
import json
from types import SimpleNamespace

SHA = "ab" * 32
PK = "cd" * 32


def _setup(monkeypatch, rows, owners_left=0, blob=True):
    from app.routers import streams
    from app.services import blossom_service, stream_vod_store
    calls = {"release": [], "deleted": [], "removed": []}
    monkeypatch.setattr(stream_vod_store, "for_user", lambda uid: [r for r in rows if r["user_id"] == uid])

    async def aremove(uid, sha):
        calls["removed"].append((uid, sha))
        return [r for r in rows if r["user_id"] == uid and r["sha256"] == sha]
    monkeypatch.setattr(stream_vod_store, "aremove_for", aremove)

    async def aget(sha):
        return SimpleNamespace(sha256=sha, owners={PK: 1}) if blob else None
    monkeypatch.setattr(blossom_service.blob_index, "aget", aget)

    async def release(db, sha, pk):
        calls["release"].append(pk)
        return owners_left
    monkeypatch.setattr(blossom_service, "release_owner", release)

    async def delete(db, b):
        calls["deleted"].append(b.sha256)
    monkeypatch.setattr(blossom_service, "delete_blob", delete)
    monkeypatch.setattr(blossom_service, "drop_meta", lambda sha: None)
    return streams, calls


def _call(streams, uid, sha):
    r = asyncio.run(streams.stream_vod_delete(sha, current_user=SimpleNamespace(id=uid), db=None))
    return (r.status_code, json.loads(r.body)) if hasattr(r, "status_code") else (200, r)


def test_your_recording_is_removed_with_its_blob(monkeypatch):
    streams, calls = _setup(monkeypatch, [{"user_id": 7, "sha256": SHA, "pubkey": PK, "token": "t", "started_at": 1}])
    code, body = _call(streams, 7, SHA)
    assert code == 200 and body["deleted"] == SHA and not body["shared"], body
    assert calls["release"] == [PK] and calls["deleted"] == [SHA] and calls["removed"] == [(7, SHA)]


def test_a_blob_someone_else_also_owns_keeps_its_bytes(monkeypatch):
    streams, calls = _setup(monkeypatch, [{"user_id": 7, "sha256": SHA, "pubkey": PK, "token": "t", "started_at": 1}], owners_left=1)
    code, body = _call(streams, 7, SHA)
    assert code == 200 and body["shared"] and calls["deleted"] == [], (body, calls)


def test_nobody_can_delete_a_recording_that_is_not_theirs(monkeypatch):
    streams, calls = _setup(monkeypatch, [{"user_id": 8, "sha256": SHA, "pubkey": PK, "token": "t", "started_at": 1}])
    code, _ = _call(streams, 7, SHA)
    assert code == 404 and calls["release"] == [] and calls["deleted"] == [] and calls["removed"] == []
    assert _call(streams, 7, "../etc")[0] == 400


def test_delete_stream_in_the_client_asks_for_its_recording_too():
    src = open("static/js/client/streams.js").read()
    i = src.index("async function _deleteStream(e){")
    body = src[i:src.index("function _forgetDeletedStream", i)]
    assert "_streamFetch('/api/streams/vods/'+sha, {method:'DELETE'})" in body and "_vodPick(vods" in body
