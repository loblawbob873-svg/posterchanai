"""In-memory cache for image proxy short IDs. Used to keep WebSocket image-search payload small (thumb_id instead of long URLs)."""
import secrets
import time
import logging

logger = logging.getLogger(__name__)

# id -> (url, expires_at)
_cache: dict[str, tuple[str, float]] = {}
_TTL_SEC = 300  # 5 minutes


def register(url: str) -> str:
    """Store URL and return a short id. Same URL can be registered multiple times (new id each time)."""
    raw = (url or "").strip()
    if not raw:
        raise ValueError("url required")
    sid = secrets.token_hex(4)  # 8 chars
    _cache[sid] = (raw, time.monotonic() + _TTL_SEC)
    return sid


def get(sid: str) -> str | None:
    """Return stored URL for id, or None if missing/expired."""
    if not sid:
        return None
    entry = _cache.get(sid)
    if not entry:
        return None
    url, expires = entry
    if time.monotonic() > expires:
        del _cache[sid]
        return None
    return url
