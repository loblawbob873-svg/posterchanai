#!/usr/bin/env python3
"""Copy the wave-2 app tables from Postgres into their DocTables on this node's relay, once (#161): bots,
user_settings, conversations -- and take the read-only census of the legacy chat `messages` rows.

The app does this by itself at startup on the port-3051 instance (wave2_migration.run_at_startup); this is the
same code for running it by hand, checking a node, or reading why a table refused. Postgres is only ever read
(the session is put in a read-only transaction). Re-running is safe: a table with a verified `_migrated`
marker is skipped.

    venv-unified/bin/python scripts/migrate_wave2_tables.py --table user_settings
    venv-unified/bin/python scripts/migrate_wave2_tables.py --table all

Output is one JSON report per table: counts, timings and keys -- never a row's content (bot configs hold keys,
settings hold mail passwords, messages are private).

Exit codes: 0 migrated (or already), 1 a copy did not verify (no marker written), 2 could not run (relay or
database unreachable).
"""
import argparse
import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _read_only_factory(factory):
    def make():
        db = factory()
        try:
            if db.bind is not None and db.bind.dialect.name == "postgresql":
                from sqlalchemy import text
                db.execute(text("SET TRANSACTION READ ONLY"))
        except Exception:       # noqa: BLE001 -- the migration itself only ever SELECTs
            pass
        return db
    return make


async def _run(names, factory) -> int:
    from app.services import doc_table_bulk, wave2_migration
    from app.services.relay_reader import Unavailable
    worst = 0
    for name in names:
        try:
            if name == wave2_migration.MESSAGES:
                rep = await wave2_migration.amessage_census(factory)
            else:
                rep = await wave2_migration.migrate_table(name, factory)
            print(json.dumps(rep, sort_keys=True, default=str))
        except doc_table_bulk.MigrationMismatch as e:
            print(json.dumps({"table": name, "error": "did not verify; no marker written", "detail": str(e)}))
            worst = max(worst, 1)
        except Unavailable as e:
            print("%s: the relay could not be asked (%s)" % (name, e), file=sys.stderr)
            return 2
        except Exception as e:      # noqa: BLE001 -- the database could not be read
            print("%s: could not run (%s)" % (name, type(e).__name__), file=sys.stderr)
            return 2
    return worst


def main(argv=None, session_factory=None) -> int:
    from app.services.wave2_migration import MESSAGES, TABLES
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--table", required=True, choices=list(TABLES) + [MESSAGES, "all"])
    args = ap.parse_args(argv)
    if session_factory is None:
        from app.database import SessionLocal as session_factory
    names = list(TABLES) + [MESSAGES] if args.table == "all" else [args.table]
    return asyncio.run(_run(names, _read_only_factory(session_factory)))


if __name__ == "__main__":
    sys.exit(main())
