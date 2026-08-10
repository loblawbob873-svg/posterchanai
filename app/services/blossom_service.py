"""Built-in Blossom media server (BUD-01/02/06).

Blossom stores binary blobs addressed by their **sha256** and authenticated with a
signed Nostr event (kind 24242). This is the server half: it lets our own users —
those granted the per-user `can_blossom` privilege (Admin → Users), identified by the
Nostr key they linked in User Settings — upload/list/delete blobs, and serves any blob
publicly by hash.

Design notes (scales to many concurrent users without pegging a CPU):
  * BIP340 verification and full-body sha256 are CPU-bound. They're written as plain
    SYNC functions here (`verify_auth`, `compute_sha256`) so the router can run them off
    the event loop via `asyncio.to_thread` — the request loop never blocks on crypto.
    (bip340 also auto-uses libsecp256k1/coincurve when present: ~0.03ms vs ~67ms/verify.)
  * Content-addressed → identical bytes uploaded by N users are stored ONCE (the row is
    keyed by sha256); the first uploader owns it.
  * Storage backend is pluggable: `proxy` (the shared PosterChanAI storage server, the
    default) or `local` (a blob dir on this node). Metadata always lives in the local
    `blossom_blobs` table.
  * Per-blob expiry (`blossom_blob_ttl_days`) is swept by a single low-frequency daemon
    thread (`start_blossom_cleanup`), not per-request.
"""

import asyncio
import hashlib
import json
import logging
import os
import shutil
import threading
import time
import base64

import httpx
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError

from app.database import SessionLocal
from app.models import BlossomBlob, User
from app.utils import lb_auth
from app.services import settings_store, keystore
from app.services.nostr import nostr_service, event as nostr_event
from app.services.proxy_utils import afallback_transport

logger = logging.getLogger(__name__)

# Repo root: app/services/blossom_service.py -> three dirs up (flat module, NOT a package).
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_DEFAULT_BLOB_DIR = os.environ.get("POSTERCHANAI_BLOSSOM_PATH") or os.path.join(_REPO_ROOT, "data", "blossom")

# Username the blobs live under on the storage server (proxy backend).
_PROXY_USER = "_blossom"
_AUTH_KIND = 24242
_MIRROR_RETRIES = 3        # DR mirror: attempts per server before giving up (4xx = give up at once)
_CLEANUP_INTERVAL_SEC = 600   # sweep expired blobs every 10 min (idle, low CPU)


# --- config -----------------------------------------------------------------

def _cfg(db: Session) -> dict:
    # Kept fresh (no memo) so admin config changes take effect at once. It's off the hot cache-hit
    # path anyway — every read helper checks the byte cache BEFORE calling _cfg, so this only runs on
    # a cache miss (where a network/disk fetch dominates and one settings read is negligible).
    rows = settings_store.prefixed("blossom_")

    def g(key, default=""):
        v = rows.get(key)
        return v if v not in (None, "") else default

    def gi(key, default):
        try:
            return int(g(key, str(default)))
        except (TypeError, ValueError):
            return default

    storage_url = settings_store.get("storage_server_url", "")
    storage_url = storage_url.strip() if storage_url else ""

    backend = (g("blossom_storage_backend", "proxy") or "proxy").lower()
    # Proxy needs a storage server; without one, fall back to local so uploads still work.
    if backend == "proxy" and not storage_url.startswith(("http://", "https://")):
        backend = "local"

    cfg = {
        "enabled": g("blossom_enabled", "false").lower() == "true",
        "public_url": g("blossom_public_url", "").rstrip("/"),
        "ttl_days": gi("blossom_blob_ttl_days", 0),
        "max_upload_mb": gi("blossom_max_upload_mb", 100),
        "user_quota_gb": gi("blossom_user_quota_gb", 0),   # 0 = unlimited (see usage_for_pubkey)
        "backend": backend,
        "blob_dir": g("blossom_storage_path", "") or _DEFAULT_BLOB_DIR,
        "storage_url": storage_url,
        "cache_mb": gi("blossom_cache_mb", 512),
        # DR: external Blossom servers to mirror each uploaded blob to (space/newline-separated).
        "mirror_servers": [s for s in (g("blossom_mirror_servers", "")).split()
                           if s.startswith(("http://", "https://"))],
    }
    return cfg


def is_enabled(db: Session) -> bool:
    return (settings_store.get("blossom_enabled", "") or "").lower() == "true"


# --- kind-10063 BUD-03 user server list (so clients fail over by hash) -------
_SERVER_LIST_KIND = 10063


def server_list_urls(db: Session) -> list:
    """Blossom servers to advertise (kind-10063): our public Blossom URL + the configured mirrors,
    deduped and order-preserved (the client tries them in order)."""
    cfg = _cfg(db)
    out, seen = [], set()
    for u in (([cfg["public_url"]] if cfg["public_url"] else []) + cfg["mirror_servers"]):
        u = (u or "").rstrip("/")
        if u and u not in seen:
            seen.add(u)
            out.append(u)
    return out


async def publish_server_list(seckey: bytes, urls: list) -> bool:
    """Publish a kind-10063 user server list (one `server` tag per URL) so clients can retry a blob
    by hash across all of them. Replaceable per-pubkey. Returns True on a relay accept."""
    if not urls:
        return False
    tags = [["server", u] for u in urls]
    ev = nostr_event.build_event(seckey, _SERVER_LIST_KIND, "", tags=tags)
    port = int(settings_store.get("nostr_relay_port", "3052") or "3052")
    try:
        return bool(await nostr_service.relay.publish([f"ws://127.0.0.1:{port}"], ev))
    except Exception as e:
        logger.warning("[blossom] kind-10063 publish failed: %s", e)
        return False


async def publish_operator_server_list(db: Session):
    """Advertise the OPERATOR's Blossom server list (kind-10063). Called on startup + when the Blossom
    mirror/public-url settings change. No-op if Blossom is off, no servers, or no operator key."""
    if not is_enabled(db):
        return
    urls = server_list_urls(db)
    if not urls:
        return
    nsec = keystore.get_operator_nsec()
    if not nsec:
        return
    try:
        sk = nostr_service.decode_seckey(nsec)
    except Exception:
        return
    if await publish_server_list(sk, urls):
        logger.info("[blossom] advertised operator kind-10063 (%d servers)", len(urls))


# --- in-RAM blob cache ------------------------------------------------------
# Serves hot blobs straight from memory so repeated GETs don't re-read disk (local) or
# re-fetch over the storage proxy (nas) — saving disk I/O and SSD wear. Bounded by the
# `blossom_cache_mb` setting; LRU eviction; per-item cap so one huge blob can't evict
# everything. Uploads also seed the cache (the bytes are already in RAM). The OS page
# cache helps too, but this also eliminates the cross-node HTTP round-trip on the proxy
# backend. Thread-safe (reads run in the event loop + the to_thread pool).
from collections import OrderedDict  # noqa: E402

_CACHE_ITEM_CAP = 16 * 1024 * 1024   # don't cache single blobs larger than this
_cache: "OrderedDict[str, bytes]" = OrderedDict()
_cache_bytes = 0
_cache_lock = threading.Lock()


def _cache_get(sha256: str) -> bytes | None:
    with _cache_lock:
        data = _cache.get(sha256)
        if data is not None:
            _cache.move_to_end(sha256)   # mark most-recently-used
        return data


def _cache_put(sha256: str, data: bytes, budget: int) -> None:
    global _cache_bytes
    n = len(data)
    if budget <= 0 or n > _CACHE_ITEM_CAP or n > budget:
        return
    with _cache_lock:
        if sha256 in _cache:
            _cache_bytes -= len(_cache.pop(sha256))
        _cache[sha256] = data
        _cache_bytes += n
        while _cache_bytes > budget and _cache:
            _, evicted = _cache.popitem(last=False)   # evict least-recently-used
            _cache_bytes -= len(evicted)


