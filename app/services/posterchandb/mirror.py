"""PosterChanDB beside the relay's Postgres: every write mirrored, reads served from RAM once it has proven itself.

The switch from Postgres is made in two steps so that no step can lose an event:

  * `shadow` -- every event the relay stores, and every event it deletes, is applied here too, in order, by
    the same rules (store.put follows _insert_one rule for rule; tests/test_posterchandb_vs_relay.py). Postgres
    answers every query; a sample of queries is also answered here and the two compared (`stats()`).
  * `serve`  -- the same, and the relay's NIP-01 queries are answered from here. Postgres is still written
    first and stays the source of truth: a query is answered from RAM only while the mirror is READY (copied,
    and its count matched Postgres EXACTLY), nothing it could depend on is still queued, and it does not throw
    -- otherwise that one query goes to Postgres. `off` is one setting away.

THE CUT-OVER IS EXACT because every write to the relay's Postgres goes through ONE writer thread
(RelayStore._w). `RelayStore.attach_mirror` runs on that thread: it opens a repeatable-read transaction and
fixes its snapshot, and in the same step starts handing writes to the mirror. So every write is either in the
snapshot or in the mirror's queue -- never both, never neither -- with no clock and no tolerance involved.

Copying: an empty or not-cleanly-closed directory is filled from that snapshot with `copy_put` -- Postgres's
rows exactly as they are, no rule re-decided (the rules depend on ARRIVAL order, which a copy cannot replay).
Only then does the queue start applying, with the rules, on top of an identical state. The same writer thread
answers the count check (`sync_point`: Postgres's count and the queue position at one instant), so "the mirror
holds what Postgres holds" is checked exactly.

A clean close leaves a `CLEAN` marker; without it (a crash, a run with the mirror off) the next start copies
afresh, because nothing else can prove the directory still matches Postgres.

Writes are applied by ONE thread, in the relay's order, so the relay never waits on this store. A query waits
for the writes enqueued before it (a client that publishes and then asks must see its event) for at most
`wait_s`, else it goes to Postgres.
"""
from __future__ import annotations

import os
import queue
import random
import threading
import time

from . import cache as cache_mod
from . import maintenance as maint_mod
from .store import DROPPED, Store

CLEAN = "CLEAN"
MODES = ("off", "shadow", "serve")
QUEUE_MAX = 2_000_000


def clear_clean_marker(path: str) -> None:
    """The relay runs WITHOUT the mirror: whatever it writes now is missing here, so the next start must copy."""
    try:
        os.remove(os.path.join(path, CLEAN))
    except OSError:
        pass


