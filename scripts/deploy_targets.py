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
TOR = "posterchanai-tor.service"
PROXY = "posterchanai-proxy.service"
GIT = "posterchanai-git.service"
# The SSH terminal keeper. THE ONLY UNIT DELIBERATELY LEFT OUT OF `ALL`, and the reason inverts this
# file's usual rule: restarting it DESTROYS USER STATE — every open shell, mid-command. Its whole
# purpose is to outlive a deploy of the app, so a conservative "restart everything" would quietly
# undo the feature several times a day. It is a leaf (paramiko + ssh_service + settings_store at
# runtime), so under-restarting it means it keeps running slightly older SSH code until someone
# restarts it on purpose — visible, harmless, and recoverable, which is not true the other way round.
SHELL = "posterchanai-shell.service"
ALL = (APP, RELAY, WORKER, MEDIA, TOR, PROXY, GIT)

# (prefix, units) — longest prefix wins. Only paths whose owners are KNOWN belong here.
#
# app/routers + templates map to app+worker, not to everything. Measured: importing app.worker,
# app.role_runner and the relay thread pulls in no app.routers module, so a router change genuinely
# does not affect the relay, mediamtx/TURN or the bots — which are the restarts that actually hurt
# (dropped Nostr clients, streams killed mid-broadcast, bots restarted into their startup race). The
# worker is included anyway as a hedge against a lazy in-function import, because restarting it is
# cheap: its cursors are durable.
_OWNED = (
    # The keeper's own code, and the session code it runs. These DO restart it — the alternative is
    # a fix to the terminal that is running nowhere.
    ("app/services/ssh_keeper.py", (SHELL,)),
    ("app/services/ssh_service.py", (APP, SHELL)),
    ("app/routers/ssh_term.py", (APP,)),
    ("app/routers/", (APP, WORKER)),
    # The shared command layer (web UI websocket + Telegram), both of which live in the app.
    # MEASURED the same way app/routers/ was, by importing each role's own modules and checking
    # sys.modules: relay_main, app.worker, stream/turn (media), tor, http_proxy, git_http and the bot
    # manager pull in NO command_service module. WORKER is included as the same cheap hedge against a
    # lazy in-function import that app/routers/ carries. Left unmapped it meant "everything", so
    # aliasing `syslogs` restarted the relay on both nodes — twice in one session, the second time
    # after the user had already pointed out that the split exists to prevent exactly this.
    ("app/services/command_service/", (APP, WORKER)),
    ("app/main.py", (APP,)),
    ("templates/", (APP,)),
    # Web search: the SearXNG resolver + the page/URL fetchers. MEASURED the same way app/routers/
    # was — importing relay_main, app.worker, tor_service, http_proxy_service, git_http_service and
    # stream_service leaves `app.services.search_service` out of sys.modules in every one of them.
    # Unmapped it meant "everything", so adding the Web Search screen restarted the RELAY on both
    # nodes and dropped every connected Nostr client, for a file the relay never loads. WORKER is the
    # same cheap hedge the router rule carries: the news/markets pollers reach it by a lazy import.
    ("app/services/search_service.py", (APP, WORKER)),
    # The datastore CLIENT (documents on the relay), and the calendar layer on top of it. MEASURED
    # the same way: relay_main, tor, proxy, git and the worker leave `app.services.nostr_store` out
    # of sys.modules; only stream_service (MEDIA) pulls it in, and the worker reaches it lazily.
    # Unmapped it meant "everything", so a change to a document helper restarted the RELAY and
    # dropped every connected Nostr client — the outage the role split exists to prevent.
    ("app/services/nostr_store.py", (APP, WORKER, MEDIA)),
    ("app/services/caldav_store.py", (APP, WORKER)),
    ("app/services/caldav/", (APP,)),          # the Radicale plugins live in the app's own process
    # The calendar alarm poller and the mailbox both run in the app process. Left unmapped they mean
    # "everything", which restarts the relay and drops every connected Nostr client for a change that
    # cannot affect it.
    ("app/services/calendar_notify_service.py", (APP,)),
    ("app/services/mail_notify_service.py", (WORKER,)),   # the poller runs in the worker only
    ("app/services/mail_store.py", (APP,)),
    ("app/services/mail_sync.py", (APP,)),
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
    ("app/services/tor_service.py", (TOR,)),
    ("app/services/http_proxy_service.py", (PROXY,)),
    ("app/services/git_http_service.py", (GIT,)),
    # The git smart-HTTP server itself: its own process, launched by git_http_service from this path.
    ("git_host_main.py", (GIT,)),
    ("botframework/", (BOTS,)),
    ("app/services/bot_manager_service.py", (BOTS,)),
)

