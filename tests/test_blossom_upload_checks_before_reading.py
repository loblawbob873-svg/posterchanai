"""PUT /blossom/upload decides who is uploading BEFORE it reads the upload (security review #9).

It read the whole body into memory first and checked the size and the signature after, so anybody,
signed in or not, could make the node buffer a body of any size -- one request per gigabyte of RAM.
Now: a missing/invalid/unauthorised Authorization is refused, and a declared Content-Length over the
limit is a 413, without a byte of the body read; a body that runs past the limit is cut off as it streams.
"""
import asyncio

import pytest
from starlette.requests import Request

from app.routers import blossom as router
from app.services import blossom_service


class _Body:
    def __init__(self, chunks):
        self.chunks, self.read = list(chunks), 0

    async def receive(self):
        if not self.chunks:
            return {"type": "http.request", "body": b"", "more_body": False}
        c = self.chunks.pop(0)
        self.read += len(c)
        return {"type": "http.request", "body": c, "more_body": bool(self.chunks)}


def _req(body, headers):
    scope = {"type": "http", "method": "PUT", "path": "/blossom/upload", "query_string": b"",
             "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()]}
    return Request(scope, body.receive)


@pytest.fixture(autouse=True)
def _cfg(monkeypatch):
    monkeypatch.setattr(blossom_service, "is_enabled", lambda db: True)
    monkeypatch.setattr(blossom_service, "_cfg", lambda db: {"max_upload_mb": 1, "public_url": ""})


def test_no_signature_no_read():
    body = _Body([b"x" * 65536] * 64)
    r = asyncio.run(router.upload(_req(body, {}), db=None))
    assert r.status_code == 401
    assert body.read == 0, "the node read %d bytes of an unsigned upload" % body.read


def test_a_declared_size_over_the_limit_is_refused_unread(monkeypatch):
    monkeypatch.setattr(blossom_service, "verify_auth", lambda h, a, s=None: "ab" * 32)

    async def allowed(db, pk):
        return True
    monkeypatch.setattr(blossom_service, "is_pubkey_allowed_async", allowed)
    body = _Body([b"x" * 65536] * 64)
    r = asyncio.run(router.upload(_req(body, {"authorization": "Nostr x", "content-length": str(5 * 1024 * 1024)}), db=None))
    assert r.status_code == 413 and body.read == 0


def test_an_unauthorised_key_is_refused_unread(monkeypatch):
    monkeypatch.setattr(blossom_service, "verify_auth", lambda h, a, s=None: "ab" * 32)

    async def refused(db, pk):
        return False
    monkeypatch.setattr(blossom_service, "is_pubkey_allowed_async", refused)
    body = _Body([b"x" * 65536] * 4)
    r = asyncio.run(router.upload(_req(body, {"authorization": "Nostr x"}), db=None))
    assert r.status_code == 403 and body.read == 0


def test_a_body_that_lies_about_its_size_is_cut_off(monkeypatch):
    monkeypatch.setattr(blossom_service, "verify_auth", lambda h, a, s=None: "ab" * 32)

    async def allowed(db, pk):
        return True
    monkeypatch.setattr(blossom_service, "is_pubkey_allowed_async", allowed)
    body = _Body([b"x" * 65536] * 64)                       # 4 MB behind a 1 MB limit, no Content-Length
    r = asyncio.run(router.upload(_req(body, {"authorization": "Nostr x"}), db=None))
    assert r.status_code == 413
    assert body.read <= 1024 * 1024 + 65536, body.read
