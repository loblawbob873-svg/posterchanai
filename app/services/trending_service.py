"""Trending -> Hot, ranked ONCE on the server instead of on every phone.

The client used to pull up to 3,000 reaction/repost events (kinds 6/7, last 24 h) and tally them itself: 2.2 MB
per visit, measured 2026-10-16. On a phone over mobile data that outran the client's query timer and Trending
said "Couldn't reach your relays" while the relay had answered in 0.13 s ("Vyram's perf video"). The answer is
the same for everybody, so it is computed here from this node's relay, cached per window for a minute, and the
phone gets the ranked ids (~16 KB). The tally is the client's, verbatim: the LAST `e` tag is the target (NIP-25),
a repost marks the row with a repost icon. "Could not ask" raises -- the route answers 503, never an empty list.
"""
from __future__ import annotations

import asyncio
import time

WINDOW_MIN, WINDOW_MAX = 24 * 3600, 30 * 24 * 3600
QUERY_LIMIT = 3000              # the client's TR_QUERY_LIMIT: same candidates as before
ROWS = 300
TTL = 60.0
_cache: dict = {}               # window -> (monotonic time, rows)
_locks: dict = {}


def window_of(seconds) -> int:
    """The client's ladder: 24 h doubling up to 30 d. Anything else snaps to it (no per-request cache keys)."""
    try:
        want = int(seconds)
    except (TypeError, ValueError):
        want = WINDOW_MIN
    w = WINDOW_MIN
    while w < want and w < WINDOW_MAX:
        w = min(w * 2, WINDOW_MAX)
    return w


def rank(events) -> list:
    tally, icon = {}, {}
    for e in events:
        es = [t for t in (e.get("tags") or []) if len(t) >= 2 and t[0] == "e"]
        if not es:
            continue
        nid = es[-1][1]
        tally[nid] = tally.get(nid, 0) + 1
        if e.get("kind") == 6:
            icon[nid] = "\U0001F501"
        elif nid not in icon:
            icon[nid] = "❤️"
    rows = sorted(tally.items(), key=lambda kv: -kv[1])[:ROWS]
    return [[nid, n, icon.get(nid, "❤️")] for nid, n in rows]


async def hot(seconds) -> dict:
    from app.services import nostr_store, settings_store
    w = window_of(seconds)
    hit = _cache.get(w)
    if hit and time.monotonic() - hit[0] < TTL:
        return {"window": w, "rows": hit[1]}
    lock = _locks.setdefault(w, asyncio.Lock())
    async with lock:                                   # one computation per window, however many phones ask
        hit = _cache.get(w)
        if hit and time.monotonic() - hit[0] < TTL:
            return {"window": w, "rows": hit[1]}
        since = int(time.time()) - w
        evs = await nostr_store._ws_query(settings_store._port(),
                                          [{"kinds": [6, 7], "since": since, "limit": QUERY_LIMIT}], strict=True)
        rows = rank(evs)
        _cache[w] = (time.monotonic(), rows)
        return {"window": w, "rows": rows}
