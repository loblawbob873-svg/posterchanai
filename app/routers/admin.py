from fastapi import APIRouter, Depends, HTTPException, status
from fastapi import Query
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError
from typing import List, Optional
from pydantic import BaseModel
from datetime import datetime
import logging
from app.database import get_db
from app.models import User, Setting, ExternalStorage
from app.schemas import UserCreate, UserResponse, SettingsUpdate, SettingsResponse
from app.auth import get_admin_user, get_password_hash
from app.services.email_service import get_email_service
from app.services.storage_service import StorageService
from app.services.video_transcode_service import transcode_video, is_video_already_optimized
from app.services.thumbnail_service import is_video_file
from pathlib import Path
import asyncio
import os

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/admin", tags=["admin"])


# External Storage Management

class ExternalStorageCreate(BaseModel):
    name: str
    mount_path: str
    mount_point: str
    description: Optional[str] = None
    is_active: bool = True
    allowed_user_ids: Optional[List[int]] = None  # List of user IDs allowed to access


class ExternalStorageUpdate(BaseModel):
    name: Optional[str] = None
    mount_path: Optional[str] = None
    mount_point: Optional[str] = None
    description: Optional[str] = None
    is_active: Optional[bool] = None
    allowed_user_ids: Optional[List[int]] = None  # List of user IDs allowed to access


class ExternalStorageResponse(BaseModel):
    id: int
    name: str
    mount_path: str
    mount_point: str
    description: Optional[str]
    is_active: bool
    created_at: datetime
    updated_at: datetime
    allowed_user_ids: List[int] = []
    allowed_users: List[dict] = []  # User details for admin UI
    
    class Config:
        from_attributes = True


def _validate_external_storage_path(mount_path: str) -> bool:
    """Validate that external storage path is safe and exists."""
    try:
        path = Path(mount_path)
        # Must be absolute path
        if not path.is_absolute():
            return False
        
        # Must exist and be a directory
        if not path.exists() or not path.is_dir():
            return False
        
        # Must be readable
        if not os.access(path, os.R_OK):
            return False
        
        # Prevent mounting sensitive system directories
        forbidden_paths = ['/etc', '/sys', '/proc', '/dev', '/boot', '/root']
        for forbidden in forbidden_paths:
            if str(path).startswith(forbidden):
                return False
        
        return True
    except Exception:
        return False


def _validate_mount_point(mount_point: str) -> bool:
    """Validate mount point name (virtual path in file manager)."""
    if not mount_point:
        return False
    
    # Must be alphanumeric with dashes/underscores, no path separators
    import re
    if not re.match(r'^[a-zA-Z0-9_-]+$', mount_point):
        return False
    
    # Reserved names
    reserved = ['home', 'root', 'api', 'static', 'admin', 'login', 'register']
    if mount_point.lower() in reserved:
        return False
    
    return True


@router.get("/external-storage", response_model=List[ExternalStorageResponse])
def get_external_storage(
    db: Session = Depends(get_db),
    admin: User = Depends(get_admin_user)
):
    """Get all external storage mounts with user access info."""
    mounts = db.query(ExternalStorage).order_by(ExternalStorage.name).all()
    result = []
    for mount in mounts:
        mount_dict = {
            "id": mount.id,
            "name": mount.name,
            "mount_path": mount.mount_path,
            "mount_point": mount.mount_point,
            "description": mount.description,
            "is_active": mount.is_active,
            "created_at": mount.created_at,
            "updated_at": mount.updated_at,
            "allowed_user_ids": [user.id for user in mount.allowed_users],
            "allowed_users": [
                {"id": user.id, "username": user.username, "email": user.email}
                for user in mount.allowed_users
            ]
        }
        result.append(mount_dict)
    return result


