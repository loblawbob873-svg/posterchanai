"""One-time copy of the wave-3 app tables from Postgres into their DocTables (#161): the Exodus wallet tables, the
instance-name applications, and then `users` (with its lookup index).

Same engine and the same promises as wave 2 (`doc_table_bulk.amigrate_rows`): copy what differs, remove documents
no SQL row accounts for, RE-READ strictly and compare row for row, and only then write the `_migrated` marker; the
SQL rows are only ever SELECTed, nothing is dropped, and a table that changed while it was being copied gets no
marker (the next pass tries again).

`users` LAST, and only once every table that points at it has moved. A users.id is referenced by a foreign key
from every per-account table; once `users` is relay-authoritative a NEW account has no SQL row, so any of those
tables still taking SQL writes would refuse that account's first row with a foreign-key error. The dependents are
read from the model metadata (not a hand list that a new table would be missing from) and each must carry a
verified marker -- wave 1's and wave 2's tables are marked by their own migrations, the Exodus tables by this one.

The index (`user_index`) is derived from the SAME SQL snapshot as the accounts and copied first; the accounts'
marker is only written when SQL still equals that snapshot, so the index can never describe a different table
than the one that was marked. An index marker left by a pass whose accounts did not verify is cleared and the
index re-copied: nothing gates on it, it only records that a copy happened.
"""
import asyncio
import logging

from app.services import doc_table_bulk
from app.services.doc_table import DocTable
from app.services.relay_reader import Unavailable

logger = logging.getLogger(__name__)

USERS = "users"
INDEX = "user_index"

# SQL table names whose DocTable (and so marker) is named differently (wave 1's registry ALIASES).
MARKER_NAMES = {"push_subscriptions": "push_subs", "direct_push_messages": "direct_push_msgs",
                "blossom_blob_owners": "blossom_blobs", "external_storage_users": "external_storage"}


class NotYet(RuntimeError):
    """A table this one depends on has not moved yet -- retried, not an error."""


def _sources():
    from app.services import exodus_tables, instance_welcome
    return {exodus_tables.WALLETS: exodus_tables.sql_wallet_rows,
            exodus_tables.RECORDS: exodus_tables.sql_record_rows,
            instance_welcome.TABLE: instance_welcome.sql_rows}


def dependents() -> list:
    """Marker names of every table holding a foreign key to users.id."""
    from app.database import Base
    import app.models  # noqa: F401 -- registers every table
    assert app.models.User.__tablename__ == USERS
    out = set()
    for t in Base.metadata.tables.values():
        if t.name == USERS:
            continue
        for fk in t.foreign_keys:
            if fk.column.table.name == USERS:
                out.add(MARKER_NAMES.get(t.name, t.name))
    return sorted(out)


def _read(session_factory, fn) -> dict:
    db = session_factory()
    try:
        return {str(k): doc_table_bulk._norm(v) for k, v in fn(db).items()}
    finally:
        try:
            db.rollback()
        finally:
            db.close()


async def migrate_table(name: str, session_factory) -> dict:
    fn = _sources()[name]
    m = await doc_table_bulk.amarker(name)
    if m and m.get("verified"):
        doc_table_bulk._seen.add(name)
        return {"table": name, "skipped": True}
    rows = await asyncio.to_thread(_read, session_factory, fn)

    async def unchanged() -> bool:
        return (await asyncio.to_thread(_read, session_factory, fn)) == rows

    return await doc_table_bulk.amigrate_rows(name, rows, before_mark=unchanged)


async def _unmigrated_dependents() -> list:
    waiting = []
    for name in dependents():
        m = await doc_table_bulk.amarker(name)
        if not (m and m.get("verified")):
            waiting.append(name)
    return waiting


async def migrate_users(session_factory) -> dict:
    """Copy the accounts (and their index) once. Raises NotYet while a dependent table is still on SQL,
    MigrationMismatch (no marker) or Unavailable."""
    from app.services import user_table
    m = await doc_table_bulk.amarker(USERS)
    if m and m.get("verified"):
        doc_table_bulk._seen.add(USERS)
        return {"table": USERS, "skipped": True}
    waiting = await _unmigrated_dependents()
    if waiting:
        raise NotYet("waiting for %s to move first" % ", ".join(waiting))
    rows = await asyncio.to_thread(_read, session_factory, user_table.sql_rows)
    index = {k: doc_table_bulk._norm(v) for k, v in user_table.index_rows(rows).items()}
    if await doc_table_bulk.amarker(INDEX):
        await DocTable(doc_table_bulk.MARKER_TABLE).adelete(INDEX)     # from a pass whose accounts did not verify
        doc_table_bulk._seen.discard(INDEX)
    await doc_table_bulk.amigrate_rows(INDEX, index)

    async def unchanged() -> bool:
        return (await asyncio.to_thread(_read, session_factory, user_table.sql_rows)) == rows

    return await doc_table_bulk.amigrate_rows(USERS, rows, before_mark=unchanged,
                                              extra={"index_entries": len(index)})


async def migrate_all(session_factory, names=None) -> dict:
    """Every wave-3 table in turn (the accounts last). {name: report | error string}."""
    out = {}
    for name in names or (tuple(_sources()) + (USERS,)):
        try:
            out[name] = await (migrate_users(session_factory) if name == USERS
                               else migrate_table(name, session_factory))
        except NotYet as e:
            out[name] = "not yet: %s" % e
            logger.info("[wave3-migration] %s %s", name, e)
        except doc_table_bulk.MigrationMismatch as e:
            out[name] = "did not verify: %s" % e
            logger.error("[wave3-migration] %s", e)
        except Exception as e:      # noqa: BLE001 -- Unavailable, a SQL error: no verdict, retried
            out[name] = "not migrated: %s" % e
            logger.warning("[wave3-migration] %s not migrated yet: %s", name, type(e).__name__)
    return out


def _done(report) -> bool:
    return isinstance(report, dict) and bool(report.get("skipped") or report.get("verified"))


async def run_at_startup(session_factory, retry_s: float = 30.0) -> dict:
    """The port-3051 startup pass: migrate, retried until every table has its marker, then load each."""
    pending = list(tuple(_sources()) + (USERS,))
    out = {}
    while pending:
        out.update(await migrate_all(session_factory, pending))
        for name in [n for n in pending if _done(out.get(n))]:
            pending.remove(name)
            try:
                await DocTable(name).aload()
            except Unavailable as e:
                logger.warning("[wave3-migration] %s migrated but not loaded yet: %s", name, e)
        if pending:
            await asyncio.sleep(retry_s)
    return out
