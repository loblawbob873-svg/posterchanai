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
import datetime
import json
import glob
import uuid
import signal
import asyncio
from collections import deque
import logging
import threading
import subprocess

from websockets.asyncio.server import serve

from app.services.nostr import nostr_service
from .store import RelayStore
from .wot import WotGate
from .server import RelayServer
from .bridges import (relay_domain as _bridge_domain, reveals_blocked_bridge,
                      author_on_blocked_bridge, is_bridged_post)

logger = logging.getLogger(__name__)

# Firehose in-process dedup: the same popular event arrives once per upstream relay. A bounded recent-id
# set drops dups WITHOUT the per-event has_event() DB round-trip (the dominant firehose read load).
_FH_SEEN: set = set()
_FH_SEEN_ORDER: deque = deque(maxlen=30000)


def _fh_mark(eid: str) -> None:
    """Record an id as HANDLED (confirmed stored), so a later duplicate from another relay skips the
    has_event() DB round-trip. Only call after the event is actually stored — marking before storage
    would let a transient add failure permanently drop the event (a dup would be wrongly skipped)."""
    if eid in _FH_SEEN:
        return
    if len(_FH_SEEN_ORDER) >= _FH_SEEN_ORDER.maxlen:
        _FH_SEEN.discard(_FH_SEEN_ORDER[0])   # leftmost is about to be evicted by the append below
    _FH_SEEN.add(eid)
    _FH_SEEN_ORDER.append(eid)

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))

# --- GRASP git-over-nostr (P3): repo-SCOPED collaboration acceptance ---------------------------
# Patches (1617) / issues (1621) / replies (1622/1623) / status (1630-1633) are accepted from ANY
# author — but ONLY when they reference a repo THIS node actually hosts (an `a` tag 30617:<owner>:<id>
# whose bare repo exists on disk, AND is not a PRIVATE repo — private repos never use the public Nostr
# flow). 30618 (repo state) is scoped to a hosted repo by its own d-tag/pubkey. This keeps the WoT
# exemption from becoming an open spam firehose. Announcements (30617) stay broadly public (Discover).
_GIT_COLLAB_KINDS = frozenset({30618, 1617, 1621, 1622, 1623, 1630, 1631, 1632, 1633})
_GIT_REPOS_DIR = os.path.join(_REPO_ROOT, "data", "git_repos")
_HOSTED_CACHE: dict = {}          # (owner_hex, repo_id) -> (is_hosted_public: bool, expires_at)
_HOSTED_TTL = 30.0                 # cheap filesystem stat, cached so it's not per-event


def _hosted_public_repo(owner_hex: str, repo_id: str) -> bool:
    """True iff data/git_repos/<owner_hex>/<repo_id>.git exists AND is not marked private. Cached
    (TTL) so the firehose doesn't stat per event. Any error -> False (don't accept)."""
    import re as _re
    if not (isinstance(owner_hex, str) and len(owner_hex) == 64):
        return False
    if not owner_hex.islower():
        owner_hex = owner_hex.lower()
    try:
        bytes.fromhex(owner_hex)
    except ValueError:
        return False
    rid = (repo_id or "").strip().lower()
    if rid.endswith(".git"):
        rid = rid[:-4]
    if not rid or not _re.match(r"^[a-z0-9][a-z0-9._-]{0,99}$", rid):
        return False
    key = (owner_hex, rid)
    now = time.time()
    hit = _HOSTED_CACHE.get(key)
    if hit and hit[1] > now:
        return hit[0]
    d = os.path.join(_GIT_REPOS_DIR, owner_hex, rid + ".git")
    ok = False
    if os.path.isdir(d):
        priv = False
        try:
            with open(os.path.join(d, "grasp.json")) as f:
                priv = bool(json.load(f).get("private"))
        except (OSError, ValueError):
            priv = False
        ok = not priv
    _HOSTED_CACHE[key] = (ok, now + _HOSTED_TTL)
    return ok


def _git_event_for_hosted_repo(ev: dict) -> bool:
    """Does this git collaboration event reference a repo THIS node publicly hosts?
      - 30618 repo state: coordinate 30618:<pubkey>:<d> -> owner=pubkey, id=d-tag.
      - 1617/1621/1622/1623/1630-1633: any `a` tag 30617:<owner>:<id>.
    """
    try:
        kind = int(ev.get("kind", 0))
        tags = ev.get("tags") or []
        if kind == 30618:
            d = next((t[1] for t in tags if len(t) >= 2 and t[0] == "d"), None)
            return bool(d) and _hosted_public_repo(ev.get("pubkey", ""), d)
        for t in tags:
            if len(t) >= 2 and t[0] == "a" and isinstance(t[1], str):
                parts = t[1].split(":")
                if len(parts) == 3 and parts[0] in ("30617", "30618"):
                    if _hosted_public_repo(parts[1], parts[2]):
                        return True
        return False
    except (ValueError, TypeError):
        return False


async def _collab_repo_announced(ev: dict, store) -> bool:
    """True if a NIP-34 collab event a-tags a repo whose PUBLIC 30617 announcement is on THIS relay's
    store — so a client reading a PEER/PROXY relay (which doesn't itself host the repo) still ingests the
    repo's issues + patches from the firehose. Private repos have no 30617, so they never match."""
    try:
        for t in (ev.get("tags") or []):
            if len(t) >= 2 and t[0] == "a" and isinstance(t[1], str):
                parts = t[1].split(":")
                if len(parts) == 3 and parts[0] in ("30617", "30618"):
                    if await store.is_repo_announced(parts[1], parts[2]):
                        return True
    except Exception:
        return False
    return False

# Per-connection protocol limits (constants — tune in code, not user-facing). Generous so
# feature-rich clients (which open many simultaneous subscriptions for feed/notifs/profiles)
# don't hit the cap and get a CLOSED, which some clients render as an unhealthy/red relay.
_MAX_MESSAGE_SIZE = 512 * 1024
_MAX_SUBS_PER_CONN = 500
_MAX_FILTERS_PER_REQ = 25

# Local-clock hours in which the heavy nightly block-purge is allowed to run (small hours, low
# traffic). Checked hourly; a daily stamp ensures it fires at most once even across restarts.
_PURGE_HOURS = (2, 3, 4)


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
        self.private_outbox = None
        self.cfg: dict = {}

    def is_running(self) -> bool:
        return self.thread is not None and self.thread.is_alive()