def _cache_drop(sha256: str) -> None:
    global _cache_bytes
    with _cache_lock:
        if sha256 in _cache:
            _cache_bytes -= len(_cache.pop(sha256))


# --- blob metadata cache ----------------------------------------------------
# The app DB is POSTGRES (shared with the relay), so every GET's `db.query(BlossomBlob)` is a
# round-trip over the local socket + a connection out of the shared pool — the ceiling at high
# read RPS. A blob row is IMMUTABLE once written (content-addressed: sha256/size/mime/storage/path
# never change), so cache the small metadata tuple and skip Postgres entirely on hot reads. Combined
# with the byte cache, a hot blob GET touches neither the DB nor the network. Invalidated only on
# delete. Bounded entry count (each entry is a few hundred bytes → a full cache is a few MB).
from collections import namedtuple  # noqa: E402

# `expires_at` is carried because a READ has to be able to say the blob is on its way out: folder
# sync HEADs before uploading and skips the body when the bytes are here, which also skips the save
# that would have cleared the stamp. Defaulted, so nothing that builds one positionally breaks.
BlobMeta = namedtuple("BlobMeta", "sha256 pubkey size mime created_at storage path expires_at",
                      defaults=(None,))
_META_MAX = 50000
_meta_cache: "OrderedDict[str, BlobMeta]" = OrderedDict()
_meta_lock = threading.Lock()


def _meta_from_row(blob: BlossomBlob) -> BlobMeta:
    return BlobMeta(blob.sha256, blob.pubkey, blob.size, blob.mime, blob.created_at, blob.storage,
                    blob.path, blob.expires_at)


def _meta_put(m: BlobMeta) -> None:
    with _meta_lock:
        _meta_cache[m.sha256] = m
        _meta_cache.move_to_end(m.sha256)
        while len(_meta_cache) > _META_MAX:
            _meta_cache.popitem(last=False)


def _meta_drop(sha256: str) -> None:
    with _meta_lock:
        _meta_cache.pop(sha256, None)


def drop_meta(sha256: str) -> None:
    """Public metadata-cache eviction. Call AFTER a delete commits (a drop BEFORE commit can be
    re-poisoned by a concurrent GET that re-queries the still-visible row under MVCC)."""
    _meta_drop(sha256)


def revalidate_meta(db: Session, sha256: str) -> None:
    """Self-heal for the read path: when a read finds the bytes gone, evict the cached metadata ONLY
    if the DB row is actually gone (a delete we raced). A transient storage outage returns the same
    'no bytes' but the row is still present — evicting then would cold-wipe the hot cache and stampede
    Postgres on recovery (every GET re-querying get_blob_meta), so leave valid entries in place."""
    try:
        if db.query(BlossomBlob.sha256).filter(BlossomBlob.sha256 == sha256).first() is None:
            _meta_drop(sha256)
    except Exception:
        pass


def get_blob_meta(db: Session, sha256: str):
    """Return a blob's metadata (BlobMeta) for reads WITHOUT hitting Postgres when it's hot. Serves
    from the metadata cache; on a miss, queries the row ONCE, caches it, and returns. None if unknown.
    Use for GET/HEAD only — DELETE needs the live ORM row (`db.delete`), so it keeps its own query."""
    m = None
    with _meta_lock:
        m = _meta_cache.get(sha256)
        if m is not None:
            _meta_cache.move_to_end(sha256)
    if m is not None:
        return m
    blob = db.query(BlossomBlob).filter(BlossomBlob.sha256 == sha256).first()
    if not blob:
        return None
    m = _meta_from_row(blob)
    _meta_put(m)
    return m


async def _aiter_bytes(data: bytes):
    yield data


# --- shared pooled HTTP client (storage-proxy I/O to nas) -------------------
# One connection pool, reused across ALL blob reads/writes/deletes. A fresh httpx.AsyncClient per
# request (the old pattern) opened a new TCP+TLS connection to the storage node on every cache-miss
# GET — at many requests/second that floods nas with handshakes and adds latency to every miss. A
# shared client keep-alives the connections instead. Bound so a burst can't open unlimited sockets.
# (The DR mirror worker keeps its OWN client — different event loop + fallback transport.)
_http_client: "httpx.AsyncClient | None" = None
_http_client_lock = threading.Lock()


def _client() -> "httpx.AsyncClient":
    global _http_client
    c = _http_client
    if c is not None and not c.is_closed:
        return c
    with _http_client_lock:
        if _http_client is None or _http_client.is_closed:
            # High max_connections: each in-flight video/audio STREAM holds one connection to nas for
            # its whole playback, so a low cap would self-throttle a busy media server (the 129th
            # concurrent stream blocking on pool acquisition). 1000 is generous headroom — far below
            # the process FD limit — while max_keepalive bounds IDLE reuse between bursts. Cacheable
            # reads/uploads/deletes are short and release their connection immediately.
            _http_client = httpx.AsyncClient(
                timeout=httpx.Timeout(120.0, connect=10.0),
                limits=httpx.Limits(max_connections=1000, max_keepalive_connections=100,
                                    keepalive_expiry=30.0),
            )
    return _http_client


async def aclose_http() -> None:
    """Close the shared client on shutdown (wired into the app's port-3051 shutdown block)."""
    global _http_client
    c = _http_client
    _http_client = None
    if c is not None and not c.is_closed:
        try:
            await c.aclose()
        except Exception:
            pass


# --- authorization ----------------------------------------------------------

_operator_cache = {"ts": 0.0, "set": frozenset()}
# 60s: this set is the upload/delete AUTHORIZATION set, so a long TTL is an auth-freshness regression —
# a revoked key stays accepted and a newly-added bot key stays rejected (bots aren't User rows, so
# is_pubkey_allowed has no live-DB fallback for them) until it expires. The rebuild scans users+bots +
# decodes seckeys on the event loop, but that's only on the (infrequent) upload/delete path — never
# reads — so 60s is a fine balance. Don't raise it for perf; reads don't touch this.
_OPERATOR_TTL = 60.0


def invalidate_operator_cache() -> None:
    """Force the next is_pubkey_allowed() to rescan users+bots. Call right after a bot row is
    created/deleted so a fresh bot can upload immediately (and a deleted one loses access at once)
    instead of waiting up to _OPERATOR_TTL — bots are authorized via this set, not the whitelist."""
    _operator_cache["ts"] = 0.0


def _operator_pubkeys(db: Session) -> frozenset:
    """The node's OWN Nostr identities — every linked user's and bot's key (same set the relay
    trusts as operators). These may always upload (it's how the bots post effect media). Cached
    briefly so a busy upload stream doesn't rescan users+bots each time."""
    now = time.time()
    if now - _operator_cache["ts"] < _OPERATOR_TTL:
        return _operator_cache["set"]
    out = set()
    try:
        from app.models import Bot
        for u in db.query(User).filter(User.nostr_nsec.isnot(None)).all():
            try:
                out.add(nostr_service.derive_pubkey(nostr_service.decode_seckey(u.nostr_nsec)))
            except Exception:
                pass
        for b in db.query(Bot).all():
            try:
                nsec = (json.loads(b.config or "{}")).get("nostr_nsec")
            except (ValueError, TypeError):
                continue
            if nsec:
                try:
                    out.add(nostr_service.derive_pubkey(nostr_service.decode_seckey(nsec)))
                except Exception:
                    pass
        # Only cache a COMPLETE scan — a mid-scan DB error must not pin a partial set for the TTL.
        _operator_cache["ts"] = now
        _operator_cache["set"] = frozenset(out)
    except Exception as e:
        logger.debug("[blossom] operator key collection failed: %s", e)
    return frozenset(out) if not _operator_cache["set"] else _operator_cache["set"]


