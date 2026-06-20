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
        ...     user = db.query(User).filter(User.id == user_id).first()
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
    """
    Get the User object associated with an API key user_id.
    
    This function takes the user_id directly (already eagerly fetched) to avoid
    lazy loading issues that can cause SQLite session errors.
    
    Args:
        db: SQLAlchemy database session
        user_id: The user ID from the API key (already fetched)
    
    Returns:
        The User object if found, None otherwise
    
    Example:
        >>> api_key, user_id = query_api_key_with_retry(db, "sk-...")
        >>> if api_key and user_id:
        ...     user = get_user_from_api_key(db, user_id)
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
