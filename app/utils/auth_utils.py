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
    try:
        # Query API key using raw connection to avoid SQLite parameter binding issues
        # SQLite can have issues with SQLAlchemy parameter binding, so use raw connection
        # Get the actual DBAPI connection from SQLAlchemy
        raw_conn = db.connection()
        # For SQLAlchemy 1.4+, connection() returns the connection directly
        # For older versions, might need raw_conn.connection
        if hasattr(raw_conn, 'connection'):
            dbapi_conn = raw_conn.connection
        else:
            dbapi_conn = raw_conn
        
        cursor = dbapi_conn.cursor()
        
        # Use parameterized query with ? placeholder (SQLite native format)
        cursor.execute("""
            SELECT id, user_id, key, name, created_at, last_used_at, is_active
            FROM api_keys
            WHERE key = ? AND is_active = 1
            LIMIT 1
        """, (token,))
        
        result = cursor.fetchone()
        cursor.close()
        
        if result:
            # Create APIKey object from result row
            api_key = APIKey(
                id=result[0],
                user_id=result[1],
                key=result[2],
                name=result[3],
                created_at=result[4],
                last_used_at=result[5],
                is_active=result[6]
            )
            user_id = result[1]  # Eagerly fetch user_id
            return api_key, user_id
        return None, None
    except Exception as e:
        # Handle SQLite session errors including parameter binding issues
        logger.warning(f"Error querying API key: {e}")
        if max_retries > 0:
            try:
                db.rollback()
                # Retry once with raw connection
                raw_conn = db.connection()
                if hasattr(raw_conn, 'connection'):
                    dbapi_conn = raw_conn.connection
                else:
                    dbapi_conn = raw_conn
                
                cursor = dbapi_conn.cursor()
                
                cursor.execute("""
                    SELECT id, user_id, key, name, created_at, last_used_at, is_active
                    FROM api_keys
                    WHERE key = ? AND is_active = 1
                    LIMIT 1
                """, (token,))
                
                result = cursor.fetchone()
                cursor.close()
                if result:
                    api_key = APIKey(
                        id=result[0],
                        user_id=result[1],
                        key=result[2],
                        name=result[3],
                        created_at=result[4],
                        last_used_at=result[5],
                        is_active=result[6]
                    )
                    user_id = result[1]
                    return api_key, user_id
                return None, None
            except Exception as retry_error:
                logger.error(f"Error retrying API key query: {retry_error}")
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
            # Rollback and retry
            db.rollback()
            user = db.query(User).filter(User.id == user_id).first()
            return user
        except Exception as retry_e:
            logger.error(f"Error accessing user for API key (retry failed): {retry_e}")
            return None
