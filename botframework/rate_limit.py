"""In-memory sliding-window rate limiter for bot listeners.

Permissionless platforms (Nostr: anyone can mention the bot from any relay) are trivially
spammed into burning GPU/LLM. This caps requests **per sender** and **globally** over a
rolling window. Per-process state — fine for the single-process listeners.

allow(key) returns True and consumes a slot only when BOTH the global and per-key caps
have room; otherwise returns False and consumes nothing (so the caller can re-check a
deferred request next poll without it counting against the limit). A cap of 0 disables
that dimension.
"""

import time


class SlidingWindowLimiter:
    def __init__(self, per_key: int, global_max: int, window: float, exempt=None):
        self.per_key = int(per_key)
        self.global_max = int(global_max)
        self.window = float(window)
        self.exempt = set(exempt or [])
        self._hits: dict = {}   # key -> [monotonic timestamps], ascending
        self._global: list = []

    @staticmethod
    def _prune(lst, cutoff):
        while lst and lst[0] < cutoff:
            lst.pop(0)

    def allow(self, key) -> bool:
        if key in self.exempt:
            return True
        now = time.monotonic()
        cutoff = now - self.window
        if self.global_max > 0:
            self._prune(self._global, cutoff)
            if len(self._global) >= self.global_max:
                return False
        if self.per_key > 0:
            lst = self._hits.setdefault(key, [])
            self._prune(lst, cutoff)
            if len(lst) >= self.per_key:
                return False
            lst.append(now)
        if self.global_max > 0:
            self._global.append(now)
        # Bound memory: drop keys whose windows have fully drained.
        if len(self._hits) > 10000:
            self._hits = {k: v for k, v in self._hits.items() if v}
        return True
