"""Shared fixture for the relay-aggregate tests: ONE list of events, loaded into each backend the relay counts from.

`sql_source` is the relay's Postgres SQL run on sqlite3 (the `?` placeholders and portable SQL aggregates.py keeps
to); `pcdb_source` is a real PosterChanDB Store in a temp dir. tests/test_relay_aggregates.py additionally runs a
REAL Postgres + mirror when one is available.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3

from app.services.nostr_relay import aggregates


def pk(name: str) -> str:
    """A stable 64-hex pubkey for a readable name (PosterChanDB stores keys as raw bytes)."""
    return hashlib.sha256(("pk:" + name).encode()).hexdigest()


_SEQ = [0]


def ev(who: str, kind: int, created_at: int, tags=(), content: str = "", origin: str = "direct") -> tuple:
    _SEQ[0] += 1
    e = {"id": hashlib.sha256(("ev:%d:%s:%d:%d" % (_SEQ[0], who, kind, created_at)).encode()).hexdigest(),
         "pubkey": who if len(who) == 64 else pk(who), "created_at": int(created_at), "kind": int(kind),
         "tags": [list(t) for t in tags], "content": content, "sig": "ab" * 64}
    return e, origin


def sql_source(events) -> aggregates._Sql:
    conn = sqlite3.connect(":memory:", check_same_thread=False)   # the IPC test reads it on a "relay" thread
    conn.execute("CREATE TABLE events (id text PRIMARY KEY, pubkey text, created_at integer, kind integer, "
                 "content text, tags text, sig text, origin text, expiration integer)")
    conn.execute("CREATE TABLE event_tags (event_id text, tag text, value text, PRIMARY KEY (event_id, tag, value))")
    for e, origin in events:
        conn.execute("INSERT INTO events VALUES (?,?,?,?,?,?,?,?,NULL)",
                     (e["id"], e["pubkey"], e["created_at"], e["kind"], e["content"], json.dumps(e["tags"]),
                      e["sig"], origin))
        for t in e["tags"]:      # the relay's indexer: single-letter names, value as str
            if len(t) >= 2 and isinstance(t[0], str) and len(t[0]) == 1:
                conn.execute("INSERT OR IGNORE INTO event_tags VALUES (?,?,?)", (e["id"], t[0], str(t[1])))
    conn.commit()
    return aggregates._Sql(conn, size_sql=None)


def pcdb_source(events, path, now: int) -> aggregates._Pcdb:
    from app.services.posterchandb.store import Store
    st = Store(str(path), flush_interval=3600, snapshots=False)
    for e, origin in events:
        assert st.copy_put(dict(e), origin=origin) == "stored"
    return aggregates._Pcdb(st, now)


def norm(x):
    """Order-insensitive comparison of an aggregate answer (SQL and numpy return rows in different orders)."""
    if isinstance(x, dict):
        return {k: norm(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        items = [norm(v) for v in x]
        try:
            return sorted(items, key=lambda v: json.dumps(v, sort_keys=True))
        except TypeError:
            return items
    return x