# Cross-process IPC paths (the relay runs in its own subprocess; the app reads its status and
# drops admin commands via files). The store itself is Postgres, so there's no on-disk DB file —
# these sidecars just need a STABLE base path both processes agree on (under the repo's data/ dir).
def _relay_db_path() -> str:
    return os.path.join(_REPO_ROOT, "data", "nostr_relay")


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
    from app.services import settings_store
    db = SessionLocal()
    try:
        # The relay reads its OWN config from the Nostr datastore — no SQL Setting table. Plumbing
        # keys (port/bind/pg_dsn) come from the local JSON; the rest are decrypted straight from the
        # relay's event store (same Postgres). Populate this process's settings cache, then read it.
        settings_store.load_local()
        settings_store.hydrate_from_db(db)

        def g(key, default=""):
            v = settings_store.get(key, None)
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
        # BLANK = the built-in default seeds (turnkey convenience) — same pattern as upstream relays
        # below. A fresh node's settings store isn't seeded at first WoT build, so without this the
        # WoT was just the operator ("only 2 members on a new install"). Admin-set seeds override.
        if not seeds and not seeds_raw.strip():
            seeds = [pk for pk in (nostr_service.to_pubkey_hex(s) for s in nostr_service.DEFAULT_WOT_SEEDS) if pk]

        # Upstream relays: BLANK = the bots' DEFAULT_RELAYS (turnkey convenience). But if the admin
        # SET a value that yields no valid URLs, don't silently fall back to ALL defaults — that's
        # the "I changed upstream but it still syncs the defaults" trap. URLs must be ws:// or wss://.
        _up_raw = g("nostr_relay_upstream_relays", "")
        upstream = nostr_service.relay.normalize_relays(_up_raw)
        if not upstream:
            if _up_raw.strip():
                logger.warning("[nostr-relay] upstream_relays is set but has no valid ws://|wss:// "
                               "URLs (%r) — using NO upstream, not the defaults. Fix the format.", _up_raw)
            else:
                upstream = list(nostr_service.DEFAULT_RELAYS)

        cfg = {
            # The relay is the app's datastore (settings/users/bots/chats/records all live in its
            # event store), so it is MANDATORY and always runs — there is no enable toggle anymore.
            "enabled": True,
            "bind": g("nostr_relay_bind", "127.0.0.1"),
            "port": gi("nostr_relay_port", 3052),
            "seeds": seeds,
            "upstream": upstream,
            # The operator's OWN relays, for mirroring the private encrypted libraries (notes,
            # passwords, budget, files index). Blank = no mirroring, which is the default: this has
            # to be a list someone chose, because these events are a permanent per-user metadata
            # trail wherever they land. Point it at your other node, not at a public relay.
            "private_relays": nostr_service.relay.normalize_relays(
                g("nostr_relay_private_relays", "") or ""),
            # Bypass the outbound Tor proxy for the relay's OWN upstream traffic (sync/outbox/
            # WoT). Direct is faster and avoids the proxy-startup-race log flood; doesn't change
            # the bots' proxy behavior. (The relay client already tries proxy-then-direct per
            # connection, so leaving this False still federates when Tor is down.)
            "direct": gb("nostr_relay_disable_proxy", False),
            # DR: broadcast the encrypted pcai: CONFIG docs to upstream (see _broadcastable).
            "backup_datastore": gb("nostr_relay_backup_datastore", True),
            # Live firehose: keep a persistent subscription to each upstream relay and store
            # only WoT authors — real-time, vs the polling sweep's per-cycle lag.
            "firehose_enabled": gb("nostr_relay_firehose_enabled", True),
            # Distributed-LB (DVM): when on, also stream cluster job/result events (kinds not in
            # ingest_kinds) addressed to this node from the upstream cluster relay — so a node can use
            # ONLY its local relay and still receive jobs/results via the WoT upstream sync.
            "dvm_enabled": gb("nostr_dvm_enabled", False),
            # Node-agent transport (kind 5300/6300 exec-over-Nostr) rides the SAME firehose DVM
            # subscription, but it's a SEPARATE feature from GPU-sharing DVM — so the DVM firehose
            # branch spawns when EITHER is on (a node can run agent commands without offloading compute).
            "agent_enabled": gb("node_exec_nostr_enabled", False),
            # Shared-cluster PEERS → the relay's write-gate accepts these npubs' DVM job-kind events even
            # if they aren't WoT members (sharing compute is a deliberate grant, separate from the social
            # web of trust). Each `nostr_dvm_peers` line is "npub relay"; we take the leading npub. The
            # node-agent transport (kind 5300/6300) rides the SAME DVM cluster path, so its worker nodes
            # (`node_exec_node_npubs`, "name npub" per line) and controllers (`node_exec_trusted_npubs`,
            # one npub per line/comma) are allowlisted here too — exec is still gated a second time by
            # nostr_dvm.is_agent_trusted before anything runs.
            "dvm_allowed": frozenset(filter(None, (
                *(nostr_service.to_pubkey_hex(_ln.split()[0])
                  for _ln in g("nostr_dvm_peers", "").replace(",", "\n").splitlines() if _ln.split()),
                *(nostr_service.to_pubkey_hex(_ln.split()[1])
                  for _ln in g("node_exec_node_npubs", "").splitlines() if len(_ln.split()) >= 2),
                *(nostr_service.to_pubkey_hex(_ln.strip())
                  for _ln in g("node_exec_trusted_npubs", "").replace(",", "\n").splitlines() if _ln.strip()),
            ))),
            # NIP-90 request kinds (see nostr_dvm._REQ_KIND); 5300 = node-agent exec (result 6300).
            "dvm_req_kinds": frozenset((5050, 5100, 5201, 5202, 5300)),
            # DVM RESULT kinds (request + 1000). A node with its OWN relay publishes results as a WoT
            # member, so this never mattered — but the STANDALONE agent (agent/pcnode_agent.py, e.g.
            # router.lan) is a keyless client with no local relay: it publishes its 6xxx result to a
            # peer's relay as a NON-member, so the write-gate must accept result kinds from a
            # dvm_allowed npub too (else the command runs but the result is rejected + never returns).
            "dvm_res_kinds": frozenset((6050, 6100, 6201, 6202, 6300)),
            # How many upstream relays the firehose streams from (0 = ALL). It's the sole
            # real-time ingestion path now, so default to all for completeness.
            "firehose_max_relays": gi("nostr_relay_firehose_max_relays", 0),
            # Heavy backfill SWEEP (per-member crawl of the whole trust graph). OFF: the firehose
            # streams + filters in real time; the sweep is the laggy "mirror their feeds" crawl.
            "mirror_feeds": gb("nostr_relay_mirror_feeds", False),
            # Postgres is the relay's store (no SQLite). libpq DSN; tunable in Admin → Relay.
            "pg_dsn": g("nostr_relay_pg_dsn", os.environ.get("NOSTR_RELAY_PG_DSN",
                        "host=127.0.0.1 port=5432 dbname=posterchan_relay user=posterchan")),
            # Age retention for high-volume FEED content only (notes/reposts/reactions/comments):
            # pruned after N days. Registered users' notes + direct-published events are ALWAYS
            # preserved (never pruned), so a user's own history is safe. 0 = keep everything.
            "retention_days": gi("nostr_relay_retention_days", 30),
            # No hard count cap on Postgres either (it's an age-agnostic RAM bound — would delete old
            # feed notes once over the limit). 0 = unlimited; the 30-day age retention is the only
            # feed cleanup, and registered users' + direct-published events are always preserved.
            "max_events": gi("nostr_relay_max_events", 0),
            "sync_budget_sec": gi("nostr_relay_sync_budget_sec", 100), # per-tick sync work budget
            "wot_refresh_sec": gi("nostr_relay_wot_refresh_sec", 604800),  # weekly (was daily)
            "wot_refresh_hour": gi("nostr_relay_wot_refresh_hour", 4),    # UTC hour for the nightly full crawl
            # Keep the cached set if a crawl resolves < this fraction of it (partial-crawl protection).
            "wot_shrink_guard_ratio": float(g("nostr_relay_wot_shrink_guard_ratio", "0.85") or 0.85),
            # Minimum gap between FULL graph crawls triggered by the refresh-wot control msg (signup/
            # follow/bot/admin). New members are added incrementally (wot-add); the expensive crawl is
            # throttled so a burst of activity can't run back-to-back full rebuilds and peg a core.
            "wot_refresh_min_interval_sec": gi("nostr_relay_wot_refresh_min_interval_sec", 1800),
            "prune_interval_sec": gi("nostr_relay_prune_interval_sec", 86400),  # nightly (was hourly)
            "wot_enabled": gb("nostr_relay_wot_enabled", True),           # off → open publishing + NO trust-graph background work
            "send_only": gb("nostr_relay_send_only", False),              # broadcast to upstream (outbox) but NEVER pull/store their events (no firehose/sync/metadata mirror)
            "wot_depth": gi("nostr_relay_wot_depth", 1),                  # 1=follows, 2=+FoF
            "wot_min_followers": gi("nostr_relay_wot_min_followers", 2),  # FoF inclusion threshold
            "wot_max": gi("nostr_relay_wot_max", 50000),                  # cap on total members
            "wot_depth3_crawl_max": gi("nostr_relay_wot_depth3_crawl_max", 2500),  # depth-3: max FoF to crawl (flood guard)
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
            # 30402 = NIP-99 classified listings (the Market/Store); 30017/30018 = NIP-15 marketplace
            # stalls/products — ingest + firehose them so the Discover → Market view sees WoT listings.
            # 2003/2004 = NIP-35 torrents (+comments), 30617 = NIP-34 git repo announcement — the
            # Discover → Torrents / Git Repos views. GRASP git-over-nostr (NIP-34): 30618 repo state,
            # 1617 patch, 1621 issue, 1622 reply, 1623 repo-reply, 1630-1633 issue/patch status —
            # ingested+firehosed for the Discover → Git view. Collaboration kinds (1617/1621/…) are
            # accepted repo-SCOPED (see _firehose_event) so it's NOT an open spam firehose. All git kinds
            # are kept forever (see store._GIT_KINDS: never pruned, never expired).
            # 41 is the NIP-28 channel-METADATA edit (a kind-40 is immutable, so a rename/picture is a
            # separate event). Without it, 40 and 42 sync but every channel edit stops at the node it was
            # made on — the channel would show its ORIGINAL name/picture everywhere else, forever.
            # 10005 is the NIP-51 "public chats" join list. Each node's push watcher reads it to decide
            # whose devices to notify about a channel message, so a list published on one node has to
            # reach the others or that user gets chat pushes from one node only.
            "ingest_kinds": [int(k) for k in (g("nostr_relay_ingest_kinds", "0,1,3,6,7,40,41,42,1111,9735,10002,10005,10050,2003,2004,30023,30311,34550,30402,30017,30018,30617,30618,1617,1621,1622,1623,1630,1631,1632,1633")
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
            # Drop ALL fediverse/Bluesky-bridged posts (any NIP-48 `proxy` tag), regardless of which
            # bridge relayed them. The domain blocklist above can't catch these — a mirror's nip05 is
            # on a normal domain and its proxy URL points at the original instance, never the bridge.
            "block_bridged": gb("nostr_relay_block_bridged", False),
            # Built-in NIP-05 identity server (served over HTTP by the relay subprocess at
            # /.well-known/nostr.json). Defaults preserve the entries previously on router.lan.
            "nip05": {
                # Off when WoT is off — a processing node shouldn't also serve identities.
                "enabled": gb("nostr_relay_nip05_enabled", True) and gb("nostr_relay_wot_enabled", True),
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
        # Preserve is a SUPERSET of operator (adds NIP-05 holders + bridged users' puppets) and is
        # kept separate on purpose — operator grants publish/WoT/DM rights, preserve grants nothing.
        cfg["preserve"] = _collect_preserve_pubkeys(db)
        # Nostr↔Fediverse bridge: the secret our deterministic fedi "puppet" keys derive from. The
        # relay validates a puppet event by re-deriving its pubkey (see nostr.bridge_keys), so it
        # needs the same secret the app signs with. Local keystore only — never leaves the node.
        try:
            from app.services import keystore
            cfg["bridge_secret"] = keystore.get_bridge_secret()
        except Exception:
            cfg["bridge_secret"] = None
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
            # A registered user's POSTING identity (login npub). Including it preserves their own
            # notes from the age-prune — a registered account's history is their data, not
            # reconstructable feed content — and lets them publish through the relay.
            npub = getattr(u, "nostr_npub", None)
            if npub:
                try:
                    h = nostr_service.to_pubkey_hex(npub)
                    if h:
                        out.add(h)
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
        # The DATASTORE OPERATOR/SIGNER key: settings/users/bots/chat docs (kind-30078 `pcai:`) are
        # signed with it. On a fresh node with no linked users this is the ONLY operator pubkey, so
        # the relay MUST trust it or it rejects its own settings docs ("not in web of trust") and the
        # whole Nostr-as-datastore write path silently fails. Lives in the local keyfile.
        try:
            from app.services import keystore
            op_nsec = keystore.get_operator_nsec()
            if op_nsec:
                out.add(nostr_service.derive_pubkey(nostr_service.decode_seckey(op_nsec)))
        except Exception:
            pass
        # Per-user SERVER-HELD storage keys: these are the keys the app signs each user's encrypted
        # chat/upload events with — our own keys, so they must be allowed to write here. They live in
        # the local keyfile (authoritative); also read the legacy UserSetting location for any not yet
        # migrated. Union of both so a key is accepted regardless of where it currently lives.
        try:
            from app.services import keystore
            for hexsk in (keystore._load().get("storage", {}) or {}).values():
                try:
                    out.add(nostr_service.derive_pubkey(nostr_service.decode_seckey(hexsk)))
                except Exception:
                    pass
        except Exception:
            pass
        from app.models import UserSetting
        for us in db.query(UserSetting).filter(UserSetting.key == "storage_nsec").all():
            if us.value:
                try:
                    out.add(nostr_service.derive_pubkey(nostr_service.decode_seckey(us.value)))
                except Exception:
                    pass
    except Exception as e:
        logger.debug("[nostr-relay] operator key collection failed: %s", e)
    return list(out)


def _collect_preserve_pubkeys(db) -> list:
    """Authors whose events are NEVER auto-pruned.

    A SUPERSET of the operator set, and deliberately NOT the same list: being an operator grants
    publish/WoT/DM privileges, so that list must stay exactly this node's own keys. Preservation
    grants nothing — it only says "this is somebody's data, not reconstructable feed content".

    Two groups the operator set misses, both of which were being aged out:
      - anyone holding a NIP-05 on THIS server (the operator-curated `nostr_relay_nip05_names`);
      - the PUPPET of a local user's linked fediverse account. A bridged user's own fedi posts are
        mirrored under their PUPPET key, never under their npub, so preserving the npub alone left
        their own history to be deleted by Mirror retention.

    Puppet NIP-05 names are NOT read here. Every puppet auto-registers one (alice_host@domain), so
    honouring those would preserve the entire mirror and Mirror retention would never delete anything.
    """
    out = set(_collect_operator_pubkeys(db))
    # NIP-05 holders on this server — the operator-curated list only.
    try:
        from app.services import settings_store   # module-local, as everywhere else in this file
        # SAME default _read_config serves NIP-05 from. Passing "" here instead would preserve nobody
        # on a node that never set the key, while the relay still answers /.well-known for the built-in
        # names — "verified here" and "never deleted here" must be the same list.
        names, _ = _parse_nip05(
            settings_store.get("nostr_relay_nip05_names", _DEFAULT_NIP05_NAMES) or _DEFAULT_NIP05_NAMES, "")
        out |= {pk for pk in names.values() if pk}
    except Exception as e:
        # WARNING, not debug: a silent partial collection here means the next prune deletes data it
        # was supposed to keep. It must be visible in the journal, not swallowed.
        logger.warning("[nostr-relay] nip05 preserve collection FAILED (%s) — those authors are "
                       "unprotected this pass", e)
    # Puppets of local users' linked fediverse accounts.
    try:
        from sqlalchemy import func
        from app.models import FediPuppet, User
        accts = {(a or "").strip().lower().lstrip("@")
                 for (a,) in db.query(User.pleroma_acct).filter(User.pleroma_acct.isnot(None)).all()}
        accts.discard("")
        if accts:
            for (pk,) in db.query(FediPuppet.pubkey_hex).filter(
                    func.lower(FediPuppet.acct).in_(accts)).all():
                if pk:
                    out.add(pk)
    except Exception as e:
        logger.warning("[nostr-relay] bridged-user puppet preserve collection FAILED (%s) — bridged "
                       "users' mirrored posts are unprotected this pass", e)
    return list(out)


async def _refresh_preserve(store) -> None:
    """UNION the current operators (registered users/bots/keys) + persisted PINNED authors (explicitly
    backfilled histories, in relay kv) into the store's preserve set, right before a prune/purge.

    Grow-only (extend, never replace) so a partial/failed operator re-collection can't SHRINK the set
    and expose a user to deletion. Runs the DB collection in an executor so it doesn't block the relay
    event loop. Deliberately does NOT touch the publish gate — preserving someone's notes must not
    grant them publish/WoT/DM privileges (pin != operator)."""
    def _collect():
        from app.database import SessionLocal
        db = SessionLocal()
        try:
            return set(_collect_preserve_pubkeys(db))
        finally:
            db.close()
    try:
        pks = await asyncio.get_event_loop().run_in_executor(None, _collect)
    except Exception as e:
        logger.debug("[nostr-relay] preserve operator re-collect failed: %s", e)
        pks = set()
    try:
        pinned = await store.kv_get("pinned_pubkeys")
        if pinned:
            pks |= {p for p in pinned.split() if len(p) == 64}
    except Exception:
        pass
    if pks:
        store.extend_preserve_pubkeys(pks)


# --- async main -------------------------------------------------------------

async def _main(cfg: dict) -> None:
    # Public port → scanners/probes will hit it; the websockets server logs each failed
    # handshake with a full traceback, which would flood the journal. Quiet it.
    logging.getLogger("websockets.server").setLevel(logging.CRITICAL)
    store = RelayStore(
        cfg["pg_dsn"],
        max_events=cfg["max_events"], retention_days=cfg["retention_days"])
    loop = asyncio.get_running_loop()
    loop.set_exception_handler(_relay_loop_exception_handler)
    store.open(loop)
    gate = WotGate()
    gate.set_operator(cfg["operator"])
    gate.set_blocked(cfg["blocked_pubkeys"])
    gate.set_bridge_secret(cfg.get("bridge_secret"))   # validate fediverse puppet events
    store.set_preserve_pubkeys(cfg.get("preserve") or cfg["operator"])   # never pruned: local users,
    # NIP-05 holders on this server, and bridged users' puppets (their fedi posts mirror under those)
    try:                                            # + persisted PINNED authors (backfilled histories)
        _pinned = await store.kv_get("pinned_pubkeys")
        if _pinned:
            store.extend_preserve_pubkeys([p for p in _pinned.split() if len(p) == 64])
    except Exception:
        pass
    await gate.load_from_store(store)              # warm from snapshot for immediate gating
    # Moderation is ON THE FLY at ingest (server._on_event + the sync sweep): every post is checked
    # BEFORE it is written — WoT membership first, then blocked npubs + words + languages + bridges —
    # so nothing matching is ever stored. We do NOT delete already-stored notes on startup/reload;
    # that retroactive purge is what was deleting legitimate notes. The ONE thing re-applied here is
    # bridge MARKING: the gate's bridged set is in-memory (load_from_store restores members only), so
    # re-scan identity/relay-list events to keep on-the-fly bridge rejection working after a restart.
    # One-shot cleanup of content stored before a rule existed (or illegal content) is the admin
    # "Purge now" button only (cmd: purge-blocks).
    if cfg["blocked_relays"]:
        await _mark_blocked_relays(store, gate, cfg["blocked_relays"])
    from .outbox import Outbox
    outbox = Outbox(cfg["upstream"], min_interval=cfg["outbox_min_interval"],
                    maxsize=cfg["outbox_max_queue"], direct=cfg["direct"],
                    retries=cfg["outbox_retries"], retry_delay=cfg["outbox_retry_delay"])
    outbox.start()
    # A SECOND outbox, with its own relay list, for the private libraries. Same paced queue and the
    # same retry behaviour — a mirror that gives up on the first refusal is not a backup. It only
    # exists when the operator named relays for it; with none, nothing is mirrored and the callback
    # is None, so the decision cannot be reached at all.
    private = None
    if cfg["private_relays"]:
        # NOT the public outbox's budget. That pacing exists to avoid being rate-limited or blocked
        # by strangers' relays; this one targets a relay the operator runs, and the events on it are
        # the ones with no other copy. At 1/s with a 500-slot queue a Joplin import or a chat burst
        # overruns it and `enqueue` drops the NEWEST writes — the vault entries this exists to
        # protect — logging the same line the public blaster does, so nothing distinguishes "we
        # skipped a stranger's relay" from "your notes were not backed up".
        private = Outbox(cfg["private_relays"], min_interval=0.05,
                         maxsize=20000, direct=cfg["direct"],
                         retries=cfg["outbox_retries"], retry_delay=cfg["outbox_retry_delay"],
                         label="private-mirror")
        private.start()
        logger.info("[nostr-relay] private mirror ON — encrypted libraries also go to %d relay(s): %s",
                    len(cfg["private_relays"]), ", ".join(cfg["private_relays"]))
    server = RelayServer(store, gate, cfg, outbox_cb=outbox.enqueue,
                         private_cb=(private.enqueue if private else None))
    await server.warm_bridge_nip05()   # load persisted fediverse-puppet NIP-05 names before serving
    _relay.store, _relay.gate, _relay.server, _relay.outbox = store, gate, server, outbox
    _relay.private_outbox = private
    _relay.stop_event = asyncio.Event()

    # WoT build on startup ONLY when there is NO cached snapshot (first run / cleared cache). If the
    # gate is warm from the persisted snapshot (load_from_store above), we use it as-is and let the
    # NIGHTLY task refresh the full graph — even if the snapshot is stale. This guarantees that a
    # restart (a deploy, a test, a crash) never triggers a 37k-follow crawl. The nightly job (+ overdue
    # safety net) keeps the cache current; new members are admitted incrementally (wot-add) meanwhile.
    if not cfg["wot_enabled"]:
        logger.info("[nostr-relay] WoT disabled — open publishing; skipping trust-graph build, "
                    "daily refresh, metadata backfill, sync sweep and firehose (no cross-node work)")
    elif not gate.members():
        logger.info("[nostr-relay] no cached WoT snapshot — building the trust graph now (first run)")
        asyncio.create_task(_initial_wot_build(gate, store, cfg, _relay.stop_event))
    else:
        logger.info("[nostr-relay] WoT warm from snapshot (%d members) — last build %dh ago, "
                    "skipping rebuild (daily cadence)",
                    len(gate.members()), int((time.time() - _read_wot_stamp(cfg)) / 3600))

    ws = await serve(
        server.handle, cfg["bind"], cfg["port"],
        process_request=server.process_request,
        max_size=cfg["max_message_size"],
        # ping_timeout raised 30→120: a mobile PWA's radio can suspend briefly, delaying the browser's
        # auto-pong; a 30s timeout dropped those still-alive connections (feed "stops after ~2-3 min").
        # App-level _keepalive NOTICEs keep the socket genuinely live; pings still reap a truly-dead conn.
        ping_interval=30, ping_timeout=120, max_queue=64,
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
            if author_on_blocked_bridge(ev, _br):
                gate.mark_bridged_identity(ev.get("pubkey", ""))   # kind-0 nip05 → block even members
            else:
                gate.mark_bridged(ev.get("pubkey", ""))
            return
        # Opt-in: drop any bridged (NIP-48 proxy) post from the live firehose, whatever bridge relayed
        # it. Operators / registered local users are exempt (first-party cross-posts).
        if cfg.get("block_bridged") and is_bridged_post(ev) and not gate.is_operator(ev.get("pubkey", "")):
            return
        _kind = int(ev.get("kind", 1))
        if _kind in (9735, 1059):
            # Zap receipts (9735) are authored by the LNURL zap SERVICE and gift wraps (1059) by a
            # throwaway ephemeral key — neither author is in the WoT, so gate on the RECIPIENT p-tag
            # instead (same as the WS write path). Without this the firehose drops every zap, so
            # posts never show a zap total.
            if not any(len(t) >= 2 and t[0] == "p" and gate.is_member(t[1]) for t in (ev.get("tags") or [])):
                return
        elif _kind == 4:
            # NIP-04 DM: accept if the sender is in the WoT OR it's addressed to one of our operators
            # (the DM-inbox subscription pulls these). Without the operator case, a DM from someone the
            # user doesn't follow — incl. a fediverse user via a bridge — would be dropped.
            if not (gate.is_member(ev.get("pubkey", "")) or
                    any(len(t) >= 2 and t[0] == "p" and gate.is_operator(t[1]) for t in (ev.get("tags") or []))):
                return
        elif _kind in (2003, 2004, 30617, 30618):
            # NIP-35 torrents (+comments) / NIP-34 repo announcement (30617) + repo state (30618) are
            # PUBLIC, browsable Discover content — sync from ANY author (not WoT-gated), since typically
            # no one in the WoT posts them, and repo metadata is low-volume + replaceable. Still
            # signature-verified below; kept forever (see store._GIT_KINDS). Patches/issues (1617/1621/…)
            # are NOT opened up here yet — they stay WoT-gated until repo-scoped acceptance lands (accept
            # a patch only when it references a repo we host), so this isn't an open spam firehose.
            pass
        elif _kind in _GIT_COLLAB_KINDS:
            # GRASP git collaboration (patch/issue/reply/status/state). Accept from ANY author, but ONLY
            # when it references a repo THIS node publicly HOSTS **or** a repo whose PUBLIC 30617 is on
            # this relay — the latter lets a client reading a peer/proxy relay see issues + patches for
            # repos hosted elsewhere. Repo-scoped either way (private repos have no 30617 → never matched),
            # so it's not an open spam firehose.
            if not (_git_event_for_hosted_repo(ev) or await _collab_repo_announced(ev, store)):
                return
        elif not gate.is_member(ev.get("pubkey", "")):
            return
        eid = ev.get("id")
        if not isinstance(eid, str) or len(eid) != 64:
            return
        if eid in _FH_SEEN:
            return   # already handled this id (dup from another upstream relay) — skip the DB round-trip
        if await store.has_event(eid):
            _fh_mark(eid)   # already stored → record so future dups skip the DB check
            return
        if not verify_event(ev):
            return   # invalid sig — NOT marked (deterministic re-reject is cheap; never store)
        if int(ev.get("kind", 1)) == 1:
            content = ev.get("content", "")
            if not content.strip():
                return   # EMPTY note — spam/noise, nothing to render; don't store or fan out (matches _on_event)
            if (_bl and blocked_language(content, _bl)) or (_bw and blocked_word(content, _bw)):
                return
        if await store.add_event(ev, origin="wot"):
            _fh_mark(eid)   # mark seen ONLY after a successful store (so a transient fail can retry)
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
                        cfg["direct"], blocked=_bl, blocked_words=_bw, gate=gate,
                        block_bridged=cfg.get("block_bridged", False))
                except Exception as e:
                    logger.debug("[nostr-relay] firehose ancestor backfill failed: %s", e)

    async def _maybe_rebuild_wot():
        # The full 37k-follow graph crawl is a NIGHTLY task: it runs only during the configured
        # low-traffic hour (UTC), and only once it's been ~a day since the last build. The stamp gates
        # it (not the timer), so frequent restarts can't trigger extra crawls. New members are admitted
        # incrementally (wot-add) all day; this just refreshes the follows-of-follows graph once a night.
        # `overdue` is a safety net so the gate can never go badly stale if a night was missed (downtime).
        age = time.time() - _read_wot_stamp(cfg)
        hour = datetime.datetime.now(datetime.timezone.utc).hour
        nightly = hour == cfg.get("wot_refresh_hour", 4) and age >= cfg.get("wot_refresh_sec", 604800) - 4 * 3600
        overdue = age >= cfg.get("wot_refresh_sec", 604800) * 2
        if nightly or overdue:
            logger.info("[nostr-relay] %s WoT refresh — rebuilding the full graph (age %dh)",
                        "nightly" if nightly else "overdue", int(age / 3600))
            await _build_wot(gate, store, cfg)

    _purge_state = {"count": None, "ts": 0}   # last block-purge result, surfaced in relay status
    _prune_state = {"count": None, "ts": 0}   # last auto-clean (age prune) result, same surfacing

    async def _purge_blocks_now() -> int:
        """Apply the configured block filters to ALREADY-STORED events (one-shot, heavy). Returns the
        number of events removed. Re-reads config so it always reflects the current Relay settings."""
        fresh = _read_config()
        # Refresh the PRESERVE set (grow-only, no gate) before the deletes: delete_by_words/langs/proxy
        # spare preserved users via store.preserve_pubkeys, but that set was only built at startup/reload
        # — so a user who linked their npub since (h@…) wasn't spared and their blocked-language notes
        # got purged, reappearing only until the next purge. Union-only so a partial re-collect can't
        # SHRINK preserve and delete a registered user's local-first notes.
        await _refresh_preserve(store)
        by_pk = (await store.delete_pubkeys(fresh["blocked_pubkeys"]) or 0) if fresh["blocked_pubkeys"] else 0
        by_word = (await store.delete_by_words(fresh["blocked_words"]) or 0) if fresh["blocked_words"] else 0
        by_lang = (await store.delete_by_langs(fresh["blocked_langs"]) or 0) if fresh["blocked_langs"] else 0
        by_bridge = (await _apply_blocked_relays(store, gate, fresh["blocked_relays"]) or 0) if fresh["blocked_relays"] else 0
        # Bridged-post purge (NIP-48 proxy tag) — preserve-aware (local users / direct-published spared).
        by_proxy = (await store.delete_by_proxy() or 0) if fresh.get("block_bridged") else 0
        total = by_pk + by_word + by_lang + by_bridge + by_proxy
        if total:
            logger.info("[nostr-relay] block-purge breakdown: total=%d (pubkeys=%d words=%d langs=%d bridge=%d proxy=%d) — local/direct-published notes preserved (word/lang purges DO cover WoT members, matching ingest)",
                        total, by_pk, by_word, by_lang, by_bridge, by_proxy)
        _purge_state["count"] = total
        _purge_state["ts"] = int(time.time())
        return total

    async def _maybe_purge_blocks() -> None:
        # Nightly retroactive purge: heavy full-corpus scans, so run at most ONCE A DAY and only in
        # the small hours (low traffic). A persisted stamp — not the timer — decides, so frequent
        # restarts can't re-trigger it (mirrors the daily WoT rebuild). Live ingest filtering is the
        # primary gate; this only cleans content stored before a rule existed.
        now = time.time()
        try:
            last = float(await store.kv_get("block_purge_last") or 0)
        except (TypeError, ValueError):
            last = 0.0
        if (now - last) < 20 * 3600:
            return
        if datetime.datetime.now().hour not in _PURGE_HOURS:
            return
        removed = await _purge_blocks_now()
        await store.kv_set("block_purge_last", str(int(now)))
        _write_status()
        logger.info("[nostr-relay] nightly block-purge removed %d stored event(s)", removed)

    # The live firehose runs as a REPLACEABLE task group so an upstream-relay change (Admin → Relay)
    # can reconnect it in place via the `reload-upstream` control msg — no subprocess restart, so
    # /client connections and settings writes are never dropped (changing upstream used to force a
    # full relay restart = ~90s outage). The group uses its OWN stop event so a reload cancels just
    # the firehose; full shutdown sets it too (see the finally below).
    _firehose = {"tasks": [], "stop": None}

    def _spawn_firehose(stagger_span=None):
        """(Re)build the firehose task group from the CURRENT cfg (upstream / firehose_max_relays /
        ingest_kinds / operator / dvm). No-op unless WoT is on, not send-only, and the firehose is
        enabled. Any prior group must be stopped first (see _restart_firehose). `stagger_span` (None
        = the firehose default) lets a live reload reconnect faster than a cold boot."""
        if not (cfg["wot_enabled"] and not cfg["send_only"] and cfg.get("firehose_enabled", True)):
            _firehose["tasks"], _firehose["stop"] = [], None
            return
        from .firehose import run_firehose, _STAGGER_SPAN
        span = _STAGGER_SPAN if stagger_span is None else stagger_span
        fstop = asyncio.Event()
        mr = cfg.get("firehose_max_relays", 0)
        grp = [asyncio.create_task(
            run_firehose(cfg["upstream"], cfg["ingest_kinds"], _firehose_event, fstop,
                         cfg["direct"], max_relays=mr, stagger_span=span, label=" (WoT)"))]
        # Targeted DM inbox: kind-4 (NIP-04) + 1059 (NIP-17 gift wrap) addressed to our operators.
        # These kinds aren't in ingest_kinds, so the global firehose never pulled incoming DMs —
        # only the user's own outgoing (published here) showed. Filtered by #p=operators.
        _ops = list(cfg.get("operator") or [])
        if _ops:
            grp.append(asyncio.create_task(
                run_firehose(cfg["upstream"], [4, 1059], _firehose_event, fstop, cfg["direct"],
                             max_relays=mr, extra={"#p": _ops}, stagger_span=span, label=" (DM inbox)")))
            # Distributed-LB (DVM): stream cluster job (5xxx) + result (6xxx) events addressed to
            # THIS node (#p=operator) from the upstream cluster relay, so a worker can use only its
            # LOCAL relay and still receive jobs/results via the WoT upstream sync. Also covers the
            # node-agent transport (5300/6300) whenever exec-over-Nostr is enabled on this node.
            if cfg.get("dvm_enabled") or cfg.get("agent_enabled"):
                grp.append(asyncio.create_task(
                    run_firehose(cfg["upstream"],
                                 [5050, 5100, 5201, 5202, 5300, 6050, 6100, 6201, 6202, 6300],
                                 _firehose_event, fstop, cfg["direct"], max_relays=mr,
                                 extra={"#p": _ops}, stagger_span=span, label=" (DVM)")))
        _firehose["tasks"], _firehose["stop"] = grp, fstop

    async def _restart_firehose():
        """Stop the running firehose group, WAIT for its connections to tear down, then spawn a fresh
        one. Awaiting the cancelled tasks before respawning avoids a window where the old and new
        streams both subscribe to the same relays and double the backfill burst. Uses a short stagger
        so the live reload reconnects promptly (a reconnect, not a cold boot)."""
        old_stop, old_tasks = _firehose.get("stop"), list(_firehose.get("tasks") or [])
        if old_stop is not None:
            old_stop.set()           # ask run_firehose + _run_one loops to exit cleanly
        for t in old_tasks:
            t.cancel()
        if old_tasks:
            await asyncio.gather(*old_tasks, return_exceptions=True)
        _spawn_firehose(stagger_span=2.0)

    async def _prune_fresh():
        # Refresh (grow-only) the preserve set right before pruning, so a user who linked their npub or
        # was backfilled since startup/reload is protected — otherwise the prune runs with a STALE
        # preserve set and deletes their synced notes (the recurring "synced notes disappear" bug).
        await _refresh_preserve(store)
        removed = await store.prune()
        # ALWAYS log the count, even zero. This used to return silently into _periodic, so there was
        # no way to tell from the logs whether auto-clean had ever run — the answer to "I set a
        # retention window, did anything happen?" was unanswerable without querying Postgres.
        logger.info("[nostr-relay] auto-clean removed %d event(s) (retention_days=%s, max_events=%s)",
                    removed, store.retention_days, store.max_events)
        _prune_state["count"] = int(removed or 0)
        _prune_state["ts"] = int(time.time())
        _write_status()
        await store.kv_set("prune_last", str(int(time.time())))
        return removed

    async def _maybe_prune() -> None:
        # Scheduled auto-clean. A PERSISTED stamp — not the timer — decides, exactly like the nightly
        # block-purge: the bare timer meant a restart pushed the next run a full interval out (a node
        # deploying more than once a day never pruned), while the `first=300` kick alone would re-run
        # a heavy full-corpus delete on every restart. The stamp gives one run per interval no matter
        # how the process is cycled. The Admin button calls _prune_fresh directly and ignores this.
        now = time.time()
        try:
            last = float(await store.kv_get("prune_last") or 0)
        except (TypeError, ValueError):
            last = 0.0
        if (now - last) < max(300, cfg["prune_interval_sec"] - 3600):
            return
        await _prune_fresh()

    # prune is the only LOCAL maintenance task — always runs. Everything else below is cross-node /
    # trust-graph work, gated on wot_enabled: with WoT OFF (a processing node) the relay is a pure
    # local store — no WoT rebuild, metadata backfill, sync sweep, or firehose. (NIP-05 serving is
    # also forced off when WoT is off — see the nip05 cfg.)
    tasks = [
        # `first=300`: check ~5 min after start, then on the normal (daily) interval. Without it the
        # first check is a full interval away and every restart resets the clock, so on a node that
        # deploys more than once a day the age prune never ran at all. _maybe_prune's persisted stamp
        # is what stops that same kick from re-running a heavy delete on every restart.
        asyncio.create_task(_periodic(_relay.stop_event, cfg["prune_interval_sec"], _maybe_prune,
                                      "prune", first=300)),
        # Nightly block-purge: checked hourly, fires once in the small hours (see _maybe_purge_blocks).
        asyncio.create_task(_periodic(_relay.stop_event, 3600, _maybe_purge_blocks, "block-purge")),
    ]
    if cfg["wot_enabled"]:
        # WoT rebuild — checked hourly, actually rebuilt only when a day has elapsed (staleness),
        # so it runs once a day regardless of restarts. NOT a feed mirror; just gate membership.
        # Runs even in send-only mode (the publishing gate still needs the trust set).
        tasks.append(asyncio.create_task(_periodic(_relay.stop_event, 3600, _maybe_rebuild_wot, "wot-refresh")))
        if cfg["send_only"]:
            # SEND-ONLY: the relay BROADCASTS its own events to upstream (via the outbox, started
            # above + unaffected) but pulls/stores NOTHING from upstream — so the local DB doesn't
            # fill with a mirror of upstream content. Skip the receive-direction tasks below.
            logger.info("[nostr-relay] send-only — broadcasting to upstream, NOT mirroring it "
                        "(no firehose / sync / metadata backfill)")
        else:
            # Profile/metadata backfill — fetches kind-0/3/10002 for WoT members missing them.
            async def _meta_backfill():
                from . import ingest as _ingest
                await _ingest.fetch_lookup_metadata(store, cfg["upstream"], cfg["author_batch"],
                                                     cfg.get("profile_limit", 1500), cfg["request_pace_sec"], cfg["direct"],
                                                     gate=gate, blocked_relays=cfg.get("blocked_relays"))
            tasks.append(asyncio.create_task(_periodic(_relay.stop_event, 45, _meta_backfill, "metadata-backfill")))
            # Heavy WoT BACKFILL SWEEP — OFF by default (the windowed full-graph crawl).
            if cfg.get("mirror_feeds", False):
                tasks.append(asyncio.create_task(_sync_loop(store, gate, server, cfg, _relay.stop_event)))
            # Live FIREHOSE — ON by default: real-time subscription keeping WoT-author events fresh.
            # Spawned as a replaceable group (see _spawn_firehose) so upstream edits reconnect it
            # live via the reload-upstream control msg, no restart.
            _spawn_firehose()

    # Cross-process admin IPC: this relay runs in its own subprocess, so the app can't read its
    # gate/store directly. Publish status to a file the app polls, and execute admin commands the
    # app drops into a control dir (Refresh-WoT / Backfill buttons). No-ops if launched in-thread.
    _paths = _relay_paths(_relay_db_path())
    os.makedirs(_paths["control"], exist_ok=True)

    def _write_status():
        try:
            tmp = _paths["status"] + ".tmp"
            with open(tmp, "w") as f:
                try:
                    online = int(server.online_count())
                except Exception:
                    online = int(getattr(server, "_conns", 0) or 0)
                try:
                    calls = int(server.active_calls())
                except Exception:
                    calls = 0
                json.dump({"running": True, "members": len(gate.members()),
                           "conns": int(getattr(server, "_conns", 0) or 0),   # raw live socket count
                           "online": online,                                  # deduped by client IP = people now
                           "calls": calls,                                    # people in a call right now (kind-25050)
                           "pid": os.getpid(), "ts": int(time.time()),
                           "block_purge": dict(_purge_state),
                           "prune": dict(_prune_state)}, f)
            os.replace(tmp, _paths["status"])
        except Exception:
            pass

    async def _status_writer():
        while not _relay.stop_event.is_set():
            _write_status()
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
                        _st = _wot_refresh_state
                        _now = time.time()
                        if _st["task"] is not None and not _st["task"].done():
                            logger.info("[nostr-relay] WoT refresh already running — request coalesced")
                        elif _now - _st["last"] < cfg.get("wot_refresh_min_interval_sec", 1800):
                            logger.info("[nostr-relay] WoT refresh throttled — last full build %ds ago "
                                        "(new members are added incrementally, no crawl needed)",
                                        int(_now - _st["last"]))
                        else:
                            _st["last"] = _now
                            _st["task"] = asyncio.create_task(_safe(_build_wot(gate, store, cfg)))
                            logger.info("[nostr-relay] control: WoT refresh requested")
                    elif cmd.get("cmd") == "wot-add" and cmd.get("pubkeys"):
                        pks = [p for p in cmd["pubkeys"] if p]
                        gate.add_members(pks)                          # immediate (in-memory)
                        asyncio.create_task(_safe(store.wot_add(pks)))  # persist
                        logger.info("[nostr-relay] control: added %d member(s) to WoT now", len(pks))
                    elif cmd.get("cmd") == "delete-author" and cmd.get("pubkeys"):
                        pks = [p for p in cmd["pubkeys"] if p]
                        asyncio.create_task(_safe(store.delete_pubkeys(pks)))  # purge their events
                        logger.info("[nostr-relay] control: purged events for %d author(s)", len(pks))
                    elif cmd.get("cmd") == "reload-blocks":
                        # Admin/web-UI edited the blocklist (Admin → Relay, or the web client's
                        # "Block author"). Apply it to the LIVE ingest gate ONLY, so the updated
                        # npubs/words/langs/bridges filter NEW posts on the fly. No stored-note
                        # deletion here — cleanup of already-stored matches (missed filtering) is the
                        # nightly purge or the "Purge now" button.
                        try:
                            fresh = _read_config()
                            cfg["blocked_pubkeys"] = fresh["blocked_pubkeys"]
                            cfg["blocked_words"] = fresh["blocked_words"]   # server reads these live
                            cfg["blocked_langs"] = fresh["blocked_langs"]   # for on-the-fly filtering
                            cfg["blocked_relays"] = fresh["blocked_relays"]
                            cfg["block_bridged"] = fresh.get("block_bridged", False)   # proxy-tag filter, live
                            cfg["operator"] = fresh["operator"]
                            cfg["preserve"] = fresh.get("preserve") or fresh["operator"]
                            gate.set_blocked(cfg["blocked_pubkeys"])
                            gate.set_operator(cfg["operator"])              # gate CAN shrink (revoke removed operators)
                            store.extend_preserve_pubkeys(cfg["preserve"])  # preserve is grow-only (keeps pinned)
                            # Re-mark bridged accounts in the live gate (load_from_store doesn't keep them).
                            if cfg["blocked_relays"]:
                                asyncio.create_task(_safe(_mark_blocked_relays(store, gate, cfg["blocked_relays"])))
                            logger.info("[nostr-relay] control: reloaded %d blocked, %d bridge, %d operator key(s)",
                                        len(cfg["blocked_pubkeys"]), len(cfg["blocked_relays"]), len(cfg["operator"]))
                        except Exception as e:
                            logger.warning("[nostr-relay] reload-blocks failed: %s", e)
                    elif cmd.get("cmd") == "purge-blocks":
                        # Admin "Purge now" (e.g. illegal content just arrived): apply the block
                        # filters to already-stored events immediately, off the nightly schedule.
                        try:
                            removed = await _purge_blocks_now()
                            await store.kv_set("block_purge_last", str(int(time.time())))
                            _write_status()
                            logger.info("[nostr-relay] control: purge-blocks removed %d stored event(s)", removed)
                        except Exception as e:
                            logger.warning("[nostr-relay] purge-blocks failed: %s", e)
                    elif cmd.get("cmd") == "prune":
                        # Admin "Run auto-clean now": the age/retention prune had NO trigger at all —
                        # its only caller was the once-a-day _periodic task, which sleeps a full
                        # interval before its first run, so every restart pushed it another 24h out
                        # and a busy node could go indefinitely without ever pruning. `dry_run` counts
                        # without deleting (the delete can span hundreds of thousands of rows).
                        try:
                            if cmd.get("dry_run"):
                                await _refresh_preserve(store)   # same preserve set the real run uses
                                pv = await store.prune_preview()
                                _prune_state["preview"] = pv
                                _prune_state["preview_ts"] = int(time.time())
                                _write_status()
                                logger.info("[nostr-relay] auto-clean DRY RUN: would remove ~%d event(s) "
                                            "(aged=%d expired=%d bridge-dm=%d count-cap=%d)", pv["total"],
                                            pv["aged"], pv["expired"], pv["bridge_dm"], pv["capped"])
                            else:
                                removed = await _prune_fresh()   # logs the count + writes status
                                logger.info("[nostr-relay] control: auto-clean removed %d stored event(s)",
                                            removed)
                        except Exception as e:
                            logger.warning("[nostr-relay] prune failed: %s", e)
                    elif cmd.get("cmd") == "reload-nip05":
                        # Admin edited the NIP-05 identities — re-read and swap in place (the
                        # server reads cfg["nip05"] live, so no restart needed).
                        try:
                            cfg["nip05"] = _read_config()["nip05"]
                            logger.info("[nostr-relay] control: reloaded %d NIP-05 name(s)",
                                        len(cfg["nip05"].get("names") or {}))
                        except Exception as e:
                            logger.warning("[nostr-relay] reload-nip05 failed: %s", e)
                    elif cmd.get("cmd") == "reload-upstream":
                        # Admin changed the upstream relay set (or firehose_max_relays). Reconnect the
                        # live firehose to the new relays IN PLACE — no subprocess restart, so /client
                        # connections + settings writes are never dropped (this used to force a full
                        # relay restart ≈ 90s outage). Re-read cfg, retarget the outbox (send path,
                        # reads cfg["upstream"] live), then respawn the firehose group (receive path).
                        try:
                            fresh = _read_config()
                            # Refresh exactly the keys the firehose group reads on respawn. `direct`
                            # is NOT here: it's driven by nostr_relay_disable_proxy, a restart-key, so
                            # it can't change on this live path — re-reading it would imply otherwise.
                            cfg["upstream"] = fresh["upstream"]
                            cfg["private_relays"] = fresh["private_relays"]
                            cfg["firehose_max_relays"] = fresh["firehose_max_relays"]
                            cfg["ingest_kinds"] = fresh["ingest_kinds"]
                            cfg["operator"] = fresh["operator"]
                            cfg["dvm_enabled"] = fresh["dvm_enabled"]
                            cfg["agent_enabled"] = fresh["agent_enabled"]
                            # Respawn the receive path FIRST; only retarget the send path once it
                            # succeeds, so a respawn failure doesn't leave the outbox publishing to
                            # the new set while the firehose ingests nothing.
                            await _restart_firehose()
                            outbox.upstream = cfg["upstream"]   # next publish targets the new set
                            # The mirror follows the same live change — re-pointed, or emptied.
                            # An operator who realises they aimed it at the wrong relay clears the
                            # box and saves; without this it keeps shipping every new note and vault
                            # entry there indefinitely, with nothing anywhere to say so. Turning it
                            # ON from blank still needs the restart (there is no worker to re-point).
                            if private is not None:
                                private.upstream = cfg["private_relays"]
                                if not cfg["private_relays"]:
                                    logger.info("[nostr-relay] private mirror OFF (relay list cleared)")
                            logger.info("[nostr-relay] control: firehose reconnected to %d upstream "
                                        "relay(s) live (no restart)", len(cfg["upstream"]))
                        except Exception as e:
                            logger.warning("[nostr-relay] reload-upstream failed: %s", e)
                    elif cmd.get("cmd") == "reload-store-config":
                        # Admin changed retention_days / max_events. The nightly prune reads
                        # store.retention_days / store.max_events, which were only set at relay
                        # startup — update them live so the prune respects the admin setting without a
                        # restart (symptom: "I set prune to 0 but old notes still get deleted").
                        try:
                            fresh = _read_config()
                            store.retention_days = fresh["retention_days"]
                            store.max_events = fresh["max_events"]
                            cfg["retention_days"] = fresh["retention_days"]
                            cfg["max_events"] = fresh["max_events"]
                            logger.info("[nostr-relay] control: store config reloaded "
                                        "(retention_days=%s, max_events=%s)",
                                        store.retention_days, store.max_events)
                        except Exception as e:
                            logger.warning("[nostr-relay] reload-store-config failed: %s", e)
                    elif cmd.get("cmd") == "backfill" and cmd.get("pubkey"):
                        pk = cmd["pubkey"]
                        logger.info("[nostr-relay] control: backfill %s", pk[:12])
                        # PIN the synced author so their history is preserved from prune/purge — but do
                        # NOT add them to the operator/gate set (that would grant an arbitrary pubkey
                        # publish/WoT/DM privileges). Persist the pin to relay kv so it survives restart;
                        # extend (never replace) the live preserve set so the next prune can't delete
                        # what we're about to sync.
                        try:
                            pinned = set((await store.kv_get("pinned_pubkeys") or "").split())
                            if pk not in pinned:
                                pinned.add(pk)
                                await store.kv_set("pinned_pubkeys", " ".join(sorted(pinned)))
                        except Exception as e:
                            logger.debug("[nostr-relay] pin persist failed: %s", e)
                        store.extend_preserve_pubkeys({pk})
                        from . import ingest as _ingest
                        asyncio.create_task(_safe(_ingest.backfill_author(
                            store, server, cfg["upstream"], pk,
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
        if _firehose.get("stop") is not None:     # firehose group lives outside `tasks` now
            _firehose["stop"].set()
        for t in (_firehose.get("tasks") or []):
            t.cancel()
        outbox.stop()
        if private is not None:
            private.stop()      # or its worker and retry tasks outlive store.close()
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
    return _relay_db_path() + ".wot_built"


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
    return (time.time() - _read_wot_stamp(cfg)) >= cfg.get("wot_refresh_sec", 604800)


# Coalesce + throttle full WoT rebuilds. Every signup/follow/bot-change drops a "refresh-wot" control
# msg; without this, each one ran a full 37k-follow-graph crawl, so a burst ran several concurrent/
# back-to-back crawls and pegged a core. Mutable dict (no `global` needed): one build at a time, and
# not more often than wot_refresh_min_interval_sec. New members stay instant via the wot-add path.
_wot_refresh_state = {"task": None, "last": 0.0}


async def _build_wot(gate, store, cfg) -> int:
    """Single entry point so every WoT build (initial / daily / manual) uses the same
    depth, pacing and caps. Records the build time so restarts honour the daily cadence."""
    n = await gate.build(
        store, cfg["upstream"], cfg["seeds"],
        depth=cfg["wot_depth"], direct=cfg["direct"],
        batch=cfg["author_batch"], pace=cfg["request_pace_sec"],
        min_followers=cfg["wot_min_followers"], max_members=cfg["wot_max"],
        min_keep_ratio=cfg.get("wot_shrink_guard_ratio", 0.85),
        depth3_crawl_max=cfg.get("wot_depth3_crawl_max", 2500))
    # Refresh the daily stamp only on a CLEAN build (follows resolved AND not a kept-cache partial), so
    # a partial crawl stays "due" and retries next cycle instead of marking the cache fresh.
    if n > len(set(cfg["seeds"]) | set(cfg["operator"])) and not gate.last_build_partial:
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


async def _periodic(stop: asyncio.Event, interval: int, action, name: str, first: int = None) -> None:
    """Run `action` every `interval` seconds. `first` overrides ONLY the initial delay: the loop
    sleeps before acting, so a daily task on a box that restarts more than once a day never fires at
    all (that is what kept the relay's auto-clean from ever running). A short `first` gives it one
    run per process start; None keeps the old sleep-a-full-interval behaviour."""
    delay = interval if first is None else first
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=max(5, delay))
        except asyncio.TimeoutError:
            pass
        delay = interval
        if stop.is_set():
            break
        try:
            await action()
        except Exception as e:
            logger.warning("[nostr-relay] %s loop error: %s", name, e)


_SUPPRESSED_PROXY_NOISE = 0


def _relay_loop_exception_handler(loop, context):
    """Swallow a known-cosmetic websockets-16 proxy-tunnel race, defer everything else.

    When federation is routed through the outbound HTTP proxy (→ Tor) and a CONNECT-tunnelled
    upstream resets right after the proxy's "200 Connection Established", websockets' asyncio client
    calls set_result()/set_exception() on an already-resolved future → asyncio.InvalidStateError,
    surfaced by the loop as a noisy "Fatal error: protocol.data_received() call failed" traceback
    per flaky proxied relay. The connect was failing anyway (it falls back to direct / retries), so
    the relay is unaffected — only the log is. Suppress exactly that signature; anything else goes
    to the default handler so real bugs still surface."""
    global _SUPPRESSED_PROXY_NOISE
    exc = context.get("exception")
    if isinstance(exc, asyncio.InvalidStateError):
        blob = " ".join(str(context.get(k, "")) for k in ("protocol", "transport", "handle", "message"))
        if "HTTPProxyConnection" in blob or "websockets" in blob or "_call_connection_lost" in blob:
            _SUPPRESSED_PROXY_NOISE += 1
            if _SUPPRESSED_PROXY_NOISE % 500 == 1:
                logger.info("[nostr-relay] suppressed %d websockets proxy-tunnel race error(s) "
                            "(cosmetic; federation unaffected)", _SUPPRESSED_PROXY_NOISE)
            return
    loop.default_exception_handler(context)


def _run(cfg: dict) -> None:
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.set_exception_handler(_relay_loop_exception_handler)
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
            # 4s (was 15): the snapshot is a warm-cache over the DURABLE Postgres store, so a quick
            # snapshot is enough; escalate to SIGKILL fast so a slow relay can't blow the service's
            # 10s stop deadline (restart was always hitting the systemd SIGKILL timeout). Re-syncs on boot.
            proc.wait(timeout=4)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
        _relay.proc = None


def restart_nostr_relay() -> dict:
    """Stop the relay subprocess and spawn a fresh one — used to pick up relay code changes
    (the relay otherwise keeps running across the app's own internal restarts).

    WHEN THIS PROCESS DOES NOT OWN THE RELAY (role split: posterchanai-relay.service runs it), the
    local `_relay` handle is empty and the code below would happily _spawn_relay() a SECOND relay as
    a child of the web app — two relays on one Postgres, the newcomer crash-looping on the bound
    :3052. It is reached from an ordinary admin Settings save (admin.py restarts the relay to apply a
    task-topology change), so it is not a rare path.

    Instead, signal the OWNING process to exit and let systemd's Restart=always bring it back with
    the fresh config. The pid comes from the status file relay_status() already trusts, and is
    verified to actually be a relay of THIS repo before signalling — never SIGTERM a pid read from a
    file without checking what it is."""
    from app.role import owns as _owns
    if not _owns("relay"):
        return _restart_relay_elsewhere()

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


def _restart_relay_elsewhere() -> dict:
    """Delegate to the process that owns the relay. Verification (pid alive, cmdline names this repo
    and a relay) lives in ONE place — role.restart_owner_process — so the relay, the git host and any
    future split component cannot drift in how carefully they signal."""
    from app.role import restart_owner_process
    return restart_owner_process(_relay_paths(_relay_db_path())["status"], "relay")


def relay_status() -> dict:
    """Status for the Admin UI: liveness from the subprocess handle, with a fallback to the
    status file (its pid alive + recent ts) so it's correct from any caller; member count comes
    from that file since the gate lives in the relay's own process now."""
    alive = _relay.proc is not None and _relay.proc.poll() is None
    members = 0
    conns = 0
    online = 0
    calls = 0
    block_purge = None
    prune = None
    try:
        with open(_relay_paths(_relay_db_path())["status"]) as f:
            st = json.load(f)
        members = int(st.get("members", 0))
        conns = int(st.get("conns", 0))
        online = int(st.get("online", conns))   # deduped people count; falls back to raw conns
        calls = int(st.get("calls", 0))         # people in a call right now
        block_purge = st.get("block_purge")
        prune = st.get("prune")
        if not alive:
            alive = (time.time() - st.get("ts", 0)) < 90 and _pid_alive(st.get("pid"))
    except Exception:
        pass
    return {"running": bool(alive), "members": members, "conns": conns, "online": online,
            "calls": calls, "block_purge": block_purge, "prune": prune}


def _drop_control(cmd: dict) -> dict:
    """Hand an admin command to the relay subprocess via its control dir (its poller picks it
    up, executes on the relay loop). Atomic write so the poller never sees a partial file.

    Liveness is the STATUS FILE (relay_status), NOT the local `_relay.proc` handle. Under the role
    split the relay is posterchanai-relay.service — not a child of the web app — so `_relay.proc` is
    None in the app process even while the relay is running, and gating on it failed EVERY command
    here with "relay not running": prune/auto-clean, block purge and reload, backfill, delete-author,
    nip05/upstream/store reloads, and the WoT refresh that a NEW SIGNUP fires to get the user through
    the gate. The control dir is a file channel the relay drains no matter who wrote to it, so which
    process owns the handle is irrelevant to whether the command can be delivered."""
    if not relay_status().get("running"):
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


def trigger_wot_add(pubkeys: list) -> dict:
    """Add pubkeys to the relay WoT immediately (new signups), so they can post + get DMs at once."""
    pks = [p for p in (pubkeys or []) if p]
    if not pks:
        return {"ok": True, "added": 0}
    return _drop_control({"cmd": "wot-add", "pubkeys": pks})


def trigger_delete_author(pubkeys: list) -> dict:
    """Purge ALL stored events authored by these pubkeys (e.g. when a bot account is deleted) — its
    profile/posts/app-data, including the kind-0 that carried its nip05/avatar."""
    pks = [p for p in (pubkeys or []) if p]
    if not pks:
        return {"ok": True}
    return _drop_control({"cmd": "delete-author", "pubkeys": pks})


def trigger_backfill(pubkey_hex: str) -> dict:
    """Admin button: ask the relay subprocess to backfill an author's history into the store."""
    if not pubkey_hex:
        return {"ok": False, "error": "no nostr key on your account"}
    return _drop_control({"cmd": "backfill", "pubkey": pubkey_hex})


async def _mark_blocked_relays(store, gate, domains) -> list:
    """Find accounts on the blocked bridge domains and add them to the gate's bridge denylist so
    their writes are rejected at ingest. Returns the pubkeys; does NOT purge stored events. The
    gate denylist is in-memory (load_from_store restores members, not bridged), so this must run on
    every start and on a live blocklist reload to keep live filtering correct."""
    try:
        weak = await store.bridged_pubkeys(domains)            # relay-list / proxy hints
        ident = await store.bridge_identity_pubkeys(domains)   # kind-0 nip05 domain (DomainPolicy)
    except Exception as e:
        logger.warning("[nostr-relay] bridge scan failed: %s", e)
        return []
    # Weak hints (a synced post's proxy tag, a relay-list entry) can show up on a real account that
    # merely cross-posts from the fediverse, so for those we still spare the whole WoT (follows +
    # operators) — they must never be bridged/purged on a hint alone.
    wot = gate.members()   # _members | _operator
    weak_pks = [p for p in (weak or []) if p not in wot]
    gate.add_bridged(weak_pks)
    # DomainPolicy (nostrify): an account whose OWN kind-0 nip05 is on the bridge domain is a mirror
    # account — block it even when followed (member-exemption is what made the blocklist a no-op),
    # but still spare operators / registered local users so the purge can never touch a real account.
    ops = gate.operators()
    ident_pks = [p for p in (ident or []) if p not in ops]
    gate.add_bridged_identity(ident_pks)
    return list(set(weak_pks) | set(ident_pks))


async def _apply_blocked_relays(store, gate, domains) -> int:
    """Mark bridged accounts in the gate AND purge their stored events — the retroactive cleanup
    used by the nightly / manual block-purge. Startup + reload use _mark_blocked_relays (mark only).
    Returns the number of events removed (so the purge total reported to the UI includes bridges)."""
    pks = await _mark_blocked_relays(store, gate, domains)
    if not pks:
        return 0
    try:
        removed = await store.delete_pubkeys(list(pks))
    except Exception as e:
        removed = 0
        logger.warning("[nostr-relay] bridge purge failed: %s", e)
    logger.info("[nostr-relay] bridge purge: %d account(s) on %d domain(s), removed %d event(s)",
                len(pks), len(domains), removed)
    return removed


def trigger_block_reload() -> dict:
    """Re-read the block filters into the running relay's live ingest gate (no restart). Does NOT
    touch already-stored events — use trigger_block_purge() for that."""
    return _drop_control({"cmd": "reload-blocks"})


def trigger_block_purge() -> dict:
    """Admin "Purge now": one-shot retroactive purge of already-stored events matching the configured
    blocked pubkeys / words / languages / bridges. Heavy (full-corpus scan); normally runs nightly,
    this forces it immediately (e.g. illegal content just arrived)."""
    return _drop_control({"cmd": "purge-blocks"})


def trigger_prune(dry_run: bool = False) -> dict:
    """Admin "Run auto-clean now": run the age/retention prune immediately instead of waiting for the
    once-a-day loop. `dry_run=True` only COUNTS what would go (no deletes) — use it first, the delete
    can be very large on a relay that has never completed a prune cycle."""
    return _drop_control({"cmd": "prune", "dry_run": bool(dry_run)})


def trigger_nip05_reload() -> dict:
    """Re-read the NIP-05 identities (Admin → Relay) into the running relay without a restart."""
    return _drop_control({"cmd": "reload-nip05"})


def trigger_upstream_reload() -> dict:
    """Reconnect the live firehose + outbox to a new upstream relay set (Admin → Relay) WITHOUT
    restarting the relay subprocess — so /client connections and settings writes aren't dropped."""
    return _drop_control({"cmd": "reload-upstream"})


def trigger_store_config_reload() -> dict:
    """Apply changed retention_days / max_events to the running relay's store live (Admin → Relay) so
    the nightly prune respects the admin setting without a restart."""
    return _drop_control({"cmd": "reload-store-config"})
