from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session
from typing import List
from pydantic import BaseModel
from app.database import get_db
from app.models import User, Setting
from app.schemas import UserCreate, UserResponse, SettingsUpdate, SettingsResponse
from app.auth import get_admin_user, get_password_hash
from app.services.email_service import get_email_service

router = APIRouter(prefix="/api/admin", tags=["admin"])


@router.get("/settings", response_model=SettingsResponse)
def get_settings(
    db: Session = Depends(get_db),
    admin: User = Depends(get_admin_user)
):
    settings = {s.key: s.value for s in db.query(Setting).all()}
    return SettingsResponse(**settings)


@router.put("/settings")
def update_settings(
    data: SettingsUpdate,
    db: Session = Depends(get_db),
    admin: User = Depends(get_admin_user)
):
    for key, value in data.settings.items():
        setting = db.query(Setting).filter(Setting.key == key).first()
        if setting:
            setting.value = value
        else:
            db.add(Setting(key=key, value=value))
    db.commit()
    return {"message": "Settings updated"}


@router.get("/users", response_model=List[UserResponse])
def list_users(
    db: Session = Depends(get_db),
    admin: User = Depends(get_admin_user)
):
    return db.query(User).all()


@router.post("/users", response_model=UserResponse, status_code=status.HTTP_201_CREATED)
def create_user(
    user_data: UserCreate,
    db: Session = Depends(get_db),
    admin: User = Depends(get_admin_user)
):
    # Check if username already exists
    existing = db.query(User).filter(User.username == user_data.username).first()
    if existing:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Username already exists"
        )

    user = User(
        username=user_data.username,
        password_hash=get_password_hash(user_data.password),
        is_admin=user_data.is_admin
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


@router.delete("/users/{user_id}")
def delete_user(
    user_id: int,
    db: Session = Depends(get_db),
    admin: User = Depends(get_admin_user)
):
    if user_id == admin.id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot delete yourself"
        )

    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found"
        )

    db.delete(user)
    db.commit()
    return {"message": "User deleted"}


class PasswordUpdate(BaseModel):
    password: str

@router.put("/users/{user_id}/password")
def update_user_password(
    user_id: int,
    data: PasswordUpdate,
    db: Session = Depends(get_db),
    admin: User = Depends(get_admin_user)
):
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found"
        )

    user.password_hash = get_password_hash(data.password)
    db.commit()
    return {"message": "Password updated"}


class TestEmailRequest(BaseModel):
    to_email: str


@router.post("/test-email")
def send_test_email(
    data: TestEmailRequest,
    db: Session = Depends(get_db),
    admin: User = Depends(get_admin_user)
):
    """Send a test email to verify SMTP configuration"""
    email_service = get_email_service(db)

    if not email_service.smtp_enabled:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="SMTP is not enabled. Enable it in settings first."
        )

    success, message = email_service.send_test_email(data.to_email)

    if success:
        return {"success": True, "message": message}
    else:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=message
        )


@router.post("/reload-model")
def reload_model(
    db: Session = Depends(get_db),
    admin: User = Depends(get_admin_user)
):
    """Reload the LLM model (for native/ipex backend)"""
    from app.services.inference_factory import get_backend_type, reload_inference_model

    backend = get_backend_type(db)

    if backend not in ("native", "ipex"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Model reload is only available for native/ipex backend. Current backend: " + backend
        )

    try:
        reload_inference_model(db)
        return {"success": True, "message": "Model reloaded successfully"}
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to reload model: {str(e)}"
        )


@router.get("/model-status")
def get_model_status(
    db: Session = Depends(get_db),
    admin: User = Depends(get_admin_user)
):
    """Get the current LLM model status"""
    from app.services.inference_factory import get_inference_status

    return get_inference_status(db)


@router.post("/reload-image-model")
def reload_image_model(
    db: Session = Depends(get_db),
    admin: User = Depends(get_admin_user)
):
    """Reload the image generation model (for native backend)"""
    from app.services.image_factory import reload_image_model, get_image_backend_info

    info = get_image_backend_info(db)

    if info.get("backend") != "native":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Model reload is only available for native image backend. Current backend: " + info.get("backend", "unknown")
        )

    try:
        reload_image_model(db)
        return {"success": True, "message": "Image model reloaded successfully"}
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to reload image model: {str(e)}"
        )


@router.get("/image-status")
def get_image_status(
    db: Session = Depends(get_db),
    admin: User = Depends(get_admin_user)
):
    """Get the current image generation backend status"""
    from app.services.image_factory import get_image_backend_info

    return get_image_backend_info(db)


@router.get("/vram-status")
def get_vram_status(
    db: Session = Depends(get_db),
    admin: User = Depends(get_admin_user)
):
    """Get the current VRAM status (which models are loaded)"""
    from app.services.vram_manager import get_vram_status

    return get_vram_status(db)
