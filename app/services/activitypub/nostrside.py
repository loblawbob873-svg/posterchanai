"""Reaching a Nostr user who does NOT live on this relay.

A local user reads this node's relay, so storing an event here is delivering it. Anybody else (every
Nostr user is reachable in `everyone` mode) reads THEIR relays: a fediverse reply or mention of them
stored only here would never be seen, and a DM to them never arrive. So those events are also
published to where that person reads -- their NIP-65 read relays for mentions and replies, their
kind-10050 DM relays for DMs (NIP-17) -- together with the sender puppet's profile and DM-relay
list, so their client can name who wrote and knows where to write back.
"""
from __future__ import annotations

import logging

from app.services import settings_store

logger = logging.getLogger(__name__)

MAX_RELAYS = 6


async def _clean(urls) -> list:
    """Public `wss://` relays only. These addresses come from relay lists ANYBODY can publish, and
    a relay on the LAN is dialled directly (around the proxy), so an unchecked list would let a
    stranger point this server at its own network: every host is resolved and a private,
    loopback or link-local address -- or a `.lan`/`.local` name, or an odd port -- is refused."""
    import asyncio as _asyncio
    from urllib.parse import urlparse
    from app.services.rss_service import is_safe_host
    out = []
    for u in urls:
        u = (u or "").strip()
        if not u.startswith("wss://") or u in out:
            continue
        p = urlparse(u)
        if not p.hostname or (p.port not in (None, 443)):
            continue
        if not await _asyncio.to_thread(is_safe_host, "https://" + p.hostname):
            continue
        out.append(u)
        if len(out) >= MAX_RELAYS:
            break
    return out


async def inbox_relays(pubkey: str, *, dm: bool) -> list:
    """Where `pubkey` reads: kind-10050 relays for a DM (NIP-17), else NIP-65 read relays; the
    defaults when they have published neither."""
    from app.services.fedi_bridge_identity import query_one
    port = settings_store._port()
    if dm:
        ok, ev = await query_one(port, {"kinds": [10050], "authors": [pubkey], "limit": 1})
        if not ok:
            return []                      # could not ask: not a reason to guess somewhere else
        urls = [t[1] for t in (ev or {}).get("tags", []) if len(t) > 1 and t[0] == "relay"]
        if urls:
            return await _clean(urls)
    ok, ev = await query_one(port, {"kinds": [10002], "authors": [pubkey], "limit": 1})
    if not ok:
        return []
    urls = [t[1] for t in (ev or {}).get("tags", [])
            if len(t) > 1 and t[0] == "r" and (len(t) < 3 or t[2] in ("read", ""))]
    if urls:
        return await _clean(urls)
    from app.services.nostr.nostr_service import DEFAULT_RELAYS
    return (await _clean(DEFAULT_RELAYS))[:4]


_pushes: dict = {}                 # recipient -> [times]
PUSHES_PER_HOUR = 120


def _push_ok(pubkey: str) -> bool:
    """A per-recipient budget: whatever the fediverse sends, one Nostr user's relays get at most
    this many pushes an hour from here."""
    import time as _time
    now = _time.monotonic()
    times = [t for t in _pushes.get(pubkey, []) if now - t < 3600]
    if len(times) >= PUSHES_PER_HOUR:
        _pushes[pubkey] = times
        return False
    times.append(now)
    _pushes[pubkey] = times
    if len(_pushes) > 20000:
        _pushes.clear()
    return True


async def puppet_identity_events(puppet_pubkey: str) -> list:
    """The sender puppet's profile and DM-relay list, as stored here -- sent along so the recipient's
    client can show a name and knows where a reply goes."""
    from app.services.fedi_bridge_identity import query_one
    out = []
    for kind in (0, 10050):
        ok, ev = await query_one(settings_store._port(), {"kinds": [kind], "authors": [puppet_pubkey], "limit": 1})
        if ok and ev:
            out.append(ev)
    return out


async def deliver(pubkey: str, events: list, *, dm: bool, force: bool = False) -> int:
    """Publish `events` where `pubkey` reads. Returns how many relays took the last (main) event.
    A local user reads this relay already, so nothing is sent for them unless `force` (this relay
    would not take the event)."""
    from app.services.activitypub import actors
    from app.services.nostr import relay
    if not events or (actors.is_actor(pubkey) and not force):
        return 0
    if not _push_ok(pubkey):
        logger.info("[activitypub] not pushing to %s: hourly limit reached", pubkey[:12])
        return 0
    relays = await inbox_relays(pubkey, dm=dm)
    took = 0
    for ev in events:
        try:
            took = len(await relay.publish_to(relays, ev))
        except Exception as e:
            logger.info("[activitypub] could not reach %s's relays: %s", pubkey[:12], type(e).__name__)
            took = 0
    return took
