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


@router.post("/reload-embedding-model")
def reload_embedding_model(
    db: Session = Depends(get_db),
    admin: User = Depends(get_admin_user)
):
    """Reload the embedding model (for RAG) with current settings."""
    from app.services.embedding_service import reload_embedding_model as reload_embed

    try:
        reload_embed(db)
        return {"success": True, "message": "Embedding model reloaded successfully"}
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to reload embedding model: {str(e)}"
        )


@router.post("/clear-rag-cache")
def clear_rag_cache(
    admin: User = Depends(get_admin_user)
):
    """Clear all RAG caches to free memory."""
    from app.services.rag_service import clear_all_caches as clear_rag
    from app.services.embedding_service import clear_embedding_cache, get_cache_stats

    try:
        # Get stats before clearing
        embed_stats = get_cache_stats()

        # Clear both caches
        clear_embedding_cache()
        clear_rag()

        return {
            "success": True,
            "message": f"Caches cleared. Freed {embed_stats['embedding_cache_size']} embedding entries."
        }
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to clear RAG caches: {str(e)}"
        )


@router.get("/mcp-status")
def get_mcp_server_status(
    admin: User = Depends(get_admin_user)
):
    """Get MCP server status and cache statistics."""
    from app.services.mcp_service import get_mcp_status, is_mcp_running

    status = get_mcp_status()
    status["running"] = is_mcp_running()
    return status


@router.post("/mcp-restart")
def restart_mcp_server(
    db: Session = Depends(get_db),
    admin: User = Depends(get_admin_user)
):
    """Restart the MCP server with current settings."""
    from app.services.mcp_service import stop_mcp_server, start_mcp_server, is_mcp_running

    try:
        # Check if MCP is enabled in settings
        mcp_enabled = db.query(Setting).filter(Setting.key == "mcp_enabled").first()
        should_run = mcp_enabled and mcp_enabled.value == "true"

        if should_run:
            # Stop if running, then start
            if is_mcp_running():
                stop_mcp_server()
                import time
                time.sleep(1)
            success = start_mcp_server(db)
            if success:
                return {"success": True, "message": "MCP server started successfully"}
            else:
                return {"success": False, "message": "Failed to start MCP server"}
        else:
            # Stop if running
            if is_mcp_running():
                stop_mcp_server()
                return {"success": True, "message": "MCP server stopped (disabled in settings)"}
            else:
                return {"success": True, "message": "MCP server is disabled"}

    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to restart MCP server: {str(e)}"
        )


@router.post("/mcp-apply")
def apply_mcp_settings(
    db: Session = Depends(get_db),
    admin: User = Depends(get_admin_user)
):
    """Apply MCP settings - start or stop server based on mcp_enabled setting."""
    from app.services.mcp_service import stop_mcp_server, start_mcp_server, is_mcp_running

    try:
        mcp_enabled = db.query(Setting).filter(Setting.key == "mcp_enabled").first()
        should_run = mcp_enabled and mcp_enabled.value == "true"
        currently_running = is_mcp_running()

        if should_run and not currently_running:
            # Start the server
            success = start_mcp_server(db)
            if success:
                return {"success": True, "message": "MCP server started", "running": True}
            else:
                return {"success": False, "message": "Failed to start MCP server", "running": False}
        elif not should_run and currently_running:
            # Stop the server
            stop_mcp_server()
            return {"success": True, "message": "MCP server stopped", "running": False}
        else:
            # No change needed
            return {"success": True, "message": "No change needed", "running": currently_running}

    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to apply MCP settings: {str(e)}"
        )


@router.post("/mcp-warmup")
def trigger_mcp_warmup(
    admin: User = Depends(get_admin_user)
):
    """Trigger MCP cache warmup."""
    from app.services.mcp_service import warmup_model

    try:
        result = warmup_model()
        return {"success": True, "result": result}
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to warmup MCP: {str(e)}"
        )
