"""Keep the delivered-notes ledger (FediBridgeDelivered) as bounded as the events it describes.

Every note that arrives over ActivityPub, and every DM that crosses, leaves a row: the dedup key
that stops a note being stored twice. The rows are bookkeeping, not content, so they follow the
relay's ONE retention window (Admin → Relay → Auto-clean, `nostr_relay_retention_days`): a row may
not outlive its event, nor be reaped before it. 0 (Auto-clean off) keeps them, matching "nothing is
auto-deleted". This job used to live in the Pleroma bridge; without it, nothing pruned the table.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

from app.services import settings_store

logger = logging.getLogger(__name__)


def prune() -> int:
    from app.database import SessionLocal
    from app.models import FediBridgeDelivered
    try:
        keep_days = int(settings_store.get("nostr_relay_retention_days", "30") or "30")
    except ValueError:
        keep_days = 30
    if keep_days <= 0:
        return 0
    db = SessionLocal()
    try:
        n = (db.query(FediBridgeDelivered)
               .filter(FediBridgeDelivered.created_at < datetime.utcnow() - timedelta(days=keep_days))
               .delete(synchronize_session=False))
        db.commit()
        return n or 0
    except Exception as e:
        db.rollback()
        logger.warning("[activitypub] ledger prune failed: %s: %s", type(e).__name__, e)
        return 0
    finally:
        db.close()
