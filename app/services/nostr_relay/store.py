"""PostgreSQL storage for the built-in Nostr relay.

Postgres is the relay's (and the app's) one and only database — there is no SQLite. PG's shared
buffers keep the hot set in RAM; durability + concurrency are the server's job. All DB I/O still
runs off the relay's asyncio loop via executors so the loop never blocks: writes go through a
single-thread executor (serialized), reads use a small pool of executor threads — each thread holds
its own psycopg2 connection (autocommit; bulk inserts wrap a transaction). Memory/retention is
bounded by `prune()` (retention window + event count + byte budget via pg_database_size).

Schema mirrors a minimal NIP-01 relay: `events` + a single-letter `event_tags` index for
`#<letter>` filters, plus `wot` (the trust set) and `relay_kv` (cursors). NIP-50 search uses a
GIN `to_tsvector` index instead of SQLite's FTS5.
"""

import os
import re
import json
import time
import asyncio
import logging
import threading
from concurrent.futures import ThreadPoolExecutor

import psycopg2
import psycopg2.extras

logger = logging.getLogger(__name__)

# Passwordless localhost (PG `trust` auth) by default; deployments needing password auth (Docker/
# remote PG) inject credentials via the NOSTR_RELAY_PG_DSN env var / the nostr_relay_pg_dsn setting.
_DEFAULT_DSN = os.environ.get("NOSTR_RELAY_PG_DSN",
                              "host=127.0.0.1 port=5432 dbname=posterchan_relay user=posterchan")

# Replaceable event kind ranges (NIP-01): keep only the newest per (pubkey, kind) — and for
# the parameterized range, per (pubkey, kind, d-tag).
_REPLACEABLE = lambda k: k in (0, 3) or 10000 <= k < 20000
_PARAM_REPLACEABLE = lambda k: 30000 <= k < 40000

_HEXSET = frozenset("0123456789abcdef")
def _is_hex64(s) -> bool:
    return isinstance(s, str) and len(s) == 64 and set(s) <= _HEXSET


class _PgConn:
    """Thin shim giving a psycopg2 connection the sqlite3-style `.execute()/.executemany()/
    .commit()` surface the relay code was written against — so the query bodies stay unchanged.
    Translates the placeholder style (`?` → `%s`) and, when params are bound, escapes literal `%`
    (so `LIKE '%x%'` survives). Rows come back as DictRows (support both r[0] and r["col"])."""

    def __init__(self, raw):
        self._raw = raw

    def execute(self, sql, params=None):
        cur = self._raw.cursor(cursor_factory=psycopg2.extras.DictCursor)
        if params is None:
            cur.execute(sql.replace("?", "%s"))               # no binds → don't touch literal %
        else:
            cur.execute(sql.replace("%", "%%").replace("?", "%s"), params)
        return cur

    def executemany(self, sql, seq):
        cur = self._raw.cursor()
        cur.executemany(sql.replace("%", "%%").replace("?", "%s"), list(seq))
        return cur

    def executescript(self, script):
        cur = self._raw.cursor()
        cur.execute(script)        # multi-statement DDL, no binds / no % placeholders
        cur.close()

    def commit(self):
        self._raw.commit()

    def rollback(self):
        self._raw.rollback()

    @property
    def autocommit(self):
        return self._raw.autocommit

    @autocommit.setter
    def autocommit(self, v):
        self._raw.autocommit = v


_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id          TEXT PRIMARY KEY,
    pubkey      TEXT NOT NULL,
    created_at  BIGINT NOT NULL,
    kind        INTEGER NOT NULL,
    content     TEXT NOT NULL,
    tags        TEXT NOT NULL,
    sig         TEXT NOT NULL,
    raw         TEXT NOT NULL,
    origin      TEXT NOT NULL DEFAULT 'wot',
    expiration  BIGINT
);
CREATE INDEX IF NOT EXISTS idx_events_pubkey       ON events(pubkey);
CREATE INDEX IF NOT EXISTS idx_events_kind_created ON events(kind, created_at);
CREATE INDEX IF NOT EXISTS idx_events_created      ON events(created_at);
CREATE INDEX IF NOT EXISTS idx_events_expiration   ON events(expiration);
CREATE INDEX IF NOT EXISTS idx_events_content_fts  ON events USING gin (to_tsvector('simple', content));
-- The hottest read is a profile's own posts: authors=[pk] AND kinds IN (...) ORDER BY created_at DESC.
-- A composite (pubkey, kind, created_at DESC) serves it without a separate sort.
CREATE INDEX IF NOT EXISTS idx_events_pubkey_kind_created ON events(pubkey, kind, created_at DESC);

CREATE TABLE IF NOT EXISTS event_tags (
    event_id TEXT NOT NULL,
    tag      TEXT NOT NULL,
    value    TEXT NOT NULL,
    PRIMARY KEY (event_id, tag, value)
);
CREATE INDEX IF NOT EXISTS idx_event_tags_tv ON event_tags(tag, value);
-- Needed so deleting an event's tags (prune / NIP-09 / author purge) is an index lookup, not a scan.
CREATE INDEX IF NOT EXISTS idx_event_tags_event ON event_tags(event_id);

CREATE TABLE IF NOT EXISTS wot (
    pubkey   TEXT PRIMARY KEY,
    depth    INTEGER NOT NULL DEFAULT 0,
    added_at BIGINT NOT NULL
);

CREATE TABLE IF NOT EXISTS relay_kv (
    key   TEXT PRIMARY KEY,
    value TEXT
);

-- Fediverse-bridge NIP-05: a puppet's local-part → its derived pubkey. Populated as puppet kind-0
-- profiles are stored (server._register_bridge_nip05); the in-memory map is warmed from here on
-- start. Kept out of the `events`/WoT machinery — it's a pure name index for /.well-known/nostr.json.
CREATE TABLE IF NOT EXISTS bridge_nip05 (
    name   TEXT PRIMARY KEY,
    pubkey TEXT NOT NULL
);

