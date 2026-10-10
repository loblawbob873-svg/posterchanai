"""Short ids for image-search thumbnails (the `images` command), so the websocket payload stays small.

IN MEMORY, not a table (#161: `proxy_image_cache` leaves Postgres). It is a pure cache: an id lives five
minutes, is minted by the command that runs in the app process (web UI websocket and Telegram both execute
commands there) and is redeemed by /api/proxy-image on the SAME process -- the app serves on one uvicorn
worker, which is the only reason the table existed ("shared across workers"). Losing it to a restart costs
nothing: the client sends `img_src` beside every `thumb_id` and falls back to it (command_service/search.py),
which is also what an expired id has always meant. Bounded, so nothing can grow it without limit.
"""
import secrets
import threading
import time
from collections import OrderedDict

_TTL_SEC = 300          # 5 minutes
_MAX = 20000            # ~5 results per search; far above any real five-minute burst

_ids: "OrderedDict[str, tuple[str, float]]" = OrderedDict()
_lock = threading.Lock()


def register(url: str, db=None) -> str:
    """Remember `url` under a short id for five minutes. `db` is accepted for the old call signature."""
    raw = (url or "").strip()
    if not raw:
        raise ValueError("url required")
    now = time.time()
    with _lock:
        sid = secrets.token_hex(4)  # 8 chars
        while sid in _ids:
            sid = secrets.token_hex(4)
        _ids[sid] = (raw, now + _TTL_SEC)
        # oldest first: drop what has expired, then anything over the cap
        while _ids:
            k, (_u, exp) = next(iter(_ids.items()))
            if exp > now and len(_ids) <= _MAX:
                break
            _ids.pop(k, None)
    return sid


def get(sid: str, db=None) -> str | None:
    """The URL for `sid`, or None if unknown or expired."""
    if not sid:
        return None
    with _lock:
        hit = _ids.get(sid)
        if hit is None:
            return None
        if time.time() > hit[1]:
            _ids.pop(sid, None)
            return None
        return hit[0]
