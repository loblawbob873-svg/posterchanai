"""Sign out everywhere: a session token issued before the account said so stops working at once.

Security review #11 (2026-10-14): a session token was valid for its whole 30 days whatever happened -- a lost
phone or a leaked cookie could not be cut off. Driven through the SHIPPED get_current_user (HTTP) and
get_user_from_websocket with the shipped token minting; only the account store is a fake.
"""
import asyncio
import time
from types import SimpleNamespace
from unittest import mock

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from app import auth
from app.services import user_table
from app.services.user_table import UserRow


def _req(token):
    scope = {"type": "http", "method": "GET", "path": "/", "headers": [], "query_string": b"", "cookies": {}}
    r = Request(scope)
    return r, SimpleNamespace(credentials=token)


@pytest.fixture
def account():
    u = UserRow(7, {"username": "alice"})
    store = {7: u}

    async def aget(db, uid):
        return store.get(int(uid))
    with mock.patch.object(user_table, "get", lambda db, uid: store.get(int(uid))), \
         mock.patch.object(user_table, "aget", aget), \
         mock.patch("app.services.relay_blocklist.is_user_blocked", lambda user: False):
        yield u


def _who(token):
    r, cred = _req(token)
    return auth.get_current_user(r, cred, db=None)


def test_a_token_works_until_the_account_signs_out_everywhere(account):
    old = auth.create_access_token({"sub": "7"})
    assert _who(old).id == 7
    account.tokens_valid_after = int(time.time()) + 1        # pressed after `old` was issued
    with pytest.raises(HTTPException) as e:
        _who(old)
    assert e.value.status_code == 401 and "signed out" in e.value.detail


def test_the_device_that_pressed_it_stays_signed_in(account):
    account.tokens_valid_after = int(time.time())
    fresh = auth.create_access_token({"sub": "7"})
    assert _who(fresh).id == 7


def test_a_token_from_before_iat_existed_is_cut_off_only_when_asked(account):
    from jose import jwt
    legacy = jwt.encode({"sub": "7", "exp": int(time.time()) + 3600}, auth.SECRET_KEY, algorithm=auth.ALGORITHM)
    assert _who(legacy).id == 7, "an account that never pressed the button must not lose its sessions"
    account.tokens_valid_after = int(time.time())
    with pytest.raises(HTTPException):
        _who(legacy)


def test_a_websocket_with_a_revoked_token_is_nobody(account):
    old = auth.create_access_token({"sub": "7"})
    account.tokens_valid_after = int(time.time()) + 1
    ws = SimpleNamespace(query_params={"token": old}, cookies={})
    assert asyncio.run(auth.get_user_from_websocket(ws, db=None)) is None


def test_every_token_reader_applies_the_rule():
    import pathlib
    root = pathlib.Path(__file__).resolve().parents[1]
    for f in ("app/auth.py", "app/routers/ssh_term.py", "app/routers/telegram_client.py"):
        src = (root / f).read_text()
        assert src.count("decode_token(") <= src.count("token_revoked(") + (1 if f == "app/auth.py" else 0), f


def test_the_column_is_stored_on_the_account():
    from app.models import User
    cols = tuple(c.name for c in User.__table__.columns if c.name != "id")
    assert cols == user_table.COLUMNS and "tokens_valid_after" in cols


def test_the_endpoint_cuts_off_other_sessions_and_keeps_this_one(account):
    from starlette.responses import Response
    from app.routers import auth as auth_router
    old = auth.create_access_token({"sub": "7"})
    saved = []

    async def asave(db, u):
        saved.append(u.tokens_valid_after); return u
    time.sleep(1.05)                                   # a different second from `old`
    r, _ = _req(None)
    with mock.patch.object(user_table, "asave", asave):
        out = asyncio.run(auth_router.revoke_other_sessions(Response(), r, current_user=account, db=None))
    assert saved and out["ok"]
    with pytest.raises(HTTPException):
        _who(old)
    assert _who(out["access_token"]).id == 7