_whitelist_cache = {"ts": 0.0, "val": "", "set": frozenset()}


def _whitelist_pubkeys(db: Session) -> frozenset:
    """Hex pubkeys from the `blossom_whitelist` setting (npub/hex list) — lets the admin grant
    Blossom upload to anyone WITHOUT them creating an AI account. Cached briefly per raw value."""
    val = settings_store.get("blossom_whitelist", "") or ""
    now = time.time()
    if val == _whitelist_cache["val"] and now - _whitelist_cache["ts"] < _OPERATOR_TTL:
        return _whitelist_cache["set"]
    out = set()
    for tok in val.replace(",", "\n").split():
        h = nostr_service.to_pubkey_hex(tok.strip())
        if h:
            out.add(h)
    _whitelist_cache.update(ts=now, val=val, set=frozenset(out))
    return _whitelist_cache["set"]


def is_pubkey_allowed(db: Session, pubkey_hex: str) -> bool:
    """A pubkey may upload/delete iff: it's one of the node's own operator keys (linked users +
    bots, so the bots can post effect media), OR it's in the `blossom_whitelist` setting (admin
    allowlist — no AI account needed), OR it's the Nostr key linked by a web user who is an admin
    or has the `can_blossom` privilege."""
    if pubkey_hex in _operator_pubkeys(db) or pubkey_hex in _whitelist_pubkeys(db):
        return True
    # Shared-cluster peers (DVM) upload their media job-results (image/music/video) to the shared
    # Blossom — auto-authorize the configured peer npubs so no per-node can_blossom grant is needed.
    # The peer list is already the DVM trust set (who may exchange jobs/results with this node).
    try:
        from app.services import nostr_dvm
        if pubkey_hex in nostr_dvm.peer_pubkeys():
            return True
    except Exception:
        pass
    try:
        npub = nostr_service.npub_of(pubkey_hex)
    except Exception:
        return False
    u = db.query(User).filter(User.nostr_npub == npub).first()
    return bool(u and (getattr(u, "is_admin", False) or getattr(u, "can_blossom", False)))


def verify_auth(authorization: str, action: str, sha256: str | None = None) -> str:
    """Verify a Blossom `Authorization: Nostr <base64-event>` header (BUD-01 §auth).

    CPU-bound (Schnorr verify) — call via asyncio.to_thread. Returns the signer's hex
    pubkey, or raises ValueError with a client-safe reason.
    """
    if not authorization:
        raise ValueError("missing Authorization header")
    parts = authorization.strip().split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "nostr":
        raise ValueError("Authorization must be 'Nostr <base64-event>'")
    try:
        event = json.loads(base64.b64decode(parts[1]))
    except Exception:
        raise ValueError("malformed authorization event")

    if int(event.get("kind", 0)) != _AUTH_KIND:
        raise ValueError("authorization event must be kind 24242")
    if not nostr_event.verify_event(event):
        raise ValueError("invalid event signature")

    tags = event.get("tags", [])
    verbs = [t[1] for t in tags if len(t) >= 2 and t[0] == "t"]
    if action not in verbs:
        raise ValueError(f"authorization not valid for '{action}'")

    # Mandatory, future-dated expiration (BUD-01) bounds replay of the auth token.
    exp = next((t[1] for t in tags if len(t) >= 2 and t[0] == "expiration"), None)
    if exp is None:
        raise ValueError("authorization missing expiration tag")
    try:
        if int(exp) <= time.time():
            raise ValueError("authorization expired")
    except (TypeError, ValueError):
        raise ValueError("authorization expired")

    # For upload/delete the event must commit to the blob hash via an `x` tag.
    if sha256 is not None:
        xs = [t[1] for t in tags if len(t) >= 2 and t[0] == "x"]
        if sha256 not in xs:
            raise ValueError("authorization x tag does not match blob hash")

    return event["pubkey"]


# --- hashing ----------------------------------------------------------------

def compute_sha256(data: bytes) -> str:
    """sha256 hex of a blob. CPU-bound on large bodies — call via asyncio.to_thread."""
    return hashlib.sha256(data).hexdigest()


def compute_sha256_file(path: str, chunk: int = 1 << 20) -> str:
    """sha256 hex of a file, read in chunks so a multi-GB blob never lands in RAM.
    CPU/IO-bound — call via asyncio.to_thread."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(chunk), b""):
            h.update(block)
    return h.hexdigest()


# --- storage backends -------------------------------------------------------

def _local_path(blob_dir: str, sha256: str) -> str:
    return os.path.join(blob_dir, sha256[:2], sha256)


def _proxy_headers() -> dict:
    return lb_auth.headers()


async def _proxy_put(storage_url: str, sha256: str, data: bytes, mime: str) -> str:
    """Upload bytes to the storage server under _blossom/blossom/<ab>/<sha>. Returns rel-path."""
    subdir = f"blossom/{sha256[:2]}"
    url = f"{storage_url.rstrip('/')}/api/storage/upload-file"
    files = {"file": (sha256, data, mime or "application/octet-stream")}
    form = {"username": _PROXY_USER, "path": subdir}
    r = await _client().post(url, headers=_proxy_headers(), files=files, data=form)
    if r.status_code != 200:
        raise RuntimeError(f"storage upload failed: HTTP {r.status_code} {r.text[:200]}")
    return (r.json().get("path") or f"{subdir}/{sha256}")


async def _proxy_put_file(storage_url: str, sha256: str, path: str, mime: str) -> str:
    """Stream a file to the storage server (no full in-memory copy) under
    _blossom/blossom/<ab>/<sha>. Returns rel-path. httpx reads the file object in chunks."""
    subdir = f"blossom/{sha256[:2]}"
    url = f"{storage_url.rstrip('/')}/api/storage/upload-file"
    form = {"username": _PROXY_USER, "path": subdir}
    with open(path, "rb") as fh:
        files = {"file": (sha256, fh, mime or "application/octet-stream")}
        # No TOTAL timeout (a multi-GB VOD upload can outlast the shared client's 120s cap), but keep
        # finite read/write timeouts so a stalled connection fails instead of pinning the task forever.
        r = await _client().post(url, headers=_proxy_headers(), files=files, data=form,
                                 timeout=httpx.Timeout(None, connect=10.0, read=600.0, write=600.0))
    if r.status_code != 200:
        raise RuntimeError(f"storage upload failed: HTTP {r.status_code} {r.text[:200]}")
    return (r.json().get("path") or f"{subdir}/{sha256}")


async def _mirror_blob(sha256: str, data: bytes, mime: str, servers: list):
    """DR: push a stored blob to external Blossom server(s) via BUD-02, signed by the operator key.
    Best-effort and fully isolated — failures (access denied, server down) are logged, never raised,
    so mirroring can't affect the user's upload. The mirror must accept our operator pubkey (e.g. a
    backup node we control); public servers that don't will just log a skip."""
    nsec = keystore.get_operator_nsec()
    if not nsec:
        return
    try:
        sk = nostr_service.decode_seckey(nsec)
    except Exception:
        return
    exp = str(int(time.time()) + 300)
    auth = nostr_event.build_event(sk, _AUTH_KIND, "Mirror blob",
                                   tags=[["t", "upload"], ["x", sha256], ["expiration", exp]])
    header = "Nostr " + base64.b64encode(json.dumps(auth).encode()).decode()
    headers = {"Authorization": header, "Content-Type": mime or "application/octet-stream"}
    for srv in servers:
        url = srv.rstrip("/") + "/upload"
        for attempt in range(1, _MIRROR_RETRIES + 1):
            try:
                # Prefer the built-in HTTP proxy; the fallback transport drops to a DIRECT connection
                # if the proxy can't be reached (safe here — the PUT is content-addressed/idempotent).
                async with httpx.AsyncClient(transport=afallback_transport(),
                                             timeout=httpx.Timeout(120.0, connect=10.0)) as client:
                    r = await client.put(url, content=data, headers=headers)
                if r.status_code // 100 == 2:
                    logger.info("[blossom] mirrored %s → %s", sha256[:12], srv)
                    break
                if r.status_code // 100 == 4:        # access denied / bad request — won't fix on retry
                    logger.warning("[blossom] mirror %s → %s rejected: HTTP %s %s (giving up)",
                                   sha256[:12], srv, r.status_code, (r.text or "")[:120])
                    break
                logger.warning("[blossom] mirror %s → %s HTTP %s (attempt %d/%d)",
                               sha256[:12], srv, r.status_code, attempt, _MIRROR_RETRIES)
            except Exception as e:
                logger.warning("[blossom] mirror %s → %s failed: %s (attempt %d/%d)",
                               sha256[:12], srv, e, attempt, _MIRROR_RETRIES)
            if attempt < _MIRROR_RETRIES:
                await asyncio.sleep(2 * attempt)     # backoff between retries
        else:
            logger.warning("[blossom] mirror %s → %s gave up after %d attempts",
                           sha256[:12], srv, _MIRROR_RETRIES)