@router.post("/external-storage", response_model=ExternalStorageResponse, status_code=status.HTTP_201_CREATED)
def create_external_storage(
    data: ExternalStorageCreate,
    db: Session = Depends(get_db),
    admin: User = Depends(get_admin_user)
):
    """Create a new external storage mount."""
    # Validate mount path
    if not _validate_external_storage_path(data.mount_path):
        raise HTTPException(
            status_code=400,
            detail="Invalid mount path. Path must be absolute, exist, be a directory, and be readable."
        )
    
    # Validate mount point
    if not _validate_mount_point(data.mount_point):
        raise HTTPException(
            status_code=400,
            detail="Invalid mount point. Must be alphanumeric with dashes/underscores only."
        )
    
    # Check if mount point already exists
    existing = db.query(ExternalStorage).filter(
        ExternalStorage.mount_point == data.mount_point
    ).first()
    if existing:
        raise HTTPException(
            status_code=400,
            detail=f"Mount point '{data.mount_point}' already exists"
        )
    
    # Create mount
    mount = ExternalStorage(
        name=data.name,
        mount_path=data.mount_path,
        mount_point=data.mount_point,
        description=data.description,
        is_active=data.is_active
    )
    
    # Set allowed users if provided
    if data.allowed_user_ids:
        users = db.query(User).filter(User.id.in_(data.allowed_user_ids)).all()
        mount.allowed_users = users
    
    db.add(mount)
    db.commit()
    db.refresh(mount)
    
    logger.info(f"Created external storage mount: {mount.name} -> {mount.mount_path} (mount_point: {mount.mount_point})")
    
    # Return with user info
    return {
        "id": mount.id,
        "name": mount.name,
        "mount_path": mount.mount_path,
        "mount_point": mount.mount_point,
        "description": mount.description,
        "is_active": mount.is_active,
        "created_at": mount.created_at,
        "updated_at": mount.updated_at,
        "allowed_user_ids": [user.id for user in mount.allowed_users],
        "allowed_users": [
            {"id": user.id, "username": user.username, "email": user.email}
            for user in mount.allowed_users
        ]
    }


@router.put("/external-storage/{mount_id}", response_model=ExternalStorageResponse)
def update_external_storage(
    mount_id: int,
    data: ExternalStorageUpdate,
    db: Session = Depends(get_db),
    admin: User = Depends(get_admin_user)
):
    """Update an external storage mount."""
    mount = db.query(ExternalStorage).filter(ExternalStorage.id == mount_id).first()
    if not mount:
        raise HTTPException(status_code=404, detail="External storage mount not found")
    
    # Validate mount path if provided
    if data.mount_path and not _validate_external_storage_path(data.mount_path):
        raise HTTPException(
            status_code=400,
            detail="Invalid mount path. Path must be absolute, exist, be a directory, and be readable."
        )
    
    # Validate mount point if provided
    if data.mount_point and not _validate_mount_point(data.mount_point):
        raise HTTPException(
            status_code=400,
            detail="Invalid mount point. Must be alphanumeric with dashes/underscores only."
        )
    
    # Check if mount point conflicts with another mount
    if data.mount_point and data.mount_point != mount.mount_point:
        existing = db.query(ExternalStorage).filter(
            ExternalStorage.mount_point == data.mount_point,
            ExternalStorage.id != mount_id
        ).first()
        if existing:
            raise HTTPException(
                status_code=400,
                detail=f"Mount point '{data.mount_point}' already exists"
            )
    
    # Update fields
    if data.name is not None:
        mount.name = data.name
    if data.mount_path is not None:
        mount.mount_path = data.mount_path
    if data.mount_point is not None:
        mount.mount_point = data.mount_point
    if data.description is not None:
        mount.description = data.description
    if data.is_active is not None:
        mount.is_active = data.is_active
    
    # Update allowed users if provided
    if data.allowed_user_ids is not None:
        users = db.query(User).filter(User.id.in_(data.allowed_user_ids)).all()
        mount.allowed_users = users
    
    db.commit()
    db.refresh(mount)
    
    logger.info(f"Updated external storage mount: {mount.name}")
    
    # Return with user info
    return {
        "id": mount.id,
        "name": mount.name,
        "mount_path": mount.mount_path,
        "mount_point": mount.mount_point,
        "description": mount.description,
        "is_active": mount.is_active,
        "created_at": mount.created_at,
        "updated_at": mount.updated_at,
        "allowed_user_ids": [user.id for user in mount.allowed_users],
        "allowed_users": [
            {"id": user.id, "username": user.username, "email": user.email}
            for user in mount.allowed_users
        ]
    }


