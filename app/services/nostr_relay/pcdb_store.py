"""The relay's store with NO Postgres: PosterChanDB is the only copy (`POSTERCHANDB_MODE=primary`, task #161).

`PcdbRelayStore` has RelayStore's whole public interface -- thread.py, server.py and ingest.py cannot tell the
two apart -- and keeps everything RelayStore kept in Postgres:

  * events + tags: a posterchandb `Store`. It already applies the relay's ingest rules exactly (replaceable /
    addressable, NIP-09, NIP-40, duplicates, the derived quote index); tests/test_posterchandb_vs_relay.py holds
    the two to the same decisions and the same answers. Nothing here re-decides an ingest rule.
  * relay_kv, wot, bridge_nip05, bridge_puppet: a small SIDECAR in the same directory, one JSON file per table,
    replaced atomically on every change (temp file, fsync, rename, fsync of the directory) -- so a crash at any
    instant leaves the last complete state, never a torn one.
  * the auto-clean (`_prune_sync`), its preview and the content purges: the SAME rules as store.py, read from its
    constants, evaluated over the store's numpy columns in CHUNKS. A pass never holds the store lock for more
    than one chunk (SCAN_ROWS rows copied out, KILL_BATCH deaths, DECODE_BATCH records decoded), because a
    465 ms hold of that lock starved the live relay once (docs/POSTERCHANDB.md).

THE DIRECTORY MUST BE PROMOTED FIRST. `open()` refuses (PrimaryNotReady) a directory without a PRIMARY marker:
scripts/posterchandb_promote.py writes it after proving the mirror directory equals Postgres, or `--new` writes
it for a node that never had Postgres. A marker from a promotion is honoured only while the mirror's CLEAN token
is the one it was promoted against -- a mirror that ran after the promotion means the sidecar is stale. Opening
consumes CLEAN: from here on this directory is the truth and Postgres is not, so a later `serve` start must copy
afresh rather than trust a token that still matches the Postgres it no longer equals.
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import numpy as np

from app.services.posterchandb import cache as cache_mod
from app.services.posterchandb import maintenance as maint_mod
from app.services.posterchandb.store import (DROPPED, ORIGINS, Store, _h, is_addressable,
                                             is_replaceable)

from . import store as pg
from .store import RelayStore

logger = logging.getLogger(__name__)

MARKER = "PRIMARY"
CLEAN = "CLEAN"                       # posterchandb/mirror.py's clean-stop marker
SIDECAR_TABLES = ("kv", "wot", "bridge_nip05", "bridge_puppet")
SIDECAR_FORMAT = 1

SCAN_ROWS = 65536       # rows copied out of the columns per lock hold
KILL_BATCH = 512        # deaths (OP_DEAD markers) per lock hold
DECODE_BATCH = 128      # records decoded per lock hold
ID_BATCH = 256          # id lookups per lock hold (an anchor check is a posting lookup each)
WORDS_INLINE = 25       # deferred search words a search may index inline (~8 ms); more = "not ready"

_DIRECT = ORIGINS["direct"]
_BRIDGE = ORIGINS["bridge"]
_PRUNABLE = np.asarray(pg._PRUNABLE_KINDS, dtype=np.uint32)
_NEVER_EXPIRE = np.asarray(pg._NEVER_EXPIRE_KINDS, dtype=np.uint32)
_RETIRED = np.asarray(pg._RETIRED_KINDS, dtype=np.uint32)
_HIDDEN_PRE = re.compile("[​‌‍⁠﻿]")
_B64_PRE = re.compile(r"\A\s*[A-Za-z0-9+/]{38,}={0,2}\s*\Z")

_COLS = {"created": np.uint64, "kind": np.uint32, "expires": np.uint64, "origin": np.uint8,
         "author": np.uint32, "dead": np.uint8}


class PrimaryNotReady(RuntimeError):
    """The directory is not (or no longer) a promoted primary store. Nothing was changed."""


class SearchNotReady(RuntimeError):
    """A search arrived while the words of events replayed at startup are still being indexed. Raised, never
    answered empty: a read that could not answer is not "no results" (the client gets CLOSED with the reason)."""


# ------------------------------------------------------------------------------------------------ files
def _fsync_dir(path: str) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def atomic_write_json(path: str, obj) -> None:
    """Replace `path` with `obj` as JSON so a crash at ANY point leaves either the old file or the new one."""
    d = os.path.dirname(path) or "."
    tmp = "%s.tmp.%d.%d" % (path, os.getpid(), threading.get_ident())
    data = json.dumps(obj, separators=(",", ":"), sort_keys=True).encode("utf-8")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        view = memoryview(data)
        while view:
            view = view[os.write(fd, view):]
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, path)
    _fsync_dir(d)


def read_marker(path: str):
    try:
        with open(os.path.join(path, MARKER)) as f:
            return json.load(f)
    except FileNotFoundError:
        return None


def write_marker(path: str, data: dict) -> None:
    atomic_write_json(os.path.join(path, MARKER), dict(data, format=SIDECAR_FORMAT))


def retire_marker(path: str) -> None:
    """The relay runs on Postgres (off/shadow/serve): this directory is a mirror again, not the primary's, and a
    later `primary` start must be promoted afresh (the sidecar would be stale). thread._start_mirror."""
    try:
        os.remove(os.path.join(path, MARKER))
    except OSError:
        pass


def read_clean_token(path: str):
    try:
        with open(os.path.join(path, CLEAN)) as f:
            return f.read().strip() or None
    except OSError:
        return None


class Sidecar:
    """relay_kv / wot / bridge_nip05 / bridge_puppet without SQL: one JSON file per table, each replaced
    atomically on every change. The relay writes them from its single writer thread; readers get copies."""

    def __init__(self, path: str):
        self.path = path
        self._lock = threading.Lock()
        os.makedirs(path, exist_ok=True)
        for name in os.listdir(path):                  # a crash mid-write leaves only a temp file behind
            if name.startswith("sidecar-") and ".tmp." in name:
                try:
                    os.remove(os.path.join(path, name))
                except OSError:
                    pass
        self.kv: dict = self._load("kv", {})
        self.wot: dict = self._load("wot", {})
        self.bridge_nip05: dict = self._load("bridge_nip05", {})
        self.bridge_puppet: set = set(self._load("bridge_puppet", []))

    def file(self, table: str) -> str:
        return os.path.join(self.path, "sidecar-%s.json" % table)

    def _load(self, table: str, empty):
        try:
            with open(self.file(table), "rb") as f:
                doc = json.loads(f.read().decode("utf-8"))
        except FileNotFoundError:
            return empty
        if not isinstance(doc, dict) or doc.get("table") != table or doc.get("format") != SIDECAR_FORMAT:
            # A file this code did not write. Refusing is the only safe answer: an empty WoT or kv read as
            # "nothing stored" would rebuild the trust set and forget the pinned (never-pruned) authors.
            raise ValueError("unreadable relay sidecar %s" % self.file(table))
        return doc["data"]

    def save(self, table: str) -> None:
        data = getattr(self, table)
        if table == "bridge_puppet":
            data = sorted(data)
        atomic_write_json(self.file(table), {"format": SIDECAR_FORMAT, "table": table, "data": data})

    def replace_all(self, kv: dict, wot: dict, bridge_nip05: dict, bridge_puppet) -> None:
        with self._lock:
            self.kv, self.wot, self.bridge_nip05 = dict(kv), dict(wot), dict(bridge_nip05)
            self.bridge_puppet = set(bridge_puppet)
            for t in SIDECAR_TABLES:
                self.save(t)


# ------------------------------------------------------------------------------------------------ housekeeping
class _Housekeeper(maint_mod.Maintainer):
    """Compaction and snapshots only. The relay's prune (thread.py, nightly) owns every deletion decision, with
    the relay's own settings -- maintenance.Policy would run a second, differently-configured cleaner."""

    def run_pass(self) -> dict:
        c = self.store.compact(pace=self.throttle)
        return {"compacted": c.get("compacted"),
                "snapshot": self._maybe_snapshot(after_compaction=bool(c.get("compacted")))}


