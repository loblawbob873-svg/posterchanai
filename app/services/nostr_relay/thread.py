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

# Per-connection protocol limits (constants — tune in code, not user-facing). Generous so
# feature-rich clients (which open many simultaneous subscriptions for feed/notifs/profiles)
# don't hit the cap and get a CLOSED, which some clients render as an unhealthy/red relay.
_MAX_MESSAGE_SIZE = 512 * 1024
_MAX_SUBS_PER_CONN = 500
_MAX_FILTERS_PER_REQ = 25


class _Relay:
    def __init__(self):
        self.thread: threading.Thread | None = None
        self.loop: asyncio.AbstractEventLoop | None = None
        self.stop_event: asyncio.Event | None = None
        self.store: RelayStore | None = None
        self.gate: WotGate | None = None
        self.server: RelayServer | None = None
        self.outbox = None
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

        # The DB lives on disk in WAL mode — durable by itself, scales to many GB, OS page
        # cache + a big mmap window keep hot pages in RAM.
        db_path = g("nostr_relay_db_path", os.path.join(_REPO_ROOT, "data", "nostr_relay.db"))

        cfg = {
            "enabled": gb("nostr_relay_enabled", False),
            "bind": g("nostr_relay_bind", "127.0.0.1"),
            "port": gi("nostr_relay_port", 3052),
            "seeds": seeds,
            "upstream": upstream,
            # Bypass the outbound Tor proxy for the relay's OWN upstream traffic (sync/outbox/
            # WoT). Direct is faster and avoids the proxy-startup-race log flood; doesn't change
            # the bots' proxy behavior.
            "direct": gb("nostr_relay_disable_proxy", False),
            # Live firehose: keep a persistent subscription to each upstream relay and store
            # only WoT authors — real-time, vs the polling sweep's per-cycle lag.
            "firehose_enabled": gb("nostr_relay_firehose_enabled", True),
            "db_path": db_path,
            "retention_days": gi("nostr_relay_retention_days", 30),
            "max_events": gi("nostr_relay_max_events", 500000),
            "max_db_mb": gi("nostr_relay_max_db_mb", 1024),
            "wal_pages": gi("nostr_relay_wal_autocheckpoint", 50000),  # ~200MB WAL before checkpoint
            # Nostr is read/write intense — default to generous RAM caches (negative cache_size
            # = KiB; mmap serves reads with zero syscalls). Tunable in Admin → Relay.
            "cache_mb": gi("nostr_relay_cache_mb", 512),               # SQLite page cache
            "mmap_mb": gi("nostr_relay_mmap_mb", 4096),                # SQLite mmap read window
            "sync_budget_sec": gi("nostr_relay_sync_budget_sec", 100), # per-tick sync work budget
            "wot_refresh_sec": gi("nostr_relay_wot_refresh_sec", 86400),  # daily
            "wot_depth": gi("nostr_relay_wot_depth", 1),                  # 1=follows, 2=+FoF
            "wot_min_followers": gi("nostr_relay_wot_min_followers", 2),  # FoF inclusion threshold
            "wot_max": gi("nostr_relay_wot_max", 50000),                  # cap on total members
            "max_connections": gi("nostr_relay_max_connections", 5000),
            # windowed ingestion
            "sync_window_sec": gi("nostr_relay_sync_window_sec", 600),
            "sync_interval_sec": gi("nostr_relay_sync_interval_sec", 120),
            # Once caught up (firehose handles freshness), the sweep backs off to this so we
            # stop sending unnecessary upstream requests.
            "sync_idle_interval_sec": gi("nostr_relay_sync_idle_interval_sec", 1800),
            "backfill_sec": gi("nostr_relay_backfill_hours", 48) * 3600,  # initial history depth
            "overlap_sec": gi("nostr_relay_overlap_sec", 120),
            "ingest_kinds": [int(k) for k in (g("nostr_relay_ingest_kinds", "1,6,7")
                             .replace(" ", "").split(",")) if k.strip().lstrip("-").isdigit()],
            "author_batch": gi("nostr_relay_author_batch", 200),
            # Politeness / anti-blast: pace upstream requests and outbox publishes so we don't
            # hammer the public relays and get rate-limited or blocked.
            "request_pace_sec": float(g("nostr_relay_request_pace_sec", "1.0") or 1.0),
            "outbox_min_interval": float(g("nostr_relay_outbox_min_interval_sec", "1.0") or 1.0),
            "outbox_max_queue": gi("nostr_relay_outbox_max_queue", 500),
            "fetch_ancestors": gb("nostr_relay_fetch_ancestors", True),
            "max_ancestors": gi("nostr_relay_max_ancestors", 20),
            "blocked_langs": {x.strip() for x in g("nostr_relay_blocked_langs", "")
                              .replace(",", " ").split() if x.strip()},
            # Reject notes whose text contains any of these words/phrases (case-insensitive
            # substring). One per line so phrases with spaces work.
            "blocked_words": {w.strip().lower() for w in g("nostr_relay_blocked_words", "")
                              .split("\n") if w.strip()},
            # Hard denylist of pubkeys (npub/hex) — rejected even if in the WoT, and their
            # existing notes are purged on startup.
            "blocked_pubkeys": [pk for pk in
                                (nostr_service.to_pubkey_hex(t.strip()) for t in
                                 g("nostr_relay_blocked_pubkeys", "").replace(",", "\n").split())
                                if pk],
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
        cfg["db_path"],
        max_events=cfg["max_events"], retention_days=cfg["retention_days"],
        max_db_mb=cfg["max_db_mb"], wal_pages=cfg["wal_pages"],
        cache_mb=cfg["cache_mb"], mmap_mb=cfg["mmap_mb"])
    loop = asyncio.get_running_loop()
    store.open(loop)
    gate = WotGate()
    gate.set_operator(cfg["operator"])
    gate.set_blocked(cfg["blocked_pubkeys"])
    await gate.load_from_store(store)              # warm from snapshot for immediate gating
    if cfg["blocked_pubkeys"]:
        removed = await store.delete_pubkeys(cfg["blocked_pubkeys"])
        logger.info("[nostr-relay] purged %d events from %d blocklisted pubkey(s)",
                    removed, len(cfg["blocked_pubkeys"]))
    if cfg["blocked_words"]:
        rw = await store.delete_by_words(cfg["blocked_words"])
        if rw:
            logger.info("[nostr-relay] purged %d stored note(s) matching %d blocked word(s)",
                        rw, len(cfg["blocked_words"]))
    from .outbox import Outbox
    outbox = Outbox(cfg["upstream"], min_interval=cfg["outbox_min_interval"],
                    maxsize=cfg["outbox_max_queue"], direct=cfg["direct"])
    outbox.start()
    server = RelayServer(store, gate, cfg, outbox_cb=outbox.enqueue)
    _relay.store, _relay.gate, _relay.server, _relay.outbox = store, gate, server, outbox
    _relay.stop_event = asyncio.Event()

    # Initial WoT build, retried with backoff: at startup the built-in HTTP proxy (Tor) the
    # upstream client routes through may not be listening yet, so the first build can resolve
    # only the seeds. Keep retrying until the follow graph actually comes through, then the
    # daily refresh maintains it.
    asyncio.create_task(_initial_wot_build(gate, store, cfg, _relay.stop_event))

    ws = await serve(
        server.handle, cfg["bind"], cfg["port"],
        process_request=server.process_request,
        max_size=cfg["max_message_size"],
        ping_interval=30, ping_timeout=30, max_queue=64,
    )
    logger.info("[nostr-relay] listening on ws://%s:%d/relay (operator=%d, seeds=%d)",
                cfg["bind"], cfg["port"], len(cfg["operator"]), len(cfg["seeds"]))

    from app.services.nostr.event import verify_event
    from .langfilter import blocked_language, blocked_word
    _bl, _bw = cfg["blocked_langs"], cfg["blocked_words"]

    async def _firehose_event(ev):
        """Apply the full WoT + filter chain to a live firehose event, store + fan out if kept.
        Non-WoT (incl. blocklisted) pubkeys are dropped by is_member BEFORE any verify/DB work."""
        if not gate.is_member(ev.get("pubkey", "")):
            return
        eid = ev.get("id")
        if not isinstance(eid, str) or len(eid) != 64:
            return
        if await store.has_event(eid):
            return
        if not verify_event(ev):
            return
        if int(ev.get("kind", 1)) == 1:
            content = ev.get("content", "")
            if (_bl and blocked_language(content, _bl)) or (_bw and blocked_word(content, _bw)):
                return
        if await store.add_event(ev, origin="wot"):
            await server.subs.fanout(ev)

    tasks = [
        asyncio.create_task(_periodic(_relay.stop_event, 3600, store.prune, "prune")),
        # Daily WoT rebuild from the seeds' follow lists.
        asyncio.create_task(_periodic(_relay.stop_event, cfg["wot_refresh_sec"],
                                      lambda: _build_wot(gate, store, cfg),
                                      "wot-refresh")),
        # Windowed WoT sweep — now just BACKFILL/gap-fill (the firehose handles freshness).
        # Self-throttling: runs often while it's still finding history, then backs WAY off
        # once caught up so we stop hammering the upstream relays.
        asyncio.create_task(_sync_loop(store, gate, server, cfg, _relay.stop_event)),
    ]
    # Live firehose: real-time WoT sync — keep only WoT authors from the upstream stream.
    if cfg.get("firehose_enabled", True):
        from .firehose import run_firehose
        tasks.append(asyncio.create_task(
            run_firehose(cfg["upstream"], cfg["ingest_kinds"], _firehose_event,
                         _relay.stop_event, cfg["direct"])))
    try:
        await _relay.stop_event.wait()
    finally:
        for t in tasks:
            t.cancel()
        outbox.stop()
        ws.close()
        try:
            await ws.wait_closed()
        except Exception:
            pass
        try:
            await store.checkpoint()             # fold WAL into the main DB on clean shutdown
        except Exception as e:
            logger.warning("[nostr-relay] final checkpoint failed: %s", e)
        store.close()
        logger.info("[nostr-relay] stopped")


