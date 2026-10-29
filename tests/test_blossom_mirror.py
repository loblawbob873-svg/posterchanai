"""BUD-04 `PUT /blossom/mirror`: copy a blob from another server, given its URL and an upload authorization.

server1, 2026-10-29: a client mirroring its uploads here got `405 Method Not Allowed` ~30 times a minute (3,450 in three
hours) -- the route did not exist, so the PUT fell on `/{sha256}`, which takes no PUT. The mirror fetches a URL the
CALLER chose from this node, so it is guarded like the web reader: the shipped `is_safe_url`, redirect hooks and pinned
transport, with only DNS and the dial replaced (tests/reader_fake_web.py), so a refusal is MEASURED on the stand-in for
this node's own network. The signature is real (a signed kind 24242); only storage is a stand-in.
"""
import asyncio
import base64
import hashlib
import json
import time

import httpx
import pytest
from fastapi import FastAPI

from app.routers import blossom as B
from app.services import blossom_service, search_service as S
from app.services.nostr.event import build_event
from tests import reader_fake_web as FW

SRC = "cdn" + FW.DOMAIN
SK = b"\x22" * 32
BLOB = FW.PNG * 40


def _auth(x, verb="upload", exp=None):
    ev = build_event(SK, 24242, "mirror", [["t", verb], ["x", x], ["expiration", str(exp or int(time.time()) + 300)]])
    return "Nostr " + base64.b64encode(json.dumps(ev).encode()).decode()


@pytest.fixture
def node(monkeypatch):
    web = FW.FakeWeb()
    web.install(monkeypatch)
    web.dns[SRC] = [FW.PUBLIC[0]]
    web.page(SRC, "/" + hashlib.sha256(BLOB).hexdigest(), BLOB, ctype="image/png")
    web.huge(SRC, "/huge", ctype="application/octet-stream")
    web.redirect(SRC, "/bounce", "http://127.0.0.1/secret")
    monkeypatch.setattr(S, "own_media_hosts", lambda: set())
    monkeypatch.setattr(blossom_service, "is_enabled", lambda db: True)
    monkeypatch.setattr(blossom_service, "_cfg", lambda db: {"max_upload_mb": 1, "public_url": ""})
    allowed = {"yes": True}

    async def is_allowed(db, pk):
        return allowed["yes"]
    monkeypatch.setattr(blossom_service, "is_pubkey_allowed_async", is_allowed)
    monkeypatch.setattr(blossom_service, "quota_exceeded", lambda db, pk, n: (False, 0, 0))
    stored = {}

    async def save_blob(db, pk, data, mime, **kw):
        stored[hashlib.sha256(data).hexdigest()] = (pk, data, mime)

    async def aget(sha):
        return {"sha256": sha, "size": len(stored[sha][1]), "type": stored[sha][2]} if sha in stored else None

    async def name_for(db, sha, pk):
        return ""
    monkeypatch.setattr(blossom_service, "save_blob", save_blob)
    monkeypatch.setattr(blossom_service.blob_index, "aget", aget)
    monkeypatch.setattr(blossom_service, "name_for", name_for)
    monkeypatch.setattr(blossom_service, "descriptor", lambda b, base, name="": {"sha256": b["sha256"], "size": b["size"]})
    monkeypatch.setattr(B, "_base_url", lambda r, db: "https://node.example/blossom")
    app = FastAPI()
    app.include_router(B.router)
    from app.database import get_db
    app.dependency_overrides[get_db] = lambda: None

    def put(url, auth):
        async def go():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://node.lan",
                                         timeout=120) as c:
                return await c.put("/blossom/mirror", json={"url": url}, headers={"Authorization": auth})
        return asyncio.run(go())
    yield put, web, stored, allowed
    web.close()


def test_a_blob_on_another_server_is_mirrored(node):
    put, web, stored, _ = node
    sha = hashlib.sha256(BLOB).hexdigest()
    r = put(f"http://{SRC}/{sha}", _auth(sha))
    assert r.status_code == 200, r.text
    assert r.json()["sha256"] == sha and stored[sha][1] == BLOB and stored[sha][2] == "image/png"


def test_bytes_that_are_not_the_authorized_blob_are_refused(node):
    put, _, stored, _ = node
    r = put(f"http://{SRC}/{hashlib.sha256(BLOB).hexdigest()}", _auth("ab" * 32))
    assert r.status_code == 401 and not stored, r.text


def test_nothing_is_fetched_for_an_unsigned_or_unauthorized_caller(node):
    put, web, stored, allowed = node
    sha = hashlib.sha256(BLOB).hexdigest()
    assert put(f"http://{SRC}/{sha}", "Nostr bm9wZQ==").status_code == 401
    allowed["yes"] = False
    assert put(f"http://{SRC}/{sha}", _auth(sha)).status_code == 403
    assert not web.public_hits and not stored, web.public_hits


def test_this_nodes_own_network_is_never_fetched(node):
    put, web, stored, _ = node
    for url in ("http://127.0.0.1/secret", "http://169.254.169.254/latest/meta-data", f"http://{SRC}/bounce"):
        r = put(url, _auth("cd" * 32))
        assert r.status_code in (400, 502), (url, r.status_code)
        assert FW.SECRET not in r.text
    assert web.internal_hits == [] and not stored, web.internal_hits


def test_a_source_bigger_than_the_limit_is_cut_off(node):
    put, web, stored, _ = node
    r = put(f"http://{SRC}/huge", _auth("ef" * 32))
    assert r.status_code == 413 and not stored, r.text