# Changed-but-restarts-nothing. The client is served as static files (router.lan pulls its own
# checkout), so a UI-only change must NOT take the ~90s outage a restart costs — that rule predates
# this script and is why "never sync.sh for UI-only changes" exists.
_INERT_PREFIXES = ("static/", "docs/", "tests/", "scripts/", ".github/", "README", "CLAUDE.md",
                   # The Electron desktop app. It is built and shipped separately (electron-builder →
                   # dist/), and NO service on a systemd node imports, reads or serves anything under
                   # here — the app is a window onto /client over HTTP like any other browser. Left
                   # unmapped it meant "could affect anything", so a one-line edit to the offline
                   # card's wording restarted all seven units on both nodes: every connected Nostr
                   # client dropped, streams killed mid-broadcast, nine bots bounced. Exactly the
                   # outage the role split removed, for a file the servers never load.
                   "desktop/",
                   # The Capacitor Android project and the Firefox extension, for the SAME reason as
                   # desktop/ above: both are built and shipped by CI (android.yml / extension.yml) to a
                   # GitHub release, and no service on a systemd node imports, reads or serves a byte of
                   # either — /apk, /desktop/* and /extension/* are 302s to those releases. mobile/ being
                   # unmapped meant "could affect anything", so a two-line comment fix in
                   # mobile/build-www.sh restarted ALL SEVEN units on both nodes: every connected Nostr
                   # client dropped, streams killed mid-broadcast, the bots bounced. The commit that did
                   # it otherwise touched only static/ and templates/ — a single-service restart.
                   "mobile/", "extension/",
                   # git_hooks/ is NOT loaded by any service. install_hooks writes a shell shim into
                   # each bare repo that `exec`s "<venv python> <checkout>/git_hooks/<file>.py", so
                   # git-receive-pack spawns a FRESH process per push and reads the file off disk
                   # every time — a pull is all a hook change needs. Left unmapped it meant "could
                   # affect anything", so editing one hook's log message restarted all seven units on
                   # both nodes, dropping every connected Nostr client. That is precisely the outage
                   # the role split removed, caused by the tooling that exists to prevent it.
                   "git_hooks/",
                   # The CONTAINER build: docker-compose.yml, the Dockerfiles, and docker/ (nginx conf,
                   # the bundled SearXNG's settings template). A systemd node runs none of it — compose
                   # is the other way to deploy this app, and docker/searxng/settings.yml is read by
                   # ./install.sh, which is a person running a command, not a service. Unmapped they
                   # meant "could affect anything", so adding a compose SERVICE restarted all seven
                   # units on both bare-metal nodes: every connected Nostr client dropped for a file
                   # neither node opens.
                   "docker/", "docker-compose", "Dockerfile")
_INERT_SUFFIXES = (".md",
                   # Templates BY DEFINITION: *.example is a file you copy and edit, so nothing reads
                   # the original at runtime — a service that loaded one would be loading a sample.
                   ".example")
# App-store listing metadata. zapstore.yaml is fetched from the REPO by the Zapstore relay (that is
# how the app is tied to its npub) and read by the publish step in android.yml — never by a running
# service. Unmapped it meant "could affect anything", so editing the store DESCRIPTION restarted all
# seven units on both nodes: every connected Nostr client dropped and the relay bounced mid-stream.
# The same mistake as desktop/, mobile/, extension/ and git_hooks/ above, one file at a time — which
# is the argument for listing what IS runtime rather than chasing what is not, but that inverts a
# deliberately fail-safe default and is not worth the risk of a silent under-restart.
# DEPLOY TOOLING. These are read fresh by whoever runs them and are imported by no service, so a
# change to one must restart NOTHING. They were unmapped, which means "could affect anything" and
# therefore EVERY unit — so editing sync.sh itself restarted the relay and put every connected web
# client into "reconnecting". The tooling that exists to avoid downtime was causing it.
#
# NOT the run-*.sh launchers: those ARE each unit's ExecStart, so a change there genuinely needs a
# restart and must keep falling through to the shared/everything branch.
_INERT_FILES = ("sync.sh", "install.sh",
                # Container build/orchestration. Irrelevant to a systemd node — nothing running on
                # these boxes loads them — so a compose/Dockerfile edit must restart NOTHING. Left
                # unmapped they meant "everything", which is how fixing a Docker doc would have
                # bounced the relay and every connected web client.
                "docker-compose.yml", "Dockerfile", "Dockerfile.sandbox", ".dockerignore",
                # App-store listing metadata. The Zapstore RELAY fetches this from the repo (that is
                # how the app is tied to its npub) and android.yml reads it when publishing — no
                # running service ever loads it. Unmapped it meant "could affect anything", so
                # editing the store DESCRIPTION restarted all seven units on both nodes: every
                # connected Nostr client dropped and the relay bounced. The same mistake as
                # desktop/, mobile/, extension/ and git_hooks/ above, one file at a time.
                # (nostr.json is deliberately NOT here: every reference to it in the tree is the URL
                # /.well-known/nostr.json that the relay serves from its own settings, and "I could
                # not find a reader" is not the same as "there is none". An over-restart costs 90
                # seconds; an under-restart ships code that is running nowhere.)
                "zapstore.yaml")


def _inert(path: str) -> bool:
    return (path in _INERT_FILES or path.startswith(_INERT_PREFIXES)
            or path.endswith(_INERT_SUFFIXES))


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