@router.delete("/external-storage/{mount_id}")
def delete_external_storage(
    mount_id: int,
    db: Session = Depends(get_db),
    admin: User = Depends(get_admin_user)
):
    """Delete an external storage mount."""
    mount = db.query(ExternalStorage).filter(ExternalStorage.id == mount_id).first()
    if not mount:
        raise HTTPException(status_code=404, detail="External storage mount not found")
    
    db.delete(mount)
    db.commit()
    
    logger.info(f"Deleted external storage mount: {mount.name}")
    return {"message": "External storage mount deleted"}


@router.get("/settings", response_model=SettingsResponse)
def get_settings(
    db: Session = Depends(get_db),
    admin: User = Depends(get_admin_user)
):
    from app.database import safe_query_settings
    settings = safe_query_settings(db)
    return SettingsResponse(**settings)


@router.put("/settings")
def update_settings(
    data: SettingsUpdate,
    db: Session = Depends(get_db),
    admin: User = Depends(get_admin_user)
):
    # Track if cache settings changed
    cache_settings_changed = False
    cache_keys = {"file_cache_enabled", "file_cache_ttl", "file_cache_max_size"}
    
    for key, value in data.settings.items():
        setting = db.query(Setting).filter(Setting.key == key).first()
        if setting:
            setting.value = value
        else:
            db.add(Setting(key=key, value=value))
        
        if key in cache_keys:
            cache_settings_changed = True
    
    db.commit()
    
    # Reload file cache if cache settings changed
    if cache_settings_changed:
        try:
            from app.routers.files import get_file_cache
            get_file_cache(db, force_reload=True)
            logger.info("[Admin] File cache settings updated, cache reloaded")
        except Exception as e:
            logger.warning(f"[Admin] Failed to reload file cache: {e}")
    
    # Reload SQLite cache settings if changed
    sqlite_cache_keys = {"sqlite_cache_mb", "sqlite_mmap_size_mb"}
    if any(key in data.settings for key in sqlite_cache_keys):
        try:
            from app.database import reload_sqlite_settings
            reload_sqlite_settings()
            logger.info("[Admin] SQLite cache settings updated (will apply on next connection)")
        except Exception as e:
            logger.warning(f"[Admin] Failed to reload SQLite cache settings: {e}")
    
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

    try:
        # Manually delete related records in the correct order to avoid foreign key violations
        # This is necessary because existing databases might not have CASCADE constraints
        
        from app.models import (
            Conversation, Message, UserSetting, APIKey, VerificationToken,
            RAGCollection, RAGDocument, RAGWatcher, Plugin
        )
        
        # Try to import RSS models (they might not be loaded)
        try:
            from plugins.rss.models import RssFeed, RssEntry
            rss_available = True
        except ImportError:
            rss_available = False
        
        # 1. Delete messages (referenced by conversations)
        # Get conversation IDs first to avoid subquery issues
        conversation_ids = [c.id for c in db.query(Conversation.id).filter(Conversation.user_id == user_id).all()]
        if conversation_ids:
            db.query(Message).filter(Message.conversation_id.in_(conversation_ids)).delete(synchronize_session=False)
        
        # 2. Delete conversations
        db.query(Conversation).filter(Conversation.user_id == user_id).delete(synchronize_session=False)
        
        # 3. Delete RSS entries (referenced by RSS feeds)
        if rss_available:
            # Get feed IDs first
            feed_ids = [f.id for f in db.query(RssFeed.id).filter(RssFeed.user_id == user_id).all()]
            if feed_ids:
                db.query(RssEntry).filter(RssEntry.feed_id.in_(feed_ids)).delete(synchronize_session=False)
            # Delete RSS feeds
            db.query(RssFeed).filter(RssFeed.user_id == user_id).delete(synchronize_session=False)
        
        # 4. Delete RAG documents (referenced by collections)
        # Get collection IDs first
        collection_ids = [c.id for c in db.query(RAGCollection.id).filter(RAGCollection.user_id == user_id).all()]
        if collection_ids:
            db.query(RAGDocument).filter(RAGDocument.collection_id.in_(collection_ids)).delete(synchronize_session=False)
        
        # 5. Delete RAG watchers (references both users and collections)
        db.query(RAGWatcher).filter(RAGWatcher.user_id == user_id).delete(synchronize_session=False)
        
        # 6. Delete RAG collections
        db.query(RAGCollection).filter(RAGCollection.user_id == user_id).delete(synchronize_session=False)
        
        # 7. Delete user settings
        db.query(UserSetting).filter(UserSetting.user_id == user_id).delete(synchronize_session=False)
        
        # 8. Delete API keys
        db.query(APIKey).filter(APIKey.user_id == user_id).delete(synchronize_session=False)
        
        # 9. Delete verification tokens
        db.query(VerificationToken).filter(VerificationToken.user_id == user_id).delete(synchronize_session=False)
        
        # 10. Delete plugins (user_id can be NULL for global plugins, so only delete user-specific ones)
        db.query(Plugin).filter(Plugin.user_id == user_id).delete(synchronize_session=False)
        
        # 11. Finally, delete the user
        db.delete(user)
        db.commit()
        
        logger.info(f"User {user_id} ({user.username}) deleted by admin {admin.id}")
        return {"message": "User deleted"}
        
    except IntegrityError as e:
        db.rollback()
        logger.error(f"Failed to delete user {user_id}: IntegrityError - {str(e)}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to delete user due to database constraints: {str(e)}"
        )
    except Exception as e:
        db.rollback()
        logger.error(f"Failed to delete user {user_id}: {type(e).__name__} - {str(e)}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to delete user: {str(e)}"
        )


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


