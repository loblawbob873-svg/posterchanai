#!/usr/bin/env python3
"""Copy app tables from Postgres into their DocTables on this node's relay, verify, mark (#161).

    venv-unified/bin/python scripts/migrate_tables_to_nostr.py --table reminders
    venv-unified/bin/python scripts/migrate_tables_to_nostr.py --table all
    venv-unified/bin/python scripts/migrate_tables_to_nostr.py --list

The app does this by itself at startup on port 3051 (app/services/table_migration.py); this is the SAME engine
and the SAME registry, for running one table by hand, checking a node, or reading why a table refused. Every
table: copy what the relay lacks or holds differently, re-read both sides strictly, settle every differing row
against a fresh SQL read, and only then write the `_migrated` marker that makes the relay authoritative.
Idempotent (a marked table is skipped). It never drops a SQL table and never changes a SQL row -- the session it
reads with is put in a read-only transaction where the database supports one.

Prints counts and keys only, never row content (API keys, share and verification tokens are secrets).
Exit codes: 0 every table migrated (or already), 1 a table did not verify (no marker written), 2 could not run
(relay or database unreachable).
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _read_only_factory(session_factory):
    def make():
        db = session_factory()
        try:
            if db.bind is not None and db.bind.dialect.name == "postgresql":
                from sqlalchemy import text
                db.execute(text("SET TRANSACTION READ ONLY"))
        except Exception:       # noqa: BLE001 -- the migration itself only ever SELECTs
            pass
        return db
    return make


def main(argv=None, session_factory=None) -> int:
    from app.services import table_migration
    names = table_migration.tables()
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--table", action="append", help="a table to migrate (repeatable), or 'all': %s" % ", ".join(names))
    g.add_argument("--all", action="store_true", help="every registered table")
    g.add_argument("--list", action="store_true", help="list the registered tables and exit")
    args = ap.parse_args(argv)
    if args.list:
        print("\n".join(names))
        return 0
    want = names if args.all or (args.table and "all" in args.table) else \
        list(dict.fromkeys(table_migration.ALIASES.get(n, n) for n in args.table))
    unknown = [n for n in want if n not in names]
    if unknown:
        ap.error("unknown table(s): %s" % ", ".join(unknown))
    if session_factory is None:
        from app.database import SessionLocal as session_factory
        try:
            from app.services import settings_store
            db = session_factory()
            try:
                settings_store.hydrate_from_db(db)
            finally:
                db.close()
        except Exception as e:      # noqa: BLE001 -- the relay port has a default
            print("settings not hydrated (%s); using defaults" % type(e).__name__, file=sys.stderr)
    table_migration.bind(_read_only_factory(session_factory))
    from app.services.doc_table_bulk import MigrationMismatch
    from app.services.relay_reader import Unavailable
    rc = 0
    for name in want:
        try:
            out = table_migration.migrate(name, in_thread=False)    # nothing else waits on this process
            out = dict(out, status="already" if out.get("skipped") else "migrated")
            print(json.dumps({k: v for k, v in out.items() if k != "digest"}, sort_keys=True, default=str))
        except MigrationMismatch as e:
            print(json.dumps({"table": name, "status": "did not verify; no marker written", "error": str(e)}))
            rc = max(rc, 1)
        except Unavailable as e:
            print("refusing: %s could not be migrated -- the relay or the database could not be asked (%s)"
                  % (name, e), file=sys.stderr)
            return 2
    return rc


if __name__ == "__main__":
    sys.exit(main())
