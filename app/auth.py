from datetime import datetime, timedelta
from typing import Optional
from jose import JWTError, jwt
import bcrypt
import secrets
import logging
from fastapi import Depends, HTTPException, status, Request, WebSocket
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from sqlalchemy.orm import Session
from app.database import get_db
from app.services.user_table import UserRow as User
from app.utils.auth_utils import query_api_key_with_retry, get_user_from_api_key
from app.services.relay_reader import Unavailable
import os
import time
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
    # `iat` is what lets a session be ended before it expires: see token_revoked.
    to_encode.update({"exp": expire, "iat": int(time.time())})
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)


def token_revoked(payload: Optional[dict], user) -> bool:
    """A SESSION TOKEN IS VALID UNTIL IT EXPIRES -- UNLESS ITS ACCOUNT SAID "SIGN OUT EVERYWHERE".

    Security review #11 (2026-10-14): a session token was valid for its full 30 days whatever happened -- a
    lost phone, a leaked cookie, a password change all left every issued token working. An account now
    carries `tokens_valid_after` (unix seconds); a token issued before it is refused. Tokens minted before
    `iat` existed carry none, and are refused only once their account has set the switch. One rule, called
    by every place that turns a token into a user (HTTP, websockets, the SSH terminal, Telegram calls)."""
    after = getattr(user, "tokens_valid_after", None) if user is not None else None
    if not after:
        return False
    try:
        return int((payload or {}).get("iat") or 0) < int(after)
    except (TypeError, ValueError):
        return True


def decode_token(token: str) -> Optional[dict]:
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        return payload
    except JWTError:
        return None


def _refuse_blocked(user):
    """A web account whose linked Nostr key is on the relay's block list can do nothing here -- an
    existing login and an API key included ("we need to make sure that blocked users can't do anything
    on the platform despite having a nip05 in their profile")."""
    from app.services import relay_blocklist
    if relay_blocklist.is_user_blocked(user):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="This account is blocked on this server")
    return user


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
        from app.services import api_key_store
        try:
            api_key, user_id = query_api_key_with_retry(db, token)
        except Unavailable:
            # The key table could not be read: neither a pass nor "invalid" -- try again.
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                                detail="API keys cannot be checked right now; try again shortly")
        if api_key and user_id:
            user = get_user_from_api_key(db, user_id)
            if user:
                api_key_store.touch(api_key)       # last_used_at, throttled; never fails the request
                request.state.via_api_key = True   # a route that MINTS a session must refuse this (routers/auth.py)
                return _refuse_blocked(user)

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

    # "User not found" only when the store ANSWERED that there is no such account; a relay that could not be
    # asked raises Unavailable (503 + Retry-After) -- a session is never logged out by an outage (#161 wave 3).
    from app.services import user_table
    user = user_table.get(db, user_id_int)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User not found"
        )
    if token_revoked(payload, user):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="This session was signed out")
    request.state.token_payload = payload             # get_admin_user's repair branch checks it against the operator

    return _refuse_blocked(user)


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
    try:
        from app.services import user_table
        from app.services.nostr import nostr_service
        op = user_table.operator(db)
        _OPERATOR_PK = nostr_service.derive_pubkey(nostr_service.decode_seckey(op.nostr_nsec)) if op else None
    except Unavailable:
        raise                   # "could not ask" -> 503, never "this is not the operator" (403)
    except Exception as e:                                  # pragma: no cover - never fatal
        # NOT cached: an account table that could not be read this time is asked again next time
        logger.warning("[auth] could not derive the operator pubkey: %s", e)
        return None
    return _OPERATOR_PK


_UNSET = object()
_OPERATOR_PK = _UNSET


def get_admin_user(current_user: User = Depends(get_current_user),
                   db: Session = Depends(get_db), request: Request = None) -> User:
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
                from app.services import user_table
                operator = user_table.operator(db)
                # Served AS the operator, so the operator's "sign out everywhere" ends it too -- it was checked only
                # against the duplicate account's switch (security review 2026-10-18).
                payload = getattr(getattr(request, "state", None), "token_payload", None)
                if operator and request is not None and token_revoked(payload, operator):
                    raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="This session was signed out")
                if operator:
                    logger.info("[auth] session on %s carries the operator's own key — serving it "
                                "as the operator", current_user.username)
                    return operator
    except (Unavailable, HTTPException):
        raise
    except Exception as e:                                  # pragma: no cover - never fatal
        logger.warning("[auth] operator-session repair failed: %s", e)

    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="Admin access required"
    )


async def get_ai_user(current_user: User = Depends(get_current_user)) -> User:
    """Gate AI features: admins always pass; so does the admin-granted `can_ai` flag; so does a
    confirmed instance member (a NIP-05 name THIS NODE granted, published in their own profile).
    Nostr-signup users start without the flag and request access (admin approves) — see the can_ai
    flow — but a member no longer has to wait for that, or for the 15-minute access reconcile.

    The membership clause is ADDITIVE and never denies: see app/services/nip05_access.py."""
    from app.services import nip05_access
    if not await nip05_access.ai_allowed(current_user):
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

    # None only when the store answered "no such account"; "could not ask" raises (the socket is then closed
    # as an error the client retries, never answered as a signed-out user).
    from app.services import relay_blocklist, user_table
    user = await user_table.aget(db, user_id_int)
    if token_revoked(payload, user):
        return None
    return None if relay_blocklist.is_user_blocked(user) else user
