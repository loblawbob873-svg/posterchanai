"""Background worker process — runs the polling schedulers off the web/API event loop.

The fediverse-timeline → Matrix bridge plus the social-notification / Nitter-feed /
matrix-notification / logs pollers all used to run on the app's single asyncio loop and
contended with request serving (the bridge in particular could stall the reactor for ~90s
on a busy global feed). They're **DB-mediated** — the bridge/relays persist their state and
maps (TimelinePost / SocialReplyMap / MatrixNotifyMap), and the app's reply/action endpoints
read those tables — so they run perfectly well in a separate process while the app keeps
serving requests.

Stays in the app process: **reminders** (needs the app's live websocket push) and the
**bot manager** (supervises bot subprocesses). Launched + supervised by the app
(`start_worker_process`/`stop_worker_process`), one per node, like the HTTP proxy.

Run standalone:  python -m app.worker
"""

import sys
import asyncio
import logging
import importlib
import subprocess
from typing import Optional

logger = logging.getLogger(__name__)

# (label, module, start-function) — each exposes an idempotent start_* that attaches an
# APScheduler AsyncIOScheduler to the running loop. Order is not significant.
_SCHEDULERS = [
    ("logs", "app.services.logs_scheduler", "start_logs_scheduler"),
    ("social-notifications", "app.services.social_notifications_service", "start_social_notifications_scheduler"),
    ("nitter-feeds", "app.services.nitter_feeds_service", "start_nitter_feeds_scheduler"),
    ("fedi-timeline", "app.services.fedi_timeline_service", "start_fedi_timeline_scheduler"),
    ("matrix-notifications", "app.services.matrix_notifications_service", "start_matrix_notifications_scheduler"),
    ("stats-bot", "app.services.stats_bot_service", "start_stats_bot_scheduler"),
    ("fedi-nostr-bridge", "app.services.fedi_nostr_bridge_service", "start_fedi_bridge_scheduler"),
    ("fedi-nostr-writeback", "app.services.fedi_nostr_writeback_service", "start_fedi_writeback_listener"),
    ("fedi-nostr-personal", "app.services.fedi_nostr_personal_service", "start_fedi_personal_scheduler"),
]

_worker_process: Optional[subprocess.Popen] = None


async def _run():
    # This is a SEPARATE process with its OWN settings_store cache (the relay is the authoritative
    # store). Hydrate it BEFORE starting the schedulers — otherwise setting-gated schedulers read their
    # build-time DEFAULTS instead of the relay value and silently never run. e.g. logs_scheduler_enabled
    # defaults to "false" but is "true" in the relay → the scheduled health report never fires. (The main
    # app process hydrates on its own startup; this worker must do it independently.)
    try:
        from app.database import SessionLocal
        from app.services import settings_store
        db = SessionLocal()
        try:
            n = settings_store.hydrate_from_db(db)
            logger.info(f"[worker] hydrated {n} setting(s) from relay before starting schedulers")
        finally:
            db.close()
    except Exception as e:
        logger.error(f"[worker] settings hydrate failed — schedulers may use defaults: {e}", exc_info=True)
    started = 0
    for name, module, fn in _SCHEDULERS:
        try:
            getattr(importlib.import_module(module), fn)()
            started += 1
            logger.info(f"[worker] started {name} scheduler")
        except Exception as e:
            logger.error(f"[worker] failed to start {name} scheduler: {e}", exc_info=True)
    logger.info(f"[worker] {started}/{len(_SCHEDULERS)} schedulers running")
    while True:  # keep the loop (and its schedulers) alive
        await asyncio.sleep(3600)


# --- supervised by the app (parent) -----------------------------------------

def start_worker_process() -> subprocess.Popen:
    """Spawn the background-scheduler worker as its own process. Idempotent."""
    global _worker_process
    if _worker_process and _worker_process.poll() is None:
        return _worker_process
    _worker_process = subprocess.Popen([sys.executable, "-m", "app.worker"])
    logger.info(f"[worker] background scheduler process started (pid {_worker_process.pid})")
    return _worker_process


def stop_worker_process():
    """Terminate the worker process if running."""
    global _worker_process
    if _worker_process and _worker_process.poll() is None:
        _worker_process.terminate()
        try:
            _worker_process.wait(timeout=10)
        except Exception:
            _worker_process.kill()
        logger.info("[worker] background scheduler process stopped")
    _worker_process = None


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [worker] %(name)s: %(message)s")
    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        pass