# ------------------------------------------------------------------------------------------------ the store
class PcdbRelayStore(RelayStore):
    """RelayStore's interface over PosterChanDB alone. Inherits only Postgres-free code (the pay-to-stay and
    preserve bookkeeping, `admit`, `is_repo_announced`, the async wrappers and `prune()`'s pass loop); every
    method that touched SQL is replaced, and `_conn` RAISES so a method that was missed fails loudly instead of
    quietly reconnecting to a Postgres this node no longer trusts."""

    def __init__(self, path: str, *, read_workers: int = 4, max_events: int = 0, retention_days: int = 30,
                 flush_interval: float = 300.0, cache_mb: float = 0, log=None, maintenance: bool = True):
        super().__init__("", read_workers=read_workers, max_events=max_events, retention_days=retention_days)
        self.dsn = None
        self.path = path
        self.flush_interval = float(flush_interval)
        self.cache_mb = float(cache_mb or 0)
        self.log = log or logger.info
        self.maintenance = maintenance
        self.store: Store | None = None
        self.sidecar: Sidecar | None = None
        self._scan_exec = ThreadPoolExecutor(max_workers=1, thread_name_prefix="relay-pcdb-scan")
        self._stop = threading.Event()
        self._flusher = None
        self._words = None
        self._maint = None
        self._gov = None
        self.lock_stats = {"max_hold_ms": 0.0}

    # ------------------------------------------------------------ lifecycle
    def open(self, loop) -> None:
        self._loop = loop
        claim_directory(self.path)
        self.sidecar = Sidecar(self.path)
        self._gov = cache_mod.CacheGovernor(budget_mb=self.cache_mb, log=self.log)
        # direct_durable OFF in the Store: a direct write is made durable by sync() below instead -- the same
        # guarantee (fsynced before the relay answers OK) without an index merge per write or an fsync under the
        # lock every reader takes.
        self.store = Store(self.path, flush_interval=self.flush_interval, direct_durable=False,
                           admit=self._gov.admit, log=self.log)
        self._gov.store = self.store
        self._gov.start()
        self.log("[nostr-relay] PosterChanDB PRIMARY store open at %s: %s" % (self.path, self.store.last_open))
        self._flusher = threading.Thread(target=self._flush_loop, name="relay-pcdb-flush", daemon=True)
        self._flusher.start()
        if self.store.words_pending:
            self._words = threading.Thread(target=self._index_words, name="relay-pcdb-words", daemon=True)
            self._words.start()
        if self.maintenance:
            self._maint = _Housekeeper(self.store, log=self.log, snapshot_hours=1.0)
            self._maint.start()

    def _flush_loop(self) -> None:
        while not self._stop.wait(1.0):
            try:
                self.store.maybe_flush()
            except Exception as e:      # noqa: BLE001 -- retried next second; the log says so
                self.log("[nostr-relay] PosterChanDB flush failed: %r" % (e,))

    def _index_words(self) -> None:
        maint_mod.lower_priority()
        st = self.store
        while not self._stop.is_set() and st.words_pending:
            st.index_pending_words(25)
            self._stop.wait(0.01)

    def close(self) -> None:
        def _shut():
            self._writes_closed = True          # nothing is stored or deleted after the final flush
        try:
            self._write_exec.submit(_shut).result(timeout=60)
        except Exception:      # noqa: BLE001 -- already shut down
            self._writes_closed = True
        self._stop.set()
        for t in (self._flusher, self._words):
            if t is not None:
                t.join(2.0)
        if self._maint is not None:
            self._maint.stop(timeout=2.0)
        if self._gov is not None:
            self._gov.stop(timeout=1.0)
        self._write_exec.shutdown(wait=True)
        self._scan_exec.shutdown(wait=True)
        self._read_exec.shutdown(wait=False)
        if self.store is not None:
            # No snapshot: the stop has systemd's 10 s, the log is complete, the hourly snapshot bounds the replay.
            self.store.close(take_snapshot=False)

    def _conn(self):
        raise RuntimeError("PosterChanDB primary store: there is no Postgres (a RelayStore method was not ported)")

    def attach_mirror(self, m) -> None:
        raise RuntimeError("the primary store has no mirror: it IS the store")

    def detach_mirror(self):
        raise RuntimeError("the primary store has no mirror: it IS the store")

    async def checkpoint(self) -> None:
        """Clean-shutdown hook: everything written so far, fsynced."""
        await self._w(self._checkpoint_sync)

    def _checkpoint_sync(self) -> None:
        if self.store is not None:
            self.store.flush()

    def stats(self) -> dict:
        out = {"mode": "primary", "state": "ready" if self.store is not None else "closed"}
        if self.store is not None:
            out.update({k: v for k, v in self.store.stats().items() if isinstance(v, (int, float))})
            out["search_words_pending"] = self.store.words_pending
        if self.sidecar is not None:
            out.update({"wot": len(self.sidecar.wot), "kv_keys": len(self.sidecar.kv),
                        "bridge_puppets": len(self.sidecar.bridge_puppet)})
        out["max_lock_hold_ms"] = round(self.lock_stats["max_hold_ms"], 2)
        return out

    # ------------------------------------------------------------ helpers: chunked access to the store
    def _held(self, t0: float) -> None:
        ms = (time.perf_counter() - t0) * 1000.0
        if ms > self.lock_stats["max_hold_ms"]:
            self.lock_stats["max_hold_ms"] = ms

    def _n(self) -> int:
        with self.store._lock:
            return len(self.store.off)

    def _chunks(self, cols, hi: int | None = None, rows: int = SCAN_ROWS):
        """Yield (lo, hi, {col: copy}) over rows [0, hi): the lock is held only while one chunk is copied out.
        `hi` is fixed when the scan starts -- events stored meanwhile belong to the next pass."""
        st = self.store
        n = self._n() if hi is None else hi
        for lo in range(0, n, rows):
            b = min(n, lo + rows)
            with st._lock:
                t0 = time.perf_counter()
                out = {c: np.frombuffer(getattr(st, c), dtype=_COLS[c])[lo:b].copy() for c in cols}
                self._held(t0)
            yield lo, b, out

    def _author_ids(self, pubkeys) -> np.ndarray:
        st = self.store
        with st._lock:
            got = [st._author_ix[p] for p in (pubkeys or ()) if p in st._author_ix]
        return np.asarray(sorted(got), dtype=np.uint32)

    def _posting_seqs(self, keys) -> np.ndarray:
        """Union of the posting lists of these index keys (dead included -- callers check liveness)."""
        st = self.store
        parts = []
        for k in keys:
            with st._lock:
                t0 = time.perf_counter()
                parts.append(np.asarray(st.idx.get(_h(k)), dtype=np.uint32).copy())
                self._held(t0)
        parts = [p for p in parts if len(p)]
        return np.unique(np.concatenate(parts)) if parts else np.zeros(0, dtype=np.uint32)

    def _git_comment_seqs(self) -> np.ndarray:
        """1111 comments rooted at a git issue/patch/PR (`K` tag): the relay's _PRUNABLE_SQL exclusion."""
        return self._posting_seqs(["t:K:%s" % k for k in pg._GIT_COMMENT_ROOT_KINDS])

    def _select(self, pred, cols, hi: int | None = None) -> np.ndarray:
        """Live seqs where pred(chunk, seqs_of_chunk) is True, ascending."""
        out = []
        for lo, b, c in self._chunks(tuple(cols) + ("dead",), hi):
            seqs = np.arange(lo, b, dtype=np.uint32)
            m = (c["dead"] == 0) & pred(c, seqs)
            if m.any():
                out.append(seqs[m])
        return np.concatenate(out) if out else np.zeros(0, dtype=np.uint32)

    def _kill_seqs(self, seqs) -> int:
        n = 0
        st = self.store
        seqs = np.asarray(seqs, dtype=np.int64)
        for i in range(0, len(seqs), KILL_BATCH):
            with st._lock:
                t0 = time.perf_counter()
                n += st.kill(seqs[i:i + KILL_BATCH])
                self._held(t0)
        return n

    def _decode(self, seqs, fn) -> None:
        """fn(seq, event, origin) for each still-live seq, DECODE_BATCH records per lock hold."""
        st = self.store
        seqs = [int(s) for s in seqs]
        for i in range(0, len(seqs), DECODE_BATCH):
            batch = []
            with st._lock:
                t0 = time.perf_counter()
                for s in seqs[i:i + DECODE_BATCH]:
                    if not st.dead[s] and st.seg[s] != DROPPED:
                        batch.append((s, st.get(s), st.origin[s]))
                self._held(t0)
            for s, ev, o in batch:       # the predicate (a language detector, a word matcher) runs unlocked
                fn(s, ev, o)

    def _live_seqs_of(self, ids) -> list:
        st = self.store
        out = []
        ids = list(ids)
        for i in range(0, len(ids), ID_BATCH):
            with st._lock:
                t0 = time.perf_counter()
                for eid in ids[i:i + ID_BATCH]:
                    s = st._seq_of(eid) if isinstance(eid, str) else None
                    out.append(s if (s is not None and not st.dead[s]) else None)
                self._held(t0)
        return out

    def _on_writer(self, fn, *a):
        """Run a mutation on the writer thread (every event write's thread) and wait for it -- from any thread."""
        if threading.current_thread().name.startswith("relay-db-w"):
            return fn(*a)
        return self._write_exec.submit(fn, *a).result()

    async def _s(self, fn, *a):
        """A long read-only scan: its own thread, so neither the writer nor the query pool waits behind it."""
        return await self._loop.run_in_executor(self._scan_exec, fn, *a)

    # ------------------------------------------------------------ writes
    def _stored(self, ev: dict, origin: str, result: str) -> bool:
        """RelayStore's answer for one put: stored, or an id it already held (ON CONFLICT DO NOTHING) -- except a
        held replaceable/addressable event that a NEWER version of its coordinate beats, which Postgres refuses."""
        if result == "stored":
            return True
        if result != "duplicate":
            return False
        kind = int(ev["kind"])
        if not (is_replaceable(kind) or is_addressable(kind)):
            return True
        st = self.store
        created, eid = int(ev["created_at"]), ev["id"]
        strict = kind == 10133 if is_replaceable(kind) else kind in pg._STRICT_TIE_KINDS
        with st._lock:
            s0 = st._seq_of(eid)
            for s in st._same_coordinate(ev, kind):
                if s == s0:
                    continue
                rc = st.created[s]
                tie_direct = (not strict and rc == created and origin == "direct" and st.origin[s] == _DIRECT)
                if not (rc < created or (rc == created and eid < st._id_hex(s)) or tie_direct):
                    return False
        return True

    def _put(self, ev: dict, origin: str) -> bool:
        created = int(ev["created_at"])
        if created > int(time.time()) + 900:
            logger.info("[nostr-relay] refused kind=%s from %s…: created_at %ds in the FUTURE — "
                        "that device's clock is wrong", ev.get("kind"), str(ev.get("pubkey"))[:12],
                        created - int(time.time()))
        return self._stored(ev, origin, self.store.put(ev, origin=origin))

    def _add_event_sync(self, ev: dict, origin: str) -> bool:
        if self._writes_closed:
            return False
        try:
            ok = self._put(ev, origin)
            if origin == "direct":
                self.store.sync()            # the relay is about to tell the author "saved"
            return ok
        except Exception as e:      # noqa: BLE001 -- one malformed event costs only itself
            logger.warning("[nostr-relay] add_event %s failed: %s", str(ev.get("id", ""))[:12], e)
            return False

    def _add_events_bulk_sync(self, events: list, origin: str) -> int:
        if self._writes_closed:
            return 0
        stored = 0
        for ev in events:
            try:
                if self._put(ev, origin):
                    stored += 1
            except Exception:      # noqa: BLE001 -- the Postgres store's per-row SAVEPOINT: skip just this one
                continue
        if origin == "direct":
            self.store.sync()
        return stored

    def _filter_existing_sync(self, ids: list) -> set:
        if not ids:
            return set()
        return {eid for eid, s in zip(ids, self._live_seqs_of(ids)) if s is not None}

    def _has_sync(self, eid: str) -> bool:
        return self._live_seqs_of([eid])[0] is not None

    # ------------------------------------------------------------ deletes (admin / moderation)
    def _delete_pubkeys_sync(self, pubkeys: list, spare_preserved: bool = True) -> int:
        if self._writes_closed or not pubkeys:
            return 0
        removed = 0
        for pk in pubkeys:
            if spare_preserved and pk in self.preserve_pubkeys:
                continue
            seqs = self._posting_seqs(["au:%s" % pk])
            removed += self._kill_seqs(seqs)
        self.store.sync()
        return removed

    def _preserve_ok_pred(self):
        """The relay's _preserve_clause as a chunk predicate: not a direct write, not a preserved author."""
        pres = self._author_ids(self.preserve_pubkeys)
        return lambda c: (c["origin"] != _DIRECT) & ~np.isin(c["author"], pres)

    def _content_candidates(self, kind_pred) -> np.ndarray:
        ok = self._preserve_ok_pred()
        return self._select(lambda c, s: kind_pred(c["kind"]) & ok(c), ("kind", "origin", "author"))

    def _delete_ids_sparing_anchors(self, ids: list) -> int:
        """The relay's _delete_sparing_anchors: never delete a note a SURVIVING event e-tags (it would orphan that
        reply's thread); doomed descendants are in `ids`, so a thread condemned top to bottom still goes."""
        if self._writes_closed:
            return 0
        st = self.store
        if ids:
            cand = set(ids)
            anchored = set()
            for i in range(0, len(ids), ID_BATCH):
                with st._lock:
                    t0 = time.perf_counter()
                    for eid in ids[i:i + ID_BATCH]:
                        for r in st.idx.get(_h("t:e:%s" % eid)):
                            r = int(r)
                            if not st.dead[r] and st.seg[r] != DROPPED and st._id_hex(r) not in cand:
                                anchored.add(eid)
                                break
                    self._held(t0)
            if anchored:
                ids = [x for x in ids if x not in anchored]
        self._kill_seqs([s for s in self._live_seqs_of(ids) if s is not None])
        self.store.sync()
        return len(ids)

    def _delete_by_words_sync(self, words: list) -> int:
        """store.py _delete_by_words_sync: the live filter's predicate, every kind but the never-word-filtered
        ones, sparing local users' own notes and thread anchors."""
        if self._writes_closed:
            return 0
        words = [w for w in words if w]
        if not words:
            return 0
        from .langfilter import blocked_word, _NEVER_WORD_FILTERED
        skip = np.asarray(sorted(int(k) for k in _NEVER_WORD_FILTERED), dtype=np.uint32)
        ids: list = []
        self._decode(self._content_candidates(lambda k: ~np.isin(k, skip)),
                     lambda s, ev, o: blocked_word(ev.get("content") or "", words) and ids.append(ev["id"]))
        return self._on_writer(self._delete_ids_sparing_anchors, ids)

    async def delete_by_words(self, words: list) -> int:
        return await self._s(self._delete_by_words_sync, list(words))

    def _delete_by_langs_sync(self, blocked) -> int:
        if self._writes_closed:
            return 0
        blocked = set(blocked)
        if not blocked:
            return 0
        from .langfilter import detect_languages
        ids: list = []
        self._decode(self._content_candidates(lambda k: k == 1),
                     lambda s, ev, o: (detect_languages(ev.get("content")) & blocked) and ids.append(ev["id"]))
        return self._on_writer(self._delete_ids_sparing_anchors, ids)

    async def delete_by_langs(self, blocked) -> int:
        return await self._s(self._delete_by_langs_sync, set(blocked))

    def _delete_hidden_payload_sync(self) -> int:
        if self._writes_closed:
            return 0
        from .langfilter import is_encoded_payload, is_hidden_payload
        ids: list = []

        def _check(s, ev, o):
            c = ev.get("content") or ""
            # the Postgres prefilter (length + two regexes), then the predicates themselves
            if len(c) >= 40 and (_HIDDEN_PRE.search(c) or _B64_PRE.search(c)) and \
                    (is_hidden_payload(c) or is_encoded_payload(c)):
                ids.append(ev["id"])
        self._decode(self._content_candidates(lambda k: k == 1), _check)
        return self._on_writer(self._delete_ids_sparing_anchors, ids)

    async def delete_hidden_payload(self) -> int:
        return await self._s(self._delete_hidden_payload_sync)

    @staticmethod
    def _tags_text(ev: dict) -> str:
        """The `tags` column text the Postgres store matched `LIKE '%"proxy"%'` against."""
        return json.dumps(ev.get("tags") or [], separators=(",", ":"))

    def _delete_by_proxy_sync(self) -> int:
        if self._writes_closed:
            return 0
        from .bridges import is_bridged_post
        ok = self._preserve_ok_pred()
        cand = self._select(lambda c, s: np.isin(c["kind"], [1, 6]) & ok(c) & (c["origin"] != _BRIDGE),
                            ("kind", "origin", "author"))
        ids: list = []

        def _check(s, ev, o):
            if '"proxy"' in self._tags_text(ev) and is_bridged_post({"kind": ev["kind"], "tags": ev["tags"]}):
                ids.append(ev["id"])
        self._decode(cand, _check)

        def _kill():
            if self._writes_closed:
                return 0
            self._kill_seqs([s for s in self._live_seqs_of(ids) if s is not None])
            self.store.sync()
            return len(ids)
        return self._on_writer(_kill)

    async def delete_by_proxy(self) -> int:
        return await self._s(self._delete_by_proxy_sync)

    # ------------------------------------------------------------ scans that only read
    def _bridged_pubkeys_sync(self, domains) -> set:
        domains = {d for d in domains if d}
        if not domains:
            return set()
        from .bridges import reveals_blocked_bridge
        out: set = set()

        def _check(s, ev, o):
            if ev["kind"] in (0, 3, 10002) or '"proxy"' in self._tags_text(ev):
                e = {"pubkey": ev["pubkey"], "kind": ev["kind"], "content": ev.get("content") or "",
                     "tags": ev.get("tags") or []}
                if ev["pubkey"] and reveals_blocked_bridge(e, domains):
                    out.add(ev["pubkey"])
        self._decode(self._select(lambda c, s: np.ones(len(s), dtype=bool), ()), _check)
        return out

    async def bridged_pubkeys(self, domains) -> set:
        return await self._s(self._bridged_pubkeys_sync, set(domains))

    def _kind0(self) -> np.ndarray:
        return self._select(lambda c, s: c["kind"] == 0, ("kind",))

    def _bridge_identity_pubkeys_sync(self, domains) -> set:
        domains = {d for d in domains if d}
        if not domains:
            return set()
        from .bridges import author_on_blocked_bridge
        out: set = set()

        def _check(s, ev, o):
            e = {"pubkey": ev["pubkey"], "kind": 0, "content": ev.get("content") or "", "tags": []}
            if ev["pubkey"] and author_on_blocked_bridge(e, domains):
                out.add(ev["pubkey"])
        self._decode(self._kind0(), _check)
        return out

    async def bridge_identity_pubkeys(self, domains) -> set:
        return await self._s(self._bridge_identity_pubkeys_sync, set(domains))

    def _social_mirror_pubkeys_sync(self) -> set:
        """Authors of stored fediverse/Bluesky MIRROR events, minus anyone who published one of our own puppet
        events (`fedibridge`) or anything stored as origin='bridge' (store.py, same rule)."""
        from .bridges import is_social_mirror
        out: set = set()
        ours: set = set()

        def _check(s, ev, o):
            raw = self._tags_text(ev)
            if '"proxy"' not in raw or not ev["pubkey"]:
                return
            if o == _BRIDGE or '"fedibridge"' in raw:
                ours.add(ev["pubkey"])
            elif is_social_mirror({"tags": ev.get("tags") or []}):
                out.add(ev["pubkey"])
        self._decode(self._select(lambda c, s: np.ones(len(s), dtype=bool), ()), _check)
        return out - ours

    async def social_mirror_pubkeys(self) -> set:
        return await self._s(self._social_mirror_pubkeys_sync)

    def _nip05_domains_sync(self) -> list:
        out: list = []

        def _check(s, ev, o):
            try:
                nip05 = (json.loads(ev.get("content") or "{}").get("nip05") or "").strip().lower()
            except Exception:      # noqa: BLE001
                return
            if "@" in nip05:
                out.append(nip05.rsplit("@", 1)[-1])
        self._decode(self._kind0(), _check)
        return out

    async def nip05_domains(self) -> list:
        return await self._s(self._nip05_domains_sync)

    # ------------------------------------------------------------ reads
    def _query_sync(self, filters: list, hard_cap: int) -> list:
        st = self.store
        if st.words_pending and any(isinstance(f, dict) and f.get("search") for f in filters or []):
            if st.words_pending > WORDS_INLINE:
                raise SearchNotReady("search is still indexing %d events replayed at startup — try again shortly"
                                     % st.words_pending)
        now = int(time.time())
        seen: dict = {}
        for flt in filters or []:
            for ev in st.query(dict(flt or {}), now=now):
                seen[ev["id"]] = ev
        out = sorted(seen.values(), key=lambda e: e.get("created_at", 0), reverse=True)
        return out[:hard_cap] if hard_cap else out

    @staticmethod
    def _selective(flt: dict) -> bool:
        if flt.get("ids") or flt.get("authors") or flt.get("search") or isinstance(flt.get("_cursor"), list):
            return True
        return any(isinstance(k, str) and k.startswith("#") and v for k, v in flt.items())

    def _match(self, flt: dict, now: int, exclude_kinds=()) -> np.ndarray:
        """Every live, unexpired seq one filter matches (no limit). A filter with nothing selective -- `kinds`,
        `since`/`until`, or nothing at all -- is a chunked column scan, never one long lock hold."""
        kinds = flt.get("kinds")
        karr = np.asarray([int(k) for k in kinds], dtype=np.uint32) if kinds else None
        since = int(flt["since"]) if flt.get("since") is not None else None
        until = int(flt["until"]) if flt.get("until") is not None else None
        excl = np.asarray(list(exclude_kinds), dtype=np.uint32)
        if self._selective(flt):
            st = self.store
            if flt.get("search") and st.words_pending > WORDS_INLINE:
                raise SearchNotReady("search is still indexing")
            seqs = st.match(dict(flt), now=now)
            if len(excl) and len(seqs):
                with st._lock:
                    k = np.frombuffer(st.kind, dtype=np.uint32)[seqs]
                seqs = seqs[~np.isin(k, excl)]
            return seqs

        def pred(c, s):
            m = (c["expires"] == 0) | (c["expires"] > now)
            if karr is not None:
                m &= np.isin(c["kind"], karr)
            if len(excl):
                m &= ~np.isin(c["kind"], excl)
            if since is not None:
                m &= c["created"] >= since
            if until is not None:
                m &= c["created"] <= until
            return m
        return self._select(pred, ("expires", "kind", "created"))

    def _neg_items_sync(self, filters: list, cap: int) -> list:
        now = int(time.time())
        st = self.store
        seen: dict = {}
        for flt in filters or []:
            seqs = self._match(dict(flt or {}), now)
            if not len(seqs):
                continue
            with st._lock:
                c = np.frombuffer(st.created, dtype=np.uint64)[seqs].astype(np.int64)
            if cap and len(seqs) > cap:                # ORDER BY created_at DESC LIMIT cap
                keep = np.argsort(-c, kind="stable")[:cap]
                seqs, c = seqs[keep], c[keep]
            for i in range(0, len(seqs), ID_BATCH * 16):
                with st._lock:
                    for s, ts in zip(seqs[i:i + ID_BATCH * 16], c[i:i + ID_BATCH * 16]):
                        raw = bytes(st.ids[int(s) * 32:int(s) * 32 + 32])
                        seen[raw] = (int(ts), raw)
        return sorted(seen.values(), key=lambda x: (x[0], x[1]))

    def _count_sync(self) -> int:
        total = 0
        for lo, b, c in self._chunks(("dead",)):
            total += int((c["dead"] == 0).sum())
        return total

    def _count_filtered_sync(self, filters: list, protect_nip78: bool = False) -> int:
        now = int(time.time())
        total = 0
        for flt in (filters or []):
            if not isinstance(flt, dict):
                continue
            kinds = flt.get("kinds")
            explicit_private = False
            if isinstance(kinds, list):
                for kind in kinds:
                    try:
                        explicit_private = explicit_private or int(kind) in (78, 30078)
                    except (TypeError, ValueError):
                        pass
            try:
                total += len(self._match(flt, now, (78, 30078) if protect_nip78 and not explicit_private else ()))
            except SearchNotReady:
                raise
            except Exception:      # noqa: BLE001 -- a malformed COUNT filter is skipped, as on Postgres
                continue
        return total

    # ------------------------------------------------------------ the sidecar tables
    def _kv_get_sync(self, key: str):
        with self.sidecar._lock:
            return self.sidecar.kv.get(key)

    def _kv_set_sync(self, key: str, value) -> None:
        sc = self.sidecar
        with sc._lock:
            if key in sc.kv and sc.kv[key] == value:
                return
            sc.kv[key] = value
            sc.save("kv")

    def _bridge_nip05_set_sync(self, name: str, pubkey: str) -> None:
        sc = self.sidecar
        with sc._lock:
            if sc.bridge_nip05.get(name) == pubkey:
                return
            sc.bridge_nip05[name] = pubkey
            sc.save("bridge_nip05")

    def _bridge_nip05_all_sync(self) -> dict:
        with self.sidecar._lock:
            return dict(self.sidecar.bridge_nip05)

    def _bridge_puppet_add_sync(self, pubkey: str) -> None:
        sc = self.sidecar
        with sc._lock:
            if pubkey in sc.bridge_puppet:
                return
            sc.bridge_puppet.add(pubkey)
            sc.save("bridge_puppet")

    def _bridge_puppets_all_sync(self) -> set:
        with self.sidecar._lock:
            return set(self.sidecar.bridge_puppet)

    def _wot_replace_sync(self, members: list, extra: list | None = None) -> int:
        """Replace the depth-1 set; operators at depth 0. One atomic file swap: no instant with an empty set."""
        now = int(time.time())
        new = {pk: [1, now] for pk in members}
        new.update({pk: [0, now] for pk in (extra or [])})
        sc = self.sidecar
        with sc._lock:
            sc.wot = new
            sc.save("wot")
            return len(sc.wot)

    def _wot_add_sync(self, pubkeys: list) -> int:
        now = int(time.time())
        sc = self.sidecar
        with sc._lock:
            fresh = [p for p in pubkeys if p and p not in sc.wot]
            for p in fresh:
                sc.wot[p] = [1, now]
            if fresh:
                sc.save("wot")
        return len([p for p in pubkeys if p])

    def _wot_members_sync(self) -> set:
        with self.sidecar._lock:
            return set(self.sidecar.wot)

    def _wot_missing_metadata_sync(self) -> list:
        """store.py _wot_missing_metadata_sync -- the same four groups in the same order -- from a chunked column
        scan instead of three DISTINCT queries."""
        k0, k10002, vis = set(), set(), set()
        for lo, b, c in self._chunks(("kind", "author", "dead")):
            live = c["dead"] == 0
            k, a = c["kind"][live], c["author"][live]
            k0.update(np.unique(a[k == 0]).tolist())
            k10002.update(np.unique(a[k == 10002]).tolist())
            vis.update(np.unique(a[np.isin(k, [1, 6, 7])]).tolist())
        with self.store._lock:
            names = self.store._authors
            have_k0 = {names[i] for i in k0}
            have_relay = {names[i] for i in k10002}
            visible = {names[i] for i in vis}
        wot = self._wot_members_sync()
        prio = [pk for pk in (wot - have_k0) if pk in visible]
        ghost = [pk for pk in visible if pk not in have_k0 and pk not in wot]
        rest = [pk for pk in (wot - have_k0) if pk not in visible]
        relay_only = [pk for pk in (wot & have_k0) if pk not in have_relay]
        return prio + ghost + rest + relay_only

    # ------------------------------------------------------------ the prune (store.py _prune_sync, over columns)
    def _exempt_subscribers(self) -> set:
        """store.py _subscriber_exempt as a SET (same rule: only with the feature on; the fresh set when the
        ledger read, else the last good one)."""
        if not self.paid_tier_enabled:
            return set()
        subs = self.subscriber_pubkeys if self.tiered_ok else self._last_good_subscribers
        return {p for p in subs if pg._is_hex64(p)}

    def _tiered_specs(self, now: int) -> list:
        """The pay-to-stay rules as data -- the masks below are built FROM these, and `_tiered_rules` describes
        them, so the description a test reads is the rule that runs."""
        if not self.free_retention_days or not self.tiered_ok:
            return []
        base = {"origin": "direct", "kinds": tuple(pg._PRUNABLE_KINDS), "git_comment_exempt": True,
                "not_preserved": True}
        out = [("aged_free", dict(base, subscribers="out", cutoff=now - self.free_retention_days * 86400))]
        if self.paid_retention_days:
            out.append(("aged_paid", dict(base, subscribers="in", cutoff=now - self.paid_retention_days * 86400)))
        return out

    @staticmethod
    def _describe(spec: dict) -> str:
        parts = ["created_at < ?"]
        if spec.get("origin"):
            parts.append("origin = '%s'" % spec["origin"])
        parts.append("kind IN (%s)" % ",".join(str(k) for k in spec["kinds"]))
        if spec.get("git_comment_exempt"):
            parts.append("NOT (kind = 1111 AND K IN (%s))" % ",".join(pg._GIT_COMMENT_ROOT_KINDS))
        if spec.get("not_preserved"):
            parts.append("pubkey NOT IN (preserved)")
        parts.append("pubkey %s (subscribers)" % ("IN" if spec["subscribers"] == "in" else "NOT IN"))
        return " AND ".join(parts)

    def _tiered_rules(self, now: int) -> list:
        return [(label, self._describe(spec), (spec["cutoff"],)) for label, spec in self._tiered_specs(now)]

    def _ctx(self, now: int) -> dict:
        """What every rule of one pass reads, fixed once: the preserve / subscriber / exempt author ids and the
        git-rooted comment seqs."""
        return {"now": now, "pres": self._author_ids(self.preserve_pubkeys),
                "subs": self._author_ids(self.subscriber_pubkeys),
                "exempt": self._author_ids(self._exempt_subscribers()),
                "git": self._git_comment_seqs()}

    @staticmethod
    def _prunable(c, s, ctx):
        k = c["kind"]
        return np.isin(k, _PRUNABLE) & ~((k == 1111) & np.isin(s, ctx["git"]))

    def _rule_seqs(self, name: str, ctx: dict, spec: dict | None = None) -> np.ndarray:
        now = ctx["now"]
        cols = ("kind", "created", "expires", "origin", "author")
        if name == "expired":
            pred = lambda c, s: (c["expires"] > 0) & (c["expires"] <= now) & ~np.isin(c["kind"], _NEVER_EXPIRE)  # noqa: E731
        elif name == "retired":
            pred = lambda c, s: np.isin(c["kind"], _RETIRED)  # noqa: E731
        elif name == "aged":
            cutoff = now - self.retention_days * 86400
            pred = lambda c, s: ((c["created"].astype(np.int64) < cutoff) & self._prunable(c, s, ctx)  # noqa: E731
                                 & (c["origin"] != _DIRECT) & ~np.isin(c["author"], ctx["pres"])
                                 & ~np.isin(c["author"], ctx["exempt"]))
        elif name == "bridge_dm":
            dmcut = now - pg._BRIDGE_DM_TTL_DAYS * 86400
            pred = lambda c, s: ((c["origin"] == _BRIDGE) & np.isin(c["kind"], [13, 1059])  # noqa: E731
                                 & (c["created"].astype(np.int64) < dmcut))
        elif name == "tiered":
            def pred(c, s):
                m = (c["created"].astype(np.int64) < spec["cutoff"]) & np.isin(
                    c["kind"], np.asarray(spec["kinds"], dtype=np.uint32))
                if spec.get("git_comment_exempt"):
                    m &= ~((c["kind"] == 1111) & np.isin(s, ctx["git"]))
                if spec.get("origin"):
                    m &= c["origin"] == ORIGINS[spec["origin"]]
                if spec.get("not_preserved"):
                    m &= ~np.isin(c["author"], ctx["pres"])
                insubs = np.isin(c["author"], ctx["subs"])
                return m & (insubs if spec["subscribers"] == "in" else ~insubs)
        else:
            raise ValueError(name)
        return self._select(pred, cols)

    def _orphan_zap_seqs(self, ctx: dict) -> np.ndarray:
        """store.py _ORPHAN_ZAP_SQL: a 9735 (not direct, older than retention) naming a post (`e`) of which none
        is stored any more."""
        cutoff = ctx["now"] - self.retention_days * 86400
        cand = self._select(lambda c, s: (c["kind"] == 9735) & (c["origin"] != _DIRECT)
                            & (c["created"].astype(np.int64) < cutoff), ("kind", "origin", "created"))
        targets: dict = {}
        self._decode(cand, lambda s, ev, o: targets.__setitem__(
            s, [str(t[1]) for t in ev.get("tags") or [] if len(t) >= 2 and t[0] == "e"]))
        out = []
        for s, es in targets.items():
            if es and all(x is None for x in self._live_seqs_of(es)):
                out.append(s)
        return np.asarray(sorted(out), dtype=np.uint32)

    def _cap_seqs(self, ctx: dict, max_events: int) -> np.ndarray:
        """The count cap: prunable, preserve-ok, non-exempt events beyond the newest `max_events` of ALL rows."""
        seqs, created, ok = [], [], []
        for lo, b, c in self._chunks(("dead", "kind", "created", "origin", "author")):
            s = np.arange(lo, b, dtype=np.uint32)
            live = c["dead"] == 0
            seqs.append(s[live])
            created.append(c["created"][live])
            m = (self._prunable(c, s, ctx) & (c["origin"] != _DIRECT) & ~np.isin(c["author"], ctx["pres"])
                 & ~np.isin(c["author"], ctx["exempt"]))
            ok.append(m[live])
        if not seqs:
            return np.zeros(0, dtype=np.uint32)
        seqs, created, ok = np.concatenate(seqs), np.concatenate(created), np.concatenate(ok)
        if len(seqs) <= max_events:
            return np.zeros(0, dtype=np.uint32)
        # newest first. Postgres leaves equal timestamps unordered; like maintenance._cap_mask (which the
        # differential test holds to Postgres) the EARLIER arrival is kept among ties.
        order = np.argsort(-created.astype(np.int64), kind="stable")
        beyond = order[max_events:]
        return np.sort(seqs[beyond][ok[beyond]])

    def _prune_preview_sync(self) -> dict:
        now = int(time.time())
        ctx = self._ctx(now)
        expired = len(self._rule_seqs("expired", ctx))
        retired_seqs = self._rule_seqs("retired", ctx)
        retired_by_kind: dict = {}
        if len(retired_seqs):
            with self.store._lock:
                ks = np.frombuffer(self.store.kind, dtype=np.uint32)[retired_seqs].copy()
            for k, c in zip(*np.unique(ks, return_counts=True)):
                retired_by_kind[int(k)] = int(c)
        retired = sum(retired_by_kind.values())
        aged = len(self._rule_seqs("aged", ctx)) if self.retention_days else 0
        orphan_zaps = len(self._orphan_zap_seqs(ctx)) if self.retention_days else 0
        bridge_dm = len(self._rule_seqs("bridge_dm", ctx))
        capped = len(self._cap_seqs(ctx, self.max_events)) if self.max_events else 0
        tiered = {label: len(self._rule_seqs("tiered", ctx, spec)) for label, spec in self._tiered_specs(now)}
        return {"expired": expired, "aged": aged, "orphan_zaps": orphan_zaps, "bridge_dm": bridge_dm,
                "capped": capped, "retired": retired, "retired_by_kind": retired_by_kind,
                **tiered,
                "total": expired + aged + orphan_zaps + bridge_dm + capped + retired + sum(tiered.values()),
                "retention_days": self.retention_days, "max_events": self.max_events,
                "free_retention_days": self.free_retention_days,
                "paid_retention_days": self.paid_retention_days,
                "subscribers": len(self.subscriber_pubkeys), "tiered_ok": self.tiered_ok}

    def _prune_sync(self, limit: int = 0) -> tuple:
        """One prune PASS -- store.py _prune_sync's rules, in its order, with its budget semantics: `limit` caps
        the deletions of the whole pass, and `more` says a rule hit the cap. Each rule sees the previous rules'
        deletions (they are applied before the next rule is scanned), as in the one Postgres transaction."""
        if self._writes_closed:
            return (0, False)
        now = int(time.time())
        ctx = self._ctx(now)
        state = {"budget": int(limit or 0), "capped": False, "removed": 0}

        def _apply(seqs) -> None:
            if limit:
                if state["budget"] <= 0:
                    state["capped"] = True
                    return
                seqs = seqs[:state["budget"]]
                if len(seqs) >= state["budget"]:
                    state["capped"] = True
                state["budget"] -= len(seqs)
            state["removed"] += self._kill_seqs(seqs)

        _apply(self._rule_seqs("expired", ctx))
        _apply(self._rule_seqs("retired", ctx))
        if self.retention_days:
            _apply(self._rule_seqs("aged", ctx))
            _apply(self._orphan_zap_seqs(ctx))
        _apply(self._rule_seqs("bridge_dm", ctx))
        for _label, spec in self._tiered_specs(now):
            _apply(self._rule_seqs("tiered", ctx, spec))
        if self.max_events:
            _apply(self._cap_seqs(ctx, self.max_events))
        if state["removed"]:
            self.store.sync()
        return state["removed"], state["capped"]


