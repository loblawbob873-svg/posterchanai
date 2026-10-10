"""The relay stores each event ONCE -- its columns -- and serves it rebuilt from them.

"the db is growing fast ... are we really cleaning good enough" / "do 1": `events.raw`, a second JSON
copy of every event, was 3.99 GB of a 17 GB database, more than content and tags together. Measured
before removing it: over 332,803 stored rows the seven NIP-01 fields rebuilt from the columns were
identical to `raw`; the only difference was a few non-Nostr keys some clients attach (`_id`,
`saved_at`), which no signature covers. Runs the relay's real insert and query code.
"""
import json
import re
import sqlite3
from pathlib import Path

import pytest

from app.services.nostr.event import build_event, verify_event
from app.services.nostr_relay.store import RelayStore, event_from_row
from app.services import git_auth


@pytest.fixture
def relay():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript('''CREATE TABLE events(id TEXT PRIMARY KEY, pubkey TEXT, created_at INTEGER, kind INTEGER,
      content TEXT, tags TEXT, sig TEXT, origin TEXT, expiration INTEGER);
      CREATE TABLE event_tags(event_id TEXT,tag TEXT,value TEXT,PRIMARY KEY(event_id,tag,value));''')
    yield RelayStore.__new__(RelayStore), conn
    conn.close()


def _ev(text="hello", kind=1, tags=None, stamp=100):
    return build_event(b"\x22" * 32, kind, text, tags or [["t", "nostr"], ["p", "ab" * 32, "", "mention"]], created_at=stamp)


def test_an_event_is_stored_without_a_second_copy_and_served_identical(relay):
    store, db = relay
    ev = _ev("emoji 🎉 and \"quotes\" and \\ backslashes", tags=[["t", "x"], ["imeta", "url https://a.test/x.png", "dim 1x2"]])
    assert store._insert_one(db, ev, "direct")
    cols = [r[1] for r in db.execute("PRAGMA table_info(events)")]
    assert "raw" not in cols and db.execute("SELECT count(*) FROM events").fetchone()[0] == 1
    got = store._query_one(db, {"ids": [ev["id"]]})
    assert got == [ev], got
    assert verify_event(got[0]), "the rebuilt event no longer verifies"


def test_a_legacy_row_is_served_from_its_columns_without_the_extra_keys(relay):
    store, db = relay
    ev = _ev("legacy")
    db.execute("INSERT INTO events (id,pubkey,created_at,kind,content,tags,sig,origin) VALUES (?,?,?,?,?,?,?,?)",
               (ev["id"], ev["pubkey"], ev["created_at"], ev["kind"], ev["content"], json.dumps(ev["tags"]), ev["sig"],
                "wot"))
    got = store._query_one(db, {"kinds": [1]})
    assert got == [ev] and "_id" not in got[0], got


def test_the_column_list_is_the_one_reader():
    """git_auth used to keep its own copy of the column list and row decoder; it reads the RELAY now
    (#161), so the store's `event_from_row` is the only one left and must stay round-trip exact."""
    assert not hasattr(git_auth, "_ECOLS") and not hasattr(git_auth, "_row_event")
    ev = _ev()
    row = (ev["id"], ev["pubkey"], ev["created_at"], ev["kind"], json.dumps(ev["tags"]), ev["content"], ev["sig"])
    assert event_from_row(row) == ev


def test_git_auth_reads_an_event_with_no_raw_copy(relay):
    """The push hook's lookup, on a row the relay now writes (raw NULL), answered by the relay's own
    query code exactly as a REQ would be."""
    store, db = relay
    ev = _ev("announcement", kind=1)
    store._insert_one(db, ev, "direct")

    class _Relay:
        def query(self, filters):
            return [e for f in filters for e in store._query_one(db, f)]

    assert git_auth.load_event_by_id(_Relay(), ev["id"]) == ev


def test_the_schema_no_longer_has_a_raw_copy_and_an_old_table_loses_it():
    """The release after the relay stopped writing `raw` drops it (metadata only, no row rewrite)."""
    from app.services.nostr_relay import store as relay_store
    assert not re.search(r"^\s*raw\s", relay_store._SCHEMA, re.M), "a fresh relay would still create raw"
    src = Path(relay_store.__file__).read_text()
    opened = src[src.index("    def open(self, loop"):][:1200]
    assert "DROP COLUMN IF EXISTS raw" in opened, "an existing relay keeps its 4 GB copy"
