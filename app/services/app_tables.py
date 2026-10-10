"""The small app tables that left Postgres for DocTable (#161, part A): reminders, scheduled posts, saved
searches (pins) and the Telegram social-reply map.

Shared plumbing only -- each feature's service owns its own rows:
  * `new_id()`        -- an integer id that stays unique ACROSS PROCESSES (the app, the worker and the torrent
                         thread all create reminders). The old ids came from a Postgres sequence; there is no
                         sequence now, so an id is milliseconds since 2026-01-01 x 100 + two random digits, kept
                         strictly increasing inside one process. Every legacy id is far below the first new one,
                         and both stay well inside a JavaScript number, so a client that already holds an id
                         (Telegram's `rem:cancel:<id>` buttons, the Drafts list) keeps working.
  * `row_lock()`      -- serialises read-check-write on the event loop (a reminder claim against a cancel), the
                         job the conditional `UPDATE ... WHERE status='pending'` used to do inside one process.
  * `table()`         -- the DocTable. Until its one-time SQL->DocTable migration has a marker, DocTable reads it
                         from SQL and writes SQL then the relay (table_migration binds the Legacy), so no reader
                         ever waits for the move and none is served a half-moved table.
  * `purge_user()`    -- the old `ON DELETE CASCADE`: a deleted account's rows go with it.
  * `to_iso()/from_iso()` -- naive-UTC datetimes as stored strings.
"""
import asyncio
import logging
import random
import threading
import time
import weakref
from datetime import datetime

from app.services.doc_table import DocTable

logger = logging.getLogger(__name__)

REMINDERS = "reminders"
SCHEDULED_POSTS = "scheduled_posts"
SAVED_SEARCHES = "saved_searches"
SOCIAL_REPLY_MAP = "social_reply_map"
TABLES = (REMINDERS, SCHEDULED_POSTS, SAVED_SEARCHES, SOCIAL_REPLY_MAP)
MIGRATED = "_migrated"          # marker documents, one per migrated table (doc_table.MARKERS)

_EPOCH_MS = 1767225600000       # 2026-01-01T00:00:00Z
_id_lock = threading.Lock()
_last_id = 0


def new_id(taken=()) -> int:
    """A fresh integer id, unique across processes (time + randomness) and increasing within this one."""
    global _last_id
    with _id_lock:
        while True:
            cand = max((int(time.time() * 1000) - _EPOCH_MS) * 100 + random.randrange(100), _last_id + 1)
            _last_id = cand
            if str(cand) not in taken:
                return cand


def to_iso(dt):
    return dt.isoformat() if isinstance(dt, datetime) else (dt or None)


def from_iso(v):
    if isinstance(v, datetime):
        return v
    try:
        return datetime.fromisoformat(v) if v else None
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------- read-check-write on the loop
_locks = weakref.WeakKeyDictionary()


def row_lock(table: str) -> asyncio.Lock:
    """One asyncio.Lock per (event loop, table). Every read-check-write of a row's status takes it."""
    loop = asyncio.get_running_loop()
    per = _locks.get(loop)
    if per is None:
        per = _locks[loop] = {}
    lk = per.get(table)
    if lk is None:
        lk = per[table] = asyncio.Lock()
    return lk


# --------------------------------------------------------------------------- the table
from app.services import doc_table as _doc_table  # noqa: E402
_pass_done = _doc_table._marked    # the markers this process has seen (marking here = "the relay is authoritative")


def mark_pass_done(table: str) -> None:
    _pass_done.add(table)


async def ready(table: str) -> bool:
    """Always True now: until a table's marker exists DocTable answers it from SQL (wave 1), so there is nothing
    for a reader or a poller to wait for. Kept for callers that still ask."""
    return True


async def table(name: str) -> DocTable:
    """The DocTable for `name`. Before its migration marker exists its reads come from SQL and its writes go to
    SQL and the relay (doc_table.Legacy) -- never a half-moved table, and never refused for being mid-move."""
    return DocTable(name)


# --------------------------------------------------------------------------- cascade
async def apurge_user(user_id) -> int:
    """Delete every row in these tables owned by `user_id` (the old FK cascade). Returns rows removed."""
    n = 0
    for name in TABLES:
        t = DocTable(name)
        for k, _row in await t.awhere(lambda r: r.get("user_id") == user_id):
            await t.adelete(k)
            n += 1
    return n


def purge_user(user_id) -> int:
    n = 0
    for name in TABLES:
        t = DocTable(name)
        for k, _row in t.where(lambda r: r.get("user_id") == user_id):
            t.delete(k)
            n += 1
    return n
