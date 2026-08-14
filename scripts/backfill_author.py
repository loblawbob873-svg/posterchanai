#!/usr/bin/env python3
"""Pull ONE author's history into this node's relay, by operator request.

Why this is separate from backfill_primal.py: that one mirrors the WoT in bulk and gates every event
on `gate.is_member`, which is exactly right for the firehose and exactly wrong here. A newly followed
author is NOT in the WoT until the snapshot rebuilds, so their history — the posts carrying a webxdc
mini app, and the kind-1063 file metadata the app is attached to — cannot land, and the gallery shows
nothing with nothing to explain it.

WHAT IS AND IS NOT RELAXED. The WoT MEMBERSHIP test is skipped, deliberately, because an operator
named this pubkey on the command line: that is a stronger statement of intent than a follow graph.
Every other gate stands — the signature is verified on every event (`verify_event`), the author is
pinned to the one requested, and storage goes through the SAME `add_events_bulk` path the live ingest
uses. Nothing here can store an event this node would otherwise consider forged.

RETENTION IS THE CATCH, and it is worth knowing before running this on old content: `origin="wot"`
events are subject to the age prune for prunable kinds (kind 1/6/7/1111), so a post older than
`nostr_relay_retention_days` can be stored and then swept that night. Kinds 1063 and 4932 are NOT
prunable — a mini app's attachment metadata and its update log survive — so a webxdc app stays
playable even after its announcement post ages out. Follow the author too: that is what keeps them
arriving, where this only fetches what already happened.

    venv-unified/bin/python scripts/backfill_author.py npub1… [--days 365] [--dry-run]
"""

import argparse
import asyncio
import logging
import os
import sys
import time
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.services.nostr.bech32 import decode_any                    # noqa: E402
from app.services.nostr.event import verify_event                   # noqa: E402
from app.services.nostr import relay as _relay                      # noqa: E402
from app.services.nostr_relay.store import RelayStore               # noqa: E402
from app.services.nostr_relay.thread import _read_config            # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger("backfill-author")

# 0/10002 identify them, 1/6/7 are the feed, 1063 is a webxdc attachment's file metadata, 4932 is a
# mini app's update log, 30023 long-form. 20932 is deliberately absent: it is EPHEMERAL by kind, so
# no relay stores it and asking for it only wastes a round trip.
DEFAULT_KINDS = [0, 1, 6, 7, 1063, 4932, 10002, 30023]
_MAX_PAGES = 12


async def _fetch(upstream, author, kinds, since, direct, pace):
    """Everything from `author` in [since, now], paged backward so a relay's own limit cannot
    silently cap the window."""
    out = {}
    until = int(time.time())
    for page in range(_MAX_PAGES):
        flt = {"authors": [author], "kinds": kinds, "since": since, "until": until}
        try:
            evs = await _relay.query(upstream, [flt], direct=direct)
        except Exception as e:
            log.warning("  query failed: %s", e)
            break
        fresh = [e for e in evs if e.get("id") not in out]
        for e in evs:
            out[e.get("id")] = e
        log.info("  page %d: %d events, %d new (total %d)", page + 1, len(evs), len(fresh), len(out))
        if not fresh:
            break
        oldest = min(e.get("created_at", until) for e in evs)
        if oldest <= since:
            break
        until = oldest - 1
        if pace > 0:
            await asyncio.sleep(pace)
    return list(out.values())


async def main(who: str, days: int, dry_run: bool, relays: list, kinds: list) -> int:
    author = who.strip()
    if author.startswith("npub"):
        raw = decode_any(author)
        if not raw or len(raw) != 32:
            log.error("that npub does not decode to a 32-byte key")
            return 1
        author = raw.hex()
    if len(author) != 64 or any(c not in "0123456789abcdef" for c in author.lower()):
        log.error("not a hex pubkey: %r", author)
        return 1
    author = author.lower()

    cfg = _read_config()
    direct = cfg.get("direct", False)
    pace = cfg.get("request_pace_sec", 1.0)
    relays = relays or cfg.get("upstream") or []
    if not relays:
        log.error("no source relays configured")
        return 1
    since = int(time.time()) - days * 86400

    store = RelayStore(cfg["pg_dsn"], retention_days=cfg.get("retention_days", 30))
    store.open(asyncio.get_event_loop())
    try:
        before = len(await store.query([{"authors": [author], "limit": 5000}]))
        log.info("author %s — we already hold %d event(s)", author[:16] + "…", before)
        log.info("asking %d relay(s) for kinds=%s over %d days", len(relays), kinds, days)

        evs = await _fetch(relays, author, kinds, since, direct, pace)
        log.info("fetched %d event(s)", len(evs))
        if not evs:
            log.warning("nothing came back — the author may publish to relays this node does not "
                        "read (pass --relays to name theirs)")
            return 0

        ids = [e["id"] for e in evs if isinstance(e.get("id"), str) and len(e["id"]) == 64]
        existing = await store.filter_existing(ids)
        keep, forged, wrong = [], 0, 0
        for e in evs:
            if e.get("id") in existing:
                continue
            if (e.get("pubkey") or "").lower() != author:      # a relay may answer with more
                wrong += 1
                continue
            if not verify_event(e):
                forged += 1
                continue
            keep.append(e)

        log.info("new=%d  already-held=%d  wrong-author=%d  BAD-SIGNATURE=%d",
                 len(keep), len(evs) - len(keep) - wrong - forged, wrong, forged)
        log.info("by kind: %s", dict(Counter(e.get("kind") for e in keep).most_common()))

        if dry_run:
            log.info("[DRY-RUN] nothing stored")
            return 0
        stored = await store.add_events_bulk(keep, origin="wot") if keep else 0
        after = len(await store.query([{"authors": [author], "limit": 5000}]))
        log.info("stored=%d — this node now holds %d of their events (was %d)", stored, after, before)
        return 0
    finally:
        store.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("who", help="npub… or 64-char hex pubkey")
    ap.add_argument("--days", type=int, default=365)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--relays", default="", help="comma-separated override")
    ap.add_argument("--kinds", default="", help="comma-separated override")
    a = ap.parse_args()
    sys.exit(asyncio.run(main(
        a.who, a.days, a.dry_run,
        [r for r in a.relays.split(",") if r.strip()],
        [int(k) for k in a.kinds.split(",") if k.strip()] or DEFAULT_KINDS,
    )))
