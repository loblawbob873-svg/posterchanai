"""Per-user misc-record read-path → Nostr relay (reminders, saved searches).

Same cache-hydrate pattern as the other *_store modules, for the small user-OWNED tables that aren't
chats: each row becomes an encrypted doc signed with the OWNER's storage key (private data, like
chats), keyed by `<ns><row_id>`. The SQLite tables stay the fast cache (the reminder scheduler keeps
its frequent `due_at <=` query local); a fresh node reconstructs the rows from the relay.

The relay is the only datastore (always on). Sync callers use the `*_blocking` wrappers.

REMINDERS AND SAVED SEARCHES LEFT THIS MODULE (#161): their store of record is now a DocTable (operator
documents; reminder_service / saved_search_service), and nothing calls the reminder/search mirrors any more.
There is no SQL at all now: `hydrate` is a no-op.
"""

import asyncio
import logging
from datetime import datetime

from app.services import nostr_store as store
from app.services.nostr_store import user_storage_seckey
from app.services import settings_store as _ss

logger = logging.getLogger(__name__)

NS_REMINDER = "pcai:reminder:"
NS_SEARCH = "pcai:search:"
NS_APIKEY = "pcai:apikey:"


def enabled(db) -> bool:
    """The relay is the ONLY datastore — always on (legacy sqlite mode removed)."""
    return True


def _run_blocking(coro):
    """Drive a coroutine to completion from a *_blocking wrapper regardless of context: these are
    called from BOTH plain sync routes (no loop) AND async command handlers (a loop is already
    running, where `asyncio.run` raises RuntimeError). With a running loop, run on a worker thread and
    join — so the caller's `db` session (used inside the coro) is never touched concurrently and stays
    open for the duration; without one, run inline."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    import threading
    box = {}
    def _t():
        try:
            box["v"] = asyncio.run(coro)
        except BaseException as e:   # noqa: BLE001 - surfaced to the caller below
            box["e"] = e
    th = threading.Thread(target=_t, name="record-mirror", daemon=True)
    th.start()
    th.join()
    if "e" in box:
        raise box["e"]
    return box.get("v")


def _iso(dt):
    return dt.isoformat() if dt else None


def _dt(v):
    try:
        return datetime.fromisoformat(v) if v else None
    except (TypeError, ValueError):
        return None


async def _put(db, user, ns, row_id, data, *, force) -> bool:
    if user is None or not getattr(user, "nostr_npub", None) or (not force and not enabled(db)):
        return False
    try:
        sk = user_storage_seckey(db, user)
        return await store.put_doc(_ss._port(), sk, f"{ns}{row_id}", data)
    except Exception as e:
        logger.warning("[record-store] put %s%s failed: %s", ns, row_id, e)
        return False


async def _delete(db, user, ns, row_id, *, force) -> bool:
    if user is None or not getattr(user, "nostr_npub", None) or (not force and not enabled(db)):
        return False
    try:
        sk = user_storage_seckey(db, user)
        return await store.delete_doc(_ss._port(), sk, f"{ns}{row_id}")
    except Exception as e:
        logger.warning("[record-store] delete %s%s failed: %s", ns, row_id, e)
        return False


# ---- reminders ----
def _reminder_rec(r) -> dict:
    return {"text": r.text, "due_at": _iso(r.due_at), "status": r.status,
            "created_at": _iso(r.created_at), "delivered_at": _iso(r.delivered_at)}


async def mirror_reminder(db, user, r, *, force=False) -> bool:
    return await _put(db, user, NS_REMINDER, r.id, _reminder_rec(r), force=force)


def mirror_reminder_blocking(db, user, r) -> None:
    try:
        _run_blocking(mirror_reminder(db, user, r))
    except Exception as e:
        logger.warning("[record-store] mirror_reminder_blocking failed: %s", e)


# ---- saved searches ----
def _search_rec(s) -> dict:
    return {"query": s.query, "created_at": _iso(s.created_at)}


def mirror_search_blocking(db, user, s) -> None:
    try:
        _run_blocking(_put(db, user, NS_SEARCH, s.id, _search_rec(s), force=False))
    except Exception as e:
        logger.warning("[record-store] mirror_search_blocking failed: %s", e)


def delete_search_blocking(db, user, search_id) -> None:
    try:
        _run_blocking(_delete(db, user, NS_SEARCH, search_id, force=False))
    except Exception as e:
        logger.warning("[record-store] delete_search_blocking failed: %s", e)


# ---- API keys: RETIRED mirror. Keys live in the operator's `api_keys` DocTable (api_key_store, #161);
# this only removes a deleted key's old per-user copy so nothing stale is left under the user's key. ----
def delete_apikey_blocking(db, user, key_id) -> None:
    try:
        _run_blocking(_delete(db, user, NS_APIKEY, key_id, force=False))
    except Exception as e:
        logger.warning("[record-store] delete_apikey_blocking failed: %s", e)


# ---- hydrate: RETIRED. It rebuilt SQL Reminder / SavedSearch rows from these per-user documents; there is no SQL
# any more (#161 -- reminders and pins are relay DocTables). Kept as a no-op for callers that still call it. ----
async def hydrate(db) -> int:
    return 0
