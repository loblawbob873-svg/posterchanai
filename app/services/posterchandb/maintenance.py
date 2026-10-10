"""PosterChanDB built-in maintenance: auto-clean, disk reclamation and a full-disk guard, with nothing
for an operator to run — and with a hard ceiling on what it may cost the machine.

WHAT IT DOES (one pass every `interval` seconds, in this order):
  1. the relay's prune rules, the SAME rules `nostr_relay/store.py:_prune_sync` runs on Postgres, read
     from its constants so the two cannot drift: NIP-40 expiry (never _NEVER_EXPIRE_KINDS), retired
     kinds, the age rule (prunable kinds, never a direct write or a preserved author, subscribers
     exempt), orphaned zap receipts, the 4-day bridge DM TTL, the pay-to-stay tiers, the count cap;
  2. compaction of at most ONE closed segment that is mostly dead (frees its file and its RAM);
  3. the disk guard: below `min_free_pct` free on the filesystem, the OLDEST re-fetchable copies go
     first (synced prunable feed, never a direct write, never a preserved author) until it recovers.

WHAT IT MAY COST — the machine this runs on is a RAID5 behind an LVM cache, which is where an
unthrottled cleaner does the most damage (a partial-stripe write is a read-modify-write of the whole
stripe, and a flood of writes evicts the cache's hot set):
  * compaction reads NOTHING from disk — records and markers are in RAM — and writes only surviving
    bytes, sequentially, in 4 MB pieces with one fsync per segment;
  * `io_mb_s` caps maintenance writes (default 8 MB/s); `cpu_pct` caps its CPU share (default 10%) —
    after every slice of work it sleeps long enough to stay under both;
  * it backs off while the machine is busy (1-minute load average per CPU above `busy_load`);
  * the thread asks for idle CPU priority (nice 19) and the idle IO class (honoured by bfq; on
    mq-deadline/none the byte cap above is what holds);
  * prune passes walk the columns in slices and write their deletion markers through the ordinary
    delayed flush, so a prune of a million events is a few MB of appends, not a million writes.
"""
from __future__ import annotations

import ctypes
import os
import platform
import shutil
import threading
import time

import numpy as np

from app.services.nostr_relay import store as relay
from .store import DROPPED, ORIGINS, _h

DAY = 86400
SLICE = 262144          # rows per prune slice (one numpy pass, then pace)
KILL_BATCH = 20000      # markers per kill() call


class Throttle:
    """Sleep after each slice of work so maintenance stays under `io_mb_s` and `cpu_pct`, and wait
    while the machine is busy. `sleep` is injectable for tests."""

    def __init__(self, io_mb_s: float = 8.0, cpu_pct: float = 10.0, busy_load: float = 0.75,
                 busy_wait: float = 5.0, max_busy_wait: float = 120.0, sleep=time.sleep, loadavg=None,
                 stop: threading.Event | None = None):
        self.io_bps = max(0.1, float(io_mb_s)) * 1024 * 1024
        self.cpu_pct = min(100.0, max(1.0, float(cpu_pct)))
        self.busy_load = float(busy_load)
        self.busy_wait = float(busy_wait)
        self.max_busy_wait = float(max_busy_wait)
        self.sleep = sleep
        self.loadavg = loadavg or (lambda: os.getloadavg()[0] / (os.cpu_count() or 1))
        self.stop = stop or threading.Event()
        self.slept = 0.0

    def _nap(self, s: float) -> None:
        if s > 0:
            self.slept += s
            if self.sleep is time.sleep:
                self.stop.wait(s)
            else:
                self.sleep(s)

    def __call__(self, nbytes: int = 0, cpu_s: float = 0.0) -> None:
        wait = max(nbytes / self.io_bps, cpu_s * (100.0 / self.cpu_pct - 1.0))
        self._nap(wait)
        waited = 0.0
        while self.busy_load > 0 and not self.stop.is_set() and waited < self.max_busy_wait:
            try:
                if self.loadavg() <= self.busy_load:
                    break
            except OSError:
                break
            self._nap(self.busy_wait)
            waited += self.busy_wait


def lower_priority() -> None:
    """Idle CPU + idle IO for the CALLING thread only (Linux applies both per thread)."""
    try:
        tid = threading.get_native_id()
        os.setpriority(os.PRIO_PROCESS, tid, 19)
    except (AttributeError, OSError):
        pass
    try:
        nr = {"x86_64": 251, "aarch64": 30, "armv7l": 314, "riscv64": 30}.get(platform.machine())
        if nr:
            libc = ctypes.CDLL(None, use_errno=True)
            libc.syscall(nr, 1, threading.get_native_id(), 3 << 13)   # IOPRIO_WHO_PROCESS, CLASS_IDLE
    except Exception:
        pass


