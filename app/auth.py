from datetime import datetime, timedelta, timezone
from typing import Optional
from jose import JWTError, jwt
import bcrypt
import secrets
from fastapi import Depends, HTTPException, status, Request, WebSocket
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from sqlalchemy.orm import Session
from app.database import get_db
from app.models import User, APIKey
import os
import warnings

# Get or generate a persistent secret key
def _get_secret_key() -> str:
    # First check environment variable
    key = os.getenv("SECRET_KEY")
    if key:
        return key

    # Try to load from file for persistence across restarts
    key_file = os.path.join(os.path.dirname(__file__), ".secret_key")
    if os.path.exists(key_file):
        with open(key_file, "r") as f:
            return f.read().strip()

    # Generate new key and save to file
    key = secrets.token_hex(32)
    try:
        with open(key_file, "w") as f:
            f.write(key)
        os.chmod(key_file, 0o600)  # Restrict permissions
    except OSError:
        warnings.warn(
            "Could not save SECRET_KEY to file. Sessions will be invalidated on restart.",
            RuntimeWarning
        )
    return key

SECRET_KEY = _get_secret_key()
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_DAYS = 30

security = HTTPBearer(auto_error=False)


def verify_password(plain_password: str, hashed_password: str) -> bool:
    return bcrypt.checkpw(plain_password.encode('utf-8'), hashed_password.encode('utf-8'))


def get_password_hash(password: str) -> str:
    return bcrypt.hashpw(password.encode('utf-8'), bcrypt.gensalt()).decode('utf-8')


def create_access_token(data: dict, expires_delta: Optional[timedelta] = None) -> str:
    to_encode = data.copy()
    expire = datetime.utcnow() + (expires_delta or timedelta(days=ACCESS_TOKEN_EXPIRE_DAYS))
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)


def decode_token(token: str) -> Optional[dict]:
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        return payload
    except JWTError:
        return None


def get_current_user(
    request: Request,
    credentials: HTTPAuthorizationCredentials = Depends(security),
    db: Session = Depends(get_db)
) -> User:
    # Try to get token from header first
    token = None
    if credentials:
        token = credentials.credentials

    # If not in header, try cookie
    if not token:
        token = request.cookies.get("access_token")

    # If not in cookie, try query parameter (for streaming endpoints like music)
    if not token:
        token = request.query_params.get("token")

    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated"
        )

    # Check if this is an API key (starts with sk-)
    if token.startswith("sk-"):
        try:
            api_key = db.query(APIKey).filter(
                APIKey.key == token,
                APIKey.is_active == True
            ).first()
        except Exception as e:
            # Handle SQLite session errors
            logger.warning(f"Error querying API key: {e}")
            try:
                db.rollback()
                # Retry once with a fresh query
                api_key = db.query(APIKey).filter(
                    APIKey.key == token,
                    APIKey.is_active == True
                ).first()
            except Exception as retry_error:
                logger.error(f"Error retrying API key query: {retry_error}")
                api_key = None
        
        if api_key:
            # Update last used timestamp
            try:
                api_key.last_used_at = datetime.now(timezone.utc)
                db.commit()
            except Exception as e:
                # If commit fails, rollback and continue without updating timestamp
                db.rollback()
                logger.warning(f"Failed to update API key last_used_at: {e}")
            
            # Access user_id directly from the APIKey object to avoid lazy loading issues
            # This prevents SQLite session errors when accessing the relationship
            try:
                user_id = api_key.user_id
                user = db.query(User).filter(User.id == user_id).first()
                if user:
                    return user
            except Exception as e:
                logger.error(f"Error accessing user for API key: {e}", exc_info=True)
                # Fall through to raise Invalid API key exception
        
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid API key"
        )

    # Otherwise treat as JWT token
    payload = decode_token(token)
    if payload is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token"
        )

    user_id = payload.get("sub")
    if user_id is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token"
        )

    user = db.query(User).filter(User.id == int(user_id)).first()
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User not found"
        )

    return user


def get_current_user_optional(
    request: Request,
    credentials: HTTPAuthorizationCredentials = Depends(security),
    db: Session = Depends(get_db)
) -> Optional[User]:
    try:
        return get_current_user(request, credentials, db)
    except HTTPException:
        return None


def get_admin_user(current_user: User = Depends(get_current_user)) -> User:
    if not current_user.is_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin access required"
        )
    return current_user


async def get_user_from_websocket(websocket: WebSocket, db: Session) -> Optional[User]:
    # Try to get token from query params or cookies
    token = websocket.query_params.get("token")
    if not token:
        token = websocket.cookies.get("access_token")

    if not token:
        return None

    payload = decode_token(token)
    if payload is None:
        return None

    user_id = payload.get("sub")
    if user_id is None:
        return None

    return db.query(User).filter(User.id == int(user_id)).first()