async def _safe(coro):
    try:
        await coro
    except Exception as e:
        logger.warning("[nostr-relay] task error: %s", e)


async def _sync_loop(store, gate, server, cfg, stop: asyncio.Event) -> None:
    """Backfill/gap-fill sweep that THROTTLES itself by upstream request load: it runs at the
    busy interval while it's still pulling history, then backs off to the idle interval once
    caught up (the firehose keeps things fresh), so we stop sending requests we don't need."""
    from . import ingest as _ingest
    busy = max(30, cfg["sync_interval_sec"])
    idle = max(busy, cfg.get("sync_idle_interval_sec", 1800))
    while not stop.is_set():
        try:
            n = await _ingest.sync_tick(store, gate, server, cfg["upstream"], cfg)
        except Exception as e:
            logger.warning("[nostr-relay] sync error: %s", e)
            n = 0
        delay = busy if n > 10 else idle   # still finding history → keep going; else back off
        try:
            await asyncio.wait_for(stop.wait(), timeout=delay)
            return
        except asyncio.TimeoutError:
            pass


async def _build_wot(gate, store, cfg) -> int:
    """Single entry point so every WoT build (initial / daily / manual) uses the same
    depth, pacing and caps."""
    return await gate.build(
        store, cfg["upstream"], cfg["seeds"],
        depth=cfg["wot_depth"], direct=cfg["direct"],
        batch=cfg["author_batch"], pace=cfg["request_pace_sec"],
        min_followers=cfg["wot_min_followers"], max_members=cfg["wot_max"])