@router.put("/users/{user_id}/storage-quota")
def update_user_storage_quota(
    user_id: int,
    quota_mb: float = Query(..., description="Storage quota in MB (0 = unlimited)"),
    db: Session = Depends(get_db),
    admin: User = Depends(get_admin_user)
):
    """Update user storage quota."""
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    
    # Convert MB to bytes (0 = unlimited)
    quota_bytes = int(quota_mb * 1024 * 1024) if quota_mb > 0 else 0
    user.storage_quota = quota_bytes
    db.commit()
    
    return {"message": "Storage quota updated", "quota_mb": quota_mb, "quota_bytes": quota_bytes}


@router.post("/storage/rescan")
async def rescan_storage(
    user_id: Optional[int] = Query(None, description="User ID to rescan (None = all users)"),
    db: Session = Depends(get_db),
    admin: User = Depends(get_admin_user)
):
    """
    Scan file storage for a user or all users. Performs comprehensive file scanning:
    - Restores EXIF timestamps from photo/video metadata
    - Generates thumbnails for images and videos
    - Updates file index and database consistency
    - Invalidates file caches
    """
    from app.services.storage_service import get_storage_service
    from app.routers.files import get_file_cache
    from app.services.thumbnail_service import generate_thumbnails_for_user
    from app.models import Setting
    
    # Check if storage server is configured - proxy the scan request if so
    storage_server_url = db.query(Setting).filter(Setting.key == "storage_server_url").first()
    if storage_server_url and storage_server_url.value:
        url = storage_server_url.value.strip()
        if url.startswith(('http://', 'https://')):
            logger.info(f"[ADMIN] Proxying storage rescan to storage server: {url}")
            try:
                import httpx
                storage_server_token = db.query(Setting).filter(Setting.key == "storage_server_token").first()
                
                headers = {}
                if storage_server_token and storage_server_token.value:
                    headers["Authorization"] = f"Bearer {storage_server_token.value}"
                
                # Proxy to storage server with long timeout for large scans
                async with httpx.AsyncClient(timeout=600.0) as client:  # 10 minutes
                    params = {}
                    if user_id:
                        params["user_id"] = user_id
                    
                    response = await client.post(
                        f"{url}/api/admin/storage/rescan",
                        params=params,
                        headers=headers
                    )
                    
                    if response.status_code == 200:
                        return response.json()
                    else:
                        logger.error(f"[ADMIN] Storage server rescan failed: {response.status_code} - {response.text[:500]}")
                        raise HTTPException(status_code=response.status_code, detail=f"Storage server error: {response.text[:200]}")
            except httpx.TimeoutException:
                logger.error(f"[ADMIN] Timeout proxying rescan to storage server")
                raise HTTPException(status_code=504, detail="Storage server scan timeout (this is normal for large collections)")
            except httpx.ConnectError as e:
                logger.error(f"[ADMIN] Cannot connect to storage server: {e}")
                raise HTTPException(status_code=503, detail=f"Cannot reach storage server: {e}")
            except HTTPException:
                raise
            except Exception as e:
                logger.error(f"[ADMIN] Failed to proxy rescan: {e}", exc_info=True)
                raise HTTPException(status_code=500, detail=f"Failed to proxy scan: {str(e)}")
    
    def _scan_user_files(user_id: int, username: str):
        """Scan files for a single user - includes EXIF, thumbnails, and indexing."""
        # Create a NEW database session for this thread (SQLite sessions are not thread-safe)
        from app.database import SessionLocal
        thread_db = SessionLocal()
        try:
            from app.utils.exif_utils import batch_restore_timestamps
            
            storage = get_storage_service(thread_db)
            user_path = storage.get_user_path(username)
            
            # Invalidate file cache for this user
            cache = get_file_cache(thread_db)
            cache.invalidate(f"{username}:")
            
            # Initialize stats
            file_count = 0
            dir_count = 0
            exif_stats = {'restored': 0, 'processed': 0}
            thumbnail_stats = {'successful': 0, 'failed': 0}
            
            if user_path.exists():
                # Step 1: Restore EXIF timestamps for all media files
                logger.info(f"[File Scan] Step 1/3: Restoring EXIF timestamps for user {username}")
                logger.info(f"[File Scan] This will update file modification times from EXIF metadata")
                logger.info(f"[File Scan] Files copied via rsync will get their original photo/video dates restored")
                exif_stats = batch_restore_timestamps(user_path)
                logger.info(f"[File Scan] EXIF stats: {exif_stats['restored']} restored, {exif_stats['processed']} processed, {exif_stats.get('skipped', 0)} skipped, {exif_stats.get('errors', 0)} errors")
                
                # Step 2: Generate thumbnails
                logger.info(f"[File Scan] Step 2/3: Generating thumbnails for user {username}")
                successful, failed = generate_thumbnails_for_user(user_path)
                thumbnail_stats = {'successful': successful, 'failed': failed}
                logger.info(f"[File Scan] Thumbnail stats: {successful} generated, {failed} failed")
                
                # Step 3: Count files for indexing
                logger.info(f"[File Scan] Step 3/3: Indexing files for user {username}")
                for item in user_path.rglob('*'):
                    try:
                        if item.is_file():
                            file_count += 1
                        elif item.is_dir():
                            dir_count += 1
                    except Exception as e:
                        logger.warning(f"Error processing {item} for user {username}: {e}")
                        continue
            
            logger.info(f"[File Scan] Complete for {username}: {file_count} files, {dir_count} directories")
            return {
                "user_id": user_id,
                "username": username,
                "files": file_count,
                "directories": dir_count,
                "exif_restored": exif_stats.get('restored', 0),
                "exif_processed": exif_stats.get('processed', 0),
                "thumbnails_generated": thumbnail_stats.get('successful', 0),
                "thumbnails_failed": thumbnail_stats.get('failed', 0),
                "status": "success"
            }
        except Exception as e:
            logger.error(f"[File Scan] Error scanning user {username}: {e}", exc_info=True)
            return {
                "user_id": user_id,
                "username": username,
                "status": "error",
                "error": str(e)
            }
        finally:
            # Always close the thread-local database session
            thread_db.close()
    
    # Run scan in thread pool to avoid blocking
    if user_id:
        # Scan specific user
        user = db.query(User).filter(User.id == user_id).first()
        if not user:
            raise HTTPException(status_code=404, detail="User not found")
        
        # Pass primitive values, not ORM objects (to avoid session issues in threads)
        result = await asyncio.to_thread(_scan_user_files, user.id, user.username)
        return {
            "message": f"File scan completed for user {user.username}",
            "results": [result]
        }
    else:
        # Scan all users - extract primitive values before passing to threads
        users = [(u.id, u.username) for u in db.query(User).all()]
        results = []
        
        # Scan all users in parallel (but in thread pool)
        # Each user is a tuple of (user_id, username)
        tasks = [asyncio.to_thread(_scan_user_files, uid, uname) for uid, uname in users]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        
        # Handle any exceptions
        processed_results = []
        for i, result in enumerate(results):
            if isinstance(result, Exception):
                uid, uname = users[i]
                logger.error(f"[File Scan] Exception for user {uname}: {result}")
                processed_results.append({
                    "user_id": uid,
                    "username": uname,
                    "status": "error",
                    "error": str(result)
                })
            else:
                processed_results.append(result)
        
        # Calculate summary stats
        total_files = sum(r.get("files", 0) for r in processed_results if r.get("status") == "success")
        total_dirs = sum(r.get("directories", 0) for r in processed_results if r.get("status") == "success")
        total_exif_restored = sum(r.get("exif_restored", 0) for r in processed_results if r.get("status") == "success")
        total_thumbnails = sum(r.get("thumbnails_generated", 0) for r in processed_results if r.get("status") == "success")
        success_count = sum(1 for r in processed_results if r.get("status") == "success")
        
        return {
            "message": f"File scan completed for {len(users)} user(s)",
            "summary": {
                "total_users": len(users),
                "successful": success_count,
                "failed": len(users) - success_count,
                "total_files": total_files,
                "total_directories": total_dirs,
                "total_exif_restored": total_exif_restored,
                "total_thumbnails_generated": total_thumbnails
            },
            "results": processed_results
        }


