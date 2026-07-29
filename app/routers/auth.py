import os
import secrets
import logging
import time
from typing import List
from datetime import datetime, timedelta
from fastapi import APIRouter, Depends, HTTPException, status, Response, UploadFile, File, Request
from starlette.requests import Request as StarletteRequest
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session
from sqlalchemy.exc import SQLAlchemyError, IntegrityError, OperationalError
from pydantic import BaseModel
from app.database import get_db

logger = logging.getLogger(__name__)
from app.models import User, APIKey, VerificationToken
from app.schemas import (
    UserLogin, UserResponse, Token, APIKeyCreate, APIKeyResponse, APIKeyListItem,
    UserSettingsUpdate, UserSettingsResponse, BridgeAccessRequest
)
from app.auth import verify_password, create_access_token, get_current_user, get_password_hash
from app.services.email_service import EmailService
from app.services.storage_service import StorageService

router = APIRouter(prefix="/api/auth", tags=["auth"])


_APP_ORIGINS = {"https://localhost", "http://localhost", "capacitor://localhost", "ionic://localhost"}


def _cookie_attrs(request: Request):
    """(samesite, secure) for the auth cookie, chosen by the request's ORIGIN — NOT blanket by scheme.
    The Capacitor app (Origin https://localhost) calls the API cross-origin, which needs SameSite=None;
    Secure or the cookie is never sent. But the same-origin PWA (Origin https://poster.place) MUST keep
    SameSite=Lax: it's our only CSRF defense (the CSRF middleware is disabled, relying on SameSite), and
    None would let the session cookie ride cross-site POSTs. So: known app origin → None+Secure; everything
    else (the PWA, direct LAN http) → Lax (+ Secure only when the connection is HTTPS)."""
    secure = False
    origin = ""
    if request is not None:
        proto = (request.headers.get("x-forwarded-proto", "") or "").split(",")[0].strip().lower()
        secure = proto == "https" or request.url.scheme == "https"
        origin = (request.headers.get("origin", "") or "").strip().lower().rstrip("/")
    if origin in _APP_ORIGINS:
        return "none", True   # cross-origin native app; None requires Secure (app is HTTPS)
    return "lax", secure


def _set_auth_cookie(response: Response, token: str, request: Request = None) -> None:
    # httponly stays False: JS reads the token for the WebSocket auth handshake.
    samesite, secure = _cookie_attrs(request)
    response.set_cookie(
        key="access_token", value=token,
        httponly=False,
        secure=secure, max_age=30 * 24 * 60 * 60,
        samesite=samesite, path="/",
    )


@router.post("/login", response_model=Token)
def login(user_data: UserLogin, response: Response, request: Request, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.username == user_data.username).first()
    if not user or not verify_password(user_data.password, user.password_hash):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid username or password"
        )

    token = create_access_token({"sub": str(user.id)})
    _set_auth_cookie(response, token, request)
    return {"access_token": token, "token_type": "bearer"}


class NostrLogin(BaseModel):
    pubkey: str          # npub or 64-hex of the logging-in key
    auth: str            # base64 of a recent Nostr event signed by that key (proves ownership)


def _verify_nostr_auth(auth_b64: str, pubkey_hex: str) -> bool:
    """A base64 signed Nostr event authored by `pubkey_hex`, within a 300s anti-replay window."""
    import base64
    import json
    from app.services.nostr import event as nostr_event
    try:
        ev = json.loads(base64.b64decode(auth_b64))
    except Exception:
        return False
    return (nostr_event.verify_event(ev) and ev.get("pubkey") == pubkey_hex
            and abs(int(ev.get("created_at", 0)) - int(time.time())) <= 300)


