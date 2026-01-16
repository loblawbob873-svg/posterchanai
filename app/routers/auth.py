import secrets
import logging
import asyncio
import time
from pathlib import Path
from typing import List
from datetime import datetime, timedelta
from fastapi import APIRouter, Depends, HTTPException, status, Response, UploadFile, File, Request
from starlette.requests import Request as StarletteRequest
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session
from app.database import get_db

logger = logging.getLogger(__name__)
from app.models import User, Setting, APIKey, VerificationToken
from app.schemas import (
    UserLogin, UserResponse, Token, UserRegister, APIKeyCreate, APIKeyResponse, APIKeyListItem,
    UserSettingsUpdate, UserSettingsResponse, TestConnectionRequest, TestConnectionResponse
)
from app.auth import verify_password, create_access_token, get_current_user, get_password_hash
from app.services.email_service import EmailService
from app.services.storage_service import StorageService

router = APIRouter(prefix="/api/auth", tags=["auth"])


@router.post("/login", response_model=Token)
def login(user_data: UserLogin, response: Response, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.username == user_data.username).first()
    if not user or not verify_password(user_data.password, user.password_hash):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid username or password"
        )

    token = create_access_token({"sub": str(user.id)})

    # Set cookie for browser-based auth
    # Note: httponly=False required for WebSocket auth (JS reads token for ws:// connection)
    # secure=True in production to prevent cookie transmission over HTTP
    import os
    is_production = os.getenv("ENVIRONMENT", "").lower() == "production"
    response.set_cookie(
        key="access_token",
        value=token,
        httponly=False,  # Required: JS reads token for WebSocket auth handshake
        secure=is_production,  # HTTPS only in production
        max_age=30 * 24 * 60 * 60,  # 30 days
        samesite="lax",
        path="/"
    )

    return {"access_token": token, "token_type": "bearer"}


@router.post("/logout")
def logout(response: Response):
    response.delete_cookie("access_token")
    return {"message": "Logged out"}


@router.get("/me", response_model=UserResponse)
def get_me(current_user: User = Depends(get_current_user)):
    return current_user


@router.get("/registration-enabled")
def check_registration_enabled(db: Session = Depends(get_db)):
    """Check if user registration is enabled"""
    setting = db.query(Setting).filter(Setting.key == "allow_registration").first()
    enabled = setting.value.lower() == "true" if setting else False
    return {"enabled": enabled}


@router.post("/register")
def register(user_data: UserRegister, request: Request, response: Response, db: Session = Depends(get_db)):
    """Register a new user (if registration is enabled)"""
    # Check if registration is enabled
    setting = db.query(Setting).filter(Setting.key == "allow_registration").first()
    if not setting or setting.value.lower() != "true":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="User registration is disabled"
        )

    # Check if username already exists
    if db.query(User).filter(User.username == user_data.username).first():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Username already registered"
        )

    # Check if email already exists (if provided)
    if user_data.email:
        if db.query(User).filter(User.email == user_data.email).first():
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Email already registered"
            )

    # Check if email verification is required
    email_service = EmailService(db)
    require_verification = email_service.smtp_enabled and user_data.email

    # Create new user
    user = User(
        username=user_data.username,
        email=user_data.email if user_data.email else None,
        password_hash=get_password_hash(user_data.password),
        is_admin=False,
        email_verified=not require_verification  # True if no verification needed
    )
    db.add(user)
    db.commit()
    db.refresh(user)

    # If email verification required, send verification email
    if require_verification:
        # Generate verification token
        token = secrets.token_urlsafe(32)
        expires_at = datetime.utcnow() + timedelta(hours=24)

        verification = VerificationToken(
            user_id=user.id,
            token=token,
            expires_at=expires_at
        )
        db.add(verification)
        db.commit()

        # Build verification URL
        base_url = str(request.base_url).rstrip('/')
        verify_url = f"{base_url}/verify/{token}"

        # Send email
        success, msg = email_service.send_verification_email(
            to_email=user.email,
            username=user.username,
            verify_url=verify_url
        )

        if not success:
            # Log but don't fail registration
            logger.error(f"Failed to send verification email: {msg}")

        return {
            "message": "Registration successful! Please check your email to verify your account.",
            "requires_verification": True
        }

    # No verification needed - create token and set cookie
    token = create_access_token({"sub": str(user.id)})
    response.set_cookie(
        key="access_token",
        value=token,
        httponly=False,
        max_age=30 * 24 * 60 * 60,
        samesite="lax",
        path="/"
    )

    return {"access_token": token, "token_type": "bearer"}


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
    return {"message": "API key toggled", "is_active": api_key.is_active}


