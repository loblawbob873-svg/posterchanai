"""Web of Trust gate + depth-1 builder.

The trust set = the configured seed pubkeys + everyone they follow (from each seed's latest
kind-3 contact list) + the operator's own keys (bots/linked users), which are *always*
trusted so they can publish through the relay even if nobody follows them. `is_member()` is
the single chokepoint both the WS write path (server.py) and the ingestion poller (ingest.py)
consult, so non-WoT events can never be stored.
"""

import time
import logging

from app.services.nostr import relay as _relay

logger = logging.getLogger(__name__)


class WotGate:
    def __init__(self):
        self._members: frozenset = frozenset()
        self._operator: frozenset = frozenset()
        self.built_at: float = 0.0

    def set_operator(self, operator_hex) -> None:
        self._operator = frozenset(operator_hex or [])

    def is_member(self, pubkey: str) -> bool:
        return bool(pubkey) and (pubkey in self._members or pubkey in self._operator)

    def members(self) -> frozenset:
        return self._members | self._operator

    async def build(self, store, upstream_relays, seeds_hex, depth: int = 1) -> int:
        """Resolve the depth-1 follow set from seeds' kind-3 lists, persist it, and swap it in.
        Falls back to keeping the previous set on a total upstream failure."""
        seeds = [s for s in (seeds_hex or []) if s]
        members = set(seeds)
        if seeds and depth >= 1:
            try:
                events = await _relay.query(
                    upstream_relays, [{"authors": seeds, "kinds": [3], "limit": len(seeds) * 2}])
                # Newest kind-3 per author, then collect p-tagged follows.
                latest: dict = {}
                for ev in events:
                    a = ev.get("pubkey")
                    if a and ev.get("created_at", 0) >= latest.get(a, {}).get("created_at", -1):
                        latest[a] = ev
                for ev in latest.values():
                    for t in ev.get("tags", []):
                        if len(t) >= 2 and t[0] == "p" and isinstance(t[1], str) and len(t[1]) == 64:
                            members.add(t[1])
            except Exception as e:
                logger.warning("[nostr-relay] WoT build query failed: %s", e)
                if not self._members:
                    # No prior set and upstream failed: at least trust the seeds + operator.
                    self._members = frozenset(seeds)
                    self.built_at = time.time()
                return len(self.members())

        self._members = frozenset(members)
        self.built_at = time.time()
        try:
            await store.wot_replace(list(members), list(self._operator))
        except Exception as e:
            logger.warning("[nostr-relay] WoT persist failed: %s", e)
        total = len(self.members())
        logger.info("[nostr-relay] WoT rebuilt: %d members (%d seeds)", total, len(seeds))
        return total

    async def load_from_store(self, store) -> int:
        """Warm the in-memory set from the persisted snapshot (used on startup before the
        first live build, so the gate works immediately after a restart)."""
        try:
            self._members = frozenset(await store.wot_members())
        except Exception:
            self._members = frozenset()
        return len(self.members())
