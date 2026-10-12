#!/usr/bin/env python3
"""Send members' events that a delivery stall left behind to the fediverse -- by hand, once.

Why this exists: on 2026-10-11 outbound ActivityPub delivery stalled for ~23 hours, and the reader then
in use took a long backlog NEWEST-FIRST with a cap of 10,000 events. When the stall was fixed (deploy
206), its first pass handed over only the newest part of the window and moved the cursor past the rest:
the oldest ~16 hours of members' posts, likes, boosts and a report were never sent. The reader now takes
a backlog oldest-first in windows (outbox._windows), so that cannot recur -- this repairs what it did.

What is NOT sent again (outbox.redeliver): an event in the delivery cursor's `done` set, an event with
a `pcai:ap:sent:` record (written when a pass planned it: every boost, like, vote and REPORT, and every
post that went beyond the followers -- so a report is never filed twice), anything an earlier run of the
same window sent (its own progress document `pcai:ap:redeliver:<since>:<until>`, so a rerun or a crash
part-way resumes), contact lists (the follow catch-up's job) and the node's own app-document deletions.
A plain post that DID go out during the stall may be sent a second time; Mastodon, Pleroma, Akkoma and
Misskey all ignore a Create of an object id they already hold. Nothing is logged but counts.

    # 1. what would go out (sends nothing):
    venv-unified/bin/python scripts/ap_redeliver.py --since 1791686323 --dry-run
    # 2. stop at where the old capped reader's first pass started reading (the boundary is computed by
    #    replaying that read as of the moment it ran -- when the worker that ran deploy 206 started):
    venv-unified/bin/python scripts/ap_redeliver.py --since 1791686323 --boundary-at <unix time> --dry-run
    # 3. for real: the same, without --dry-run

Run it on the node whose relay holds the events (the delivery worker's node), as the service user, from
the repo root. It needs the relay up; it reads settings the way the worker does.
"""
import argparse
import asyncio
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


async def _main(args) -> int:
    from app.services import settings_store
    settings_store.load_local()
    from app.database import SessionLocal
    db = SessionLocal()
    try:
        settings_store.hydrate_from_db(db)
    finally:
        db.close()
    from app.services.activitypub import config, outbox
    if not config.enabled() or not config.base_url():
        print("ActivityPub is off on this node (or it has no domain): nothing to send.")
        return 2
    until = args.until
    if args.boundary_at:
        b = await outbox.capped_read_boundary(args.since, args.boundary_at)
        if b <= args.since:
            print(f"the capped read as of {args.boundary_at} was not capped: it dropped nothing")
            return 0
        print(f"the capped read as of {args.boundary_at} handed over nothing older than {b} "
              f"({time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime(b))}); stopping there")
        until = b
    out = await outbox.redeliver(args.since, until, dry_run=args.dry_run, log=print)
    print(json.dumps(out, indent=2, sort_keys=True))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--since", type=int, required=True, help="unix time the stall began (the stuck cursor)")
    ap.add_argument("--until", type=int, default=None, help="unix time to stop at (default: now)")
    ap.add_argument("--boundary-at", type=int, default=None,
                    help="stop where the OLD capped reader's first pass, run at this unix time, stopped")
    ap.add_argument("--dry-run", action="store_true", help="count what would be sent; send nothing")
    return asyncio.run(_main(ap.parse_args()))


if __name__ == "__main__":
    sys.exit(main())
