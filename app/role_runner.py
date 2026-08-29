"""Run ONE component of the stack as its own process (see run.py --role).

`media` and `bots` are the two roles with no standalone entrypoint of their own — `relay` is
relay_main.py and `worker` is `python -m app.worker`, both of which already run this way. This module
is the thin equivalent for the other two: start the SAME supervisors the app's lifespan starts, then
block until signalled and shut them down cleanly.

Why they leave the app process at all: under the historical single-process layout the web app
supervises the relay, mediamtx, pion-turn, tor and nine bots, so restarting to ship a one-line router
change drops every Nostr client, kills live streams MID-BROADCAST, drops active calls, and restarts
the bots — which is where their startup-race crashes cluster. The least stable component supervises
the most stable ones. Splitting them means a deploy restarts only what actually changed.

Deliberately NOT a second implementation: each role calls the existing `start_*`/`stop_*` pair, so
there is one supervisor per component and it cannot drift from the in-app path. Under `--role all`
those same functions are still called from app/main.py exactly as before.
"""
from __future__ import annotations

import logging
import os
import signal
import socket
import threading

logger = logging.getLogger(__name__)

# role -> [(label, module, start_fn, stop_fn)]
_ROLE_SERVICES = {
    "media": [
        ("streams (mediamtx)", "app.services.stream_service", "start_stream_server", "stop_stream_server"),
        ("TURN (pion-turn)", "app.services.turn_service", "start_turn_server", "stop_turn_server"),
    ],
    "bots": [
        ("bot manager", "app.services.bot_manager_service", "start_bot_manager", "stop_bot_manager"),
    ],
    "tor": [
        ("tor", "app.services.tor_service", "start_from_settings", "stop_tor_service"),
    ],
    "proxy": [
        ("http proxy", "app.services.http_proxy_service", "start_from_settings", "stop_http_proxy_process"),
    ],
    "git": [
        ("git host", "app.services.git_http_service", "start_git_http", "stop_git_http"),
    ],
    "shell": [
        ("ssh keeper", "app.services.ssh_keeper", "start_ssh_keeper", "stop_ssh_keeper"),
    ],
}


def _notify_ready() -> None:
    """Tell a Type=notify unit that every role service passed its own startup readiness gate.

    No systemd Python package is required. An abstract notify socket is spelled with ``@`` in the
    environment and a leading NUL at the AF_UNIX layer. Outside systemd this is deliberately a
    no-op, so direct role runs retain their existing behaviour.
    """
    address = os.environ.get("NOTIFY_SOCKET", "")
    if not address:
        return
    if address.startswith("@"):
        address = "\0" + address[1:]
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as notify:
        notify.connect(address)
        notify.sendall(b"READY=1")


def _bootstrap_settings() -> None:
    """Hydrate this process's OWN settings cache before starting anything.

    Copied in intent from app/worker.py, and load-bearing for the same reason: every process has its
    own settings_store cache, and a setting-gated supervisor that starts before hydration reads its
    BUILD-TIME DEFAULT instead of the relay value — so it silently never runs. `stream_enabled` and
    `bots_manager_enabled` both default to off, so getting this wrong means the role starts, logs
    nothing interesting, and supervises nothing.

    load_local() first: local-only keys (ports, plumbing) live in local_settings.json and the relay
    hydrate deliberately skips them.
    """
    from app.database import SessionLocal
    from app.services import settings_store
    settings_store.load_local()
    db = SessionLocal()
    try:
        n = settings_store.hydrate_from_db(db)
        # hydrate_from_db deliberately catches database errors because app startup can continue on
        # defaults.  A split role is different: it has no later hydration pass.  If Postgres is
        # still recovering during boot and we accept that empty cache, setting-gated services return
        # False and this process remains "active" forever while supervising nothing.  Make systemd's
        # Restart=always perform the retry it was intended to perform.
        if not settings_store.is_hydrated():
            raise RuntimeError("relay settings are not hydrated yet")
        logger.info("[role] hydrated %d setting(s) from the relay", n)
    finally:
        db.close()


def run_role(role: str) -> int:
    """Start `role`'s services and block until SIGTERM/SIGINT. Returns a process exit code."""
    logging.basicConfig(level=logging.INFO,
                        format="%%(asctime)s [role:%s] %%(name)s: %%(message)s" % role)
    services = _ROLE_SERVICES.get(role)
    if not services:
        logger.error("[role] no services defined for role %r", role)
        return 2

    try:
        _bootstrap_settings()
    except Exception as e:
        # Configuration-gated supervisors default to OFF. Starting them after a failed hydrate
        # therefore creates a healthy-looking role process that supervises nothing forever: this
        # left Go Live returning WHIP 502 for a day after Postgres was briefly unavailable at boot.
        # Exit instead. Every role unit has Restart=always, so systemd retries the complete hydrate
        # three seconds later and no partially initialised process can become the steady state.
        logger.error("[role] settings hydrate failed — exiting for a clean retry: %s", e,
                     exc_info=True)
        return 1

    started = []
    for label, module, start_fn, stop_fn in services:
        try:
            import importlib
            getattr(importlib.import_module(module), start_fn)()
            started.append((label, module, stop_fn))
            logger.info("[role] started %s", label)
        except Exception as e:
            logger.error("[role] failed to start %s: %s", label, e, exc_info=True)

    if not started:
        logger.error("[role] nothing started for role %r — exiting so systemd restarts us", role)
        return 1

    # The proxy's start function returns only after its child owns the configured listener. Its
    # Type=notify unit therefore cannot become active during the bind/readiness window, which made
    # `enable --now` and boot both report success while :8118 was still absent.
    try:
        _notify_ready()
    except OSError as e:
        logger.error("[role] could not report startup readiness: %s", e, exc_info=True)
        for _label, module, stop_fn in reversed(started):
            try:
                import importlib
                getattr(importlib.import_module(module), stop_fn)()
            except Exception:
                pass
        return 1

    # Block. The supervisors run their own threads/subprocesses, so this process only has to stay
    # alive and own their lifetime — when it exits, systemd's cgroup cleanup takes the children with
    # it, which is the property that makes `systemctl restart posterchanai-media` mean what it says.
    stop = threading.Event()

    def _sig(signum, _frame):
        logger.info("[role] signal %s — shutting down", signum)
        stop.set()

    signal.signal(signal.SIGTERM, _sig)
    signal.signal(signal.SIGINT, _sig)
    unhealthy = False
    while not stop.wait(1.0):
        # Optional service-specific liveness contract. Most roles supervise internally; the proxy
        # is a child process and must make its parent's systemd unit fail when that child disappears.
        for label, module, _stop_fn in started:
            try:
                import importlib
                check = getattr(importlib.import_module(module), "role_healthy", None)
                if check is not None and not check():
                    logger.error("[role] %s became unhealthy — exiting so systemd restarts it", label)
                    unhealthy = True
                    stop.set()
                    break
            except Exception as e:
                logger.error("[role] health check failed for %s: %s", label, e, exc_info=True)
                unhealthy = True
                stop.set()
                break

    for label, module, stop_fn in reversed(started):
        try:
            import importlib
            getattr(importlib.import_module(module), stop_fn)()
            logger.info("[role] stopped %s", label)
        except Exception as e:
            logger.warning("[role] error stopping %s: %s", label, e)
    return 1 if unhealthy else 0
