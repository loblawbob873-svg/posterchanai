"""Which store answers for a table that is being moved off Postgres while the app keeps running (#161, wave 2:
bots, user_settings, conversations).

A wave-2 table lives in TWO stores during the move, and the `_migrated` marker (doc_table_bulk) decides which one
is the truth:

  * NO MARKER -- SQL is authoritative. Reads come from SQL; every write goes to BOTH, the relay document FIRST and
    the SQL commit after it. Relay-first is what makes the copy safe to run beside live traffic: a write either
    lands on the relay before SQL commits (so a copy that read the older SQL row sees SQL change underneath it
    and refuses -- `doc_table_bulk.amigrate_rows(before_mark=...)`), or it fails before SQL is touched.
  * MARKER -- the relay is authoritative. SQL is never read or written again (the table is kept, not dropped).

`arelay_mode()` answers which, and it answers it STRICTLY: a marker that cannot be read raises Unavailable. "Could
not ask" is neither "not migrated" (that would serve a stale SQL row after the move) nor "migrated" (that would
serve an empty DocTable before it). Once a process has seen the marker it never asks again (`_seen` is monotonic).

The point reads below are the other half of "never empty for could-not-ask": while a big table is still loading
after a restart, a lookup by key reads that ONE document from the relay instead of failing or waiting for the load.
"""
import logging

from app.services import doc_table_bulk
from app.services.doc_table import DocTable, _in_loop
from app.services.relay_reader import Unavailable

logger = logging.getLogger(__name__)

MARKER_TABLE = doc_table_bulk.MARKER_TABLE


_loading: set = set()


def _load_soon(t: DocTable) -> None:
    """Start a strict load of `t` on the running loop (once at a time), for a synchronous caller on the loop
    that could only be told "not yet": the next call is then answered from memory."""
    import asyncio
    if t.name in _loading:
        return

    async def _go():
        try:
            await t.aload()
        except Exception as e:      # noqa: BLE001 -- the caller already got Unavailable; the next one retries
            logger.debug("[table-gate] background load of %s failed: %s", t.name, e)
        finally:
            _loading.discard(t.name)
    _loading.add(t.name)
    task = asyncio.get_running_loop().create_task(_go())
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


_tasks: set = set()


def _verified(m) -> bool:
    return isinstance(m, dict) and bool(m.get("verified"))


async def arelay_mode(table: str) -> bool:
    """True once `table` has a verified `_migrated` marker. Raises Unavailable when the marker cannot be read."""
    if table in doc_table_bulk._seen:
        return True
    m = await DocTable(MARKER_TABLE).aget(table)
    if _verified(m):
        doc_table_bulk._seen.add(table)
        return True
    return False


def relay_mode(table: str) -> bool:
    """`arelay_mode` for synchronous code. Off the event loop it may load the (small) marker table; ON the loop it
    can only answer from memory, and raises Unavailable until the markers have been loaded once."""
    if table in doc_table_bulk._seen:
        return True
    markers = DocTable(MARKER_TABLE)
    if _in_loop():
        if not markers.loaded:
            _load_soon(markers)
            raise Unavailable("the table-migration markers are not loaded yet")
        m = markers.peek(table)
    else:
        m = markers.get(table)
    if _verified(m):
        doc_table_bulk._seen.add(table)
        return True
    return False


# ------------------------------------------------------------------ relay-mode reads
async def aget_row(t: DocTable, k):
    """One row by key: from memory once the table is loaded, otherwise ONE strict relay read (a restart's load
    of a big table must not turn a lookup into a failure). None = the relay answered and holds no such row."""
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
        _load_soon(t)
        raise Unavailable("table %s is still loading" % t.name)
    return t.get(k)


async def arows(t: DocTable) -> list:
    """[(key, row)] for the whole table, loading it (strictly) when this process has not yet. Rows are COPIES."""
    await t._ensure()               # the strict first load, and the periodic resync after it
    return [(k, dict(r)) for k, r in t.rows_view()]


def rows(t: DocTable) -> list:
    if not t.loaded and _in_loop():
        _load_soon(t)
        raise Unavailable("table %s is still loading" % t.name)
    t._ensure_sync()
    return [(k, dict(r)) for k, r in t.rows_view()]
