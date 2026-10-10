"""E-mail verification tokens as a DocTable (#161) -- what used to be the SQL `verification_tokens` table.

One document per token, keyed by an integer id (the token itself is a secret: it lives only inside the
encrypted document, never in a d-tag and never in a log line).

The rules are the old ones, made explicit:
  * a token is valid until `expires_at` (naive UTC);
  * it is SINGLE-USE: `consume` removes it from the relay -- and the relay must CONFIRM the removal --
    before the caller is allowed to act on it. A deletion that did not land would leave a used link
    working; so a failed one is `Unavailable` and the verification does not happen;
  * an expired token, or one whose user is gone, is removed as before;
  * resending deletes the user's previous tokens first.
A table that cannot be read raises `relay_reader.Unavailable` -- never "invalid link".
"""
from __future__ import annotations

import asyncio
import hmac
import secrets
import threading
from datetime import datetime, timedelta

from app.services import table_migrate as tm
from app.services.doc_table import DocTable
from app.services.table_migrate import Row

TABLE = "verification_tokens"
_lock = threading.Lock()
_alock: dict = {}


def _find(rows, token: str):
    if not token:
        return None, None
    t = token.encode("utf-8", "surrogatepass")
    hit = None
    for k, r in rows:
        v = str(r.get("token") or "").encode("utf-8", "surrogatepass")
        if v and hmac.compare_digest(v, t):
            hit = (k, r)
    return hit if hit else (None, None)


def _verdict(r: dict) -> str:
    exp = tm.dt(r.get("expires_at"))
    return "expired" if exp is None or exp < datetime.utcnow() else "ok"


def consume(token: str) -> tuple:
    """(status, user_id): "invalid" (no such token), "expired" (removed), or "ok" (removed: the caller
    may now act on user_id). Raises Unavailable -- including when the removal was not confirmed."""
    with _lock:
        k, r = _find(tm.view(TABLE), token)
        if k is None:
            return "invalid", None
        DocTable(TABLE).delete(k)
        return _verdict(r), r.get("user_id")


async def aconsume(token: str) -> tuple:
    lock = _alock.setdefault(id(asyncio.get_running_loop()), asyncio.Lock())
    async with lock:
        k, r = _find(await tm.aview(TABLE), token)
        if k is None:
            return "invalid", None
        await DocTable(TABLE).adelete(k)
        return _verdict(r), r.get("user_id")


def issue(user_id: int, hours: int = 24) -> str:
    """Replace the user's tokens with a fresh one; returns the token. Raises Unavailable."""
    with _lock:
        delete_for_user(user_id)
        token = secrets.token_urlsafe(32)
        DocTable(TABLE).insert({"id": None, "user_id": int(user_id), "token": token,
                                "created_at": tm.iso(datetime.utcnow()),
                                "expires_at": tm.iso(datetime.utcnow() + timedelta(hours=hours))},
                               lambda: tm.anext_id(TABLE))     # SQL's id until the marker exists
        return token


def delete_for_user(user_id: int) -> int:
    n = 0
    for k, r in tm.view(TABLE):
        if r.get("user_id") == user_id:
            DocTable(TABLE).delete(k)
            n += 1
    return n


def for_user(user_id: int) -> list:
    return [Row(r) for _k, r in tm.view(TABLE) if r.get("user_id") == user_id]


# ------------------------------------------------------------------------------------ the SQL side (#161 wave 1)
def _token_from_sql(v) -> dict:
    return {"id": int(v.id), "user_id": int(v.user_id), "token": v.token,
            "created_at": tm.iso(v.created_at), "expires_at": tm.iso(v.expires_at)}


def rows_from_sql(db) -> dict:
    from app.models import VerificationToken
    return {str(v.id): _token_from_sql(v) for v in db.query(VerificationToken).all()}


def _legacy_from_sql():
    from app.services.legacy_sql import ModelLegacy
    return ModelLegacy(TABLE, "VerificationToken", _token_from_sql)


def _register_legacy():
    from app.services import table_migration
    table_migration.register(_legacy_from_sql())


_register_legacy()

