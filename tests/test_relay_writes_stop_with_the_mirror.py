"""After the mirror is detached for a clean stop, the relay stores nothing more.

2026-10-10, server1 moved shadow -> serve with a clean restart: the mirror reopened WITHOUT copying (its CLEAN
token matched Postgres) and its count was ONE short -- Postgres 2,618,989, mirror 2,618,988 -- so it went stale.
The stop detaches the mirror on the writer thread and stamps the clean token, then closes; but every event write
goes through that same writer queue, and anything queued BEHIND the detach (a firehose batch, a client's EVENT
already in flight) still reached Postgres -- with no mirror attached, under a token that says the two are
identical. Postgres and the mirror must stop at the same point: after detach, event writes are refused.
"""
import asyncio
import uuid

import pytest

from app.services.nostr_relay import store as relay
from app.services.posterchandb import mirror as M
from tests.test_posterchandb_durability import _events

psycopg2 = pytest.importorskip("psycopg2")
from tests import scratch_postgres  # noqa: E402


@pytest.fixture
def dsn():
    try:
        admin = psycopg2.connect(scratch_postgres.dsn(), connect_timeout=5)
    except Exception as e:      # noqa: BLE001
        pytest.skip("Postgres not reachable: %s" % e)
    admin.autocommit = True
    schema = "pcai_pcdb_stop_" + uuid.uuid4().hex[:10]
    admin.cursor().execute(f'CREATE SCHEMA "{schema}"')
    try:
        yield scratch_postgres.dsn() + f" options=-csearch_path={schema}"
    finally:
        admin.cursor().execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        admin.close()


def _count(rs):
    return rs._write_exec.submit(lambda: rs._conn().execute("SELECT count(*) AS n FROM events").fetchone()["n"]).result()


def test_nothing_reaches_postgres_after_the_mirror_is_detached(dsn, tmp_path):
    evs = _events(91, 40)
    rs = relay.RelayStore(dsn)
    rs.open(asyncio.new_event_loop())
    m = M.Mirror(str(tmp_path / "relay"), "serve", lambda: psycopg2.connect(dsn), flush_interval=3600, maintenance=False)
    try:
        rs.attach_mirror(m)
        for ev in evs[:30]:
            rs._write_exec.submit(rs._add_event_sync, ev, "wot").result()
        before = _count(rs)
        m2, token = rs.detach_mirror()
        # what the stop races: writes queued behind the detach, on every write path
        assert rs._write_exec.submit(rs._add_event_sync, evs[30], "wot").result() is False
        assert rs._write_exec.submit(rs._add_events_bulk_sync, evs[31:35], "wot").result() == 0
        assert rs._write_exec.submit(rs._delete_pubkeys_sync, [evs[0]["pubkey"]], False).result() == 0
        assert _count(rs) == before, "an event reached Postgres after the mirror stopped listening"
        m2.close(clean_token=token)
    finally:
        rs.mirror = None
        rs.close()


def test_a_count_mismatch_names_the_events_that_differ(dsn, tmp_path):
    """"count differs (postgres 2618989, mirror 2618988)" could not be traced after the fact: the mirror went
    stale, stopped taking writes, and every later diff was buried in ordinary traffic. The check names them."""
    evs = _events(92, 20)
    rs = relay.RelayStore(dsn)
    rs.open(asyncio.new_event_loop())
    for ev in evs[:10]:
        rs._write_exec.submit(rs._add_event_sync, ev, "wot").result()
    m = M.Mirror(str(tmp_path / "relay"), "serve", lambda: psycopg2.connect(dsn), flush_interval=3600, maintenance=False)
    lines = []
    m.log = lines.append
    try:
        rs.attach_mirror(m)
        for _ in range(400):
            if m.state in ("ready", "stale", "failed"):
                break
            import time
            time.sleep(0.05)
        assert m.state == "ready", m.stats()
        # an event Postgres has and the mirror never heard of (what a write behind the detach produced)
        rs.mirror = None
        rs._write_exec.submit(rs._add_event_sync, evs[10], "wot").result()
        rs.mirror = m
        assert m._verify() is False
        text = "\n".join(lines)
        assert "1 only in Postgres, 0 only in the mirror" in text, text[-800:]
        assert evs[10]["id"][:16] in text and "kind=%d" % evs[10]["kind"] in text, text[-800:]
        assert evs[10].get("content", "x") not in text or not evs[10].get("content"), "event content was logged"
    finally:
        rs.mirror = None
        m.close()
        rs.close()