# --- background mirror worker (own thread + queue) --------------------------
# Mirroring runs on a DEDICATED thread with its own event loop, fed by a bounded queue, so it never
# competes with the app's request loop ("keep the app good") and drains ONE blob at a time, paced
# ("queue requests to save bandwidth and not DDoS the mirror servers"). The queue is bounded by total
# queued bytes: if mirrors are down and the backlog grows past the cap we drop + log (the blob is safe
# locally and re-mirrors on a future upload), so a stall can never OOM the app.
import queue as _queue  # noqa: E402

_MIRROR_QUEUE_MAX_BYTES = 256 * 1024 * 1024   # cap the in-flight mirror backlog
_MIRROR_PACE_SEC = 1.0                         # gap between blobs (rate-limit the mirror servers)
_mirror_q: "_queue.Queue" = _queue.Queue()
_mirror_q_bytes = 0
_mirror_q_lock = threading.Lock()
_mirror_thread: "threading.Thread | None" = None
_mirror_stop = threading.Event()


def _enqueue_mirror(sha256: str, data: bytes, mime: str, servers: list) -> None:
    """Queue a freshly-stored blob for the background mirror worker. Non-blocking; bounded by total
    queued bytes (drops + logs over the cap). Lazily starts the worker thread on first use."""
    global _mirror_q_bytes
    n = len(data)
    with _mirror_q_lock:
        if _mirror_q_bytes + n > _MIRROR_QUEUE_MAX_BYTES:
            logger.warning("[blossom] mirror backlog full (%.0f MB) — dropping %s",
                           _mirror_q_bytes / 1048576, sha256[:12])
            return
        _mirror_q_bytes += n
    _mirror_q.put((sha256, data, mime, list(servers)))
    _ensure_mirror_worker()


def _ensure_mirror_worker() -> None:
    global _mirror_thread
    if _mirror_thread and _mirror_thread.is_alive():
        return
    _mirror_stop.clear()
    _mirror_thread = threading.Thread(target=_mirror_worker, name="blossom-mirror", daemon=True)
    _mirror_thread.start()


def _mirror_worker() -> None:
    """Drain the mirror queue one blob at a time on a private event loop, paced between blobs."""
    global _mirror_q_bytes
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        while not _mirror_stop.is_set():
            try:
                sha256, data, mime, servers = _mirror_q.get(timeout=1.0)
            except _queue.Empty:
                continue
            try:
                loop.run_until_complete(_mirror_blob(sha256, data, mime, servers))
            except Exception as e:
                logger.warning("[blossom] mirror worker error on %s: %s", sha256[:12], e)
            finally:
                with _mirror_q_lock:
                    _mirror_q_bytes = max(0, _mirror_q_bytes - len(data))
                _mirror_q.task_done()
            _mirror_stop.wait(_MIRROR_PACE_SEC)   # pace between blobs
    finally:
        loop.close()


async def save_blob(db: Session, pubkey: str, data: bytes, mime: str, mirror: bool = True,
                    private: bool = False, expires_days: int = 0, filename: str = "",
                    keep: bool = False) -> dict:
    """Persist a blob (dedup by sha256) and record its row. Returns a descriptor dict
    (without `url`, which the router fills from the request base). `expires_days` > 0 stamps an
    explicit per-blob TTL (expires_at) so transient blobs — e.g. agent workspace backups — are swept
    regardless of the global `blossom_blob_ttl_days`, instead of piling up forever.
    `filename` is the uploader's original name, kept per-owner for listings and downloads.
    `keep` marks the blob exempt from the age sweep forever (encrypted-drive content — Notes
    attachments, music, the files index — whose only copy is here)."""
    cfg = _cfg(db)
    sha256 = await asyncio.to_thread(compute_sha256, data)
    size = len(data)

    existing = db.query(BlossomBlob).filter(BlossomBlob.sha256 == sha256).first()
    if existing:
        # Already stored (possibly by another user) — content-addressed, so nothing to write.
        # Retention is governed live by the admin TTL setting (see _cleanup_once), keyed off
        # created_at, so re-uploads don't need to re-stamp anything.
        # The one thing that DOES need re-stamping is the explicit per-blob TTL: dedup means these
        # identical bytes may now be referenced by something with a different lifetime. A save that
        # wants the blob KEPT (expires_days=0 — a chat image) must clear a TTL a transient artifact
        # stamped earlier, or the permanent reference is swept out from under it; a save that wants a
        # TTL only ever pushes the expiry LATER, never sooner.
        _want = (int(time.time()) + int(expires_days) * 86400) if expires_days and int(expires_days) > 0 else None
        _cur = existing.expires_at or None
        _dirty = False
        if _cur and (_want is None or _want > _cur):
            existing.expires_at = _want
            _dirty = True
        # `keep` only ever goes False→True, never back: dedup means one set of bytes can be both a
        # throwaway chat image and a Notes attachment, and the reference that must survive wins. The
        # same asymmetry as the TTL re-stamp above, for the same reason.
        if keep and not existing.keep:
            existing.keep = True
            _dirty = True
        if _dirty:
            try:
                db.commit()
            except Exception:
                db.rollback()
        # These bytes already exist, but THIS user may not have referenced them before — without this
        # their upload would 200 and the file would never show up in their own drive.
        add_owner(db, sha256, pubkey, filename)
        _meta_put(_meta_from_row(existing))
        return _descriptor_fields(existing)

    # End the read transaction BEFORE the upload — the same reason save_blob_file does it below, and
    # this variant is the one the CHAT ATTACHMENT path uses (artifact_store.save_bytes). A video
    # attached to `extractaudio` held this connection idle-in-transaction past Postgres'
    # idle_in_transaction_session_timeout (60s); Postgres killed it, and the next statement after the
    # upload died with "server closed the connection unexpectedly", taking the websocket down so the
    # command produced no result and no error. pool_pre_ping can't catch it — the connection is
    # already checked out and held. Reads above are done; a fresh txn opens on the insert.
    db.rollback()

    if cfg["backend"] == "proxy":
        path = await _proxy_put(cfg["storage_url"], sha256, data, mime)
        storage = "proxy"
    else:
        path = _local_path(cfg["blob_dir"], sha256)

        def _write():
            os.makedirs(os.path.dirname(path), exist_ok=True)
            tmp = path + ".tmp"
            with open(tmp, "wb") as f:
                f.write(data)
            os.replace(tmp, path)

        await asyncio.to_thread(_write)
        storage = "local"

    now = int(time.time())
    # expires_at: an explicit per-blob TTL when expires_days>0 (transient artifacts); else NULL, and
    # ordinary retention is driven live by the admin `blossom_blob_ttl_days` setting against created_at.
    _exp = (now + int(expires_days) * 86400) if expires_days and int(expires_days) > 0 else None
    blob = BlossomBlob(
        sha256=sha256, pubkey=pubkey, size=size, mime=mime or None, created_at=now,
        expires_at=_exp, storage=storage, path=path, private=bool(private), keep=bool(keep),
    )
    db.add(blob)
    try:
        db.commit()
    except IntegrityError:
        # Two concurrent first-uploads of the SAME new bytes race on the sha256 primary key — one
        # commit wins, the other hits a duplicate-key IntegrityError. The bytes are content-addressed
        # and already stored (identical), so this is just a dedup hit: roll back, re-query the row the
        # winner committed, and return its descriptor (same as the pre-existing-blob path above).
        db.rollback()
        winner = db.query(BlossomBlob).filter(BlossomBlob.sha256 == sha256).first()
        if winner is not None:
            add_owner(db, sha256, pubkey, filename)   # the race loser still owns a reference
            _cache_put(sha256, data, cfg["cache_mb"] * 1024 * 1024)   # bytes are identical + in RAM — seed the cache like the normal path
            _meta_put(_meta_from_row(winner))
            return _descriptor_fields(winner)
        raise
    # Seed the read + metadata caches — the bytes are already in RAM and the row is immutable, so a
    # fetch right after upload (the common case) touches neither disk/proxy nor Postgres.
    add_owner(db, sha256, pubkey, filename)      # first owner of these bytes
    _cache_put(sha256, data, cfg["cache_mb"] * 1024 * 1024)
    _meta_put(_meta_from_row(blob))
    # DR: hand the new blob to the background mirror worker (own thread + queue) so mirroring never
    # touches the request's event loop and is paced/serialised (saves bandwidth, polite to mirrors).
    # Only newly-stored blobs are queued — a re-upload of the same hash is already mirrored.
    if mirror and cfg["mirror_servers"]:
        _enqueue_mirror(sha256, data, mime, cfg["mirror_servers"])
    return _descriptor_fields(blob)