class Policy:
    """What the relay's prune reads from settings, in one place. Defaults = do nothing beyond the
    rules that need no setting (expiry, retired kinds, bridge DM TTL)."""

    def __init__(self, retention_days: int = 0, max_events: int = 0, preserve_pubkeys=(),
                 subscribers=(), free_retention_days: int = 0, paid_retention_days: int = 0,
                 tiered_ok: bool = False, min_free_pct: float = 5.0, mirror_of_postgres: bool = False):
        self.retention_days = int(retention_days or 0)
        self.max_events = int(max_events or 0)
        self.preserve_pubkeys = set(preserve_pubkeys or ())
        self.subscribers = set(subscribers or ())
        self.free_retention_days = int(free_retention_days or 0)
        self.paid_retention_days = int(paid_retention_days or 0)
        self.tiered_ok = bool(tiered_ok)
        self.min_free_pct = float(min_free_pct)
        # A MIRROR of Postgres deletes only what neither side counts (expired events). Every other rule is
        # Postgres's to apply on its own schedule; the mirror receives those deletions. Applied here on the
        # mirror's clock (a bridged DM crossing its TTL between Postgres's nightly prunes) it left the two one
        # event apart and the next restart marked the mirror stale (2026-10-10).
        self.mirror_of_postgres = bool(mirror_of_postgres)


def _author_mask(store, n: int, pubkeys) -> np.ndarray:
    ids = [store._author_ix[p] for p in pubkeys if p in store._author_ix]
    if not ids or not n:
        return np.zeros(n, dtype=bool)
    return np.isin(np.frombuffer(store.author, dtype=np.uint32)[:n], np.asarray(ids, dtype=np.uint32))


def _git_comment_exempt(store, n: int) -> np.ndarray:
    """1111 comments whose root (`K`) is a git issue/patch/PR — the relay's _PRUNABLE_SQL exclusion."""
    m = np.zeros(n, dtype=bool)
    for k in relay._GIT_COMMENT_ROOT_KINDS:
        hit = store.idx.get(_h("t:K:%s" % k))
        hit = hit[hit < n]
        m[hit] = True
    return m


