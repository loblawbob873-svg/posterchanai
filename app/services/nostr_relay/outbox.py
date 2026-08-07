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
import time

from app.services.nostr import relay as _relay

logger = logging.getLogger(__name__)


class Outbox:
    def __init__(self, upstream, min_interval: float = 1.0, maxsize: int = 500, direct: bool = False,
                 retries: int = 2, retry_delay: float = 15.0, max_inflight_retries: int = 50,
                 label: str = "outbox"):
        # Named, because there are two of these now and the private mirror's drops mean something
        # entirely different from the public blaster's: one is "we were polite to a stranger's
        # relay", the other is "your notes were not backed up".
        self.label = label
        self.upstream = upstream
        self.min_interval = min_interval
        self.direct = direct
        # Per-event retry: relays that don't accept on the first pass (a flaky relay or one whose
        # connection was still re-establishing right after a relay restart) are re-sent a few times
        # then given up on. Runs in the background so a slow relay can't stall the main drain.
        self.retries = max(0, retries)
        self.retry_delay = retry_delay
        self._max_inflight_retries = max_inflight_retries
        self._q: asyncio.Queue = asyncio.Queue(maxsize=maxsize)
        self._task: asyncio.Task | None = None
        self._retry_tasks: set[asyncio.Task] = set()
        # Serialize+pace retry sends: the misses are usually the SAME relay, so without this all
        # in-flight retry tasks would hit it at once when it recovers — the exact blast min_interval
        # is meant to prevent.
        self._retry_gate = asyncio.Lock()
        self._dropped = 0
        # Lifetime counters for the public Server Stats relay panel (and the admin view). Plain ints
        # bumped on the drain path — no timers, no history, so reading them is a dict build.
        #
        # `_acks`/`_targets` are a RATE, and they are the honest way to ask "are our broadcasts
        # landing". The obvious alternative — count the events every target accepted — is worthless
        # on a real fanout: with 26 upstream relays it takes ONE of them being briefly unreachable
        # for the answer to be zero, so it reads as total failure while delivery is in fact fine.
        # `_gave_up` is the one number here that means something went undelivered, and even then only
        # to SOME relays; it is separate from `_dropped`, which never left the queue at all.
        self._sent = 0
        self._acks = 0
        self._targets = 0
        self._no_targets = 0   # events dequeued while EVERY upstream was paused (nothing sent)
        self._failed = 0
        self._gave_up = 0
        self._last_at = 0.0

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._worker())

    def stop(self) -> None:
        if self._task:
            self._task.cancel()
            self._task = None
        for t in list(self._retry_tasks):
            t.cancel()
        self._retry_tasks.clear()

    def enqueue(self, ev: dict) -> None:
        """Non-blocking; called from the WS write path. Drops (with a log) if the queue is
        full so a post-blasting client can't balloon memory or our upstream send rate."""
        try:
            self._q.put_nowait(ev)
        except asyncio.QueueFull:
            self._dropped += 1
            if self._dropped % 50 == 1:
                logger.warning("[nostr-relay] %s queue full — dropped %d (sending too fast)",
                               self.label, self._dropped)

    async def _worker(self) -> None:
        while True:
            ev = await self._q.get()
            # Re-read self.upstream each event so a live upstream change (reload-upstream retargets
            # outbox.upstream in place) takes effect without restarting the worker — otherwise the
            # miss-set / retry path would keep hitting the OLD relay set.
            # SENDABLE targets, not every configured relay. A relay the breaker has paused (dead, or
            # refusing our writes with pow:/blocked:/restricted:) can never ack, so counting it here
            # made it a permanent miss: retried twice per event forever — which is what pinned
            # `retrying` at its 50-task cap, and the drain only spawns a retry while UNDER that cap,
            # so REAL misses silently stopped being retried at all. It also dragged the delivery rate
            # down to a figure describing our own circuit breaker rather than the network.
            targets = _relay.publishable(self.upstream)
            try:
                if not targets:
                    # Every upstream is paused. Deliberately NOT `continue`: the pacing sleep is at
                    # the bottom of this loop, so skipping it would spin the drain through the whole
                    # queue at full speed, burning every event with nothing sent and nothing said
                    # per event. Count it, say it occasionally, and keep the same cadence.
                    self._no_targets += 1
                    if self._no_targets % 50 == 1:
                        logger.warning("[nostr-relay] %s: every upstream relay is paused — %d event(s) "
                                       "not sent", self.label, self._no_targets)
                else:
                    accepted = await _relay.publish_to(targets, ev, direct=self.direct)
                    skipped = len(_relay.normalize_relays(self.upstream)) - len(targets)
                    logger.info("[nostr-relay] outbox: %s → %d/%d relays%s (queue=%d)",
                                ev.get("id", "")[:12], len(accepted), len(targets),
                                f" ({skipped} paused)" if skipped else "", self._q.qsize())
                    misses = set(targets) - accepted
                    self._sent += 1
                    self._acks += len(accepted)
                    self._targets += len(targets)
                    self._last_at = time.time()
                    if misses and self.retries > 0 and len(self._retry_tasks) < self._max_inflight_retries:
                        t = asyncio.create_task(self._retry_misses(ev, misses))
                        self._retry_tasks.add(t)
                        t.add_done_callback(self._retry_tasks.discard)
            except Exception as e:
                self._failed += 1
                logger.warning("[nostr-relay] outbox publish failed: %s", e)
            # Pace: don't blast the public relays back-to-back.
            if self.min_interval > 0:
                await asyncio.sleep(self.min_interval)

    async def _retry_misses(self, ev: dict, misses: set) -> None:
        """Re-send `ev` to the relays that didn't accept it — a few times, then give up.

        Runs as a background task so the main drain keeps pace. Re-publishing is safe: relays
        dedup by event id, so a relay that already stored it just ACKs again."""
        remaining = set(misses)
        eid = ev.get("id", "")[:12]
        for attempt in range(1, self.retries + 1):
            await asyncio.sleep(self.retry_delay)
            # Re-filter every attempt. A relay can start refusing BETWEEN the first publish and a
            # retry — a `pow:`/`blocked:` answer to some other event is what usually trips it — and
            # a miss set frozen at send time would keep dialling it for the full retry schedule,
            # which is the waste the breaker exists to stop.
            remaining = set(_relay.publishable(list(remaining)))
            if not remaining:
                return
            # Paced behind the gate so concurrent retry tasks don't blast a recovering relay.
            async with self._retry_gate:
                ok = await _relay.publish_to(list(remaining), ev, direct=self.direct)
                if self.min_interval > 0:
                    await asyncio.sleep(self.min_interval)
            remaining -= ok
            if ok:
                logger.info("[nostr-relay] outbox retry %d/%d: %s recovered %d relay(s)",
                            attempt, self.retries, eid, len(ok))
            if not remaining:
                return
        self._gave_up += 1
        logger.warning("[nostr-relay] outbox: %s gave up on %d relay(s) after %d retries: %s",
                       eid, len(remaining), self.retries, ", ".join(sorted(remaining)))

    def stats(self) -> dict:
        """A snapshot for the status file → Server Stats. Counts only; no event ids, no relay URLs
        (the private mirror's targets are the operator's own machines and this feeds a PUBLIC page).
        `queued`/`retrying` are live depths, everything else is since the relay process started."""
        return {
            "label": self.label,
            "queued": self._q.qsize(),
            "max": self._q.maxsize,
            "relays": len(_relay.normalize_relays(self.upstream)),
            # How many of those we are NOT currently sending to (dead, or refusing our writes).
            # Without this the delivery rate is unreadable: "60%" means something very different
            # when a third of the list is a relay that has told us in words it will never take our
            # events. It is a LIVE depth like `queued`, not a lifetime counter.
            "paused": max(0, len(_relay.normalize_relays(self.upstream))
                          - len(_relay.publishable(self.upstream))),
            "not_sent": self._no_targets,   # dequeued with every upstream paused
            "sent": self._sent,
            "acks": self._acks,          # (relay, event) pairs accepted on the first attempt…
            "targets": self._targets,    # …out of this many attempted — a rate, not all-or-nothing
            "dropped": self._dropped,    # never left the queue (overflow)
            "failed": self._failed,      # the publish call itself raised
            "gave_up": self._gave_up,    # left the queue, still unaccepted after the last retry
            "retrying": len(self._retry_tasks),
            "last_at": int(self._last_at),
        }
