"""Runs the built-in Nostr relay in its own daemon thread + asyncio event loop, fully
isolated from the main uvicorn request loop. Owns the websockets listener, the tmpfs
store, the WoT gate, and the periodic loops (WoT refresh, snapshot, prune; ingestion +
outbox are wired in later phases).

Public API: start_nostr_relay() / stop_nostr_relay(), called from app/main.py under the
port-3051 guard. Both are no-ops unless `nostr_relay_enabled` is set.
"""

import os
import json
import asyncio
import logging
import threading

from websockets.asyncio.server import serve

from app.services.nostr import nostr_service
from .store import RelayStore
from .wot import WotGate
from .server import RelayServer

logger = logging.getLogger(__name__)

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))

# Per-connection protocol limits (constants — tune in code, not user-facing).
_MAX_MESSAGE_SIZE = 256 * 1024
_MAX_SUBS_PER_CONN = 20
_MAX_FILTERS_PER_REQ = 10


class _Relay:
    def __init__(self):
        self.thread: threading.Thread | None = None
        self.loop: asyncio.AbstractEventLoop | None = None
        self.stop_event: asyncio.Event | None = None
        self.store: RelayStore | None = None
        self.gate: WotGate | None = None
        self.server: RelayServer | None = None
        self.cfg: dict = {}

    def is_running(self) -> bool:
        return self.thread is not None and self.thread.is_alive()


_relay = _Relay()


# --- settings ---------------------------------------------------------------

def _read_config() -> dict:
    from app.database import SessionLocal
    from app.models import Setting
    db = SessionLocal()
    try:
        rows = {s.key: s.value for s in db.query(Setting).filter(
            Setting.key.like("nostr_relay_%")).all()}

        def g(key, default=""):
            v = rows.get(key)
            return v if v not in (None, "") else default

        def gi(key, default):
            try:
                return int(g(key, str(default)))
            except (ValueError, TypeError):
                return default

        def gb(key, default=False):
            return str(g(key, str(default))).strip().lower() in ("1", "true", "yes", "on")

        seeds_raw = g("nostr_relay_wot_seeds", "")
        seeds = []
        for tok in seeds_raw.replace(",", "\n").split():
            pk = nostr_service.to_pubkey_hex(tok.strip())
            if pk:
                seeds.append(pk)

        upstream = nostr_service.relay.normalize_relays(
            g("nostr_relay_upstream_relays", "")) or list(nostr_service.DEFAULT_RELAYS)

        scratch = g("nostr_relay_scratch_dir", "/tmp")
        hot_path = os.path.join(scratch, "posterchanai-nostr-relay.db")
        snap_path = g("nostr_relay_db_path", os.path.join(_REPO_ROOT, "data", "nostr_relay.db"))

        cfg = {
            "enabled": gb("nostr_relay_enabled", False),
            "bind": g("nostr_relay_bind", "127.0.0.1"),
            "port": gi("nostr_relay_port", 3052),
            "seeds": seeds,
            "upstream": upstream,
            "hot_path": hot_path,
            "snapshot_path": snap_path,
            "snapshot_sec": gi("nostr_relay_snapshot_sec", 600),
            "retention_days": gi("nostr_relay_retention_days", 30),
            "max_events": gi("nostr_relay_max_events", 500000),
            "max_db_mb": gi("nostr_relay_max_db_mb", 1024),
            "wot_refresh_sec": gi("nostr_relay_wot_refresh_sec", 86400),  # daily
            "max_connections": gi("nostr_relay_max_connections", 5000),
            # windowed ingestion
            "sync_window_sec": gi("nostr_relay_sync_window_sec", 600),
            "sync_interval_sec": gi("nostr_relay_sync_interval_sec", 120),
            "overlap_sec": gi("nostr_relay_overlap_sec", 120),
            "ingest_kinds": [int(k) for k in (g("nostr_relay_ingest_kinds", "1,6,7")
                             .replace(" ", "").split(",")) if k.strip().lstrip("-").isdigit()],
            "author_batch": gi("nostr_relay_author_batch", 200),
            "fetch_ancestors": gb("nostr_relay_fetch_ancestors", True),
            "max_ancestors": gi("nostr_relay_max_ancestors", 20),
            "blocked_langs": {x.strip() for x in g("nostr_relay_blocked_langs", "")
                              .replace(",", " ").split() if x.strip()},
            # NIP-11 metadata
            "name": g("nostr_relay_name", "PosterChanAI Relay"),
            "description": g("nostr_relay_description", "Web-of-trust relay"),
            "pubkey": nostr_service.to_pubkey_hex(g("nostr_relay_pubkey", "")) or "",
            "contact": g("nostr_relay_contact", ""),
            # protocol limits
            "max_message_size": _MAX_MESSAGE_SIZE,
            "max_subs_per_conn": _MAX_SUBS_PER_CONN,
            "max_filters_per_req": _MAX_FILTERS_PER_REQ,
        }
        cfg["operator"] = _collect_operator_pubkeys(db)
        return cfg
    finally:
        db.close()


