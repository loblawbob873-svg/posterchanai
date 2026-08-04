#!/usr/bin/env python3
"""Copy the encrypted libraries already on this relay out to the private mirror relays.

The relay's private mirror only fires on a WRITE, so turning it on protects everything from that
moment forward and nothing from before it — which, for a notebook and a password vault, is most of
what there is. This is the one-time backfill: every `pcai:note:` / `pcai:pw:` / `pcai:pwfolder:` /
`pcai:notefolder:` / `pcai:pwkey` / `pcai:budget` / `pcai:files-index` event in the local store,
published to the relays named in `nostr_relay_private_relays`.

It sends the events EXACTLY as stored, signature and all. There is no re-signing and no key here:
a Nostr event is self-authenticating, so a mirror is a byte copy, and this script could not alter
one if it tried.

    venv-unified/bin/python scripts/mirror_private_docs.py --dry-run
    venv-unified/bin/python scripts/mirror_private_docs.py

Run it on the node that HOLDS the library (the one the clients publish to). Re-running is safe:
relays deduplicate by event id, and a replaceable event that is already there is a no-op.
"""
import argparse
import asyncio
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.services.nostr import relay as nostr_relay                              # noqa: E402
from app.services.nostr_relay.server import _PRIVATE_KINDS, _private_mirrorable  # noqa: E402
from app.services.nostr_relay.store import RelayStore                            # noqa: E402
from app.services.nostr_relay.thread import _read_config                         # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("mirror")

KIND = 30078
PAGE = 2000       # under the store's own 5000 clamp, so a page is never silently truncated


def _d(ev):
    return next((t[1] for t in ev.get("tags", []) if len(t) >= 2 and t[0] == "d"), "")


async def _collect(store):
    """Every private event in the store, paged BACKWARDS in time.

    The store clamps any filter limit to 5000 and orders created_at DESC, so a single wide query
    returns the newest 5000 kind-30078 events of ALL namespaces — chat messages, settings, uploads —
    and only then does the Python filter run. On a node with any real chat history that window is
    almost entirely chat, and the truncation is newest-first, so the OLD notes this script exists to
    copy are exactly the ones discarded. It would print "found 3 events", send 3, and exit 0.

    So: page with `until`, and stop when a page returns nothing new. Paging by time can repeat
    events that share a second, which is what the `seen` set is for.
    """
    seen, want = set(), []
    for kinds in ([KIND], list(_PRIVATE_KINDS)):
        until = None
        while True:
            f = {"kinds": kinds, "limit": PAGE}
            if until is not None:
                f["until"] = until
            evs = await store.query([f], hard_cap=PAGE)
            fresh = [e for e in evs if e.get("id") not in seen]
            if not fresh:
                break
            for e in fresh:
                seen.add(e.get("id"))
                if _private_mirrorable(e):
                    want.append(e)
            oldest = min(int(e.get("created_at") or 0) for e in evs)
            if len(evs) < PAGE:
                break
            # +1 so an event exactly on the boundary is not skipped; `seen` absorbs the overlap.
            nxt = oldest + 1
            if until is not None and nxt >= until:
                # A whole page inside one second. Stopping here would silently drop everything
                # OLDER and still exit 0 — a partial backup reporting success, which is the one
                # outcome this script must never produce. Say so and fail.
                log.error("more than %d events share created_at=%d; this page cannot advance and "
                          "everything older would be skipped. Re-run with --pace 0 --relays … after "
                          "raising PAGE, or mirror those seconds by hand.", PAGE, oldest)
                raise SystemExit(3)
            until = nxt
    return want


async def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true",
                    help="list what would be sent, send nothing")
    ap.add_argument("--relays", default="",
                    help="comma/newline separated, overriding the nostr_relay_private_relays setting")
    ap.add_argument("--pace", type=float, default=0.25,
                    help="seconds between publishes (default 0.25) — a mirror is not a race, and a "
                         "relay that rate-limits you mid-run leaves a partial copy")
    args = ap.parse_args()

    # _read_config, NOT settings_store.get: the settings cache is EMPTY in a standalone process, so
    # every key reads back as "" — the script would report "no mirror relays configured" on a node
    # that has them, and fall back to the default DSN (a different database) for the store, where it
    # would find nothing, print "0 events to mirror" and exit 0. A backup that says it worked.
    cfg = _read_config()
    relays = nostr_relay.normalize_relays(args.relays or "") or cfg.get("private_relays") or []
    if not relays:
        # Refusing rather than falling back to the public defaults. The whole reason this list is
        # separate is that "somewhere" is not an acceptable answer for where a vault gets copied.
        log.error("no private mirror relays configured.\n"
                  "Set Admin → Nostr → Private mirror relays (or pass --relays), and point it at a "
                  "relay YOU run. Not a public one: the bodies are encrypted, but every event is a "
                  "permanent record of how many passwords a user has and when each changed.")
        return 2
    log.info("mirroring to %d relay(s): %s", len(relays), ", ".join(relays))

    store = RelayStore(cfg["pg_dsn"], retention_days=cfg.get("retention_days", 30))
    store.open(asyncio.get_running_loop())
    try:
        want = await _collect(store)
    finally:
        try:
            store.close()
        except Exception:
            pass

    by_ns = {}
    for e in want:
        d = _d(e)
        ns = d.split(":")[1] if d.count(":") >= 1 else d
        by_ns[ns] = by_ns.get(ns, 0) + 1
    log.info("found %d event(s) to mirror: %s", len(want),
             ", ".join(f"{n}×{c}" for n, c in sorted(by_ns.items())) or "none")
    if args.dry_run:
        log.info("dry run — nothing sent")
        return 0
    if not want:
        return 0

    sent = failed = 0
    for i, e in enumerate(want, 1):
        try:
            n = await nostr_relay.publish(relays, e, direct=cfg.get("direct", False))
        except Exception as exc:                       # a relay refusing must not end the run
            n, exc_s = 0, str(exc)
            log.warning("  %s: %s", _d(e), exc_s)
        if n:
            sent += 1
        else:
            failed += 1
        if i % 50 == 0 or i == len(want):
            log.info("  %d/%d (%d accepted, %d refused)", i, len(want), sent, failed)
        await asyncio.sleep(args.pace)

    log.info("done — %d accepted by at least one relay, %d refused everywhere", sent, failed)
    # Nonzero on any miss: a backup that reports success while a note did not make it is worse than
    # no backup, because it is the one you stop checking.
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
