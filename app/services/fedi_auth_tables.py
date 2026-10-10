"""The six app tables moved from Postgres to DocTables in #161 batch "d": the fediverse puppet registry,
the delivered-notes ledger, external storage mounts, API keys, share links and verification tokens.

They migrate through the ONE engine and registry (app/services/table_migration.py), at port-3051 startup
and from `scripts/migrate_tables_to_nostr.py --table <name>`; until a table's marker exists SQL is its store of
record (its Legacy, in each store module). This module keeps the per-batch entry points its callers and tests
were written against, reading SQL through a GIVEN session.
"""
from __future__ import annotations

import logging

from app.services import (api_key_store, external_storage_store, fedi_tables, share_store,
                          verification_store)

logger = logging.getLogger(__name__)

TABLES = (fedi_tables.PUPPETS, fedi_tables.LEDGER, external_storage_store.TABLE, api_key_store.TABLE,
          share_store.TABLE, verification_store.TABLE)


async def migrate_one(db, name: str) -> dict:
    """Copy one table reading SQL through `db` (table_migration.migrate_table): {table, status, rows, copied}."""
    from app.services import table_migration
    if name not in TABLES:
        raise KeyError(name)
    out = await table_migration.migrate_table(name, db)
    if out.get("skipped"):
        return {"table": name, "status": "already", "rows": out.get("rows")}
    return {"table": name, "status": "migrated", "rows": out.get("rows"), "copied": out.get("copied")}


MIGRATIONS = {name: (lambda n: (lambda db: migrate_one(db, n)))(name) for name in TABLES}


async def migrate_all(db) -> dict:
    """{table: result-or-error}. Never raises: each table is logged and left as it is (on SQL) on failure."""
    out = {}
    for name in TABLES:
        try:
            out[name] = await migrate_one(db, name)
        except Exception as e:      # noqa: BLE001 -- that table stays on SQL
            logger.error("[tables] %s NOT migrated (it stays on SQL): %s: %s", name, type(e).__name__, e)
            out[name] = {"table": name, "status": "error", "error": type(e).__name__}
    return out


async def preload() -> None:
    """Kept for old callers: table_migration.start_loading loads every table on its own thread now."""
    return None
