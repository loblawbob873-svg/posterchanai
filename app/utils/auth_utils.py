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
from app.models import User

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
    """
    Get the User object associated with an API key user_id.

    This function takes the user_id directly (already eagerly fetched) to avoid
    lazy loading issues that can cause SQLite session errors.

    Args:
        db: SQLAlchemy database session
        user_id: The user ID from the API key (already fetched)

    Returns:
        The User object if found, None otherwise
    """
    try:
        user = db.query(User).filter(User.id == user_id).first()
        return user
    except (IndexError, Exception) as e:
        # Handle tuple index out of range and other SQLite errors
        logger.warning(f"Error accessing user for API key (attempt 1): {e}")
        try:
            # Check if session is active before rollback
            if db.is_active:
                try:
                    db.rollback()
                except Exception as rollback_error:
                    logger.debug(f"Could not rollback during user query retry (database may be closed): {rollback_error}")

            # Retry query
            user = db.query(User).filter(User.id == user_id).first()
            return user
        except Exception as retry_e:
            logger.error(f"Error accessing user for API key (retry failed): {retry_e}")
            return None
