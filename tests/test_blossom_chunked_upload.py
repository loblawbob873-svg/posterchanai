"""Uploads larger than Cloudflare's 100 MB body limit arrive in parts (app/services/chunked_upload.py).

2026-10-21: the Meme Builder became a video editor, and a phone video over 100 MB could not reach poster.place at
all. The client sends <= 64 MB parts to PUT /blossom/upload/part/{id}/{i} and joins them with POST
/blossom/upload/finish/{id}. Pinned here: each part is authorised (bound to the whole file's hash) BEFORE a byte is
read; a part past PART_MAX is cut off as it streams; the joined file must be exactly the declared size and hash or
nothing is stored; the parts are freed as they are joined; abandoned uploads are swept; an id cannot walk the disk.
"""
import asyncio
import hashlib
import json
import os
import time

import pytest
from starlette.requests import Request

from app.routers import blossom as router
from app.services import blossom_service, chunked_upload

PK = "ab" * 32
UID = "0123456789abcdef0123456789abcdef"


class _Body:
    def __init__(self, chunks):
        self.chunks, self.read = list(chunks), 0

    async def receive(self):
        if not self.chunks:
            return {"type": "http.request", "body": b"", "more_body": False}
        c = self.chunks.pop(0)
        self.read += len(c)
        return {"type": "http.request", "body": c, "more_body": bool(self.chunks)}


def _req(method, path, body, headers):
    scope = {"type": "http", "method": method, "path": path, "query_string": b"",
             "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()]}
    return Request(scope, body.receive)


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    monkeypatch.setenv("PC_WORK_DIR", str(tmp_path / "work"))
    monkeypatch.setattr(blossom_service, "is_enabled", lambda db: True)
    monkeypatch.setattr(blossom_service, "_cfg", lambda db: {"max_upload_mb": 4, "public_url": ""})
    monkeypatch.setattr(blossom_service, "quota_exceeded", lambda db, pk, n: (False, 0, 0))
    seen = []

    def verify(h, action, sha=None):
        if not h:
            raise ValueError("missing authorization")
        seen.append(sha)
        return PK
    monkeypatch.setattr(blossom_service, "verify_auth", verify)

    async def allowed(db, pk):
        return True
    monkeypatch.setattr(blossom_service, "is_pubkey_allowed_async", allowed)
    return seen


def _put(i, data, sha, uid=UID, auth="Nostr x"):
    body = _Body([data[k:k + 65536] for k in range(0, len(data), 65536)] or [b""])
    hdr = {"x-upload-sha256": sha}
    if auth:
        hdr["authorization"] = auth
    r = asyncio.run(router.upload_part(uid, i, _req("PUT", "/blossom/upload/part", body, hdr), db=None))
    return r, body


def _finish(meta, uid=UID):
    raw = json.dumps(meta).encode()
    hdr = {"authorization": "Nostr x", "content-type": "application/json", "x-filename": "clip.mp4"}
    return asyncio.run(router.upload_finish(uid, _req("POST", "/blossom/upload/finish", _Body([raw]), hdr), db=None))


def _updir():
    return os.path.join(os.environ["PC_WORK_DIR"], "chunked", PK, UID)


def test_an_unsigned_part_is_refused_unread():
    data = b"x" * 300000
    r, body = _put(0, data, hashlib.sha256(data).hexdigest(), auth="")
    assert r.status_code == 401 and body.read == 0


def test_each_part_is_authorised_against_the_whole_files_hash(_env):
    sha = "cd" * 32
    r, _ = _put(0, b"abc", sha)
    assert r.status_code == 200 and _env[-1] == sha


def test_a_part_past_the_limit_is_cut_off_while_it_streams(monkeypatch):
    monkeypatch.setattr(chunked_upload, "PART_MAX", 200000)
    r, body = _put(0, b"x" * 2_000_000, "cd" * 32)
    assert r.status_code == 400 and body.read <= 200000 + 65536
    assert not os.listdir(_updir()), "a refused part left a file behind"


def test_parts_join_into_the_blob_and_are_freed(monkeypatch):
    data = os.urandom(700000)
    sha = hashlib.sha256(data).hexdigest()
    stored = {}

    async def save(db, pk, path, mime, filename="", keep=False):
        stored.update(bytes=open(path, "rb").read(), mime=mime, filename=filename, pk=pk)
        return {}
    monkeypatch.setattr(blossom_service, "save_blob_file", save)

    class _Blob:
        sha256, size, mime = sha, len(data), "video/mp4"

    async def aget(s):
        return _Blob() if s == sha else None
    monkeypatch.setattr(blossom_service.blob_index, "aget", aget)
    monkeypatch.setattr(blossom_service, "descriptor", lambda blob, base, name="": {"sha256": blob.sha256, "url": "u"})
    size = 300000
    for i in (2, 0, 1):                             # sent out of order: the join is by number, not arrival
        r, _ = _put(i, data[i * size:(i + 1) * size], sha)
        assert r.status_code == 200
    r = _finish({"parts": 3, "size": len(data), "sha256": sha, "type": "video/mp4"})
    assert r.status_code == 200, r.body
    assert json.loads(r.body)["sha256"] == sha
    assert stored["bytes"] == data and stored["mime"] == "video/mp4" and stored["filename"] == "clip.mp4"
    assert not os.path.exists(_updir()), "the parts and the joined file must be removed once stored"


@pytest.mark.parametrize("case", ["missing", "damaged", "short", "too_big"])
def test_a_join_that_is_not_exactly_the_declared_file_stores_nothing(monkeypatch, case):
    data = os.urandom(600000)
    sha = hashlib.sha256(data).hexdigest()
    called = []

    async def save(*a, **k):
        called.append(1)
    monkeypatch.setattr(blossom_service, "save_blob_file", save)
    parts = [data[:300000], data[300000:]]
    if case == "damaged":
        parts[1] = b"\0" + parts[1][1:]
    for i, p in enumerate(parts):
        if case == "missing" and i == 1:
            continue
        _put(i, p, sha)
    meta = {"parts": 2, "size": len(data), "sha256": sha}
    if case == "short":
        meta["size"] = len(data) + 1
    if case == "too_big":
        meta["size"] = 5 * 1024 * 1024               # over the 4 MB cap configured above
    r = _finish(meta)
    assert r.status_code == 400 and not called
    assert not os.path.exists(_updir())


def test_an_upload_id_cannot_leave_its_directory():
    for bad in ("../../etc", "ABCDEF0123456789", "x" * 10):
        r, _ = _put(0, b"a", "cd" * 32, uid=bad)
        assert r.status_code == 400
    for idx in (-1, chunked_upload.MAX_PARTS):
        r, _ = _put(idx, b"a", "cd" * 32)
        assert r.status_code == 400


def test_abandoned_uploads_are_swept():
    _put(0, b"abc", "cd" * 32)
    assert os.path.isdir(_updir())
    assert chunked_upload.sweep(time.time() + 60) == 0
    assert chunked_upload.sweep(time.time() + chunked_upload.UPLOAD_TTL_S + 60) == 1
    assert not os.path.exists(_updir())


def test_the_default_blossom_cap_takes_a_long_video():
    src = open(blossom_service.__file__).read()
    assert '"max_upload_mb": gi("blossom_max_upload_mb", 2048)' in src


def test_the_client_sends_a_big_file_in_parts():
    import subprocess
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    r = subprocess.run(["node", "tests/client/upload_chunked_runtime.mjs"], cwd=root, capture_output=True, text=True,
                       timeout=60)
    assert r.returncode == 0 and r.stdout.strip().endswith("OK"), r.stdout + r.stderr