class Maintainer:
    """Runs `run_pass()` every `interval` seconds on its own low-priority thread. `policy` is a
    callable returning a Policy (re-read each pass, so settings changes apply without a restart)."""

    def __init__(self, store, policy=None, *, interval: float = 600.0, io_mb_s: float = 8.0,
                 cpu_pct: float = 10.0, busy_load: float = 0.75, log=None, now=None, throttle=None,
                 disk_usage=None, snapshot_hours: float = 24.0):
        self.store = store
        self.policy = policy or (lambda: Policy())
        self.interval = float(interval)
        self.stop_ev = threading.Event()
        self.throttle = throttle or Throttle(io_mb_s, cpu_pct, busy_load, stop=self.stop_ev)
        self.log = log or (lambda *a: None)
        self.now = now or (lambda: int(time.time()))
        self.disk_usage = disk_usage or (lambda: shutil.disk_usage(store.path))
        self.snapshot_hours = float(snapshot_hours)
        self._thread = None
        self.last = {}

    # ------------------------------------------------------------ thread
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self.stop_ev.clear()
        self._thread = threading.Thread(target=self._loop, name="posterchandb-maint", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 30.0) -> None:
        self.stop_ev.set()
        if self._thread:
            self._thread.join(timeout)

    def _loop(self) -> None:
        lower_priority()
        while not self.stop_ev.wait(self.interval):
            try:
                self.last = self.run_pass()
                if any(v for k, v in self.last.items() if k != "compacted"):
                    self.log("[posterchandb] maintenance: %s" % self.last)
            except Exception as e:      # a maintenance bug must never take the relay down
                self.log("[posterchandb] maintenance pass failed: %r" % (e,))

    # ------------------------------------------------------------ the pass
    def _apply(self, mask: np.ndarray) -> int:
        seqs = np.nonzero(mask)[0]
        n = 0
        for i in range(0, len(seqs), KILL_BATCH):
            if self.stop_ev.is_set():
                break
            t0 = time.thread_time()
            n += self.store.kill(seqs[i:i + KILL_BATCH])
            self.throttle(33 * KILL_BATCH, time.thread_time() - t0)
        return n

    def prune_masks(self, pol: Policy, now: int) -> dict:
        """Each rule as a boolean mask over the rows that exist right now (live, not compacted away).
        Pure reads of the numpy columns; nothing changes here."""
        st = self.store
        with st._lock:
            n = len(st.off)
            dead = np.frombuffer(st.dead, dtype=np.uint8)[:n].copy()
            kind = np.frombuffer(st.kind, dtype=np.uint32)[:n].copy()
            created = np.frombuffer(st.created, dtype=np.uint64)[:n].astype(np.int64)
            expires = np.frombuffer(st.expires, dtype=np.uint64)[:n].astype(np.int64)
            origin = np.frombuffer(st.origin, dtype=np.uint8)[:n].copy()
            preserved = _author_mask(st, n, pol.preserve_pubkeys)
            subs = _author_mask(st, n, pol.subscribers)
            git_exempt = _git_comment_exempt(st, n)
        live = dead == 0
        direct = origin == ORIGINS["direct"]
        prunable = np.isin(kind, np.asarray(relay._PRUNABLE_KINDS, dtype=np.uint32)) & ~((kind == 1111) & git_exempt)
        preserve_ok = ~direct & ~preserved          # the relay's _preserve_clause
        out = {}
        out["expired"] = live & (expires > 0) & (expires <= now) & \
            ~np.isin(kind, np.asarray(relay._NEVER_EXPIRE_KINDS, dtype=np.uint32))
        if pol.mirror_of_postgres:
            return out
        out["retired"] = live & np.isin(kind, np.asarray(relay._RETIRED_KINDS, dtype=np.uint32))
        sub_exempt = subs if pol.subscribers else np.zeros(n, dtype=bool)
        if pol.retention_days:
            cutoff = now - pol.retention_days * DAY
            out["aged"] = live & (created < cutoff) & prunable & preserve_ok & ~sub_exempt
        out["bridge_dm"] = live & (origin == ORIGINS["bridge"]) & np.isin(kind, [13, 1059]) & \
            (created < now - relay._BRIDGE_DM_TTL_DAYS * DAY)
        if pol.free_retention_days and pol.tiered_ok:
            base = live & direct & prunable & ~preserved
            out["aged_free"] = base & ~subs & (created < now - pol.free_retention_days * DAY)
            if pol.paid_retention_days:
                out["aged_paid"] = base & subs & (created < now - pol.paid_retention_days * DAY)
        return out

    def _orphan_zaps(self, pol: Policy, now: int, gone_mask: np.ndarray) -> np.ndarray:
        """9735 receipts (not direct, older than retention) that name a post (`e`) none of which is stored."""
        st = self.store
        n = len(gone_mask)
        out = np.zeros(n, dtype=bool)
        if not pol.retention_days:
            return out
        cutoff = now - pol.retention_days * DAY
        with st._lock:
            kind = np.frombuffer(st.kind, dtype=np.uint32)[:n]
            created = np.frombuffer(st.created, dtype=np.uint64)[:n].astype(np.int64)
            origin = np.frombuffer(st.origin, dtype=np.uint8)[:n]
            dead = np.frombuffer(st.dead, dtype=np.uint8)[:n]
            cand = np.nonzero((kind == 9735) & (origin != ORIGINS["direct"]) & (created < cutoff) & (dead == 0))[0]
        for i in range(0, len(cand), 2000):
            t0 = time.thread_time()
            with st._lock:
                for s in cand[i:i + 2000]:
                    s = int(s)
                    if st.dead[s] or st.seg[s] == DROPPED:
                        continue
                    es = [t[1] for t in st.get(s)["tags"] if len(t) >= 2 and t[0] == "e"]
                    if not es:
                        continue
                    alive = False
                    for e in es:
                        q = st._seq_of(e)
                        if q is not None and not st.dead[q] and not (q < n and gone_mask[q]):
                            alive = True
                            break
                    if not alive:
                        out[s] = True
            self.throttle(0, time.thread_time() - t0)
        return out

    def run_pass(self) -> dict:
        pol = self.policy()
        now = self.now()
        res = {}
        masks = self.prune_masks(pol, now)
        gone = np.zeros(len(next(iter(masks.values()))), dtype=bool) if masks else np.zeros(0, dtype=bool)
        for name in ("expired", "retired", "aged"):
            if name in masks:
                res[name] = self._apply(masks[name])
                gone |= masks[name]
        if "aged" in masks:
            res["orphan_zaps"] = self._apply(self._orphan_zaps(pol, now, gone))
        for name in ("bridge_dm", "aged_free", "aged_paid"):
            if name in masks:
                res[name] = self._apply(masks[name])
        if pol.max_events:
            res["cap"] = self._apply(self._cap_mask(pol))
        res["guard"] = self.disk_guard(pol)
        c = self.store.compact(pace=self.throttle)
        res["compacted"] = c.get("compacted")
        res["snapshot"] = self._maybe_snapshot(after_compaction=bool(res["compacted"]))
        return res

    def _maybe_snapshot(self, after_compaction: bool) -> bool:
        """A compaction makes the last snapshot unusable (it lists the segment that was just deleted), so take
        one right after; otherwise only every `snapshot_hours`, and only when something was written."""
        st = self.store
        if not getattr(st, "snapshots", False) or st._since_snap <= 0:
            return False
        age_h = (time.time() - getattr(st, "last_snapshot", 0.0)) / 3600.0
        if not after_compaction and (self.snapshot_hours <= 0 or age_h < self.snapshot_hours):
            return False
        try:
            return st.snapshot() is not None
        except OSError as e:
            self.log("[posterchandb] snapshot failed: %r" % (e,))
            return False

    def _cap_mask(self, pol: Policy) -> np.ndarray:
        """The relay's count cap: prunable + preserve-ok events beyond the newest `max_events` overall."""
        st = self.store
        with st._lock:
            n = len(st.off)
            dead = np.frombuffer(st.dead, dtype=np.uint8)[:n]
            live_idx = np.nonzero(dead == 0)[0]
            if len(live_idx) <= pol.max_events:
                return np.zeros(n, dtype=bool)
            created = np.frombuffer(st.created, dtype=np.uint64)[:n].astype(np.int64)
            kind = np.frombuffer(st.kind, dtype=np.uint32)[:n]
            origin = np.frombuffer(st.origin, dtype=np.uint8)[:n]
            preserved = _author_mask(st, n, pol.preserve_pubkeys)
            subs = _author_mask(st, n, pol.subscribers)
            git_exempt = _git_comment_exempt(st, n)
        order = live_idx[np.argsort(-created[live_idx], kind="stable")]
        beyond = np.zeros(n, dtype=bool)
        beyond[order[pol.max_events:]] = True
        prunable = np.isin(kind, np.asarray(relay._PRUNABLE_KINDS, dtype=np.uint32)) & ~((kind == 1111) & git_exempt)
        return beyond & prunable & (origin != ORIGINS["direct"]) & ~preserved & ~subs

    def disk_guard(self, pol: Policy) -> int:
        """Below `min_free_pct` free: drop the OLDEST re-fetchable copies (synced prunable feed — never a
        direct write, never a preserved author) and compact, a tenth of them at a time, until it recovers."""
        if pol.min_free_pct <= 0:
            return 0
        removed = 0
        for _ in range(10):
            du = self.disk_usage()
            if du.total <= 0 or 100.0 * du.free / du.total >= pol.min_free_pct or self.stop_ev.is_set():
                break
            st = self.store
            with st._lock:
                n = len(st.off)
                dead = np.frombuffer(st.dead, dtype=np.uint8)[:n]
                kind = np.frombuffer(st.kind, dtype=np.uint32)[:n]
                origin = np.frombuffer(st.origin, dtype=np.uint8)[:n]
                created = np.frombuffer(st.created, dtype=np.uint64)[:n].astype(np.int64)
                preserved = _author_mask(st, n, pol.preserve_pubkeys)
                git_exempt = _git_comment_exempt(st, n)
            ok = (dead == 0) & np.isin(kind, np.asarray(relay._PRUNABLE_KINDS, dtype=np.uint32)) & \
                ~((kind == 1111) & git_exempt) & (origin != ORIGINS["direct"]) & ~preserved
            cand = np.nonzero(ok)[0]
            if not len(cand):
                self.log("[posterchandb] disk guard: below %.1f%% free and nothing re-fetchable left to drop"
                         % pol.min_free_pct)
                break
            take = cand[np.argsort(created[cand], kind="stable")][:max(1, len(cand) // 10)]
            mask = np.zeros(n, dtype=bool)
            mask[take] = True
            removed += self._apply(mask)
            st.flush()
            while st.compact(sid=_deadest(st), pace=self.throttle).get("compacted"):
                if self.stop_ev.is_set():
                    break
        return removed


def _deadest(st):
    pcts = [(pct, sid) for sid, pct in st.segment_dead_pct().items() if sid != st._active and pct > 0]
    return max(pcts)[1] if pcts else None
