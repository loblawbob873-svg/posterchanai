"""Outbox: re-broadcast inbound client writes to the upstream public relays.

Only events *submitted to this relay* over its WS `EVENT` (i.e. our own bots/users who
point their relay list here) are fanned out — NOT events we ingested from upstream (those
already came from there; re-publishing would loop). Best-effort and fire-and-forget so it
never blocks the client's OK response.
"""

import logging

from app.services.nostr import relay as _relay

logger = logging.getLogger(__name__)


async def broadcast(upstream, ev: dict) -> None:
    try:
        n = await _relay.publish(upstream, ev)
        logger.info("[nostr-relay] outbox: %s broadcast to %d/%d relays",
                    ev.get("id", "")[:12], n, len(upstream))
    except Exception as e:
        logger.warning("[nostr-relay] outbox broadcast failed: %s", e)
