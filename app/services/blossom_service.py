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
from app.models import Setting, BlossomBlob, User
from app.services.nostr import nostr_service, event as nostr_event

logger = logging.getLogger(__name__)

# Repo root: app/services/blossom_service.py -> three dirs up (flat module, NOT a package).
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_DEFAULT_BLOB_DIR = os.environ.get("POSTERCHANAI_BLOSSOM_PATH") or os.path.join(_REPO_ROOT, "data", "blossom")

# Username the blobs live under on the storage server (proxy backend).
_PROXY_USER = "_blossom"
_AUTH_KIND = 24242
_CLEANUP_INTERVAL_SEC = 600   # sweep expired blobs every 10 min (idle, low CPU)


# --- config -----------------------------------------------------------------

def _cfg(db: Session) -> dict:
    rows = {s.key: s.value for s in db.query(Setting).filter(Setting.key.like("blossom_%")).all()}

    def g(key, default=""):
        v = rows.get(key)
        return v if v not in (None, "") else default

    def gi(key, default):
        try:
            return int(g(key, str(default)))
        except (TypeError, ValueError):
            return default

    storage_url = db.query(Setting).filter(Setting.key == "storage_server_url").first()
    storage_url = (storage_url.value.strip() if storage_url and storage_url.value else "")

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
    }


def is_enabled(db: Session) -> bool:
    row = db.query(Setting).filter(Setting.key == "blossom_enabled").first()
    return bool(row and (row.value or "").lower() == "true")


# --- authorization ----------------------------------------------------------

def is_pubkey_allowed(db: Session, pubkey_hex: str) -> bool:
    """A pubkey may upload/delete iff it's the Nostr key linked by a user who is an admin
    or has the `can_blossom` privilege. Single indexed-ish lookup by npub — no full scan."""
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


async def save_blob(db: Session, pubkey: str, data: bytes, mime: str) -> dict:
    """Persist a blob (dedup by sha256) and record its row. Returns a descriptor dict
    (without `url`, which the router fills from the request base)."""
    cfg = _cfg(db)
    sha256 = await asyncio.to_thread(compute_sha256, data)
    size = len(data)

    existing = db.query(BlossomBlob).filter(BlossomBlob.sha256 == sha256).first()
    if existing:
        # Already stored (possibly by another user). Refresh TTL window on re-upload.
        if cfg["ttl_days"] > 0:
            existing.expires_at = int(time.time()) + cfg["ttl_days"] * 86400
            db.commit()
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
    blob = BlossomBlob(
        sha256=sha256, pubkey=pubkey, size=size, mime=mime or None, created_at=now,
        expires_at=(now + cfg["ttl_days"] * 86400) if cfg["ttl_days"] > 0 else None,
        storage=storage, path=path,
    )
    db.add(blob)
    db.commit()
    return _descriptor_fields(blob)


async def read_blob(db: Session, blob: BlossomBlob):
    """Return (async-byte-iterator, mime, size) for a stored blob, or None if the bytes
    are gone. Streams from the storage server (proxy) or disk (local)."""
    cfg = _cfg(db)
    if blob.storage == "proxy":
        storage_url = cfg["storage_url"]
        if not storage_url:
            return None
        from urllib.parse import quote
        url = (f"{storage_url.rstrip('/')}/api/storage/view-file"
               f"?username={_PROXY_USER}&file_path={quote(blob.path)}&download=1")

        async def _proxy_stream():
            async with httpx.AsyncClient(timeout=httpx.Timeout(120.0, connect=10.0)) as client:
                async with client.stream("GET", url, headers=_proxy_headers()) as resp:
                    if resp.status_code != 200:
                        return
                    async for chunk in resp.aiter_bytes():
                        yield chunk

        return _proxy_stream(), (blob.mime or "application/octet-stream"), blob.size

    # local
    if not os.path.isfile(blob.path):
        return None

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

    return _file_stream(), (blob.mime or "application/octet-stream"), blob.size


async def delete_blob_bytes(db: Session, blob: BlossomBlob) -> None:
    """Best-effort removal of the underlying bytes (the row is deleted by the caller)."""
    cfg = _cfg(db)
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
    """Delete blobs past their expires_at (bytes + row). Returns count removed."""
    db = SessionLocal()
    removed = 0
    try:
        now = int(time.time())
        expired = (db.query(BlossomBlob)
                   .filter(BlossomBlob.expires_at.isnot(None),
                           BlossomBlob.expires_at > 0,
                           BlossomBlob.expires_at <= now)
                   .limit(500).all())
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