@router.post("/users/{user_id}/toggle-rss-skip")
def toggle_rss_skip_summarization(
    user_id: int,
    db: Session = Depends(get_db),
    admin: User = Depends(get_admin_user)
):
    """Toggle rss_skip_summarization for a user (for bot users that do their own summarization)"""
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found"
        )

    user.rss_skip_summarization = not getattr(user, 'rss_skip_summarization', False)
    db.commit()
    return {
        "message": "RSS skip summarization toggled",
        "rss_skip_summarization": user.rss_skip_summarization
    }


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


@router.post("/generate-thumbnails")
async def generate_thumbnails(
    user_id: Optional[int] = Query(None, description="User ID to generate thumbnails for (None = all users)"),
    db: Session = Depends(get_db),
    admin: User = Depends(get_admin_user)
):
    """
    DEPRECATED: Use /storage/rescan instead, which includes thumbnail generation + EXIF restoration.
    
    This endpoint is kept for backwards compatibility but just redirects to the unified scan endpoint.
    """
    logger.warning("[Thumbnails] /generate-thumbnails endpoint is deprecated, use /storage/rescan instead")
    return await rescan_storage(user_id=user_id, db=db, admin=admin)


@router.post("/transcode-video")
async def transcode_video_admin(
    username: str = Query(..., description="Username to transcode videos for"),
    file_path: Optional[str] = Query(None, description="Specific file path to transcode (optional, if not provided, transcodes all videos)"),
    force: bool = Query(False, description="Force re-transcoding even if transcoded version exists"),
    db: Session = Depends(get_admin_user)
):
    """
    Transcode video(s) for a user. Can transcode a specific video or all videos.
    Admin only.
    """
    storage = StorageService(db)
    user_path = storage.get_user_path(username)
    
    if not user_path.exists():
        raise HTTPException(status_code=404, detail=f"User storage not found: {username}")
    
    if file_path:
        # Transcode specific file
        try:
            safe_path = Path(*[p for p in file_path.split('/') if p])
            video_path = user_path / safe_path
            
            if not video_path.exists():
                raise HTTPException(status_code=404, detail="Video file not found")
            
            if not is_video_file(video_path):
                raise HTTPException(status_code=400, detail="File is not a video")
            
            # Check if transcoded version already exists (unless force)
            from app.services.video_transcode_service import get_transcoded_video_if_exists
            if not force:
                existing_transcoded = get_transcoded_video_if_exists(user_path, video_path)
                if existing_transcoded:
                    return {
                        "message": "Transcoded version already exists",
                        "file": str(video_path.relative_to(user_path)),
                        "transcoded": str(existing_transcoded.relative_to(user_path)),
                        "skipped": True
                    }
            
            # Transcode (always transcode to ensure web-optimized format)
            transcoded_path = await asyncio.to_thread(transcode_video, user_path, video_path)
            
            if transcoded_path:
                return {
                    "message": "Video transcoded successfully",
                    "file": str(video_path.relative_to(user_path)),
                    "transcoded": str(transcoded_path.relative_to(user_path))
                }
            else:
                raise HTTPException(status_code=500, detail="Video transcoding failed")
                
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Error transcoding video {file_path}: {e}", exc_info=True)
            raise HTTPException(status_code=500, detail=f"Error transcoding video: {str(e)}")
    else:
        # Transcode all videos for user
        def _find_all_videos():
            videos = []
            for item in user_path.rglob('*'):
                if item.is_file() and is_video_file(item):
                    # Skip already transcoded videos
                    if '.transcoded' not in str(item):
                        videos.append(item)
            return videos
        
        videos = await asyncio.to_thread(_find_all_videos)
        
        if not videos:
            return {
                "message": "No videos found to transcode",
                "count": 0
            }
        
        # Transcode videos in background (don't block response)
        async def _transcode_all():
            transcoded = 0
            failed = 0
            skipped = 0
            
            for video_path in videos:
                try:
                    # Check if transcoded version already exists (unless force)
                    if not force:
                        from app.services.video_transcode_service import get_transcoded_video_if_exists
                        existing_transcoded = get_transcoded_video_if_exists(user_path, video_path)
                        if existing_transcoded:
                            skipped += 1
                            continue
                    
                    # Always transcode to ensure web-optimized format
                    transcoded_path = await asyncio.to_thread(transcode_video, user_path, video_path)
                    if transcoded_path:
                        transcoded += 1
                    else:
                        failed += 1
                except Exception as e:
                    logger.error(f"Error transcoding {video_path.name}: {e}")
                    failed += 1
            
            logger.info(f"Video transcoding complete for {username}: {transcoded} transcoded, {failed} failed, {skipped} skipped")
        
        # Start transcoding in background
        asyncio.create_task(_transcode_all())
        
        return {
            "message": f"Started transcoding {len(videos)} video(s) in background",
            "count": len(videos),
            "status": "processing"
        }


@router.get("/transcode-status")
async def get_transcode_status(
    username: str = Query(..., description="Username to check transcoding status for"),
    db: Session = Depends(get_admin_user)
):
    """
    Get transcoding status for a user's videos.
    Admin only.
    """
    storage = StorageService(db)
    user_path = storage.get_user_path(username)
    
    if not user_path.exists():
        raise HTTPException(status_code=404, detail=f"User storage not found: {username}")
    
    def _get_status():
        from app.services.video_transcode_service import get_transcoded_video_if_exists
        
        all_videos = []
        transcoded_videos = []
        optimized_videos = []
        
        for item in user_path.rglob('*'):
            if item.is_file() and is_video_file(item):
                # Skip transcoded files themselves
                if '.transcoded' in str(item):
                    continue
                
                all_videos.append(item)
                
                # Check if transcoded version exists
                transcoded_path = get_transcoded_video_if_exists(user_path, item)
                if transcoded_path:
                    transcoded_videos.append(item)
        
        return {
            "total_videos": len(all_videos),
            "transcoded": len(transcoded_videos),
            "needs_transcoding": len(all_videos) - len(transcoded_videos)
        }
    
    status = await asyncio.to_thread(_get_status)
    return status


# WebDAV sync config endpoints removed
