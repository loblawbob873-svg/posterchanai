import secrets
from pathlib import Path
from typing import List
from datetime import datetime, timedelta
from fastapi import APIRouter, Depends, HTTPException, status, Response, Request, UploadFile, File
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session
from app.database import get_db
from app.models import User, Setting, APIKey, VerificationToken
from app.schemas import UserLogin, UserResponse, Token, UserRegister, APIKeyCreate, APIKeyResponse, APIKeyListItem
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
    response.set_cookie(
        key="access_token",
        value=token,
        httponly=False,  # Allow JS to read for WebSocket auth
        max_age=30 * 24 * 60 * 60,  # 30 days
        samesite="lax",
        path="/"  # Cookie available for all paths
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
            print(f"Failed to send verification email: {msg}")

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
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to send verification email: {msg}"
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

@router.get("/settings")
def get_user_settings(current_user: User = Depends(get_current_user)):
    """Get current user's settings"""
    avatar_url = f"/api/auth/avatar/{current_user.username}" if current_user.avatar else None
    return {
        "notification_email": current_user.notification_email,
        "avatar": avatar_url
    }


@router.put("/settings")
def update_user_settings(
    settings: dict,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Update current user's settings"""
    notification_email = settings.get("notification_email", "").strip()

    # Basic email validation if provided
    if notification_email and "@" not in notification_email:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid email address"
        )

    current_user.notification_email = notification_email if notification_email else None
    db.commit()

    return {"message": "Settings updated", "notification_email": current_user.notification_email}


@router.post("/avatar")
async def upload_avatar(
    file: UploadFile = File(...),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Upload user avatar image"""
    # Validate file type
    allowed_types = {"image/jpeg", "image/png", "image/gif", "image/webp"}
    if file.content_type not in allowed_types:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid file type. Allowed: JPEG, PNG, GIF, WebP"
        )

    # Read file content
    content = await file.read()
    if len(content) > 5 * 1024 * 1024:  # 5MB limit
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="File too large. Maximum size is 5MB"
        )

    # Get file extension
    ext_map = {"image/jpeg": ".jpg", "image/png": ".png", "image/gif": ".gif", "image/webp": ".webp"}
    ext = ext_map.get(file.content_type, ".png")

    # Save avatar
    storage = StorageService(db)
    filename = storage.save_avatar(current_user.username, content, ext)

    # Update user record
    current_user.avatar = filename
    db.commit()

    return {
        "message": "Avatar uploaded",
        "avatar": f"/api/auth/avatar/{current_user.username}"
    }


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
def get_avatar(username: str, db: Session = Depends(get_db)):
    """Get user avatar image"""
    storage = StorageService(db)
    avatar_path = storage.get_avatar_path(username)

    if not avatar_path or not avatar_path.exists():
        raise HTTPException(status_code=404, detail="Avatar not found")

    # Determine content type
    ext = avatar_path.suffix.lower()
    content_types = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".gif": "image/gif", ".webp": "image/webp"}
    content_type = content_types.get(ext, "image/png")

    return FileResponse(avatar_path, media_type=content_type)
