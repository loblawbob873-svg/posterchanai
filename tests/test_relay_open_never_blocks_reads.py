"""A relay start must never stall the relay's READS, whatever else holds the events table.

2026-10-09, poster.place: RelayStore.open() runs `ALTER TABLE events DROP COLUMN IF EXISTS raw` (and a DROP TABLE).
ALTER TABLE takes an ACCESS EXCLUSIVE lock BEFORE it checks whether the column exists, so behind any long reader
(here a 35-minute repeatable-read export; a pg_dump or a slow report does the same) it WAITS -- and Postgres lock
queues are first-come, so every relay read after it waited too. Notes, timelines and threads all came back empty on
every device until the waiting ALTER was terminated by hand. The cleanup statements are idempotent housekeeping:
open() now gives them a short lock_timeout and skips them when the table is busy (they run at the next start).
"""
import asyncio
import threading
import time
import uuid

import pytest

psycopg2 = pytest.importorskip("psycopg2")

from app.services.nostr_relay import store as relay   # noqa: E402
from tests import scratch_postgres                     # noqa: E402

DSN = scratch_postgres.dsn()


def test_open_behind_a_long_reader_does_not_stall_reads():
    try:
        admin = psycopg2.connect(DSN, connect_timeout=5)
    except Exception as e:      # noqa: BLE001
        pytest.skip("Postgres not reachable: %s" % e)
    admin.autocommit = True
    schema = "pcai_open_lock_" + uuid.uuid4().hex[:10]
    admin.cursor().execute(f'CREATE SCHEMA "{schema}"')
    dsn = DSN + f" options=-csearch_path={schema}"
    first = relay.RelayStore(dsn)
    first.open(asyncio.new_event_loop())           # the schema exists, as on any running node
    first.close()
    long_reader = psycopg2.connect(dsn)
    long_reader.set_session(isolation_level="REPEATABLE READ", readonly=True)
    long_reader.cursor().execute("SELECT count(*) FROM events")   # holds ACCESS SHARE until it ends
    rs = relay.RelayStore(dsn)
    opened = {}

    def _open():
        t = time.time()
        rs.open(asyncio.new_event_loop())
        opened["s"] = time.time() - t

    th = threading.Thread(target=_open, daemon=True)
    try:
        th.start()
        time.sleep(0.5)                                 # open() is now at its DDL
        reader = psycopg2.connect(dsn, connect_timeout=5)
        reader.autocommit = True
        cur = reader.cursor()
        t = time.time()
        cur.execute("SET statement_timeout = '15s'")
        cur.execute("SELECT count(*) FROM events")      # what every relay REQ does
        waited = time.time() - t
        reader.close()
        th.join(30)
        assert waited < 5, "a relay read waited %.1fs behind the restart's DDL" % waited
        assert not th.is_alive() and opened.get("s", 99) < 15, "open() never finished: %r" % opened
    finally:
        long_reader.rollback()
        long_reader.close()
        th.join(30)
        rs.close()
        admin.cursor().execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        admin.close()
