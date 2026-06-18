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
from .langfilter import blocked_language

logger = logging.getLogger(__name__)


def _is_evid(x) -> bool:
    return isinstance(x, str) and len(x) == 64


async def sync_tick(store, gate, server, upstream, cfg) -> int:
    """One windowed sync pass over the WoT author set. Returns count of new events."""
    members = list(gate.members())
    if not members:
        return 0
    now = int(time.time())
    try:
        cursor = int(await store.kv_get("sync_cursor") or 0)
    except (ValueError, TypeError):
        cursor = 0
    window = cfg["sync_window_sec"]
    overlap = cfg["overlap_sec"]
    # Never look back less than `window`; if we fell behind (cursor old / was down), look back
    # to the cursor instead. Overlap re-pulls boundary stragglers. Dedup makes it idempotent.
    base = min(cursor, now - window) if cursor else (now - window)
    since = max(0, base - overlap)
    until = now
    kinds = cfg["ingest_kinds"]
    batch = cfg["author_batch"]
    blocked = cfg.get("blocked_langs")
    pace = cfg.get("request_pace_sec", 1.0)
    direct = cfg.get("direct", False)
    deadline = time.monotonic() + cfg.get("budget_sec", 45)

    new_events = []
    scanned = 0
    for i in range(0, len(members), batch):
        if time.monotonic() > deadline:
            logger.info("[nostr-relay] sync budget hit (%d/%d authors); rest next tick",
                        scanned, len(members))
            break
        if i > 0 and pace > 0:
            await asyncio.sleep(pace)   # pace upstream REQs — don't blast the relays
        chunk = members[i:i + batch]
        scanned += len(chunk)
        try:
            evs = await _relay.query(
                upstream, [{"authors": chunk, "kinds": kinds, "since": since, "until": until}],
                direct=direct)
        except Exception as e:
            logger.warning("[nostr-relay] sync query failed: %s", e)
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
            if blocked and int(ev.get("kind", 1)) == 1 and \
                    blocked_language(ev.get("content", ""), blocked):
                continue
            if await store.add_event(ev, origin="wot"):
                new_events.append(ev)

    # Advance the cursor: the look-back window always re-covers recent time, so authors
    # skipped by the budget this tick are picked up next tick (no permanent gap).
    await store.kv_set("sync_cursor", str(until))

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
