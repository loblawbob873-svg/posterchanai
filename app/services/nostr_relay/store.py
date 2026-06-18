"""SQLite storage for the built-in Nostr relay.

The hot DB lives on tmpfs (RAM) for fast write churn; a periodic online snapshot
(`sqlite3.Connection.backup`) persists it to a disk file, restored on startup. All
DB I/O is run off the relay's asyncio loop via executors so connection handling never
blocks: writes are funneled through a single-thread executor (serialized, no lock
contention), reads use a small pool over WAL (concurrent readers). Memory is hard-bounded
by `prune()` (retention window + event count + byte budget).

Schema mirrors a minimal NIP-01 relay: `events` + a single-letter `event_tags` index for
`#<letter>` filters, plus `wot` (the trust set) and `relay_kv` (cursors).
"""

import os
import re
import json
import time
import shutil
import sqlite3
import asyncio
import logging
import threading
from concurrent.futures import ThreadPoolExecutor

logger = logging.getLogger(__name__)


def _fts_match(search: str) -> str | None:
    """Turn a NIP-50 search string into a safe FTS5 MATCH expression: alphanumeric tokens,
    each quoted, AND-ed together. Quoting avoids FTS5 syntax errors from arbitrary input."""
    toks = re.findall(r"\w+", (search or "").lower())
    return " ".join(f'"{t}"' for t in toks[:12]) if toks else None

# Replaceable event kind ranges (NIP-01): keep only the newest per (pubkey, kind) — and for
# the parameterized range, per (pubkey, kind, d-tag).
_REPLACEABLE = lambda k: k in (0, 3) or 10000 <= k < 20000
_PARAM_REPLACEABLE = lambda k: 30000 <= k < 40000

_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id          TEXT PRIMARY KEY,
    pubkey      TEXT NOT NULL,
    created_at  INTEGER NOT NULL,
    kind        INTEGER NOT NULL,
    content     TEXT NOT NULL,
    tags        TEXT NOT NULL,
    sig         TEXT NOT NULL,
    raw         TEXT NOT NULL,
    origin      TEXT NOT NULL DEFAULT 'wot'  -- 'wot' | 'ancestor' (thread-context backfill)
);
CREATE INDEX IF NOT EXISTS idx_events_pubkey      ON events(pubkey);
CREATE INDEX IF NOT EXISTS idx_events_kind_created ON events(kind, created_at);
CREATE INDEX IF NOT EXISTS idx_events_created      ON events(created_at);

CREATE TABLE IF NOT EXISTS event_tags (
    event_id TEXT NOT NULL,
    tag      TEXT NOT NULL,   -- single-letter tag name (NIP-01 queryable)
    value    TEXT NOT NULL,
    PRIMARY KEY (event_id, tag, value)
);
CREATE INDEX IF NOT EXISTS idx_event_tags_tv ON event_tags(tag, value);

