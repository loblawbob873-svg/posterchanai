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

_HEXSET = frozenset("0123456789abcdef")
def _is_hex64(s) -> bool:
    return isinstance(s, str) and len(s) == 64 and set(s) <= _HEXSET

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
    origin      TEXT NOT NULL DEFAULT 'wot',  -- 'wot' | 'ancestor' (thread-context backfill)
    expiration  INTEGER  -- NIP-40: unix ts after which the event is gone (NULL = never expires)
);
CREATE INDEX IF NOT EXISTS idx_events_pubkey      ON events(pubkey);
CREATE INDEX IF NOT EXISTS idx_events_kind_created ON events(kind, created_at);
CREATE INDEX IF NOT EXISTS idx_events_created      ON events(created_at);
-- NOTE: idx_events_expiration is created in open() AFTER the column is ensured (ALTER for old
-- DBs) — putting it here would crash executescript on a pre-existing table lacking the column.

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


# The age-based auto-cleaner deletes ONLY high-volume, reconstructable FEED content — never
# important events. Allowlist (not denylist) so a kind is pruned only if it is explicitly one
# of these: notes (1), reposts (6), reactions (7), NIP-22 comments (1111). EVERYTHING else is
# kept indefinitely — profiles (0), contacts (3), ALL replaceable identity/relay lists
# (10000-19999: relay list 10002, DM relays 10050, search relays 10007, blossom servers 10063,
# mute/bookmark/etc.), private DMs (legacy 4, NIP-59 seal 13, NIP-17 gift wrap 1059), long-form
# articles (30023), and any other/unknown kind. (Client-published `origin='direct'` events are
# additionally never pruned regardless of kind.)
_PRUNABLE_KINDS = (1, 6, 7, 1111)


