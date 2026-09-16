"""Replay protection and idempotency for VM-host requests.

TWO DIFFERENT QUESTIONS, and conflating them is how a retry turns into a second VM:

  * "Have I seen this EVENT?" — `SeenIds`, an LRU of event ids. A relay redelivers on reconnect and
    the firehose can hand the same event over twice; either way it is dropped without an answer,
    because the first delivery already produced one.
  * "Have I done this OPERATION?" — `OpJournal`, keyed on (requester, the request's own `id`). A
    client that heard nothing retries with a NEW event (new created_at, new signature) carrying the
    SAME id. The event is new, so the seen-LRU cannot catch it; the journal returns the stored result
    instead of creating the VM, deleting the disk or rebooting the guest a second time. A retry that
    arrives while the first run is still going waits on it and gets the same answer.

The journal is also written to `.state/journal/ops.jsonl` so an app restart between "did it" and
"said so" does not turn the client's retry into a repeat. Entries older than 15 minutes are dropped
on load and on write; a request can only be retried inside its own short expiration anyway.
"""
from __future__ import annotations

import asyncio
import collections
import json
import os
import time
from pathlib import Path

JOURNAL_TTL = 15 * 60


class SeenIds:
    def __init__(self, cap: int = 2000):
        self.cap = cap
        self._d: "collections.OrderedDict[str, float]" = collections.OrderedDict()

    def __contains__(self, eid) -> bool:
        return eid in self._d

    def add(self, eid: str) -> None:
        self._d[eid] = time.time()
        self._d.move_to_end(eid)
        while len(self._d) > self.cap:
            self._d.popitem(last=False)


class OpJournal:
    def __init__(self, path: Path | None = None, ttl: int = JOURNAL_TTL, now=time.time):
        self.path = Path(path) if path else None
        self.ttl = ttl
        self.now = now
        self._done: dict = {}          # (requester, id) -> (ts, op, response)
        self._running: dict = {}       # (requester, id) -> asyncio.Future
        self._load()

    def _load(self) -> None:
        if not self.path or not self.path.exists():
            return
        cutoff = self.now() - self.ttl
        try:
            with open(self.path, encoding="utf-8") as f:
                for line in f:
                    try:
                        rec = json.loads(line)
                        if rec.get("t", 0) >= cutoff:
                            self._done[(rec["r"], rec["id"])] = (rec["t"], rec["op"], rec["res"])
                    except (ValueError, KeyError, TypeError):
                        continue
        except OSError:
            pass

    def _prune(self) -> None:
        cutoff = self.now() - self.ttl
        for k in [k for k, v in self._done.items() if v[0] < cutoff]:
            self._done.pop(k, None)

    def _persist(self) -> None:
        if not self.path:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                for (r, i), (t, op, res) in self._done.items():
                    f.write(json.dumps({"t": t, "r": r, "id": i, "op": op, "res": res}) + "\n")
            os.replace(tmp, self.path)
        except OSError:
            pass

    def lookup(self, requester: str, req_id: str):
        self._prune()
        rec = self._done.get((requester, req_id))
        return rec[2] if rec else None

    async def run_once(self, requester: str, req_id: str, op: str, fn):
        """Run `fn()` at most once per (requester, id) within the TTL; every caller gets the same
        response dict. A different op under a reused id is refused rather than answered with the
        stored result of something else."""
        key = (requester, req_id)
        self._prune()
        rec = self._done.get(key)
        if rec is not None:
            if rec[1] != op:
                return None
            return rec[2]
        fut = self._running.get(key)
        if fut is not None:
            return await asyncio.shield(fut)
        fut = asyncio.get_running_loop().create_future()
        self._running[key] = fut
        try:
            res = await fn()
        except BaseException as e:
            self._running.pop(key, None)
            if not fut.done():
                if isinstance(e, Exception):
                    fut.set_exception(e)
                    fut.exception()      # mark retrieved: a waiter is optional
                else:
                    fut.cancel()
            raise
        self._running.pop(key, None)
        if not fut.done():
            fut.set_result(res)
        # Only a SUCCESS is remembered. A refusal (busy, capacity, a libvirt hiccup) must let the
        # retry actually retry; remembering it would pin the failure for fifteen minutes.
        if isinstance(res, dict) and res.get("ok"):
            self._done[key] = (self.now(), op, res)
            self._persist()
        return res
