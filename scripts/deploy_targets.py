#!/usr/bin/env python3
"""Which systemd units a deploy actually needs to restart, from the files it changed.

    scripts/deploy_targets.py <git-range>      e.g. HEAD~1..HEAD
    scripts/deploy_targets.py --files a.py b.py

Prints one unit per line (empty = restart nothing). Used by sync.sh.

The point of the role split is that shipping a router change should not drop every Nostr client, kill
live streams mid-broadcast or restart nine bots. That is only true if the deploy knows what it
touched — otherwise `systemctl restart` on everything gives back exactly the outage the split
removed.

CONSERVATIVE BY DESIGN. A path is mapped to one role only when it is unambiguously that role's; the
moment anything shared changes (app/database.py, app/models.py, settings_store, run.py, the role
plumbing itself) this returns EVERY unit. Under-restarting ships code that is running nowhere, which
is far harder to notice than an extra restart — the failure would be "the fix didn't work" with no
error anywhere.
"""
from __future__ import annotations

import os
import subprocess
import sys

APP = "posterchanai.service"
RELAY = "posterchanai-relay.service"
WORKER = "posterchanai-worker.service"
MEDIA = "posterchanai-media.service"
# The bot manager deliberately stays IN THE APP (see app/role.py:roles) — Admin -> Bots drives it
# through an in-process registry, so running it elsewhere showed every bot as stopped and made a
# button press spawn a second copy of each. Bot code therefore restarts the app.
BOTS = APP
ALL = (APP, RELAY, WORKER, MEDIA)

# (prefix, units) — longest prefix wins. Only paths whose owners are KNOWN belong here.
#
# app/routers + templates map to app+worker, not to everything. Measured: importing app.worker,
# app.role_runner and the relay thread pulls in no app.routers module, so a router change genuinely
# does not affect the relay, mediamtx/TURN or the bots — which are the restarts that actually hurt
# (dropped Nostr clients, streams killed mid-broadcast, bots restarted into their startup race). The
# worker is included anyway as a hedge against a lazy in-function import, because restarting it is
# cheap: its cursors are durable.
_OWNED = (
    ("app/routers/", (APP, WORKER)),
    ("app/main.py", (APP,)),
    ("templates/", (APP,)),
    ("relay_main.py", (RELAY,)),
    ("app/services/nostr_relay/", (RELAY,)),
    ("app/worker.py", (WORKER,)),
    ("app/services/logs_scheduler.py", (WORKER,)),
    ("app/services/social_notifications_service.py", (WORKER,)),
    ("app/services/nitter_feeds_service.py", (WORKER,)),
    ("app/services/uptime_service.py", (WORKER,)),
    ("app/services/stats_bot_service.py", (WORKER,)),
    ("app/services/nostr_push_service.py", (WORKER,)),
    ("app/services/fedi_nostr_bridge_service.py", (WORKER,)),
    ("app/services/fedi_nostr_writeback_service.py", (WORKER,)),
    ("app/services/fedi_nostr_personal_service.py", (WORKER,)),
    ("app/services/stream_service.py", (MEDIA,)),
    ("app/services/turn_service.py", (MEDIA,)),
    ("streamserver/", (MEDIA,)),
    ("turnserver/", (MEDIA,)),
    ("botframework/", (BOTS,)),
    ("app/services/bot_manager_service.py", (BOTS,)),
)

# Changed-but-restarts-nothing. The client is served as static files (router.lan pulls its own
# checkout), so a UI-only change must NOT take the ~90s outage a restart costs — that rule predates
# this script and is why "never sync.sh for UI-only changes" exists.
_INERT_PREFIXES = ("static/", "docs/", "tests/", "scripts/", ".github/", "README", "CLAUDE.md")
_INERT_SUFFIXES = (".md",)


def _inert(path: str) -> bool:
    return path.startswith(_INERT_PREFIXES) or path.endswith(_INERT_SUFFIXES)


def units_for(paths) -> list:
    """The units to restart for `paths`. Empty when nothing needs one."""
    live = [p for p in paths if p and not _inert(p)]
    if not live:
        return []
    units, shared = set(), False
    for p in live:
        owner = None
        for prefix, unit in _OWNED:
            if p == prefix or p.startswith(prefix):
                # longest prefix wins, so a more specific mapping added later still applies
                if owner is None or len(prefix) > owner[0]:
                    owner = (len(prefix), unit)
        if owner:
            units.update(owner[1])
        else:
            shared = True       # unmapped => could affect anything => everything restarts
    if shared:
        return list(ALL)
    # A role-only change still leaves the app process untouched, which is the whole win.
    return sorted(units)


def _changed(rng: str) -> list:
    out = subprocess.run(["git", "diff", "--name-only", rng], capture_output=True, text=True,
                         cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    if out.returncode != 0:
        # Can't tell what changed → restart everything. Never silently under-restart.
        print(f"# git diff failed: {out.stderr.strip()}", file=sys.stderr)
        return None
    return [l.strip() for l in out.stdout.splitlines() if l.strip()]


if __name__ == "__main__":
    args = sys.argv[1:]
    if args and args[0] == "--files":
        paths = args[1:]
    elif args:
        paths = _changed(args[0])
        if paths is None:
            print("\n".join(ALL))
            sys.exit(0)
    else:
        print(__doc__)
        sys.exit(2)
    print("\n".join(units_for(paths)))
