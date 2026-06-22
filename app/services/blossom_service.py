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
import threading
import time
import base64

import httpx
from sqlalchemy.orm import Session

from app.database import SessionLocal
from app.models import BlossomBlob, User
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

    return {
        "enabled": g("blossom_enabled", "false").lower() == "true",
        "public_url": g("blossom_public_url", "").rstrip("/"),
        "ttl_days": gi("blossom_blob_ttl_days", 0),
        "max_upload_mb": gi("blossom_max_upload_mb", 100),
        "backend": backend,
        "blob_dir": g("blossom_storage_path", "") or _DEFAULT_BLOB_DIR,
        "storage_url": storage_url,
        "cache_mb": gi("blossom_cache_mb", 512),
        # DR: external Blossom servers to mirror each uploaded blob to (space/newline-separated).
        "mirror_servers": [s for s in (g("blossom_mirror_servers", "")).split()
                           if s.startswith(("http://", "https://"))],
    }


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


async def _aiter_bytes(data: bytes):
    yield data


# --- authorization ----------------------------------------------------------

_operator_cache = {"ts": 0.0, "set": frozenset()}
_OPERATOR_TTL = 60.0


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


# --- storage backends -------------------------------------------------------

def _local_path(blob_dir: str, sha256: str) -> str:
    return os.path.join(blob_dir, sha256[:2], sha256)


def _proxy_headers() -> dict:
    return {"X-Posterchanai-Load-Balanced": "true"}


async def _proxy_put(storage_url: str, sha256: str, data: bytes, mime: str) -> str:
    """Upload bytes to the storage server under _blossom/blossom/<ab>/<sha>. Returns rel-path."""
    subdir = f"blossom/{sha256[:2]}"
    url = f"{storage_url.rstrip('/')}/api/storage/upload-file"
    files = {"file": (sha256, data, mime or "application/octet-stream")}
    form = {"username": _PROXY_USER, "path": subdir}
    async with httpx.AsyncClient(timeout=httpx.Timeout(120.0, connect=10.0)) as client:
        r = await client.post(url, headers=_proxy_headers(), files=files, data=form)
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


async def save_blob(db: Session, pubkey: str, data: bytes, mime: str) -> dict:
    """Persist a blob (dedup by sha256) and record its row. Returns a descriptor dict
    (without `url`, which the router fills from the request base)."""
    cfg = _cfg(db)
    sha256 = await asyncio.to_thread(compute_sha256, data)
    size = len(data)

    existing = db.query(BlossomBlob).filter(BlossomBlob.sha256 == sha256).first()
    if existing:
        # Already stored (possibly by another user) — content-addressed, so nothing to write.
        # Retention is governed live by the admin TTL setting (see _cleanup_once), keyed off
        # created_at, so re-uploads don't need to re-stamp anything.
        return _descriptor_fields(existing)

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
    # expires_at is reserved for an explicit per-blob TTL (left NULL here); ordinary retention
    # is driven live by the admin `blossom_blob_ttl_days` setting against created_at.
    blob = BlossomBlob(
        sha256=sha256, pubkey=pubkey, size=size, mime=mime or None, created_at=now,
        expires_at=None, storage=storage, path=path,
    )
    db.add(blob)
    db.commit()
    # Seed the read cache — the bytes are already in RAM, so a fetch right after upload
    # (the common case) won't touch disk or the storage proxy.
    _cache_put(sha256, data, cfg["cache_mb"] * 1024 * 1024)
    # DR: hand the new blob to the background mirror worker (own thread + queue) so mirroring never
    # touches the request's event loop and is paced/serialised (saves bandwidth, polite to mirrors).
    # Only newly-stored blobs are queued — a re-upload of the same hash is already mirrored.
    if cfg["mirror_servers"]:
        _enqueue_mirror(sha256, data, mime, cfg["mirror_servers"])
    return _descriptor_fields(blob)


