"""Small row helpers the relay-only app tables share (#161), and their integer ids.

Every one of these tables is a DocTable on this node's relay from the first request (no SQL side; a node's old
Postgres rows are copied once by scripts/migrate_tables_to_nostr.py). "Could not ask" raises Unavailable --
never an empty answer: an API key that "does not exist" is a refused user, a puppet that "does not exist" gets a
second identity minted beside the real one, a delivered-note ledger that "is empty" delivers everything twice.
"""
from __future__ import annotations

import asyncio
import json
import logging
import threading
from datetime import datetime

from app.services.doc_table import DocTable

logger = logging.getLogger(__name__)

MARKERS = "_migrated"
SEQ = "_seq"


class Row(dict):
    """A table row (a plain dict, which is what is stored) that also reads like the ORM row it replaced."""

    def __getattr__(self, k):
        try:
            return self[k]
        except KeyError:
            raise AttributeError(k) from None

    def __setattr__(self, k, v):
        self[k] = v


def iso(dt) -> str | None:
    """A datetime as it is stored (naive UTC ISO-8601, which is also what FastAPI used to answer)."""
    if dt is None:
        return None
    if isinstance(dt, str):
        return dt
    return dt.isoformat()


def dt(value) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value
    try:
        d = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    if d.tzinfo is not None:            # compared with naive utcnow() everywhere: normalise to naive UTC
        from datetime import timezone
        d = d.astimezone(timezone.utc).replace(tzinfo=None)
    return d


def canon(row: dict) -> dict:
    """Exactly what DocTable stores for `row` (its JSON round trip)."""
    return json.loads(json.dumps(row, default=str))


# ------------------------------------------------------------------------------------ reads
# Names this batch's stores were written against (there is no readiness gate: the relay is the only store).

def require(name: str) -> None:
    return None



def view(name: str) -> list:
    """[(key, row)] of the WHOLE table without copying the rows (callers read, never mutate them). Sync code off
    the loop loads the table if it must; on an event loop -- or while the background load runs -- an unloaded
    table is Unavailable (Loading), never a blocked loop."""
    return DocTable(name).view()


def row(name: str, k) -> dict | None:
    """ONE row by key (a copy): memory once loaded, the relay's one document while the table loads (off the loop)."""
    return DocTable(name).get(k)


async def aview(name: str) -> list:
    return await DocTable(name).aview()



# ------------------------------------------------------------------------------------ ids
_seq_locks: dict = {}
_seq_thread_lock = threading.Lock()


async def anext_id(name: str) -> int:
    """A fresh integer id for a table keyed by integer ids: above every id the table holds (its high-water mark in
    `_seq`, and the loaded rows when this process has them), persisted BEFORE it is handed out, and confirmed free
    with a point read -- so it never collides, even while the table is still loading. Migrated rows keep their old
    SQL ids; the copy script leaves no mark, so the first id is decided by the whole table."""
    lock = _seq_locks.setdefault((id(asyncio.get_running_loop()), name), asyncio.Lock())
    async with lock:
        cur = await DocTable(SEQ).aget(name)
        t = DocTable(name)
        if t.loaded or cur is None:
            rows = t.rows_view() if t.loaded else await t.aview()
            n = max([int((cur or {}).get("n") or 0)] + [int(k) for k, _r in rows if str(k).isdigit()])
        else:
            n = int(cur.get("n") or 0)
        n += 1
        while await t.aget(str(n)) is not None:
            n += 1
        await DocTable(SEQ).aput(name, {"n": n})
        return n


def next_id(name: str) -> int:
    from app.services import doc_table
    with _seq_thread_lock:
        return doc_table._run(anext_id(name))
