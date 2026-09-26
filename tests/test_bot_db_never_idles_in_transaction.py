"""A bot reading the Akkoma database must never sit "idle in transaction".

Run: venv-unified/bin/python -m pytest tests/test_bot_db_never_idles_in_transaction.py

Found on nas.lan (2026-09-26) while cleaning the `detroitriotcity` database: welcomebot's
connection had been "idle in transaction" since the bot started. psycopg2 opens a transaction on
the first execute() and holds it until commit(), and the bots (welcomebot, blockbot, reportbot,
engagement, unfollowbot) only read and never commit — so each process lived inside ONE transaction.
It held an AccessShareLock on `users` (a DROP TRIGGER to remove pg_repack debris timed out five times
in a row behind it) and pinned the xmin horizon, so VACUUM could not reclaim dead rows anywhere in
that database. Every bot now connects through `botframework/akkoma_db.connect`, which is autocommit.

These tests ask the REAL Postgres what state the connection is in (pg_stat_activity) and whether an
exclusive lock can be taken — the two things that were wrong — against the local server the relay
tests already use, and skip if it is not reachable.
"""
import os
import re
import sys
import uuid

import pytest

psycopg2 = pytest.importorskip("psycopg2")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BOTS = os.path.join(ROOT, "botframework")
sys.path.insert(0, BOTS)
import akkoma_db  # noqa: E402

DSN = dict(host="127.0.0.1", port=5432, dbname="posterchan_relay", user="posterchan")


@pytest.fixture
def pg():
    try:
        admin = psycopg2.connect(connect_timeout=5, **DSN)
    except Exception as e:
        pytest.skip(f"Postgres not reachable: {e}")
    admin.autocommit = True
    schema = "botdb_" + uuid.uuid4().hex[:10]
    with admin.cursor() as c:
        c.execute(f"CREATE SCHEMA {schema}")
        c.execute(f"CREATE TABLE {schema}.users (id int, nickname text)")
        c.execute(f"INSERT INTO {schema}.users VALUES (1, 'alice')")
    yield admin, schema
    with admin.cursor() as c:
        c.execute(f"DROP SCHEMA {schema} CASCADE")
    admin.close()


def _bot(**kw):
    return akkoma_db.connect(DSN["dbname"], DSN["user"], "", host=DSN["host"], port=DSN["port"], **kw)


def _state(admin, conn):
    with admin.cursor() as c:
        c.execute("SELECT state FROM pg_stat_activity WHERE pid = %s", (conn.get_backend_pid(),))
        return c.fetchone()[0]


def _can_lock(admin, schema):
    """What the pg_repack cleanup needed: an exclusive lock, without waiting."""
    with admin.cursor() as c:
        try:
            c.execute("BEGIN")
            c.execute(f"LOCK TABLE {schema}.users IN ACCESS EXCLUSIVE MODE NOWAIT")
            return True
        except psycopg2.errors.LockNotAvailable:
            return False
        finally:
            c.execute("ROLLBACK")


def test_a_read_leaves_the_connection_idle_and_the_table_lockable(pg):
    admin, schema = pg
    bot = _bot()
    with bot.cursor() as c:
        c.execute(f"SELECT id, nickname FROM {schema}.users")
        assert c.fetchall() == [(1, "alice")]
    assert _state(admin, bot) == "idle", "the bot's read left a transaction open"
    assert _can_lock(admin, schema), "the bot is still holding a lock on the table it read"
    bot.close()


def test_the_old_connection_is_what_this_catches(pg):
    """Proof the checks can fail: a plain psycopg2 connection — what every bot used — reproduces
    exactly what nas.lan showed."""
    admin, schema = pg
    old = psycopg2.connect(connect_timeout=5, **DSN)
    with old.cursor() as c:
        c.execute(f"SELECT id FROM {schema}.users")
        c.fetchall()
    assert _state(admin, old) == "idle in transaction"
    assert not _can_lock(admin, schema)
    old.close()


def test_a_failed_query_does_not_wedge_the_next_poll(pg):
    admin, schema = pg
    bot = _bot()
    with bot.cursor() as c:
        with pytest.raises(psycopg2.Error):
            c.execute(f"SELECT no_such_column FROM {schema}.users")
    with bot.cursor() as c:
        c.execute(f"SELECT count(*) FROM {schema}.users")
        assert c.fetchone() == (1,), "the next poll must work after a failed one"
    assert _state(admin, bot) == "idle"
    bot.close()


def _code(path):
    with open(path, encoding="utf-8") as fh:
        src = fh.read()
    src = re.sub(r'"""(?:.|\n)*?"""', "", src)
    return re.sub(r"#.*", "", src)


def test_no_bot_connects_any_other_way():
    """A sixth copy of the old block would bring the bug back for that bot only."""
    offenders = [f for f in sorted(os.listdir(BOTS))
                 if f.endswith(".py") and f != "akkoma_db.py"
                 and "psycopg2.connect(" in _code(os.path.join(BOTS, f))]
    assert offenders == [], f"these connect to Postgres without akkoma_db (no autocommit): {offenders}"
