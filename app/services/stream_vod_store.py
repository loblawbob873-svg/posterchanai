"""Saved live-stream recordings (`stream_vods`) as Nostr documents (#161).

`DocTable("stream_vods")` row `<token>:<started_at>` -- the old unique (token, started_at) index IS the key, so
a finalize retried after an end-event re-delivery finds its own row and writes nothing (the idempotency the SQL
unique index gave). Fields as the SQL row: id, user_id, pubkey, token, sha256, mime, size, duration_s, title,
started_at, created_at. A small table (one row per recorded broadcast), so it is simply held whole.

Relay-only (no SQL side; a node's old rows are copied once by scripts/migrate_tables_to_nostr.py).
"""
import time

from app.services.doc_table import DocTable
from app.services.relay_reader import Unavailable

TABLE = "stream_vods"
_LIMIT = 200


class NotMigrated(Unavailable):
    """Kept for callers that catch it; nothing raises it (there is no SQL side)."""


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
    await table().ainsert(row, lambda: vod_key(token, started_at))
    return row


