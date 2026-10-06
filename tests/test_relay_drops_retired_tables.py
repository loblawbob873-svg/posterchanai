"""Opening the relay store drops what retired features left in the database.

`fedi_only_events` belonged to the Pleroma bridge's fedi-only client mode (removed 2026-09-23). Nothing
reads or writes it, yet poster.place still held 32 rows and nas.lan an empty table. Driven against a real
(throwaway) Postgres: an existing relay loses the table on open, and opening twice is harmless.
"""
import asyncio
import uuid

import psycopg2
import pytest

from app.services.nostr_relay.store import RelayStore
from tests import scratch_postgres

DSN = scratch_postgres.dsn()


def _admin():
    try:
        conn = psycopg2.connect(DSN, connect_timeout=5)
    except Exception as e:
        pytest.skip(f"Postgres not reachable for the relay store: {e}")
    conn.autocommit = True
    return conn


def test_an_existing_relay_loses_fedi_only_events_on_open():
    schema = "pcai_retired_test_" + uuid.uuid4().hex[:10]
    a = _admin()
    a.cursor().execute(f'CREATE SCHEMA "{schema}"')
    a.cursor().execute(f'CREATE TABLE "{schema}".fedi_only_events (id text primary key, created_at bigint)')
    a.cursor().execute(f'INSERT INTO "{schema}".fedi_only_events VALUES (\'x\', 1)')
    loop = asyncio.new_event_loop()
    try:
        for _ in range(2):                      # the second open finds nothing to drop and must not fail
            st = RelayStore(DSN + f" options=-csearch_path={schema}")
            st.open(loop)
            st.close()
        cur = a.cursor()
        cur.execute("select count(*) from pg_tables where schemaname=%s and tablename='fedi_only_events'", (schema,))
        assert cur.fetchone()[0] == 0, "fedi_only_events survived the relay opening"
    finally:
        loop.close()
        a.cursor().execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        a.close()