class RelayStore:
    def __init__(self, db_path: str, *,
                 read_workers: int = 4, max_events: int = 0,
                 retention_days: int = 30, max_db_mb: int = 0, wal_pages: int = 50000,
                 cache_mb: int = 64, mmap_mb: int = 256):
        self.db_path = db_path       # the DB lives on disk in WAL mode (durable by itself)
        self.max_events = max_events
        self.retention_days = retention_days
        self.max_db_mb = max_db_mb
        self.wal_pages = wal_pages   # WAL autocheckpoint threshold (large = fewer checkpoints)
        self.cache_mb = cache_mb     # per-connection page cache
        self.mmap_mb = mmap_mb       # memory-mapped read window
        self.preserve_pubkeys: frozenset = frozenset()  # local users — never age/cap-pruned
        self._tls = threading.local()
        self._write_exec = ThreadPoolExecutor(max_workers=1, thread_name_prefix="relay-db-w")
        self._read_exec = ThreadPoolExecutor(max_workers=read_workers, thread_name_prefix="relay-db-r")
        self._fts = False    # NIP-50 full-text search available?
        self._loop: asyncio.AbstractEventLoop | None = None

    # --- lifecycle ----------------------------------------------------------

    def open(self, loop: asyncio.AbstractEventLoop) -> None:
        """Open the on-disk DB (WAL = durable by itself) and init the schema."""
        self._loop = loop
        os.makedirs(os.path.dirname(self.db_path) or ".", exist_ok=True)
        # Initialize schema on a fresh connection.
        conn = self._conn()
        conn.executescript(_SCHEMA)
        conn.commit()
        # NIP-40: add the expiration column to pre-existing DBs (created before this column).
        # CREATE TABLE IF NOT EXISTS won't alter an existing table, so do it explicitly +
        # idempotently. The index is created AFTER, outside the try, so it runs whether the column
        # was just added (old DB) or already present from CREATE TABLE (fresh DB).
        try:
            conn.execute("ALTER TABLE events ADD COLUMN expiration INTEGER")
            conn.commit()
        except sqlite3.OperationalError:
            pass  # column already exists
        conn.execute("CREATE INDEX IF NOT EXISTS idx_events_expiration ON events(expiration)")
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
        self._read_exec.shutdown(wait=False)

    def _conn(self) -> sqlite3.Connection:
        """Per-thread connection (executor threads each get their own), WAL + busy wait."""
        c = getattr(self._tls, "conn", None)
        if c is None:
            c = sqlite3.connect(self.db_path, timeout=10, check_same_thread=False)
            c.row_factory = sqlite3.Row
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("PRAGMA synchronous=NORMAL")
            c.execute("PRAGMA busy_timeout=5000")
            # Read/write cache: a big page cache keeps hot pages in RAM, mmap serves reads with
            # zero read() syscalls (big win for a disk DB's queries + existence checks), and
            # temp_store=MEMORY keeps sorts/joins off disk.
            c.execute(f"PRAGMA cache_size={int(self.cache_mb) * -1024}")   # negative = KiB
            c.execute(f"PRAGMA mmap_size={int(self.mmap_mb) * 1024 * 1024}")
            c.execute("PRAGMA temp_store=MEMORY")
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

    def _insert_one(self, conn: sqlite3.Connection, ev: dict, origin: str) -> bool:
        """Insert a single event on the given connection WITHOUT committing (so it can be
        batched). Returns whether a row was written. Raises on malformed input."""
        eid = ev["id"]
        kind = int(ev["kind"])
        pubkey = ev["pubkey"]
        created = int(ev["created_at"])
        tags = ev.get("tags") or []
        # NIP-40: parse the expiration timestamp (if any). An already-expired event is never
        # stored — applies to direct writes AND synced/bulk events uniformly.
        expiration = None
        for t in tags:
            if len(t) >= 2 and t[0] == "expiration":
                try:
                    expiration = int(t[1])
                except (ValueError, TypeError):
                    expiration = None
                break
        if expiration is not None and expiration <= int(time.time()):
            return False
        if True:
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
                "(id, pubkey, created_at, kind, content, tags, sig, raw, origin, expiration) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (eid, pubkey, created, kind, ev.get("content", ""),
                 json.dumps(tags, separators=(",", ":")), ev.get("sig", ""),
                 json.dumps(ev, separators=(",", ":")), origin, expiration))
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
            return True

    def _add_event_sync(self, ev: dict, origin: str) -> bool:
        conn = self._conn()
        try:
            ok = self._insert_one(conn, ev, origin)
            conn.commit()
            return ok
        except Exception as e:
            logger.warning("[nostr-relay] add_event %s failed: %s", ev.get("id", "")[:12], e)
            try:
                conn.rollback()
            except Exception:
                pass
            return False

    def _add_events_bulk_sync(self, events: list, origin: str) -> int:
        """Insert many events in ONE transaction — far fewer write round-trips than per-event
        add_event, which is the bottleneck when a backfill batch returns thousands of events."""
        conn = self._conn()
        stored = 0
        try:
            for ev in events:
                try:
                    if self._insert_one(conn, ev, origin):
                        stored += 1
                except Exception:
                    continue
            conn.commit()
        except Exception as e:
            logger.warning("[nostr-relay] bulk add failed: %s", e)
            try:
                conn.rollback()
            except Exception:
                pass
        return stored

    async def add_events_bulk(self, events: list, origin: str = "wot") -> int:
        return await self._w(self._add_events_bulk_sync, events, origin)

    def _filter_existing_sync(self, ids: list) -> set:
        if not ids:
            return set()
        conn = self._conn()
        out = set()
        # Chunk to stay under SQLite's parameter limit.
        for i in range(0, len(ids), 900):
            chunk = ids[i:i + 900]
            rows = conn.execute(
                f"SELECT id FROM events WHERE id IN ({','.join('?' * len(chunk))})", chunk
            ).fetchall()
            out.update(r["id"] for r in rows)
        return out

    async def filter_existing(self, ids: list) -> set:
        """Return the subset of `ids` already stored — ONE query instead of per-event has_event."""
        return await self._r(self._filter_existing_sync, ids)

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

    def _delete_by_words_sync(self, words: list) -> int:
        """Purge stored kind-1 notes whose content contains any blocked word (case-insensitive
        substring) — the same match blocked_word() uses, applied retroactively."""
        if not words:
            return 0
        conn = self._conn()
        removed = 0
        for w in words:
            like = "%" + w.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
            ids = [r["id"] for r in conn.execute(
                "SELECT id FROM events WHERE kind=1 AND LOWER(content) LIKE ? ESCAPE '\\'",
                (like,)).fetchall()]
            for i in range(0, len(ids), 900):
                chunk = ids[i:i + 900]
                ph = ",".join("?" * len(chunk))
                conn.execute(f"DELETE FROM event_tags WHERE event_id IN ({ph})", chunk)
                conn.execute(f"DELETE FROM events WHERE id IN ({ph})", chunk)
                removed += len(chunk)
        conn.commit()
        return removed

    async def delete_by_words(self, words: list) -> int:
        return await self._w(self._delete_by_words_sync, list(words))

    def _delete_by_langs_sync(self, blocked) -> int:
        """Purge stored kind-1 notes written in a blocked language — the same detection
        blocked_language() uses, applied retroactively. Language detection has no SQL form,
        so this scans kind-1 content once (cheap at relay scale; the live filter keeps the
        set small thereafter)."""
        blocked = set(blocked)
        if not blocked:
            return 0
        from .langfilter import detect_languages
        conn = self._conn()
        ids = [r["id"] for r in conn.execute("SELECT id, content FROM events WHERE kind=1")
               if detect_languages(r["content"]) & blocked]
        removed = 0
        for i in range(0, len(ids), 900):
            chunk = ids[i:i + 900]
            ph = ",".join("?" * len(chunk))
            conn.execute(f"DELETE FROM event_tags WHERE event_id IN ({ph})", chunk)
            conn.execute(f"DELETE FROM events WHERE id IN ({ph})", chunk)
            removed += len(chunk)
        conn.commit()
        return removed

    async def delete_by_langs(self, blocked) -> int:
        return await self._w(self._delete_by_langs_sync, set(blocked))

    def _bridged_pubkeys_sync(self, domains) -> set:
        """Scan the identity/relay-list events (+ anything carrying a `proxy` tag) and return the
        pubkeys whose author lives on a blocked bridge domain. Caller blocks + purges them."""
        domains = {d for d in domains if d}
        if not domains:
            return set()
        from .bridges import reveals_blocked_bridge
        conn = self._conn()
        out: set = set()
        rows = conn.execute("SELECT pubkey, kind, content, tags FROM events "
                            "WHERE kind IN (0,3,10002) OR tags LIKE '%\"proxy\"%'")
        for r in rows:
            try:
                tags = json.loads(r["tags"] or "[]")
            except Exception:
                tags = []
            ev = {"pubkey": r["pubkey"], "kind": r["kind"], "content": r["content"] or "", "tags": tags}
            if r["pubkey"] and reveals_blocked_bridge(ev, domains):
                out.add(r["pubkey"])
        return out

    async def bridged_pubkeys(self, domains) -> set:
        return await self._w(self._bridged_pubkeys_sync, set(domains))

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

    def _build_where(self, flt: dict):
        """Build the SQL WHERE clause + params for a NIP-01 filter. Returns (where, params),
        or None if the filter can't match anything (e.g. unparseable search)."""
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
                    return None
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
        # NIP-40: never serve an event past its expiration, even before the periodic purge
        # (see _prune_sync) has reclaimed it. Applied to every read (query/count/negentropy).
        where.append("(e.expiration IS NULL OR e.expiration > ?)")
        params.append(int(time.time()))
        return where, params

    def _query_one(self, conn: sqlite3.Connection, flt: dict) -> list:
        built = self._build_where(flt)
        if built is None:
            return []
        where, params = built
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

    def _neg_items_sync(self, filters: list, cap: int) -> list:
        """Lightweight (timestamp, id_bytes) set for NIP-77 negentropy, sorted by (ts, id)."""
        conn = self._conn()
        seen: dict = {}
        for flt in filters or []:
            built = self._build_where(flt or {})
            if built is None:
                continue
            where, params = built
            sql = "SELECT e.created_at, e.id FROM events e"
            if where:
                sql += " WHERE " + " AND ".join(where)
            sql += " ORDER BY e.created_at DESC LIMIT ?"
            params.append(cap)
            for r in conn.execute(sql, params).fetchall():
                try:
                    seen[r["id"]] = (int(r["created_at"]), bytes.fromhex(r["id"]))
                except ValueError:
                    continue
        return sorted(seen.values(), key=lambda x: (x[0], x[1]))

    async def neg_items(self, filters: list, cap: int = 500000) -> list:
        return await self._r(self._neg_items_sync, filters, cap)

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

    def _wot_add_sync(self, pubkeys: list) -> int:
        conn = self._conn()
        now = int(time.time())
        conn.executemany("INSERT OR IGNORE INTO wot (pubkey, depth, added_at) VALUES (?,1,?)",
                         [(p, now) for p in pubkeys if p])
        conn.commit()
        return len([p for p in pubkeys if p])

    async def wot_add(self, pubkeys: list) -> int:
        """Incrementally add members (new signups followed by the operator) WITHOUT a full rebuild —
        so they can post + receive DMs immediately, not after the daily upstream-driven rebuild."""
        return await self._w(self._wot_add_sync, list(pubkeys))

    def _wot_members_sync(self) -> set:
        return {r["pubkey"] for r in self._conn().execute("SELECT pubkey FROM wot").fetchall()}

    async def wot_members(self) -> set:
        return await self._r(self._wot_members_sync)

    def _wot_missing_metadata_sync(self) -> list:
        # Pubkeys lacking lookup metadata (kind-0 profile and/or kind-10002 relay list).
        # Computed with SET MATH over a few indexed kind-scans — NOT per-member correlated
        # subqueries, which over a 37k-member WoT took 30s+ and stalled the whole backfill.
        # Ordering prioritizes authors with VISIBLE content (notes/reposts/reactions) so their
        # avatars/names resolve first, then other no-profile members, then profile-but-no-relay.
        # Crucially this is NOT limited to WoT members: replies/quotes/boosts pull in non-WoT
        # authors whose events we store and the client renders — without backfilling their kind-0
        # those avatars stay default forever (the "still see missing" pics). add_event isn't
        # WoT-gated, so fetching + storing their profile is fine.
        conn = self._conn()
        wot = {r[0] for r in conn.execute("SELECT pubkey FROM wot").fetchall()}
        have_k0 = {r[0] for r in conn.execute("SELECT DISTINCT pubkey FROM events WHERE kind=0").fetchall()}
        have_relay = {r[0] for r in conn.execute("SELECT DISTINCT pubkey FROM events WHERE kind=10002").fetchall()}
        # Authors of anything the client paints an avatar for: notes, reposts, reactions.
        visible = {r[0] for r in conn.execute(
            "SELECT DISTINCT pubkey FROM events WHERE kind IN (1,6,7)").fetchall()}
        prio = [pk for pk in (wot - have_k0) if pk in visible]               # WoT visible authors
        ghost = [pk for pk in visible if pk not in have_k0 and pk not in wot]  # non-WoT visible authors
        rest = [pk for pk in (wot - have_k0) if pk not in visible]           # WoT members, no content
        relay_only = [pk for pk in (wot & have_k0) if pk not in have_relay]  # have profile, want relay list
        return prio + ghost + rest + relay_only

    async def wot_missing_metadata(self) -> list:
        return await self._r(self._wot_missing_metadata_sync)

    # --- checkpoint + prune -------------------------------------------------

    def _checkpoint_sync(self) -> None:
        try:
            self._conn().execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except Exception as e:
            logger.warning("[nostr-relay] checkpoint failed: %s", e)

    async def checkpoint(self) -> None:
        """Fold the WAL back into the main DB (e.g. on clean shutdown)."""
        await self._w(self._checkpoint_sync)

    def set_preserve_pubkeys(self, pubkeys) -> None:
        """Authors whose notes are NEVER pruned (local users / operators)."""
        self.preserve_pubkeys = frozenset(p for p in (pubkeys or []) if p)

    def _preserve_clause(self) -> str:
        """Extra SQL: exclude direct-write events (data entrusted to this relay) and local
        users' events from a prune DELETE. Pubkeys are our own 64-hex config values (safe to
        inline). `origin='direct'` = a client published here; `'wot'`/`'ancestor'` = synced feed."""
        cond = "origin != 'direct'"
        if self.preserve_pubkeys:
            vals = ",".join("'" + p + "'" for p in self.preserve_pubkeys if _is_hex64(p))
            if vals:
                cond += f" AND pubkey NOT IN ({vals})"
        return cond

    def _prune_sync(self) -> int:
        conn = self._conn()
        removed = 0
        prunable = ",".join(str(k) for k in _PRUNABLE_KINDS)
        preserve = self._preserve_clause()
        # NIP-40 expiration sweep FIRST — unconditional: an expired event is gone per the AUTHOR's
        # explicit intent, so unlike the age-based prune below this ignores kind allowlist AND the
        # preserve clause (even a local user's / profile / DM event with an `expiration` tag goes).
        cur = conn.execute(
            "DELETE FROM events WHERE expiration IS NOT NULL AND expiration <= ?",
            (int(time.time()),))
        removed += cur.rowcount or 0
        # Age-based auto-cleaner: delete only old feed content (notes/reposts/reactions/comments
        # — kinds in _PRUNABLE_KINDS). Everything else (profiles, contacts, relay/identity lists,
        # DMs, articles, …) is never touched, so important events survive indefinitely.
        if self.retention_days:
            cutoff = int(time.time()) - self.retention_days * 86400
            cur = conn.execute(
                f"DELETE FROM events WHERE created_at < ? AND kind IN ({prunable}) "
                f"AND {preserve}", (cutoff,))
            removed += cur.rowcount or 0
        # Hard count cap (memory bound): trim oldest prunable feed events beyond the limit.
        if self.max_events:
            cur = conn.execute(
                f"DELETE FROM events WHERE kind IN ({prunable}) AND {preserve} AND id IN "
                "(SELECT id FROM events ORDER BY created_at DESC LIMIT -1 OFFSET ?)",
                (self.max_events,))
            removed += cur.rowcount or 0
        # Hard byte cap (RAM bound): trim oldest prunable events until under budget.
        if self.max_db_mb:
            budget = self.max_db_mb * 1024 * 1024
            for _ in range(50):
                if self._db_bytes() <= budget:
                    break
                cur = conn.execute(
                    f"DELETE FROM events WHERE id IN (SELECT id FROM events "
                    f"WHERE kind IN ({prunable}) AND {preserve} "
                    "ORDER BY created_at ASC LIMIT 2000)")
                if not cur.rowcount:
                    break  # only kept/preserved events remain — don't spin
                removed += cur.rowcount
                conn.commit()
        # Drop orphaned tag rows.
        conn.execute("DELETE FROM event_tags WHERE event_id NOT IN (SELECT id FROM events)")
        conn.commit()
        return removed

    def _db_bytes(self) -> int:
        try:
            total = os.path.getsize(self.db_path)
            for ext in ("-wal", "-shm"):
                p = self.db_path + ext
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
