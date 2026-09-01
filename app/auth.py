from datetime import datetime, timedelta, timezone
from typing import Optional
from jose import JWTError, jwt
import bcrypt
import secrets
import logging
from fastapi import Depends, HTTPException, status, Request, WebSocket
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from sqlalchemy.orm import Session
from app.database import get_db
from app.models import User
from app.utils.auth_utils import query_api_key_with_retry, get_user_from_api_key
import os
import urllib.parse
import warnings

logger = logging.getLogger(__name__)

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

    # If not in header, try cookie (value may be URL-encoded by client)
    if not token:
        token = request.cookies.get("access_token")
        if token:
            token = urllib.parse.unquote(token)

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
        # Get api_key AND user_id together to avoid lazy loading issues
        api_key, user_id = query_api_key_with_retry(db, token)
        
        if api_key and user_id:
            # Get user using the already-fetched user_id (no lazy loading needed)
            user = get_user_from_api_key(db, user_id)
            
            if user:
                # Now update last used timestamp (after we've already fetched user)
                try:
                    # Update last_used_at using direct SQL to avoid SQLite parameter binding issues
                    # SQLite can have issues with ORM updates, so use raw SQL
                    try:
                        from sqlalchemy import text
                        now_utc = datetime.now(timezone.utc)
                        # Use parameterized query but with explicit parameter names to avoid SQLite issues
                        db.execute(
                            text("UPDATE api_keys SET last_used_at = :last_used_at WHERE id = :id"),
                            {"last_used_at": now_utc, "id": api_key.id}
                        )
                        db.commit()
                    except Exception as e:
                        # If direct SQL update fails, try ORM method as fallback
                        try:
                            db.rollback()
                            # Fallback: try refreshing and updating via ORM
                            try:
                                db.refresh(api_key)
                            except Exception:
                                pass
                            api_key.last_used_at = datetime.now(timezone.utc)
                            db.commit()
                        except Exception as fallback_error:
                            # If both methods fail, rollback but we already have the user
                            try:
                                db.rollback()
                            except Exception:
                                pass
                            logger.warning(f"Failed to update API key last_used_at (both methods): {fallback_error}")
                except Exception as e:
                    # If commit fails, rollback but we already have the user
                    try:
                        db.rollback()
                    except Exception:
                        pass  # Ignore rollback errors
                    logger.warning(f"Failed to update API key last_used_at: {e}")
                
                return user
        
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
    if user_id is None or user_id == "":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token"
        )

    try:
        user_id_int = int(user_id)
    except (ValueError, TypeError):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token format"
        )

    user = db.query(User).filter(User.id == user_id_int).first()
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


def _operator_pubkey(db: Session) -> Optional[str]:
    """The pubkey this node signs its own events with, or None. Cached: it cannot change without a
    restart, and this runs on every admin request."""
    global _OPERATOR_PK
    if _OPERATOR_PK is not _UNSET:
        return _OPERATOR_PK
    _OPERATOR_PK = None
    try:
        from app.services.nostr import nostr_service
        op = db.query(User).filter(User.is_admin == True,  # noqa: E712
                                   User.nostr_nsec.isnot(None)).first()
        if op:
            _OPERATOR_PK = nostr_service.derive_pubkey(nostr_service.decode_seckey(op.nostr_nsec))
    except Exception as e:                                  # pragma: no cover - never fatal
        logger.warning("[auth] could not derive the operator pubkey: %s", e)
    return _OPERATOR_PK


_UNSET = object()
_OPERATOR_PK = _UNSET


def get_admin_user(current_user: User = Depends(get_current_user),
                   db: Session = Depends(get_db)) -> User:
    if current_user.is_admin:
        return current_user

    # AN EXISTING SESSION HEALS ITSELF — NOBODY SHOULD HAVE TO LOG OUT TO FIX OUR BUG.
    #
    # nostr-login used to mint a second, ordinary account for an operator whose account had no
    # linked npub, so phones are already signed in AS that duplicate. Fixing the login path alone
    # left every one of those sessions broken until the person signed out and in again — which is
    # asking the user to perform the repair, and they were right to refuse.
    #
    # The claim is identical to the one the login path makes: this account carries the npub of the
    # key this node signs with, so whoever holds it already controls the node completely. That is
    # true of a live session exactly as it is of a fresh sign-in, so it is honoured here too and
    # the phone recovers on its next request with nothing asked of anybody.
    try:
        npub = (current_user.nostr_npub or "").strip()
        if npub:
            from app.services.nostr import nostr_service
            op_pk = _operator_pubkey(db)
            if op_pk and nostr_service.to_pubkey_hex(npub) == op_pk:
                operator = db.query(User).filter(User.is_admin == True,  # noqa: E712
                                                 User.nostr_nsec.isnot(None)).first()
                if operator:
                    logger.info("[auth] session on %s carries the operator's own key — serving it "
                                "as the operator", current_user.username)
                    return operator
    except Exception as e:                                  # pragma: no cover - never fatal
        logger.warning("[auth] operator-session repair failed: %s", e)

    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="Admin access required"
    )


def get_ai_user(current_user: User = Depends(get_current_user)) -> User:
    """Gate AI features: admins always pass; everyone else needs the admin-granted `can_ai` flag.
    Nostr-signup users start without it and request access (admin approves) — see the can_ai flow."""
    if not (current_user.is_admin or getattr(current_user, "can_ai", False)):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="AI access not enabled for this account — request access and an admin will approve."
        )
    return current_user


# The ORIGINS a native build legitimately speaks from — Capacitor on Android (androidScheme=https →
# https://localhost), Capacitor elsewhere, and the Electron desktop bundle's privileged scheme. One
# list, because it is used twice: the CORS middleware in main.py and the SSH terminal's WebSocket
# origin check, and two copies is how the socket ends up refusing the app the API already trusts.
#
# NOT http://localhost. With credentials allowed, any plaintext-http page on localhost could read the
# victim's authed responses — see the note beside the CORS middleware.
NATIVE_APP_ORIGINS = ("https://localhost", "capacitor://localhost", "app://posterchan")


async def get_user_from_websocket(websocket: WebSocket, db: Session) -> Optional[User]:
    # Try to get token from query params or cookies (cookie may be URL-encoded)
    token = websocket.query_params.get("token")
    if not token:
        token = websocket.cookies.get("access_token")
        if token:
            token = urllib.parse.unquote(token)

    if not token:
        return None

    payload = decode_token(token)
    if payload is None:
        return None

    user_id = payload.get("sub")
    if user_id is None or user_id == "":
        return None

    try:
        user_id_int = int(user_id)
    except (ValueError, TypeError):
        return None

    return db.query(User).filter(User.id == user_id_int).first()