CREATE TABLE IF NOT EXISTS wot (
    pubkey   TEXT PRIMARY KEY,
    depth    INTEGER NOT NULL DEFAULT 0,
    added_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS relay_kv (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""

# NIP-50 search: an FTS5 index over note (kind-1) content, kept in sync by triggers. Created
# separately (with a graceful fallback) since FTS5 may not be compiled into SQLite.
_FTS_SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS events_fts USING fts5(content, content='events', content_rowid='rowid');
CREATE TRIGGER IF NOT EXISTS events_fts_ai AFTER INSERT ON events WHEN new.kind=1 BEGIN
  INSERT INTO events_fts(rowid, content) VALUES (new.rowid, new.content);
END;
CREATE TRIGGER IF NOT EXISTS events_fts_ad AFTER DELETE ON events WHEN old.kind=1 BEGIN
  INSERT INTO events_fts(events_fts, rowid, content) VALUES('delete', old.rowid, old.content);
END;
"""


# Kinds preserved forever by the age-based auto-cleaner: profiles (0) and contact lists (3).
# They're replaceable (one per author) so they don't grow unbounded, and clients need them
# to render names/avatars/follows even for very old notes.
_KEEP_KINDS = (0, 3)


class RelayStore:
    def __init__(self, hot_path: str, snapshot_path: str, *,
                 read_workers: int = 4, max_events: int = 0,
                 retention_days: int = 30, max_db_mb: int = 0, wal_pages: int = 50000):
        self.hot_path = hot_path
        self.snapshot_path = snapshot_path
        self.max_events = max_events
        self.retention_days = retention_days
        self.max_db_mb = max_db_mb
        self.wal_pages = wal_pages   # WAL autocheckpoint threshold (large = fewer checkpoints)
        self._tls = threading.local()
        self._write_exec = ThreadPoolExecutor(max_workers=1, thread_name_prefix="relay-db-w")
        self._read_exec = ThreadPoolExecutor(max_workers=read_workers, thread_name_prefix="relay-db-r")
        self._snap_exec = ThreadPoolExecutor(max_workers=1, thread_name_prefix="relay-db-snap")
        self._last_dv = -1   # PRAGMA data_version at last snapshot (dirty check)
        self._fts = False    # NIP-50 full-text search available?
        self._loop: asyncio.AbstractEventLoop | None = None

    # --- lifecycle ----------------------------------------------------------

    def open(self, loop: asyncio.AbstractEventLoop) -> None:
        """Restore from snapshot if the tmpfs DB is gone (post-reboot), then init schema."""
        self._loop = loop
        os.makedirs(os.path.dirname(self.hot_path) or ".", exist_ok=True)
        os.makedirs(os.path.dirname(self.snapshot_path) or ".", exist_ok=True)
        if not os.path.exists(self.hot_path) and os.path.exists(self.snapshot_path):
            try:
                shutil.copy2(self.snapshot_path, self.hot_path)
                logger.info("[nostr-relay] restored hot DB from snapshot %s", self.snapshot_path)
            except Exception as e:
                logger.warning("[nostr-relay] snapshot restore failed: %s", e)
        # Initialize schema on a fresh connection.
        conn = self._conn()
        conn.executescript(_SCHEMA)
        conn.commit()
        # NIP-50 search index (FTS5). Graceful: if FTS5 isn't compiled in, search falls back
        # to a LIKE scan. Populate from existing notes on first creation.
        try:
            conn.executescript(_FTS_SCHEMA)
            if conn.execute("SELECT COUNT(*) FROM events_fts").fetchone()[0] == 0 and \
                    conn.execute("SELECT 1 FROM events WHERE kind=1 LIMIT 1").fetchone():
                conn.execute("INSERT INTO events_fts(rowid, content) "
                             "SELECT rowid, content FROM events WHERE kind=1")
            conn.commit()
            self._fts = True
        except Exception as e:
            logger.warning("[nostr-relay] FTS5 unavailable; NIP-50 search uses LIKE: %s", e)
            self._fts = False

    def close(self) -> None:
        self._write_exec.shutdown(wait=True)
        self._snap_exec.shutdown(wait=True)
        self._read_exec.shutdown(wait=False)

    async def _snap(self, fn, *a):
        return await self._loop.run_in_executor(self._snap_exec, fn, *a)

    def _conn(self) -> sqlite3.Connection:
        """Per-thread connection (executor threads each get their own), WAL + busy wait."""
        c = getattr(self._tls, "conn", None)
        if c is None:
            c = sqlite3.connect(self.hot_path, timeout=10, check_same_thread=False)
            c.row_factory = sqlite3.Row
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("PRAGMA synchronous=NORMAL")
            c.execute("PRAGMA busy_timeout=5000")
            c.execute("PRAGMA cache_size=-8000")  # ~8MB page cache
            # Large WAL autocheckpoint → far fewer checkpoints, much faster sustained writes on
            # a multi-GB disk DB (the WAL absorbs bursts; readers stay non-blocking).
            c.execute(f"PRAGMA wal_autocheckpoint={int(self.wal_pages)}")
            self._tls.conn = c
        return c

    async def _w(self, fn, *a):
        return await self._loop.run_in_executor(self._write_exec, fn, *a)

    async def _r(self, fn, *a):
        return await self._loop.run_in_executor(self._read_exec, fn, *a)

    # --- writes -------------------------------------------------------------

    def _add_event_sync(self, ev: dict, origin: str) -> bool:
        conn = self._conn()
        eid = ev["id"]
        kind = int(ev["kind"])
        pubkey = ev["pubkey"]
        created = int(ev["created_at"])
        tags = ev.get("tags") or []
        try:
            # Replaceable-event handling: drop older versions so only the newest survives.
            if _REPLACEABLE(kind):
                cur = conn.execute(
                    "SELECT id, created_at FROM events WHERE pubkey=? AND kind=?",
                    (pubkey, kind))
                for row in cur.fetchall():
                    if row["created_at"] <= created and row["id"] != eid:
                        self._delete_sync(conn, row["id"])
                    elif row["created_at"] > created:
                        return False  # a newer version already stored
            elif _PARAM_REPLACEABLE(kind):
                d = next((t[1] for t in tags if len(t) >= 2 and t[0] == "d"), "")
                cur = conn.execute(
                    "SELECT e.id, e.created_at FROM events e WHERE e.pubkey=? AND e.kind=?",
                    (pubkey, kind))
                for row in cur.fetchall():
                    rd = conn.execute(
                        "SELECT value FROM event_tags WHERE event_id=? AND tag='d' LIMIT 1",
                        (row["id"],)).fetchone()
                    rdv = rd["value"] if rd else ""
                    if rdv == d:
                        if row["created_at"] <= created and row["id"] != eid:
                            self._delete_sync(conn, row["id"])
                        elif row["created_at"] > created:
                            return False

            conn.execute(
                "INSERT OR IGNORE INTO events "
                "(id, pubkey, created_at, kind, content, tags, sig, raw, origin) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (eid, pubkey, created, kind, ev.get("content", ""),
                 json.dumps(tags, separators=(",", ":")), ev.get("sig", ""),
                 json.dumps(ev, separators=(",", ":")), origin))
            # Index single-letter tags only (NIP-01 queryable tags).
            for t in tags:
                if len(t) >= 2 and isinstance(t[0], str) and len(t[0]) == 1:
                    conn.execute(
                        "INSERT OR IGNORE INTO event_tags (event_id, tag, value) VALUES (?,?,?)",
                        (eid, t[0], str(t[1])))
            # NIP-09: a kind-5 deletion removes the author's own e-tagged events.
            if kind == 5:
                for t in tags:
                    if len(t) >= 2 and t[0] == "e":
                        conn.execute(
                            "DELETE FROM events WHERE id=? AND pubkey=?", (t[1], pubkey))
                        conn.execute("DELETE FROM event_tags WHERE event_id=?", (t[1],))
            conn.commit()
            return True
        except Exception as e:
            logger.warning("[nostr-relay] add_event %s failed: %s", eid[:12], e)
            try:
                conn.rollback()
            except Exception:
                pass
            return False

    def _delete_sync(self, conn: sqlite3.Connection, eid: str) -> None:
        conn.execute("DELETE FROM events WHERE id=?", (eid,))
        conn.execute("DELETE FROM event_tags WHERE event_id=?", (eid,))

    async def add_event(self, ev: dict, origin: str = "wot") -> bool:
        """Insert an event (already verified + WoT-gated by the caller). Returns stored?"""
        return await self._w(self._add_event_sync, ev, origin)

    def _delete_pubkeys_sync(self, pubkeys: list) -> int:
        if not pubkeys:
            return 0
        conn = self._conn()
        removed = 0
        for pk in pubkeys:
            conn.execute("DELETE FROM event_tags WHERE event_id IN "
                         "(SELECT id FROM events WHERE pubkey=?)", (pk,))
            cur = conn.execute("DELETE FROM events WHERE pubkey=?", (pk,))
            removed += cur.rowcount or 0
        conn.commit()
        return removed

    async def delete_pubkeys(self, pubkeys: list) -> int:
        """Purge all events authored by these pubkeys (e.g. when an author is blocklisted)."""
        return await self._w(self._delete_pubkeys_sync, list(pubkeys))

    def _has_sync(self, eid: str) -> bool:
        return self._conn().execute(
            "SELECT 1 FROM events WHERE id=? LIMIT 1", (eid,)).fetchone() is not None

    async def has_event(self, eid: str) -> bool:
        return await self._r(self._has_sync, eid)

    # --- reads --------------------------------------------------------------

    def _query_sync(self, filters: list, hard_cap: int) -> list:
        """NIP-01 filter set (OR across filters); returns raw event dicts, newest-first."""
        conn = self._conn()
        seen: dict[str, dict] = {}
        for flt in filters or []:
            for ev in self._query_one(conn, flt or {}):
                seen[ev["id"]] = ev
        out = sorted(seen.values(), key=lambda e: e.get("created_at", 0), reverse=True)
        return out[:hard_cap] if hard_cap else out

    def _query_one(self, conn: sqlite3.Connection, flt: dict) -> list:
        where, params = [], []
        ids = flt.get("ids")
        if ids:
            where.append(f"e.id IN ({','.join('?' * len(ids))})")
            params += list(ids)
        authors = flt.get("authors")
        if authors:
            where.append(f"e.pubkey IN ({','.join('?' * len(authors))})")
            params += list(authors)
        kinds = flt.get("kinds")
        if kinds:
            where.append(f"e.kind IN ({','.join('?' * len(kinds))})")
            params += [int(k) for k in kinds]
        if flt.get("since") is not None:
            where.append("e.created_at >= ?")
            params.append(int(flt["since"]))
        if flt.get("until") is not None:
            where.append("e.created_at <= ?")
            params.append(int(flt["until"]))
        # NIP-50 full-text search over note content.
        search = flt.get("search")
        if search:
            if self._fts:
                m = _fts_match(search)
                if not m:
                    return []
                where.append("e.rowid IN (SELECT rowid FROM events_fts WHERE events_fts MATCH ?)")
                params.append(m)
            else:
                for term in re.findall(r"\w+", search.lower())[:12]:
                    where.append("LOWER(e.content) LIKE ?")
                    params.append("%" + term + "%")
        # Tag filters: keys like "#e", "#p", "#t" → join event_tags (AND across tag keys).
        for key, vals in flt.items():
            if not (isinstance(key, str) and key.startswith("#") and len(key) == 2 and vals):
                continue
            where.append(
                "e.id IN (SELECT event_id FROM event_tags WHERE tag=? AND value IN "
                f"({','.join('?' * len(vals))}))")
            params.append(key[1])
            params += [str(v) for v in vals]
        limit = int(flt.get("limit") or 500)
        limit = max(1, min(limit, 5000))
        sql = "SELECT e.raw FROM events e"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY e.created_at DESC LIMIT ?"
        params.append(limit)
        rows = conn.execute(sql, params).fetchall()
        out = []
        for r in rows:
            try:
                out.append(json.loads(r["raw"]))
            except Exception:
                continue
        return out

    async def query(self, filters: list, hard_cap: int = 5000) -> list:
        return await self._r(self._query_sync, filters, hard_cap)

    # --- kv (cursors) -------------------------------------------------------

    def _kv_get_sync(self, key: str) -> str | None:
        row = self._conn().execute("SELECT value FROM relay_kv WHERE key=?", (key,)).fetchone()
        return row["value"] if row else None

    def _kv_set_sync(self, key: str, value: str) -> None:
        conn = self._conn()
        conn.execute("INSERT INTO relay_kv (key, value) VALUES (?,?) "
                     "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
        conn.commit()

    async def kv_get(self, key: str) -> str | None:
        return await self._r(self._kv_get_sync, key)

    async def kv_set(self, key: str, value: str) -> None:
        await self._w(self._kv_set_sync, key, value)

    # --- WoT membership -----------------------------------------------------

    def _wot_replace_sync(self, members: list, extra: list | None = None) -> int:
        """Replace the depth-1 WoT set; `extra` are always-trusted (operator) keys at depth 0."""
        conn = self._conn()
        now = int(time.time())
        conn.execute("DELETE FROM wot")
        rows = [(pk, 1, now) for pk in members] + [(pk, 0, now) for pk in (extra or [])]
        conn.executemany(
            "INSERT OR REPLACE INTO wot (pubkey, depth, added_at) VALUES (?,?,?)", rows)
        conn.commit()
        return conn.execute("SELECT COUNT(*) AS c FROM wot").fetchone()["c"]

    async def wot_replace(self, members: list, extra: list | None = None) -> int:
        return await self._w(self._wot_replace_sync, members, extra)

    def _wot_members_sync(self) -> set:
        return {r["pubkey"] for r in self._conn().execute("SELECT pubkey FROM wot").fetchall()}

    async def wot_members(self) -> set:
        return await self._r(self._wot_members_sync)

    def _wot_missing_metadata_sync(self) -> list:
        # Members lacking lookup metadata: no kind-0 profile OR no kind-10002 relay list.
        # Drives the lookup-relay backfill so clients can resolve profiles + outbox relays.
        rows = self._conn().execute(
            "SELECT w.pubkey FROM wot w WHERE "
            "NOT EXISTS (SELECT 1 FROM events e WHERE e.pubkey=w.pubkey AND e.kind=0) "
            "OR NOT EXISTS (SELECT 1 FROM events e WHERE e.pubkey=w.pubkey AND e.kind=10002)"
        ).fetchall()
        return [r["pubkey"] for r in rows]

    async def wot_missing_metadata(self) -> list:
        return await self._r(self._wot_missing_metadata_sync)

    # --- snapshot + prune ---------------------------------------------------

    def _snapshot_sync(self) -> bool:
        # Runs on its own connection/thread, NOT the write thread — so it never blocks
        # writers (WAL lets it read concurrently). `data_version` changes whenever another
        # connection has committed, so we skip the copy entirely when nothing changed.
        src = self._conn()
        try:
            dv = src.execute("PRAGMA data_version").fetchone()[0]
        except Exception:
            dv = None
        if dv is not None and dv == self._last_dv:
            return False  # idle since last snapshot — nothing to copy
        tmp = self.snapshot_path + ".tmp"
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
            dst = sqlite3.connect(tmp)
            # Stepped copy: 4096 pages (~16MB) per step, yielding between steps so the
            # snapshot stays cheap and cooperative even as the DB grows.
            src.backup(dst, pages=4096, sleep=0.01)
            dst.close()
            os.replace(tmp, self.snapshot_path)
            if dv is not None:
                self._last_dv = dv
            return True
        except Exception as e:
            logger.warning("[nostr-relay] snapshot failed: %s", e)
            try:
                if os.path.exists(tmp):
                    os.remove(tmp)
            except Exception:
                pass
            return False

    async def snapshot(self) -> None:
        await self._snap(self._snapshot_sync)

    def _checkpoint_sync(self) -> None:
        try:
            self._conn().execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except Exception as e:
            logger.warning("[nostr-relay] checkpoint failed: %s", e)

    async def checkpoint(self) -> None:
        """Fold the WAL back into the main DB (disk mode, clean shutdown)."""
        await self._w(self._checkpoint_sync)

    def _prune_sync(self) -> int:
        conn = self._conn()
        removed = 0
        keep = ",".join(str(k) for k in _KEEP_KINDS)
        # Age-based auto-cleaner: delete only old NOTES/reactions/reposts — never profiles
        # (kind 0) or contact lists (kind 3), so identities/follows survive indefinitely.
        if self.retention_days:
            cutoff = int(time.time()) - self.retention_days * 86400
            cur = conn.execute(
                f"DELETE FROM events WHERE created_at < ? AND kind NOT IN ({keep})", (cutoff,))
            removed += cur.rowcount or 0
        # Hard count cap (memory bound): trim oldest non-kept events beyond the limit.
        if self.max_events:
            cur = conn.execute(
                f"DELETE FROM events WHERE kind NOT IN ({keep}) AND id IN "
                "(SELECT id FROM events ORDER BY created_at DESC LIMIT -1 OFFSET ?)",
                (self.max_events,))
            removed += cur.rowcount or 0
        # Hard byte cap (RAM bound): trim oldest non-kept events until under budget.
        if self.max_db_mb:
            budget = self.max_db_mb * 1024 * 1024
            for _ in range(50):
                if self._db_bytes() <= budget:
                    break
                cur = conn.execute(
                    f"DELETE FROM events WHERE kind NOT IN ({keep}) AND id IN "
                    "(SELECT id FROM events ORDER BY created_at ASC LIMIT 2000)")
                if not cur.rowcount:
                    break  # only kept kinds remain — don't spin
                removed += cur.rowcount
                conn.commit()
        # Drop orphaned tag rows.
        conn.execute("DELETE FROM event_tags WHERE event_id NOT IN (SELECT id FROM events)")
        conn.commit()
        return removed

    def _db_bytes(self) -> int:
        try:
            total = os.path.getsize(self.hot_path)
            for ext in ("-wal", "-shm"):
                p = self.hot_path + ext
                if os.path.exists(p):
                    total += os.path.getsize(p)
            return total
        except OSError:
            return 0

    async def prune(self) -> int:
        return await self._w(self._prune_sync)

    def _count_sync(self) -> int:
        return self._conn().execute("SELECT COUNT(*) AS c FROM events").fetchone()["c"]

    async def count(self) -> int:
        return await self._r(self._count_sync)
