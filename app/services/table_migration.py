"""The ONE migration of the app tables from Postgres to DocTables (#161 wave 1): one registry, one engine, one
startup task.

REGISTRY. Every moved table's store module defines a `doc_table.Legacy` (how its rows are read and written in
SQL) and registers it here with `register()`. `bind()` imports those modules and attaches each Legacy to its
DocTable, which is what makes SQL the authoritative read path -- and SQL-then-relay the write path -- until the
table's `_migrated` marker exists. Every process that serves these tables calls `bind()` at start (the app, the
worker, the script), because a process that did not would read an incomplete relay copy as the table.

ENGINE. `amigrate(name)` is `doc_table_bulk.amigrate_rows` fed by the table's Legacy: copy what differs, re-read
both sides strictly, settle every differing key against a FRESH SQL read (the app keeps writing SQL meanwhile),
then the marker. Idempotent; a table that will not verify raises `MigrationMismatch` and stays on SQL -- with the
app fully working, because SQL is still what it reads.

STARTUP (port 3051):
  * `start_loading()`, as early as possible: binds, then loads every table whose marker exists on a thread of its
    own -- after a restart the big tables take a while, and meanwhile point reads go to the relay one document at
    a time and only whole-table reads answer 503 + Retry-After;
  * `start_background()`, once the startup hydrates have rebuilt the SQL side (record_store writes reminder and
    pin rows): copies every table that has no marker yet, on its own thread, retried until each has a verdict.

What it never does: drop a SQL table, delete or change a SQL row, or treat "the relay could not be asked" as
"nothing there" -- every read the engine decides from is strict.
"""
from __future__ import annotations

import asyncio
import importlib
import logging
import threading
import time

from app.services import doc_table
from app.services.doc_table import DocTable, MARKERS, Legacy
from app.services.doc_table_bulk import MigrationMismatch, amarker, amigrate_rows
from app.services.relay_reader import Unavailable

logger = logging.getLogger(__name__)

Mismatch = MigrationMismatch            # the name part A's callers and tests know

# The store modules whose import registers a Legacy, in migration order.
MODULES = (
    "app.services.reminder_service",
    "app.services.scheduled_posts_service",
    "app.services.saved_search_service",
    "app.services.social_notifications_service",
    "app.services.push_store",
    "app.services.blob_index",
    "app.services.stream_vod_store",
    "app.services.fedi_tables",
    "app.services.external_storage_store",
    "app.services.api_key_store",
    "app.services.share_store",
    "app.services.verification_store",
)

REGISTRY: dict = {}             # DocTable name -> Legacy, in registration order
# SQL table names that are not their DocTable's name (the script accepts either)
ALIASES = {"push_subscriptions": "push_subs", "direct_push_messages": "direct_push_msgs",
           "blossom_blob_owners": "blossom_blobs"}
SEQ = "_seq"                    # high-water marks of tables with integer ids (`next_id`)


def register(legacy: Legacy) -> Legacy:
    """Called by a store module at import: from then on its DocTable reads SQL until the marker exists -- in
    EVERY process that imports the store, so none can forget and read an incomplete relay copy as the table."""
    if not legacy.name:
        raise ValueError("a Legacy needs the DocTable name")
    REGISTRY[legacy.name] = legacy
    if legacy.copy:
        doc_table.LEGACIES[legacy.name] = legacy
        t = DocTable._registry.get(legacy.name)
        if t is not None:
            t.bind_legacy(legacy)
    return legacy


def load_registry() -> dict:
    for m in MODULES:
        importlib.import_module(m)
    return REGISTRY


def tables() -> list:
    return list(load_registry())


def legacy(name: str) -> Legacy:
    return load_registry()[ALIASES.get(name, name)]


def bind(session_factory=None) -> None:
    """Register every moved table (importing its store) and give this process its SQL sessions: until a table's
    marker exists it is read from SQL and written to SQL then the relay."""
    load_registry()
    if session_factory is not None:
        doc_table.set_session_factory(session_factory)
        # the markers, held and kept current: lets even synchronous code on an event loop tell SQL-or-relay
        DocTable(MARKERS).start_background_load()


# ------------------------------------------------------------------------------------------- the engine
async def amigrate(name: str, *, in_thread: bool = True) -> dict:
    """Copy table `name` from SQL into its DocTable, verify, mark. Raises MigrationMismatch (no marker) when it
    will not verify, Unavailable when the relay or SQL cannot be asked."""
    name = ALIASES.get(name, name)
    lg = legacy(name)
    if not lg.copy:
        m = await amarker(name)
        if doc_table.is_marker(m):
            doc_table._saw_marked(name)
            return dict(m, table=name, skipped=True)
        info = {"ok": True, "verified": True, "rows": 0, "at": int(time.time()),
                "skipped": getattr(lg, "why_not_copied", "not copied")}
        await DocTable(MARKERS)._relay_put(name, info)
        doc_table._saw_marked(name)
        return dict(info, table=name)

    def rows():
        return doc_table._sql(lg.rows)

    def point(k):
        return doc_table._sql(lambda db: lg.get(db, k))
    return await amigrate_rows(name, rows, point=point, in_thread=in_thread)