def _collect_operator_pubkeys(db) -> list:
    """Pubkeys that may always publish through the relay: every linked user's and bot's
    Nostr key. So our own bots/users can point their relay list here and still be accepted."""
    out: set = set()
    try:
        from app.models import User, Bot
        for u in db.query(User).all():
            nsec = getattr(u, "nostr_nsec", None)
            if nsec:
                try:
                    out.add(nostr_service.derive_pubkey(nostr_service.decode_seckey(nsec)))
                except Exception:
                    pass
        for b in db.query(Bot).all():
            try:
                cfg = json.loads(b.config or "{}")
            except (ValueError, TypeError):
                continue
            nsec = cfg.get("nostr_nsec")
            if nsec:
                try:
                    out.add(nostr_service.derive_pubkey(nostr_service.decode_seckey(nsec)))
                except Exception:
                    pass
    except Exception as e:
        logger.debug("[nostr-relay] operator key collection failed: %s", e)
    return list(out)


# --- async main -------------------------------------------------------------

async def _main(cfg: dict) -> None:
    # Public port → scanners/probes will hit it; the websockets server logs each failed
    # handshake with a full traceback, which would flood the journal. Quiet it.
    logging.getLogger("websockets.server").setLevel(logging.CRITICAL)
    store = RelayStore(
        cfg["hot_path"], cfg["snapshot_path"],
        max_events=cfg["max_events"], retention_days=cfg["retention_days"],
        max_db_mb=cfg["max_db_mb"])
    loop = asyncio.get_running_loop()
    store.open(loop)
    gate = WotGate()
    gate.set_operator(cfg["operator"])
    await gate.load_from_store(store)              # warm from snapshot for immediate gating
    from . import outbox as _outbox
    server = RelayServer(store, gate, cfg,
                         outbox_cb=lambda ev: _outbox.broadcast(cfg["upstream"], ev))
    _relay.store, _relay.gate, _relay.server = store, gate, server
    _relay.stop_event = asyncio.Event()

    # Initial WoT build (best-effort; non-fatal if upstream is slow).
    asyncio.create_task(_safe(gate.build(store, cfg["upstream"], cfg["seeds"])))

    ws = await serve(
        server.handle, cfg["bind"], cfg["port"],
        process_request=server.process_request,
        max_size=cfg["max_message_size"],
        ping_interval=30, ping_timeout=30, max_queue=64,
    )
    logger.info("[nostr-relay] listening on ws://%s:%d/relay (operator=%d, seeds=%d)",
                cfg["bind"], cfg["port"], len(cfg["operator"]), len(cfg["seeds"]))

    from . import ingest as _ingest
    tasks = [
        asyncio.create_task(_periodic(_relay.stop_event, cfg["snapshot_sec"], store.snapshot,
                                      "snapshot")),
        asyncio.create_task(_periodic(_relay.stop_event, 3600, store.prune, "prune")),
        # Daily WoT rebuild from the seeds' follow lists.
        asyncio.create_task(_periodic(_relay.stop_event, cfg["wot_refresh_sec"],
                                      lambda: gate.build(store, cfg["upstream"], cfg["seeds"]),
                                      "wot-refresh")),
        # Windowed WoT ingestion (the curated feed) — runs on its own cadence.
        asyncio.create_task(_periodic(_relay.stop_event, cfg["sync_interval_sec"],
                                      lambda: _ingest.sync_tick(store, gate, server,
                                                                cfg["upstream"], cfg),
                                      "sync")),
    ]
    try:
        await _relay.stop_event.wait()
    finally:
        for t in tasks:
            t.cancel()
        ws.close()
        try:
            await ws.wait_closed()
        except Exception:
            pass
        try:
            await store.snapshot()           # final durable snapshot
        except Exception as e:
            logger.warning("[nostr-relay] final snapshot failed: %s", e)
        store.close()
        logger.info("[nostr-relay] stopped")


async def _safe(coro):
    try:
        await coro
    except Exception as e:
        logger.warning("[nostr-relay] task error: %s", e)


async def _periodic(stop: asyncio.Event, interval: int, action, name: str) -> None:
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=max(5, interval))
        except asyncio.TimeoutError:
            pass
        if stop.is_set():
            break
        try:
            await action()
        except Exception as e:
            logger.warning("[nostr-relay] %s loop error: %s", name, e)


def _run(cfg: dict) -> None:
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    _relay.loop = loop
    try:
        loop.run_until_complete(_main(cfg))
    except Exception as e:
        logger.error("[nostr-relay] thread crashed: %s", e, exc_info=True)
    finally:
        try:
            loop.close()
        except Exception:
            pass
        _relay.loop = None


# --- public API -------------------------------------------------------------

def start_nostr_relay() -> None:
    if _relay.is_running():
        return
    cfg = _read_config()
    if not cfg["enabled"]:
        logger.info("[nostr-relay] disabled (nostr_relay_enabled off) — not starting")
        return
    _relay.cfg = cfg
    t = threading.Thread(target=_run, args=(cfg,), name="nostr-relay", daemon=True)
    _relay.thread = t
    t.start()
    logger.info("[nostr-relay] thread started")


def stop_nostr_relay() -> None:
    if not _relay.is_running() or _relay.loop is None or _relay.stop_event is None:
        return
    try:
        _relay.loop.call_soon_threadsafe(_relay.stop_event.set)
    except Exception:
        pass
    _relay.thread.join(timeout=15)
    _relay.thread = None
