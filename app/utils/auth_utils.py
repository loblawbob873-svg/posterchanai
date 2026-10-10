"""
Authentication utility functions for API key management.
"""
import logging
from typing import Optional
from sqlalchemy.orm import Session
from app.models import APIKey, User

logger = logging.getLogger(__name__)


def query_api_key_with_retry(db: Session, token: str, max_retries: int = 1) -> tuple:
    """
    Query an API key from the database with automatic retry on SQLite session errors.
    
    This function handles SQLite InterfaceError exceptions that can occur due to
    session management issues, automatically rolling back and retrying the query.
    
    Returns a tuple of (api_key, user_id) to avoid lazy loading issues.
    The user_id is eagerly fetched while the session is still valid.
    
    Args:
        db: SQLAlchemy database session
        token: The API key token to query (should start with "sk-")
        max_retries: Maximum number of retry attempts (default: 1)
    
    Returns:
        Tuple of (APIKey object, user_id) if found, (None, None) otherwise
    
    Example:
        >>> api_key, user_id = query_api_key_with_retry(db, "sk-...")
        >>> if api_key and user_id:
        ...     user = get_user_from_api_key(db, user_id)
    """
    # ORM query — DB-agnostic (the old raw `WHERE key = ? AND is_active = 1` was SQLite-only and
    # errored on PostgreSQL: `?` isn't a PG placeholder and `is_active = 1` isn't a boolean compare).
    # Retries once on a transient session error (rollback first).
    for attempt in range(max_retries + 1):
        try:
            ak = (db.query(APIKey)
                    .filter(APIKey.key == token, APIKey.is_active == True)  # noqa: E712
                    .first())
            return (ak, ak.user_id) if ak else (None, None)
        except Exception as e:
            logger.warning(f"Error querying API key (attempt {attempt + 1}): {e}")
            try:
                if db.is_active:
                    db.rollback()
            except Exception as rollback_error:
                logger.debug(f"Could not rollback during API key retry: {rollback_error}")
            if attempt >= max_retries:
                return None, None
    return None, None


def get_user_from_api_key(db: Session, user_id: int) -> Optional[User]:
    """The account an API key belongs to (`user_table`, #161 wave 3), or None when the store answered that there
    is no such account. "Could not ask" RAISES (relay_reader.Unavailable -> 503): it used to be swallowed here
    into None, which every caller answers as "Invalid API key" -- a relay blip read as a revoked key."""
    from app.services import user_table
    return user_table.get(db, user_id)
