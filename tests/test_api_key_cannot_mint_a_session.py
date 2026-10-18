"""An API key never becomes a session token.

Security review 2026-10-18: POST /api/auth/sessions/revoke ("sign out everywhere else") authenticated through
get_current_user, which accepts `sk-` API keys, and answered with a FRESH 30-day session JWT -- so a leaked key could
be turned into a session that outlives the key's revocation, while signing the owner out of every real device.
"""
import asyncio
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials
from starlette.requests import Request
from starlette.responses import Response

from app import auth
from app.routers import auth as auth_router


def _request(token):
    return Request({"type": "http", "method": "POST", "path": "/api/auth/sessions/revoke", "query_string": b"",
                    "headers": [(b"authorization", ("Bearer " + token).encode())], "client": ("1.2.3.4", 5)})


def test_an_api_key_cannot_mint_a_session(monkeypatch):
    user = SimpleNamespace(id=7, tokens_valid_after=None, is_active=True, username="alice")
    monkeypatch.setattr(auth, "query_api_key_with_retry", lambda db, t: (SimpleNamespace(id=1), 7))
    monkeypatch.setattr(auth, "get_user_from_api_key", lambda db, uid: user)
    monkeypatch.setattr(auth, "_refuse_blocked", lambda u: u)
    from app.services import api_key_store, user_table
    monkeypatch.setattr(api_key_store, "touch", lambda k: None)
    saved = []

    async def asave(db, u):
        saved.append(u)
    monkeypatch.setattr(user_table, "asave", asave)

    req = _request("sk-leaked")
    who = auth.get_current_user(req, HTTPAuthorizationCredentials(scheme="Bearer", credentials="sk-leaked"), db=None)
    assert who is user
    with pytest.raises(HTTPException) as e:
        asyncio.run(auth_router.revoke_other_sessions(Response(), req, current_user=who, db=None))
    assert e.value.status_code == 403
    assert saved == [] and user.tokens_valid_after is None, "the owner's sessions were revoked by an API key"
