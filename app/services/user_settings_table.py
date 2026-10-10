"""Per-user key/value settings (the old `user_settings` table) as a DocTable on this node's relay (#161, wave 2).

One row per (user, key): document `pcai:t:user_settings:<user_id>:<key>`, row `{"user_id", "key", "value"}`,
NIP-44-encrypted to the operator key like every DocTable. Values include secrets -- mail passwords inside
`mail_accounts`, a legacy user's `storage_nsec`, stream tokens, a parked pre-signed stream-end event -- so nothing
here ever logs a value (or a key's value-shaped neighbour); only key NAMES and counts.

Relay-only: there is no SQL side (a node's old rows are copied once by scripts/migrate_tables_to_nostr.py).

Every reader that cannot ask raises Unavailable -- a settings row that could not be read is never "not set": that
would, for instance, mint a NEW stream key over the one a user's OBS is configured with, or forget which follows
were already announced and announce them all again.

The sync functions are for synchronous code OFF the event loop (threadpool routes, scheduler threads); async code
uses the `a*` versions. A sync call on the event loop is answered from memory once the table is loaded and raises
Unavailable before that (it can never block the loop on a socket).
"""
import logging

from app.services import table_gate
from app.services.doc_table import DocTable

logger = logging.getLogger(__name__)

TABLE = "user_settings"


def _t() -> DocTable:
    return DocTable(TABLE)


def row_key(user_id, key: str) -> str:
    return "%d:%s" % (int(user_id), key)


def _row(user_id, key, value) -> dict:
    return {"user_id": int(user_id), "key": str(key), "value": value}


# ------------------------------------------------------------------ reads
def get(db, user_id, key: str, default=None):
    """The value stored for (user, key), or `default` when there is no such row."""
    row = table_gate.get_row(_t(), row_key(user_id, key))
    return row.get("value") if row is not None else default


async def aget(db, user_id, key: str, default=None):
    row = await table_gate.aget_row(_t(), row_key(user_id, key))
    return row.get("value") if row is not None else default


def has(db, user_id, key: str) -> bool:
    return get(db, user_id, key, _MISSING) is not _MISSING


async def ahas(db, user_id, key: str) -> bool:
    return (await aget(db, user_id, key, _MISSING)) is not _MISSING


_MISSING = object()


def _for_user_rows(rows, user_id) -> dict:
    uid = int(user_id)
    return {r["key"]: r.get("value") for _k, r in rows if r.get("user_id") == uid}


def for_user(db, user_id) -> dict:
    """{key: value} for one user."""
    return _for_user_rows(table_gate.rows(_t()), user_id)


async def afor_user(db, user_id) -> dict:
    return _for_user_rows(await table_gate.arows(_t()), user_id)


def _by_key_rows(rows, key) -> dict:
    return {int(r["user_id"]): r.get("value") for _k, r in rows if r.get("key") == key}


def by_key(db, key: str) -> dict:
    """{user_id: value} for every user holding `key`."""
    return _by_key_rows(table_gate.rows(_t()), key)


async def aby_key(db, key: str) -> dict:
    return _by_key_rows(await table_gate.arows(_t()), key)


def find_user(db, key: str, value) -> int | None:
    """The user whose `key` is exactly `value` (a token lookup), or None."""
    for uid, v in by_key(db, key).items():
        if v == value:
            return uid
    return None


async def afind_user(db, key: str, value) -> int | None:
    for uid, v in (await aby_key(db, key)).items():
        if v == value:
            return uid
    return None


# ------------------------------------------------------------------ writes (relay only)
async def aset(db, user_id, key: str, value) -> None:
    value = None if value is None else str(value)
    await _t().aput(row_key(user_id, key), _row(user_id, key, value))


def set(db, user_id, key: str, value) -> None:   # noqa: A001 -- the module's verb, called as user_settings_table.set
    value = None if value is None else str(value)
    _t().put(row_key(user_id, key), _row(user_id, key, value))


async def adelete(db, user_id, key: str) -> None:
    await _t().adelete(row_key(user_id, key))


def delete(db, user_id, key: str) -> None:
    _t().delete(row_key(user_id, key))


async def apurge_user(db, user_id) -> int:
    """Every setting of a deleted account (the old FK cascade). Returns the rows removed."""
    n = 0
    t = _t()
    for k, _r in [(k, r) for k, r in await table_gate.arows(t) if r.get("user_id") == int(user_id)]:
        await t.adelete(k)
        n += 1
    return n


def purge_user(db, user_id) -> int:
    n = 0
    t = _t()
    for k, _r in [(k, r) for k, r in table_gate.rows(t) if r.get("user_id") == int(user_id)]:
        t.delete(k)
        n += 1
    return n
