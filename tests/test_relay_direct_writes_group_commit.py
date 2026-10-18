"""Direct writes are made durable together: one fsync for everybody waiting, never one per event in a row.

2026-10-17, scripts/relay_loadtest.py (400 clients on a copy of poster.place's store, ~13 posts/s): 25 s median and
50 s p99 from publishing a post to its OK -- each direct write was fsynced on the single writer thread before the
next could start, so throughput was one write per fsync and each client's next REQ waited behind its own publish.
"""
import asyncio
import time

from app.services.nostr.event import build_event
from tests.test_relay_prune import _run, store_factory  # noqa: F401  (fixture)

FSYNC = 0.2


def test_thirty_people_publishing_at_once_cost_a_few_fsyncs_not_thirty(store_factory):  # noqa: F811
    async def go(loop):
        store = store_factory(loop)
        syncs, real = [], store.store.sync

        def slow_sync():
            time.sleep(FSYNC)
            syncs.append(time.monotonic())
            return real()
        store.store.sync = slow_sync
        evs = [build_event(bytes([i + 1]) * 32, 1, "post %d" % i, []) for i in range(30)]
        t0 = time.monotonic()
        oks = await asyncio.gather(*(store.add_event(e, origin="direct") for e in evs))
        took = time.monotonic() - t0
        assert all(oks), oks
        assert len(syncs) <= 4, ("one fsync per write, in a row", len(syncs))
        assert took < 30 * FSYNC / 3, took
        # durable before "saved": every write was answered only after a sync finished after it was stored
        have = {e["id"] for e in await store.query([{"ids": [e["id"] for e in evs]}])}
        assert have == {e["id"] for e in evs}
    _run(go)


def test_a_failed_sync_is_not_saved(store_factory):  # noqa: F811
    async def go(loop):
        store = store_factory(loop)

        def broken():
            raise OSError("disk gone")
        store.store.sync = broken
        ev = build_event(bytes([9]) * 32, 1, "x", [])
        assert await store.add_event(ev, origin="direct") is False, "an author was told saved without a sync"
    _run(go)


def test_a_write_answered_during_shutdown_is_fsynced_first(store_factory):  # noqa: F811
    """A direct write that lands just before close() was answered "saved" with NO sync once writes were closed --
    before close's own final fsync, so a crash in that window lost an event its author was told was stored."""
    async def go(loop):
        store = store_factory(loop)
        synced = []
        real = store.store.sync

        def counting():
            synced.append(1)
            return real()
        store.store.sync = counting
        ev = build_event(bytes([7]) * 32, 1, "just before the stop", [])
        await loop.run_in_executor(None, store._add_event_sync, ev, "direct")
        store._writes_closed = True                      # close() has begun
        assert await asyncio.wait_for(store._durable(), 5) is True
        assert synced, "the author was told saved without an fsync"
    _run(go)
