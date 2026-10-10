"""Saved live-stream recordings (`stream_vods`) as Nostr documents (#161).

`DocTable("stream_vods")` row `<token>:<started_at>` -- the old unique (token, started_at) index IS the key, so
a finalize retried after an end-event re-delivery finds its own row and writes nothing (the idempotency the SQL
unique index gave). Fields as the SQL row: id, user_id, pubkey, token, sha256, mime, size, duration_s, title,
started_at, created_at. A small table (one row per recorded broadcast), so it is simply held whole.

Before the SQL rows are copied (no `_migrated` marker) SQL is still the store of record (#161 wave 1): DocTable reads
the SQL table through `_LEGACY` below and writes SQL first, then the relay -- never a listing missing the old
recordings, and nothing refused for being mid-move. A new recording then takes SQL's id.
"""
import time

from app.services.doc_table import DocTable
from app.services.relay_reader import Unavailable

TABLE = "stream_vods"
_LIMIT = 200


class NotMigrated(Unavailable):
    """Kept for callers that catch it; nothing raises it any more -- before the marker SQL answers."""


def table() -> DocTable:
    return DocTable(TABLE)


def vod_key(token: str, started_at: int) -> str:
    return "%s:%d" % (token, int(started_at))


def _newest(rows) -> list:
    return sorted(rows, key=lambda r: (int(r.get("started_at") or 0), int(r.get("id") or 0)), reverse=True)[:_LIMIT]


def _require_sync() -> DocTable:
    return table()


async def _require() -> DocTable:
    return table()


def for_user(user_id: int) -> list:
    """The user's recordings, newest first (≤200). Synchronous: called from a threadpool route."""
    return _newest(r for _k, r in _require_sync().where(lambda r: r.get("user_id") == user_id))


def for_token(token: str) -> list:
    return _newest(r for _k, r in _require_sync().where(lambda r: r.get("token") == token))


async def aexists(token: str, started_at: int) -> bool:
    return (await (await _require()).aget(vod_key(token, started_at))) is not None


async def aadd(*, user_id, pubkey, token, sha256, mime, size, duration_s, title, started_at) -> dict:
    now = int(time.time())
    row = {"id": int(time.time() * 1000), "user_id": user_id, "pubkey": pubkey, "token": token,
           "sha256": sha256, "mime": mime, "size": int(size), "duration_s": duration_s, "title": title,
           "started_at": int(started_at), "created_at": now}
    # SQL assigns the id until the marker exists (the Legacy writes it into the row); a ms id after.
    await table().ainsert(row, lambda: vod_key(token, started_at))
    return row


# ------------------------------------------------------------------ the SQL side (#161 wave 1)
def _vod_from_sql(v) -> dict:
    return {"id": v.id, "user_id": v.user_id, "pubkey": v.pubkey, "token": v.token, "sha256": v.sha256,
            "mime": v.mime, "size": int(v.size), "duration_s": v.duration_s, "title": v.title,
            "started_at": int(v.started_at), "created_at": int(v.created_at)}


def sql_rows(db) -> dict:
    from app.models import StreamVOD
    return {vod_key(v.token, v.started_at): _vod_from_sql(v)
            for v in db.query(StreamVOD).order_by(StreamVOD.id).yield_per(2000)}


def _find(db, k):
    from app.models import StreamVOD
    token, started = str(k).rsplit(":", 1)
    return db.query(StreamVOD).filter(StreamVOD.token == token, StreamVOD.started_at == int(started)).first()


def _vod_cols(row: dict) -> dict:
    return {c: row.get(c) for c in ("user_id", "pubkey", "token", "sha256", "mime", "size", "duration_s",
                                    "title", "started_at", "created_at")}


def _make_legacy():
    from app.services.legacy_sql import ModelLegacy
    return ModelLegacy(TABLE, "StreamVOD", _vod_from_sql, key_of=lambda v: vod_key(v.token, v.started_at),
                       find=_find, to_cols=_vod_cols)


_LEGACY = _make_legacy()


def _register_legacy():
    from app.services import table_migration
    table_migration.register(_LEGACY)


_register_legacy()


async def amigrate(db=None) -> dict:
    """The copy through the one engine -- from `db` when given (tests, a hand run), else table_migration's."""
    if db is None:
        from app.services import table_migration
        return await table_migration.amigrate(TABLE)
    from app.services.doc_table_bulk import amigrate_rows
    lg = _make_legacy()
    return await amigrate_rows(TABLE, lambda: sql_rows(db), point=lambda k: lg.get(db, k))


def migrate() -> dict:
    from app.services import table_migration
    return table_migration.migrate(TABLE)


def start_background_migration() -> None:
    """Kept for old callers: the copy is table_migration's startup task now."""
    return None
