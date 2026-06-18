"""Windowed Web-of-Trust ingestion — the heart of the relay.

Each tick pulls recent notes authored by WoT members from the upstream public relays into
the local store, in overlapping time windows so nothing is missed across polls (the same
drain-with-overlap idea as fedi_timeline_service). Also:
  - **profile auto-download**: fills missing kind-0 for WoT members so clients get names/avatars;
  - **thread completion**: backfills reply-to (`e`-tag) ancestors that we don't have, even if
    authored outside the WoT (stored as origin='ancestor'), so threads aren't orphaned.

All ingested events are signature-verified (untrusted source) and WoT-gated before storage,
and new events are pushed live to matching open subscriptions.
"""

import time
import asyncio
import logging

from app.services.nostr import relay as _relay
from app.services.nostr.event import verify_event
from .langfilter import blocked_language, blocked_word

logger = logging.getLogger(__name__)


def _is_evid(x) -> bool:
    return isinstance(x, str) and len(x) == 64


async def sync_tick(store, gate, server, upstream, cfg) -> int:
    """One windowed sync pass over the WoT author set. Returns count of new events."""
    members = sorted(gate.members())   # stable order so the rotating offset is meaningful
    n_members = len(members)
    if not members:
        return 0
    now = int(time.time())
    window = cfg["sync_window_sec"]
    overlap = cfg["overlap_sec"]
    kinds = cfg["ingest_kinds"]
    batch = cfg["author_batch"]
    blocked = cfg.get("blocked_langs")
    blocked_words = cfg.get("blocked_words")
    pace = cfg.get("request_pace_sec", 1.0)
    direct = cfg.get("direct", False)
    deadline = time.monotonic() + cfg.get("budget_sec", 45)

    # Rotating sweep: a WoT too large to scan in one budget is still FULLY covered. We continue
    # from where the last tick's budget stopped (`sync_offset`) and cycle through every author.
    # All queries in one cycle share a time floor (`sync_floor`); when a full cycle completes,
    # the floor advances to when that cycle started — so consecutive cycles overlap and no
    # author's notes are ever missed (the tail is just delayed by up to one cycle).
    async def _kv_int(key, default):
        try:
            v = await store.kv_get(key)
            return int(v) if v not in (None, "") else default
        except (ValueError, TypeError):
            return default

    offset = await _kv_int("sync_offset", 0)
    if offset >= n_members:
        offset = 0
    floor = await _kv_int("sync_floor", 0) or (now - window)
    if offset == 0:
        await store.kv_set("sync_cycle_start", str(now))
    cycle_start = await _kv_int("sync_cycle_start", now)
    since = max(0, floor - overlap)
    until = now

    new_events = []
    i = offset
    scanned = 0
    while i < n_members:
        if time.monotonic() > deadline:
            break
        if scanned > 0 and pace > 0:
            await asyncio.sleep(pace)   # pace upstream REQs — don't blast the relays
        chunk = members[i:i + batch]
        scanned += len(chunk)
        try:
            evs = await _relay.query(
                upstream, [{"authors": chunk, "kinds": kinds, "since": since, "until": until}],
                direct=direct)
        except Exception as e:
            logger.warning("[nostr-relay] sync query failed: %s", e)
            i += len(chunk)
            continue
        for ev in evs:
            if not gate.is_member(ev.get("pubkey", "")):
                continue
            if not _is_evid(ev.get("id")):
                continue
            if await store.has_event(ev["id"]):
                continue
            if not verify_event(ev):
                continue
            if int(ev.get("kind", 1)) == 1:
                _content = ev.get("content", "")
                if blocked and blocked_language(_content, blocked):
                    continue
                if blocked_words and blocked_word(_content, blocked_words):
                    continue
            if await store.add_event(ev, origin="wot"):
                new_events.append(ev)
        i += len(chunk)

    # Persist the sweep position. On a completed cycle, wrap to 0 and advance the floor to this
    # cycle's start (next cycle re-covers [cycle_start, now], overlapping the one just finished).
    cycled = i >= n_members
    if cycled:
        await store.kv_set("sync_offset", "0")
        await store.kv_set("sync_floor", str(cycle_start))
    else:
        await store.kv_set("sync_offset", str(i))

    for ev in new_events:
        await server.subs.fanout(ev)

    if cfg.get("fetch_ancestors", True) and new_events:
        try:
            await backfill_ancestors(store, server, upstream, new_events,
                                     cfg.get("max_ancestors", 20), direct)
        except Exception as e:
            logger.warning("[nostr-relay] ancestor backfill failed: %s", e)

    try:
        await fetch_missing_profiles(store, upstream, batch, cfg.get("profile_limit", 500),
                                     pace, direct)
    except Exception as e:
        logger.warning("[nostr-relay] profile fetch failed: %s", e)

    logger.info("[nostr-relay] sync tick: %d new (window %ds, %d/%d members scanned)",
                len(new_events), now - since, scanned, len(members))
    return len(new_events)


