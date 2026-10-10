"""Per-user key/value settings (the old `user_settings` table) as a DocTable on this node's relay (#161, wave 2).

One row per (user, key): document `pcai:t:user_settings:<user_id>:<key>`, row `{"user_id", "key", "value"}`,
NIP-44-encrypted to the operator key like every DocTable. Values include secrets -- mail passwords inside
`mail_accounts`, a legacy user's `storage_nsec`, stream tokens, a parked pre-signed stream-end event -- so nothing
here ever logs a value (or a key's value-shaped neighbour); only key NAMES and counts.

Until the `user_settings` migration marker exists SQL is authoritative (table_gate): reads come from SQL, writes go
to the relay first and then SQL. After it the relay is authoritative and SQL is not touched.

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


# ------------------------------------------------------------------ SQL side (authoritative until the marker)
def _sql_get(db, user_id, key):
    from app.models import UserSetting
    return (db.query(UserSetting).filter(UserSetting.user_id == user_id, UserSetting.key == key)
            .order_by(UserSetting.id.desc()).first())   # the newest of any duplicates (see sql_rows)


def _sql_set(db, user_id, key, value) -> None:
    from app.models import UserSetting
    row = _sql_get(db, user_id, key)
    if row is None:
        db.add(UserSetting(user_id=user_id, key=key, value=value))
    else:
        row.value = value
    db.commit()


def _sql_delete(db, user_id, key) -> bool:
    from app.models import UserSetting
    n = db.query(UserSetting).filter(UserSetting.user_id == user_id, UserSetting.key == key).delete()
    db.commit()
    return bool(n)


def sql_rows(db) -> dict:
    """{row_key: row} for the whole SQL table -- the migration's source. Two SQL rows for one (user, key) (the
    table never had a unique constraint) collapse to the NEWEST (highest id): the one `.first()` readers got is
    unspecified, and the newest is the one the last save wrote."""
    from app.models import UserSetting
    out = {}
    for r in db.query(UserSetting).order_by(UserSetting.id.asc()).all():
        out[row_key(r.user_id, r.key)] = _row(r.user_id, r.key, r.value)
    return out


def _sql_mode(db) -> bool:
    return not table_gate.relay_mode(TABLE)


async def _asql_mode(db) -> bool:
    return not await table_gate.arelay_mode(TABLE)


# ------------------------------------------------------------------ reads
def get(db, user_id, key: str, default=None):
    """The value stored for (user, key), or `default` when there is no such row."""
    if _sql_mode(db):
        row = _sql_get(db, user_id, key)
        return row.value if row is not None else default
    row = table_gate.get_row(_t(), row_key(user_id, key))
    return row.get("value") if row is not None else default


async def aget(db, user_id, key: str, default=None):
    if await _asql_mode(db):
        row = _sql_get(db, user_id, key)
        return row.value if row is not None else default
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
    if _sql_mode(db):
        return for_user_sql(db, user_id)
    return _for_user_rows(table_gate.rows(_t()), user_id)


async def afor_user(db, user_id) -> dict:
    if await _asql_mode(db):
        return for_user_sql(db, user_id)
    return _for_user_rows(await table_gate.arows(_t()), user_id)


def for_user_sql(db, user_id) -> dict:
    from app.models import UserSetting
    return {r.key: r.value for r in db.query(UserSetting).filter(UserSetting.user_id == user_id)
            .order_by(UserSetting.id.asc()).all()}


def _by_key_rows(rows, key) -> dict:
    return {int(r["user_id"]): r.get("value") for _k, r in rows if r.get("key") == key}


def by_key(db, key: str) -> dict:
    """{user_id: value} for every user holding `key`."""
    if _sql_mode(db):
        return _by_key_sql(db, key)
    return _by_key_rows(table_gate.rows(_t()), key)


async def aby_key(db, key: str) -> dict:
    if await _asql_mode(db):
        return _by_key_sql(db, key)
    return _by_key_rows(await table_gate.arows(_t()), key)


def _by_key_sql(db, key) -> dict:
    from app.models import UserSetting
    return {r.user_id: r.value for r in db.query(UserSetting).filter(UserSetting.key == key)
            .order_by(UserSetting.id.asc()).all()}


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


# ------------------------------------------------------------------ writes (relay first, then SQL before the marker)
async def aset(db, user_id, key: str, value) -> None:
    value = None if value is None else str(value)
    sql = await _asql_mode(db)
    await _t().aput(row_key(user_id, key), _row(user_id, key, value))
    if sql:
        _sql_set(db, user_id, key, value)


def set(db, user_id, key: str, value) -> None:   # noqa: A001 -- the module's verb, called as user_settings_table.set
    value = None if value is None else str(value)
    sql = _sql_mode(db)
    _t().put(row_key(user_id, key), _row(user_id, key, value))
    if sql:
        _sql_set(db, user_id, key, value)


async def adelete(db, user_id, key: str) -> None:
    sql = await _asql_mode(db)
    await _t().adelete(row_key(user_id, key))
    if sql:
        _sql_delete(db, user_id, key)


def delete(db, user_id, key: str) -> None:
    sql = _sql_mode(db)
    _t().delete(row_key(user_id, key))
    if sql:
        _sql_delete(db, user_id, key)


async def apurge_user(db, user_id) -> int:
    """Every setting of a deleted account (the old FK cascade). Returns the relay rows removed."""
    sql = await _asql_mode(db)
    n = 0
    t = _t()
    for k, _r in [(k, r) for k, r in await table_gate.arows(t) if r.get("user_id") == int(user_id)]:
        await t.adelete(k)
        n += 1
    if sql:
        from app.models import UserSetting
        db.query(UserSetting).filter(UserSetting.user_id == user_id).delete(synchronize_session=False)
        db.commit()
    return n


def purge_user(db, user_id) -> int:
    sql = _sql_mode(db)
    n = 0
    t = _t()
    for k, _r in [(k, r) for k, r in table_gate.rows(t) if r.get("user_id") == int(user_id)]:
        t.delete(k)
        n += 1
    if sql:
        from app.models import UserSetting
        db.query(UserSetting).filter(UserSetting.user_id == user_id).delete(synchronize_session=False)
        db.commit()
    return n


def delete_legacy_sql_rows(db, user_id) -> None:
    """A deleted account's SQL rows, in the caller's transaction (no commit) -- in EITHER mode, so the users
    row can go (old databases have no ON DELETE CASCADE) and no secret outlives its account in Postgres."""
    from app.models import UserSetting
    db.query(UserSetting).filter(UserSetting.user_id == int(user_id)).delete(synchronize_session=False)
