#!/usr/bin/env python3
"""Copy a node's OLD Postgres app tables into their relay DocTables, ONCE, by hand (#161).

The app is relay-only: it never reads SQL and never migrates anything at startup. Only the two nodes that ever had
Postgres data (server1 and nas.lan) need this, once each, to carry their existing rows across -- ids kept (an
account's sessions, a chat's transcript, a bot's admin URL all carry them).

    venv-unified/bin/python scripts/migrate_tables_to_nostr.py --list
    venv-unified/bin/python scripts/migrate_tables_to_nostr.py --table users
    venv-unified/bin/python scripts/migrate_tables_to_nostr.py --all
    venv-unified/bin/python scripts/migrate_tables_to_nostr.py --all --database-url postgresql+psycopg2://...

For every table: read SQL (a READ-ONLY transaction -- nothing in Postgres is ever changed or dropped), copy what the
relay lacks or holds differently, remove documents no SQL row accounts for (an interrupted run's), re-read the
table strictly from the relay and compare ROW FOR ROW; only then the id high-water mark and the `_migrated` record.
Idempotent and resumable: run it again and it writes nothing that already matches. Needs the node's operator key
(the keyfile) and its relay running (POSTERCHANAI_RELAY_PORT / the nostr_relay_port setting).

Prints one JSON line per table: counts, timings and keys -- never a row's content (password hashes, linked nsecs,
API keys, sealed wallet seeds, mail passwords are in these rows).
Exit codes: 0 every table copied and verified, 1 a table did not verify (no marker written for it), 2 could not run
(relay or database unreachable, a schema the readers do not match).

This script and scripts/nostr_migration/ are the ONLY code that imports the SQLAlchemy models of these tables.
"""
import argparse
import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def sources():
    """({table: fn(db)}, {derived_table: fn(src)}) from every wave's readers, in copy order (accounts last)."""
    from scripts.nostr_migration import wave1_sources, wave23_sources
    src = dict(wave1_sources.SOURCES)
    src.update(wave23_sources.SOURCES)
    derived = dict(getattr(wave1_sources, "DERIVED", {}))
    derived.update(wave23_sources.DERIVED)
    return src, derived


def table_names() -> list:
    src, derived = sources()
    names = list(src) + list(derived)
    for last in ("user_index", "users"):        # the accounts last: every table that refers to them first
        if last in names:
            names.remove(last)
            names.append(last)
    return names


def _engine(url: str):
    from sqlalchemy import create_engine
    if not url:
        raise SystemExit("no database: pass --database-url or set DATABASE_URL")
    return create_engine(url, pool_pre_ping=True)


def read_only_factory(bind):
    from sqlalchemy.orm import Session

    def make():
        db = Session(bind=bind, autoflush=False)
        try:
            if bind.dialect.name == "postgresql":
                from sqlalchemy import text
                db.execute(text("SET TRANSACTION READ ONLY"))
        except Exception:       # noqa: BLE001 -- the readers only ever SELECT
            pass
        return db
    return make


class _Rows(dict):
    """Source rows by table, read on first use (a derived table reads its parent's)."""

    def __init__(self, src, session_factory):
        super().__init__()
        self._src, self._factory = src, session_factory

    def __missing__(self, name):
        db = self._factory()
        try:
            rows = self._src[name](db)
        finally:
            try:
                db.rollback()
            finally:
                db.close()
        self[name] = rows
        return rows


async def _copy(names, session_factory) -> int:
    from app.services import doc_table_bulk
    from app.services.relay_reader import Unavailable
    src, derived = sources()
    rows = _Rows(src, session_factory)
    worst = 0
    for name in names:
        try:
            got = derived[name](rows) if name in derived else rows[name]
            rep = await doc_table_bulk.amigrate_rows(name, got)
            print(json.dumps(rep, sort_keys=True, default=str), flush=True)
        except doc_table_bulk.MigrationMismatch as e:
            print(json.dumps({"table": name, "error": "did not verify; no marker written", "detail": str(e)}),
                  flush=True)
            worst = max(worst, 1)
        except Unavailable as e:
            print("%s: the relay could not be asked (%s)" % (name, e), file=sys.stderr)
            return 2
        except Exception as e:      # noqa: BLE001 -- the database could not be read / a schema mismatch
            print("%s: could not run (%s: %s)" % (name, type(e).__name__, e), file=sys.stderr)
            return 2
    return worst


def main(argv=None, session_factory=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--table", action="append", default=[], help="a table to copy (repeatable)")
    ap.add_argument("--all", action="store_true", help="every table, the accounts last")
    ap.add_argument("--list", action="store_true", help="the tables this script copies")
    ap.add_argument("--database-url", default=os.environ.get("DATABASE_URL", ""))
    args = ap.parse_args(argv)
    names = table_names()
    if args.list:
        print("\n".join(names))
        return 0
    if args.all:
        chosen = names
    elif args.table:
        bad = [t for t in args.table if t not in names]
        if bad:
            ap.error("unknown table(s): %s (see --list)" % ", ".join(bad))
        chosen = [n for n in names if n in args.table]
    else:
        ap.error("--table NAME, --all or --list")
    if session_factory is None:
        try:
            session_factory = read_only_factory(_engine(args.database_url))
        except SystemExit:
            raise
        except Exception as e:      # noqa: BLE001
            print("could not open the database: %s" % type(e).__name__, file=sys.stderr)
            return 2
    return asyncio.run(_copy(chosen, session_factory))


if __name__ == "__main__":
    sys.exit(main())
