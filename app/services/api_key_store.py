"""Users' API keys (`sk-…`) as a DocTable (#161) -- what used to be the SQL `api_keys` table.

One document per key, keyed by its integer id (the id the client's API-key list already holds, kept
across the migration; new ids come from table_migrate.next_id and are never reused). The key itself
lives only INSIDE the document, which DocTable encrypts to the operator: it is never a d-tag (d-tags
are plaintext on the relay) and never logged.

Verification keeps the old rules exactly: a token matches an ACTIVE row with the same key, and
nothing else. What is new is the third answer: a table that cannot be read raises
`relay_reader.Unavailable`, and every caller turns that into "try again" (503) -- never into
"invalid API key", which would make an outage look like a revocation, and never into a pass.

AFTER A RESTART the table loads on a background thread; meanwhile a key is checked with two POINT reads -- the
`api_key_index` document named by sha256(key) (never the key itself: d-tags are plaintext) gives the id, the id's
document is compared in constant time -- so `sk-` keys keep working instead of answering 503 for the load. Relay-only: there is no SQL side (a node's old
rows, ids included, are copied once by scripts/migrate_tables_to_nostr.py, which also derives the index).

`last_used_at` is written at most every few minutes per key (it used to be an UPDATE per request;
here every write is a signed relay event). The per-user mirror under the user's storage key
(record_store NS_APIKEY) is retired with this table: the operator-signed table IS the copy a fresh
node rebuilds from.
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
import threading
from datetime import datetime, timedelta

from app.services import doc_table
from app.services import table_migrate as tm
from app.services.doc_table import DocTable
from app.services.table_migrate import Row

TABLE = "api_keys"
INDEX = "api_key_index"
TOUCH_EVERY = timedelta(minutes=5)

_FIELDS = {"id": 0, "user_id": 0, "key": "", "name": "Default", "created_at": None, "last_used_at": None,
           "is_active": True}
_lock = threading.Lock()
_touching: set = set()


def _row(r: dict) -> Row:
    return Row({**_FIELDS, **(r or {})})


def _match(rows, token: str):
    """The ACTIVE row whose key equals `token` (compared in constant time per row)."""
    if not token:
        return None
    t = token.encode("utf-8", "surrogatepass")
    hit = None
    for _k, r in rows:
        k = str(r.get("key") or "").encode("utf-8", "surrogatepass")
        if k and hmac.compare_digest(k, t) and r.get("is_active") is True and r.get("user_id"):
            hit = r
    return _row(hit) if hit is not None else None


def key_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8", "surrogatepass")).hexdigest()


async def _point_lookup(token: str):
    """The table is still loading: the index document names the id, the id's document is the row."""
    idx = await DocTable(INDEX).aget(key_hash(token))
    if not idx or idx.get("id") is None:
        return None
    r = await DocTable(TABLE).aget(str(int(idx["id"])))
    return _match([("", r)], token) if r is not None else None


async def alookup(token: str) -> Row | None:
    """The active key row for `token`, or None. Raises Unavailable (never "invalid" for "could not ask")."""
    if not token:
        return None
    t = DocTable(TABLE)
    if not t.loaded:
        t.start_background_load()
        return await _point_lookup(token)
    return _match(t.rows_view(), token)


def lookup(token: str) -> Row | None:
    """Synchronous `alookup` (a sync route runs on a worker thread)."""
    if not token:
        return None
    t = DocTable(TABLE)
    if t.loaded:
        return _match(t.rows_view(), token)
    if doc_table._in_loop():
        t.start_background_load()
        raise doc_table.Loading("API keys are still loading")
    return doc_table._run(alookup(token))


def _needs_touch(row) -> bool:
    last = tm.dt(row.get("last_used_at"))
    return last is None or datetime.utcnow() - last >= TOUCH_EVERY


def touch(row) -> None:
    """Record a use (throttled). Best effort: raises nothing a request should fail on."""
    if not row or not _needs_touch(row):
        return
    from app.services import doc_table
    if doc_table._in_loop():                # a sync caller on the event loop: never block it on a write
        import asyncio
        t = asyncio.get_running_loop().create_task(atouch(row))
        _touching.add(t)
        t.add_done_callback(_touching.discard)
        return
    try:
        cur = DocTable(TABLE).get(str(row["id"]))
        if cur is not None:
            DocTable(TABLE).put(str(row["id"]), {**cur, "last_used_at": tm.iso(datetime.utcnow())})
    except Exception:       # noqa: BLE001
        pass


