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
from .bridges import reveals_blocked_bridge, author_on_blocked_bridge, is_bridged_post

logger = logging.getLogger(__name__)


def _is_evid(x) -> bool:
    return isinstance(x, str) and len(x) == 64


def _content_blocked(ev, blocked, blocked_words) -> bool:
    """True if a kind-1 note should be rejected by the language/word content filters — applied
    on EVERY ingestion path (sync, ancestor backfill) so blocked content can't sneak in as a
    backfilled reply parent."""
    if int(ev.get("kind", 1)) != 1:
        return False
    content = ev.get("content", "")
    return bool((blocked and blocked_language(content, blocked)) or
                (blocked_words and blocked_word(content, blocked_words)))


async def sync_tick(store, gate, server, upstream, cfg) -> int:
    """One windowed sync pass over the WoT author set. Returns count of new events."""
    members = sorted(gate.members())   # stable order so the rotating offset is meaningful
    n_members = len(members)
    if not members:
        return 0
    now = int(time.time())
    overlap = cfg["overlap_sec"]
    kinds = cfg["ingest_kinds"]
    batch = cfg["author_batch"]
    blocked = cfg.get("blocked_langs")
    blocked_words = cfg.get("blocked_words")
    blocked_relays = cfg.get("blocked_relays")
    block_bridged = cfg.get("block_bridged")
    pace = cfg.get("request_pace_sec", 1.0)
    direct = cfg.get("direct", False)
    deadline = time.monotonic() + cfg.get("sync_budget_sec", cfg.get("budget_sec", 100))

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
    # First run (no floor yet): reach back `backfill_sec` (default 48h) so a fresh relay pulls
    # real history and the feed isn't empty. After the first full cycle the floor advances and
    # sync becomes incremental.
    floor = await _kv_int("sync_floor", 0)
    if floor <= 0:
        floor = now - cfg.get("backfill_sec", 48 * 3600)
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
        # Bulk: one existence query for the whole batch, then verify only the new ones and
        # insert them in a single transaction. This replaces thousands of per-event DB
        # round-trips (the backfill bottleneck) with two calls per upstream query.
        ids = [ev["id"] for ev in evs if _is_evid(ev.get("id"))]
        existing = await store.filter_existing(ids)
        to_store = []
        for ev in evs:
            eid = ev.get("id")
            if not _is_evid(eid) or eid in existing:
                continue
            # VERIFY FIRST. Everything below reads ev["pubkey"]/["content"]/["tags"] and can mark an
            # identity as bridged — and mark_bridged_identity blocks even WoT members and primes their
            # stored events for purge. Acting on an UNVERIFIED event let a hostile/compromised upstream
            # relay hand us a forged, unsigned event carrying a victim's pubkey and permanently block
            # them. server.py already verifies before this same check; this path didn't.
            if not verify_event(ev):
                continue
            # Learn + drop bridge accounts (mostr.pub etc.) so the gate rejects everything they post.
            # A kind-0 nip05 on the bridge domain marks the account even when it's a WoT member
            # (DomainPolicy); weaker hints stay member-exempt (handled inside mark_bridged).
            if blocked_relays and reveals_blocked_bridge(ev, blocked_relays):
                if author_on_blocked_bridge(ev, blocked_relays):
                    gate.mark_bridged_identity(ev.get("pubkey", ""))
                else:
                    gate.mark_bridged(ev.get("pubkey", ""))
                continue
            # Opt-in: drop any bridged (NIP-48 proxy) post, whatever bridge relayed it. Operators
            # are exempt (their own cross-posts are first-party).
            if block_bridged and is_bridged_post(ev) and not gate.is_operator(ev.get("pubkey", "")):
                continue
            if not gate.is_member(ev.get("pubkey", "")):
                continue
            if int(ev.get("kind", 1)) == 1:
                _content = ev.get("content", "")
                if not _content.strip():
                    continue   # empty note — spam/noise
                if (blocked and blocked_language(_content, blocked)) or \
                        (blocked_words and blocked_word(_content, blocked_words)):
                    continue
            to_store.append(ev)
        if to_store:
            await store.add_events_bulk(to_store, origin="wot")
            new_events.extend(to_store)
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
        server.subs.fanout(ev, server._send)

    if cfg.get("fetch_ancestors", True) and new_events:
        try:
            await backfill_ancestors(store, server, upstream, new_events,
                                     cfg.get("max_ancestors", 20), direct,
                                     blocked=blocked, blocked_words=blocked_words, gate=gate,
                                     block_bridged=block_bridged)
        except Exception as e:
            logger.warning("[nostr-relay] ancestor backfill failed: %s", e)

    try:
        await fetch_lookup_metadata(store, upstream, batch, cfg.get("profile_limit", 1500),
                                    pace, direct, gate=gate, blocked_relays=blocked_relays)
    except Exception as e:
        logger.warning("[nostr-relay] metadata fetch failed: %s", e)

    logger.info("[nostr-relay] sync tick: %d new (window %ds, %d/%d members scanned)",
                len(new_events), now - since, scanned, len(members))
    return len(new_events)