async def read_blob(db: Session, blob: BlossomBlob):
    """Return (async-byte-iterator, mime, size) for a stored blob, or None if the bytes
    are gone. Serves from the in-RAM cache when possible; otherwise reads from the storage
    server (proxy) or disk (local) — small blobs are buffered into the cache, large ones
    stream straight through (never cached, to bound RAM)."""
    cfg = _cfg(db)
    mime = blob.mime or "application/octet-stream"

    cached = _cache_get(blob.sha256)
    if cached is not None:
        return _aiter_bytes(cached), mime, len(cached)

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
            async with httpx.AsyncClient(timeout=httpx.Timeout(120.0, connect=10.0)) as client:
                resp = await client.get(url, headers=_proxy_headers())
            if resp.status_code != 200:
                return None
            data = resp.content
            _cache_put(blob.sha256, data, budget)
            return _aiter_bytes(data), mime, len(data)

        async def _proxy_stream():
            async with httpx.AsyncClient(timeout=httpx.Timeout(120.0, connect=10.0)) as client:
                async with client.stream("GET", url, headers=_proxy_headers()) as resp:
                    if resp.status_code != 200:
                        return
                    async for chunk in resp.aiter_bytes():
                        yield chunk

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
        async with httpx.AsyncClient(timeout=httpx.Timeout(120.0, connect=10.0)) as client:
            r = await client.get(url, headers=_proxy_headers())
        if r.status_code != 200:
            return None
        data = r.content
    else:
        if not os.path.isfile(blob.path):
            return None
        data = await asyncio.to_thread(lambda: open(blob.path, "rb").read())
    _cache_put(blob.sha256, data, cfg["cache_mb"] * 1024 * 1024)
    return data


async def delete_blob_bytes(db: Session, blob: BlossomBlob) -> None:
    """Best-effort removal of the underlying bytes (the row is deleted by the caller)."""
    cfg = _cfg(db)
    _cache_drop(blob.sha256)
    try:
        if blob.storage == "proxy" and cfg["storage_url"]:
            from urllib.parse import quote
            url = (f"{cfg['storage_url'].rstrip('/')}/api/storage/delete-file"
                   f"?username={_PROXY_USER}&file_path={quote(blob.path)}")
            async with httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=10.0)) as client:
                await client.delete(url, headers=_proxy_headers())
        elif blob.storage == "local":
            await asyncio.to_thread(lambda: os.path.isfile(blob.path) and os.remove(blob.path))
    except Exception as e:
        logger.warning("[blossom] failed to delete bytes for %s: %s", blob.sha256, e)


# --- descriptors / queries --------------------------------------------------

def _descriptor_fields(blob: BlossomBlob) -> dict:
    return {
        "sha256": blob.sha256,
        "size": blob.size,
        "type": blob.mime or "application/octet-stream",
        "uploaded": blob.created_at,
        "_path": blob.path,  # internal; router strips before responding
    }


def descriptor(blob: BlossomBlob, base_url: str) -> dict:
    d = {
        "url": f"{base_url.rstrip('/')}/{blob.sha256}",
        "sha256": blob.sha256,
        "size": blob.size,
        "type": blob.mime or "application/octet-stream",
        "uploaded": blob.created_at,
    }
    return d


def list_for_pubkey(db: Session, pubkey_hex: str) -> list[BlossomBlob]:
    return (db.query(BlossomBlob)
            .filter(BlossomBlob.pubkey == pubkey_hex)
            .order_by(BlossomBlob.created_at.desc())
            .all())


# --- expiry cleanup (daemon thread) -----------------------------------------

_cleanup_stop = threading.Event()
_cleanup_thread: threading.Thread | None = None


def _cleanup_once() -> int:
    """Delete expired blobs (bytes + row). Returns count removed.

    Expiry is governed LIVE by the admin `blossom_blob_ttl_days` setting: when > 0, any blob
    whose `created_at` is older than that many days is swept — so lowering/raising the setting
    in the admin UI takes effect on the next sweep for ALL blobs (including migrated ones), not
    just future uploads. An explicit per-blob `expires_at` (if ever set) is also honoured.
    """
    from sqlalchemy import or_, and_
    db = SessionLocal()
    removed = 0
    try:
        cfg = _cfg(db)
        now = int(time.time())
        conds = [and_(BlossomBlob.expires_at.isnot(None),
                      BlossomBlob.expires_at > 0,
                      BlossomBlob.expires_at <= now)]
        if cfg["ttl_days"] > 0:
            conds.append(BlossomBlob.created_at <= now - cfg["ttl_days"] * 86400)
        expired = db.query(BlossomBlob).filter(or_(*conds)).limit(500).all()
        for blob in expired:
            try:
                asyncio.run(delete_blob_bytes(db, blob))
            except Exception:
                pass
            db.delete(blob)
            removed += 1
        if removed:
            db.commit()
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