-- Every fediverse-bridge puppet pubkey (independent of whether it got a NIP-05 name) so the relay's
-- DM-to-puppet inbox set survives a restart even when no NIP-05 domain is configured.
CREATE TABLE IF NOT EXISTS bridge_puppet (
    pubkey TEXT PRIMARY KEY
);
"""

# Puppet-addressed DM gift-wraps/seals are transient (consumed by write-back, then federated). Prune
# them on a short TTL so spamming derivable puppet npubs can't fill the disk (kind-1059 is otherwise
# never pruned). Kept longer than the write-back listener's DM replay window so a restart still sees them.
_BRIDGE_DM_TTL_DAYS = 4


# The age-based auto-cleaner deletes ONLY high-volume, reconstructable FEED content — never
# important events. Allowlist (not denylist) so a kind is pruned only if it is explicitly one
# of these: notes (1), reposts (6), reactions (7), NIP-22 comments (1111). EVERYTHING else is
# kept indefinitely — profiles (0), contacts (3), ALL replaceable identity/relay lists
# (10000-19999: relay list 10002, DM relays 10050, search relays 10007, blossom servers 10063,
# mute/bookmark/etc.), private DMs (legacy 4, NIP-59 seal 13, NIP-17 gift wrap 1059), and any
# other/unknown kind. (Client-published `origin='direct'` events are additionally never pruned
# regardless of kind — so the instance's OWN chat/articles/streams survive; only synced-in copies
# from the WoT firehose age out.) Prunable feed content: notes/reposts/reactions/comments, plus
# public-chat messages (42), long-form articles (30023) and live-stream events (30311) — all of
# which accumulate from the sync. Channel/community DEFINITIONS (40/34550) are kept (tiny, and
# needed to render rooms).
_PRUNABLE_KINDS = (1, 6, 7, 42, 1111, 30023, 30311)

# Events deleted per prune PASS. The writer thread is single, so this is the unit of ingestion stall:
# one pass' delete + its event_tags cleanup runs to completion before any queued write gets in.
# 20k keeps a pass to roughly a second while still clearing a large backlog in a handful of passes.
_PRUNE_CHUNK = 20000

# NIP-34 git-over-nostr events — a repo's source of truth (announcement 30617, repo state 30618,
# patches 1617, issues 1621, replies/PRs 1622, issue-status 1623, and status 1630-1633). These are
# the collaboration record; losing one loses code/history that isn't reconstructable from the WoT
# firehose. They are DELIBERATELY absent from _PRUNABLE_KINDS (so age/bridge/count-cap prunes never
# touch them — kept forever), and are also exempted from the NIP-40 expiration sweep below so a stray
# `expiration` tag can't quietly delete a repo. NEVER add any of these to _PRUNABLE_KINDS.
_GIT_KINDS = (30617, 30618, 1617, 1621, 1622, 1623, 1630, 1631, 1632, 1633)
assert not (set(_GIT_KINDS) & set(_PRUNABLE_KINDS)), "git kinds must never be prunable"

# A NIP-22 comment (kind 1111) whose ROOT is a NIP-34 issue (1621) / patch (1617) / PR (1618) is the
# collaboration record's CURRENT reply shape — NIP-34 dropped kind 1622, so gitworkshop et al. thread
# issue discussion as 1111 comments. In spirit these belong to _GIT_KINDS ("kept forever"), but the
# kind is shared with ordinary community/article comments that MUST keep aging out (the relay's bound
# on firehose growth), so the shield is TAG-scoped, not kind-scoped: NIP-22 repeats the root kind in
# an uppercase `K` tag at every depth, which idx_event_tags_tv (tag, value) answers cheaply. Every
# rule that says "prunable feed content" uses this predicate, so the exemption cannot drift per rule.
_GIT_COMMENT_ROOT_KINDS = ("1617", "1621", "1618")
_PRUNABLE_SQL = ("(kind IN (%s) AND NOT (kind = 1111 AND id IN "
                 "(SELECT event_id FROM event_tags WHERE tag = 'K' AND value IN (%s))))" % (
                     ",".join(str(k) for k in _PRUNABLE_KINDS),
                     ",".join("'%s'" % k for k in _GIT_COMMENT_ROOT_KINDS)))

# Kinds a NIP-40 `expiration` tag must NEVER be able to delete. The expiration sweep is otherwise
# unconditional (it ignores the kind allowlist AND the preserve clause, by design — an author's
# explicit intent), which makes a single stray tag a silent data-loss vector for anything that is
# somebody's only copy. Git events are a repo's source of truth; kind 30078 is this app's own
# datastore — settings, users, chats, Notes — and it is the one that would bite by ACCIDENT rather
# than by attack: NIP-37 (Draft Events) recommends stamping `expiration: now + 90 days`, so a note
# written or touched by any other client following that convention would quietly disappear 90 days
# later, from the relay that holds the only copy. Kept regardless, at ingest and in the sweep.
_NEVER_EXPIRE_KINDS = _GIT_KINDS + (30078,)
assert not (set(_NEVER_EXPIRE_KINDS) & set(_PRUNABLE_KINDS)), "never-expire kinds must never be prunable"


class RelayStore:
    def __init__(self, dsn: str = None, *,
                 read_workers: int = 4, max_events: int = 0,
                 retention_days: int = 30):
        # `dsn` is a libpq connection string. Postgres tunes its own buffers/WAL server-side, so
        # there are no SQLite-style page-cache/mmap/WAL knobs here — auto-clean is age + count only.
        # There is exactly ONE age window (retention_days) and it covers every origin, the fediverse
        # mirror included; the old per-bridge `bridge_retention_days` second cleaner is gone.
        self.dsn = dsn or _DEFAULT_DSN
        self.max_events = max_events
        self.retention_days = retention_days
        # Authors nothing auto-deletes: local users, this server's NIP-05 holders, and the puppets of
        # local users' linked fediverse accounts (see thread._collect_preserve_pubkeys).
        self.preserve_pubkeys: frozenset = frozenset()
        # --- pay-to-stay (OPTIONAL, and OFF unless an admin turns it on) --------------------
        # Everything a client published HERE (origin='direct') is data entrusted to this relay and
        # the rules above never touch it, at any age. `free_retention_days` is the one exception,
        # and it exists only when the admin enables the paid-retention feature: a direct write by an
        # author who has NO account here (not in preserve_pubkeys) and NO paid subscription ages out
        # after that window; a subscriber's keeps `paid_retention_days` (0 = forever). Both default
        # to 0, so on every node that never turns this on the prune behaves exactly as it always did.
        self.free_retention_days = 0
        self.paid_retention_days = 0
        self.subscriber_pubkeys: frozenset = frozenset()
        self.tiered_ok = False        # ledger READ succeeded → the tiered rules may run (fail-closed)
        self.paid_tier_enabled = False        # the admin master switch, mirrored here
        self._last_good_subscribers: frozenset = frozenset()   # see _subscriber_exempt
        self._tls = threading.local()
        self._write_exec = ThreadPoolExecutor(max_workers=1, thread_name_prefix="relay-db-w")
        self._read_exec = ThreadPoolExecutor(max_workers=read_workers, thread_name_prefix="relay-db-r")
        self._fts = True     # Postgres always has full-text search (to_tsvector)
        self._loop: asyncio.AbstractEventLoop | None = None
        self._repo_ann_cache: dict = {}   # (owner_hex, repo_id) -> (bool, expiry_ts): 30617 present?

    async def is_repo_announced(self, owner_hex: str, repo_id: str) -> bool:
        """True iff a PUBLIC NIP-34 repo announcement (kind 30617) for owner/repo exists on THIS relay.
        Private repos are NEVER announced (grasp_selfhost --private skips 30617), so a present 30617 ==
        public — this is what scopes issue/patch (1617/1621/…) acceptance to KNOWN repos WITHOUT leaking
        private ones, and without requiring this node to HOST the repo (so a proxy node whose relay the
        client reads still accepts collaboration for repos hosted on a peer). Cached (60s) so the collab
        gate never hits the DB per event."""
        owner = (owner_hex or "").strip().lower()
        rid = (repo_id or "").strip().lower()
        if rid.endswith(".git"):
            rid = rid[:-4]
        if len(owner) != 64 or not rid:
            return False
        key = (owner, rid)
        now = time.time()
        hit = self._repo_ann_cache.get(key)
        if hit and hit[1] > now:
            return hit[0]
        try:
            rows = await self.query([{"kinds": [30617], "authors": [owner], "#d": [rid], "limit": 1}],
                                    hard_cap=1)
            ok = bool(rows)
        except Exception:
            ok = False
        self._repo_ann_cache[key] = (ok, now + 60.0)
        return ok

    # --- lifecycle ----------------------------------------------------------

    def open(self, loop: asyncio.AbstractEventLoop) -> None:
        """Connect to Postgres and ensure the schema (idempotent)."""
        self._loop = loop
        conn = self._conn()
        conn.executescript(_SCHEMA)
        conn.commit()

    def close(self) -> None:
        self._write_exec.shutdown(wait=True)
        self._read_exec.shutdown(wait=False)

    def _conn(self) -> _PgConn:
        """Per-thread psycopg2 connection (each executor thread gets its own). Autocommit on:
        reads never leave an idle-in-transaction; the single write thread serializes writes, and
        the only place atomicity matters (bulk insert) flips autocommit off around its batch."""
        c = getattr(self._tls, "conn", None)
        if c is None or c._raw.closed:
            raw = psycopg2.connect(self.dsn, connect_timeout=10)
            raw.autocommit = True
            c = _PgConn(raw)
            self._tls.conn = c
        return c

    async def _w(self, fn, *a):
        return await self._loop.run_in_executor(self._write_exec, fn, *a)

    async def _r(self, fn, *a):
        return await self._loop.run_in_executor(self._read_exec, fn, *a)

    # --- writes -------------------------------------------------------------

    def _insert_one(self, conn: _PgConn, ev: dict, origin: str) -> bool:
        """Insert a single event on the given connection WITHOUT committing (so it can be
        batched). Returns whether a row was written. Raises on malformed input."""
        eid = ev["id"]
        kind = int(ev["kind"])
        pubkey = ev["pubkey"]
        created = int(ev["created_at"])
        # Reject far-future events (bad client clock or malicious): a stored future created_at permanently
        # freezes replaceable updates for that pubkey/kind (every real update compares as "older" → rejected
        # below) AND evades age-based retention (created_at never falls before the cutoff). 15-min skew
        # tolerance, matching common relay policy (NIP-22 upper bound).
        if created > int(time.time()) + 900:
            # SAY THE SKEW. This refusal is otherwise a generic "not stored, retry" — and a device
            # with a fast clock has EVERY write refused, silently, for as long as the clock is
            # wrong: keys that never save, records that never persist, resets that never stick.
            # One log line turns days of that into a sentence naming the device's clock.
            logger.info("[nostr-relay] refused kind=%s from %s…: created_at %ds in the FUTURE — "
                        "that device's clock is wrong", kind, str(pubkey)[:12],
                        created - int(time.time()))
            return False
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
        # _NEVER_EXPIRE_KINDS opt out entirely: the tag is DROPPED rather than merely un-swept.
        # Storing it and only exempting the sweep would still hide the event from every read —
        # the query builder filters on `expiration > now` — so the note would be intact on disk
        # and invisible, which is worse than deleted (it looks like corruption, not policy).
        if kind in _NEVER_EXPIRE_KINDS:
            expiration = None
        if expiration is not None and expiration <= int(time.time()):
            return False
        if True:
            # Replaceable-event handling: drop older versions so only the newest survives.
            if _REPLACEABLE(kind):
                cur = conn.execute(
                    "SELECT id, created_at, origin FROM events WHERE pubkey=? AND kind=?",
                    (pubkey, kind))
                for row in cur.fetchall():
                    # Same NIP-01 tie-break as the addressable branch below: equal created_at is
                    # settled by the LOWER id, not by arrival order. Kinds 0/3/10002 are the user's
                    # profile, follow list and relay list — the ones where two nodes disagreeing
                    # forever is most visible.
                    if row["id"] == eid:
                        continue
                    # Same direct-tie rule as the addressable branch below: a device saving its
                    # own profile/contacts twice in one second must not lose to its own write.
                    _tie_direct = (row["created_at"] == created and origin == "direct"
                                   and str(row["origin"] or "") == "direct")
                    if row["created_at"] < created or (row["created_at"] == created and eid < row["id"]) or _tie_direct:
                        self._delete_sync(conn, row["id"])
                    else:
                        return False  # a newer (or tie-winning) version already stored
            elif _PARAM_REPLACEABLE(kind):
                d = next((t[1] for t in tags if len(t) >= 2 and t[0] == "d"), "")
                # JOIN on the `d` tag — do NOT walk this author's events asking for each one's tag.
                #
                # That is one query per row, and the row set is every kind-30078 document the author
                # owns: this app's whole datastore lives in that kind, so a key with a calendar, an
                # addressbook and a mailbox reaches thousands. MEASURED on this node: 2405 documents
                # for one user, so storing ONE more message cost 2405 single-row lookups, and the
                # cost grew with every message stored. A mail sync of a few hundred was hundreds of
                # thousands of tiny queries — Postgres pinned at ~62% with nothing slow in
                # pg_stat_activity, because each query really was sub-millisecond.
                #
                # `idx_event_tags_tv (tag, value)` already exists, so this is one indexed lookup. An
                # empty `d` is the tagless case the old code represented as "" and must still match,
                # hence the LEFT JOIN branch rather than an inner join on a value that isn't there.
                if d:
                    cur = conn.execute(
                        "SELECT e.id, e.created_at, e.origin FROM events e "
                        "JOIN event_tags t ON t.event_id = e.id AND t.tag='d' AND t.value=? "
                        "WHERE e.pubkey=? AND e.kind=?", (d, pubkey, kind))
                else:
                    # An empty `d` is BOTH "no d tag at all" and an explicit ["d",""] — ingest
                    # indexes the empty value, so a NOT EXISTS alone missed the explicit form and
                    # every revision of that coordinate accumulated instead of replacing.
                    cur = conn.execute(
                        "SELECT e.id, e.created_at, e.origin FROM events e "
                        "WHERE e.pubkey=? AND e.kind=? AND (NOT EXISTS ("
                        "  SELECT 1 FROM event_tags t WHERE t.event_id = e.id AND t.tag='d')"
                        " OR EXISTS (SELECT 1 FROM event_tags t2 WHERE t2.event_id = e.id"
                        "            AND t2.tag='d' AND t2.value=''))",
                        (pubkey, kind))
                # Every row returned now IS a same-coordinate match, so there is nothing left to
                # compare but the NIP-01 tie-break.
                for row in cur.fetchall():
                    # On EQUAL created_at the lower event id wins. This used to hand a tie to the
                    # newcomer and delete the incumbent, which is not just non-conformant — it makes
                    # two relays that mirror each other disagree forever. Save the same note from two
                    # devices inside one second and each node keeps flipping to whatever the other
                    # last sent it, re-arming the mirror's "is this new?" guard on every flip,
                    # because the losing version is DELETED and so always looks new again. The rule
                    # has to be total and identical on both ends, and lowest-id is what the spec
                    # defines.
                    if row["id"] == eid:
                        continue
                    older = row["created_at"] < created
                    tie_lost = row["created_at"] == created and eid < row["id"]
                    # A DIRECT write from the author updating their OWN document wins a tie.
                    # The lowest-id rule is right for two RELAYS converging on one history and
                    # wrong for one device saving twice in one second: ids are random, so half of
                    # a user's rapid saves were refused "not stored, retry" — notes, prefs, sync
                    # manifests, measured six-a-second on a real account. Scoped to origin='direct'
                    # replacing origin='direct': mirrored copies still settle by the spec, so two
                    # nodes syncing each other cannot flip-flop.
                    tie_direct = (row["created_at"] == created and origin == "direct"
                                  and str(row["origin"] if "origin" in row.keys() else "") == "direct")
                    if older or tie_lost or tie_direct:
                        self._delete_sync(conn, row["id"])
                    else:
                        return False

            conn.execute(
                "INSERT INTO events "
                "(id, pubkey, created_at, kind, content, tags, sig, raw, origin, expiration) "
                "VALUES (?,?,?,?,?,?,?,?,?,?) ON CONFLICT (id) DO NOTHING",
                (eid, pubkey, created, kind, ev.get("content", ""),
                 json.dumps(tags, separators=(",", ":")), ev.get("sig", ""),
                 json.dumps(ev, separators=(",", ":")), origin, expiration))
            # Index single-letter tags only (NIP-01 queryable tags).
            for t in tags:
                if len(t) >= 2 and isinstance(t[0], str) and len(t[0]) == 1:
                    conn.execute(
                        "INSERT INTO event_tags (event_id, tag, value) VALUES (?,?,?) "
                        "ON CONFLICT DO NOTHING",
                        (eid, t[0], str(t[1])))
            # NIP-09: a kind-5 deletion removes the author's own events. `e` = by event id;
            # `a` = addressable (kind:pubkey:dtag) — used for article drafts (30024), articles
            # (30023), communities (34550), etc. Only the author's own, not-newer events go.
            if kind == 5:
                for t in tags:
                    if len(t) >= 2 and t[0] == "e":
                        # Author-gate BOTH deletes: strip the tag index ONLY if we actually removed the
                        # event (it was the kind-5 author's own). Otherwise a kind-5 referencing ANOTHER
                        # author's event id leaves the event but wipes its tag rows → silently unqueryable
                        # by #e/#p/#t (a data-loss vector, since the firehose ingests every author's kind-5).
                        # Concord's outer author is a shared stream key held by every member.
                        # Author-gating kind-5 would therefore let any member erase the room.
                        # Concord deletions travel inside a giftwrap and are folded by clients.
                        r = conn.execute(
                            "DELETE FROM events WHERE id=? AND pubkey=? AND kind<>1059 RETURNING id",
                            (t[1], pubkey)).fetchone()
                        if r:
                            conn.execute("DELETE FROM event_tags WHERE event_id=?", (t[1],))
                    elif len(t) >= 2 and t[0] == "a":
                        parts = str(t[1]).split(":", 2)
                        if len(parts) == 3 and parts[1] == pubkey and parts[0].isdigit():
                            rows = conn.execute(
                                "SELECT event_id FROM event_tags WHERE tag='d' AND value=? AND event_id IN "
                                "(SELECT id FROM events WHERE kind=? AND pubkey=? AND created_at<=?)",
                                (parts[2], int(parts[0]), pubkey, created)).fetchall()
                            for r in rows:
                                conn.execute("DELETE FROM events WHERE id=?", (r["event_id"],))
                                conn.execute("DELETE FROM event_tags WHERE event_id=?", (r["event_id"],))
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
        conn.autocommit = False   # one transaction for the whole batch (far fewer round-trips)
        try:
            for ev in events:
                # Per-row SAVEPOINT: in Postgres any statement error aborts the whole transaction,
                # so a bad event must be isolated or it would discard the entire batch.
                conn.execute("SAVEPOINT s")
                try:
                    ok = self._insert_one(conn, ev, origin)
                    conn.execute("RELEASE SAVEPOINT s")
                    if ok:
                        stored += 1
                except Exception:
                    conn.execute("ROLLBACK TO SAVEPOINT s")
            conn.commit()
        except Exception as e:
            logger.warning("[nostr-relay] bulk add failed: %s", e)
            try:
                conn.rollback()
            except Exception:
                pass
        finally:
            conn.autocommit = True
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

    def _delete_sync(self, conn: _PgConn, eid: str) -> None:
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
            # NEVER purge a registered user's events — even if they're flagged as a bridge (e.g. they
            # cross-post from the fediverse so a synced post carries a proxy/relay hint to a blocked
            # bridge) or explicitly blocklisted. Their history (incl. synced posts) is their data;
            # block/bridge handling only gates NEW writes, it must not delete first-party accounts.
            if pk in self.preserve_pubkeys:
                continue
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
        # Spare only LOCAL users' own notes (preserve/direct). A blocked word is blocked at INGEST for
        # everyone including WoT members (server.py has no WoT exemption), so the retroactive purge matches:
        # it also purges WoT members' matching notes. Blocked words are EXACT admin-defined strings (no
        # heuristic false-positive risk), so this can't delete legitimate content the way a bad lang guess
        # could. Kept consistent with _delete_by_langs_sync.
        preserve = self._preserve_clause()
        for w in words:
            like = "%" + w.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
            ids = [r["id"] for r in conn.execute(
                f"SELECT id FROM events WHERE kind=1 AND {preserve} AND LOWER(content) LIKE ? ESCAPE '\\'",
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
        # Spare only LOCAL users' own notes (preserve/direct). A blocked language is blocked at INGEST for
        # everyone including WoT members (server.py has no WoT exemption), so the retroactive purge must
        # match — otherwise a followed member's already-stored foreign posts linger forever. Only that
        # member's blocked-language notes go; their other-language posts don't match detect_languages.
        ids = [r["id"] for r in conn.execute(
                   f"SELECT id, content FROM events WHERE kind=1 AND {self._preserve_clause()}")
               if detect_languages(r["content"]) & blocked]
        # Spare thread ANCHORS: don't delete a note that a KEPT event still references via an e-tag —
        # deleting it orphans a surviving reply's whole thread (the "open a notification → no original
        # post, no threaded replies" bug). A note is an anchor if any event OUTSIDE this delete set
        # e-tags it (e.g. a blocked-language root with a surviving English reply). Its own blocked
        # descendants are in the set, so a thread that's blocked top-to-bottom is still fully removed.
        if ids:
            cand = set(ids)
            anchored = set()
            for i in range(0, len(ids), 900):
                chunk = ids[i:i + 900]
                ph = ",".join("?" * len(chunk))
                for row in conn.execute(
                        f"SELECT value, event_id FROM event_tags WHERE tag='e' AND value IN ({ph})", chunk):
                    if row["event_id"] not in cand:
                        anchored.add(row["value"])
            if anchored:
                ids = [x for x in ids if x not in anchored]
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

    def _bridge_identity_pubkeys_sync(self, domains) -> set:
        """nostrify DomainPolicy: authors whose kind-0 profile nip05 domain (or subdomain) is
        blocklisted. This is the definitive bridge-mirror identity, so the caller blocks these even
        when followed (it still spares operators). Separate from _bridged_pubkeys_sync, whose
        relay-list / proxy hints stay member-exempt to avoid false-positiving a real crossposter."""
        domains = {d for d in domains if d}
        if not domains:
            return set()
        from .bridges import author_on_blocked_bridge
        conn = self._conn()
        out: set = set()
        for r in conn.execute("SELECT pubkey, content FROM events WHERE kind=0"):
            ev = {"pubkey": r["pubkey"], "kind": 0, "content": r["content"] or "", "tags": []}
            if r["pubkey"] and author_on_blocked_bridge(ev, domains):
                out.add(r["pubkey"])
        return out

    async def bridge_identity_pubkeys(self, domains) -> set:
        return await self._w(self._bridge_identity_pubkeys_sync, set(domains))

    def _delete_by_proxy_sync(self) -> int:
        """Purge bridged PUBLIC POSTS: notes/reposts (kind 1,6) carrying a NIP-48 `proxy` tag
        (ActivityPub / atproto mirror content from mostr.pub, momostr.pink, ditto.pub, brid.gy, …).
        Scoped to timeline kinds via is_bridged_post so it NEVER touches DMs (kind 4 / NIP-17 1059) —
        a fediverse user DMing through a bridge sends proxy-tagged kind-4s, and deleting those ate
        incoming DMs. Preserve-aware too: a local user's own/direct events are never deleted.

        EXCLUDES origin='bridge' — OUR OWN built-in bridge's puppet content. Those posts are proxy-tagged
        too, but ingest deliberately keeps them (server.py: `not _is_puppet`), so purging them here
        contradicted that and silently deleted the fediverse posts a user had replied to (orphaning the
        thread: 'Replying to a post that couldn't be loaded'). This purge is for EXTERNAL bridge mirror
        content synced in (origin='wot'), not the bridge we run ourselves."""
        from .bridges import is_bridged_post
        conn = self._conn()
        preserve = self._preserve_clause()
        rows = conn.execute(
            f"SELECT id, kind, tags FROM events WHERE kind IN (1,6) AND {preserve} "
            f"AND origin != 'bridge' AND tags LIKE '%\"proxy\"%'").fetchall()
        ids = []
        for r in rows:
            try:
                tags = json.loads(r["tags"] or "[]")
            except Exception:
                tags = []
            if is_bridged_post({"kind": r["kind"], "tags": tags}):
                ids.append(r["id"])
        removed = 0
        for i in range(0, len(ids), 900):
            chunk = ids[i:i + 900]
            ph = ",".join("?" * len(chunk))
            conn.execute(f"DELETE FROM event_tags WHERE event_id IN ({ph})", chunk)
            conn.execute(f"DELETE FROM events WHERE id IN ({ph})", chunk)
            removed += len(chunk)
        conn.commit()
        return removed

    async def delete_by_proxy(self) -> int:
        return await self._w(self._delete_by_proxy_sync)

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
        # Stable local paging cursor. NIP-01's second-resolution `until` cannot advance when more
        # than one page shares a timestamp. `_cursor: [created_at, id]` follows this relay's
        # deterministic ORDER BY and is used only by PosterChan's private document store.
        cursor = flt.get("_cursor")
        if isinstance(cursor, list) and len(cursor) == 2:
            where.append("(e.created_at < ? OR (e.created_at = ? AND e.id < ?))")
            params.extend([int(cursor[0]), int(cursor[0]), str(cursor[1])])
        # NIP-50 full-text search over content (Postgres GIN to_tsvector index). plainto_tsquery
        # is injection-safe and tolerant of arbitrary input (empty/garbage → matches nothing).
        search = flt.get("search")
        if search:
            where.append("to_tsvector('simple', e.content) @@ plainto_tsquery('simple', ?)")
            params.append(search)
        # Tag filters: keys like "#e", "#p", "#t" → join event_tags (AND across tag keys).
        for key, vals in flt.items():
            if not (isinstance(key, str) and key.startswith("#") and len(key) == 2 and vals):
                continue
            where.append(
                "e.id IN (SELECT event_id FROM event_tags WHERE tag=? AND value IN "
                f"({','.join('?' * len(vals))}))")
            params.append(key[1])
            params += [str(v) for v in vals]
        # PREFIX tag filter — a LOCAL extension, not NIP-01, and the app's datastore depends on it.
        #
        # A `d` tag is a path (`pcai:mail:<account>:<folder>:<uid>`), but a Nostr filter can only
        # match a tag EXACTLY. So reading one folder meant asking for every kind-30078 document the
        # author owns and filtering in Python: measured on this node, opening a mail folder pulled
        # 5000 events and 91.9 MB across the socket to display 35 messages, in 2.6 seconds — and hit
        # the limit, so it was silently truncating as well. That is the read-side twin of the
        # quadratic write path, and it is why one user could saturate the relay by clicking Email.
        #
        # `{"#d~": ["pcai:mail:me@example.com:INBOX:"]}` matches by prefix instead, straight down
        # idx_event_tags_tv. Only this app's own store sends it; a stranger's client never will, and
        # a relay that does not understand it simply returns nothing rather than the wrong thing.
        for key, vals in flt.items():
            if not (isinstance(key, str) and len(key) == 3 and key.startswith("#")
                    and key.endswith("~") and vals):
                continue
            ors, tag = [], key[1]
            for v in vals:
                # LIKE, with the caller's text escaped: a `%` or `_` inside a d-tag must be a
                # literal, not a wildcard, or one document's name could read another's.
                esc = str(v).replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
                ors.append("(t2.tag=? AND t2.value LIKE ? ESCAPE '\\')")
                params.append(tag)
                params.append(esc + "%")
            where.append("EXISTS (SELECT 1 FROM event_tags t2 WHERE t2.event_id = e.id AND ("
                         + " OR ".join(ors) + "))")
        # NIP-40: never serve an event past its expiration, even before the periodic purge
        # (see _prune_sync) has reclaimed it. Applied to every read (query/count/negentropy).
        where.append("(e.expiration IS NULL OR e.expiration > ?)")
        params.append(int(time.time()))
        return where, params

    def _query_one(self, conn: _PgConn, flt: dict) -> list:
        built = self._build_where(flt)
        if built is None:
            return []
        where, params = built
        limit = int(flt.get("limit") or 500)
        limit = max(1, min(limit, 5000))
        sql = "SELECT e.raw FROM events e"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY e.created_at DESC, e.id DESC LIMIT ?"
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

    # --- fediverse-bridge NIP-05 name index ---------------------------------

    def _bridge_nip05_set_sync(self, name: str, pubkey: str) -> None:
        conn = self._conn()
        conn.execute("INSERT INTO bridge_nip05 (name, pubkey) VALUES (?,?) "
                     "ON CONFLICT(name) DO UPDATE SET pubkey=excluded.pubkey", (name, pubkey))
        conn.commit()

    async def bridge_nip05_set(self, name: str, pubkey: str) -> None:
        await self._w(self._bridge_nip05_set_sync, name, pubkey)

    def _bridge_nip05_all_sync(self) -> dict:
        conn = self._conn()
        return {r["name"]: r["pubkey"] for r in
                conn.execute("SELECT name, pubkey FROM bridge_nip05").fetchall()}

    async def bridge_nip05_all(self) -> dict:
        return await self._r(self._bridge_nip05_all_sync)

    def _bridge_puppet_add_sync(self, pubkey: str) -> None:
        conn = self._conn()
        conn.execute("INSERT INTO bridge_puppet (pubkey) VALUES (?) ON CONFLICT(pubkey) DO NOTHING", (pubkey,))
        conn.commit()

    async def bridge_puppet_add(self, pubkey: str) -> None:
        await self._w(self._bridge_puppet_add_sync, pubkey)

    def _bridge_puppets_all_sync(self) -> set:
        conn = self._conn()
        return {r["pubkey"] for r in conn.execute("SELECT pubkey FROM bridge_puppet").fetchall()}

    async def bridge_puppets_all(self) -> set:
        return await self._r(self._bridge_puppets_all_sync)

    # --- WoT membership -----------------------------------------------------

    def _wot_replace_sync(self, members: list, extra: list | None = None) -> int:
        """Replace the depth-1 WoT set; `extra` are always-trusted (operator) keys at depth 0."""
        conn = self._conn()
        now = int(time.time())
        rows = [(pk, 1, now) for pk in members] + [(pk, 0, now) for pk in (extra or [])]
        conn.autocommit = False   # atomic swap: no window where the trust set is empty
        try:
            conn.execute("DELETE FROM wot")
            conn.executemany(
                "INSERT INTO wot (pubkey, depth, added_at) VALUES (?,?,?) "
                "ON CONFLICT (pubkey) DO UPDATE SET depth=EXCLUDED.depth, added_at=EXCLUDED.added_at",
                rows)
            conn.commit()
        finally:
            conn.autocommit = True
        return conn.execute("SELECT COUNT(*) AS c FROM wot").fetchone()["c"]

    async def wot_replace(self, members: list, extra: list | None = None) -> int:
        return await self._w(self._wot_replace_sync, members, extra)

    def _wot_add_sync(self, pubkeys: list) -> int:
        conn = self._conn()
        now = int(time.time())
        conn.executemany("INSERT INTO wot (pubkey, depth, added_at) VALUES (?,1,?) "
                         "ON CONFLICT (pubkey) DO NOTHING",
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
        pass  # Postgres manages its own WAL/checkpoints — nothing to do client-side.

    async def checkpoint(self) -> None:
        """No-op on Postgres (kept for call-site compatibility, e.g. clean-shutdown hooks)."""
        await self._w(self._checkpoint_sync)

    def set_preserve_pubkeys(self, pubkeys) -> None:
        """Authors whose notes are NEVER pruned (local users / operators)."""
        self.preserve_pubkeys = frozenset(p for p in (pubkeys or []) if p)

    def extend_preserve_pubkeys(self, pubkeys) -> None:
        """UNION more authors into the preserve set — never removes any. Used by the prune/purge
        refresh so a partial/failed operator re-collection can't SHRINK the set and expose a user to
        deletion (preserve is deliberately grow-only; the publish gate is what tracks removals)."""
        add = frozenset(p for p in (pubkeys or []) if p)
        if add - self.preserve_pubkeys:
            self.preserve_pubkeys = self.preserve_pubkeys | add

    def set_subscribers(self, pubkeys, *, ledger_ok: bool) -> None:
        """Install the pay-to-stay subscriber set (authors with a live paid subscription).

        Unlike preserve, this set is REPLACED, not grown — a lapsed subscription has to be able to
        leave it. That makes an unreadable ledger indistinguishable from "nobody subscribed", which
        would delete exactly the data people paid to keep, so the read result is passed in
        explicitly: `ledger_ok=False` (relay unreachable, no ledger document, decrypt failed) turns
        the tiered rules OFF for the pass rather than running them against an empty set.

        A successful read is also remembered (`_last_good_subscribers`) for the SYNCED-content
        exemption below, which needs an answer even when this pass couldn't get a fresh one."""
        self.subscriber_pubkeys = frozenset(p for p in (pubkeys or []) if _is_hex64(p))
        self.tiered_ok = bool(ledger_ok)
        if ledger_ok:
            self._last_good_subscribers = self.subscriber_pubkeys

    def set_paid_tier_enabled(self, on: bool) -> None:
        """Master switch, mirrored onto the store. Turning it OFF drops the remembered subscriber set
        too — otherwise a stale in-memory copy would go on exempting people from the ordinary age
        prune on a relay whose operator has switched the whole feature off."""
        self.paid_tier_enabled = bool(on)
        if not self.paid_tier_enabled:
            self._last_good_subscribers = frozenset()

    def _subscriber_exempt(self) -> str:
        """`AND pubkey NOT IN (…)` for the ORDINARY age prune — the one that deletes SYNCED feed
        content — so a subscriber's notes aren't aged out from under them just because the copy we
        hold arrived over the firehose instead of being published here. Empty string (no exemption)
        when the feature is off or no subscriber set is known.

        This treats a failed ledger read DIFFERENTLY from the tiered rules, on purpose. There, an
        unreadable ledger disables the rule: a direct write can be the only copy in existence, and
        deleting it is unrecoverable. Here the row is a MIRROR of a note that lives on the relays it
        was synced from, and the rule it belongs to is the relay's only bound on firehose growth —
        so skipping the prune outright on a hiccup would trade a recoverable loss for unbounded
        disk. Instead it falls back to the last set successfully read: over-protecting a mirror is
        harmless, and the set still shrinks the moment a real read shows a subscription lapsed."""
        if not self.paid_tier_enabled:
            return ""
        subs = self.subscriber_pubkeys if self.tiered_ok else self._last_good_subscribers
        vals = ",".join("'" + p + "'" for p in sorted(subs) if _is_hex64(p))
        return f" AND pubkey NOT IN ({vals})" if vals else ""

    def _not_preserved(self) -> str:
        """SQL excluding local users' events (see preserve_pubkeys) from a DELETE. Pubkeys are our
        own 64-hex config values, so they're safe to inline."""
        if self.preserve_pubkeys:
            vals = ",".join("'" + p + "'" for p in self.preserve_pubkeys if _is_hex64(p))
            if vals:
                return f"pubkey NOT IN ({vals})"
        return "TRUE"

    def _preserve_clause(self) -> str:
        """Extra SQL: exclude direct-write events (data entrusted to this relay) and local
        users' events from a prune DELETE. `origin='direct'` = a client published here;
        `'wot'`/`'ancestor'` = synced feed."""
        cond = "origin != 'direct'"
        nots = self._not_preserved()
        if nots != "TRUE":
            cond += f" AND {nots}"
        return cond

    def _subscriber_sql(self) -> str:
        """The subscriber pubkey list as an SQL IN-list. Empty set → `''`, which no 64-hex pubkey can
        equal: `NOT IN ('')` is then true for everyone (free tier) and `IN ('')` false for everyone
        (the paid rule deletes nothing). An empty `IN ()` is a syntax error, so the sentinel matters."""
        vals = ",".join("'" + p + "'" for p in sorted(self.subscriber_pubkeys) if _is_hex64(p))
        return vals or "''"

    def _tiered_rules(self, now: int) -> list:
        """The pay-to-stay age rules as (label, where, params) — the ONLY rules that can delete a
        direct-published event. Empty (feature off) unless the admin set a free window AND the
        subscriber ledger was successfully read this pass. Shared by the prune and its preview so
        the two can't drift apart (the preview is what an admin trusts before deleting)."""
        if not self.free_retention_days or not self.tiered_ok:
            return []
        subs = self._subscriber_sql()
        # Same three qualifiers on both rules: only ever direct writes, only ever high-volume feed
        # kinds, and never an author with an account here.
        base = f"origin = 'direct' AND {_PRUNABLE_SQL} AND {self._not_preserved()}"
        out = [("aged_free", f"created_at < ? AND {base} AND pubkey NOT IN ({subs})",
                (now - self.free_retention_days * 86400,))]
        if self.paid_retention_days:
            # 0 = a subscription buys "kept forever", so there is no second rule at all.
            out.append(("aged_paid", f"created_at < ? AND {base} AND pubkey IN ({subs})",
                        (now - self.paid_retention_days * 86400,)))
        return out

    def _prune_preview_sync(self) -> dict:
        """DRY RUN: how many events _prune_sync would delete right now, broken down by rule. Same
        WHERE clauses as the real prune (kept adjacent so they can't drift) but COUNT, no DELETE —
        so an admin can see the damage before running an age prune that may span hundreds of
        thousands of rows on a live relay. The rules can overlap (an expired note may also be past
        the age window), so `total` is an UPPER BOUND, not an exact delete count."""
        conn = self._conn()
        preserve = self._preserve_clause()
        _keepk = ",".join(str(k) for k in _NEVER_EXPIRE_KINDS)
        now = int(time.time())

        def _n(sql, params=()):
            try:
                return int(conn.execute(sql, params).fetchone()["c"])
            except Exception:
                return 0

        expired = _n(f"SELECT COUNT(*) AS c FROM events WHERE expiration IS NOT NULL AND "
                     f"expiration <= ? AND kind NOT IN ({_keepk})", (now,))
        aged = 0
        if self.retention_days:
            aged = _n(f"SELECT COUNT(*) AS c FROM events WHERE created_at < ? AND "
                      f"{_PRUNABLE_SQL} AND {preserve}{self._subscriber_exempt()}",
                      (now - self.retention_days * 86400,))
        bridge_dm = _n("SELECT COUNT(*) AS c FROM events WHERE origin = 'bridge' AND "
                       "kind IN (13, 1059) AND created_at < ?", (now - _BRIDGE_DM_TTL_DAYS * 86400,))
        capped = 0
        if self.max_events:
            capped = _n(f"SELECT COUNT(*) AS c FROM events WHERE {_PRUNABLE_SQL} AND {preserve}"
                        f"{self._subscriber_exempt()} "
                        "AND id IN (SELECT id FROM events ORDER BY created_at DESC LIMIT ALL OFFSET ?)",
                        (self.max_events,))
        # Pay-to-stay (usually absent — the feature is off by default).
        tiered = {label: _n(f"SELECT COUNT(*) AS c FROM events WHERE {where}", params)
                  for label, where, params in self._tiered_rules(now)}
        return {"expired": expired, "aged": aged, "bridge_dm": bridge_dm, "capped": capped,
                **tiered,
                "total": expired + aged + bridge_dm + capped + sum(tiered.values()),
                "retention_days": self.retention_days, "max_events": self.max_events,
                "free_retention_days": self.free_retention_days,
                "paid_retention_days": self.paid_retention_days,
                "subscribers": len(self.subscriber_pubkeys), "tiered_ok": self.tiered_ok}

    async def prune_preview(self) -> dict:
        return await self._r(self._prune_preview_sync)

    def _prune_sync(self, limit: int = 0) -> tuple:
        """One prune PASS. `limit` (0 = unbounded, the old behaviour) caps how many events this pass
        may delete across all four rules; `prune()` calls it in a loop.

        Why bounded: every write goes through the store's single writer thread, and everything the
        relay ingests queues behind whatever is running on it. The unbounded form deleted the whole
        backlog plus its event_tags in ONE transaction — fine for a nightly pass that clears a day of
        feed, minutes of stalled ingestion for a first run with a few hundred thousand events behind
        it. Returns (removed, more) where `more` means a rule hit the cap and there is work left.
        """
        conn = self._conn()
        removed = 0
        gone: list = []   # ids deleted this pass → their event_tags must be removed too (no FK CASCADE)
        preserve = self._preserve_clause()
        budget = int(limit or 0)
        capped = False    # a rule was cut short by the budget → caller should run another pass

        def _delete(where: str, params: tuple) -> list:
            """DELETE ... WHERE <where>, clamped to the remaining budget. Bounding it as a subselect
            (rather than DELETE ... LIMIT, which Postgres has no such thing as) keeps each rule's
            predicate exactly as written below — the preserve/kind clauses are what stop this from
            eating data, so they must not be restated or paraphrased here."""
            nonlocal budget, capped
            if not limit:
                rows = conn.execute(f"DELETE FROM events WHERE {where} RETURNING id", params).fetchall()
                return [r["id"] for r in rows]
            if budget <= 0:
                capped = True
                return []
            rows = conn.execute(
                f"DELETE FROM events WHERE id IN (SELECT id FROM events WHERE {where} LIMIT ?) "
                f"RETURNING id", tuple(params) + (budget,)).fetchall()
            if len(rows) >= budget:
                capped = True
            budget -= len(rows)
            return [r["id"] for r in rows]
        # NIP-40 expiration sweep FIRST — unconditional: an expired event is gone per the AUTHOR's
        # explicit intent, so unlike the age-based prune below this ignores kind allowlist AND the
        # preserve clause (even a local user's / profile / DM event with an `expiration` tag goes).
        # EXCEPTION: _NEVER_EXPIRE_KINDS — git events (a repo's source of truth) and kind 30078
        # (this app's datastore: settings/users/chats/Notes). A stray `expiration` tag must not be
        # able to delete a repo or a note. Ingest already nulls the column for these kinds; this
        # clause covers rows written before that did, and is the belt to its braces.
        _keepk = ",".join(str(k) for k in _NEVER_EXPIRE_KINDS)
        ids = _delete(f"expiration IS NOT NULL AND expiration <= ? AND kind NOT IN ({_keepk})",
                      (int(time.time()),))
        gone += ids; removed += len(ids)
        # Age-based auto-cleaner — THE cleaner. Deletes only old feed content (kinds in
        # _PRUNABLE_KINDS: notes/reposts/reactions/comments + public chat/articles/streams), and only
        # what the preserve clause allows. Everything else (profiles, contacts, relay/identity lists,
        # DMs, channel/community defs, …) is never touched, at any age.
        #
        # This used to carry `AND origin != 'bridge'`, because fediverse-mirror content had a SECOND
        # age prune of its own driven by the "Mirror retention" setting. Two cleaners with two windows
        # and two meanings for 0 was the confusing part, and it hid a real bug: the bridge prune had no
        # preserve clause, so a bridged user's own fediverse history was aged out. There is now ONE
        # age window for every origin, and one preserve set that decides what survives it —
        # _collect_preserve_pubkeys covers local users, this server's NIP-05 holders, and the puppets
        # of local users' linked fediverse accounts (a bridged user's posts are authored by their
        # PUPPET key, never their npub, which is why preserving the npub alone never protected them).
        #
        # `_subscriber_exempt()` is appended (empty string unless pay-to-stay is on): a paying
        # author's notes are not aged out just because the copy we hold came over the firehose
        # rather than being published here — "your posts stay" would be a lie otherwise.
        if self.retention_days:
            cutoff = int(time.time()) - self.retention_days * 86400
            ids = _delete(f"created_at < ? AND {_PRUNABLE_SQL} AND {preserve}"
                          f"{self._subscriber_exempt()}", (cutoff,))
            gone += ids; removed += len(ids)
        # NOT retention, and deliberately not folded into the setting above: puppet-addressed DM
        # gift-wraps/seals (origin='bridge', kinds 13/1059) are undeliverable junk anyone can generate,
        # since puppet npubs are derivable. Auto-clean never touches DMs by design (1059 isn't a
        # prunable kind), so with no separate bound this is an unbounded write amplifier for a
        # stranger. Fixed short TTL, no knob.
        dmcut = int(time.time()) - _BRIDGE_DM_TTL_DAYS * 86400
        ids = _delete("origin = 'bridge' AND kind IN (13, 1059) AND created_at < ?", (dmcut,))
        gone += ids; removed += len(ids)
        # Pay-to-stay: the ONLY rules that ever delete a direct-published event, and they exist only
        # when an admin turned the feature on, set a free window, AND the subscriber ledger was read
        # this pass (_tiered_rules returns nothing otherwise, so every other node is unaffected).
        for _label, _where, _params in self._tiered_rules(int(time.time())):
            ids = _delete(_where, _params)
            gone += ids; removed += len(ids)
        # Hard count cap (memory bound): trim oldest prunable feed events beyond the limit. Exempts
        # subscribers for the same reason the age rule does — the cap already spares preserved
        # authors and every direct write, so a paying author is not the one to treat more harshly.
        if self.max_events:
            ids = _delete(
                f"{_PRUNABLE_SQL} AND {preserve}{self._subscriber_exempt()} AND id IN "
                "(SELECT id FROM events ORDER BY created_at DESC LIMIT ALL OFFSET ?)",
                (self.max_events,))
            gone += ids; removed += len(ids)
        # There is NO FK CASCADE — purge the deleted events' tags ourselves (batched), else event_tags
        # grows unbounded and slows every #e/#p/#t filter. (The old global NOT-IN anti-join pinned a
        # core for minutes; deleting only THIS pass's ids via the event_id index is cheap.)
        for i in range(0, len(gone), 500):
            chunk = gone[i:i + 500]
            conn.execute(f"DELETE FROM event_tags WHERE event_id IN ({','.join('?' * len(chunk))})", chunk)
        conn.commit()
        # One-time reclaim of orphans left by the previous (tag-leaking) prune. Guarded by a relay_kv
        # flag so it runs ONCE, here in the nightly prune (off-peak), not on every startup. NOT EXISTS
        # uses the event_tags(event_id) + events(id PK) indexes — far cheaper than the old NOT IN.
        try:
            done = conn.execute("SELECT value FROM relay_kv WHERE key='event_tags_orphans_cleaned'").fetchone()
            if not done:
                conn.execute("DELETE FROM event_tags WHERE NOT EXISTS "
                             "(SELECT 1 FROM events e WHERE e.id = event_tags.event_id)")
                conn.execute("INSERT INTO relay_kv (key, value) VALUES ('event_tags_orphans_cleaned','1') "
                             "ON CONFLICT (key) DO NOTHING")
                conn.commit()
        except Exception as e:
            logger.warning("[nostr-relay] one-time event_tags orphan cleanup skipped: %s", e)
        return removed, capped

    async def prune(self, chunk: int = _PRUNE_CHUNK) -> int:
        """Run the prune to completion in bounded passes, yielding the writer thread between them so
        queued ingest/publish writes interleave instead of waiting out the whole backlog. `chunk=0`
        restores the single-transaction behaviour. Each pass commits, so an interrupted prune (relay
        restart mid-run) keeps the work it already did and resumes on the next call."""
        total = 0
        while True:
            removed, more = await self._w(self._prune_sync, chunk)
            total += removed
            # `not removed` is the belt-and-braces exit: a rule that reports "capped" but deletes
            # nothing (e.g. rows vanishing under a concurrent NIP-09 delete) must not spin forever.
            if not more or not removed:
                break
            await asyncio.sleep(0.05)   # hand the writer thread back between passes
        return total

    def _count_sync(self) -> int:
        return self._conn().execute("SELECT COUNT(*) AS c FROM events").fetchone()["c"]

    async def count(self) -> int:
        return await self._r(self._count_sync)

    def _count_filtered_sync(self, filters: list, protect_nip78: bool = False) -> int:
        """COUNT(*) for a NIP-45 COUNT request — never materializes or json.loads rows (the old path
        loaded up to 1000 full kind-3 contact-list blobs just to len() them: the 'profile click' spike)."""
        conn = self._conn()
        total = 0
        for flt in (filters or []):
            try:
                built = self._build_where(flt)
            except Exception:
                continue   # malformed COUNT filter (e.g. kinds:"abc", since:"x") — skip it, don't crash the WS conn
            if built is None:
                continue
            where, params = built
            # COUNT filters are OR-ed and counted independently. Apply this per filter: a request
            # may combine one authenticated explicit NIP-78 filter with a broad public filter, and
            # the latter must not regain access to every author's private document count.
            kinds = flt.get("kinds") if isinstance(flt, dict) else None
            explicit_private = False
            if isinstance(kinds, list):
                for kind in kinds:
                    try:
                        explicit_private = explicit_private or int(kind) in (78, 30078)
                    except (TypeError, ValueError):
                        pass
            if protect_nip78 and not explicit_private:
                where.append("e.kind NOT IN (?,?)")
                params.extend((78, 30078))
            sql = "SELECT COUNT(*) AS c FROM events e"
            if where:
                sql += " WHERE " + " AND ".join(where)
            try:
                total += conn.execute(sql, params).fetchone()["c"]
            except Exception:
                continue
        return total

    async def count_filtered(self, filters: list, protect_nip78: bool = False) -> int:
        return await self._r(self._count_filtered_sync, filters, protect_nip78)