async def save_blob_file(db: Session, pubkey: str, path: str, mime: str) -> dict:
    """Like save_blob, but for a large file on disk (e.g. a recorded stream in tmpfs): hash and
    upload by STREAMING from the path so a multi-GB blob never sits fully in RAM. Dedup by sha256.
    Does not seed the RAM byte-cache (too big) and does not auto-mirror (would re-read the file).
    Returns the descriptor dict. The caller owns the source file (delete it after)."""
    cfg = _cfg(db)
    sha256 = await asyncio.to_thread(compute_sha256_file, path)
    size = await asyncio.to_thread(os.path.getsize, path)

    existing = db.query(BlossomBlob).filter(BlossomBlob.sha256 == sha256).first()
    if existing:
        # These bytes already exist, but THIS user may not have referenced them before — without this
        # their upload would 200 and the file would never show up in their own drive.
        add_owner(db, sha256, pubkey)
        _meta_put(_meta_from_row(existing))
        return _descriptor_fields(existing)

    # End the read transaction BEFORE the (minutes-long, multi-GB) upload — otherwise the connection sits
    # idle-in-transaction and Postgres' idle_in_transaction_session_timeout (60s) kills it, so the commit
    # below fails and the blob is never recorded. Reads above are done; a fresh txn opens on the insert.
    db.rollback()

    if cfg["backend"] == "proxy":
        stored_path = await _proxy_put_file(cfg["storage_url"], sha256, path, mime)
        storage = "proxy"
    else:
        stored_path = _local_path(cfg["blob_dir"], sha256)

        def _copy():
            os.makedirs(os.path.dirname(stored_path), exist_ok=True)
            tmp = stored_path + ".tmp"
            # Stream copy (tmpfs → blob dir are different filesystems, so os.link would EXDEV).
            with open(path, "rb") as src, open(tmp, "wb") as dst:
                shutil.copyfileobj(src, dst, 1 << 20)
            os.replace(tmp, stored_path)

        await asyncio.to_thread(_copy)
        storage = "local"

    now = int(time.time())
    blob = BlossomBlob(
        sha256=sha256, pubkey=pubkey, size=size, mime=mime or None, created_at=now,
        expires_at=None, storage=storage, path=stored_path,
    )
    db.add(blob)
    db.commit()
    add_owner(db, sha256, pubkey)      # first owner of these bytes
    _meta_put(_meta_from_row(blob))
    return _descriptor_fields(blob)


async def read_blob(db: Session, blob: BlossomBlob):
    """Return (async-byte-iterator, mime, size) for a stored blob, or None if the bytes
    are gone. Serves from the in-RAM cache when possible; otherwise reads from the storage
    server (proxy) or disk (local) — small blobs are buffered into the cache, large ones
    stream straight through (never cached, to bound RAM)."""
    mime = blob.mime or "application/octet-stream"

    cached = _cache_get(blob.sha256)
    if cached is not None:
        return _aiter_bytes(cached), mime, len(cached)   # hot path: no _cfg, no DB, no network

    cfg = _cfg(db)
    budget = cfg["cache_mb"] * 1024 * 1024
    cacheable = 0 < blob.size <= _CACHE_ITEM_CAP and blob.size <= budget

    if blob.storage == "proxy":
        storage_url = cfg["storage_url"]
        if not storage_url:
            return None
        from urllib.parse import quote
        url = (f"{storage_url.rstrip('/')}/api/storage/view-file"
               f"?username={_PROXY_USER}&file_path={quote(blob.path)}&download=1")

        if cacheable:
            resp = await _client().get(url, headers=_proxy_headers())
            if resp.status_code != 200:
                return None
            data = resp.content
            _cache_put(blob.sha256, data, budget)
            return _aiter_bytes(data), mime, len(data)

        # Open + check the upstream status BEFORE returning, so gone/errored bytes yield None (→ clean
        # 404 + self-heal) instead of a StreamingResponse 200 whose promised Content-Length never
        # arrives (client hang) — the same fix as read_range (see its note).
        c = _client()
        try:
            resp = await c.send(c.build_request("GET", url, headers=_proxy_headers()), stream=True)
        except Exception:
            return None
        if resp.status_code != 200:
            await resp.aclose()
            return None

        async def _proxy_stream():
            try:
                async for chunk in resp.aiter_bytes():
                    yield chunk
            finally:
                await resp.aclose()

        return _proxy_stream(), mime, blob.size

    # local
    if not os.path.isfile(blob.path):
        return None

    if cacheable:
        data = await asyncio.to_thread(lambda: open(blob.path, "rb").read())
        _cache_put(blob.sha256, data, budget)
        return _aiter_bytes(data), mime, len(data)

    async def _file_stream():
        def _open():
            return open(blob.path, "rb")
        f = await asyncio.to_thread(_open)
        try:
            while True:
                chunk = await asyncio.to_thread(f.read, 262144)
                if not chunk:
                    break
                yield chunk
        finally:
            f.close()

    return _file_stream(), mime, blob.size


