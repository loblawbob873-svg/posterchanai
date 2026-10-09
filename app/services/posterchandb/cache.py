"""PosterChanDB read cache: which segments' bytes stay in RAM, decided from the MACHINE, not a fixed size.

The store keeps indexes, ids and per-event columns in RAM always (~100 bytes an event). The record bytes
of each CLOSED segment are a cache: resident (a bytearray) or cold (read with pread through the kernel's
page cache). This decides which, every `interval` seconds:

  DRAIN — when the machine is short of memory: `MemAvailable` below the low-water mark, or the kernel's
  own pressure signal (PSI `some avg10`, /proc/pressure/memory) above `psi_high`, or the resident total
  over the budget. The least-recently-read segments go first, until the shortfall is covered. A dropped
  bytearray of this size is an mmap'd allocation, so it goes straight back to the OS.

  FILL — only when it pays and only when there is room: a cold segment comes back after it has been read
  at least `fill_hits` times recently, when RAM is comfortably above the low-water mark (twice it, after
  the load), when it fits in the budget, and not within `cooldown` seconds of being drained (no thrash).
  At most one segment per tick, read sequentially.

`budget_mb` is the admin setting `posterchandb_read_cache_mb`: 0 = automatic (30% of RAM). The active
segment is never drained — its unflushed tail exists only in RAM.
"""
from __future__ import annotations

import threading
import time

MB = 1024 * 1024


def meminfo(path: str = "/proc/meminfo") -> dict:
    """{'total': bytes, 'available': bytes} — MemAvailable is the kernel's own estimate of what can be
    allocated without swapping (it counts reclaimable page cache), which is the right question here."""
    out = {}
    try:
        with open(path) as f:
            for line in f:
                k, _, v = line.partition(":")
                if k in ("MemTotal", "MemAvailable"):
                    out["total" if k == "MemTotal" else "available"] = int(v.split()[0]) * 1024
    except OSError:
        pass
    return out


def psi_some_avg10(path: str = "/proc/pressure/memory"):
    """% of the last 10 s in which some task stalled waiting for memory, or None when PSI is unavailable."""
    try:
        with open(path) as f:
            for line in f:
                if line.startswith("some"):
                    for part in line.split():
                        if part.startswith("avg10="):
                            return float(part[6:])
    except (OSError, ValueError):
        pass
    return None


class CacheGovernor:
    def __init__(self, store=None, *, budget_mb: float = 0, interval: float = 5.0, low_water_mb: float = 0,
                 psi_high: float = 10.0, fill_hits: int = 32, cooldown: float = 300.0,
                 mem=meminfo, psi=psi_some_avg10, clock=time.monotonic, log=None):
        self.store = store
        self.budget_mb = float(budget_mb or 0)
        self.interval = float(interval)
        self.low_water_mb = float(low_water_mb or 0)
        self.psi_high = float(psi_high)
        self.fill_hits = int(fill_hits)
        self.cooldown = float(cooldown)
        self.mem, self.psi, self.clock = mem, psi, clock
        self.log = log or (lambda *a: None)
        self.drained_at: dict[int, float] = {}
        self._stop = threading.Event()
        self._thread = None
        self.last: dict = {}

    # ------------------------------------------------------------ the numbers
    def budget(self, total: int) -> int:
        return int(self.budget_mb * MB) if self.budget_mb > 0 else int(total * 0.30)

    def low_water(self, total: int) -> int:
        if self.low_water_mb > 0:
            return int(self.low_water_mb * MB)
        return max(512 * MB, int(total * 0.10))

    def admit(self, nbytes: int) -> bool:
        """At startup: may a segment just read stay resident? Only within the budget and while RAM stays
        comfortably above the low-water mark — a store opening on a busy box starts mostly cold."""
        m = self.mem()
        total, avail = m.get("total", 0), m.get("available", 0)
        if not total:
            return True
        resident = self.store.resident_bytes() if self.store is not None else 0
        return resident + nbytes <= self.budget(total) and avail - nbytes > 2 * self.low_water(total)

    # ------------------------------------------------------------ one decision
    def tick(self) -> dict:
        st = self.store
        m = self.mem()
        total, avail = m.get("total", 0), m.get("available", 0)
        res = {"drained": [], "filled": [], "pressure": False}
        if not total:
            return res
        low, budget = self.low_water(total), self.budget(total)
        p = self.psi()
        now = self.clock()
        with st._lock:
            closed = [(sid, len(a)) for sid, a in st.arenas.items() if a is not None and sid != st._active]
            cold = [sid for sid, a in st.arenas.items() if a is None]
            resident = st.resident_bytes()
            last = dict(st.seg_last)
            hits = dict(st.seg_hits)
            for sid in list(st.seg_hits):          # decay: "recently" means the last few ticks
                st.seg_hits[sid] = st.seg_hits[sid] / 2.0
        pressure = avail < low or (p is not None and p > self.psi_high)
        res["pressure"] = pressure
        # DRAIN: least recently read first
        need = 0
        if pressure:
            need = max(int(low * 1.5) - avail, 1)
        over = resident - budget
        need = max(need, over)
        if need > 0:
            for sid, size in sorted(closed, key=lambda x: last.get(x[0], 0.0)):
                if need <= 0:
                    break
                freed = st.evict(sid)
                if freed:
                    need -= freed
                    resident -= freed
                    avail += freed
                    self.drained_at[sid] = now
                    res["drained"].append(sid)
        # FILL: hottest cold segment that pays, fits, and was not just drained
        if not pressure and not res["drained"]:
            for sid in sorted(cold, key=lambda s: -hits.get(s, 0)):
                if hits.get(sid, 0) < self.fill_hits:
                    break
                size = st.seg_size.get(sid, 0)
                if now - self.drained_at.get(sid, -1e18) < self.cooldown:
                    continue
                if resident + size > budget or avail - size < 2 * low:
                    continue
                if st.load(sid):
                    res["filled"].append(sid)
                break
        res.update(available_mb=avail // MB, resident_mb=resident // MB, budget_mb=budget // MB, psi=p)
        self.last = res
        if res["drained"] or res["filled"]:
            self.log("[posterchandb] cache: %s" % res)
        return res

    # ------------------------------------------------------------ thread
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="posterchandb-cache", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 10.0) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout)

    def _loop(self) -> None:
        while not self._stop.wait(self.interval):
            try:
                self.tick()
            except Exception as e:      # the cache must never take the relay down
                self.log("[posterchandb] cache tick failed: %r" % (e,))
