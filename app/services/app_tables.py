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
  * `ready()`         -- whether a table may be read yet: its one-time SQL->DocTable migration has a marker, or
                         this process has finished its migration pass. Until then a reader answers "unavailable",
                         never a list that is missing the rows still sitting in Postgres.
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
from app.services.relay_reader import Unavailable

logger = logging.getLogger(__name__)

REMINDERS = "reminders"
SCHEDULED_POSTS = "scheduled_posts"
SAVED_SEARCHES = "saved_searches"
SOCIAL_REPLY_MAP = "social_reply_map"
TABLES = (REMINDERS, SCHEDULED_POSTS, SAVED_SEARCHES, SOCIAL_REPLY_MAP)
MIGRATED = "_migrated"          # marker documents, one per migrated table

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


# --------------------------------------------------------------------------- migration gate
_pass_done: set = set()        # tables this process finished a migration pass for (any outcome)


def mark_pass_done(table: str) -> None:
    _pass_done.add(table)


async def ready(table: str) -> bool:
    """True once the table's migration marker exists or this process finished its pass. Raises Unavailable
    when the marker cannot be read -- "could not ask" is not "not migrated" and not "migrated" either."""
    if table in _pass_done:
        return True
    return (await DocTable(MIGRATED).aget(table)) is not None


async def table(name: str) -> DocTable:
    """The DocTable for `name`, refused (Unavailable) until it is ready to be read."""
    if not await ready(name):
        raise Unavailable("the %s table is still being moved to the relay -- try again shortly" % name)
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
