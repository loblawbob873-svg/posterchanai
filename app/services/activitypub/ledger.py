"""Keep the delivered-notes ledger (the `fedi_bridge_delivered` DocTable) as bounded as the events it describes.

Every note that arrives over ActivityPub, and every DM that crosses, leaves a row: the dedup key
that stops a note being stored twice. The rows are bookkeeping, not content, so they follow the
relay's ONE retention window (Admin → Relay → Auto-clean, `nostr_relay_retention_days`): a row may
not outlive its event, nor be reaped before it. 0 (Auto-clean off) keeps them, matching "nothing is
auto-deleted". This job used to live in the Pleroma bridge; without it, nothing pruned the table.
"""
from __future__ import annotations

import logging
import time

from app.services import settings_store

logger = logging.getLogger(__name__)

_DAY = 86400
_state = {"ok_at": 0.0}


def prune() -> int:
    """Delete ledger rows past retention (sync: the scheduler runs it on a worker thread). A ledger that
    cannot be read prunes nothing this time -- "could not ask" is never "nothing old"."""
    import asyncio
    from app.services import fedi_tables
    try:
        keep_days = int(settings_store.get("nostr_relay_retention_days", "30") or "30")
    except ValueError:
        keep_days = 30
    if keep_days <= 0:
        return 0
    try:
        n = asyncio.run(fedi_tables.aprune(keep_days))
    except Exception as e:      # noqa: BLE001
        logger.warning("[activitypub] ledger prune not run (retried within the hour): %s: %s", type(e).__name__, e)
        return 0
    _state["ok_at"] = time.time()
    return n


def prune_if_due() -> int:
    """`prune` once a day -- asked HOURLY, so a prune that could not run (the ledger still loading after a
    restart, a busy relay) is retried within the hour instead of waiting a whole day."""
    if time.time() - _state["ok_at"] < _DAY - 1800:
        return 0
    return prune()
