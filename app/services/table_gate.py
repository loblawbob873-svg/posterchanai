"""Read helpers shared by the relay-only app tables (#161).

Every app table is a DocTable on this node's relay from the first request -- there is no SQL side and no marker
gate any more (the one-time copy of a node's old Postgres rows is scripts/migrate_tables_to_nostr.py). What is left
here is the one rule the tables share for reading while a restart is still loading them:

  * a POINT read (`aget_row` / `get_row`) answers from memory once the table is loaded, and before that reads the
    ONE document from the relay -- a lookup never waits for, or fails on, a big table's load;
  * a WHOLE-TABLE read (`arows` / `rows`) needs the table: it loads it (strictly) or raises Unavailable/Loading,
    never answers from part of it.
"""
import asyncio
import logging

from app.services.doc_table import DocTable, _in_loop, _run
from app.services.relay_reader import Unavailable

logger = logging.getLogger(__name__)


def _load_soon(t: DocTable) -> None:
    """Start loading `t` on its own thread (a synchronous caller on the loop could only be told "not yet")."""
    t.start_background_load()


async def aget_row(t: DocTable, k):
    """One row by key: from memory once the table is loaded, otherwise ONE strict relay read. None = the relay
    answered and holds no such row."""
    if t.loaded:
        row = t.peek(k)
        return dict(row) if row is not None else None
    return await t.aget_remote(k)


def get_row(t: DocTable, k):
    """`aget_row` for synchronous code: on the event loop only memory can answer (Unavailable until loaded)."""
    if t.loaded:
        row = t.peek(k)
        return dict(row) if row is not None else None
    if _in_loop():
        # ONE ROW NEEDS ONE RELAY READ, not the whole table (2026-10-10: mail sync answered 503 for 10+ minutes
        # while the whole-table load kept timing out). Read just this document on a helper thread, bounded;
        # "could not ask" still raises Unavailable. The background load still starts.
        t.start_background_load()
        import concurrent.futures
        ex = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        fut = ex.submit(lambda: asyncio.run(t.aget_remote(k)))
        try:
            return fut.result(timeout=5.0)
        except concurrent.futures.TimeoutError:
            raise Unavailable("table %s is still loading" % t.name)
        finally:
            ex.shutdown(wait=False)         # never hold the loop past the bound for a slow read
    return _run(t.aget_remote(k))


async def arows(t: DocTable) -> list:
    """[(key, row)] for the whole table, loading it (strictly) when this process has not yet. Rows are COPIES."""
    await t._ensure()
    return [(k, dict(r)) for k, r in t.rows_view()]


def rows(t: DocTable) -> list:
    if not t.loaded and _in_loop():
        _load_soon(t)
        raise Unavailable("table %s is still loading" % t.name)
    t._ensure_sync()
    return [(k, dict(r)) for k, r in t.rows_view()]