async def _backfill_filter(store, server, upstream, base_filter: dict, *, direct: bool,
                           pace: float, max_total: int, max_pages: int,
                           require_author: str | None) -> int:
    """Page a filter back in time with `until`, storing matching events. `require_author`
    restricts to that author (for own-posts); None accepts any author (for received DMs,
    whose gift-wrap author is a random key)."""
    until = int(time.time())
    stored = 0
    for _ in range(max_pages):
        try:
            evs = await _relay.query(upstream, [dict(base_filter, until=until, limit=200)],
                                     direct=direct)
        except Exception as e:
            logger.warning("[nostr-relay] backfill query failed: %s", e)
            break
        if not evs:
            break
        oldest = until
        for ev in evs:
            if require_author and ev.get("pubkey") != require_author:
                continue
            oldest = min(oldest, int(ev.get("created_at", until)))
            if not _is_evid(ev.get("id")) or not verify_event(ev):
                continue
            if await store.has_event(ev["id"]):
                continue
            if await store.add_event(ev, origin="wot"):
                stored += 1
                server.subs.fanout(ev, server._send)
        if oldest >= until:
            break  # no older events found → done
        until = oldest - 1
        if stored >= max_total:
            break
        if pace > 0:
            await asyncio.sleep(pace)
    return stored


# The user's own encrypted libraries: kind 30078 carries Notes, the password vault, the calendar,
# the addressbook, Budget and the files index, and 30024/30403 are unpublished article/listing
# drafts. They are restored from the PRIVATE mirror relays only — see backfill_author.
_PRIVATE_LIB_KINDS = [30024, 30078, 30403]


async def backfill_author(store, server, upstream, pubkey: str, *, direct: bool = False,
                          kinds=None, pace: float = 1.0, max_total: int = 20000,
                          max_pages: int = 200, private_relays=None) -> int:
    """Backfill a user's FULL Nostr history into the store: everything they AUTHORED (notes,
    articles, reposts, reactions, comments, profile, contacts, relay list), the private
    DMs ADDRESSED to them (NIP-17 gift wraps + legacy kind-4), and — from the operator's private
    mirror relays — their own encrypted libraries. Writes straight to the store (origin='wot'), so
    it does NOT go through the WS write path and is NOT re-broadcast."""
    # profile, notes, contacts, reposts, reactions, comments, relay list, long-form articles,
    # NIP-53 live events (30311, Streams), NIP-35 torrents (2003/2004), NIP-34 repos (30617) — so a
    # follow / WoT refresh pulls a torrent-poster's FULL back-catalog, not just recent firehose hits.
    kinds = kinds or [0, 1, 3, 6, 7, 1111, 2003, 2004, 10002, 10050, 30023, 30311, 30617]
    logger.info("[nostr-relay] sync started for %s…", pubkey[:12])
    common = dict(direct=direct, pace=pace, max_total=max_total, max_pages=max_pages)
    stored = await _backfill_filter(store, server, upstream,
                                    {"authors": [pubkey], "kinds": kinds},
                                    require_author=pubkey, **common)
    # Private messages addressed to the user (gift-wrap author is random → match by #p, any author).
    stored += await _backfill_filter(store, server, upstream,
                                     {"#p": [pubkey], "kinds": [1059, 4]},
                                     require_author=None, **common)
    # The encrypted libraries — Notes, the vault, the calendar, contacts, Budget, drafts.
    #
    # A SECOND pass against a DIFFERENT relay set, and both halves of that are the point. These
    # events are deliberately withheld from the public upstreams (_broadcastable in server.py), so
    # asking `upstream` for them returns nothing however many kinds the filter names — which is why
    # "sync my data" restored a user's posts and none of their notes, passwords or calendar. The only
    # other copies are on the relays the operator named in `private_relays`, so that is where this
    # asks. Blank (the default) → normalize_relays gives [], query returns [], and this costs one
    # no-op call: there is genuinely no second copy to restore from, and inventing one by reaching
    # for the public set would publish the very metadata trail the private list exists to contain.
    #
    # Deliberately NOT added to the public pass above: with `backup_datastore` on, kind 30078 also
    # carries this node's OWN config upstream (pcai:setting:/user:/bot:), and a per-user "sync my
    # data" button must not be able to pull another node's settings into this one's store.
    if private_relays:
        stored += await _backfill_filter(store, server, private_relays,
                                         {"authors": [pubkey], "kinds": _PRIVATE_LIB_KINDS},
                                         require_author=pubkey, **common)
    logger.info("[nostr-relay] backfilled %d events for %s…", stored, pubkey[:12])
    return stored


