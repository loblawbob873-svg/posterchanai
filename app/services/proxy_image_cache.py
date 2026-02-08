"""Cache for image proxy short IDs. Uses DB so it works across workers (e.g. uvicorn --workers 2)."""
import secrets
import time
import logging
from sqlalchemy.orm import Session

from app.models import ProxyImageCache as ProxyImageCacheModel

logger = logging.getLogger(__name__)
_TTL_SEC = 300  # 5 minutes


def register(url: str, db: Session) -> str:
    """Store URL in DB and return a short id."""
    raw = (url or "").strip()
    if not raw:
        raise ValueError("url required")
    sid = secrets.token_hex(4)  # 8 chars
    expires_at = int(time.time()) + _TTL_SEC
    row = ProxyImageCacheModel(id=sid, url=raw, expires_at=expires_at)
    db.add(row)
    db.commit()
    return sid


def get(sid: str, db: Session) -> str | None:
    """Return stored URL for id, or None if missing/expired."""
    if not sid:
        return None
    row = db.query(ProxyImageCacheModel).filter(ProxyImageCacheModel.id == sid).first()
    if not row:
        return None
    if time.time() > row.expires_at:
        db.delete(row)
        db.commit()
        return None
    return row.url
