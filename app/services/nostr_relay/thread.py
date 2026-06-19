"""Runs the built-in Nostr relay in its own daemon thread + asyncio event loop, fully
isolated from the main uvicorn request loop. Owns the websockets listener, the tmpfs
store, the WoT gate, and the periodic loops (WoT refresh, snapshot, prune; ingestion +
outbox are wired in later phases).

Public API: start_nostr_relay() / stop_nostr_relay(), called from app/main.py under the
port-3051 guard. Both are no-ops unless `nostr_relay_enabled` is set.
"""

import os
import sys
import time
import json
import glob
import uuid
import signal
import asyncio
import logging
import threading
import subprocess

from websockets.asyncio.server import serve

from app.services.nostr import nostr_service
from .store import RelayStore
from .wot import WotGate
from .server import RelayServer
from .bridges import relay_domain as _bridge_domain, reveals_blocked_bridge

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
        self.proc: subprocess.Popen | None = None   # the relay runs as a subprocess now
        self.loop: asyncio.AbstractEventLoop | None = None
        self.stop_event: asyncio.Event | None = None
        self.store: RelayStore | None = None
        self.gate: WotGate | None = None
        self.server: RelayServer | None = None
        self.outbox = None
        self.cfg: dict = {}

    def is_running(self) -> bool:
        return self.thread is not None and self.thread.is_alive()


# Cross-process IPC paths (the relay runs in its own subprocess; the app reads its status and
# drops admin commands via files). All derived from the relay DB path so they share its volume.
_DB_PATH_CACHE = ""


def _relay_db_path() -> str:
    global _DB_PATH_CACHE
    if _DB_PATH_CACHE:
        return _DB_PATH_CACHE
    p = ""
    try:
        from app.database import SessionLocal
        from app.models import Setting
        db = SessionLocal()
        try:
            row = db.query(Setting).filter(Setting.key == "nostr_relay_db_path").first()
            p = (row.value if row else "") or ""
        finally:
            db.close()
    except Exception:
        p = ""
    _DB_PATH_CACHE = p or os.path.join(_REPO_ROOT, "data", "nostr_relay.db")
    return _DB_PATH_CACHE


def _relay_paths(db_path: str) -> dict:
    return {"status": db_path + ".status.json", "control": db_path + ".control.d"}


def _pid_alive(pid) -> bool:
    try:
        os.kill(int(pid), 0)
        return True
    except Exception:
        return False


# Set when the app is shutting the relay down on purpose, so the watchdog doesn't respawn it
# in the middle of a clean shutdown. `_relay_lock` serializes all lifecycle ops (start/stop/
# restart/watchdog) so they can't race into a double-spawn (→ EADDRINUSE → respawn loop).
_relay_shutdown = False
_monitor_thread: threading.Thread | None = None
_relay_lock = threading.RLock()
# Watchdog respawn rate-limit: a single crash respawns instantly, but a relay that keeps
# crashing is left DOWN (with a loud error) instead of being hammered every 15s forever.
_RESPAWN_WINDOW = 600   # seconds
_RESPAWN_MAX = 5        # max respawns per window before backing off
_respawn_times: list = []


_relay = _Relay()


# --- settings ---------------------------------------------------------------

# Default NIP-05 identities — the entries this deployment already served from router.lan's
# static nostr.json, baked in so they keep resolving out of the box (editable in Admin → Relay).
_DEFAULT_NIP05_NAMES = (
    "verita84 4b56bbf41c92e586e88927acb78836eb49f2b184081ef852625cf78be7d56bd6\n"
    "posterchan c7de13bab5818ab7918b5b47a05de11735c4e519e49c8577fd7ce7267fe84d4b"
)
_DEFAULT_NIP05_RELAYS = "wss://relay.poster.place"