# ============== User Settings ==============

@router.get("/settings", response_model=UserSettingsResponse)
def get_user_settings(current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Get current user's settings including custom AI service configuration"""
    import json
    from app.models import UserSetting

    avatar_url = f"/api/auth/avatar/{current_user.username}" if current_user.avatar else None

    # Get calendar/contacts settings from UserSetting table
    schedule_enabled = False
    caldav_calendars = []
    carddav_url = None
    carddav_username = None
    carddav_has_password = False

    schedule_setting = db.query(UserSetting).filter(
        UserSetting.user_id == current_user.id,
        UserSetting.key == "schedule_enabled"
    ).first()
    if schedule_setting:
        schedule_enabled = schedule_setting.value.lower() == "true"

    calendars_setting = db.query(UserSetting).filter(
        UserSetting.user_id == current_user.id,
        UserSetting.key == "caldav_calendars"
    ).first()
    if calendars_setting and calendars_setting.value:
        try:
            calendars = json.loads(calendars_setting.value)
            # Mask passwords
            caldav_calendars = [
                {**c, 'password': '********' if c.get('password') else ''}
                for c in calendars
            ]
        except json.JSONDecodeError:
            pass

    carddav_setting = db.query(UserSetting).filter(
        UserSetting.user_id == current_user.id,
        UserSetting.key == "carddav_config"
    ).first()
    if carddav_setting and carddav_setting.value:
        try:
            carddav = json.loads(carddav_setting.value)
            carddav_url = carddav.get('url')
            carddav_username = carddav.get('username')
            carddav_has_password = bool(carddav.get('password'))
        except json.JSONDecodeError:
            pass

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

    # Get WebDAV music settings
    webdav_music_url = None
    webdav_music_username = None
    webdav_music_has_password = False
    webdav_music_setting = db.query(UserSetting).filter(
        UserSetting.user_id == current_user.id,
        UserSetting.key == "webdav_music_config"
    ).first()
    if webdav_music_setting and webdav_music_setting.value:
        try:
            webdav_config = json.loads(webdav_music_setting.value)
            webdav_music_url = webdav_config.get('url')
            webdav_music_username = webdav_config.get('username')
            webdav_music_has_password = bool(webdav_config.get('password'))
        except json.JSONDecodeError:
            pass

    return UserSettingsResponse(
        notification_email=current_user.notification_email,
        avatar=avatar_url,
        # Custom LLM settings
        custom_ai_enabled=current_user.custom_ai_enabled or False,
        custom_ai_type=current_user.custom_ai_type,
        custom_ai_url=current_user.custom_ai_url,
        custom_ai_model=current_user.custom_ai_model,
        custom_ai_has_api_key=bool(current_user.custom_ai_api_key),
        # Custom Image Generation settings
        custom_image_enabled=current_user.custom_image_enabled or False,
        custom_image_url=current_user.custom_image_url,
        # Scheduled news settings
        news_schedule_enabled=current_user.news_schedule_enabled or False,
        news_schedule_time=current_user.news_schedule_time or "12:00",
        news_sources=current_user.news_sources or "",
        # Native RSS settings
        rss_enabled=current_user.rss_enabled if hasattr(current_user, 'rss_enabled') else False,
        rss_skip_summarization=current_user.rss_skip_summarization if hasattr(current_user, 'rss_skip_summarization') else False,
        # Calendar & Contacts settings
        schedule_enabled=schedule_enabled,
        caldav_calendars=caldav_calendars,
        carddav_url=carddav_url,
        carddav_username=carddav_username,
        carddav_has_password=carddav_has_password,
        # Mail settings
        mail_accounts=mail_accounts,
        # Music settings (WebDAV)
        webdav_music_url=webdav_music_url,
        webdav_music_username=webdav_music_username,
        webdav_music_has_password=webdav_music_has_password
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

    # Update custom LLM settings
    if settings.custom_ai_enabled is not None:
        current_user.custom_ai_enabled = settings.custom_ai_enabled
    if settings.custom_ai_type is not None:
        current_user.custom_ai_type = settings.custom_ai_type
    if settings.custom_ai_url is not None:
        current_user.custom_ai_url = settings.custom_ai_url.strip() if settings.custom_ai_url else None
    if settings.custom_ai_model is not None:
        current_user.custom_ai_model = settings.custom_ai_model.strip() if settings.custom_ai_model else None
    if settings.custom_ai_api_key is not None:
        # Allow clearing the API key with empty string
        current_user.custom_ai_api_key = settings.custom_ai_api_key if settings.custom_ai_api_key else None

    # Update custom Image Generation settings
    if settings.custom_image_enabled is not None:
        current_user.custom_image_enabled = settings.custom_image_enabled
    if settings.custom_image_url is not None:
        current_user.custom_image_url = settings.custom_image_url.strip() if settings.custom_image_url else None

    # Update scheduled news settings
    if settings.news_schedule_enabled is not None:
        current_user.news_schedule_enabled = settings.news_schedule_enabled
    if settings.news_schedule_time is not None:
        # Validate time format (HH:MM)
        import re
        time_str = settings.news_schedule_time.strip()
        if time_str and re.match(r'^([01]?[0-9]|2[0-3]):[0-5][0-9]$', time_str):
            current_user.news_schedule_time = time_str
        elif time_str:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid time format. Use HH:MM (e.g., 12:00)"
            )
    if settings.news_sources is not None:
        current_user.news_sources = settings.news_sources

    # Update Native RSS settings
    if settings.rss_enabled is not None:
        current_user.rss_enabled = settings.rss_enabled
    if settings.rss_skip_summarization is not None:
        current_user.rss_skip_summarization = settings.rss_skip_summarization

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

    if settings.schedule_enabled is not None:
        save_user_setting("schedule_enabled", "true" if settings.schedule_enabled else "false")

    if settings.caldav_calendars is not None:
        # Get existing calendars to preserve passwords if not changed
        existing_setting = db.query(UserSetting).filter(
            UserSetting.user_id == current_user.id,
            UserSetting.key == "caldav_calendars"
        ).first()
        existing_calendars = []
        if existing_setting and existing_setting.value:
            try:
                existing_calendars = json.loads(existing_setting.value)
            except json.JSONDecodeError:
                pass

        # Merge new calendars with existing passwords
        new_calendars = []
        for cal in settings.caldav_calendars:
            new_cal = {
                'name': cal.get('name', ''),
                'url': cal.get('url', ''),
                'username': cal.get('username', ''),
                'password': cal.get('password') or ''
            }
            # If password is null (meaning keep existing), try to find it
            if cal.get('password') is None:
                for existing in existing_calendars:
                    if existing.get('url') == new_cal['url']:
                        new_cal['password'] = existing.get('password', '')
                        break
            new_calendars.append(new_cal)

        save_user_setting("caldav_calendars", json.dumps(new_calendars))

    if settings.carddav_url is not None or settings.carddav_username is not None or settings.carddav_password is not None:
        # Get existing config
        existing_setting = db.query(UserSetting).filter(
            UserSetting.user_id == current_user.id,
            UserSetting.key == "carddav_config"
        ).first()
        existing_config = {}
        if existing_setting and existing_setting.value:
            try:
                existing_config = json.loads(existing_setting.value)
            except json.JSONDecodeError:
                pass

        # Update only provided fields
        if settings.carddav_url is not None:
            existing_config['url'] = settings.carddav_url
        if settings.carddav_username is not None:
            existing_config['username'] = settings.carddav_username
        if settings.carddav_password is not None:
            existing_config['password'] = settings.carddav_password

        save_user_setting("carddav_config", json.dumps(existing_config))

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

    # Save WebDAV music settings
    logger.info(f"WebDAV Music settings received: url={settings.webdav_music_url}, username={settings.webdav_music_username}, has_password={settings.webdav_music_password is not None}")
    if settings.webdav_music_url is not None or settings.webdav_music_username is not None or settings.webdav_music_password is not None:
        from app.services.webdav_music_service import save_user_webdav_config
        logger.info(f"Saving WebDAV config for user {current_user.id}")
        save_user_webdav_config(
            current_user.id, db,
            url=settings.webdav_music_url or '',
            username=settings.webdav_music_username or '',
            password=settings.webdav_music_password
        )
        logger.info("WebDAV config saved")

    db.commit()

    return {"message": "Settings updated"}


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
    storage_server_url = db.query(Setting).filter(Setting.key == "storage_server_url").first()
    if storage_server_url and storage_server_url.value:
        logger.debug(f"Proxying avatar upload to storage server: {storage_server_url.value}")
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
                except:
                    # Fallback: use default filename
                    logger.warning("Could not parse avatar upload response, using default filename")
                    filename = "avatar.png"
            
            # Update user record with avatar filename
            current_user.avatar = filename
            db.commit()
            
            # Add cache busting to avatar URL
            avatar_url = f"/api/auth/avatar/{current_user.username}?t={int(time.time())}"
            return {
                "message": "Avatar uploaded",
                "avatar": avatar_url,
                "filename": filename
            }
        except HTTPException as e:
            # If proxy fails, fall back to local save (might be on storage server itself)
            logger.warning(f"Proxy failed ({e.status_code}): {e.detail}, falling back to local save")
            # Fall through to local save
        except Exception as e:
            # If proxy fails, fall back to local save (might be on storage server itself)
            logger.warning(f"Error proxying avatar upload: {e}, falling back to local save")
            # Fall through to local save
    
    # Local file saving (storage server node or fallback from proxy failure)
    logger.debug(f"Saving avatar locally for user: {current_user.username}")
    try:
        # Get file extension
        ext_map = {"image/jpeg": ".jpg", "image/png": ".png", "image/gif": ".gif", "image/webp": ".webp"}
        ext = ext_map.get(file.content_type, ".png")
        logger.debug(f"File extension: {ext}, content_type: {file.content_type}, size: {len(content)} bytes")

        # Save avatar (run in thread pool since save_avatar is blocking I/O)
        storage = StorageService(db)
        
        def _save_avatar_sync():
            try:
                return storage.save_avatar(current_user.username, content, ext)
            except Exception as e:
                logger.error(f"Error in _save_avatar_sync: {e}", exc_info=True)
                raise
        
        filename = await asyncio.to_thread(_save_avatar_sync)
        logger.debug(f"Avatar saved with filename: {filename}")

        # Update user record
        current_user.avatar = filename
        db.commit()
        logger.debug(f"User record updated with avatar: {filename}")

        # Add cache busting to avatar URL
        avatar_url = f"/api/auth/avatar/{current_user.username}?t={int(time.time())}"
        return {
            "message": "Avatar uploaded",
            "avatar": avatar_url,
            "filename": filename
        }
    except Exception as e:
        logger.error(f"Error saving avatar locally: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to save avatar: {str(e)}")


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

    return {"message": "Avatar deleted"}


@router.get("/storage-addresses")
async def get_storage_addresses(
    request: StarletteRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Get WebDAV/CalDAV/CardDAV server addresses for current user."""
    # Get server settings
    webdav_enabled = db.query(Setting).filter(Setting.key == "webdav_enabled").first()
    webdav_port = db.query(Setting).filter(Setting.key == "webdav_port").first()
    webdav_base_url = db.query(Setting).filter(Setting.key == "webdav_base_url").first()
    
    caldav_enabled = db.query(Setting).filter(Setting.key == "caldav_enabled").first()
    caldav_port = db.query(Setting).filter(Setting.key == "caldav_port").first()
    caldav_base_url = db.query(Setting).filter(Setting.key == "caldav_base_url").first()
    
    cardav_enabled = db.query(Setting).filter(Setting.key == "cardav_enabled").first()
    cardav_port = db.query(Setting).filter(Setting.key == "cardav_port").first()
    cardav_base_url = db.query(Setting).filter(Setting.key == "cardav_base_url").first()
    
    logger.debug(f"Storage addresses request for {current_user.username}: "
                 f"webdav_enabled={webdav_enabled.value if webdav_enabled else None}, "
                 f"caldav_enabled={caldav_enabled.value if caldav_enabled else None}, "
                 f"cardav_enabled={cardav_enabled.value if cardav_enabled else None}")
    
    # Helper to build URL - use base_url if set, otherwise use request hostname
    def build_dav_url(base_url_setting, port_setting, default_port, path_suffix):
        if not base_url_setting or not base_url_setting.value or not base_url_setting.value.strip():
            # Use request hostname and scheme (auto-detect)
            # Don't include port - assumes reverse proxy handles routing
            scheme = request.url.scheme
            hostname = request.url.hostname
            # Always omit port in URL (reverse proxy should handle routing)
            base = f"{scheme}://{hostname}"
            return f"{base}{path_suffix}"
        else:
            # Use configured base URL (user can include port if needed for direct access)
            base = base_url_setting.value.rstrip('/')
            return f"{base}{path_suffix}"
    
    webdav_url = ""
    if webdav_enabled and webdav_enabled.value.lower() == "true":
        webdav_url = build_dav_url(webdav_base_url, webdav_port, "8080", f"/{current_user.username}")
        logger.debug(f"Built WebDAV URL: {webdav_url}")
    
    caldav_url = ""
    if caldav_enabled and caldav_enabled.value.lower() == "true":
        caldav_url = build_dav_url(caldav_base_url, caldav_port, "8081", f"/caldav/{current_user.username}/")
        logger.debug(f"Built CalDAV URL: {caldav_url}")
    
    cardav_url = ""
    if cardav_enabled and cardav_enabled.value.lower() == "true":
        cardav_url = build_dav_url(cardav_base_url, cardav_port, "8082", f"/carddav/{current_user.username}/")
        logger.debug(f"Built CardDAV URL: {cardav_url}")
    
    result = {
        "username": current_user.username,
        "webdav_url": webdav_url,
        "caldav_url": caldav_url,
        "cardav_url": cardav_url
    }
    logger.debug(f"Returning storage addresses: {result}")
    return result


@router.get("/avatar/{username}")
async def get_avatar(
    username: str,
    request: StarletteRequest,
    db: Session = Depends(get_db)
):
    """Get user avatar image. Proxies to storage server if configured."""
    # Check if storage server is configured - proxy request if so
    storage_server_url = db.query(Setting).filter(Setting.key == "storage_server_url").first()
    if storage_server_url and storage_server_url.value:
        logger.debug(f"Proxying avatar request for {username} to storage server: {storage_server_url.value}")
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
            logger.warning(f"HTTPException proxying avatar for {username}: {e.status_code} - {e.detail}")
            if e.status_code == 404:
                raise HTTPException(status_code=404, detail="Avatar not found")
            raise
        except Exception as e:
            logger.warning(f"Error proxying avatar get for {username}, falling back to local: {e}", exc_info=True)
            # Fall through to local serving
    else:
        logger.debug(f"Serving avatar for {username} from local storage (no storage_server_url configured)")
    
    # Serve avatar from local storage
    logger.debug(f"Serving avatar for {username} from local storage")
    
    # Local file serving
    storage = StorageService(db)
    avatar_path = storage.get_avatar_path(username)
    
    logger.debug(f"Avatar path for {username}: {avatar_path} (exists: {avatar_path.exists() if avatar_path else False})")

    if not avatar_path or not avatar_path.exists():
        logger.warning(f"Avatar not found for {username} at {avatar_path}")
        raise HTTPException(status_code=404, detail="Avatar not found")

    # Determine content type
    ext = avatar_path.suffix.lower()
    content_types = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".gif": "image/gif", ".webp": "image/webp"}
    content_type = content_types.get(ext, "image/png")
    
    logger.debug(f"Serving avatar file: {avatar_path} (type: {content_type})")

    return FileResponse(avatar_path, media_type=content_type)


# ============== Custom AI Service Testing ==============

@router.post("/test-custom-ai", response_model=TestConnectionResponse)
async def test_custom_ai_connection(
    request: TestConnectionRequest,
    current_user: User = Depends(get_current_user)
):
    """Test connection to a custom AI service (Ollama or OpenAI-compatible)"""
    import httpx

    url = request.url.rstrip('/')
    models = []

    # Use stored API key if requested and available
    api_key = request.api_key
    if request.use_stored_key and current_user.custom_ai_api_key:
        api_key = current_user.custom_ai_api_key

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            if request.api_type == "ollama":
                # Test Ollama API - list models
                response = await client.get(f"{url}/api/tags")
                if response.status_code == 200:
                    data = response.json()
                    models = [m.get("name", m.get("model", "unknown")) for m in data.get("models", [])]
                    return TestConnectionResponse(
                        success=True,
                        message=f"Connected to Ollama. Found {len(models)} model(s).",
                        models=models
                    )
                else:
                    return TestConnectionResponse(
                        success=False,
                        message=f"Ollama returned status {response.status_code}"
                    )
            else:
                # Test OpenAI-compatible API (Open-WebUI, Posterchanai)
                headers = {}
                if api_key:
                    headers["Authorization"] = f"Bearer {api_key}"

                response = await client.get(f"{url}/v1/models", headers=headers)
                if response.status_code == 200:
                    data = response.json()
                    models = [m.get("id", "unknown") for m in data.get("data", [])]
                    return TestConnectionResponse(
                        success=True,
                        message=f"Connected to OpenAI-compatible API. Found {len(models)} model(s).",
                        models=models
                    )
                elif response.status_code == 401:
                    return TestConnectionResponse(
                        success=False,
                        message="Authentication failed. Check your API key."
                    )
                else:
                    return TestConnectionResponse(
                        success=False,
                        message=f"API returned status {response.status_code}"
                    )

    except httpx.ConnectError:
        return TestConnectionResponse(
            success=False,
            message=f"Could not connect to {url}. Check the URL and ensure the service is running."
        )
    except httpx.TimeoutException:
        return TestConnectionResponse(
            success=False,
            message="Connection timed out. The service may be slow or unreachable."
        )
    except Exception as e:
        return TestConnectionResponse(
            success=False,
            message=f"Error: {str(e)}"
        )


@router.post("/test-custom-image", response_model=TestConnectionResponse)
async def test_custom_image_connection(
    url: str,
    current_user: User = Depends(get_current_user)
):
    """Test connection to a custom ComfyUI instance"""
    import httpx

    url = url.rstrip('/')

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            # Test ComfyUI API - get system stats
            response = await client.get(f"{url}/system_stats")
            if response.status_code == 200:
                return TestConnectionResponse(
                    success=True,
                    message="Connected to ComfyUI successfully."
                )
            else:
                # Try alternative endpoint
                response = await client.get(f"{url}/prompt")
                if response.status_code in [200, 400]:  # 400 means it's running but needs a prompt
                    return TestConnectionResponse(
                        success=True,
                        message="Connected to ComfyUI successfully."
                    )
                return TestConnectionResponse(
                    success=False,
                    message=f"ComfyUI returned status {response.status_code}"
                )

    except httpx.ConnectError:
        return TestConnectionResponse(
            success=False,
            message=f"Could not connect to {url}. Check the URL and ensure ComfyUI is running."
        )
    except httpx.TimeoutException:
        return TestConnectionResponse(
            success=False,
            message="Connection timed out. ComfyUI may be slow or unreachable."
        )
    except Exception as e:
        return TestConnectionResponse(
            success=False,
            message=f"Error: {str(e)}"
        )


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


@router.get("/calendar/event/{uid}")
async def get_calendar_event(
    uid: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Get calendar event by UID for editing"""
    from app.services.caldav_service import get_event_by_uid
    import json

    # Get user's calendar settings
    from app.models import UserSetting
    calendars_setting = db.query(UserSetting).filter(
        UserSetting.user_id == current_user.id,
        UserSetting.key == "caldav_calendars"
    ).first()

    if not calendars_setting or not calendars_setting.value:
        raise HTTPException(status_code=404, detail="No calendars configured")

    try:
        calendars = json.loads(calendars_setting.value)
    except json.JSONDecodeError:
        raise HTTPException(status_code=500, detail="Invalid calendar configuration")

    # Search all calendars for the event
    for cal in calendars:
        event = get_event_by_uid(cal['url'], cal['username'], cal['password'], uid)
        if event:
            return {
                "uid": event.uid,
                "title": event.summary,
                "date": event.start.strftime('%Y-%m-%d'),
                "time": event.start.strftime('%H:%M'),
                "endTime": event.end.strftime('%H:%M') if event.end else "",
                "location": event.location or "",
                "description": event.description or "",
                "recurrence": rrule_to_human(event.rrule) if event.rrule else ""
            }

    raise HTTPException(status_code=404, detail="Event not found")