# ------------------------------------------------------------------------------------------------ the directory
def claim_directory(path: str) -> None:
    """Refuse a directory that is not a promoted primary store, else make it ours: the marker says `primary` and
    the mirror's CLEAN marker is consumed (see the module docstring)."""
    m = read_marker(path)
    if m is None:
        raise PrimaryNotReady("%s has no %s marker — stop the relay and run scripts/posterchandb_promote.py "
                              "(or `--new` on a node that never had Postgres)" % (path, MARKER))
    clean = read_clean_token(path)
    state = m.get("state")
    if clean is not None:
        if state != "promoted" or clean != m.get("clean_token"):
            raise PrimaryNotReady("%s was written as a Postgres mirror after it was promoted (its CLEAN token is "
                                  "not the one promoted) — promote it again" % path)
    elif state != "primary":
        raise PrimaryNotReady("%s was promoted but its mirror CLEAN marker is gone (a mirror ran and did not stop "
                              "cleanly) — promote it again" % path)
    if state != "primary":
        write_marker(path, dict(m, state="primary", primary_since=int(time.time())))
    if clean is not None:
        os.remove(os.path.join(path, CLEAN))
        _fsync_dir(path)


def init_new(path: str) -> None:
    """A primary store for a node with NO Postgres (scripts/posterchandb_promote.py --new). Refuses a directory
    that already holds events: those are a mirror's, and only a promotion may adopt them."""
    os.makedirs(path, exist_ok=True)
    if read_marker(path) is not None:
        raise PrimaryNotReady("%s is already a primary store" % path)
    st = Store(path, flush_interval=3600, snapshots=True)
    try:
        held = len(st.off)
    finally:
        st.close(take_snapshot=False)
    if held:
        raise PrimaryNotReady("%s holds %d events (a mirror's) — promote it from Postgres instead" % (path, held))
    try:
        os.remove(os.path.join(path, CLEAN))
    except OSError:
        pass
    Sidecar(path).replace_all({}, {}, {}, ())
    write_marker(path, {"state": "primary", "created_new": int(time.time())})
