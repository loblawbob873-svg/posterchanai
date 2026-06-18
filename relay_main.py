#!/usr/bin/env python3
"""Standalone Nostr relay process.

Spawned by the main app (NOT a systemd service — see app/services/nostr_relay/thread.py
start_nostr_relay) so the built-in WoT relay runs in its OWN OS process. Python's GIL means a
thread can't run Python in parallel with the app; a separate process gets its own GIL/core, so
the relay's firehose parsing no longer steals CPU from the app's request/bot handling.

Reuses thread._read_config() + thread._main() verbatim; the app talks to this process via the
status file + control dir that _main maintains (so Admin → Relay still works cross-process). The
relay is a child of the app's cgroup, so a `systemctl restart` takes it down with the app and the
new app instance spawns a fresh one — code changes apply on deploy, logs land in the journal.
"""
import os
import sys
import signal
import asyncio
import logging

_REPO_ROOT = os.path.dirname(os.path.abspath(__file__))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from app.services.nostr_relay import thread as _t


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    log = logging.getLogger("nostr-relay-proc")
    cfg = _t._read_config()
    if not cfg.get("enabled"):
        log.info("[nostr-relay] disabled (nostr_relay_enabled off) — exiting")
        return

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    _t._relay.loop = loop
    _t._relay.cfg = cfg

    def _stop(*_a):
        ev = getattr(_t._relay, "stop_event", None)
        if ev is not None:
            loop.call_soon_threadsafe(ev.set)

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    log.info("[nostr-relay] standalone process starting (pid %d)", os.getpid())
    try:
        loop.run_until_complete(_t._main(cfg))
    except Exception:
        log.exception("[nostr-relay] process crashed")
    finally:
        try:
            loop.close()
        except Exception:
            pass


if __name__ == "__main__":
    main()
