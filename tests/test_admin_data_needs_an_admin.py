"""Who asked for AI/streaming access, and any account's admin flag and capabilities, are for admins only
(security review #10). /client/ai-requests, /client/stream-requests and /client/user-caps answered anybody.
"""
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.auth import get_admin_user
from app.routers import client as client_router


def _app():
    app = FastAPI()
    app.include_router(client_router.router)
    return app


def test_a_stranger_is_refused():
    c = TestClient(_app(), raise_server_exceptions=False)
    for path in ("/client/ai-requests", "/client/stream-requests", "/client/user-caps?pubkey=" + "ab" * 32):
        r = c.get(path)
        assert r.status_code in (401, 403), (path, r.status_code, r.text[:200])


def test_an_admin_still_reads_them(monkeypatch):
    app = _app()
    app.dependency_overrides[get_admin_user] = lambda: SimpleNamespace(is_admin=True, id=1)
    from app.services import user_settings_table, user_table

    async def none(*a, **k):
        return {}

    async def nobody(*a, **k):
        return None
    monkeypatch.setattr(user_settings_table, "aby_key", none)
    monkeypatch.setattr(user_table, "aby_npub", nobody)
    c = TestClient(app, raise_server_exceptions=False)
    assert c.get("/client/ai-requests").json() == {"ok": True, "requests": []}
    assert c.get("/client/stream-requests").json() == {"ok": True, "requests": []}
    assert c.get("/client/user-caps?pubkey=" + "ab" * 32).json() == {"ok": True, "exists": False}