async def atouch(row) -> None:
    if not row or not _needs_touch(row):
        return
    try:
        cur = await DocTable(TABLE).aget(str(row["id"]))
        if cur is not None:
            await DocTable(TABLE).aput(str(row["id"]), {**cur, "last_used_at": tm.iso(datetime.utcnow())})
    except Exception:       # noqa: BLE001
        pass


def for_user(user_id: int) -> list:
    rows = [_row(r) for _k, r in tm.view(TABLE) if r.get("user_id") == user_id]
    return sorted(rows, key=lambda r: int(r.id or 0))


def get_for_user(key_id: int, user_id: int) -> Row | None:
    """The key with this id IF it belongs to `user_id` (another user's id answers None, as before)."""
    r = tm.row(TABLE, str(int(key_id)))
    return _row(r) if r is not None and r.get("user_id") == user_id else None


def new_key() -> str:
    return f"sk-{secrets.token_hex(32)}"


async def acreate(user_id: int, name: str | None = None, key: str | None = None) -> Row:
    """A new active key. Raises Unavailable when it was not stored. The row first (a fresh id, never reused),
    then its index entry -- an index entry never points at a key nobody was given."""
    row = {"id": None, "user_id": int(user_id), "key": key or new_key(), "name": name or "Default",
           "created_at": tm.iso(datetime.utcnow()), "last_used_at": None, "is_active": True}
    kid = await DocTable(TABLE).ainsert(row, lambda: tm.anext_id(TABLE))
    row["id"] = int(kid)
    await DocTable(INDEX).aput(key_hash(row["key"]), {"id": int(kid)})
    return _row(row)


def create(user_id: int, name: str | None = None, key: str | None = None) -> Row:
    with _lock:
        return doc_table._run(acreate(user_id, name, key))


def set_active(key_id: int, active: bool) -> Row:
    cur = tm.row(TABLE, str(int(key_id)))
    if cur is None:
        raise KeyError(key_id)
    row = {**cur, "is_active": bool(active)}
    DocTable(TABLE).put(str(int(key_id)), row)
    return _row(row)


async def adelete(key_id: int) -> None:
    """Remove a key for good -- the row first (a lookup then finds nothing), then its index entry. Raises
    Unavailable unless the relay confirmed the deletion."""
    t = DocTable(TABLE)
    cur = await t.aget(str(int(key_id)))
    await t.adelete(str(int(key_id)))
    if cur and cur.get("key"):
        await DocTable(INDEX).adelete(key_hash(cur["key"]))


def delete(key_id: int) -> None:
    doc_table._run(adelete(key_id))


def delete_for_user(user_id: int) -> int:
    n = 0
    for k, r in list(tm.view(TABLE)):
        if r.get("user_id") == user_id:
            delete(int(k))
            n += 1
    return n


def active_named(user_id: int, name: str) -> Row | None:
    rows = [_row(r) for _k, r in tm.view(TABLE)
            if r.get("user_id") == user_id and r.get("name") == name and r.get("is_active") is True]
    return min(rows, key=lambda r: int(r.id or 0)) if rows else None


async def aactive_named(user_id: int, name: str) -> Row | None:
    rows = [_row(r) for _k, r in await tm.aview(TABLE)
            if r.get("user_id") == user_id and r.get("name") == name and r.get("is_active") is True]
    return min(rows, key=lambda r: int(r.id or 0)) if rows else None


def pick_key(db=None, *, user_id: int | None = None, admin: bool = False, active: bool = False,
             newest: bool = False) -> str | None:
    """A key for the dev scripts (which used to read the SQL table directly): by user, admins only,
    active only, lowest id first (or the newest). Sync; raises Unavailable."""
    rows = [_row(r) for _k, r in tm.view(TABLE)]
    if user_id is not None:
        rows = [r for r in rows if r.user_id == user_id]
    if active:
        rows = [r for r in rows if r.is_active is True]
    if admin:
        from app.services import user_table
        admins = {u.id for u in user_table.admins(db)}
        rows = [r for r in rows if r.user_id in admins]
    rows.sort(key=lambda r: int(r.id or 0), reverse=newest)
    return rows[0].key if rows else None
