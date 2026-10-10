"""Pinned/saved commands — the `pin` command.

A user pins something they run often (`pin ai news`, or any command like
`pin screenshot https://google.com`); `pins` lists them with clickable Run / Delete.
Running one re-runs it: a bare query → `search <query>`, anything starting with a known
command → that command verbatim (resolved at run time via CommandService.parse_command).
No scheduler/time component (unlike reminders) — plain CRUD shared by web UI + Telegram."""
import logging
import re
from datetime import datetime
from types import SimpleNamespace
from typing import Optional

from sqlalchemy.orm import Session

from app.models import User

logger = logging.getLogger("saved_search_service")

_MAX_QUERY = 300


def normalize_query(query: str) -> str:
    """Store just the search terms for a plain search pin. Users often pin `search xrp news`
    (with the word "search"), which would otherwise double up to `search search xrp news`
    when re-run — strip a leading "search "/"web search ". NOTE: only the literal search verb
    is stripped, never real command words like `images`/`screenshot`, so `pin <command> ...`
    keeps the command and re-runs it verbatim."""
    q = (query or "").strip()
    q = re.sub(r'^\s*(search|web\s*search)\s+', '', q, flags=re.IGNORECASE)
    return q.strip()[:_MAX_QUERY]


# The store of record is the `saved_searches` DocTable (#161): one operator document per pin, keyed by its
# integer id as a string, row {user_id, query, created_at}. Callers get a SimpleNamespace with the old model's
# attribute names. "Could not ask" raises relay_reader.Unavailable -- never "you have no pins".

UNAVAILABLE_TEXT = ("⚠️ Pins are unavailable right now — this node's datastore could not be asked. "
                    "Try again in a moment.")


def search_row(user_id, query, created_at=None) -> dict:
    from app.services.app_tables import to_iso
    return {"user_id": user_id, "query": query or "", "created_at": to_iso(created_at)}


def as_search(key, row: dict) -> SimpleNamespace:
    from app.services.app_tables import from_iso
    return SimpleNamespace(id=int(key), user_id=row.get("user_id"), query=row.get("query") or "",
                           created_at=from_iso(row.get("created_at")))


async def acreate_saved_search(db: Session, user: User, query: str) -> Optional[SimpleNamespace]:
    from app.services.app_tables import table, row_lock, new_id, SAVED_SEARCHES
    query = normalize_query(query)
    if not query:
        return None
    t = await table(SAVED_SEARCHES)
    async with row_lock(SAVED_SEARCHES):
        # De-dupe: if the same query is already pinned, return the existing row.
        rows = await t.aall()
        mine = sorted((int(k), r) for k, r in rows.items()
                      if r.get("user_id") == user.id and r.get("query") == query)
        if mine:
            return as_search(*mine[0])
        row = search_row(user.id, query, datetime.utcnow())
        sid = await t.ainsert(row, lambda: new_id(rows))    # SQL's id until the marker exists
    return as_search(sid, row)


async def alist_saved_searches(db: Session, user: User) -> list:
    """The user's pins, newest first."""
    from app.services.app_tables import table, SAVED_SEARCHES
    t = await table(SAVED_SEARCHES)
    out = [as_search(k, r) for k, r in await t.awhere(lambda r: r.get("user_id") == user.id)]
    out.sort(key=lambda s: (s.created_at or datetime.min, s.id), reverse=True)
    return out


async def adelete_saved_search(db: Session, user: User, sid: int) -> bool:
    from app.services.app_tables import table, row_lock, SAVED_SEARCHES
    t = await table(SAVED_SEARCHES)
    async with row_lock(SAVED_SEARCHES):
        row = await t.aget(str(int(sid)))
        if not row or row.get("user_id") != user.id:
            return False
        await t.adelete(str(int(sid)))
    return True


# ---- the SQL side (#161 wave 1): SQL is the store of record until the `saved_searches` marker exists ----
def _search_from_sql(r) -> dict:
    return search_row(r.user_id, r.query, r.created_at)


def _register_legacy():
    from app.services import table_migration
    from app.services.app_tables import SAVED_SEARCHES
    from app.services.legacy_sql import ModelLegacy
    table_migration.register(ModelLegacy(SAVED_SEARCHES, "SavedSearch", _search_from_sql))


_register_legacy()