async def amigrate_all(names=None) -> dict:
    """Every table in turn; one table failing does not stop the others. {table: report | error string}."""
    out = {}
    for name in names or tables():
        try:
            out[name] = await amigrate(name)
            if not out[name].get("skipped"):
                logger.info("[table-migration] %s: %d row(s) verified (%s written, %s fixed) in %ss", name,
                            out[name].get("rows", 0), out[name].get("written", 0), out[name].get("fixed", 0),
                            out[name].get("total_s", "?"))
        except MigrationMismatch as e:
            out[name] = "did not verify: %s" % e
            logger.error("[table-migration] %s did not verify -- it stays on SQL: %s", name, e)
        except Exception as e:      # noqa: BLE001 -- Unavailable, a SQL error: no verdict, retried
            out[name] = "not migrated: %s: %s" % (type(e).__name__, e)
            logger.warning("[table-migration] %s not migrated yet: %s", name, e)
    return out


def migrate(name: str, *, in_thread: bool = True) -> dict:
    """Synchronous `amigrate` (the script, a thread)."""
    return doc_table._run(amigrate(name, in_thread=in_thread))


# --- the forms part A's callers were written against: one table, or several, from a GIVEN session ---
async def migrate_table(name: str, db) -> dict:
    """Copy `name` reading SQL through `db` (on this thread). The report counts what SQL held, what was written
    and what was already identical; raises MigrationMismatch / Unavailable like `amigrate`."""
    name = ALIASES.get(name, name)
    lg = legacy(name)
    out = await amigrate_rows(name, lambda: lg.rows(db), point=lambda k: lg.get(db, k), in_thread=False)
    if out.get("skipped"):
        return out
    return dict(out, sql_rows=out["rows"], copied=out["written"], already=out["rows"] - out["written"],
                verified=out["rows"])


async def migrate_all(db, names=None) -> dict:
    """Every table in `names` (default: part A's), one failing not stopping the others: {table: report | error
    string}. A table that did not verify is reported and stays on SQL."""
    from app.services import app_tables
    out = {}
    for name in names or app_tables.TABLES:
        try:
            out[name] = await migrate_table(name, db)
        except MigrationMismatch as e:
            out[name] = "did not verify: %s" % e
            logger.error("[table-migration] %s did not verify -- it stays on SQL: %s", name, e)
        except Exception as e:      # noqa: BLE001 -- Unavailable, a SQL error: no verdict, retried
            out[name] = "not migrated: %s" % e
            logger.warning("[table-migration] %s not migrated yet: %s", name, e)
    return out


async def run_at_startup(session_factory, retry_s: float = 30.0, names=None) -> dict:
    """`migrate_all` retried until every table is marked (a mismatch is retried too: the table stays on SQL,
    which keeps serving it, and the next pass may find the relay willing)."""
    from app.services import app_tables
    pending = list(names or app_tables.TABLES)
    out = {}
    while True:
        db = session_factory()
        try:
            out.update(await migrate_all(db, pending))
        finally:
            db.close()
        pending = [n for n in pending if not isinstance(out.get(n), dict)]
        if not pending:
            return out
        await asyncio.sleep(retry_s)


# ------------------------------------------------------------------------------------------- startup
_threads: dict = {}


def start_loading(session_factory=None) -> None:
    """Bind, then load the marker table and every moved table on threads of their own (a table with no marker
    yet waits for it -- SQL answers meanwhile). Idempotent."""
    bind(session_factory)
    DocTable(MARKERS).start_background_load()
    for name, lg in REGISTRY.items():
        if lg.copy:
            prepare = getattr(lg, "prepare", None)
            if prepare is not None:
                prepare()
            DocTable(name).start_background_load()


def _migrate_loop(retry_s: float) -> None:
    pending = tables()
    backoff = retry_s
    while pending:
        out = doc_table._run(amigrate_all(pending))
        pending = [n for n, r in out.items() if not isinstance(r, dict)]
        if pending:
            time.sleep(backoff)
            backoff = min(backoff * 2, 600.0)


def start_background(session_factory=None, *, retry_s: float = 30.0) -> None:
    """Port-3051 startup, after the hydrates that rebuild SQL rows: copy every table that has no marker yet, on a
    thread, retried until each one verifies. Nothing waits on it -- until a table's marker exists SQL answers."""
    start_loading(session_factory)
    th = _threads.get("migrate")
    if th is not None and th.is_alive():
        return
    th = threading.Thread(target=_migrate_loop, args=(retry_s,), name="table-migration", daemon=True)
    _threads["migrate"] = th
    th.start()


# ------------------------------------------------------------------------------------------- ids
async def anext_id(name: str) -> int:
    """A fresh integer id for an `int_ids` table once the relay is authoritative: above every id the table holds
    (its high-water mark, and the loaded rows when this process has them), persisted BEFORE it is handed out,
    and confirmed free with a point read -- so it never collides, even while the table is still loading."""
    lock = _seq_locks.setdefault((id(asyncio.get_running_loop()), name), asyncio.Lock())
    async with lock:
        cur = await DocTable(SEQ).aget(name)
        t = DocTable(name)
        if t.loaded or cur is None:
            # no high-water mark (a table copied before marks were kept): the whole table decides
            rows = t.rows_view() if t.loaded else await t.aview()
            n = max([int((cur or {}).get("n") or 0)] + [int(k) for k, _r in rows if str(k).isdigit()])
        else:
            n = int(cur.get("n") or 0)
        n += 1
        while await t.aget(str(n)) is not None:
            n += 1
        await DocTable(SEQ).aput(name, {"n": n})
        return n


_seq_locks: dict = {}
_seq_thread_lock = threading.Lock()


def next_id(name: str) -> int:
    with _seq_thread_lock:
        return doc_table._run(anext_id(name))
