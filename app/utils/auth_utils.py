"""
Authentication utility functions for API key management.

API keys live in the `api_keys` DocTable (app/services/api_key_store.py). A key check has THREE
answers: a key row, None (no such active key -> 401/403 as before), or `relay_reader.Unavailable`
raised (the table could not be read -> the caller answers 503; it must neither accept the token nor
call it invalid).
"""
import logging
from typing import Optional
from sqlalchemy.orm import Session
from app.services.user_table import UserRow as User

logger = logging.getLogger(__name__)


def query_api_key_with_retry(db: Session, token: str, max_retries: int = 1) -> tuple:
    """(api_key row, user_id) for an ACTIVE key equal to `token`, else (None, None).

    Sync: call it from a sync route (FastAPI runs those on a worker thread). Raises
    `Unavailable` when the key table cannot be read. `db` and `max_retries` are kept for callers;
    the lookup no longer touches SQL."""
    from app.services import api_key_store
    ak = api_key_store.lookup(token)
    return (ak, ak.user_id) if ak else (None, None)


async def aquery_api_key(token: str) -> tuple:
    """`query_api_key_with_retry` for async code (never blocks the event loop)."""
    from app.services import api_key_store
    ak = await api_key_store.alookup(token)
    return (ak, ak.user_id) if ak else (None, None)


def get_user_from_api_key(db: Session, user_id: int) -> Optional[User]:
    """The account an API key belongs to (`user_table`, #161 wave 3), or None when the store answered that there
    is no such account. "Could not ask" RAISES (relay_reader.Unavailable -> 503): it used to be swallowed here
    into None, which every caller answers as "Invalid API key" -- a relay blip read as a revoked key."""
    from app.services import user_table
    return user_table.get(db, user_id)
