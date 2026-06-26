"""Web of Trust gate + depth-1 builder.

The trust set = the configured seed pubkeys + everyone they follow (from each seed's latest
kind-3 contact list) + the operator's own keys (bots/linked users), which are *always*
trusted so they can publish through the relay even if nobody follows them. `is_member()` is
the single chokepoint both the WS write path (server.py) and the ingestion poller (ingest.py)
consult, so non-WoT events can never be stored.
"""

import time
import asyncio
import logging
from collections import Counter

from app.services.nostr import relay as _relay

logger = logging.getLogger(__name__)


class WotGate:
    def __init__(self):
        self._members: frozenset = frozenset()
        self._operator: frozenset = frozenset()
        self._blocked: frozenset = frozenset()
        # Fediverse-bridge "puppet" secret: the app mirrors the global fediverse timeline through
        # deterministic per-fedi-user keys derived from this secret (see nostr.bridge_keys). A puppet
        # event self-validates — it carries the actor URI it derives from — so there is NO allowlist:
        # the relay re-derives the pubkey and accepts only when it matches the (already verified)
        # signer. Puppets are NOT web-of-trust members (the upstream sync/firehose never accept them);
        # the exemption applies ONLY on the local WS publish path, for a fixed kind set.
        self._bridge_secret: bytes | None = None
        # Accounts detected as living on a blocked bridge relay (mostr.pub etc.). Like _blocked,
        # but grown at runtime as a bridge account's profile/relay-list flows through, and reseeded
        # from a store scan on (re)load — kept separate so the daily WoT rebuild can't undo it.
        self._bridged: set = set()
        self.built_at: float = 0.0
        self.last_build_partial: bool = False   # last crawl looked partial (kept cache) → don't stamp it

    def add_members(self, pubkeys) -> None:
        """Add members immediately (e.g. a freshly-followed signup) without a full rebuild."""
        extra = {p for p in (pubkeys or []) if p}
        if extra:
            self._members = self._members | frozenset(extra)

    def set_operator(self, operator_hex) -> None:
        self._operator = frozenset(operator_hex or [])

    def set_blocked(self, blocked_hex) -> None:
        self._blocked = frozenset(blocked_hex or [])

    def set_bridge_secret(self, secret) -> None:
        self._bridge_secret = secret or None

    def set_bridged(self, bridged_hex) -> None:
        self._bridged = set(bridged_hex or [])

    def add_bridged(self, pubkeys) -> None:
        # Never bridge-block a WoT member (the trust set: follows + operators). They may legitimately
        # cross-post from the fediverse, so a synced post can carry a proxy/relay hint to a blocked
        # bridge — that must not get the whole trusted account marked as a bridge and purged.
        self._bridged |= {p for p in (pubkeys or []) if p and p not in self._members and p not in self._operator}

    def mark_bridged(self, pubkey: str) -> None:
        if pubkey and pubkey not in self._members and pubkey not in self._operator:
            self._bridged.add(pubkey)

    def mark_bridged_identity(self, pubkey: str) -> None:
        """DomainPolicy mark: the account's OWN kind-0 nip05 is on a blocked bridge domain. Unlike
        mark_bridged() this blocks even a WoT *member* — a mirror account (mostr.pub, momostr.pink,
        brid.gy) is almost always followed by someone, so member-exemption made the blocklist a
        no-op. Still spares operators / registered local users (the preserve set), so it can never
        delete a real first-party account's posts."""
        if pubkey and pubkey not in self._operator:
            self._bridged.add(pubkey)

    def add_bridged_identity(self, pubkeys) -> None:
        self._bridged |= {p for p in (pubkeys or []) if p and p not in self._operator}

    def operators(self) -> frozenset:
        return self._operator

    def is_blocked(self, pubkey: str) -> bool:
        return bool(pubkey) and (pubkey in self._blocked or pubkey in self._bridged)

    def is_member(self, pubkey: str) -> bool:
        if not pubkey or pubkey in self._blocked or pubkey in self._bridged:
            return False  # explicit denylist / blocked bridge overrides everything, even operator
        return pubkey in self._members or pubkey in self._operator

    def is_operator(self, pubkey: str) -> bool:
        """A relay user (linked account/bot). DMs addressed to one are accepted as inbox."""
        return bool(pubkey) and pubkey in self._operator

    def is_puppet_event(self, ev: dict) -> bool:
        """A fediverse-bridge puppet event (self-validating — see nostr.bridge_keys). Gate-exempt on
        the local WS publish path ONLY (a fixed kind set), never a WoT member, so it can't widen the
        upstream-facing trust graph."""
        from app.services.nostr import bridge_keys
        return bridge_keys.is_puppet_event(self._bridge_secret, ev)

    def members(self) -> frozenset:
        return self._members | self._operator

    async def _follows_counter(self, upstream, authors, direct, batch, pace) -> Counter:
        """Fetch kind-3 for `authors` (batched + paced) and count how many of them follow
        each pubkey. Returns a Counter(followed_pubkey -> #followers within `authors`)."""
        counter: Counter = Counter()
        authors = list(authors)
        for i in range(0, len(authors), batch):
            if i > 0 and pace > 0:
                await asyncio.sleep(pace)
            chunk = authors[i:i + batch]
            try:
                events = await _relay.query(upstream, [{"authors": chunk, "kinds": [3]}], direct=direct)
            except Exception as e:
                logger.warning("[nostr-relay] WoT kind-3 query failed: %s", e)
                continue
            latest: dict = {}
            for ev in events:
                a = ev.get("pubkey")
                if a and ev.get("created_at", 0) >= latest.get(a, {}).get("created_at", -1):
                    latest[a] = ev
            for ev in latest.values():
                for t in ev.get("tags", []):
                    if len(t) >= 2 and t[0] == "p" and isinstance(t[1], str) and len(t[1]) == 64:
                        counter[t[1]] += 1
        return counter

    async def build(self, store, upstream_relays, seeds_hex, depth: int = 1, direct: bool = False,
                    *, batch: int = 200, pace: float = 1.0, min_followers: int = 2,
                    max_members: int = 0, min_keep_ratio: float = 0.85,
                    depth3_crawl_max: int = 2500) -> int:
        """Build the WoT and swap it in. Depth 1 = seeds + their follows; depth 2 also adds
        friends-of-friends (followed by >= `min_followers` of your follows); depth 3 also adds
        friends-of-friends-of-friends (followed by >= `min_followers` of your FoF), capped at
        `max_members` (highest-occurrence kept). Falls back to the prior set on total failure, and
        KEEPS the cached set if a crawl resolves < `min_keep_ratio` of the cached size (partial crawl
        protection — an upstream timeout must not shrink/degrade the trusted set)."""
        self.last_build_partial = False
        seeds = [s for s in (seeds_hex or []) if s]
        if not seeds or depth < 1:
            self._members = frozenset(seeds)
            self.built_at = time.time()
            await self._persist(store, self._members)
            return len(self.members())

        # Depth 1: seeds' direct follows.
        d1_counter = await self._follows_counter(upstream_relays, seeds, direct, batch, pace)
        follows1 = set(d1_counter.keys())
        if not follows1 and not self._members:
            # Total upstream failure with no prior set: at least trust the seeds.
            self._members = frozenset(seeds)
            self.built_at = time.time()
            return len(self.members())

        members = set(seeds) | follows1

        # Depth 2: friends-of-friends, pruned by how many of your follows also follow them.
        fof = set()
        if depth >= 2 and follows1:
            fof_counter = await self._follows_counter(upstream_relays, follows1, direct, batch, pace)
            fof = {pk for pk, c in fof_counter.items() if c >= min_followers}
            members |= fof
            logger.info("[nostr-relay] WoT depth-2: +%d friends-of-friends (>=%d followers)",
                        len(members) - len(seeds) - len(follows1), min_followers)

        # Depth 3: friends-of-friends-of-friends, pruned the same way against the FoF tier.
        fofof_counter: Counter = Counter()
        if depth >= 3 and fof:
            # BOUND THE CRAWL INPUT. The FoF tier can be tens of thousands of pubkeys (35k seen in
            # prod); crawling kind-3 for all of them fans out a federation storm and pegs the box.
            # Only crawl the most-trusted FoF — the top `depth3_crawl_max` by how many of your follows
            # follow them — so the upstream fan-out is bounded regardless of how big the FoF tier got.
            if depth3_crawl_max and len(fof) > depth3_crawl_max:
                crawl_seeds = [pk for pk, _ in fof_counter.most_common() if pk in fof][:depth3_crawl_max]
                logger.info("[nostr-relay] WoT depth-3: crawling top %d of %d FoF (bounded)",
                            len(crawl_seeds), len(fof))
            else:
                crawl_seeds = list(fof)
            fofof_counter = await self._follows_counter(upstream_relays, crawl_seeds, direct, batch, pace)
            before = len(members)
            fofof = {pk for pk, c in fofof_counter.items() if c >= min_followers}
            members |= fofof
            logger.info("[nostr-relay] WoT depth-3: +%d friends-of-friends-of-friends (>=%d followers)",
                        len(members) - before, min_followers)

        if depth >= 2 and max_members and len(members) > max_members:
            # Keep seeds + direct follows always; fill remaining room with the most-followed outer
            # members (FoF first, then FoFoF) by occurrence across the crawled tiers.
            core = set(seeds) | follows1
            room = max(0, max_members - len(core))
            outer = Counter()
            if depth >= 2:
                outer.update({pk: c for pk, c in fof_counter.items() if pk not in core})
            if depth >= 3:
                for pk, c in fofof_counter.items():
                    if pk not in core:
                        outer[pk] += c
            top = [pk for pk, _ in outer.most_common()][:room]
            members = core | set(top)

        new_members = frozenset(members)
        prior = self._members
        # STRONG CACHE: a crawl that resolves far fewer members than the cached set is almost always a
        # PARTIAL crawl (an upstream relay timed out), not thousands of real unfollows. Keep the cached
        # set and flag the build partial so the caller won't refresh the daily stamp (it stays due and
        # retries next cycle) — degrading a 40k-member gate to 31k on a flaky crawl is the bug we saw.
        if prior and len(new_members) < min_keep_ratio * len(prior):
            self.last_build_partial = True
            logger.warning("[nostr-relay] WoT crawl got %d members vs cached %d (< %d%%) — looks partial, "
                           "KEEPING the cached set (not degrading)",
                           len(new_members), len(prior), int(min_keep_ratio * 100))
            return len(self.members())
        self._members = new_members
        self.built_at = time.time()
        await self._persist(store, self._members)
        total = len(self.members())
        logger.info("[nostr-relay] WoT rebuilt: %d members (%d seeds, %d direct, depth=%d)",
                    total, len(seeds), len(follows1), depth)
        return total

    async def _persist(self, store, members) -> None:
        try:
            await store.wot_replace(list(members), list(self._operator))
        except Exception as e:
            logger.warning("[nostr-relay] WoT persist failed: %s", e)

    async def load_from_store(self, store) -> int:
        """Warm the in-memory set from the persisted snapshot (used on startup before the
        first live build, so the gate works immediately after a restart)."""
        try:
            self._members = frozenset(await store.wot_members())
        except Exception:
            self._members = frozenset()
        return len(self.members())
