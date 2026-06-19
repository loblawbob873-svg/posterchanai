"""Per-user misc-record read-path → Nostr relay (reminders, saved searches).

Same cache-hydrate pattern as the other *_store modules, for the small user-OWNED tables that aren't
chats: each row becomes an encrypted doc signed with the OWNER's storage key (private data, like
chats), keyed by `<ns><row_id>`. The SQLite tables stay the fast cache (the reminder scheduler keeps
its frequent `due_at <=` query local); a fresh node reconstructs the rows from the relay.

Gated by `records_backend` (`relay` on; default `sqlite`). Sync callers use the `*_blocking` wrappers.
"""

import asyncio
import logging
from datetime import datetime

from app.models import Setting, Reminder, SavedSearch, User
from app.services import nostr_store as store
from app.services.nostr_store import user_storage_seckey
from app.services import settings_store as _ss

logger = logging.getLogger(__name__)

NS_REMINDER = "pcai:reminder:"
NS_SEARCH = "pcai:search:"


def enabled(db) -> bool:
    row = db.query(Setting).filter(Setting.key == "records_backend").first()
    return bool(row and (row.value or "").strip().lower() == "relay")


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
        return await store.put_doc(_ss._port(db), sk, f"{ns}{row_id}", data)
    except Exception as e:
        logger.warning("[record-store] put %s%s failed: %s", ns, row_id, e)
        return False


async def _delete(db, user, ns, row_id, *, force) -> bool:
    if user is None or not getattr(user, "nostr_npub", None) or (not force and not enabled(db)):
        return False
    try:
        sk = user_storage_seckey(db, user)
        return await store.delete_doc(_ss._port(db), sk, f"{ns}{row_id}")
    except Exception as e:
        logger.warning("[record-store] delete %s%s failed: %s", ns, row_id, e)
        return False


# ---- reminders ----
def _reminder_rec(r: Reminder) -> dict:
    return {"text": r.text, "due_at": _iso(r.due_at), "status": r.status,
            "created_at": _iso(r.created_at), "delivered_at": _iso(r.delivered_at)}


async def mirror_reminder(db, user, r: Reminder, *, force=False) -> bool:
    return await _put(db, user, NS_REMINDER, r.id, _reminder_rec(r), force=force)


def mirror_reminder_blocking(db, user, r: Reminder) -> None:
    try:
        if enabled(db):
            asyncio.run(mirror_reminder(db, user, r))
    except Exception as e:
        logger.warning("[record-store] mirror_reminder_blocking failed: %s", e)


# ---- saved searches ----
def _search_rec(s: SavedSearch) -> dict:
    return {"query": s.query, "created_at": _iso(s.created_at)}


def mirror_search_blocking(db, user, s: SavedSearch) -> None:
    try:
        if enabled(db):
            asyncio.run(_put(db, user, NS_SEARCH, s.id, _search_rec(s), force=False))
    except Exception as e:
        logger.warning("[record-store] mirror_search_blocking failed: %s", e)


def delete_search_blocking(db, user, search_id) -> None:
    try:
        if enabled(db):
            asyncio.run(_delete(db, user, NS_SEARCH, search_id, force=False))
    except Exception as e:
        logger.warning("[record-store] delete_search_blocking failed: %s", e)


# ---- hydrate (relay → cache) ----
async def hydrate(db) -> int:
    """Recreate missing Reminder + SavedSearch rows for every user from their relay docs. Additive."""
    if not enabled(db):
        return 0
    made = 0
    for user in db.query(User).filter(User.nostr_npub.isnot(None)).all():
        try:
            sk = user_storage_seckey(db, user)
            rem = await store.list_docs(_ss._port(db), NS_REMINDER, seckey=sk)
            srch = await store.list_docs(_ss._port(db), NS_SEARCH, seckey=sk)
        except Exception:
            continue
        for d_tag, rec in (rem or {}).items():
            if not isinstance(rec, dict):
                continue
            try:
                rid = int(d_tag[len(NS_REMINDER):])
            except (TypeError, ValueError):
                continue
            if db.query(Reminder).filter(Reminder.id == rid).first():
                continue
            db.add(Reminder(id=rid, user_id=user.id, text=rec.get("text") or "",
                            due_at=_dt(rec.get("due_at")) or datetime.utcnow(),
                            status=rec.get("status") or "pending",
                            created_at=_dt(rec.get("created_at")), delivered_at=_dt(rec.get("delivered_at"))))
            made += 1
        for d_tag, rec in (srch or {}).items():
            if not isinstance(rec, dict):
                continue
            try:
                sid = int(d_tag[len(NS_SEARCH):])
            except (TypeError, ValueError):
                continue
            if db.query(SavedSearch).filter(SavedSearch.id == sid).first():
                continue
            db.add(SavedSearch(id=sid, user_id=user.id, query=rec.get("query") or "",
                               created_at=_dt(rec.get("created_at"))))
            made += 1
    if made:
        db.commit()
    logger.info("[record-store] hydrated %d record(s) from relay", made)
    return made


async def migrate(db) -> dict:
    """Force-push all reminders + saved searches into the relay (idempotent)."""
    rem = sav = 0
    users = {u.id: u for u in db.query(User).filter(User.nostr_npub.isnot(None)).all()}
    for r in db.query(Reminder).all():
        u = users.get(r.user_id)
        if u and await mirror_reminder(db, u, r, force=True):
            rem += 1
    for s in db.query(SavedSearch).all():
        u = users.get(s.user_id)
        if u and await _put(db, u, NS_SEARCH, s.id, _search_rec(s), force=True):
            sav += 1
    return {"reminders": rem, "saved_searches": sav}
