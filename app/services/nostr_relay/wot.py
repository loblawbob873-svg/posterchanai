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
        self.built_at: float = 0.0

    def set_operator(self, operator_hex) -> None:
        self._operator = frozenset(operator_hex or [])

    def set_blocked(self, blocked_hex) -> None:
        self._blocked = frozenset(blocked_hex or [])

    def is_blocked(self, pubkey: str) -> bool:
        return bool(pubkey) and pubkey in self._blocked

    def is_member(self, pubkey: str) -> bool:
        if not pubkey or pubkey in self._blocked:
            return False  # explicit denylist overrides everything, even operator
        return pubkey in self._members or pubkey in self._operator

    def is_operator(self, pubkey: str) -> bool:
        """A relay user (linked account/bot). DMs addressed to one are accepted as inbox."""
        return bool(pubkey) and pubkey in self._operator

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
                    max_members: int = 0) -> int:
        """Build the WoT and swap it in. Depth 1 = seeds + their follows; depth 2 also adds
        friends-of-friends (followed by >= `min_followers` of your follows), capped at
        `max_members` (highest-occurrence kept). Falls back to the prior set on total failure."""
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
        if depth >= 2 and follows1:
            fof_counter = await self._follows_counter(upstream_relays, follows1, direct, batch, pace)
            fof = {pk for pk, c in fof_counter.items() if c >= min_followers}
            members |= fof
            if max_members and len(members) > max_members:
                # Keep seeds + direct follows always; fill remaining room with the most-followed FoF.
                core = set(seeds) | follows1
                room = max(0, max_members - len(core))
                top = [pk for pk, _ in fof_counter.most_common() if pk not in core][:room]
                members = core | set(top)
            logger.info("[nostr-relay] WoT depth-2: +%d friends-of-friends (>=%d followers)",
                        len(members) - len(seeds) - len(follows1), min_followers)

        self._members = frozenset(members)
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
