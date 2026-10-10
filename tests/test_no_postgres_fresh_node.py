"""A FRESH NODE WITH NO POSTGRES serves the app (#161, owner: "the entire point of this was to avoid postgres").

The whole app (app.main's routes, over ASGI) against the shipped RelayServer on a PosterChanDB PRIMARY store -- no
SQL database anywhere -- doing what a new user does first: sign up with a Nostr key, come back with the session,
make an API key and use it, upload to Blossom and list it, save a setting and read it back, and send a chat
message and read the conversation. Then a FRESH PROCESS (every table forgotten) still knows the session.

DATABASE_URL points at a port nothing listens on, so a code path that still reaches for SQL fails here instead of
finding a database by accident (and never the default URL, which on a production box is a real one).
"""
import os

os.environ["DATABASE_URL"] = "postgresql+psycopg2://nobody@127.0.0.1:9/no_database"   # before any app import
os.environ["PGCONNECT_TIMEOUT"] = "2"

import asyncio  # noqa: E402
import base64  # noqa: E402
import hashlib  # noqa: E402
import json  # noqa: E402
import time  # noqa: E402

import httpx  # noqa: E402
import pytest  # noqa: E402

from app.services.nostr import bech32, bip340  # noqa: E402
from app.services.nostr.event import build_event  # noqa: E402
from tests.test_doc_table import SK, _Relay  # noqa: E402

USER_SK = bytes.fromhex("5a" * 32)
USER_PK = bip340.pubkey_from_seckey(USER_SK).hex()


@pytest.fixture
def node(tmp_path, monkeypatch):
    relay = _Relay(str(tmp_path / "relay"))
    from app.services import keystore, settings_store, doc_table
    monkeypatch.setattr(keystore, "get_operator_nsec", lambda: bech32.encode("nsec", SK))
    monkeypatch.setenv("POSTERCHANAI_RELAY_PORT", str(relay.port))
    monkeypatch.setenv("POSTERCHANAI_AUTO_ADMIN", "1")
    monkeypatch.setattr(doc_table, "NS", "pcai:fresh%d:" % int(time.time() * 1000))
    doc_table.DocTable._registry.clear()
    values = {"nostr_relay_port": str(relay.port), "blossom_enabled": "true",
              "blossom_storage_backend": "local", "blossom_storage_path": str(tmp_path / "blobs"),
              "registration_enabled": "true"}
    real_get = settings_store.get
    monkeypatch.setattr(settings_store, "get", lambda k, d=None: values.get(k, real_get(k, d)))
    monkeypatch.setattr(settings_store, "put", lambda k, v: values.__setitem__(k, v))
    from app.main import app
    try:
        yield app
    finally:
        doc_table.DocTable._registry.clear()
        relay.close()


def _proof(purpose):
    ev = build_event(USER_SK, 27235, purpose)
    return base64.b64encode(json.dumps(ev).encode()).decode()


def _blossom_auth(sha):
    ev = build_event(USER_SK, 24242, "upload", tags=[["t", "upload"], ["x", sha],
                                                      ["expiration", str(int(time.time()) + 300)]])
    return "Nostr " + base64.b64encode(json.dumps(ev).encode()).decode()


async def _ask(c, method, url, **kw):
    """One request the way a client makes it: a 503 (a table still loading after the restart -- whole-table reads
    answer Retry-After rather than from part of a table) is retried, for a bounded time."""
    deadline = time.monotonic() + 20
    while True:
        r = await c.request(method, url, **kw)
        if r.status_code != 503 or time.monotonic() > deadline:
            return r
        await asyncio.sleep(0.25)