async def backfill_ancestors(store, server, upstream, events, max_ancestors: int,
                             direct: bool = False, blocked=None, blocked_words=None, gate=None,
                             block_bridged=False) -> int:
    """Walk reply-to (`e`-tag) references up to the thread root, fetching by id any event we
    don't have. Parents may be outside the WoT (stored as origin='ancestor') — the deliberate,
    bounded relaxation that keeps threads whole. Still honours the language/word content
    filters: a blocked-language/word parent is NOT stored (the block wins over completeness)."""
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
            # A parent may be outside the WoT (that's the point), but NEVER store a blocked
            # author's event — otherwise blocklisted spam leaks in as a thread ancestor.
            if gate is not None and gate.is_blocked(ev.get("pubkey", "")):
                continue
            if block_bridged and is_bridged_post(ev) and not (gate is not None and gate.is_operator(ev.get("pubkey", ""))):
                continue
            if int(ev.get("kind", 1)) == 1 and not (ev.get("content") or "").strip():
                continue   # empty note — don't backfill spam as a thread ancestor
            if _content_blocked(ev, blocked, blocked_words):
                continue
            if await store.add_event(ev, origin="ancestor"):
                fetched += 1
                server.subs.fanout(ev, server._send)
                for t in ev.get("tags", []):
                    if len(t) >= 2 and t[0] == "e" and _is_evid(t[1]):
                        nxt.add(t[1])
        pending = nxt
        hops += 1
    if fetched:
        logger.info("[nostr-relay] backfilled %d ancestor(s) for thread completion", fetched)
    return fetched


# Lookup-relay metadata kinds: profile (NIP-01), contact list (NIP-02), relay list (NIP-65).
_LOOKUP_KINDS = [0, 3, 10002, 10050]


async def fetch_lookup_metadata(store, upstream, batch: int, limit: int, pace: float = 1.0,
                                direct: bool = False, *, gate=None, blocked_relays=None) -> int:
    """Pull lookup metadata (kind-0 profile, kind-3 contacts, kind-10002 relay list) for WoT
    members that lack it, so clients can use this relay to resolve who-is-who and where each
    member posts (the outbox / NIP-65 lookup-relay role). Batched + paced; replaceable events
    keep only the newest. These authors are WoT members, so they pass the gate."""
    missing = await store.wot_missing_metadata()
    if not missing:
        return 0
    missing = missing[:limit]
    stored = 0
    for i in range(0, len(missing), batch):
        if i > 0 and pace > 0:
            await asyncio.sleep(pace)
        chunk = missing[i:i + batch]
        try:
            evs = await _relay.query(upstream, [{"authors": chunk, "kinds": _LOOKUP_KINDS}],
                                     direct=direct)
        except Exception:
            continue
        # Newest per (author, kind) — these are all replaceable.
        latest: dict = {}
        for ev in evs:
            key = (ev.get("pubkey"), ev.get("kind"))
            if key[0] is not None and ev.get("created_at", 0) >= latest.get(key, {}).get("created_at", -1):
                latest[key] = ev
        for ev in latest.values():
            if blocked_relays and reveals_blocked_bridge(ev, blocked_relays):
                if gate is not None:
                    if author_on_blocked_bridge(ev, blocked_relays):
                        gate.mark_bridged_identity(ev.get("pubkey", ""))   # kind-0 nip05 → block even members
                    else:
                        gate.mark_bridged(ev.get("pubkey", ""))
                continue   # never store a bridge account's profile/relay-list
            if verify_event(ev) and await store.add_event(ev, origin="wot"):
                stored += 1
    if stored:
        logger.info("[nostr-relay] fetched %d lookup-metadata event(s) (profiles/contacts/relay-lists)",
                    stored)
    return stored