@router.post("/nostr-login")
async def nostr_login(data: NostrLogin, response: Response, request: Request, db: Session = Depends(get_db)):
    """Log in / sign up with a Nostr key (NIP-07 / Amber / nsec — signed client-side). Finds the
    user by linked npub or creates a fresh, AI-gated account, then issues the normal session cookie
    so the whole AI app works unchanged. New users have NO AI access until an admin approves."""
    from app.services.nostr import nostr_service
    pk = nostr_service.to_pubkey_hex(data.pubkey)
    if not pk:
        raise HTTPException(status_code=400, detail="invalid pubkey")
    if not _verify_nostr_auth(data.auth, pk):
        raise HTTPException(status_code=403, detail="invalid or stale Nostr signature")
    npub = nostr_service.npub_of(pk)

    user = db.query(User).filter(User.nostr_npub == npub).first()
    created = False
    if not user:
        # derive a unique username from the npub; AI stays off until an admin grants it
        base = "npub_" + npub[4:16]
        username = base
        for i in range(2, 100):
            if not db.query(User).filter(User.username == username).first():
                break
            username = f"{base}{i}"
        user = User(
            username=username, email=None,
            password_hash=get_password_hash(secrets.token_urlsafe(32)),  # unusable (Nostr-only)
            is_admin=False, email_verified=True, nostr_npub=npub,
            can_image=True, can_music=True, can_video=False, can_torrent=False,
            can_blossom=False, can_ai=False,   # gated — request → admin approves
        )
        db.add(user)
        db.commit()
        db.refresh(user)
        created = True
        logger.info("[auth] Nostr signup: %s (%s)", username, npub[:16])
        # Admit the new account to the relay WoT (+ operator follow) so it can post & receive DMs
        # immediately — covers login-with-existing-key users, not just the create-identity flow.
        try:
            from app.routers.client import follow_and_admit
            await follow_and_admit(db, pk)
        except Exception as e:
            logger.warning("[auth] follow/admit on nostr-login failed: %s", e)

    # Provision the server-held per-user storage key (used to encrypt this user's chats/uploads at
    # rest in the relay and decrypt them for the AI). Created once, reused thereafter.
    try:
        from app.services import nostr_store
        nostr_store.user_storage_seckey(db, user)
    except Exception as e:
        logger.warning("[auth] storage key provisioning failed for %s: %s", npub[:16], e)

    # TURNKEY admin: the FIRST npub to sign in claims admin automatically — no manual "become admin"
    # click — so a fresh node is immediately usable by its owner (their OWN key gets full access:
    # AI / image / Blossom). Locked the moment any admin with an npub exists, so it can never take
    # over a configured instance. Disable with POSTERCHANAI_AUTO_ADMIN=0 (admin then granted manually).
    if os.environ.get("POSTERCHANAI_AUTO_ADMIN", "1").strip().lower() in ("1", "true", "yes", "on"):
        if not db.query(User).filter(User.is_admin == True, User.nostr_npub.isnot(None)).first():  # noqa: E712
            user.is_admin = True
            user.can_ai = True
            user.can_image = True
            user.can_blossom = True
            db.commit()
            logger.info("[auth] first-login admin auto-claimed by %s", npub[:16])
            try:
                from app.services import settings_store as _ss
                _seeds = _ss.get("nostr_relay_wot_seeds", "") or ""
                if npub not in _seeds:
                    _ss.put("nostr_relay_wot_seeds", (_seeds.rstrip() + "\n" + npub).strip() if _seeds.strip() else npub)
            except Exception as _e:
                logger.warning("[auth] could not seed WoT with first admin: %s", _e)
            # Refresh the relay's operator set so it trusts the new admin's keys for writes immediately,
            # AND rebuild the WoT from the new seed + the admin's follows — otherwise a fresh turnkey node
            # leaves the WoT-only global timeline empty until the next periodic rebuild (the "no global
            # posts on a fresh node" report).
            try:
                from app.services.nostr_relay.thread import trigger_block_reload, trigger_wot_refresh
                trigger_block_reload()
                trigger_wot_refresh()
            except Exception:
                pass

    # Mirror the account-authority record to the relay (the authoritative datastore).
    try:
        from app.services import users_store
        await users_store.sync_user(db, user)
    except Exception as e:
        logger.warning("[auth] account sync to relay failed for %s: %s", npub[:16], e)

    token = create_access_token({"sub": str(user.id)})
    _set_auth_cookie(response, token, request)
    return {
        "access_token": token, "token_type": "bearer",
        "user": {"id": user.id, "username": user.username, "npub": npub,
                 "is_admin": bool(user.is_admin), "can_ai": bool(user.is_admin or user.can_ai),
                 # Lets the client hide Go Live for accounts that can't use it, rather than showing a
                 # button whose only outcome is a permission error.
                 "can_stream": bool(user.is_admin or getattr(user, "can_stream", False)),
                 "new": created},
    }


async def _notify_admins_ai_request(db: Session, requester: User, npub: str) -> None:
    """DM every admin (with a linked npub) over Nostr that this user wants AI access, sent from the
    node operator's key via the local relay (kind-4 NIP-04 — shows in their client's Messages)."""
    from app.services.nostr import nostr_service, nip04
    from app.services.nostr.event import build_event
    from app.services import nostr_store
    op = db.query(User).filter(User.is_admin == True, User.nostr_nsec.isnot(None)).first()  # noqa: E712
    if not op:
        return
    try:
        op_sk = nostr_service.decode_seckey(op.nostr_nsec)
    except Exception:
        return
    from app.services import settings_store
    port = settings_store.get_int("nostr_relay_port", 3052)
    text = (f"🤖 AI access requested by {requester.username} ({npub}). "
            f"Approve from their profile ☰ menu or Admin → Users.")
    admins = db.query(User).filter(User.is_admin == True, User.nostr_npub.isnot(None)).all()  # noqa: E712
    for a in admins:
        apk = nostr_service.to_pubkey_hex(a.nostr_npub)
        if not apk:
            continue
        try:
            ev = build_event(op_sk, 4, nip04.encrypt(op_sk, bytes.fromhex(apk), text), tags=[["p", apk]])
            await nostr_store._ws_publish(port, ev)
        except Exception as e:
            logger.debug("[auth] ai-request DM to %s failed: %s", apk[:8], e)


