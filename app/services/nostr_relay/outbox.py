"""Outbox: re-broadcast inbound client writes to the upstream public relays — PACED.

Only events *submitted to this relay* over its WS `EVENT` (our own bots/users who point
their relay list here) are fanned out; events we ingested from upstream are NOT re-broadcast
(they already came from there). To avoid hammering the public relays (and getting
rate-limited/blocked), sends go through a bounded queue drained by a single worker that
enforces a minimum interval between publishes. The queue drops on overflow rather than
growing unbounded under a burst.
"""

import asyncio
import logging

from app.services.nostr import relay as _relay

logger = logging.getLogger(__name__)


class Outbox:
    def __init__(self, upstream, min_interval: float = 1.0, maxsize: int = 500, direct: bool = False):
        self.upstream = upstream
        self.min_interval = min_interval
        self.direct = direct
        self._q: asyncio.Queue = asyncio.Queue(maxsize=maxsize)
        self._task: asyncio.Task | None = None
        self._dropped = 0

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._worker())

    def stop(self) -> None:
        if self._task:
            self._task.cancel()
            self._task = None

    def enqueue(self, ev: dict) -> None:
        """Non-blocking; called from the WS write path. Drops (with a log) if the queue is
        full so a post-blasting client can't balloon memory or our upstream send rate."""
        try:
            self._q.put_nowait(ev)
        except asyncio.QueueFull:
            self._dropped += 1
            if self._dropped % 50 == 1:
                logger.warning("[nostr-relay] outbox queue full — dropped %d (sending too fast)",
                               self._dropped)

    async def _worker(self) -> None:
        while True:
            ev = await self._q.get()
            try:
                n = await _relay.publish(self.upstream, ev, direct=self.direct)
                logger.info("[nostr-relay] outbox: %s → %d/%d relays (queue=%d)",
                            ev.get("id", "")[:12], n, len(self.upstream), self._q.qsize())
            except Exception as e:
                logger.warning("[nostr-relay] outbox publish failed: %s", e)
            # Pace: don't blast the public relays back-to-back.
            if self.min_interval > 0:
                await asyncio.sleep(self.min_interval)