def test_a_fresh_node_with_no_postgres_serves_a_new_user(node):
    from app import database
    assert not hasattr(database, "engine") or database.engine is None     # no SQL engine exists at all

    async def flow():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(node), base_url="http://node.test") as c:
            # sign up with a Nostr key
            r = await _ask(c, "POST", "/api/auth/nostr-login", json={"pubkey": USER_PK, "auth": _proof("ai-login")})
            assert r.status_code == 200, r.text
            out = r.json()
            assert out["user"]["new"] is True
            token = out["access_token"]
            h = {"Authorization": "Bearer " + token}
            # log in with the session
            me = await _ask(c, "GET", "/api/auth/me", headers=h)
            assert me.status_code == 200, me.text
            assert me.json()["id"] == out["user"]["id"]
            # an API key, and the API with it
            k = await _ask(c, "POST", "/api/auth/api-keys", json={"name": "fresh"}, headers=h)
            assert k.status_code == 200, k.text
            key = k.json()["key"]
            me2 = await _ask(c, "GET", "/api/auth/me", headers={"Authorization": "Bearer " + key})
            assert me2.status_code == 200 and me2.json()["id"] == out["user"]["id"], me2.text
            # Blossom: upload, then list
            blob = b"hello from a node with no database"
            sha = hashlib.sha256(blob).hexdigest()
            up = await _ask(c, "PUT", "/blossom/upload", content=blob,
                             headers={"Authorization": _blossom_auth(sha), "Content-Type": "text/plain"})
            assert up.status_code in (200, 201), up.text
            ls = await _ask(c, "GET", "/blossom/list/" + USER_PK)
            assert ls.status_code == 200, ls.text
            assert sha in [b.get("sha256") for b in ls.json()]
            # a setting, round trip
            s = await _ask(c, "PUT", "/api/auth/settings", json={"theme": "win98"}, headers=h)
            assert s.status_code == 200, s.text
            g = await _ask(c, "GET", "/api/auth/settings", headers=h)
            assert g.status_code == 200 and g.json().get("theme") == "win98", g.text
            # a chat message, and the conversation holding it
            conv = await _ask(c, "POST", "/api/conversations", json={"title": "first"}, headers=h)
            assert conv.status_code == 201, conv.text
            cid = conv.json()["id"]
            sent = await _ask(c, "POST", "/api/chat/send", json={"conversation_id": cid, "content": "help"}, headers=h)
            assert sent.status_code == 200, sent.text
            msgs = await _ask(c, "GET", "/api/conversations/%d/messages" % cid, headers=h)
            assert msgs.status_code == 200, msgs.text
            roles = [(m.get("role"), m.get("content")) for m in msgs.json()]
            assert ("user", "help") in roles and any(r == "assistant" and c for r, c in roles), msgs.text
            return token

    token = asyncio.run(flow())

    # a FRESH PROCESS: nothing held in memory, the session still resolves from the relay
    from app.services import doc_table
    doc_table.DocTable._registry.clear()

    async def again():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(node), base_url="http://node.test") as c:
            me = await _ask(c, "GET", "/api/auth/me", headers={"Authorization": "Bearer " + token})
            assert me.status_code == 200, me.text
            g = await _ask(c, "GET", "/api/auth/settings", headers={"Authorization": "Bearer " + token})
            assert g.json().get("theme") == "win98"
    asyncio.run(again())


def test_the_relay_store_defaults_to_posterchandb_only_on_a_node_with_no_postgres(tmp_path, monkeypatch):
    """No DSN anywhere and no PosterChanDB directory: a fresh install -> primary. A directory already holding a
    MIRROR's data means the node has a Postgres (its DSN default was never written down): keep the old default
    rather than refuse to start. POSTERCHANDB_MODE always wins."""
    from app.services import settings_store
    from app.services.nostr_relay import thread
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("NOSTR_RELAY_PG_DSN", raising=False)
    monkeypatch.delenv("POSTERCHANDB_MODE", raising=False)
    monkeypatch.setenv("POSTERCHANDB_DIR", str(tmp_path / "pcdb"))
    real_get = settings_store.get
    monkeypatch.setattr(settings_store, "get", lambda k, d=None: None if k == "nostr_relay_pg_dsn" else real_get(k, d))
    assert thread._pcdb_mode() == "primary"
    (tmp_path / "pcdb" / "relay").mkdir(parents=True)
    (tmp_path / "pcdb" / "relay" / "segment-0").write_bytes(b"x")     # a mirror's files, never promoted
    assert thread._pcdb_mode() == "off"
    monkeypatch.setenv("POSTERCHANDB_MODE", "serve")
    assert thread._pcdb_mode() == "serve"
    monkeypatch.delenv("POSTERCHANDB_MODE")
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg2://x@127.0.0.1:9/y")
    assert thread._pcdb_mode() == "off"
