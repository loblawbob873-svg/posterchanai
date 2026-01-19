"""
Authentication utility functions for API key management.
"""
import logging
from typing import Optional
from sqlalchemy.orm import Session
from app.models import APIKey, User

logger = logging.getLogger(__name__)


def query_api_key_with_retry(db: Session, token: str, max_retries: int = 1) -> Optional[APIKey]:
    """
    Query an API key from the database with automatic retry on SQLite session errors.
    
    This function handles SQLite InterfaceError exceptions that can occur due to
    session management issues, automatically rolling back and retrying the query.
    
    Args:
        db: SQLAlchemy database session
        token: The API key token to query (should start with "sk-")
        max_retries: Maximum number of retry attempts (default: 1)
    
    Returns:
        The APIKey object if found, None otherwise
    
    Example:
        >>> api_key = query_api_key_with_retry(db, "sk-...")
        >>> if api_key:
        ...     user_id = api_key.user_id
    """
    try:
        api_key = db.query(APIKey).filter(
            APIKey.key == token,
            APIKey.is_active == True
        ).first()
        return api_key
    except Exception as e:
        # Handle SQLite session errors
        logger.warning(f"Error querying API key: {e}")
        if max_retries > 0:
            try:
                db.rollback()
                # Retry once with a fresh query
                api_key = db.query(APIKey).filter(
                    APIKey.key == token,
                    APIKey.is_active == True
                ).first()
                return api_key
            except Exception as retry_error:
                logger.error(f"Error retrying API key query: {retry_error}")
                return None
        return None


def get_user_from_api_key(db: Session, api_key: APIKey) -> Optional[User]:
    """
    Get the User object associated with an API key, with proper error handling.
    
    This function safely accesses the user_id from the APIKey object to avoid
    lazy loading issues that can cause SQLite session errors.
    
    Args:
        db: SQLAlchemy database session
        api_key: The APIKey object
    
    Returns:
        The User object if found, None otherwise
    
    Raises:
        Exception: If there's an error accessing the user_id or querying the user
    
    Example:
        >>> api_key = query_api_key_with_retry(db, "sk-...")
        >>> if api_key:
        ...     user = get_user_from_api_key(db, api_key)
    """
    try:
        # Access user_id directly from the APIKey object to avoid lazy loading issues
        # This prevents SQLite session errors when accessing the relationship
        user_id = api_key.user_id
        user = db.query(User).filter(User.id == user_id).first()
        return user
    except Exception as e:
        logger.error(f"Error accessing user for API key: {e}", exc_info=True)
        return None
