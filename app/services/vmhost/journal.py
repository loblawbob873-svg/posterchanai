"""Replay protection and idempotency for VM-host requests.

TWO DIFFERENT QUESTIONS, and conflating them is how a retry turns into a second VM:

  * "Have I seen this EVENT?" — `SeenIds`, event ids kept for the clock window (by time, never evicted
    by count). A relay redelivers on reconnect and
    the firehose can hand the same event over twice; either way it is dropped without an answer,
    because the first delivery already produced one.
  * "Have I done this OPERATION?" — `OpJournal`, keyed on (requester, the request's own `id`) and
    checked against the op AND a hash of its arguments (a reused id for anything else is refused). A
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
import json
import os
import threading
import time
from pathlib import Path

JOURNAL_TTL = 15 * 60


class SeenIds:
    """Event ids already handled, kept by TIME, not by count.

    An id is kept for `ttl` seconds after it was first handled — the transport passes the width of its
    clock window, so an id lives exactly as long as a replay of its event could still get past the
    window. Evicting by COUNT let a flood of cheap valid requests push a victim's id out, after which a
    replay of the victim's captured request was "new". At `cap` live ids a NEW id is refused ("full")
    rather than an old one forgotten: dropping a request under a flood is recoverable, re-running one
    is not.

    With a `path` the ids are also appended to a small log (`flush`, meant for a worker thread) and
    re-read on start, so a PROCESS restart does not re-run what the relay replays to the new
    subscription either."""

    def __init__(self, ttl: float = 150, cap: int = 50_000, path: Path | None = None, now=time.time):
        self.ttl = ttl
        self.cap = cap
        self.now = now
        self.path = Path(path) if path else None
        self._d: dict = {}             # eid -> expiry; insertion order == expiry order
        self._unflushed: list = []
        self._io = threading.Lock()
        self._lines = 0
        self._load()

    def _purge(self) -> None:
        t = self.now()
        while self._d:
            k = next(iter(self._d))
            if self._d[k] > t:
                break
            del self._d[k]

    def __contains__(self, eid) -> bool:
        exp = self._d.get(eid)
        return exp is not None and exp > self.now()

    def __len__(self) -> int:
        self._purge()
        return len(self._d)

    def clear(self) -> None:
        self._d.clear()
        self._unflushed.clear()

    def add(self, eid: str) -> str:
        """'ok' (recorded), 'dup' (already handled) or 'full' (refuse this request)."""
        self._purge()
        if eid in self._d:
            return "dup"
        if len(self._d) >= self.cap:
            return "full"
        exp = self.now() + self.ttl
        self._d[eid] = exp
        if self.path is not None:
            self._unflushed.append((exp, eid))
        return "ok"

    def _load(self) -> None:
        if self.path is None or not self.path.exists():
            return
        t = self.now()
        try:
            with open(self.path, encoding="utf-8") as f:
                for line in f:
                    self._lines += 1
                    parts = line.split()
                    try:
                        exp, eid = float(parts[0]), parts[1]
                    except (IndexError, ValueError):
                        continue
                    if exp > t and len(self._d) < self.cap:
                        self._d[eid] = exp
        except OSError:
            pass
        self._d = dict(sorted(self._d.items(), key=lambda kv: kv[1]))

    def flush(self) -> None:
        """Append what was added since the last flush; compact once the log is mostly expired. Blocking
        file I/O — call it from a worker thread."""
        if self.path is None:
            return
        with self._io:
            pending, self._unflushed = self._unflushed, []
            if not pending:
                return
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                if self._lines + len(pending) > 2 * len(self._d) + 1000:
                    tmp = self.path.with_suffix(".tmp")
                    live = list(self._d.items())
                    with open(tmp, "w", encoding="utf-8") as f:
                        f.writelines(f"{exp:.0f} {eid}\n" for eid, exp in live)
                    os.replace(tmp, self.path)
                    self._lines = len(live)
                else:
                    with open(self.path, "a", encoding="utf-8") as f:
                        f.writelines(f"{exp:.0f} {eid}\n" for exp, eid in pending)
                    self._lines += len(pending)
            except OSError:
                pass


class OpJournal:
    def __init__(self, path: Path | None = None, ttl: int = JOURNAL_TTL, now=time.time):
        self.path = Path(path) if path else None
        self.ttl = ttl
        self.now = now
        self._done: dict = {}          # (requester, id) -> (ts, op, response, args hash)
        self._running: dict = {}       # (requester, id) -> (asyncio.Future, op, args hash)
        self._write_lock: asyncio.Lock | None = None
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
                            self._done[(rec["r"], rec["id"])] = (rec["t"], rec["op"], rec["res"],
                                                                 str(rec.get("h", "")))
                    except (ValueError, KeyError, TypeError):
                        continue
        except OSError:
            pass

    def _prune(self) -> None:
        cutoff = self.now() - self.ttl
        for k in [k for k, v in self._done.items() if v[0] < cutoff]:
            self._done.pop(k, None)

    def _write(self, lines: list) -> None:
        """Blocking: runs in a worker thread (see `_persist`)."""
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                f.writelines(lines)
            os.replace(tmp, self.path)
        except OSError:
            pass

    async def _persist(self) -> None:
        """Snapshot on the loop, write in a thread — never a synchronous file rewrite on the single
        uvicorn worker's event loop. Writes are serialized so an older snapshot cannot land last."""
        if not self.path:
            return
        if self._write_lock is None:
            self._write_lock = asyncio.Lock()
        async with self._write_lock:
            lines = [json.dumps({"t": t, "r": r, "id": i, "op": op, "res": res, "h": h}) + "\n"
                     for (r, i), (t, op, res, h) in list(self._done.items())]
            await asyncio.to_thread(self._write, lines)

    def lookup(self, requester: str, req_id: str):
        self._prune()
        rec = self._done.get((requester, req_id))
        return rec[2] if rec else None

    async def run_once(self, requester: str, req_id: str, op: str, fn, args_hash: str = ""):
        """Run `fn()` at most once per (requester, id) within the TTL; every caller gets the same
        response dict. A reused id carrying a different op OR different arguments (`args_hash`) is
        refused (None) — never answered with the stored result of something else, and never run."""
        key = (requester, req_id)
        self._prune()
        rec = self._done.get(key)
        if rec is not None:
            if rec[1] != op or rec[3] != args_hash:
                return None
            return rec[2]
        running = self._running.get(key)
        if running is not None:
            fut, r_op, r_hash = running
            if r_op != op or r_hash != args_hash:
                return None
            return await asyncio.shield(fut)
        fut = asyncio.get_running_loop().create_future()
        self._running[key] = (fut, op, args_hash)
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
            self._done[key] = (self.now(), op, res, args_hash)
            await self._persist()
        return res
