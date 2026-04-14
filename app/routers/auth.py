import secrets
import logging
import asyncio
import time
from pathlib import Path
from typing import List
from datetime import datetime, timedelta
from fastapi import APIRouter, Depends, HTTPException, status, Response, UploadFile, File, Request, Form
from starlette.requests import Request as StarletteRequest
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session
from sqlalchemy.exc import SQLAlchemyError, IntegrityError, OperationalError
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

    return UserSettingsResponse(
        notification_email=current_user.notification_email,
        avatar=avatar_url,
        # Custom LLM settings
        custom_ai_enabled=current_user.custom_ai_enabled or False,
        custom_ai_type=current_user.custom_ai_type,
        custom_ai_url=current_user.custom_ai_url,
        custom_ai_model=current_user.custom_ai_model,
        custom_ai_has_api_key=bool(current_user.custom_ai_api_key),
        custom_llm_prompt=current_user.custom_llm_prompt if hasattr(current_user, 'custom_llm_prompt') else None,
        # Custom Image Generation settings
        custom_image_enabled=current_user.custom_image_enabled or False,
        custom_image_url=current_user.custom_image_url,
        # Scheduled news settings
        news_schedule_enabled=current_user.news_schedule_enabled or False,
        news_schedule_time=current_user.news_schedule_time or "12:00",
        news_sources=current_user.news_sources or "",
        # Mail settings
        mail_accounts=mail_accounts,
        # Telegram settings
        telegram_enabled=current_user.telegram_enabled if hasattr(current_user, 'telegram_enabled') else False,
        telegram_chat_id=current_user.telegram_chat_id if hasattr(current_user, 'telegram_chat_id') else None,
        telegram_notifications=current_user.telegram_notifications if hasattr(current_user, 'telegram_notifications') else "",
        telegram_pending_key=current_user.telegram_key if hasattr(current_user, 'telegram_key') else None,
        telegram_key_expires_at=current_user.telegram_key_expires_at if hasattr(current_user, 'telegram_key_expires_at') else None,
        # Misskey settings
        misskey_enabled=current_user.misskey_enabled if hasattr(current_user, 'misskey_enabled') else False,
        misskey_instance_url=current_user.misskey_instance_url if hasattr(current_user, 'misskey_instance_url') else None,
        misskey_has_api_token=bool(current_user.misskey_api_token) if hasattr(current_user, 'misskey_api_token') else False,
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
    if settings.custom_llm_prompt is not None:
        # Save custom LLM prompt (can be empty to clear)
        current_user.custom_llm_prompt = settings.custom_llm_prompt if settings.custom_llm_prompt.strip() else None

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

    # Save Telegram settings
    if settings.telegram_enabled is not None:
        current_user.telegram_enabled = settings.telegram_enabled
    if settings.telegram_chat_id is not None:
        current_user.telegram_chat_id = settings.telegram_chat_id.strip() if settings.telegram_chat_id else None
    if settings.telegram_notifications is not None:
        current_user.telegram_notifications = settings.telegram_notifications

    # Save Misskey settings
    if settings.misskey_enabled is not None:
        current_user.misskey_enabled = settings.misskey_enabled
    if settings.misskey_instance_url is not None:
        current_user.misskey_instance_url = settings.misskey_instance_url.strip() if settings.misskey_instance_url else None
    if settings.misskey_api_token is not None:
        current_user.misskey_api_token = settings.misskey_api_token if settings.misskey_api_token else None

    try:
        # Flush changes to database before commit
        db.flush()
        
        # Commit the transaction - ensure this succeeds
        db.commit()
        logger.info(f"[Auth] Successfully saved user settings for user {current_user.username}")
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
    storage_server_url = db.query(Setting).filter(Setting.key == "storage_server_url").first()
    if storage_server_url and storage_server_url.value:
        # Validate URL has protocol before proxying
        url = storage_server_url.value.strip()
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