async def read_full(db: Session, blob: BlossomBlob) -> bytes | None:
    """Return the blob's full bytes (cache-aware). Used for HTTP Range responses — browsers need
    range support to play many MP4s (moov atom at the end) and to seek."""
    cached = _cache_get(blob.sha256)
    if cached is not None:
        return cached
    cfg = _cfg(db)
    if blob.storage == "proxy":
        if not cfg["storage_url"]:
            return None
        from urllib.parse import quote
        url = (f"{cfg['storage_url'].rstrip('/')}/api/storage/view-file"
               f"?username={_PROXY_USER}&file_path={quote(blob.path)}&download=1")
        r = await _client().get(url, headers=_proxy_headers())
        if r.status_code != 200:
            return None
        data = r.content
    else:
        if not os.path.isfile(blob.path):
            return None
        data = await asyncio.to_thread(lambda: open(blob.path, "rb").read())
    _cache_put(blob.sha256, data, cfg["cache_mb"] * 1024 * 1024)
    return data


async def read_range(db: Session, blob: BlossomBlob, start: int, end: int):
    """Async-iterate ONLY bytes [start, end] (inclusive) of a blob, without buffering the whole thing.
    Serves from the RAM cache (slice) when hot; otherwise forwards the Range to the storage proxy (nas
    returns 206 via FileResponse) or seeks the local file. This is what makes video seeking O(range)
    instead of re-downloading the full blob from nas on every seek. Returns an async iterator, or None
    if the bytes are gone. No caching of partial reads (a full GET seeds the cache)."""
    cached = _cache_get(blob.sha256)
    if cached is not None:
        return _aiter_bytes(cached[start:end + 1])

    n = end - start + 1
    cfg = _cfg(db)
    if blob.storage == "proxy":
        if not cfg["storage_url"]:
            return None
        from urllib.parse import quote
        url = (f"{cfg['storage_url'].rstrip('/')}/api/storage/view-file"
               f"?username={_PROXY_USER}&file_path={quote(blob.path)}&download=1")
        headers = {**_proxy_headers(), "Range": f"bytes={start}-{end}"}
        # Open the stream and check the upstream status HERE, before returning an iterator — so a
        # missing/errored blob on nas (404/5xx/connect error) yields None → the router sends a clean
        # 404, instead of a committed 206 whose promised Content-Length never arrives (client hang).
        c = _client()
        try:
            resp = await c.send(c.build_request("GET", url, headers=headers), stream=True)
        except Exception:
            return None
        if resp.status_code not in (200, 206):
            await resp.aclose()
            return None
        partial = resp.status_code == 206

        async def _proxy_range():
            try:
                if partial:
                    async for chunk in resp.aiter_bytes():
                        yield chunk
                else:
                    # Defensive: nas returned 200 (FileResponse normally honours Range → 206). Slice the
                    # stream so we still return only [start, end], never buffering the whole blob to RAM.
                    skip, remaining = start, n
                    async for chunk in resp.aiter_bytes():
                        if remaining <= 0:
                            break
                        if skip:
                            if len(chunk) <= skip:
                                skip -= len(chunk)
                                continue
                            chunk = chunk[skip:]
                            skip = 0
                        chunk = chunk[:remaining]
                        remaining -= len(chunk)
                        yield chunk
            finally:
                await resp.aclose()

        return _proxy_range()

    # local — seek to start, read exactly n bytes (never loads the whole file)
    if not os.path.isfile(blob.path):
        return None

    async def _local_range():
        def _open():
            f = open(blob.path, "rb")
            f.seek(start)
            return f
        f = await asyncio.to_thread(_open)
        remaining = n
        try:
            while remaining > 0:
                chunk = await asyncio.to_thread(f.read, min(262144, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
                yield chunk
        finally:
            f.close()

    return _local_range()


async def delete_blob_bytes(db: Session, blob: BlossomBlob, fresh_client: bool = False) -> None:
    """Best-effort removal of the underlying bytes (the row is deleted by the caller).

    `fresh_client=True` MUST be passed when this runs off the app's main event loop (the cleanup
    sweep thread drives it via asyncio.run on its own loop): the shared `_client()` is bound to the
    main loop, so reusing it from another loop raises deep in httpx and the proxy DELETE silently
    fails (caught below) — deleting the row but ORPHANING the bytes on the storage node. A private
    client created inside the current loop (mirrors _mirror_blob) makes the proxy delete actually
    happen. Short-lived: the delete is a single request that releases immediately."""
    cfg = _cfg(db)
    _cache_drop(blob.sha256)
    # Evict the metadata cache here so EVERY delete path invalidates it (router /delete, the cleanup
    # sweep, artifact_store, admin purge, bot-delete purge all route through here). This is pre-commit,
    # so a GET racing the delete could re-query the still-visible row and re-cache a stale entry; that
    # narrow window is closed by the post-commit drop_meta in the interactive /delete route and by the
    # revalidate_meta self-heal on the read path (which only evicts once the row is truly gone).
    _meta_drop(blob.sha256)
    try:
        if blob.storage == "proxy" and cfg["storage_url"]:
            from urllib.parse import quote
            url = (f"{cfg['storage_url'].rstrip('/')}/api/storage/delete-file"
                   f"?username={_PROXY_USER}&file_path={quote(blob.path)}")
            timeout = httpx.Timeout(30.0, connect=10.0)
            if fresh_client:
                async with httpx.AsyncClient(timeout=timeout) as client:
                    await client.delete(url, headers=_proxy_headers())
            else:
                await _client().delete(url, headers=_proxy_headers(), timeout=timeout)
        elif blob.storage == "local":
            await asyncio.to_thread(lambda: os.path.isfile(blob.path) and os.remove(blob.path))
    except Exception as e:
        logger.warning("[blossom] failed to delete bytes for %s: %s", blob.sha256, e)


# --- descriptors / queries --------------------------------------------------

# Extensions we pin ourselves; everything else falls through to `mimetypes`. Only the cases where
# the stdlib is absent or picks a suffix nothing else uses (audio/ogg → .oga) need to be here.
_EXT_BY_MIME = {
    "image/jpeg": "jpg", "image/jpg": "jpg", "image/svg+xml": "svg",
    "audio/ogg": "ogg", "audio/opus": "opus", "audio/x-m4a": "m4a", "audio/mp4": "m4a",
    "video/quicktime": "mov", "video/x-matroska": "mkv",
    "text/markdown": "md", "application/x-tar": "tar", "application/x-7z-compressed": "7z",
}


def ext_for_mime(mime: str) -> str:
    """File extension (no dot) for a MIME type, or '' when it isn't known.

    Blossom blobs are named by their sha256, so this suffix is the ONLY thing that tells a browser
    (or another Nostr client) what a blob is — BUD-02 says the descriptor `url` must carry it when
    known. Without it every download landed as an extensionless `a1b2c3…` file.
    """
    m = (mime or "").split(";")[0].strip().lower()
    if not m or m == "application/octet-stream":
        return ""
    if m in _EXT_BY_MIME:
        return _EXT_BY_MIME[m]
    try:
        import mimetypes
        guessed = mimetypes.guess_extension(m) or ""
    except Exception:
        guessed = ""
    ext = guessed.lstrip(".").lower()
    return ext if ext.isalnum() and len(ext) <= 8 else ""


# Magic numbers → extension, for blobs whose stored MIME says nothing (application/octet-stream is
# what a client that didn't set Content-Type uploads, and what every artifact stored through
# upload_store/artifact_store carries). Without this such a blob has no type, no extension and no
# name anywhere, and downloads as a file the OS refuses to open — the "video with no extension" case.
_MAGIC = (
    (b"\x89PNG\r\n\x1a\n", 0, "png"), (b"\xff\xd8\xff", 0, "jpg"), (b"GIF8", 0, "gif"),
    (b"ftyp", 4, "mp4"),                       # ISO-BMFF: mp4 / m4v / mov
    (b"\x1a\x45\xdf\xa3", 0, "webm"),          # matroska
    (b"OggS", 0, "ogg"), (b"fLaC", 0, "flac"), (b"ID3", 0, "mp3"),
    (b"%PDF", 0, "pdf"), (b"PK\x03\x04", 0, "zip"),
    (b"\x1f\x8b", 0, "gz"), (b"7z\xbc\xaf", 0, "7z"), (b"Rar!", 0, "rar"),
)


def sniff_ext(head: bytes) -> str:
    """Extension implied by a blob's first bytes, or '' — needs only the first 16."""
    if not head:
        return ""
    if len(head) >= 12 and head[:4] == b"RIFF":
        tag = head[8:12]
        return {b"WEBP": "webp", b"WAVE": "wav", b"AVI ": "avi"}.get(tag, "")
    for sig, off, ext in _MAGIC:
        if head[off:off + len(sig)] == sig:
            return ext
    if len(head) >= 2 and head[0] == 0xFF and (head[1] & 0xE0) == 0xE0:
        return "mp3"                            # MPEG audio frame sync
    return ""


def safe_filename(name: str) -> str:
    """A user-supplied filename reduced to a harmless BASENAME.

    This string ends up in a `Content-Disposition` header and in the browser's save dialog, so it
    must not carry a path (`../`), a quote or a newline (header injection) — and must stay short."""
    n = (name or "").replace("\\", "/").split("/")[-1].strip()
    n = "".join(c for c in n if c.isprintable() and c not in '"\\;')
    n = n.lstrip(".").strip()          # no dotfiles / no "..", and no leading-dot weirdness
    return n[:120]


def _descriptor_fields(blob: BlossomBlob) -> dict:
    return {
        "sha256": blob.sha256,
        "size": blob.size,
        "type": blob.mime or "application/octet-stream",
        "uploaded": blob.created_at,
        "_path": blob.path,  # internal; router strips before responding
    }


def descriptor(blob: BlossomBlob, base_url: str, name: str = "") -> dict:
    # `url` carries the extension (BUD-02) — the server ignores the suffix when serving
    # (`_strip_ext`), but it's what makes a download save as `.pdf`/`.png` instead of a bare hash,
    # and what lets other clients recognise the media type of a link we hand them. The uploader's
    # own extension wins when we kept their filename, so `.jpeg` doesn't come back as `.jpg`.
    name = safe_filename(name)
    ext = ""
    if "." in name:
        cand = name.rsplit(".", 1)[1].lower()
        if cand.isalnum() and len(cand) <= 8:
            ext = cand
    ext = ext or ext_for_mime(blob.mime or "")
    d = {
        "url": f"{base_url.rstrip('/')}/{blob.sha256}" + (f".{ext}" if ext else ""),
        "sha256": blob.sha256,
        "size": blob.size,
        "type": blob.mime or "application/octet-stream",
        "uploaded": blob.created_at,
    }
    if name:
        d["name"] = name          # non-standard but widely used; lets any client show a real filename
    return d


def names_for_pubkey(db: Session, pubkey_hex: str) -> dict:
    """sha256 → the uploader's original filename, for the blobs this pubkey owns ('' when unknown)."""
    from app.models import BlossomBlobOwner
    try:
        rows = (db.query(BlossomBlobOwner.sha256, BlossomBlobOwner.name)
                  .filter(BlossomBlobOwner.pubkey == pubkey_hex,
                          BlossomBlobOwner.name.isnot(None)).all())
        return {sha: nm for sha, nm in rows if nm}
    except Exception as e:
        logger.warning("[blossom] could not read blob names: %s", e)
        return {}


def name_for(db: Session, sha256: str, pubkey_hex: str = "") -> str:
    """The stored filename for a blob. Prefers `pubkey_hex`'s own name, else any owner's."""
    from app.models import BlossomBlobOwner
    try:
        q = db.query(BlossomBlobOwner.name).filter(BlossomBlobOwner.sha256 == sha256,
                                                   BlossomBlobOwner.name.isnot(None))
        if pubkey_hex:
            row = q.filter(BlossomBlobOwner.pubkey == pubkey_hex).first()
            if row and row[0]:
                return row[0]
        row = q.first()
        return (row[0] if row else "") or ""
    except Exception:
        return ""


def list_for_pubkey(db: Session, pubkey_hex: str, include_private: bool = False) -> list[BlossomBlob]:
    """BUD-02 listing. Private (AI-chat) blobs are EXCLUDED unless the caller proved ownership.

    This listing is unauthenticated by design (BUD-02 makes auth optional) and that was fine while
    every blob was public media. It stopped being fine once AI-chat artifacts were stored here: the
    listing published their sha256, and /client/file hands back the DECRYPTED bytes to anyone holding
    that sha256 — so `GET /blossom/list/<storage-pubkey>` was an unauthenticated dump of every user's
    private chat files. The sha256 is the capability, so it must not be listed to strangers."""
    from app.models import BlossomBlobOwner
    # Join the OWNERS table, not blossom_blobs.pubkey. Dedup means the blob row is owned by whoever
    # uploaded these bytes first, so listing by it hid the file from everyone who uploaded it after —
    # their upload succeeded and then simply wasn't in their drive.
    q = (db.query(BlossomBlob)
           .join(BlossomBlobOwner, BlossomBlobOwner.sha256 == BlossomBlob.sha256)
           .filter(BlossomBlobOwner.pubkey == pubkey_hex))
    if not include_private:
        q = q.filter(BlossomBlob.private.is_(False))
    return q.order_by(BlossomBlob.created_at.desc()).all()


def add_owner(db: Session, sha256: str, pubkey_hex: str, name: str = "") -> None:
    """Record `pubkey_hex` as referencing `sha256`. Idempotent; never raises into the upload path.

    `name` is the uploader's original filename (optional). A re-upload only ever FILLS IN a missing
    name — it never renames a file the user already has."""
    from app.models import BlossomBlobOwner
    if not sha256 or not pubkey_hex:
        return
    name = safe_filename(name)
    try:
        exists = db.query(BlossomBlobOwner).filter(
            BlossomBlobOwner.sha256 == sha256, BlossomBlobOwner.pubkey == pubkey_hex).first()
        if exists:
            if name and not (exists.name or ""):
                exists.name = name
                db.commit()
            return
        db.add(BlossomBlobOwner(sha256=sha256, pubkey=pubkey_hex, created_at=int(time.time()),
                                name=name or None))
        db.commit()
    except Exception as e:
        db.rollback()
        logger.warning("[blossom] could not record owner %s/%s: %s", sha256[:12], pubkey_hex[:12], e)


def expire_blob_in(db: Session, sha256: str, days: int) -> bool:
    """Give `sha256` a TTL of `days` from now, so the existing cleanup sweep reclaims it later.

    Used for SUPERSEDED Files-index blobs. Deleting them immediately is what left a wiped index with
    nothing to restore from, and never deleting them leaks ~133 KB per save forever — a TTL is the
    honest middle: the old index stays recoverable for a month, then goes. Only ever moves the expiry
    LATER (never sooner) and never touches a blob that has none, so it cannot shorten the life of
    something else that happens to share these bytes."""
    if not sha256 or days <= 0:
        return False
    try:
        blob = db.query(BlossomBlob).filter(BlossomBlob.sha256 == sha256).first()
        if blob is None:
            return False
        want = int(time.time()) + days * 86400
        if blob.expires_at and blob.expires_at >= want:
            return False                     # already expiring at or after that — leave it alone
        blob.expires_at = want
        db.commit()
        _meta_drop(sha256)
        return True
    except Exception as e:
        db.rollback()
        logger.warning("[blossom] could not stamp TTL on %s: %s", sha256[:12], e)
        return False


def clear_blob_expiry(db: Session, sha256: str) -> bool:
    """Remove a blob's TTL — it has become permanently referenced (e.g. a refused index save that was
    later accepted). The inverse of expire_blob_in; both are needed or a retry inherits the expiry."""
    if not sha256:
        return False
    try:
        blob = db.query(BlossomBlob).filter(BlossomBlob.sha256 == sha256).first()
        if blob is None or blob.expires_at is None:
            return False
        blob.expires_at = None
        db.commit()
        _meta_drop(sha256)
        return True
    except Exception as e:
        db.rollback()
        logger.warning("[blossom] could not clear TTL on %s: %s", sha256[:12], e)
        return False


def usage_for_pubkey(db: Session, pubkey_hex: str) -> int:
    """Total bytes this pubkey references, counted through the owners table so a shared blob is
    charged to everyone holding it (dedup saves the DISK, it shouldn't hand out free quota)."""
    from app.models import BlossomBlobOwner
    from sqlalchemy import func
    v = (db.query(func.coalesce(func.sum(BlossomBlob.size), 0))
           .join(BlossomBlobOwner, BlossomBlobOwner.sha256 == BlossomBlob.sha256)
           .filter(BlossomBlobOwner.pubkey == pubkey_hex).scalar())
    return int(v or 0)


def quota_exceeded(db: Session, pubkey_hex: str, incoming: int) -> tuple[bool, int, int]:
    """(over?, used, limit_bytes) for `pubkey_hex` taking `incoming` more bytes.

    Skipped entirely when no quota is configured — the default — so the aggregate never runs on the
    upload hot path unless an admin has opted in. Without SOME cap nothing bounds growth at all now
    that blobs are kept forever, and one uploader can fill the disk for everybody."""
    limit_gb = _cfg(db)["user_quota_gb"]
    if not limit_gb or limit_gb <= 0:
        return False, 0, 0
    limit = int(limit_gb) * 1024 ** 3
    used = usage_for_pubkey(db, pubkey_hex)
    return (used + max(0, incoming)) > limit, used, limit


def is_owner(db: Session, sha256: str, pubkey_hex: str) -> bool:
    """Does `pubkey_hex` hold a reference to `sha256`?"""
    from app.models import BlossomBlobOwner
    return db.query(BlossomBlobOwner).filter(BlossomBlobOwner.sha256 == sha256,
                                             BlossomBlobOwner.pubkey == pubkey_hex).first() is not None


def release_owner(db: Session, sha256: str, pubkey_hex: str) -> int:
    """Drop one owner's reference. Returns how many owners REMAIN — the caller deletes the bytes only
    at zero, so one user removing a shared file can no longer delete it out from under the others."""
    from app.models import BlossomBlobOwner
    db.query(BlossomBlobOwner).filter(BlossomBlobOwner.sha256 == sha256,
                                      BlossomBlobOwner.pubkey == pubkey_hex).delete()
    db.flush()
    return db.query(BlossomBlobOwner).filter(BlossomBlobOwner.sha256 == sha256).count()


# --- expiry cleanup (daemon thread) -----------------------------------------

_cleanup_stop = threading.Event()
_cleanup_thread: threading.Thread | None = None


def _cleanup_once() -> int:
    """Delete expired blobs (bytes + row). Returns count removed.

    Expiry is governed LIVE by the admin `blossom_blob_ttl_days` setting: when > 0, any blob
    whose `created_at` is older than that many days is swept — so lowering/raising the setting
    in the admin UI takes effect on the next sweep for ALL blobs (including migrated ones), not
    just future uploads. An explicit per-blob `expires_at` (if ever set) is also honoured.

    EXCEPT `keep` blobs, which THE AGE RULE may never delete. They are the client-side encrypted
    drive — Notes attachments, music tracks, the files index — ciphertext this node holds the only
    copy of, and which no user could tell had been deleted until they opened the note. Everything
    else swept here is recoverable or visibly broken; this isn't. That makes the setting a one-way
    promise: turning the TTL on later must not retroactively eat a drive that was uploaded while it
    was off, which is exactly the shape of the accident this guards.

    An EXPLICIT `expires_at` is a different thing and is honoured on every blob. It is never a
    blanket policy — it is stamped one blob at a time by code that has proven those exact bytes are
    referenced by nothing (a files-index blob that fell out of backup retention, a folder-sync
    manifest two generations stale). Sweeping `keep` out of that path did not protect anything; it
    only meant those callers reclaimed NOTHING, for ever, while looking like they did.
    """
    from sqlalchemy import or_, and_
    db = SessionLocal()
    removed = 0
    try:
        cfg = _cfg(db)
        now = int(time.time())
        # An EXPLICIT per-blob expiry applies to every blob, `keep` included. The exemption below is
        # about the ADMIN'S AGE SETTING, which is a blanket rule nobody set per blob — turning it on
        # must not retroactively eat an encrypted drive. An `expires_at` is the opposite: it is only
        # ever stamped by code that has PROVEN these particular bytes are referenced by nothing (a
        # files-index blob that fell out of backup retention, a superseded folder-sync manifest), and
        # while `keep` also swallowed those, that code was reclaiming nothing at all — a TTL that
        # could never fire, quietly leaking every superseded index and manifest for ever.
        explicit = and_(BlossomBlob.expires_at.isnot(None),
                        BlossomBlob.expires_at > 0,
                        BlossomBlob.expires_at <= now)
        conds = [explicit]
        if cfg["ttl_days"] > 0:
            conds.append(and_(BlossomBlob.keep.is_(False),
                              BlossomBlob.created_at <= now - cfg["ttl_days"] * 86400))
        expired = db.query(BlossomBlob).filter(or_(*conds)).limit(500).all()
        gone = []
        for blob in expired:
            try:
                # This runs in the cleanup daemon thread on a throwaway loop (asyncio.run), so the
                # delete MUST use a fresh httpx client — the shared _client() belongs to the main
                # loop and cross-loop reuse fails silently, orphaning the bytes on the storage node.
                asyncio.run(delete_blob_bytes(db, blob, fresh_client=True))
            except Exception:
                pass
            gone.append(blob.sha256)
            db.delete(blob)
            removed += 1
        if removed:
            db.commit()
            for sha in gone:
                _meta_drop(sha)   # evict metadata AFTER the commit (see drop_meta)
            logger.info("[blossom] cleanup removed %d expired blob(s)", removed)
    except Exception as e:
        logger.warning("[blossom] cleanup error: %s", e)
    finally:
        db.close()
    return removed


def _cleanup_loop() -> None:
    # Wait before the first sweep so startup isn't contended; then sweep on interval.
    while not _cleanup_stop.wait(_CLEANUP_INTERVAL_SEC):
        try:
            db = SessionLocal()
            try:
                enabled = is_enabled(db)
            finally:
                db.close()
            if enabled:
                _cleanup_once()
        except Exception as e:
            logger.warning("[blossom] cleanup loop error: %s", e)


def start_blossom_cleanup() -> None:
    """Start the expiry sweep (idempotent; no-op if already running)."""
    global _cleanup_thread
    if _cleanup_thread and _cleanup_thread.is_alive():
        return
    _cleanup_stop.clear()
    _cleanup_thread = threading.Thread(target=_cleanup_loop, name="blossom-cleanup", daemon=True)
    _cleanup_thread.start()
    logger.info("[blossom] expiry cleanup thread started (every %ds)", _CLEANUP_INTERVAL_SEC)


def stop_blossom_cleanup() -> None:
    _cleanup_stop.set()
    _mirror_stop.set()   # also halt the DR mirror worker (daemon thread; exits within ~1s)