def _parse_nip05(names_raw: str, relays_raw: str):
    """Parse the admin NIP-05 settings into (names: {name->hex}, relays: [url,...]).
    Each names line is "<name> <npub-or-hex>" (also tolerates "name=hex" / commas); blank
    and #-comment lines are skipped. Relays are a shared list advertised for every name."""
    names = {}
    for line in (names_raw or "").split("\n"):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        toks = line.replace("=", " ").replace(",", " ").split()
        if len(toks) < 2:
            continue
        pk = nostr_service.to_pubkey_hex(toks[1].strip())
        if toks[0] and pk:
            names[toks[0].strip()] = pk
    relays = nostr_service.relay.normalize_relays(relays_raw or "")
    return names, relays


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
            # How many upstream relays the firehose streams from (0 = ALL). It's the sole
            # real-time ingestion path now, so default to all for completeness.
            "firehose_max_relays": gi("nostr_relay_firehose_max_relays", 0),
            # Heavy backfill SWEEP (per-member crawl of the whole trust graph). OFF: the firehose
            # streams + filters in real time; the sweep is the laggy "mirror their feeds" crawl.
            "mirror_feeds": gb("nostr_relay_mirror_feeds", False),
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
            # profile (0), contacts (3), notes (1), reposts (6), reactions (7), zap receipts (9735),
            # NIP-22 comments (1111), NIP-65 relay list (10002), NIP-23 long-form (30023), NIP-53
            # live events (30311). Including 0/3/10002 lets the firehose stream WoT members' IDENTITY
            # metadata too, so the relay serves profiles without a separate fetch — same
            # stream-and-filter path, no extra crawl. (30311 powers the client's Streams view.)
            "ingest_kinds": [int(k) for k in (g("nostr_relay_ingest_kinds", "0,1,3,6,7,1111,9735,10002,30023,30311")
                             .replace(" ", "").split(",")) if k.strip().lstrip("-").isdigit()],
            "author_batch": gi("nostr_relay_author_batch", 200),
            # Politeness / anti-blast: pace upstream requests and outbox publishes so we don't
            # hammer the public relays and get rate-limited or blocked.
            "request_pace_sec": float(g("nostr_relay_request_pace_sec", "1.0") or 1.0),
            "outbox_min_interval": float(g("nostr_relay_outbox_min_interval_sec", "1.0") or 1.0),
            "outbox_max_queue": gi("nostr_relay_outbox_max_queue", 500),
            # Re-send to relays that missed an event (down/handshake-timeout on the first pass)
            # a few times, then give up. Covers the gap right after a relay restart.
            "outbox_retries": gi("nostr_relay_outbox_retries", 2),
            "outbox_retry_delay": float(g("nostr_relay_outbox_retry_delay_sec", "15.0") or 15.0),
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
            # Bridge/relay domains to block (mostr.pub, brid.gy, …). Accounts whose profile nip05 or
            # relay list lives on one of these are denied + purged (suffix match → covers subdomains).
            "blocked_relays": {d for d in
                               (_bridge_domain(t) for t in
                                g("nostr_relay_blocked_relays", "").replace(",", "\n").split())
                               if d},
            # Built-in NIP-05 identity server (served over HTTP by the relay subprocess at
            # /.well-known/nostr.json). Defaults preserve the entries previously on router.lan.
            "nip05": {
                "enabled": gb("nostr_relay_nip05_enabled", True),
                **dict(zip(("names", "relays"), _parse_nip05(
                    g("nostr_relay_nip05_names", _DEFAULT_NIP05_NAMES),
                    g("nostr_relay_nip05_relays", _DEFAULT_NIP05_RELAYS)))),
            },
            # NIP-11 metadata
            "name": g("nostr_relay_name", "PosterChanAI Relay"),
            "description": g("nostr_relay_description", "Web-of-trust relay"),
            "pubkey": nostr_service.to_pubkey_hex(g("nostr_relay_pubkey", "")) or "",
            "contact": g("nostr_relay_contact", ""),
            "icon": g("nostr_relay_icon", ""),   # blank = PosterChan mascot from this host
            # Advertise NIP-11 restricted_writes. Off by default: outbox-model clients
            # (Yakihonne) otherwise refuse a restricted relay as a sole write target and
            # inject their defaults, resetting a single-relay NIP-65 list. WoT gate still
            # enforces writes at runtime either way.
            "advertise_restricted_writes": gb("nostr_relay_advertise_restricted_writes", False),
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
    store.set_preserve_pubkeys(cfg["operator"])   # local users' notes are never pruned
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
    if cfg["blocked_langs"]:
        rl = await store.delete_by_langs(cfg["blocked_langs"])
        if rl:
            logger.info("[nostr-relay] purged %d stored note(s) in %d blocked language(s)",
                        rl, len(cfg["blocked_langs"]))
    if cfg["blocked_relays"]:
        await _apply_blocked_relays(store, gate, cfg["blocked_relays"])
    from .outbox import Outbox
    outbox = Outbox(cfg["upstream"], min_interval=cfg["outbox_min_interval"],
                    maxsize=cfg["outbox_max_queue"], direct=cfg["direct"],
                    retries=cfg["outbox_retries"], retry_delay=cfg["outbox_retry_delay"])
    outbox.start()
    server = RelayServer(store, gate, cfg, outbox_cb=outbox.enqueue)
    _relay.store, _relay.gate, _relay.server, _relay.outbox = store, gate, server, outbox
    _relay.stop_event = asyncio.Event()

    # WoT build cadence: ONCE A DAY. The gate is already warm from the snapshot
    # (load_from_store above), so on a (re)start we only re-crawl the 37k follow graph if it's
    # been >= a day since the last successful build. Otherwise a frequent restart (every deploy)
    # would re-crawl every time and peg a core — that's the churn. Build on startup only when
    # the cache is empty or stale; the daily refresh below maintains it after that.
    if not gate.members() or _wot_stale(cfg):
        asyncio.create_task(_initial_wot_build(gate, store, cfg, _relay.stop_event))
    else:
        logger.info("[nostr-relay] WoT warm from snapshot (%d members) — last build %dh ago, "
                    "skipping rebuild (daily cadence)",
                    len(gate.members()), int((time.time() - _read_wot_stamp(cfg)) / 3600))

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
        Non-WoT (incl. blocklisted/bridged) pubkeys are dropped by is_member BEFORE any verify/DB work."""
        # Learn bridge accounts as their profile/relay-list streams by: mark + drop so every event
        # they author is then rejected by the is_member gate below. Read the list live (reloadable).
        _br = cfg.get("blocked_relays")
        if _br and reveals_blocked_bridge(ev, _br):
            gate.mark_bridged(ev.get("pubkey", ""))
            return
        _kind = int(ev.get("kind", 1))
        if _kind in (9735, 1059):
            # Zap receipts (9735) are authored by the LNURL zap SERVICE and gift wraps (1059) by a
            # throwaway ephemeral key — neither author is in the WoT, so gate on the RECIPIENT p-tag
            # instead (same as the WS write path). Without this the firehose drops every zap, so
            # posts never show a zap total.
            if not any(len(t) >= 2 and t[0] == "p" and gate.is_member(t[1]) for t in (ev.get("tags") or [])):
                return
        elif not gate.is_member(ev.get("pubkey", "")):
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
            server.subs.fanout(ev, server._send)
            # Thread completion: a reply may e-tag parents we don't have. Backfill the ancestor
            # chain (bounded + deduped, parents may be outside the WoT → origin='ancestor') so
            # threads aren't orphaned. This used to run in the sync sweep (off by default now), so
            # the firehose has to do it. Only for replies; mostly a no-op (parents already stored).
            if cfg.get("fetch_ancestors", True) and any(
                    t and len(t) >= 2 and t[0] == "e" for t in (ev.get("tags") or [])):
                from . import ingest as _ingest
                try:
                    await _ingest.backfill_ancestors(
                        store, server, cfg["upstream"], [ev], cfg.get("max_ancestors", 20),
                        cfg["direct"], blocked=_bl, blocked_words=_bw, gate=gate)
                except Exception as e:
                    logger.debug("[nostr-relay] firehose ancestor backfill failed: %s", e)

    async def _maybe_rebuild_wot():
        # Rebuild the WoT (write-gate membership) at most ONCE A DAY — the stamp decides, not the
        # timer, so frequent restarts (which reset timers) can't trigger extra 37k crawls.
        if _wot_stale(cfg):
            logger.info("[nostr-relay] daily WoT refresh (>= %dh since last build) — rebuilding",
                        int(cfg["wot_refresh_sec"] / 3600))
            await _build_wot(gate, store, cfg)

    tasks = [
        asyncio.create_task(_periodic(_relay.stop_event, 3600, store.prune, "prune")),
        # WoT rebuild — checked hourly, actually rebuilt only when a day has elapsed (staleness),
        # so it runs once a day regardless of restarts. NOT a feed mirror; just gate membership.
        asyncio.create_task(_periodic(_relay.stop_event, 3600, _maybe_rebuild_wot, "wot-refresh")),
    ]
    # Profile/metadata backfill — runs INDEPENDENTLY of the (off-by-default) heavy sync sweep, so
    # clients can resolve names/avatars even when only the firehose is on. Fetches kind-0/3/10002
    # for WoT members missing them (note-authors prioritized in store.wot_missing_metadata). Paced.
    async def _meta_backfill():
        from . import ingest as _ingest
        await _ingest.fetch_lookup_metadata(store, cfg["upstream"], cfg["author_batch"],
                                             cfg.get("profile_limit", 1500), cfg["request_pace_sec"], cfg["direct"],
                                             gate=gate, blocked_relays=cfg.get("blocked_relays"))
    tasks.append(asyncio.create_task(_periodic(_relay.stop_event, 45, _meta_backfill, "metadata-backfill")))
    # Heavy WoT BACKFILL SWEEP — OFF by default. This is the windowed crawl that walks the WHOLE
    # trust graph (tens of thousands of members via depth-2 FoF) re-fetching history; it pegs a
    # core and lags. That's the "mirror their feeds" machinery we don't want by default.
    if cfg.get("mirror_feeds", False):
        tasks.append(asyncio.create_task(_sync_loop(store, gate, server, cfg, _relay.stop_event)))
    # Live FIREHOSE — ON by default. Lightweight: a real-time subscription that keeps WoT-author
    # events as they're published (so the relay shows NEW posts), NOT a backfill crawl. This is
    # how fresh content arrives without the heavy sweep above.
    if cfg.get("firehose_enabled", True):
        from .firehose import run_firehose
        tasks.append(asyncio.create_task(
            run_firehose(cfg["upstream"], cfg["ingest_kinds"], _firehose_event,
                         _relay.stop_event, cfg["direct"],
                         max_relays=cfg.get("firehose_max_relays", 0))))

    # Cross-process admin IPC: this relay runs in its own subprocess, so the app can't read its
    # gate/store directly. Publish status to a file the app polls, and execute admin commands the
    # app drops into a control dir (Refresh-WoT / Backfill buttons). No-ops if launched in-thread.
    _paths = _relay_paths(cfg["db_path"])
    os.makedirs(_paths["control"], exist_ok=True)

    async def _status_writer():
        while not _relay.stop_event.is_set():
            try:
                tmp = _paths["status"] + ".tmp"
                with open(tmp, "w") as f:
                    json.dump({"running": True, "members": len(gate.members()),
                               "pid": os.getpid(), "ts": int(time.time())}, f)
                os.replace(tmp, _paths["status"])
            except Exception:
                pass
            try:
                await asyncio.wait_for(_relay.stop_event.wait(), timeout=15)
            except asyncio.TimeoutError:
                pass

    async def _control_poller():
        while not _relay.stop_event.is_set():
            try:
                for cf in sorted(glob.glob(os.path.join(_paths["control"], "cmd_*.json"))):
                    try:
                        with open(cf) as f:
                            cmd = json.load(f)
                    except Exception:
                        cmd = {}
                    try:
                        os.remove(cf)
                    except Exception:
                        pass
                    if cmd.get("cmd") == "refresh-wot":
                        logger.info("[nostr-relay] control: WoT refresh requested")
                        asyncio.create_task(_safe(_build_wot(gate, store, cfg)))
                    elif cmd.get("cmd") == "reload-blocks":
                        # Re-read the blocklist from the DB (admin edited it via /client/block or
                        # the admin UI), apply it to the gate, and purge the blocked authors' events.
                        try:
                            fresh = _read_config()
                            cfg["blocked_pubkeys"] = fresh["blocked_pubkeys"]
                            gate.set_blocked(cfg["blocked_pubkeys"])
                            if cfg["blocked_pubkeys"]:
                                asyncio.create_task(_safe(store.delete_pubkeys(cfg["blocked_pubkeys"])))
                            # Bridge/relay blocklist: re-read (firehose reads cfg live), scan + purge.
                            cfg["blocked_relays"] = fresh["blocked_relays"]
                            if cfg["blocked_relays"]:
                                asyncio.create_task(_safe(_apply_blocked_relays(store, gate, cfg["blocked_relays"])))
                            logger.info("[nostr-relay] control: reloaded %d blocked pubkey(s), %d bridge domain(s)",
                                        len(cfg["blocked_pubkeys"]), len(cfg["blocked_relays"]))
                        except Exception as e:
                            logger.warning("[nostr-relay] reload-blocks failed: %s", e)
                    elif cmd.get("cmd") == "reload-nip05":
                        # Admin edited the NIP-05 identities — re-read and swap in place (the
                        # server reads cfg["nip05"] live, so no restart needed).
                        try:
                            cfg["nip05"] = _read_config()["nip05"]
                            logger.info("[nostr-relay] control: reloaded %d NIP-05 name(s)",
                                        len(cfg["nip05"].get("names") or {}))
                        except Exception as e:
                            logger.warning("[nostr-relay] reload-nip05 failed: %s", e)
                    elif cmd.get("cmd") == "backfill" and cmd.get("pubkey"):
                        logger.info("[nostr-relay] control: backfill %s", cmd["pubkey"][:12])
                        from . import ingest as _ingest
                        asyncio.create_task(_safe(_ingest.backfill_author(
                            store, server, cfg["upstream"], cmd["pubkey"],
                            direct=cfg["direct"], pace=cfg["request_pace_sec"])))
            except Exception as e:
                logger.debug("[nostr-relay] control poll error: %s", e)
            try:
                await asyncio.wait_for(_relay.stop_event.wait(), timeout=5)
            except asyncio.TimeoutError:
                pass

    tasks.append(asyncio.create_task(_status_writer()))
    tasks.append(asyncio.create_task(_control_poller()))
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
        try:
            os.remove(_paths["status"])           # don't leave a stale "running" status behind
        except Exception:
            pass
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


def _wot_stamp_path(cfg) -> str:
    return (cfg.get("db_path") or "nostr_relay.db") + ".wot_built"


def _read_wot_stamp(cfg) -> float:
    """Unix time of the last successful WoT build (0 if never / unreadable)."""
    try:
        with open(_wot_stamp_path(cfg)) as f:
            return float(f.read().strip())
    except Exception:
        return 0.0


def _write_wot_stamp(cfg) -> None:
    try:
        with open(_wot_stamp_path(cfg), "w") as f:
            f.write(str(int(time.time())))
    except Exception as e:
        logger.debug("[nostr-relay] could not write WoT stamp: %s", e)


def _wot_stale(cfg) -> bool:
    """True if it's been >= the daily refresh interval since the last successful build, so a
    restart should rebuild. Within the interval, a restart reuses the snapshot-warmed gate."""
    return (time.time() - _read_wot_stamp(cfg)) >= cfg.get("wot_refresh_sec", 86400)


async def _build_wot(gate, store, cfg) -> int:
    """Single entry point so every WoT build (initial / daily / manual) uses the same
    depth, pacing and caps. Records the build time so restarts honour the daily cadence."""
    n = await gate.build(
        store, cfg["upstream"], cfg["seeds"],
        depth=cfg["wot_depth"], direct=cfg["direct"],
        batch=cfg["author_batch"], pace=cfg["request_pace_sec"],
        min_followers=cfg["wot_min_followers"], max_members=cfg["wot_max"])
    if n > len(set(cfg["seeds"]) | set(cfg["operator"])):  # follows actually resolved
        _write_wot_stamp(cfg)
    return n


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

def _spawn_relay(cfg: dict) -> None:
    """Spawn the relay as a child subprocess — own OS process → own GIL/core, so its firehose
    parsing no longer steals CPU from the app. It's in the app's cgroup, so `systemctl restart`
    takes it down with the app and the next start respawns it (code changes apply on deploy);
    stdout/stderr are inherited so its logs land in the journal."""
    global _DB_PATH_CACHE
    _DB_PATH_CACHE = cfg["db_path"]
    entry = os.path.join(_REPO_ROOT, "relay_main.py")
    _relay.proc = subprocess.Popen([sys.executable, entry], cwd=_REPO_ROOT)
    logger.info("[nostr-relay] spawned relay subprocess pid %d", _relay.proc.pid)


def _monitor_loop() -> None:
    """Watchdog: respawn the relay subprocess if it dies (crash / OOM), unless we're shutting it
    down on purpose. Mirrors bot_manager's reconcile so a relay crash isn't a silent outage."""
    while not _relay_shutdown:
        time.sleep(15)
        if _relay_shutdown:
            break
        try:
            with _relay_lock:
                if _relay_shutdown:
                    break
                if _relay.proc is not None and _relay.proc.poll() is None:
                    continue  # alive
                cfg = _relay.cfg or _read_config()
                if not cfg.get("enabled"):
                    continue
                now = time.time()
                _respawn_times[:] = [t for t in _respawn_times if now - t < _RESPAWN_WINDOW]
                if len(_respawn_times) >= _RESPAWN_MAX:
                    logger.error("[nostr-relay] relay crashed %d× in %dm — backing off, NOT "
                                 "respawning (fix the relay)", len(_respawn_times),
                                 _RESPAWN_WINDOW // 60)
                    continue
                _respawn_times.append(now)
                logger.warning("[nostr-relay] subprocess not running — respawning (watchdog)")
                _relay.cfg = cfg
                _spawn_relay(cfg)
        except Exception as e:
            logger.debug("[nostr-relay] watchdog error: %s", e)


def start_nostr_relay() -> None:
    global _relay_shutdown, _monitor_thread
    with _relay_lock:
        if _relay.proc is not None and _relay.proc.poll() is None:
            return  # already running
        cfg = _read_config()
        if not cfg["enabled"]:
            logger.info("[nostr-relay] disabled (nostr_relay_enabled off) — not starting")
            return
        _relay_shutdown = False
        _relay.cfg = cfg
        _spawn_relay(cfg)
        if _monitor_thread is None or not _monitor_thread.is_alive():
            _monitor_thread = threading.Thread(target=_monitor_loop, name="nostr-relay-monitor",
                                               daemon=True)
            _monitor_thread.start()


def stop_nostr_relay() -> None:
    global _relay_shutdown
    with _relay_lock:
        _relay_shutdown = True           # tell the watchdog not to respawn
        proc = _relay.proc
        if proc is None or proc.poll() is not None:
            return
        try:
            proc.terminate()             # SIGTERM → graceful shutdown + snapshot in relay_main
        except Exception:
            pass
        try:
            proc.wait(timeout=15)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
        _relay.proc = None


def restart_nostr_relay() -> dict:
    """Stop the relay subprocess and spawn a fresh one — used to pick up relay code changes
    (the relay otherwise keeps running across the app's own internal restarts)."""
    global _relay_shutdown
    with _relay_lock:
        stop_nostr_relay()
        cfg = _read_config()
        if not cfg["enabled"]:
            return {"ok": False, "error": "relay disabled"}
        _relay_shutdown = False          # re-arm the watchdog
        _relay.cfg = cfg
        _spawn_relay(cfg)
    return {"ok": True, "restarted": True}


def relay_status() -> dict:
    """Status for the Admin UI: liveness from the subprocess handle, with a fallback to the
    status file (its pid alive + recent ts) so it's correct from any caller; member count comes
    from that file since the gate lives in the relay's own process now."""
    alive = _relay.proc is not None and _relay.proc.poll() is None
    members = 0
    try:
        with open(_relay_paths(_relay_db_path())["status"]) as f:
            st = json.load(f)
        members = int(st.get("members", 0))
        if not alive:
            alive = (time.time() - st.get("ts", 0)) < 90 and _pid_alive(st.get("pid"))
    except Exception:
        pass
    return {"running": bool(alive), "members": members}


def _drop_control(cmd: dict) -> dict:
    """Hand an admin command to the relay subprocess via its control dir (its poller picks it
    up, executes on the relay loop). Atomic write so the poller never sees a partial file."""
    if _relay.proc is None or _relay.proc.poll() is not None:
        return {"ok": False, "error": "relay not running"}
    try:
        ctrl = _relay_paths(_relay_db_path())["control"]
        os.makedirs(ctrl, exist_ok=True)
        fn = os.path.join(ctrl, "cmd_%d_%s.json" % (int(time.time()), uuid.uuid4().hex[:8]))
        tmp = fn + ".tmp"
        with open(tmp, "w") as f:
            json.dump(cmd, f)
        os.replace(tmp, fn)
        return {"ok": True, "started": True}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def trigger_wot_refresh() -> dict:
    """Admin button: ask the relay subprocess to rebuild the WoT now (it polls /status after)."""
    return _drop_control({"cmd": "refresh-wot"})


def trigger_backfill(pubkey_hex: str) -> dict:
    """Admin button: ask the relay subprocess to backfill an author's history into the store."""
    if not pubkey_hex:
        return {"ok": False, "error": "no nostr key on your account"}
    return _drop_control({"cmd": "backfill", "pubkey": pubkey_hex})


async def _apply_blocked_relays(store, gate, domains) -> None:
    """Find accounts living on the blocked bridge domains, add them to the gate's bridge denylist,
    and purge their stored events. Safe to call repeatedly (startup + on a live blocklist reload)."""
    try:
        pks = await store.bridged_pubkeys(domains)
    except Exception as e:
        logger.warning("[nostr-relay] bridge scan failed: %s", e)
        return
    if not pks:
        return
    gate.add_bridged(pks)
    try:
        removed = await store.delete_pubkeys(list(pks))
    except Exception as e:
        removed = 0
        logger.warning("[nostr-relay] bridge purge failed: %s", e)
    logger.info("[nostr-relay] blocked %d bridged account(s) on %d relay domain(s), purged %d event(s)",
                len(pks), len(domains), removed)


def trigger_block_reload() -> dict:
    """Re-apply the nostr_relay_blocked_pubkeys denylist (gate + purge) without a restart."""
    return _drop_control({"cmd": "reload-blocks"})


def trigger_nip05_reload() -> dict:
    """Re-read the NIP-05 identities (Admin → Relay) into the running relay without a restart."""
    return _drop_control({"cmd": "reload-nip05"})