class Mirror:
    def __init__(self, path: str, mode: str, pg_connect, *, flush_interval: float = 300.0, cache_mb: float = 0,
                 sample: float = 0.05, wait_s: float = 0.05, log=None, maintenance: bool = True):
        if mode not in ("shadow", "serve"):
            raise ValueError("mirror mode must be shadow or serve")
        self.path = path
        self.mode = mode
        self.pg_connect = pg_connect                # () -> a NEW psycopg2 connection to the relay's database
        self.flush_interval = float(flush_interval)
        self.cache_mb = float(cache_mb or 0)
        self.sample = float(sample)
        self.wait_s = float(wait_s)
        self.id_listing = None              # RelayStore.attach_mirror: (now) -> ({id: (kind, origin, created)}, queue pos)
        self.log = log or (lambda *a: None)
        self.maintenance = maintenance
        self.state = "opening"                      # opening | copying | catching-up | ready | stale | failed | closed
        self.why = ""
        self.store: Store | None = None
        self.sync_point = None                      # set by RelayStore.attach_mirror: () -> (pg_count, enqueued)
        os.makedirs(path, exist_ok=True)
        self.clean_token = None
        try:
            with open(os.path.join(path, CLEAN)) as f:
                self.clean_token = f.read().strip() or None
        except OSError:
            pass
        self.clean = self.clean_token is not None
        self._q: queue.Queue = queue.Queue(maxsize=QUEUE_MAX)
        self._enq = 0
        self._applied = 0
        self._cv = threading.Condition()
        self._stop = threading.Event()
        self._thread = None
        self._maint = None
        self._gov = None
        self.counters = {"put": 0, "gone": 0, "served": 0, "fallback": 0, "shadow_ok": 0, "shadow_diff": 0,
                         "apply_errors": 0, "copied": 0, "search_to_postgres": 0}
        self._words_thread = None
        self.last_diff = None
        self.pg_count = self.my_count = None

    def needs_copy(self) -> bool:
        return not self.clean

    def distrust(self, why: str) -> None:
        """Copy afresh instead of reopening (RelayStore.attach_mirror decides, on the writer thread)."""
        self.clean = False
        self.log("[posterchandb] mirror will copy Postgres again: %s" % why)

    # ---------------------------------------------------------------- lifecycle
    def start(self, snapshot_conn=None) -> None:
        """`snapshot_conn`: the repeatable-read connection attach_mirror opened on the writer thread (a copy is
        needed), else None (a clean reopen)."""
        self._thread = threading.Thread(target=self._run, args=(snapshot_conn,), name="posterchandb-mirror",
                                        daemon=True)
        self._thread.start()

    def _run(self, conn) -> None:
        try:
            clear_clean_marker(self.path)               # from here on a crash means "copy again next time"
            if conn is not None:
                for name in os.listdir(self.path):
                    if name.startswith("seg-") or name.startswith("index.snap"):
                        os.remove(os.path.join(self.path, name))
            self._gov = cache_mod.CacheGovernor(budget_mb=self.cache_mb, log=self.log)
            self.store = Store(self.path, flush_interval=self.flush_interval, admit=self._gov.admit, log=self.log)
            self._gov.store = self.store
            self._gov.start()
            if conn is not None:
                self._set("copying", "copying Postgres")
                self._copy(conn)
            self._set("catching-up", "applying writes made since")
            if self._verify():
                self._set("ready", "%d events, the same as Postgres" % self.my_count)
            else:
                self._set("stale", "count differs from Postgres (postgres %s, mirror %s)" % (self.pg_count,
                                                                                         self.my_count))
        except Exception as e:      # noqa: BLE001 -- the mirror failing must never take the relay down
            self._set("failed", "%r" % (e,))
            return
        self._apply_loop()

    def _set(self, state: str, why: str) -> None:
        self.state, self.why = state, why
        self.log("[posterchandb] mirror %s: %s" % (state, why))
        if state == "ready" and self._words_thread is None and self.store is not None and self.store.words_pending:
            self._words_thread = threading.Thread(target=self._index_words, name="posterchandb-words", daemon=True)
            self._words_thread.start()
        if state == "ready" and self.maintenance and self._maint is None:
            pol = lambda: maint_mod.Policy(min_free_pct=0, mirror_of_postgres=True)     # noqa: E731 -- Postgres decides what is deleted
            # Snapshots HOURLY, never at shutdown: the stop must fit systemd's 10 s, and with a snapshot at most an
            # hour old the next start replays seconds of log instead of re-indexing everything.
            self._maint = maint_mod.Maintainer(self.store, pol, log=self.log, snapshot_hours=1.0)
            self._maint.start()

    def _index_words(self) -> None:
        """Search words deferred by the copy (or a replay), in small batches at idle priority with a pause between
        them: the tokenizer is pure Python, so a long run would hold the GIL against the relay's own event loop.
        Searches go to Postgres until this finishes; then a snapshot makes the next start complete at once."""
        maint_mod.lower_priority()
        st, t0, total = self.store, time.monotonic(), self.store.words_pending
        while not self._stop.is_set() and st.words_pending:
            # Small batches: each holds the store lock, and a query waits behind it. 300 at a time held it ~90 ms
            # (the Postgres-exact tokenizer is ~300 us an event); 25 is ~8 ms.
            st.index_pending_words(25)
            self._stop.wait(0.01)
        if self._stop.is_set():
            return
        self.log("[posterchandb] search words indexed for %d events in %.0fs" % (total, time.monotonic() - t0))
        try:
            st.snapshot()
        except OSError as e:
            self.log("[posterchandb] snapshot after word indexing failed: %r" % (e,))

    def close(self, clean_token: str | None = None, timeout: float = 5.0) -> None:
        """Drain what the relay already wrote, flush + fsync, and mark CLEAN only if nothing was lost. NO snapshot
        here: the relay's unit stops within 10 s (TimeoutStopSec) and a snapshot of millions of events is the
        slow part -- a SIGKILL'd stop would leave no CLEAN and cost a full copy from Postgres at the next start.
        tests/test_posterchandb_durability.py holds the whole stop to a budget."""
        self._stop.set()
        if self._thread:
            self._thread.join(timeout)
        if self._words_thread:
            self._words_thread.join(2.0)        # one batch at most; the rest resumes from the log next start
        if self._maint:
            self._maint.stop(timeout=2.0)       # a compaction cut short is crash-safe (it is replayed or redone)
        if self._gov:
            self._gov.stop(timeout=1.0)
        st = self.store
        if st is None:
            return
        try:
            alive = bool(self._thread and self._thread.is_alive())
            # CLEAN needs the token RelayStore.detach_mirror stored in Postgres -- without it nothing proves which
            # Postgres state this directory matches, so the next start copies (the safe answer).
            ok = self.state == "ready" and not alive and self._q.empty() and bool(clean_token)
            st.close(take_snapshot=False)
            if ok:
                with open(os.path.join(self.path, CLEAN), "w") as f:
                    f.write(clean_token + "\n")
                    f.flush()
                    os.fsync(f.fileno())
                st._fsync_dir()
        except Exception as e:      # noqa: BLE001
            self.log("[posterchandb] mirror close failed: %r" % (e,))
        self.state = "closed"

    # ---------------------------------------------------------------- writes (the relay's writer thread)
    def _enqueue(self, item) -> None:
        if self.state in ("failed", "stale", "closed"):
            return
        try:
            self._q.put_nowait(item)
        except queue.Full:
            self._set("stale", "write queue overflowed -- Postgres serves until a restart copies again")
            return
        with self._cv:
            self._enq += 1

    def put(self, ev: dict, origin: str) -> None:
        self._enqueue(("put", ev, origin))

    def gone(self, ids) -> None:
        ids = [i for i in (ids or []) if isinstance(i, str) and len(i) == 64]
        if ids:
            self._enqueue(("gone", ids, None))

    # ---------------------------------------------------------------- the one applier
    def _apply_one(self, item) -> None:
        op, a, b = item
        try:
            if op == "put":
                self.store.put(a, origin=b)
                self.counters["put"] += 1
            else:
                self._kill_ids(a)
                self.counters["gone"] += 1
        except Exception as e:      # noqa: BLE001 -- one bad write costs the mirror's trust, never the relay
            self.counters["apply_errors"] += 1
            self._set("stale", "a write could not be applied: %r" % (e,))
        with self._cv:
            self._applied += 1
            self._cv.notify_all()

    def _apply_until(self, target: int) -> None:
        while self._applied < target:
            self._apply_one(self._q.get())

    def _apply_loop(self) -> None:
        st = self.store
        while True:
            try:
                item = self._q.get(timeout=1.0)
            except queue.Empty:
                if self._stop.is_set():
                    return
                try:
                    st.maybe_flush()
                except Exception as e:      # noqa: BLE001
                    self.log("[posterchandb] flush failed: %r" % (e,))
                continue
            self._apply_one(item)
            if self._stop.is_set() and self._q.empty():
                return

    def _kill_ids(self, ids) -> None:
        st = self.store
        with st._lock:
            for eid in ids:
                s = st._seq_of(eid)
                if s is not None and not st.dead[s] and st.seg[s] != DROPPED:
                    st._kill(s, persist=True)

    # ---------------------------------------------------------------- the copy
    def _copy(self, raw) -> None:
        import psycopg2.extras
        from app.services.nostr_relay import store as relay_store
        st = self.store
        try:
            n = 0
            with raw.cursor(name="pcdb_mirror_copy", cursor_factory=psycopg2.extras.DictCursor) as cur:
                cur.itersize = 20000
                cur.execute("SELECT " + relay_store.EVENT_COLUMNS + ", e.origin FROM events e "
                            "ORDER BY e.created_at, e.id")
                for row in cur:
                    if self._stop.is_set():
                        raise RuntimeError("stopped during the copy")
                    try:
                        ev = relay_store.event_from_row(row)
                    except Exception:      # noqa: BLE001 -- unreadable on Postgres = unservable there too
                        continue
                    st.copy_put(ev, origin=row["origin"] or "wot")
                    n += 1
                    if n % 200000 == 0:
                        st.flush()
                        self.log("[posterchandb] mirror copied %d events" % n)
            with raw.cursor() as cur:
                cur.execute("SELECT event_id, value FROM event_tags WHERE tag='_quote_author'")
                for eid, val in cur:
                    st.add_derived_tag(eid, "_quote_author", val)
            self.counters["copied"] = n
            st.flush()
        finally:
            try:
                raw.rollback()
                raw.close()
            except Exception:      # noqa: BLE001
                pass

    # ---------------------------------------------------------------- the proof
    def _live_count(self, now: int) -> int:
        """Events a query could return: not dead, not expired at `now` (the relay's `expiration > now`)."""
        import numpy as np
        st = self.store
        with st._lock:
            n = len(st.off)
            dead = np.frombuffer(st.dead, dtype=np.uint8)[:n]
            exp = np.frombuffer(st.expires, dtype=np.uint64)[:n]
            live = int(((dead == 0) & ((exp == 0) | (exp > now))).sum())
            del dead, exp                     # views must die under the lock (store._grow)
        return live

    def _verify(self) -> bool:
        """Postgres's queryable count and the queue position, read at ONE instant on the relay's writer thread;
        apply the queue up to that position; then the two counts must be EQUAL."""
        if self.sync_point is None:
            return False
        now = int(time.time())
        pg, target = self.sync_point(now)
        self._apply_until(target)
        self.pg_count, self.my_count = pg, self._live_count(now)
        self.log("[posterchandb] count check: postgres %d, mirror %d" % (pg, self.my_count))
        if pg != self.my_count:
            self._explain_mismatch(now)
        return pg == self.my_count

    def _live_ids(self, now: int) -> set:
        st = self.store
        with st._lock:
            n = len(st.off)
            return {bytes(st.ids[i * 32:i * 32 + 32]).hex() for i in range(n)
                    if not st.dead[i] and (not st.expires[i] or st.expires[i] > now)}

    def _explain_mismatch(self, now: int) -> None:
        """WHICH events differ, not just how many -- the one-event gap of 2026-10-10 could not be traced after the
        fact. Read at one instant on the writer thread (like the count), so only a real difference shows. Logs ids,
        kinds, origins and times, never content. A failure here changes nothing: the mirror is already stale."""
        if self.id_listing is None:
            return
        try:
            pg, target = self.id_listing(now)
            self._apply_until(target)
            mine = self._live_ids(now)
            only_pg = [i for i in pg if i not in mine]
            only_me = [i for i in mine if i not in pg]
            self.log("[posterchandb] mismatch: %d only in Postgres, %d only in the mirror" % (len(only_pg), len(only_me)))
            for i in only_pg[:10]:
                kind, origin, created = pg[i]
                self.log("[posterchandb]   only in Postgres: %s kind=%s origin=%s created=%s" % (i[:16], kind, origin, created))
            for i in only_me[:10]:
                self.log("[posterchandb]   only in the mirror: %s" % i[:16])
        except Exception as e:      # noqa: BLE001
            self.log("[posterchandb] could not list the difference: %r" % (e,))

    # ---------------------------------------------------------------- reads (relay threads)
    def _wait_applied(self, target: int, timeout: float) -> bool:
        end = time.monotonic() + timeout
        with self._cv:
            while self._applied < target:
                left = end - time.monotonic()
                if left <= 0:
                    return False
                self._cv.wait(left)
        return True

    def serving(self) -> bool:
        return self.mode == "serve" and self.state == "ready"

    def query(self, filters: list, hard_cap: int):
        """The relay's _query_sync, answered from RAM -- or None (the caller asks Postgres)."""
        if not self.serving():
            return None
        if self.store.words_pending and any((f or {}).get("search") for f in filters or []):
            self.counters["search_to_postgres"] += 1    # its words are still being indexed
            return None
        if not self._wait_applied(self._enq, self.wait_s):
            self.counters["fallback"] += 1
            return None
        try:
            out = self._answer(filters, hard_cap)
        except Exception as e:      # noqa: BLE001
            self.counters["fallback"] += 1
            self.log("[posterchandb] query fell back to Postgres: %r" % (e,))
            return None
        self.counters["served"] += 1
        return out

    def _answer(self, filters: list, hard_cap: int) -> list:
        now = int(time.time())
        seen: dict = {}
        for flt in filters or []:
            for ev in self.store.query(dict(flt or {}), now=now):
                seen[ev["id"]] = ev
        out = sorted(seen.values(), key=lambda e: e.get("created_at", 0), reverse=True)
        return out[:hard_cap] if hard_cap else out

    def shadow(self, filters: list, hard_cap: int, pg_answer: list) -> None:
        """Compare a SAMPLE of Postgres answers with this store's. Only the filter's KEYS are ever recorded --
        never values (search text, authors): feedback_no_prompt_logging."""
        if self.state != "ready" or random.random() >= self.sample:
            return
        if self.store.words_pending and any((f or {}).get("search") for f in filters or []):
            return                                       # answering it here would index everything inline
        if not self._wait_applied(self._enq, 0.05):
            return
        try:
            mine = [e["id"] for e in self._answer(filters, hard_cap)]
        except Exception as e:      # noqa: BLE001
            self.counters["shadow_diff"] += 1
            self.last_diff = {"error": repr(e)[:200]}
            return
        theirs = [e["id"] for e in pg_answer]
        if mine == theirs:
            self.counters["shadow_ok"] += 1
        else:
            self.counters["shadow_diff"] += 1
            self.last_diff = {"keys": sorted({k for f in (filters or []) for k in (f or {})}),
                              "postgres": len(theirs), "mirror": len(mine),
                              "only_postgres": len(set(theirs) - set(mine)), "only_mirror": len(set(mine) - set(theirs)),
                              "same_set": set(mine) == set(theirs), "at": int(time.time())}

    def stats(self) -> dict:
        out = {"mode": self.mode, "state": self.state, "why": self.why, "queued": self._enq - self._applied,
               "search_words_pending": self.store.words_pending if self.store is not None else None,
               "pg_count": self.pg_count, "mirror_count": self.my_count}
        out.update(self.counters)
        if self.last_diff:
            out["last_diff"] = self.last_diff
        if self.store is not None:
            try:
                out.update({k: v for k, v in self.store.stats().items() if isinstance(v, (int, float))})
            except Exception:      # noqa: BLE001
                pass
        return out
