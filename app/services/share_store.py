"""Public file-share links as a DocTable (#161) -- what used to be the SQL `shared_files` table.

One document per share, keyed by its integer id (the id the share list and "revoke" already use, kept
across the migration). The share TOKEN is the secret in the public URL (`/api/files/shared/<token>`):
it lives only inside the encrypted document, never in a d-tag and never in a log line.

The old rules, unchanged: a share answers only while `is_active`; an `expires_at` in the past or an
`access_count` that reached `max_accesses` deactivates it; every served download counts. What changes
is that the count is a relay write: for a share WITH a limit the write must land BEFORE the file is
served (a count that did not land would let the limit be exceeded), while an unlimited share's count is
bookkeeping and never blocks the download. A table that cannot be read raises
`relay_reader.Unavailable` -- the link answers "try again" (503), never "not found".
"""
from __future__ import annotations

import asyncio
import hmac
from datetime import datetime

from app.services import table_migrate as tm
from app.services.doc_table import DocTable
from app.services.table_migrate import Row

TABLE = "shared_files"
_FIELDS = {"id": 0, "user_id": 0, "token": "", "file_path": "", "filename": "", "created_at": None,
           "expires_at": None, "access_count": 0, "max_accesses": None, "is_active": True}
_count_locks: dict = {}


def _row(r) -> Row:
    return Row({**_FIELDS, **(r or {})})


def _by_token(rows, token: str):
    if not token:
        return None
    t = token.encode("utf-8", "surrogatepass")
    hit = None
    for _k, r in rows:
        v = str(r.get("token") or "").encode("utf-8", "surrogatepass")
        if v and hmac.compare_digest(v, t) and r.get("is_active") is True:
            hit = r
    return _row(hit) if hit is not None else None


async def aactive_by_token(token: str) -> Row | None:
    """The ACTIVE share with this token, or None. Raises Unavailable."""
    return _by_token(await tm.aview(TABLE), token)


def expired(share) -> bool:
    exp = tm.dt(share.get("expires_at"))
    return bool(exp and exp < datetime.utcnow())


def limit_reached(share) -> bool:
    return bool(share.get("max_accesses") and int(share.get("access_count") or 0) >= int(share["max_accesses"]))


async def adeactivate(share) -> None:
    cur = await DocTable(TABLE).aget(str(share["id"]))
    if cur is not None and cur.get("is_active") is not False:
        await DocTable(TABLE).aput(str(share["id"]), {**cur, "is_active": False})


async def acount_access(share) -> bool:
    """Count one served download. Returns False when the share's limit was reached in the meantime
    (do not serve). Raises Unavailable when a LIMITED share's count could not be stored."""
    sid = str(share["id"])
    lock = _count_locks.setdefault((id(asyncio.get_running_loop()), sid), asyncio.Lock())
    async with lock:
        cur = await DocTable(TABLE).aget(sid)
        if cur is None or cur.get("is_active") is not True:
            return False
        if limit_reached(cur):
            return False
        new = {**cur, "access_count": int(cur.get("access_count") or 0) + 1}
        try:
            await DocTable(TABLE).aput(sid, new)
        except Exception:       # noqa: BLE001
            if cur.get("max_accesses"):
                raise
        return True


async def acreate(*, user_id: int, token: str, file_path: str, filename: str, expires_at,
                  max_accesses) -> Row:
    row = {"id": None, "user_id": int(user_id), "token": token, "file_path": file_path, "filename": filename,
           "created_at": tm.iso(datetime.utcnow()), "expires_at": tm.iso(expires_at), "access_count": 0,
           "max_accesses": max_accesses, "is_active": True}
    sid = await DocTable(TABLE).ainsert(row, lambda: tm.anext_id(TABLE))   # SQL's id until the marker exists
    row["id"] = int(sid)
    return _row(row)


async def aactive_for_user(user_id: int) -> list:
    rows = [_row(r) for _k, r in await tm.aview(TABLE)
            if r.get("user_id") == user_id and r.get("is_active") is True]
    return sorted(rows, key=lambda r: (r.created_at or "", int(r.id or 0)), reverse=True)


async def aget_for_user(share_id: int, user_id: int) -> Row | None:
    r = await DocTable(TABLE).aget(str(int(share_id)))
    return _row(r) if r is not None and r.get("user_id") == user_id else None


def delete_for_user(user_id: int) -> int:
    """Remove every share of a deleted account (the SQL foreign key used to cascade)."""
    n = 0
    for k, r in tm.view(TABLE):
        if r.get("user_id") == user_id:
            DocTable(TABLE).delete(k)
            n += 1
    return n


# ------------------------------------------------------------------------------------ the SQL side (#161 wave 1)
def _share_from_sql(s) -> dict:
    return {"id": int(s.id), "user_id": int(s.user_id), "token": s.token, "file_path": s.file_path,
            "filename": s.filename, "created_at": tm.iso(s.created_at),
            "expires_at": tm.iso(s.expires_at), "access_count": int(s.access_count or 0),
            "max_accesses": s.max_accesses,
            # NULL never matched `is_active == True`: it stays inactive
            "is_active": s.is_active is True}


def rows_from_sql(db) -> dict:
    from app.models import SharedFile
    return {str(s.id): _share_from_sql(s) for s in db.query(SharedFile).all()}


def _legacy_from_sql():
    from app.services.legacy_sql import ModelLegacy
    return ModelLegacy(TABLE, "SharedFile", _share_from_sql)


def _register_legacy():
    from app.services import table_migration
    table_migration.register(_legacy_from_sql())


_register_legacy()