async def _initial_wot_build(gate, store, cfg, stop: asyncio.Event) -> None:
    """Build the WoT, retrying with backoff until the follow graph resolves (i.e. the
    outbound HTTP proxy + relays are reachable). Without this, a startup race with the
    proxy leaves the trust set stuck at seeds-only until the next daily rebuild."""
    base = len(set(cfg["seeds"]) | set(cfg["operator"]))
    delay = 20
    for _ in range(40):
        if stop.is_set():
            return
        try:
            n = await _build_wot(gate, store, cfg)
        except Exception as e:
            logger.warning("[nostr-relay] initial WoT build error: %s", e)
            n = 0
        if n > base:  # follows came through → proxy/relays reachable
            logger.info("[nostr-relay] initial WoT ready: %d members", n)
            return
        logger.info("[nostr-relay] WoT seeds-only (%d) — proxy/relays not ready, retry in %ds",
                    n, delay)
        try:
            await asyncio.wait_for(stop.wait(), timeout=delay)
            return  # stop requested
        except asyncio.TimeoutError:
            pass
        delay = min(int(delay * 1.5), 300)


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


def trigger_wot_refresh() -> dict:
    """Kick off a WoT rebuild now (Admin button). Fire-and-forget on the relay's own loop —
    a depth-2 (friends-of-friends) build can take minutes, so we don't block; the UI polls
    /status for the updated member count."""
    if not _relay.is_running() or _relay.loop is None or _relay.gate is None or _relay.store is None:
        return {"ok": False, "error": "relay not running"}
    cfg = _relay.cfg
    try:
        asyncio.run_coroutine_threadsafe(
            _safe(_build_wot(_relay.gate, _relay.store, cfg)), _relay.loop)
        return {"ok": True, "started": True, "members": len(_relay.gate.members())}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def trigger_backfill(pubkey_hex: str) -> dict:
    """Backfill an author's full history into the relay (Admin button). Fire-and-forget on the
    relay loop; writes straight to the store so the outbox does NOT re-broadcast old posts."""
    if not _relay.is_running() or _relay.loop is None or _relay.store is None or _relay.server is None:
        return {"ok": False, "error": "relay not running"}
    if not pubkey_hex:
        return {"ok": False, "error": "no nostr key on your account"}
    cfg = _relay.cfg

    async def _run():
        from . import ingest as _ingest
        await _ingest.backfill_author(
            _relay.store, _relay.server, cfg["upstream"], pubkey_hex,
            direct=cfg["direct"], pace=cfg["request_pace_sec"])

    try:
        asyncio.run_coroutine_threadsafe(_safe(_run()), _relay.loop)
        return {"ok": True, "started": True}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def relay_status() -> dict:
    """Lightweight status for the Admin UI."""
    if not _relay.is_running() or _relay.gate is None:
        return {"running": False, "members": 0}
    return {"running": True, "members": len(_relay.gate.members())}


def stop_nostr_relay() -> None:
    if not _relay.is_running() or _relay.loop is None or _relay.stop_event is None:
        return
    try:
        _relay.loop.call_soon_threadsafe(_relay.stop_event.set)
    except Exception:
        pass
    _relay.thread.join(timeout=15)
    _relay.thread = None