async def backfill_author(store, server, upstream, pubkey: str, *, direct: bool = False,
                          kinds=None, pace: float = 1.0, max_total: int = 20000,
                          max_pages: int = 200) -> int:
    """Backfill a single author's FULL history from upstream into the store — paging back in
    time with `until`. Writes straight to the store (origin='wot'), so it does NOT go through
    the WS write path and is NOT re-broadcast by the outbox. Used to seed e.g. the operator's
    own posts. The author should already be a WoT/operator member."""
    kinds = kinds or [0, 1, 3, 6, 7]
    until = int(time.time())
    stored = 0
    for _ in range(max_pages):
        try:
            evs = await _relay.query(
                upstream, [{"authors": [pubkey], "kinds": kinds, "until": until, "limit": 200}],
                direct=direct)
        except Exception as e:
            logger.warning("[nostr-relay] backfill query failed: %s", e)
            break
        if not evs:
            break
        oldest = until
        for ev in evs:
            if ev.get("pubkey") != pubkey:
                continue
            oldest = min(oldest, int(ev.get("created_at", until)))
            if not _is_evid(ev.get("id")) or not verify_event(ev):
                continue
            if await store.has_event(ev["id"]):
                continue
            if await store.add_event(ev, origin="wot"):
                stored += 1
                await server.subs.fanout(ev)
        if oldest >= until:
            break  # no older events found → done
        until = oldest - 1
        if stored >= max_total:
            break
        if pace > 0:
            await asyncio.sleep(pace)
    logger.info("[nostr-relay] backfilled %d events for %s…", stored, pubkey[:12])
    return stored


async def backfill_ancestors(store, server, upstream, events, max_ancestors: int,
                             direct: bool = False) -> int:
    """Walk reply-to (`e`-tag) references up to the thread root, fetching by id any event we
    don't have. Parents may be outside the WoT (stored as origin='ancestor') — the deliberate,
    bounded relaxation that keeps threads whole."""
    pending = set()
    for ev in events:
        for t in ev.get("tags", []):
            if len(t) >= 2 and t[0] == "e" and _is_evid(t[1]):
                pending.add(t[1])
    fetched = 0
    hops = 0
    while pending and fetched < max_ancestors and hops < max_ancestors:
        missing = []
        for eid in pending:
            if not await store.has_event(eid):
                missing.append(eid)
        if not missing:
            break
        missing = missing[: max_ancestors - fetched]
        try:
            anc = await _relay.query(upstream, [{"ids": missing}], direct=direct)
        except Exception:
            break
        nxt = set()
        for ev in anc:
            if not _is_evid(ev.get("id")) or not verify_event(ev):
                continue
            if await store.add_event(ev, origin="ancestor"):
                fetched += 1
                await server.subs.fanout(ev)
                for t in ev.get("tags", []):
                    if len(t) >= 2 and t[0] == "e" and _is_evid(t[1]):
                        nxt.add(t[1])
        pending = nxt
        hops += 1
    if fetched:
        logger.info("[nostr-relay] backfilled %d ancestor(s) for thread completion", fetched)
    return fetched


async def fetch_missing_profiles(store, upstream, batch: int, limit: int, pace: float = 1.0,
                                 direct: bool = False) -> int:
    """Pull kind-0 metadata for WoT members we have no profile for, so clients render
    names/avatars. Batched by author to respect relay filter caps, paced to be polite."""
    missing = await store.wot_missing_profiles()
    if not missing:
        return 0
    missing = missing[:limit]
    stored = 0
    for i in range(0, len(missing), batch):
        if i > 0 and pace > 0:
            await asyncio.sleep(pace)
        chunk = missing[i:i + batch]
        try:
            evs = await _relay.query(upstream, [{"authors": chunk, "kinds": [0]}], direct=direct)
        except Exception:
            continue
        latest: dict = {}
        for ev in evs:
            a = ev.get("pubkey")
            if a and ev.get("created_at", 0) >= latest.get(a, {}).get("created_at", -1):
                latest[a] = ev
        for ev in latest.values():
            if verify_event(ev) and await store.add_event(ev, origin="wot"):
                stored += 1
    if stored:
        logger.info("[nostr-relay] fetched %d missing profile(s)", stored)
    return stored
