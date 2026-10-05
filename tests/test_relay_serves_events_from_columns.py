"""The relay stores each event ONCE -- its columns -- and serves it rebuilt from them.

"the db is growing fast ... are we really cleaning good enough" / "do 1": `events.raw`, a second JSON
copy of every event, was 3.99 GB of a 17 GB database, more than content and tags together. Measured
before removing it: over 332,803 stored rows the seven NIP-01 fields rebuilt from the columns were
identical to `raw`; the only difference was a few non-Nostr keys some clients attach (`_id`,
`saved_at`), which no signature covers. Runs the relay's real insert and query code.
"""
import json
import sqlite3

import pytest

from app.services.nostr.event import build_event, verify_event
from app.services.nostr_relay.store import RelayStore, EVENT_COLUMNS, event_from_row
from app.services import git_auth


@pytest.fixture
def relay():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript('''CREATE TABLE events(id TEXT PRIMARY KEY, pubkey TEXT, created_at INTEGER, kind INTEGER,
      content TEXT, tags TEXT, sig TEXT, raw TEXT, origin TEXT, expiration INTEGER);
      CREATE TABLE event_tags(event_id TEXT,tag TEXT,value TEXT,PRIMARY KEY(event_id,tag,value));''')
    yield RelayStore.__new__(RelayStore), conn
    conn.close()


def _ev(text="hello", kind=1, tags=None, stamp=100):
    return build_event(b"\x22" * 32, kind, text, tags or [["t", "nostr"], ["p", "ab" * 32, "", "mention"]], created_at=stamp)


def test_an_event_is_stored_without_a_second_copy_and_served_identical(relay):
    store, db = relay
    ev = _ev("emoji 🎉 and \"quotes\" and \\ backslashes", tags=[["t", "x"], ["imeta", "url https://a.test/x.png", "dim 1x2"]])
    assert store._insert_one(db, ev, "direct")
    assert db.execute("SELECT raw FROM events").fetchone()[0] is None, "the event was written twice"
    got = store._query_one(db, {"ids": [ev["id"]]})
    assert got == [ev], got
    assert verify_event(got[0]), "the rebuilt event no longer verifies"


def test_a_legacy_row_is_served_from_its_columns_without_the_extra_keys(relay):
    store, db = relay
    ev = _ev("legacy")
    junk = dict(ev, _id="mongo", saved_at=5)                     # what some clients attached
    db.execute("INSERT INTO events (id,pubkey,created_at,kind,content,tags,sig,raw,origin) VALUES (?,?,?,?,?,?,?,?,?)",
               (ev["id"], ev["pubkey"], ev["created_at"], ev["kind"], ev["content"], json.dumps(ev["tags"]), ev["sig"],
                json.dumps(junk), "wot"))
    got = store._query_one(db, {"kinds": [1]})
    assert got == [ev] and "_id" not in got[0], got


def test_the_column_list_is_the_one_both_readers_use():
    assert EVENT_COLUMNS == git_auth._ECOLS and git_auth._COLS == EVENT_COLUMNS.replace("e.", "")
    ev = _ev()
    row = (ev["id"], ev["pubkey"], ev["created_at"], ev["kind"], json.dumps(ev["tags"]), ev["content"], ev["sig"])
    assert event_from_row(row) == ev == git_auth._row_event(row)


def test_git_auth_reads_an_event_with_no_raw_copy(relay):
    """The push hook's lookup, on a row the relay now writes (raw NULL)."""
    store, db = relay
    ev = _ev("announcement", kind=1)
    store._insert_one(db, ev, "direct")

    class _Cur:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def execute(self, sql, params):
            self.rows = db.execute(sql.replace("%s", "?"), params).fetchall()
        def fetchone(self): return tuple(self.rows[0]) if self.rows else None
        def fetchall(self): return [tuple(r) for r in self.rows]

    class _Conn:
        def cursor(self): return _Cur()

    assert git_auth.load_event_by_id(_Conn(), ev["id"]) == ev
