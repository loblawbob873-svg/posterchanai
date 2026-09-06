"""Background worker process — runs the polling schedulers off the web/API event loop.

The fediverse bridge plus the social-notification / logs pollers all used to
run on the app's single asyncio loop and
contended with request serving (the bridge in particular could stall the reactor for ~90s
on a busy global feed). They're **DB-mediated** — the bridge/relays persist their state and
maps (SocialReplyMap / FediBridgeDelivered), and the app's reply/action endpoints
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
    ("instance-welcome", "app.services.instance_welcome", "start_instance_welcome_scheduler"),
    ("logs", "app.services.logs_scheduler", "start_logs_scheduler"),
    ("social-notifications", "app.services.social_notifications_service", "start_social_notifications_scheduler"),
    ("stats-bot", "app.services.stats_bot_service", "start_stats_bot_scheduler"),
    ("uptime", "app.services.uptime_service", "start_uptime_scheduler"),
    # Subscribed calendars (a published .ics mirrored into one of yours). In the WORKER because the
    # people reading these are on a PHONE, over CalDAV, in an app that never opens PosterChan — a
    # refresh that only ran when somebody looked at the web UI would leave the phone confidently
    # showing last term.
    ("calendar-subscriptions", "app.services.caldav_subscribe",
     "start_calendar_subscriptions_scheduler"),
    # Pay-to-stay zap watcher — a no-op tick unless nostr_relay_paid_retention_enabled is on.
    ("paid-retention", "app.services.paid_retention_service", "start_paid_retention_scheduler"),
    ("fedi-nostr-bridge", "app.services.fedi_nostr_bridge_service", "start_fedi_bridge_scheduler"),
    ("fedi-nostr-writeback", "app.services.fedi_nostr_writeback_service", "start_fedi_writeback_listener"),
    ("fedi-nostr-personal", "app.services.fedi_nostr_personal_service", "start_fedi_personal_scheduler"),
    ("nostr-push", "app.services.nostr_push_service", "start_nostr_push_scheduler"),
    # New mail → push, for a phone whose screen is off. Self-gating: start_* is a no-op unless
    # `mail_poll_enabled` is on, and it lives HERE rather than in the app process because an IMAP
    # round trip per account is exactly the long await that should not share the request loop.
    ("mail-notify", "app.services.mail_notify_service", "start_mail_notify_scheduler"),
    # Local Wallet is a pooled Monero wallet with one account per user. Maintain each account's
    # independently spendable outputs here; the operator-only timer cannot see these funds.
    ("monero-user-outputs", "app.services.monero_user_wallets",
     "start_user_wallet_output_scheduler"),
]

_worker_process: Optional[subprocess.Popen] = None


async def _wait_for_relay(timeout: float = 60.0) -> None:
    """Block until the local Nostr relay is accepting TCP connections, before starting the
    relay-dependent schedulers. Otherwise the bridge/stats pollers race the relay subprocess boot and
    every publish/query fails 'connection refused' for ~a minute (log-noise + wasted dials on every
    restart). Bounded — on timeout we start anyway and let the components' own retries take over."""
    import time as _t
    from app.services import settings_store
    try:
        port = int(settings_store.get("nostr_relay_port", 3052) or 3052)
    except (ValueError, TypeError):
        port = 3052
    deadline = _t.monotonic() + timeout
    while _t.monotonic() < deadline:
        try:
            _, w = await asyncio.wait_for(asyncio.open_connection("127.0.0.1", port), timeout=2)
            w.close()
            try:
                await w.wait_closed()
            except Exception:
                pass
            logger.info("[worker] local relay listening on :%d — starting schedulers", port)
            return
        except Exception:
            await asyncio.sleep(1.0)
    logger.warning("[worker] local relay not up after %ds — starting schedulers anyway", int(timeout))


async def _run():
    # This is a SEPARATE process with its OWN settings_store cache (the relay is the authoritative
    # store). Hydrate it BEFORE starting the schedulers — otherwise setting-gated schedulers read their
    # build-time DEFAULTS instead of the relay value and silently never run. e.g. logs_scheduler_enabled
    # defaults to "false" but is "true" in the relay → the scheduled health report never fires. (The main
    # app process hydrates on its own startup; this worker must do it independently.)
    try:
        from app.database import SessionLocal
        from app.services import settings_store
        # Load LOCAL-ONLY keys (plumbing + per-node runtime cursors like fedi_bridge_global_since) from
        # local_settings.json FIRST — the relay hydrate below skips these, so without this the worker
        # starts every restart with no cursor ("cursor lost — resuming…") and re-derives it each boot.
        settings_store.load_local()
        db = SessionLocal()
        try:
            n = settings_store.hydrate_from_db(db)
            logger.info(f"[worker] hydrated {n} setting(s) from relay before starting schedulers")
        finally:
            db.close()
    except Exception as e:
        logger.error(f"[worker] settings hydrate failed — schedulers may use defaults: {e}", exc_info=True)
    # Wait for the local relay to be up before starting the relay-dependent schedulers (avoids the
    # ~60s of 'connection refused' noise while the relay subprocess is still booting).
    await _wait_for_relay()
    started = 0
    for name, module, fn in _SCHEDULERS:
        try:
            getattr(importlib.import_module(module), fn)()
            started += 1
            logger.info(f"[worker] started {name} scheduler")
        except Exception as e:
            logger.error(f"[worker] failed to start {name} scheduler: {e}", exc_info=True)
    logger.info(f"[worker] {started}/{len(_SCHEDULERS)} schedulers running")
    # Keep the loop alive AND periodically re-hydrate settings from the relay, so changes made in the
    # main process (admin Save, the bridge OAuth token write) reach this separate process within a
    # couple of minutes without a restart. hydrate_from_db reads the relay's Postgres directly (cheap).
    from app.database import SessionLocal
    from app.services import settings_store
    while True:
        await asyncio.sleep(120)
        try:
            db = SessionLocal()
            try:
                settings_store.hydrate_from_db(db)
            finally:
                db.close()
        except Exception as e:
            logger.debug(f"[worker] periodic settings re-hydrate failed: {e}")


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
            _worker_process.wait(timeout=3)   # was 10 — escalate to SIGKILL fast so it can't
            #                                   push the service past its 10s stop deadline (the
            #                                   worker only runs pollers; cursors persist to the relay)
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