@router.post("/ai-request")
async def ai_request(data: NostrLogin, db: Session = Depends(get_db)):
    """A Nostr-signup user requests AI access; an admin approves it (profile ☰ menu / Admin → Users).
    Records the pending request and DMs the admins over Nostr."""
    from app.services.nostr import nostr_service
    from app.models import UserSetting
    pk = nostr_service.to_pubkey_hex(data.pubkey)
    if not pk:
        raise HTTPException(status_code=400, detail="invalid pubkey")
    if not _verify_nostr_auth(data.auth, pk):
        raise HTTPException(status_code=403, detail="invalid or stale Nostr signature")
    npub = nostr_service.npub_of(pk)
    user = db.query(User).filter(User.nostr_npub == npub).first()
    if not user:
        raise HTTPException(status_code=404, detail="log in with your Nostr key first")
    if user.is_admin or user.can_ai:
        return {"ok": True, "already": True}
    row = db.query(UserSetting).filter(UserSetting.user_id == user.id,
                                       UserSetting.key == "ai_requested").first()
    if row:
        row.value = str(int(time.time()))
    else:
        db.add(UserSetting(user_id=user.id, key="ai_requested", value=str(int(time.time()))))
    db.commit()
    logger.info("[auth] AI access requested by %s (%s)", user.username, npub[:16])
    try:
        await _notify_admins_ai_request(db, user, npub)
    except Exception as e:
        logger.warning("[auth] ai-request admin notify failed: %s", e)
    return {"ok": True}


@router.post("/logout")
def logout(response: Response, request: Request):
    # Clear with the SAME SameSite/Secure the cookie was set with (Origin-aware) — a cross-origin app
    # cookie is SameSite=None; Secure, and a Lax delete won't clear it in that context.
    samesite, secure = _cookie_attrs(request)
    response.delete_cookie("access_token", path="/", samesite=samesite, secure=secure)
    return {"message": "Logged out"}


@router.get("/me", response_model=UserResponse)
def get_me(current_user: User = Depends(get_current_user)):
    return current_user


@router.get("/verify/{token}")
def verify_email(token: str, response: Response, db: Session = Depends(get_db)):
    """Verify email address using token from email link"""
    # Find the verification token
    verification = db.query(VerificationToken).filter(
        VerificationToken.token == token
    ).first()

    if not verification:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid verification token"
        )

    # Check if expired
    if verification.expires_at < datetime.utcnow():
        # Delete expired token
        db.delete(verification)
        db.commit()
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Verification token has expired. Please register again."
        )

    # Get the user
    user = db.query(User).filter(User.id == verification.user_id).first()
    if not user:
        db.delete(verification)
        db.commit()
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="User not found"
        )

    # Mark email as verified
    user.email_verified = True
    db.delete(verification)  # Token is single-use
    db.commit()

    # Create token and set cookie - log them in
    access_token = create_access_token({"sub": str(user.id)})
    response.set_cookie(
        key="access_token",
        value=access_token,
        httponly=False,
        max_age=30 * 24 * 60 * 60,
        samesite="lax",
        path="/"
    )

    return {
        "message": "Email verified successfully! You are now logged in.",
        "access_token": access_token,
        "token_type": "bearer"
    }


@router.post("/resend-verification")
def resend_verification(request: Request, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    """Resend verification email to current user"""
    if current_user.email_verified:
        return {"message": "Email already verified"}

    if not current_user.email:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No email address on file"
        )

    # Delete any existing tokens
    db.query(VerificationToken).filter(
        VerificationToken.user_id == current_user.id
    ).delete()

    # Generate new token
    token = secrets.token_urlsafe(32)
    expires_at = datetime.utcnow() + timedelta(hours=24)

    verification = VerificationToken(
        user_id=current_user.id,
        token=token,
        expires_at=expires_at
    )
    db.add(verification)
    db.commit()

    # Build verification URL
    base_url = str(request.base_url).rstrip('/')
    verify_url = f"{base_url}/verify/{token}"

    # Send email
    email_service = EmailService(db)
    success, msg = email_service.send_verification_email(
        to_email=current_user.email,
        username=current_user.username,
        verify_url=verify_url
    )

    if not success:
        logger.error(f"Failed to send verification email to {current_user.email}: {msg}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to send verification email. Please try again later."
        )

    return {"message": "Verification email sent"}


# ============== API Key Management ==============

