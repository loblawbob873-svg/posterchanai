#!/usr/bin/env python3
"""One-shot backfill: pull recent WoT-member events from Primal's relay into THIS node's built-in
relay so the Nostr Stats bot has fuller numbers.

It REQs `wss://relay.primal.net` for each batch of WoT members over the past N days and feeds the
results through the SAME store path the live ingest uses (`store.add_events_bulk(..., origin="wot")`),
after a signature check + WoT-membership gate + dedup. Nothing here bypasses the trust gate or the
local-relay-only rule — it only mirrors WoT authors' own events, exactly like the firehose/sync.

Window: defaults to 29 days, deliberately INSIDE the relay's 30-day age-retention for prunable feed
kinds (kind-1/6/7/1111). Staying inside retention means the nightly prune won't immediately delete
the backfill, so no retention change is needed. Bump --days past the retention window only if you
also raise nostr_relay_retention_days (else the oldest slice ages out within ~24h).

Run on the node whose relay you want to fill (uses its Postgres DSN from _read_config):
    venv-unified/bin/python scripts/backfill_primal.py --days 29
    venv-unified/bin/python scripts/backfill_primal.py --days 29 --kinds 1,6,7,1111 --dry-run
"""

import os
import sys
import time
import asyncio
import argparse
import logging

# Run as `scripts/backfill_primal.py`: only scripts/ lands on sys.path, so add the repo root for `app`.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.services.nostr_relay.thread import _read_config
from app.services.nostr_relay.store import RelayStore
from app.services.nostr_relay.wot import WotGate
from app.services.nostr import relay as _relay
from app.services.nostr.event import verify_event

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger("backfill-primal")

# By default the backfill pulls from the NODE'S OWN configured upstream relays (Admin → Relay) —
# never a hardcoded relay, so it can't pull from a relay the operator deliberately removed. _relay
# .query fans out to all of them and unions+dedupes. NOTE: relay.primal.net's NIP-01 relay only
# serves ~the last week (its deep archive is behind a separate caching protocol), so for the older
# days the longer-retention relays in your upstream set (eden.nostr.land, nostr.wine, nos.lol, …)
# are what fill the window. Pass --relays to override (e.g. to skip dead/slow ones for speed).
# Page a busy author batch backwards in time so one REQ's server-side limit can't silently cap the
# window — stop when a page returns nothing new or we cross `since`.
_MAX_PAGES = 8


async def _fetch_batch(upstream, authors, kinds, since, direct, pace):
    """All events in [since, now] for `authors`, paged backward by `until`."""
    out = {}
    until = int(time.time())
    for _page in range(_MAX_PAGES):
        flt = {"authors": authors, "kinds": kinds, "since": since, "until": until}
        try:
            evs = await _relay.query(upstream, [flt], direct=direct)
        except Exception as e:
            log.warning("  query failed: %s", e)
            break
        fresh = [e for e in evs if e.get("id") not in out]
        for e in evs:
            out[e.get("id")] = e
        if not fresh:
            break
        oldest = min(e.get("created_at", until) for e in evs)
        if oldest <= since:
            break
        until = oldest - 1            # next page: strictly older
        if pace > 0:
            await asyncio.sleep(pace)
    return list(out.values())


async def main(days: int, kinds: list, dry_run: bool, relays: list):
    cfg = _read_config()
    dsn = cfg["pg_dsn"]
    direct = cfg.get("direct", False)
    batch = cfg.get("author_batch", 200)
    pace = cfg.get("request_pace_sec", 1.0)
    since = int(time.time()) - days * 86400
    relays = relays or cfg.get("upstream") or []   # default: the node's own configured upstream set
    if not relays:
        log.error("no source relays (empty --relays and no configured upstream)")
        return 1

    store = RelayStore(dsn, retention_days=cfg.get("retention_days", 30))
    store.open(asyncio.get_event_loop())
    gate = WotGate()
    await gate.load_from_store(store)
    members = sorted(gate.members())
    if not members:
        log.error("WoT is empty — nothing to backfill (relay not warmed up?)")
        store.close()
        return 1

    log.info("Backfilling %d WoT members from %s, last %dd, kinds=%s%s",
             len(members), ",".join(relays), days, kinds, " [DRY-RUN]" if dry_run else "")
    stored = scanned = 0
    for i in range(0, len(members), batch):
        chunk = members[i:i + batch]
        evs = await _fetch_batch(relays, chunk, kinds, since, direct, pace)
        scanned += len(evs)
        # Gate + verify + dedup, exactly like the live ingest path.
        ids = [e["id"] for e in evs if isinstance(e.get("id"), str) and len(e["id"]) == 64]
        existing = await store.filter_existing(ids)
        keep = [e for e in evs
                if e.get("id") not in existing
                and gate.is_member(e.get("pubkey", ""))
                and verify_event(e)]
        if keep and not dry_run:
            stored += await store.add_events_bulk(keep, origin="wot")
        elif keep:
            stored += len(keep)
        log.info("  batch %d/%d: %d fetched, %d new",
                 i // batch + 1, (len(members) + batch - 1) // batch, len(evs), len(keep))
        if pace > 0:
            await asyncio.sleep(pace)

    log.info("Done. scanned=%d  %s=%d", scanned, "would-store" if dry_run else "stored", stored)
    store.close()
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=29,
                    help="lookback window in days (default 29 — inside the 30d retention)")
    ap.add_argument("--kinds", default="1",
                    help="comma kinds to pull (default 1; stats bot counts kind-1). e.g. 1,6,7,1111")
    ap.add_argument("--dry-run", action="store_true", help="fetch + count, don't store")
    ap.add_argument("--relays", default="",
                    help="comma-separated source relays (default: the node's configured upstream set)")
    a = ap.parse_args()
    kinds = [int(k) for k in a.kinds.replace(" ", "").split(",") if k.strip().lstrip("-").isdigit()]
    relays = [r.strip() for r in a.relays.split(",") if r.strip()]
    sys.exit(asyncio.run(main(a.days, kinds, a.dry_run, relays)))
