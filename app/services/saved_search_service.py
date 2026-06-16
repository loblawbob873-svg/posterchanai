"""Pinned/saved searches — the `pin` command.

A user saves a search query (`pin ai news`); `pins` lists them with clickable Run / Delete.
Running one just executes `search <query>`. No scheduler/time component (unlike reminders) — plain
CRUD shared by the web UI and Telegram."""
import logging
from typing import Optional

from sqlalchemy.orm import Session

from app.models import User, SavedSearch

logger = logging.getLogger("saved_search_service")

_MAX_QUERY = 300


def create_saved_search(db: Session, user: User, query: str) -> Optional[SavedSearch]:
    query = (query or "").strip()[:_MAX_QUERY]
    if not query:
        return None
    # De-dupe: if the same query is already pinned, return the existing row.
    existing = (db.query(SavedSearch)
                .filter(SavedSearch.user_id == user.id, SavedSearch.query == query)
                .first())
    if existing:
        return existing
    s = SavedSearch(user_id=user.id, query=query)
    db.add(s)
    db.commit()
    db.refresh(s)
    return s


def list_saved_searches(db: Session, user: User) -> list:
    return (db.query(SavedSearch)
            .filter(SavedSearch.user_id == user.id)
            .order_by(SavedSearch.created_at.desc())
            .all())


def delete_saved_search(db: Session, user: User, sid: int) -> bool:
    s = (db.query(SavedSearch)
         .filter(SavedSearch.id == sid, SavedSearch.user_id == user.id)
         .first())
    if not s:
        return False
    db.delete(s)
    db.commit()
    return True