@router.get("/api-keys", response_model=List[APIKeyListItem])
def list_api_keys(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """List all API keys for the current user"""
    keys = db.query(APIKey).filter(APIKey.user_id == current_user.id).all()
    return [
        APIKeyListItem(
            id=k.id,
            name=k.name,
            key_preview=f"sk-...{k.key[-4:]}",
            created_at=k.created_at,
            last_used_at=k.last_used_at,
            is_active=k.is_active
        )
        for k in keys
    ]


@router.post("/api-keys", response_model=APIKeyResponse)
def create_api_key(
    key_data: APIKeyCreate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Create a new API key for the current user"""
    # Generate a secure random key
    raw_key = secrets.token_hex(32)
    api_key = f"sk-{raw_key}"

    new_key = APIKey(
        user_id=current_user.id,
        key=api_key,
        name=key_data.name or "Default"
    )
    db.add(new_key)
    db.commit()
    db.refresh(new_key)
    from app.services import record_store
    record_store.mirror_apikey_blocking(db, current_user, new_key)

    return APIKeyResponse(
        id=new_key.id,
        name=new_key.name,
        key=api_key,  # Only returned once on creation
        created_at=new_key.created_at,
        last_used_at=new_key.last_used_at,
        is_active=new_key.is_active
    )


@router.get("/api-keys/{key_id}")
def get_api_key(
    key_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Get the full API key (for copying)"""
    api_key = db.query(APIKey).filter(
        APIKey.id == key_id,
        APIKey.user_id == current_user.id
    ).first()

    if not api_key:
        raise HTTPException(status_code=404, detail="API key not found")

    return {"key": api_key.key}


@router.delete("/api-keys/{key_id}")
def delete_api_key(
    key_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Delete an API key"""
    api_key = db.query(APIKey).filter(
        APIKey.id == key_id,
        APIKey.user_id == current_user.id
    ).first()

    if not api_key:
        raise HTTPException(status_code=404, detail="API key not found")

    db.delete(api_key)
    db.commit()
    from app.services import record_store
    record_store.delete_apikey_blocking(db, current_user, key_id)
    return {"message": "API key deleted"}


@router.put("/api-keys/{key_id}/toggle")
def toggle_api_key(
    key_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Enable or disable an API key"""
    api_key = db.query(APIKey).filter(
        APIKey.id == key_id,
        APIKey.user_id == current_user.id
    ).first()

    if not api_key:
        raise HTTPException(status_code=404, detail="API key not found")

    api_key.is_active = not api_key.is_active
    db.commit()
    from app.services import record_store
    record_store.mirror_apikey_blocking(db, current_user, api_key)
    return {"message": "API key toggled", "is_active": api_key.is_active}


# ============== User Settings ==============

@router.post("/timezone")
def set_user_timezone(
    data: dict,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Store the user's UTC offset (minutes east of UTC, from the browser) so natural-language
    reminders are parsed and displayed in their local time, not UTC."""
    import re
    from app.models import UserSetting

    def _save(key: str, value: str):
        s = db.query(UserSetting).filter(
            UserSetting.user_id == current_user.id, UserSetting.key == key
        ).first()
        if s:
            s.value = value
        else:
            db.add(UserSetting(user_id=current_user.id, key=key, value=value))

    try:
        offset = max(-840, min(840, int(data.get("offset_minutes"))))  # clamp to ±14h
    except (TypeError, ValueError):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="offset_minutes (int) required")
    _save("tz_offset_minutes", str(offset))

    # The IANA zone name (e.g. "Asia/Bangkok") is preferred — DST-aware. Sanity-check length/charset.
    tz_name = (data.get("tz_name") or "").strip()
    if tz_name and len(tz_name) <= 64 and re.match(r'^[A-Za-z0-9_+\-/]+$', tz_name):
        _save("tz_name", tz_name)

    db.commit()
    return {"ok": True, "offset_minutes": offset, "tz_name": tz_name}


@router.get("/settings", response_model=UserSettingsResponse)
def get_user_settings(current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Get current user's settings including custom AI service configuration"""
    import json
    from app.models import UserSetting

    avatar_url = f"/api/auth/avatar/{current_user.username}" if current_user.avatar else None

    # Get mail account settings
    mail_accounts = []
    mail_setting = db.query(UserSetting).filter(
        UserSetting.user_id == current_user.id,
        UserSetting.key == "mail_accounts"
    ).first()
    if mail_setting and mail_setting.value:
        try:
            accounts = json.loads(mail_setting.value)
            # Mask passwords
            mail_accounts = [
                {**acc, 'password': '********' if acc.get('password') else ''}
                for acc in accounts
            ]
        except json.JSONDecodeError:
            pass

    # Nitter feeds (newline-separated RSS URLs) for the Telegram post-card poller
    nitter_setting = db.query(UserSetting).filter(
        UserSetting.user_id == current_user.id,
        UserSetting.key == "nitter_feeds"
    ).first()
    nitter_feeds = nitter_setting.value if nitter_setting and nitter_setting.value else ""

    return UserSettingsResponse(
        notification_email=current_user.notification_email,
        avatar=avatar_url,
        theme=(getattr(current_user, "theme", None) or "cyberpunk"),
        news_sources=current_user.news_sources or "",
        # Mail settings
        mail_accounts=mail_accounts,
        # Telegram settings
        telegram_enabled=current_user.telegram_enabled if hasattr(current_user, 'telegram_enabled') else False,
        telegram_chat_id=current_user.telegram_chat_id if hasattr(current_user, 'telegram_chat_id') else None,
        telegram_notifications=current_user.telegram_notifications if hasattr(current_user, 'telegram_notifications') else "",
        telegram_pending_key=current_user.telegram_key if hasattr(current_user, 'telegram_key') else None,
        telegram_key_expires_at=current_user.telegram_key_expires_at if hasattr(current_user, 'telegram_key_expires_at') else None,
        # Pleroma settings
        pleroma_enabled=current_user.pleroma_enabled if hasattr(current_user, 'pleroma_enabled') else False,
        pleroma_instance_url=current_user.pleroma_instance_url if hasattr(current_user, 'pleroma_instance_url') else None,
        pleroma_has_access_token=bool(current_user.pleroma_access_token) if hasattr(current_user, 'pleroma_access_token') else False,
        # Nostr settings (key linked via /api/nostr/connect; never returned, only presence)
        nostr_enabled=current_user.nostr_enabled if hasattr(current_user, 'nostr_enabled') else False,
        nostr_npub=current_user.nostr_npub if hasattr(current_user, 'nostr_npub') else None,
        nostr_has_key=bool(current_user.nostr_nsec) if hasattr(current_user, 'nostr_nsec') else False,
        nostr_relays=current_user.nostr_relays if hasattr(current_user, 'nostr_relays') else None,
        nostr_media_service=current_user.nostr_media_service if hasattr(current_user, 'nostr_media_service') else None,
        nostr_media_endpoint=current_user.nostr_media_endpoint if hasattr(current_user, 'nostr_media_endpoint') else None,
        social_notif_enabled=current_user.social_notif_enabled if hasattr(current_user, 'social_notif_enabled') else False,
        fedi_bridge_enabled=current_user.fedi_bridge_enabled if hasattr(current_user, 'fedi_bridge_enabled') else False,
        fedi_crosspost_enabled=current_user.fedi_crosspost_enabled if hasattr(current_user, 'fedi_crosspost_enabled') else False,
        nitter_feeds=nitter_feeds,
    )


@router.put("/settings")
def update_user_settings(
    settings: UserSettingsUpdate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Update current user's settings including custom AI service configuration"""
    # Update notification email if provided
    if settings.notification_email is not None:
        notification_email = settings.notification_email.strip()
        if notification_email and "@" not in notification_email:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid email address"
            )
        current_user.notification_email = notification_email if notification_email else None

    # News sources for the on-demand `news` command (the scheduled daily-digest feature was removed).
    if settings.news_sources is not None:
        current_user.news_sources = settings.news_sources

    # Client UI theme (mirrored to Nostr via users_store CONFIG_FIELDS). Validate against the
    # allowlist so an unknown slug can't be persisted; blank/unknown falls back to the default.
    if settings.theme is not None:
        from app.schemas import CLIENT_THEMES
        t = settings.theme.strip()
        current_user.theme = t if t in CLIENT_THEMES else "cyberpunk"


    # Update Calendar & Contacts settings (stored in UserSetting table)
    import json
    from app.models import UserSetting

    def save_user_setting(key: str, value: str):
        setting = db.query(UserSetting).filter(
            UserSetting.user_id == current_user.id,
            UserSetting.key == key
        ).first()
        if setting:
            setting.value = value
        else:
            db.add(UserSetting(user_id=current_user.id, key=key, value=value))

    # Save Nitter feeds (newline-separated RSS URLs) for the Telegram post-card poller
    if settings.nitter_feeds is not None:
        save_user_setting("nitter_feeds", settings.nitter_feeds.strip())

    # Save mail account settings
    if settings.mail_accounts is not None:
        # Get existing accounts to preserve passwords if not changed
        existing_setting = db.query(UserSetting).filter(
            UserSetting.user_id == current_user.id,
            UserSetting.key == "mail_accounts"
        ).first()
        existing_accounts = []
        if existing_setting and existing_setting.value:
            try:
                existing_accounts = json.loads(existing_setting.value)
            except json.JSONDecodeError:
                pass

        # Merge new accounts with existing passwords
        new_accounts = []
        for acc in settings.mail_accounts:
            new_acc = {
                'email': acc.get('email', ''),
                'imap_server': acc.get('imap_server', ''),
                'imap_port': acc.get('imap_port', 993),
                'smtp_server': acc.get('smtp_server', ''),
                'smtp_port': acc.get('smtp_port', 587),
                'password': acc.get('password') or ''
            }
            # If password is null/empty, try to keep existing password
            if not new_acc['password']:
                for existing in existing_accounts:
                    if existing.get('email') == new_acc['email']:
                        new_acc['password'] = existing.get('password', '')
                        break
            new_accounts.append(new_acc)

        save_user_setting("mail_accounts", json.dumps(new_accounts))

    # Save Telegram settings — only notifications; linking/unlinking goes through /api/telegram/*
    if settings.telegram_notifications is not None:
        current_user.telegram_notifications = settings.telegram_notifications


    # Nostr settings (the secret key is linked via /api/nostr/connect; here we let the user
    # toggle/disable and tweak relays + media host without re-pasting the key).
    if settings.nostr_enabled is not None:
        current_user.nostr_enabled = settings.nostr_enabled
    if settings.nostr_relays is not None:
        current_user.nostr_relays = settings.nostr_relays.strip() or None
    if settings.nostr_media_service is not None:
        current_user.nostr_media_service = settings.nostr_media_service.strip() or None
    if settings.nostr_media_endpoint is not None:
        current_user.nostr_media_endpoint = settings.nostr_media_endpoint.strip() or None


    # Relay social notifications to Telegram (master per-user toggle)
    if settings.social_notif_enabled is not None:
        current_user.social_notif_enabled = settings.social_notif_enabled


    # Nostr ↔ Fediverse bridge: opt in to personal DMs + notifications on the Nostr side
    if settings.fedi_bridge_enabled is not None:
        current_user.fedi_bridge_enabled = settings.fedi_bridge_enabled

    # Cross-post my top-level Nostr notes to my linked Pleroma account
    if settings.fedi_crosspost_enabled is not None:
        current_user.fedi_crosspost_enabled = settings.fedi_crosspost_enabled

    try:
        # Flush changes to database before commit
        db.flush()
        
        # Commit the transaction - ensure this succeeds
        db.commit()
        logger.info(f"[Auth] Successfully saved user settings for user {current_user.username}")
        # Mirror per-user config to the relay (the authoritative datastore). Sync handler → drive the
        # coroutine with asyncio.run.
        try:
            from app.services import users_store
            import asyncio as _aio
            async def _mirror():
                await users_store.sync_user(db, current_user)        # account cols → event
                await users_store.sync_user_kv(db, current_user)     # mail/nitter/caldav kv → event
            _aio.run(_mirror())
        except Exception as e:
            logger.warning(f"[Auth] account sync to relay after settings save failed: {e}")
    except IntegrityError as e:
        # Handle constraint violations (e.g., unique constraint, foreign key)
        db.rollback()
        logger.error(f"[Auth] Integrity error saving user settings for {current_user.username}: {e}", exc_info=True)
        raise HTTPException(status_code=400, detail=f"Invalid data: {str(e)}")
    except OperationalError as e:
        # Handle database connection/operational errors
        db.rollback()
        logger.error(f"[Auth] Database operational error saving user settings for {current_user.username}: {e}", exc_info=True)
        raise HTTPException(status_code=503, detail="Database temporarily unavailable. Please try again.")
    except SQLAlchemyError as e:
        # Handle other database errors
        db.rollback()
        logger.error(f"[Auth] Database error saving user settings for {current_user.username}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Database error: {str(e)}")
    except Exception as e:
        # Handle unexpected errors
        db.rollback()
        logger.error(f"[Auth] Unexpected error saving user settings for {current_user.username}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Unexpected error: {str(e)}")

    return {"message": "Settings updated"}


@router.post("/bridge-access")
async def bridge_access(
    data: BridgeAccessRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """1-click Bridge Access: auto-create a fediverse account on the configured home instance, copy
    this user's Nostr profile, register their NIP-05, and enable both bridge toggles (or disable)."""
    from app.services import fedi_bridge_access
    try:
        result = await (fedi_bridge_access.enable(db, current_user) if data.enable
                        else fedi_bridge_access.disable(db, current_user))
    except Exception as e:
        logger.error(f"[Auth] bridge-access failed for {current_user.username}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Bridge access error: {e}")
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("error") or "Bridge access failed")
    return result


@router.post("/avatar")
async def upload_avatar(
    request: StarletteRequest,
    file: UploadFile = File(...),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Upload user avatar image. Proxies to storage server if configured."""
    # Validate file type
    allowed_types = {"image/jpeg", "image/png", "image/gif", "image/webp"}
    if file.content_type not in allowed_types:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid file type. Allowed: JPEG, PNG, GIF, WebP"
        )

    # Read file content (no size limit)
    content = await file.read()

    # Check if storage server is configured - proxy request if so
    from app.services import settings_store
    storage_server_url = settings_store.get("storage_server_url", "")
    if storage_server_url:
        logger.debug(f"Proxying avatar upload to storage server: {storage_server_url}")
        # Proxy to storage server - use the storage endpoint, not auth endpoint
        from app.services.storage_proxy import proxy_storage_request
        files = {
            "file": (file.filename or "avatar", content, file.content_type)
        }
        # Add username as form data (required by storage endpoint)
        # Note: json_body is used for form data when files are present
        form_data = {"username": current_user.username}
        
        try:
            result = await proxy_storage_request(
                db=db,
                request=request,
                endpoint="/api/storage/save-avatar",
                method="POST",
                files=files,
                json_body=form_data  # This becomes form data when files are present
            )
            
            logger.debug(f"Proxy result type: {type(result)}, value: {result}")
            
            # Handle response - proxy_storage_request returns dict for JSON responses
            if isinstance(result, dict):
                filename = result.get("filename", "avatar.png")
            else:
                # If it's a Response object, try to parse JSON
                try:
                    import json
                    result_json = await result.json() if hasattr(result, 'json') else {}
                    filename = result_json.get("filename", "avatar.png")
                except Exception:
                    # Fallback: use default filename
                    logger.warning("Could not parse avatar upload response, using default filename")
                    filename = "avatar.png"
            
            # Update user record with avatar filename
            current_user.avatar = filename
            db.commit()
            try:
                from app.services import users_store
                await users_store.sync_user(db, current_user)   # avatar → relay (fresh-node rebuild)
            except Exception as e:
                logger.warning("[auth] avatar sync to relay failed: %s", e)

            # Add cache busting to avatar URL
            avatar_url = f"/api/auth/avatar/{current_user.username}?t={int(time.time())}"
            return {
                "message": "Avatar uploaded",
                "avatar": avatar_url,
                "filename": filename
            }
        except HTTPException as e:
            # NO FALLBACK - if proxy fails, request fails
            logger.error(f"Proxy failed ({e.status_code}): {e.detail}")
            raise
        except Exception as e:
            # NO FALLBACK - if proxy fails, request fails
            logger.error(f"Error proxying avatar upload: {e}", exc_info=True)
            raise HTTPException(status_code=500, detail=f"Failed to proxy avatar upload: {str(e)}")
    
    # If storage server is not configured, fail explicitly
    raise HTTPException(status_code=500, detail="Storage server not configured. Cannot upload avatar.")


@router.delete("/avatar")
def delete_avatar(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Delete user avatar"""
    if current_user.avatar:
        storage = StorageService(db)
        avatar_path = storage.get_avatar_path(current_user.username)
        if avatar_path and avatar_path.exists():
            avatar_path.unlink()

        current_user.avatar = None
        db.commit()
        try:
            from app.services import users_store
            users_store.sync_user_blocking(db, current_user)   # avatar removal → relay
        except Exception as e:
            logger.warning("[auth] avatar-delete sync to relay failed: %s", e)

    return {"message": "Avatar deleted"}


@router.get("/avatar/{username}")
async def get_avatar(
    username: str,
    request: StarletteRequest,
    db: Session = Depends(get_db)
):
    """Get user avatar image. Proxies to storage server if configured."""
    from fastapi.responses import FileResponse
    from app.services.storage_service import StorageService
    
    # Check if storage server is configured - proxy request if so
    from app.services import settings_store
    storage_server_url = settings_store.get("storage_server_url", "")
    if storage_server_url:
        # Validate URL has protocol before proxying
        url = storage_server_url.strip()
        if url.startswith(('http://', 'https://')):
            logger.debug(f"Proxying avatar request for {username} to storage server: {url}")
            # Try to get avatar from storage server via auth API (same endpoint)
            # This will proxy to the storage server's /api/auth/avatar endpoint
            try:
                from app.services.storage_proxy import proxy_storage_request
                # Proxy to the storage server's avatar endpoint
                result = await proxy_storage_request(
                    db=db,
                    request=request,
                    endpoint=f"/api/auth/avatar/{username}",
                    method="GET",
                    stream=True
                )
                logger.debug(f"Successfully proxied avatar for {username} from storage server")
                return result
            except HTTPException as e:
                # If storage server is unavailable (503/504), fallback to local storage
                if e.status_code in (503, 504):
                    logger.warning(f"Storage server unavailable ({e.status_code}), falling back to local storage for avatar: {username}")
                    # Fall through to local file serving below
                else:
                    logger.error(f"HTTPException proxying avatar for {username}: {e.status_code} - {e.detail}")
                    raise
            except Exception as e:
                # Connection errors, timeouts, etc. - fallback to local storage
                logger.warning(f"Error proxying avatar for {username}, falling back to local storage: {e}")
                # Fall through to local file serving below
        else:
            # Invalid URL - fail explicitly
            raise HTTPException(status_code=500, detail="Invalid storage_server_url configuration")
    
    # Local file serving (storage server node or when storage_server_url is not configured)
    storage = StorageService(db)
    
    # Try to get avatar path - this searches for avatar.* files in user directory
    avatar_path = storage.get_avatar_path(username)
    
    # If not found via glob, check database for specific filename (if user exists in this DB)
    if not avatar_path or not avatar_path.exists():
        from app.models import User
        user = db.query(User).filter(User.username == username).first()
        if user and user.avatar:
            user_path = storage.get_user_path(username)
            avatar_path = user_path / user.avatar
    
    if not avatar_path or not avatar_path.exists():
        raise HTTPException(status_code=404, detail="Avatar not found")
    
    # Determine media type from file extension
    suffix = avatar_path.suffix.lower()
    media_types = {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".gif": "image/gif",
        ".webp": "image/webp",
    }
    media_type = media_types.get(suffix, "image/png")
    
    return FileResponse(avatar_path, media_type=media_type)


@router.post("/scan-storage")
async def scan_user_storage(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """
    Scan the current user's local storage.
    Invalidates file cache and counts files/directories.
    Also restores EXIF timestamps and generates thumbnails.
    """
    from app.routers.files import get_file_cache
    from app.services.storage_service import get_storage_service
    from app.utils.exif_utils import batch_restore_timestamps
    from app.services.thumbnail_service import generate_thumbnails_for_user
    
    # Invalidate file cache for this user
    cache = get_file_cache(db)
    cache.invalidate(f"{current_user.username}:")
    
    try:
        # Scan local filesystem
        storage = get_storage_service(db)
        user_path = storage.get_user_path(current_user.username)
        
        file_count = 0
        dir_count = 0
        exif_stats = {'restored': 0, 'processed': 0}
        thumbnail_stats = {'successful': 0, 'failed': 0}
        
        if user_path.exists():
            # Restore EXIF timestamps
            logger.info(f"[User Scan] Restoring EXIF timestamps for {current_user.username}")
            exif_stats = batch_restore_timestamps(user_path)
            
            # Generate thumbnails
            logger.info(f"[User Scan] Generating thumbnails for {current_user.username}")
            successful, failed = generate_thumbnails_for_user(user_path)
            thumbnail_stats = {'successful': successful, 'failed': failed}
            
            # Count files
            for item in user_path.rglob('*'):
                try:
                    if item.is_file():
                        file_count += 1
                    elif item.is_dir():
                        dir_count += 1
                except Exception as e:
                    logger.warning(f"Error processing {item}: {e}")
                    continue
        
        return {
            "message": f"Storage scan complete for {current_user.username}",
            "files": file_count,
            "directories": dir_count,
            "exif_restored": exif_stats.get('restored', 0),
            "exif_processed": exif_stats.get('processed', 0),
            "thumbnails_generated": thumbnail_stats.get('successful', 0),
            "thumbnails_failed": thumbnail_stats.get('failed', 0),
            "storage_type": "local"
        }
    except Exception as e:
        logger.error(f"[User Scan] Error scanning storage for {current_user.username}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to scan storage: {str(e)}")


# ============== Calendar Event API ==============

def rrule_to_human(rrule: str) -> str:
    """Convert RRULE string to human-readable format."""
    if not rrule:
        return ""

    # Parse RRULE components
    parts = {}
    for part in rrule.split(';'):
        if '=' in part:
            key, value = part.split('=', 1)
            parts[key.upper()] = value

    freq = parts.get('FREQ', '')
    interval = parts.get('INTERVAL', '1')
    byday = parts.get('BYDAY', '')

    # Day code to name mapping
    day_names = {
        'MO': 'Mon', 'TU': 'Tue', 'WE': 'Wed', 'TH': 'Thu',
        'FR': 'Fri', 'SA': 'Sat', 'SU': 'Sun'
    }

    result = ""
    if freq == 'DAILY':
        result = "daily" if interval == '1' else f"every {interval} days"
    elif freq == 'WEEKLY':
        if byday:
            days = [day_names.get(d.strip(), d) for d in byday.split(',')]
            result = f"weekly {' '.join(days)}"
        else:
            result = "weekly" if interval == '1' else f"every {interval} weeks"
    elif freq == 'MONTHLY':
        result = "monthly" if interval == '1' else f"every {interval} months"
    elif freq == 'YEARLY':
        result = "yearly" if interval == '1' else f"every {interval} years"
    else:
        result = rrule  # Fallback to raw if unknown

    return result



