"""
Storage Router - Internal API endpoints for storage server operations.
These endpoints are called by client nodes when proxying file operations.
All blocking I/O operations are run in thread pools to prevent blocking.
"""
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form, Request, Query
from fastapi import Request as FastAPIRequest
from fastapi.responses import FileResponse, JSONResponse
import json
from sqlalchemy.orm import Session
from pathlib import Path
from datetime import datetime
import os
import time
from app.database import get_db
from app.auth import get_current_user, get_current_user_optional
from app.models import User
from app.services import settings_store
from app.services.storage_service import StorageService, _sanitize_path_component, _validate_path_within_base, ascii_safe_header_filename
from app.utils.image_validation import validate_and_clean_image_data, ensure_serializable_image
from app.utils import lb_auth
from typing import Optional
import logging
import asyncio

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/storage", tags=["storage"])

# Also add routes under /api/files for compatibility with main server proxy
files_router = APIRouter(prefix="/api/files", tags=["files"])


def safe_query_setting(db: Session, key: str) -> Optional[str]:
    """Safely read a setting value, handling errors."""
    try:
        return settings_store.get(key)
    except Exception as e:
        logger.error(f"Unexpected error querying setting '{key}': {e}", exc_info=True)
        return None


@router.post("/save-image")
async def save_image(
    file: UploadFile = File(...),
    username: str = Form(...),
    conversation_id: int = Form(...),
    prefix: str = Form("img"),
    request: FastAPIRequest = None,
    db: Session = Depends(get_db),
    current_user: Optional[User] = Depends(get_current_user_optional)
):
    """
    Save an image file. Called by client nodes when proxying file uploads.
    Only accessible on storage server node.
    Supports load-balanced requests from other posterchanai nodes.
    """
    # Allow load-balanced requests from other posterchanai nodes without authentication
    is_server_request = lb_auth.is_internal(request)

    if is_server_request:
        # For load-balanced requests, only verify user exists. Do not create Conversation rows:
        # conversation_id is from the client node's DB; inserting it here can violate primary key
        # (storage server has its own conversation ids). File path only needs username + conversation_id.
        user = db.query(User).filter(User.username == username).first()
        if not user:
            raise HTTPException(status_code=404, detail="User not found")
    else:
        # For regular requests, require authentication and verify ownership
        if not current_user:
            raise HTTPException(status_code=401, detail="Not authenticated")
        
        # Verify user owns this conversation
        from app.models import Conversation
        conversation = db.query(Conversation).filter(
            Conversation.id == conversation_id,
            Conversation.user_id == current_user.id
        ).first()
        if not conversation:
            raise HTTPException(status_code=404, detail="Conversation not found")
        
        # Verify username matches
        if current_user.username != username:
            raise HTTPException(status_code=403, detail="Access denied")
        
        user = current_user
    
    # Read file content
    content = await file.read()
    
    # Convert to base64 for storage service
    import base64
    image_base64 = base64.b64encode(content).decode('utf-8')
    
    # Run blocking file I/O in thread pool to prevent blocking other requests
    # Use bypass_proxy=True since we're on the storage server node
    def _save_image_sync():
        storage = StorageService(db)
        return storage.save_image(username, conversation_id, image_base64, prefix, bypass_proxy=True)
    
    file_path = await asyncio.to_thread(_save_image_sync)
    
    # Generate thumbnail for uploaded image asynchronously (don't block response)
    try:
        from app.services.thumbnail_service import is_image_file, generate_thumbnail_for_image
        from pathlib import Path
        
        image_path = Path(file_path)
        if image_path.exists() and is_image_file(image_path):
            storage = StorageService(db)
            user_path = storage.get_user_path(username)
            
            # Schedule thumbnail generation in background
            asyncio.create_task(
                asyncio.to_thread(generate_thumbnail_for_image, user_path, image_path)
            )
            logger.debug(f"Scheduled thumbnail generation for uploaded chat image: {image_path}")
    except Exception as e:
        logger.warning(f"Failed to schedule thumbnail generation for {file_path}: {e}")
    
    # Invalidate file cache for conversations directory (non-blocking)
    # Images are stored in: {upload_path}/{username}/conversations/{conversation_id}/img/
    try:
        from app.routers.files import get_file_cache
        cache = get_file_cache(db)
        # Invalidate root and conversations directory
        cache.invalidate(f"{username}:")
        cache.invalidate(f"{username}:conversations")
    except Exception as e:
        logger.warning(f"Failed to invalidate cache: {e}")
    
    return {"file_path": file_path}


@router.post("/save-avatar")
async def save_avatar(
    file: UploadFile = File(...),
    username: str = Form(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    Save user avatar. Called by client nodes when proxying avatar uploads.
    Only accessible on storage server node.
    """
    # Verify username matches
    if current_user.username != username:
        raise HTTPException(status_code=403, detail="Access denied")
    
    # Read file content
    content = await file.read()
    
    # Get file extension
    ext = "." + (file.filename.split('.')[-1] if '.' in file.filename else "png")
    
    # Run blocking file I/O in thread pool to prevent blocking other requests
    # Use bypass_proxy=True since we're on the storage server node
    def _save_avatar_sync():
        storage = StorageService(db)
        return storage.save_avatar(username, content, ext, bypass_proxy=True)
    
    filename = await asyncio.to_thread(_save_avatar_sync)
    
    # Avatars don't affect file listings, so no cache invalidation needed
    
    return {"filename": filename}


@router.post("/save-file")
async def save_file(
    request: FastAPIRequest,
    file: UploadFile = File(...),
    username: str = Form(...),
    conversation_id: int = Form(...),
    original_name: str = Form("file.txt"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_optional)
):
    """
    Save a text file. Called by client nodes when proxying file uploads.
    Only accessible on storage server node.
    
    Note: For proxied requests, current_user may be None if using load-balanced requests.
    In that case, we trust the main server and skip conversation verification.
    """
    # Check if this is a server-to-server request (load-balanced from another posterchanai node)
    is_server_request = lb_auth.is_internal(request)
    if not is_server_request and current_user is None:
        raise HTTPException(status_code=401, detail="Not authenticated")
    
    if not is_server_request:
        # Verify user owns this conversation (for user requests)
        from app.models import Conversation
        conversation = db.query(Conversation).filter(
            Conversation.id == conversation_id,
            Conversation.user_id == current_user.id
        ).first()
        if not conversation:
            raise HTTPException(status_code=404, detail="Conversation not found")
        
        # Verify username matches
        if current_user.username != username:
            raise HTTPException(status_code=403, detail="Access denied")
    # For server-to-server requests, we skip conversation verification (main server already verified)
    
    # Read file content
    content = await file.read()
    
    # Decode as text
    try:
        text_content = content.decode('utf-8')
        is_text = True
    except UnicodeDecodeError:
        is_text = False
    
    # Run blocking file I/O in thread pool to prevent blocking other requests
    def _save_file_sync():
        storage = StorageService(db)
        if is_text:
            return storage.save_file(username, conversation_id, text_content, original_name, bypass_proxy=True)
        else:
            return storage.save_raw_file(username, conversation_id, content, original_name)
    
    file_path = await asyncio.to_thread(_save_file_sync)
    
    # Invalidate file cache for conversations directory (non-blocking)
    try:
        from app.routers.files import get_file_cache
        cache = get_file_cache(db)
        cache.invalidate(f"{username}:")
        cache.invalidate(f"{username}:conversations")
    except Exception as e:
        logger.warning(f"Failed to invalidate cache: {e}")
    
    return {"file_path": file_path}




@router.post("/save-mail-attachment")
async def save_mail_attachment(
    request: FastAPIRequest,
    file: UploadFile = File(...),
    username: str = Form(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_optional)
):
    """
    Save a mail attachment file. Called by client nodes when proxying mail attachment saves.
    Only accessible on storage server node.
    """
    # Check if this is a server-to-server request (load-balanced from another posterchanai node)
    is_server_request = lb_auth.is_internal(request)
    if not is_server_request and current_user is None:
        raise HTTPException(status_code=401, detail="Not authenticated")

    if not is_server_request:
        # Verify username matches for user requests
        if current_user.username != username:
            raise HTTPException(status_code=403, detail="Access denied")

    # Read file content
    content = await file.read()

    # Get original filename from form or file
    original_name = file.filename or "attachment"
    
    # Run blocking file I/O in thread pool to prevent blocking other requests
    # Use bypass_proxy=True since we're on the storage server node
    def _save_mail_attachment_sync():
        storage = StorageService(db)
        return storage.save_mail_attachment(username, content, original_name, bypass_proxy=True)
    
    filename = await asyncio.to_thread(_save_mail_attachment_sync)
    
    return {"filename": filename}


@router.post("/save-generated-image")
async def save_generated_image(
    request: FastAPIRequest,
    username: str = Form(...),
    image_base64: str = Form(...),
    prompt: str = Form(""),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_optional)
):
    """
    Save a generated image to user storage. Called by client nodes when proxying generated image saves.
    Only accessible on storage server node.
    Accepts form data (for compatibility with requests library).
    """
    # Check if this is a server-to-server request (load-balanced from another posterchanai node)
    is_server_request = lb_auth.is_internal(request)
    if not is_server_request and current_user is None:
        raise HTTPException(status_code=401, detail="Not authenticated")

    if not is_server_request:
        # Verify username matches for user requests
        if current_user.username != username:
            raise HTTPException(status_code=403, detail="Access denied")

    # Run blocking file I/O in thread pool to prevent blocking other requests
    # Use bypass_proxy=True since we're on the storage server node
    def _save_generated_image_sync():
        storage = StorageService(db)
        return storage.save_generated_image(username, image_base64, prompt, bypass_proxy=True)
    
    filename = await asyncio.to_thread(_save_generated_image_sync)
    
    return {"filename": filename}


@router.post("/upload-file")
async def upload_file(
    request: FastAPIRequest,
    file: UploadFile = File(...),
    username: str = Form(...),
    path: str = Form(""),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_optional)
):
    """
    Upload a file via file manager. Called by client nodes when proxying file uploads.
    Only accessible on storage server node.
    
    Note: For proxied requests, current_user may be None if using load-balanced requests.
    In that case, we trust the main server and skip user verification.
    """
    # Check if this is a server-to-server request
    is_server_request = lb_auth.is_internal(request)
    if not is_server_request and current_user is None:
        raise HTTPException(status_code=401, detail="Not authenticated")
    if not is_server_request:
        # Verify username matches for user requests
        if current_user.username != username:
            raise HTTPException(status_code=403, detail="Access denied")
    
    # Read file content
    content = await file.read()
    
    # Get original filename
    original_name = file.filename or "uploaded_file"
    
    # Run blocking file I/O in thread pool
    def _upload_file_sync():
        from pathlib import Path
        from app.services.storage_service import _sanitize_path_component, _validate_path_within_base
        
        storage = StorageService(db)
        user_path = storage.get_user_path(username)
        
        # Sanitize and validate target path
        target_path = user_path
        if path:
            try:
                safe_path = Path(*[_sanitize_path_component(p) for p in path.split('/') if p])
                target_path = user_path / safe_path
                
                # Validate path is within user directory
                if not _validate_path_within_base(target_path, user_path):
                    raise ValueError("Access denied")
                
                # Create directory if it doesn't exist
                target_path.mkdir(parents=True, exist_ok=True)
            except ValueError as e:
                raise Exception(f"Invalid path: {e}")
        
        # Sanitize filename
        try:
            safe_filename = _sanitize_path_component(original_name)
        except ValueError as e:
            raise Exception(f"Invalid filename: {e}")
        
        full_file_path = target_path / safe_filename
        
        # Check if path already exists
        if full_file_path.exists():
            # If it exists as a directory, we need to handle it differently
            if full_file_path.is_dir():
                # Remove the directory if it's empty, or rename the file
                try:
                    # Try to remove if empty
                    if not any(full_file_path.iterdir()):
                        full_file_path.rmdir()
                        logger.info(f"Removed empty directory {full_file_path} to make way for file")
                    else:
                        # Directory is not empty - add number suffix to filename
                        base_name = full_file_path.stem
                        extension = full_file_path.suffix
                        counter = 1
                        while full_file_path.exists():
                            full_file_path = target_path / f"{base_name}_{counter}{extension}"
                            counter += 1
                except Exception as e:
                    # If we can't remove it, rename the file
                    logger.warning(f"Could not remove directory {full_file_path}: {e}. Renaming file.")
                    base_name = full_file_path.stem
                    extension = full_file_path.suffix
                    counter = 1
                    while full_file_path.exists():
                        full_file_path = target_path / f"{base_name}_{counter}{extension}"
                        counter += 1
            else:
                # File exists - add number suffix
                base_name = full_file_path.stem
                extension = full_file_path.suffix
                counter = 1
                while full_file_path.exists():
                    full_file_path = target_path / f"{base_name}_{counter}{extension}"
                    counter += 1
        
        # Ensure parent directory exists
        full_file_path.parent.mkdir(parents=True, exist_ok=True)
        
        # Write file
        with open(full_file_path, 'wb') as f:
            f.write(content)
        
        # Verify the file was created correctly
        if not full_file_path.is_file():
            raise Exception(f"File was not created correctly at {full_file_path}")
        
        # Restore EXIF timestamp if it's a media file
        try:
            from app.utils.exif_utils import restore_exif_timestamp
            if restore_exif_timestamp(full_file_path):
                logger.info(f"[UPLOAD] ✓ Restored EXIF timestamp for: {full_file_path.name}")
        except Exception as e:
            logger.debug(f"[UPLOAD] Could not restore EXIF timestamp for {full_file_path.name}: {e}")
        
        return str(full_file_path.relative_to(user_path)), safe_filename, full_file_path
    
    relative_path, safe_filename, full_file_path = await asyncio.to_thread(_upload_file_sync)
    
    # Generate thumbnail and transcode videos asynchronously (don't block upload response)
    try:
        from app.services.thumbnail_service import is_image_file, is_video_file, generate_thumbnail_for_media
        from app.services.video_transcode_service import transcode_video
        # Get user_path in async context
        storage = StorageService(db)
        user_path = storage.get_user_path(username)
        
        if is_image_file(full_file_path) or is_video_file(full_file_path):
            # Schedule thumbnail generation in background
            asyncio.create_task(
                asyncio.to_thread(generate_thumbnail_for_media, user_path, full_file_path)
            )
            media_type = "image" if is_image_file(full_file_path) else "video"
            logger.info(f"[UPLOAD] ✓ Scheduled thumbnail generation for {media_type}: {full_file_path.name}")
            
            # For videos, also schedule transcoding for faster playback
            if is_video_file(full_file_path):
                asyncio.create_task(
                    asyncio.to_thread(transcode_video, user_path, full_file_path)
                )
                logger.info(f"[UPLOAD] ✓ Scheduled video transcoding for: {full_file_path.name}")
    except Exception as e:
        logger.warning(f"Failed to schedule media processing for {full_file_path}: {e}")
    
    # Invalidate file cache
    try:
        from app.routers.files import get_file_cache
        cache = get_file_cache(db)
        cache.invalidate(f"{username}:")
        if path:
            cache.invalidate(f"{username}:{path}")
    except Exception as e:
        logger.warning(f"Failed to invalidate cache: {e}")
    
    return {
        "message": "File uploaded successfully",
        "path": relative_path,
        "filename": safe_filename
    }


# Note attachment endpoints removed - notes feature was removed


@router.get("/list-files")
async def list_files(
    request: FastAPIRequest,
    username: str = Query(...),
    path: str = Query(""),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_optional)
):
    """
    List files in user's storage. Called by client nodes when proxying file listings.
    Only accessible on storage server node.
    """
    # Check if this is a server-to-server request (load-balanced from another posterchanai node)
    is_server_request = lb_auth.is_internal(request)
    if not is_server_request and current_user is None:
        raise HTTPException(status_code=401, detail="Not authenticated")
    
    if not is_server_request:
        # Verify username matches for user requests
        if current_user.username != username:
            raise HTTPException(status_code=403, detail="Access denied")
    
    storage = StorageService(db)
    user_path = storage.get_user_path(username)
    
    # Sanitize and validate path
    target_path = user_path
    if path:
        try:
            safe_path = Path(*[_sanitize_path_component(p) for p in path.split('/') if p])
            target_path = user_path / safe_path
            
            # Validate path is within user directory
            if not _validate_path_within_base(target_path, user_path):
                raise HTTPException(status_code=403, detail="Access denied")
        except ValueError as e:
            raise HTTPException(status_code=400, detail=f"Invalid path: {e}")
    
    if not target_path.exists():
        raise HTTPException(status_code=404, detail="Path not found")
    
    if not target_path.is_dir():
        raise HTTPException(status_code=400, detail="Path is not a directory")
    
    # Run directory listing in thread pool
    def _list_directory_sync():
        """Synchronous directory listing function."""
        items = []
        try:
            for item in sorted(target_path.iterdir()):
                try:
                    # Use stat() to get file info - more reliable than separate calls
                    stat = item.stat()
                    
                    # Check if it's a directory - is_dir() is the authoritative check
                    is_dir = item.is_dir()
                    
                    # Double-check: if it's a symlink, resolve it
                    if item.is_symlink():
                        try:
                            resolved = item.resolve()
                            is_dir = resolved.is_dir()
                        except Exception:
                            # If symlink is broken, use original check
                            pass
                    
                    # Get file size - directories can have non-zero size on some filesystems
                    # Always trust is_dir() - it's the correct filesystem check
                    file_size = stat.st_size
                    
                    item_path = item.relative_to(user_path).as_posix()
                    
                    item_info = {
                        "name": item.name,
                        "path": item_path,
                        "is_directory": is_dir,
                        "size": file_size if not is_dir else 0,
                        "modified": stat.st_mtime,
                        "is_external": False,
                    }
                    
                    # Generate thumbnail for images (skip for now to avoid circular import)
                    # Thumbnails can be generated on the client node if needed
                    
                    items.append(item_info)
                except (OSError, PermissionError) as e:
                    logger.debug(f"Error reading item {item.name}: {e}")
                    continue
                except Exception as e:
                    logger.warning(f"Error reading item {item}: {e}")
                    continue
        except Exception as e:
            logger.error(f"Error listing directory {target_path}: {e}", exc_info=True)
            raise Exception(f"Error listing directory: {e}")
        return items
    
    try:
        items = await asyncio.to_thread(_list_directory_sync)
        
        # Calculate storage usage
        def calculate_directory_size(directory):
            total = 0
            try:
                for item in Path(directory).rglob('*'):
                    if item.is_file():
                        try:
                            total += item.stat().st_size
                        except (OSError, PermissionError):
                            pass
            except Exception:
                pass
            return total
        
        current_usage = await asyncio.to_thread(calculate_directory_size, user_path)
        
        # Get user quota from database
        from app.models import User
        user = db.query(User).filter(User.username == username).first()
        user_quota = user.storage_quota if user else 0
        
        return {
            "items": items,
            "path": path if path else "",
            "is_external": False,
            "external_name": None,
            "storage": {
                "used": current_usage,
                "quota": user_quota,
                "quota_mb": user_quota / (1024 * 1024) if user_quota > 0 else 0,
                "used_mb": current_usage / (1024 * 1024),
                "unlimited": user_quota == 0
            }
        }
    except Exception as e:
        logger.error(f"Error listing files: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/all-images")
async def get_all_images(
    request: FastAPIRequest,
    username: str = Query(...),
    limit: int = Query(1000, description="Maximum number of images to return"),
    offset: int = Query(0, description="Offset for pagination"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_optional)
):
    """
    Get all images and videos from user's storage recursively, sorted by newest first.
    Called by client nodes when proxying image/video requests.
    Only accessible on storage server node.
    """
    # Check if this is a server-to-server request (load-balanced from another posterchanai node)
    is_server_request = lb_auth.is_internal(request)
    if not is_server_request and current_user is None:
        raise HTTPException(status_code=401, detail="Not authenticated")
    
    if not is_server_request:
        # Verify username matches for user requests
        if current_user.username != username:
            raise HTTPException(status_code=403, detail="Access denied")
    
    storage = StorageService(db)
    user_path = storage.get_user_path(username)
    
    # Image and video extensions
    from app.services.thumbnail_service import IMAGE_EXTENSIONS, VIDEO_EXTENSIONS
    media_extensions = IMAGE_EXTENSIONS | VIDEO_EXTENSIONS
    
    def _get_all_images_sync():
        """Synchronous function to get all images and videos."""
        images = []
        skipped_count = 0
        skipped_reasons = {}
        
        try:
            # Recursively find all image and video files
            for item in user_path.rglob('*'):
                try:
                    # Skip directories and non-media files
                    if item.is_dir() or item.suffix.lower() not in media_extensions:
                        continue
                    
                    # Skip hidden files (starting with .) - these are usually system files
                    if item.name.startswith('.'):
                        skipped_count += 1
                        skipped_reasons['hidden'] = skipped_reasons.get('hidden', 0) + 1
                        continue
                    
                    # Skip thumbnails directory - check path parts, not just string contains
                    # This avoids false positives like "my.thumbnails.jpg"
                    try:
                        relative = item.relative_to(user_path)
                        # Skip any path that contains .thumbnails as a directory component
                        if any(part == '.thumbnails' for part in relative.parts):
                            skipped_count += 1
                            skipped_reasons['thumbnails'] = skipped_reasons.get('thumbnails', 0) + 1
                            continue
                        # Skip other hidden/system directories
                        if any(part.startswith('.') and part != '.' for part in relative.parts):
                            skipped_count += 1
                            skipped_reasons['hidden_dir'] = skipped_reasons.get('hidden_dir', 0) + 1
                            continue
                    except ValueError:
                        # If relative path calculation fails, fall back to string check
                        if '.thumbnails' in str(item):
                            skipped_count += 1
                            skipped_reasons['thumbnails'] = skipped_reasons.get('thumbnails', 0) + 1
                            continue

                    # Skip files from non-photo folders (Music, Documents, etc.)
                    # Also skip files not in year-based photo folders (Pictures/YYYY/)
                    try:
                        relative = item.relative_to(user_path)
                        parts = relative.parts

                        # Get first path component (top-level folder)
                        top_folder = parts[0].lower() if parts else ''

                        # Exclude common non-photo folders
                        excluded_folders = {'music', 'documents', 'downloads', 'desktop', 'videos'}
                        if top_folder in excluded_folders:
                            skipped_count += 1
                            skipped_reasons['excluded_folder'] = skipped_reasons.get('excluded_folder', 0) + 1
                            continue

                        # For Pictures folder, only include files from year subfolders (2020, 2021, etc.)
                        # This excludes Pictures/Social Media, Pictures/Videos, Pictures/sparrow, etc.
                        if top_folder == 'pictures' and len(parts) >= 2:
                            second_folder = parts[1]
                            # Check if second folder is a 4-digit year (2000-2099)
                            if not (second_folder.isdigit() and len(second_folder) == 4 and 2000 <= int(second_folder) <= 2099):
                                skipped_count += 1
                                skipped_reasons['non_year_folder'] = skipped_reasons.get('non_year_folder', 0) + 1
                                continue

                        # Skip files in root of Pictures (not in any subfolder)
                        if top_folder == 'pictures' and len(parts) < 2:
                            skipped_count += 1
                            skipped_reasons['pictures_root'] = skipped_reasons.get('pictures_root', 0) + 1
                            continue

                        # Skip root-level files (not in any folder)
                        if len(parts) < 1 or (len(parts) == 1 and parts[0] == item.name):
                            skipped_count += 1
                            skipped_reasons['root_level'] = skipped_reasons.get('root_level', 0) + 1
                            continue

                    except (ValueError, IndexError):
                        pass  # If path calculation fails, include the file

                    # Check if file exists and is readable
                    if not item.exists() or not item.is_file():
                        skipped_count += 1
                        skipped_reasons['not_file'] = skipped_reasons.get('not_file', 0) + 1
                        continue
                    
                    stat = item.stat()
                    
                    # Skip empty files (likely not valid media)
                    if stat.st_size == 0:
                        skipped_count += 1
                        skipped_reasons['empty'] = skipped_reasons.get('empty', 0) + 1
                        continue
                    
                    # Skip very small files that are likely not real images/videos
                    # Images should be at least 1KB, videos at least 10KB
                    if stat.st_size < 1024:  # Less than 1KB
                        skipped_count += 1
                        skipped_reasons['too_small'] = skipped_reasons.get('too_small', 0) + 1
                        continue
                    
                    relative_path = str(item.relative_to(user_path))
                    
                    # Get modification time for sorting
                    # Use mtime (modification time) which should match photo date after EXIF restoration
                    # Only fall back to ctime if mtime is invalid
                    modified_time = stat.st_mtime if stat.st_mtime > 0 else (stat.st_ctime if stat.st_ctime > 0 else time.time())
                    
                    # Try to extract date from filename if modification time seems wrong
                    # Common patterns: YYYYMMDD, YYYYMMDD_HHMMSS, YY-MM-DD HH-MM-SS, etc.
                    filename = item.name
                    import re
                    from datetime import datetime

                    # Try multiple date patterns (4-digit year first, then 2-digit year)
                    patterns = [
                        # YYYYMMDD_HHMMSS or YYYYMMDD-HHMMSS (e.g., 20250910_172438, IMG_20250910_172438)
                        r'(\d{4})(\d{2})(\d{2})[_-](\d{2})(\d{2})(\d{2})',
                        # YYYYMMDD (e.g., 20250910, IMG_20250910)
                        r'(\d{4})(\d{2})(\d{2})',
                        # YY-MM-DD HH-MM-SS or YY-MM-DD HH:MM:SS (e.g., 25-09-10 17-24-38)
                        r'(\d{2})[-./ ](\d{2})[-./ ](\d{2})[\s_-]+(\d{2})[-.:](\d{2})[-.:](\d{2})',
                        # YY-MM-DD (e.g., 25-09-10)
                        r'(\d{2})[-./ ](\d{2})[-./ ](\d{2})',
                        # YYYY-MM-DD HH:MM:SS or YYYY-MM-DD HH-MM-SS (e.g., 2025-09-10 17:24:38)
                        r'(\d{4})[-./ ](\d{2})[-./ ](\d{2})[\s_-]+(\d{2})[-.:](\d{2})[-.:](\d{2})',
                        # YYYY-MM-DD (e.g., 2025-09-10)
                        r'(\d{4})[-./ ](\d{2})[-./ ](\d{2})',
                    ]

                    filename_date = None
                    for pattern in patterns:
                        match = re.search(pattern, filename)
                        if match:
                            try:
                                groups = match.groups()
                                year = int(groups[0])
                                month = int(groups[1])
                                day = int(groups[2])

                                # Convert 2-digit year to 4-digit (assume 20xx for years 00-99)
                                if year < 100:
                                    year += 2000

                                # Extract time if available
                                hour, minute, second = 0, 0, 0
                                if len(groups) >= 6:
                                    hour = int(groups[3])
                                    minute = int(groups[4])
                                    second = int(groups[5])

                                # Validate date
                                if 1 <= month <= 12 and 1 <= day <= 31 and 0 <= hour < 24 and 0 <= minute < 60 and 0 <= second < 60:
                                    filename_date = datetime(year, month, day, hour, minute, second)
                                    break  # Found valid date, stop searching
                            except (ValueError, OverflowError):
                                continue  # Try next pattern

                    if filename_date:
                        filename_timestamp = filename_date.timestamp()

                        # If filename date is significantly older than modification time (more than 30 days difference),
                        # OR if mtime is very recent but filename suggests old date,
                        # use filename date instead. This handles cases where files were copied but have old photo dates.
                        current_time = time.time()
                        mtime_age_days = (current_time - modified_time) / 86400
                        filename_age_days = (current_time - filename_timestamp) / 86400

                        mtime_is_recent = mtime_age_days < 7  # Modified within last week
                        filename_is_old = filename_age_days > 30  # Filename suggests date older than 30 days

                        if filename_age_days > (mtime_age_days + 30) or (mtime_is_recent and filename_is_old):
                            # Filename suggests much older date than modification time
                            # Use filename date for sorting
                            modified_time = filename_timestamp
                            logger.info(f"[STORAGE] Using filename date for {filename}: {filename_date} (mtime was {datetime.fromtimestamp(stat.st_mtime)}, mtime_age={mtime_age_days:.1f}d, filename_age={filename_age_days:.1f}d)")
                    else:
                        # No filename date extracted - check if this is a recently copied file
                        current_time = time.time()
                        mtime_age_days = (current_time - modified_time) / 86400

                        # If mtime is very recent (< 7 days) and no filename date, this is likely
                        # a file copied via rsync with no EXIF data. Push to bottom by using a very old date.
                        if mtime_age_days < 7:
                            modified_time = 0.0  # Epoch time - will sort to bottom
                            logger.info(f"[STORAGE] No filename date for {filename}, recent mtime ({mtime_age_days:.1f}d old) - pushing to bottom")

                    # Fallback to current time if both are invalid
                    if modified_time <= 0:
                        logger.warning(f"Invalid timestamp for {item}: mtime={stat.st_mtime}, ctime={stat.st_ctime}, using 0")
                        modified_time = 0.0  # Push invalid timestamps to bottom
                    
                    from app.services.thumbnail_service import is_image_file, is_video_file
                    
                    is_image = is_image_file(item)
                    is_video = is_video_file(item)
                    
                    # Only include files that are actually images or videos
                    if not is_image and not is_video:
                        skipped_count += 1
                        skipped_reasons['not_media'] = skipped_reasons.get('not_media', 0) + 1
                        continue
                    
                    # Additional size checks based on file type
                    if is_image and stat.st_size < 1024:  # Images should be at least 1KB
                        skipped_count += 1
                        skipped_reasons['image_too_small'] = skipped_reasons.get('image_too_small', 0) + 1
                        continue
                    
                    if is_video and stat.st_size < 10240:  # Videos should be at least 10KB
                        skipped_count += 1
                        skipped_reasons['video_too_small'] = skipped_reasons.get('video_too_small', 0) + 1
                        continue
                    
                    # Skip files that are likely thumbnails based on name/path patterns
                    name_lower = item.name.lower()
                    if any(pattern in name_lower for pattern in ['thumb', 'thumbnail', '_tn', '_small', '_mini', '_thumb']):
                        try:
                            relative = item.relative_to(user_path)
                            if '.thumbnails' in relative.parts:
                                skipped_count += 1
                                skipped_reasons['thumbnail_file'] = skipped_reasons.get('thumbnail_file', 0) + 1
                                continue
                        except ValueError:
                            pass
                    
                    # Skip PIL validation for performance - trust file extension and size checks
                    # PIL Image.open() is very slow when scanning thousands of files
                    # We rely on file extension, size checks, and thumbnail_service.is_image_file() instead
                    
                    # Ensure all values are JSON serializable - be very explicit
                    # Convert everything to basic Python types (str, int, float, bool)
                    name_str = str(item.name) if not isinstance(item.name, bytes) else item.name.decode('utf-8', errors='ignore')
                    path_str = str(relative_path) if not isinstance(relative_path, bytes) else relative_path.decode('utf-8', errors='ignore')
                    
                    image_info = {
                        "name": name_str,
                        "path": path_str,
                        "size": int(stat.st_size),
                        "modified": float(modified_time),  # CRITICAL: Ensure it's a float, not string
                        "modified_date": str(datetime.fromtimestamp(modified_time).isoformat()),
                        "type": str("image" if is_image else "video" if is_video else "unknown"),
                    }
                    
                    # Final safety check - ensure no bytes slipped through
                    for key, value in list(image_info.items()):
                        if isinstance(value, bytes):
                            logger.warning(f"[STORAGE] Found bytes in image_info.{key}, converting")
                            image_info[key] = value.decode('utf-8', errors='ignore')
                        elif not isinstance(value, (str, int, float, bool, type(None))):
                            logger.warning(f"[STORAGE] Found non-serializable type {type(value)} in image_info.{key}, converting")
                            image_info[key] = str(value)
                    
                    # Validate and clean image data
                    logger.debug(f"[STORAGE] Before validation: name={image_info.get('name')}, path={image_info.get('path')}")
                    cleaned_image_info = validate_and_clean_image_data(image_info, item_path=item)
                    if not cleaned_image_info:
                        logger.error(f"[STORAGE] ❌ Image validation FAILED for: {item.name}")
                        logger.error(f"[STORAGE]    - path in dict: {'path' in image_info}")
                        logger.error(f"[STORAGE]    - path value: '{image_info.get('path', 'KEY_MISSING')}'")
                        logger.error(f"[STORAGE]    - name value: '{image_info.get('name', 'KEY_MISSING')}'")
                        logger.error(f"[STORAGE]    - All keys: {list(image_info.keys())}")
                        continue
                    logger.debug(f"[STORAGE] ✓ After validation: name={cleaned_image_info.get('name')}, path={cleaned_image_info.get('path')}")
                    image_info = cleaned_image_info
                    
                    # Skip loading thumbnails during initial scan for performance
                    # Thumbnails will be loaded on-demand by the frontend via /thumbnail endpoint
                    # This dramatically speeds up the initial scan when there are thousands of files
                    
                    images.append(image_info)
                except Exception as e:
                    # Don't log "cannot identify" errors as warnings
                    if "cannot identify" not in str(e).lower():
                        logger.warning(f"Error processing image {item}: {e}")
                    continue
        except Exception as e:
            logger.error(f"Error getting all images: {e}")
            raise Exception(f"Error getting all images: {e}")
        
        # Remove duplicates based on path FIRST (before sorting)
        seen_paths = set()
        unique_images = []
        duplicates_removed = 0
        for img in images:
            path = img.get('path', '')
            if path in seen_paths:
                duplicates_removed += 1
                continue
            seen_paths.add(path)
            unique_images.append(img)
        images = unique_images
        
        # Sort by modified time (newest first)
        # CRITICAL: Convert ALL timestamps to float BEFORE sorting to ensure proper numeric comparison
        for img in images:
            if 'modified' in img:
                # Ensure it's a float, handle None/empty cases
                try:
                    modified_val = img['modified']
                    if modified_val is None or modified_val == '':
                        img['modified'] = 0.0
                    else:
                        img['modified'] = float(modified_val)
                except (ValueError, TypeError) as e:
                    logger.warning(f"Failed to convert modified time for {img.get('path', 'unknown')}: {e}, using 0.0")
                    img['modified'] = 0.0
        
        # Sort by modified time descending (newest first), then by path for stability
        # Use positive timestamps with reverse=True for clarity
        def sort_key(img):
            modified = float(img.get('modified', 0) or 0)
            path = str(img.get('path', '')).lower()
            # Return tuple: (modified, path) - we'll use reverse=True to get descending order
            return (modified, path)
        
        # Sort the list - descending by modified time (newest first)
        # reverse=True means higher timestamps (newer files) come first
        images.sort(key=sort_key, reverse=True)
        
        # Double-check: verify sort worked
        prev_ts = None
        sort_errors = []
        for i, img in enumerate(images[:50]):
            curr_ts = float(img.get('modified', 0) or 0)
            if prev_ts is not None and curr_ts > prev_ts:
                sort_errors.append((i, img.get('name'), curr_ts, prev_ts))
                logger.error(f"[STORAGE] CRITICAL: Sort failed! Image {i} ({img.get('name')}) has timestamp {curr_ts} which is NEWER than previous {prev_ts}")
            prev_ts = curr_ts
        
        if sort_errors:
            logger.error(f"[STORAGE] Found {len(sort_errors)} sorting errors in first 50 images!")
            for idx, name, curr, prev in sort_errors[:5]:
                logger.error(f"[STORAGE] Error {idx}: {name} - current={curr}, previous={prev}, diff={curr-prev}")
            if len(sort_errors) > 10:
                logger.error("[STORAGE] Too many errors - attempting to fix by re-sorting...")
                # CRITICAL: Must use reverse=True to keep newest first!
                images.sort(key=sort_key, reverse=True)
                logger.error("[STORAGE] Re-sorted array with reverse=True (newest first)")
        else:
            logger.info(f"[STORAGE] ✓ Sort verified: First 50 images in correct order (newest first)")
        
        # Debug: log statistics
        total_scanned = len(images) + skipped_count
        logger.info(f"[STORAGE] Image scan complete:")
        logger.info(f"  - Total files scanned: {total_scanned}")
        logger.info(f"  - Valid images/videos: {len(images)}")
        logger.info(f"  - Files skipped: {skipped_count}")
        logger.info(f"  - Duplicates removed: {duplicates_removed}")
        if skipped_reasons:
            logger.info(f"  - Skip reasons breakdown: {skipped_reasons}")
        logger.info(f"[STORAGE] Final count returned to client: {len(images)} images/videos")
        
        # Log first few images for debugging if we have any
        if len(images) > 0:
            logger.info(f"[STORAGE] First 3 images: {[(img.get('name', 'NO_NAME'), img.get('path', 'NO_PATH')) for img in images[:3]]}")
        else:
            logger.warning(f"[STORAGE] WARNING: No images found after scanning {total_scanned} files!")
        
        # Debug: log first few images to verify sorting
        if images:
            logger.info(f"[STORAGE] Found {len(images)} images total. First 10 (newest first):")
            for i, img in enumerate(images[:10]):
                mod_time = img.get('modified', 0)
                mod_date = datetime.fromtimestamp(mod_time).isoformat() if mod_time > 0 else 'N/A'
                path_info = img.get('path', 'unknown')
                # Check if this looks like a thumbnail file
                is_thumbnail = '.thumbnails' in path_info.lower()
                thumb_marker = " [THUMBNAIL?]" if is_thumbnail else ""
                logger.info(f"  {i+1}. {img.get('name')} - modified: {mod_time} ({mod_date}) - path: {path_info}{thumb_marker}")
            
            # Verify sorting - check more images
            prev_time = None
            sorting_errors = []
            for i, img in enumerate(images[:50]):  # Check first 50
                curr_time = float(img.get('modified', 0) or 0)
                if prev_time is not None and curr_time > prev_time:
                    sorting_errors.append((i, img.get('name'), curr_time, prev_time))
                    logger.warning(f"[STORAGE] Sorting error at index {i}: {img.get('name')} (time: {curr_time}) is newer than previous (time: {prev_time})")
                prev_time = curr_time
            
            if not sorting_errors:
                logger.info(f"[STORAGE] ✓ Sorting verified: All {min(50, len(images))} checked images are in correct order (newest first)")
            else:
                logger.error(f"[STORAGE] ❌ Found {len(sorting_errors)} sorting errors in first 50 images")
        
        # Apply pagination
        total = len(images)
        paginated_images = images[offset:offset + limit]
        
        # Ensure all data is JSON serializable
        serializable_images = [ensure_serializable_image(img) for img in paginated_images]
        
        return {
            "images": serializable_images,
            "total": int(total),
            "limit": int(limit),
            "offset": int(offset),
            "has_more": bool(offset + limit < total)
        }
    
    def _clean_for_json(obj, depth=0):
        """Recursively clean object to ensure JSON serializability."""
        if depth > 10:  # Prevent infinite recursion
            return ""
        
        if obj is None:
            return None
        elif isinstance(obj, bytes):
            try:
                return obj.decode('utf-8', errors='ignore')
            except Exception:
                return ""
        elif isinstance(obj, Path):
            return str(obj)
        elif isinstance(obj, dict):
            cleaned = {}
            for k, v in obj.items():
                # Ensure key is string
                key_str = str(k) if not isinstance(k, bytes) else k.decode('utf-8', errors='ignore')
                cleaned[key_str] = _clean_for_json(v, depth + 1)
            return cleaned
        elif isinstance(obj, (list, tuple)):
            return [_clean_for_json(item, depth + 1) for item in obj]
        elif isinstance(obj, (str, int, float, bool)):
            return obj
        elif isinstance(obj, dict):
            # Nested dict - clean recursively
            cleaned = {}
            for k, v in obj.items():
                key_str = str(k) if not isinstance(k, bytes) else k.decode('utf-8', errors='ignore')
                cleaned[key_str] = _clean_for_json(v, depth + 1)
            return cleaned
        elif isinstance(obj, (list, tuple)):
            # Nested list - clean recursively
            return [_clean_for_json(item, depth + 1) for item in obj]
        else:
            # Convert anything else to string
            try:
                # Check if it's a type that might contain bytes
                if hasattr(obj, '__bytes__'):
                    return obj.__bytes__().decode('utf-8', errors='ignore')
                return str(obj)
            except Exception:
                return ""
    
    try:
        result = await asyncio.to_thread(_get_all_images_sync)
        # Result is already cleaned by ensure_serializable_image() 
        # Don't do any additional processing that might convert dicts to strings
        
        # Verify the structure is correct
        if isinstance(result, dict) and 'images' in result:
            if result['images'] and isinstance(result['images'][0], str):
                logger.error(f"[STORAGE] BUG: Images are strings! First: {result['images'][0][:100]}")
        
        # Return directly without additional cleaning
        return JSONResponse(content=result)
    except Exception as e:
        logger.error(f"[STORAGE] Error in get_all_images: {e}", exc_info=True)
        # Ensure error message is also JSON serializable
        error_msg = str(e)
        if isinstance(e, bytes):
            error_msg = e.decode('utf-8', errors='ignore')
        raise HTTPException(status_code=500, detail=error_msg)


@router.post("/mkdir")
async def mkdir(
    request: FastAPIRequest,
    username: str = Form(...),
    path: str = Form(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_optional)
):
    """
    Create a directory. Called by client nodes when proxying directory creation.
    Only accessible on storage server node.
    """
    # Check if this is a server-to-server request (load-balanced from another posterchanai node)
    is_server_request = lb_auth.is_internal(request)
    if not is_server_request and current_user is None:
        raise HTTPException(status_code=401, detail="Not authenticated")
    
    if not is_server_request:
        # Verify username matches for user requests
        if current_user.username != username:
            raise HTTPException(status_code=403, detail="Access denied")
    
    storage = StorageService(db)
    user_path = storage.get_user_path(username)
    
    # Sanitize and validate path
    try:
        safe_path = Path(*[_sanitize_path_component(p) for p in path.split('/') if p])
        full_path = user_path / safe_path
    except ValueError as e:
        raise HTTPException(status_code=400, detail=f"Invalid path: {e}")
    
    # Validate path is within user directory
    if not _validate_path_within_base(full_path, user_path):
        raise HTTPException(status_code=403, detail="Access denied")
    
    if full_path.exists():
        raise HTTPException(status_code=400, detail="Directory already exists")
    
    # Run directory creation in thread pool
    def _create_dir_sync():
        try:
            full_path.mkdir(parents=True, exist_ok=True)
            return str(full_path.relative_to(user_path))
        except Exception as e:
            raise Exception(f"Error creating directory: {e}")
    
    try:
        relative_path = await asyncio.to_thread(_create_dir_sync)
        
        # Invalidate file cache
        try:
            from app.routers.files import get_file_cache
            cache = get_file_cache(db)
            cache.invalidate(f"{username}:")
            if path:
                cache.invalidate(f"{username}:{path}")
        except Exception as e:
            logger.warning(f"Failed to invalidate cache: {e}")
        
        return {
            "message": "Directory created successfully",
            "path": relative_path
        }
    except Exception as e:
        logger.error(f"Error creating directory: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/delete-file")
async def delete_file(
    request: FastAPIRequest,
    username: str = Query(...),
    file_path: str = Query(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_optional)
):
    """
    Delete a file or directory. Called by client nodes when proxying file deletions.
    Only accessible on storage server node.
    """
    # Check if this is a server-to-server request (load-balanced from another posterchanai node)
    is_server_request = lb_auth.is_internal(request)
    if not is_server_request and current_user is None:
        raise HTTPException(status_code=401, detail="Not authenticated")
    
    if not is_server_request:
        # Verify username matches for user requests
        if current_user.username != username:
            raise HTTPException(status_code=403, detail="Access denied")
    
    storage = StorageService(db)
    user_path = storage.get_user_path(username)
    
    # Sanitize and validate path
    try:
        safe_path = Path(*[_sanitize_path_component(p) for p in file_path.split('/') if p])
        full_path = user_path / safe_path
    except ValueError as e:
        raise HTTPException(status_code=400, detail=f"Invalid path: {e}")
    
    # Validate path is within user directory
    if not _validate_path_within_base(full_path, user_path):
        raise HTTPException(status_code=403, detail="Access denied")
    
    if not full_path.exists():
        raise HTTPException(status_code=404, detail="File or directory not found")
    
    # Run deletion in thread pool
    def _delete_file_sync():
        try:
            if full_path.is_file():
                # Delete thumbnail if it's an image
                try:
                    from app.services.thumbnail_service import is_image_file, delete_thumbnail
                    if is_image_file(full_path):
                        delete_thumbnail(user_path, full_path)
                except Exception as e:
                    logger.warning(f"Failed to delete thumbnail for {file_path}: {e}")
                
                full_path.unlink()
            elif full_path.is_dir():
                import shutil
                shutil.rmtree(full_path)
            else:
                raise Exception("Path is neither file nor directory")
        except Exception as e:
            raise Exception(f"Error deleting: {e}")
    
    try:
        await asyncio.to_thread(_delete_file_sync)
        
        # Invalidate file cache
        try:
            from app.routers.files import get_file_cache
            cache = get_file_cache(db)
            cache.invalidate(f"{username}:")
            # Invalidate parent directory cache
            if file_path:
                parent_path = '/'.join(file_path.split('/')[:-1]) if '/' in file_path else ""
                cache.invalidate(f"{username}:{parent_path}")
        except Exception as e:
            logger.warning(f"Failed to invalidate cache: {e}")
        
        return {"message": "File deleted successfully"}
    except Exception as e:
        logger.error(f"Error deleting file: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/list-files")
async def list_files(
    request: FastAPIRequest,
    username: str = Query(..., description="Username"),
    path: str = Query("", description="Directory path relative to user root"),
    depth: int = Query(1, description="Listing depth: 1=immediate children, >1=recursive"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_optional)
):
    """
    List files and directories. Called by client nodes when proxying file listings.
    Only accessible on storage server node.
    depth=1: immediate children only (default)
    depth>1: recursive listing of all descendants
    """
    # Decode path: unquote then + as space (query string style)
    from urllib.parse import unquote
    path = (unquote(path) if path else "").replace("+", " ").strip("/")

    logger.info(f"[Storage API] list_files called: username={username}, path={path}, depth={depth}")

    # Check if this is a server-to-server request (load-balanced from another posterchanai node)
    is_server_request = lb_auth.is_internal(request)
    if not is_server_request and current_user is None:
        raise HTTPException(status_code=401, detail="Not authenticated")
    
    if not is_server_request:
        # Verify username matches for user requests
        if current_user.username != username:
            raise HTTPException(status_code=403, detail="Access denied")
    
    storage = StorageService(db)
    user_path = storage.get_user_path(username)
    logger.info(f"[Storage API] user_path={user_path}")

    # Handle path
    target_path = user_path
    if path:
        try:
            safe_path = Path(*[_sanitize_path_component(p) for p in path.split('/') if p])
            target_path = user_path / safe_path
            logger.info(f"[Storage API] target_path={target_path}, exists={target_path.exists()}")
            if not _validate_path_within_base(target_path, user_path):
                raise HTTPException(status_code=403, detail="Access denied")
        except ValueError as e:
            raise HTTPException(status_code=400, detail=f"Invalid path: {e}")

    if not target_path.exists():
        # Case-insensitive fallback: e.g. "Photos" -> "photos" on Linux
        path_parts = [p for p in path.split('/') if p]
        if len(path_parts) == 1 and user_path.is_dir():
            want = path_parts[0]
            for child in user_path.iterdir():
                if child.is_dir() and child.name.lower() == want.lower():
                    target_path = child
                    path = child.name  # use actual name so client can use it for view/download
                    logger.info(f"[Storage API] Resolved path case-insensitively: {want!r} -> {path!r}")
                    break
        if not target_path.exists():
            logger.warning(f"[Storage API] Path not found: {target_path}")
            raise HTTPException(status_code=404, detail="Path not found")
    if not target_path.is_dir():
        raise HTTPException(status_code=400, detail="Path is not a directory")
    
    # List directory
    def _list_sync():
        items = []

        if depth == 1:
            # Immediate children only
            for item in sorted(target_path.iterdir()):
                try:
                    # Use stat() to get file info - more reliable than separate calls
                    stat = item.stat()

                    # Check if it's a directory - is_dir() is the authoritative check
                    is_dir = item.is_dir()

                    # Double-check: if it's a symlink, resolve it
                    if item.is_symlink():
                        try:
                            resolved = item.resolve()
                            is_dir = resolved.is_dir()
                        except Exception:
                            # If symlink is broken, use original check
                            pass

                    # Get file size - directories can have non-zero size on some filesystems
                    # Always trust is_dir() - it's the correct filesystem check
                    file_size = stat.st_size

                    item_path = item.relative_to(user_path).as_posix()

                    item_info = {
                        "name": item.name,
                        "path": item_path,
                        "is_directory": is_dir,
                        "size": file_size if not is_dir else 0,
                        "modified": stat.st_mtime,
                        "is_external": False,
                    }
                    items.append(item_info)
                except Exception as e:
                    logger.warning(f"Error reading item {item}: {e}")
                    continue
        else:
            # Recursive listing for depth > 1
            for root, dirs, files in os.walk(target_path):
                root_path = Path(root)

                # Add directories
                for dir_name in sorted(dirs):
                    try:
                        dir_item = root_path / dir_name
                        stat = dir_item.stat()
                        item_path = dir_item.relative_to(user_path).as_posix()

                        item_info = {
                            "name": dir_name,
                            "path": item_path,
                            "is_directory": True,
                            "size": 0,
                            "modified": stat.st_mtime,
                            "is_external": False,
                        }
                        items.append(item_info)
                    except Exception as e:
                        logger.warning(f"Error reading directory {dir_name}: {e}")
                        continue

                # Add files
                for file_name in sorted(files):
                    try:
                        file_item = root_path / file_name
                        stat = file_item.stat()
                        item_path = file_item.relative_to(user_path).as_posix()

                        item_info = {
                            "name": file_name,
                            "path": item_path,
                            "is_directory": False,
                            "size": stat.st_size,
                            "modified": stat.st_mtime,
                            "is_external": False,
                        }
                        items.append(item_info)
                    except Exception as e:
                        logger.warning(f"Error reading file {file_name}: {e}")
                        continue

        return items
    
    items = await asyncio.to_thread(_list_sync)
    
    # Calculate usage
    def _calc_usage():
        total = 0
        for root, dirs, files in os.walk(user_path):
            for f in files:
                try:
                    total += (Path(root) / f).stat().st_size
                except Exception:
                    pass
        return total
    
    current_usage = await asyncio.to_thread(_calc_usage)
    
    return {
        "items": items,
        "path": path,
        "current_usage": current_usage,
        "quota": 0  # Quota is managed on main server
    }


@router.get("/view-file")
async def view_file(
    request: FastAPIRequest,
    username: str = Query(...),
    file_path: str = Query(...),
    download: bool = Query(False, description="If true, return as attachment (download) instead of inline"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_optional)
):
    """
    View/download a file. Called by client nodes when proxying file view requests.
    Only accessible on storage server node.
    """
    # Check if this is a server-to-server request (load-balanced from another posterchanai node)
    is_server_request = lb_auth.is_internal(request)
    if not is_server_request and current_user is None:
        raise HTTPException(status_code=401, detail="Not authenticated")
    
    if not is_server_request:
        # Verify username matches for user requests
        if current_user.username != username:
            raise HTTPException(status_code=403, detail="Access denied")
    
    storage = StorageService(db)
    user_path = storage.get_user_path(username)

    # Decode query param: unquote first (%2B -> +), then + as space (query string style), normalize slashes
    from urllib.parse import unquote
    file_path = unquote(file_path).replace("+", " ").replace("\\", "/").strip("/")
    if not file_path:
        raise HTTPException(status_code=400, detail="Invalid file path: empty path")

    # Sanitize and validate path
    try:
        safe_path = Path(*[_sanitize_path_component(p) for p in file_path.split('/') if p])
        full_path = user_path / safe_path
    except ValueError as e:
        raise HTTPException(status_code=400, detail=f"Invalid path: {e}")
    
    # Validate path is within user directory
    if not _validate_path_within_base(full_path, user_path):
        raise HTTPException(status_code=403, detail="Access denied")
    
    if not full_path.exists() or not full_path.is_file():
        logger.warning(f"[STORAGE] view-file 404: path={file_path!r} resolved={full_path!s} (user={username})")
        raise HTTPException(status_code=404, detail=f"File not found: {file_path}")
    
    # Check if this is a video file - transcode on-the-fly for bandwidth savings (inline only)
    from app.services.thumbnail_service import is_video_file
    import subprocess
    from starlette.responses import StreamingResponse
    
    # For videos: only transcode when streaming inline. For download=1 serve original to avoid ffmpeg failures.
    if is_video_file(full_path) and not download:
        logger.info(f"[STORAGE] Streaming transcoded video on-the-fly: {full_path.name}")
        
        # Transcode to stdout and stream directly to client
        # Using H.264 video + AAC audio for web compatibility and bandwidth savings
        ffmpeg_cmd = [
            'ffmpeg',
            '-i', str(full_path),
            '-c:v', 'libx264',           # H.264 codec for video
            '-preset', 'veryfast',       # Fast encoding for real-time streaming
            '-crf', '23',                # Quality (18-28, lower = better)
            '-maxrate', '2M',            # Max bitrate for bandwidth control
            '-bufsize', '4M',            # Buffer size
            '-vf', 'scale=trunc(iw/2)*2:trunc(ih/2)*2',  # Ensure even dimensions
            '-c:a', 'aac',               # AAC audio codec
            '-b:a', '128k',              # Audio bitrate
            '-movflags', 'frag_keyframe+empty_moov+faststart',  # Enable streaming
            '-f', 'mp4',                 # MP4 container
            'pipe:1'                     # Output to stdout
        ]
        
        try:
            # Start ffmpeg process to transcode and stream
            process = subprocess.Popen(
                ffmpeg_cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=10**8  # Large buffer for smooth streaming
            )
            
            # Stream the transcoded output
            def stream_transcoded():
                try:
                    while True:
                        chunk = process.stdout.read(65536)  # 64KB chunks
                        if not chunk:
                            break
                        yield chunk
                finally:
                    process.stdout.close()
                    process.wait()
            
            safe_name = ascii_safe_header_filename(full_path.stem + ".mp4")
            content_disp = f'attachment; filename="{safe_name}"' if download else f'inline; filename="{safe_name}"'
            return StreamingResponse(
                stream_transcoded(),
                media_type='video/mp4',
                headers={
                    'Accept-Ranges': 'none',  # No range requests for transcoded stream
                    'Content-Disposition': content_disp
                }
            )
        except Exception as transcode_err:
            logger.error(f"[STORAGE] On-the-fly transcode error: {transcode_err}, serving original")
            # Fall through to serve original video if transcoding fails
    
    # For images and non-transcoded videos, serve the original file
    video_path_to_serve = full_path
    
    # Determine media type
    suffix = video_path_to_serve.suffix.lower()
    media_types = {
        '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.png': 'image/png',
        '.gif': 'image/gif', '.webp': 'image/webp', '.bmp': 'image/bmp',
        '.pdf': 'application/pdf',
        '.txt': 'text/plain', '.md': 'text/markdown',
        '.html': 'text/html', '.css': 'text/css', '.js': 'text/javascript',
        '.mp4': 'video/mp4', '.webm': 'video/webm', '.mov': 'video/quicktime',
        '.avi': 'video/x-msvideo', '.mkv': 'video/x-matroska',
    }
    media_type = media_types.get(suffix, 'application/octet-stream')
    
    # When download=1 use attachment; otherwise inline for images/videos
    # Use ASCII-safe filename in headers to avoid latin-1 encode errors (e.g. U+2019)
    headers = {}
    safe_filename = ascii_safe_header_filename(full_path.name)
    if download:
        headers['Content-Disposition'] = f'attachment; filename="{safe_filename}"'
    elif suffix in ['.jpg', '.jpeg', '.png', '.gif', '.webp', '.bmp']:
        headers['Content-Disposition'] = 'inline'
    elif suffix in ['.mp4', '.webm', '.mov', '.avi', '.mkv']:
        headers['Content-Disposition'] = 'inline'
        # Enable range requests for video streaming
        headers['Accept-Ranges'] = 'bytes'
    
    return FileResponse(
        video_path_to_serve,
        media_type=media_type,
        filename=safe_filename,
        headers=headers
    )


@router.get("/thumbnail-file")
async def thumbnail_file(
    request: FastAPIRequest,
    username: str = Query(...),
    file_path: str = Query(...),
    size: int = Query(200),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_optional)
):
    """
    Get thumbnail for an image file. Called by client nodes when proxying thumbnail requests.
    Only accessible on storage server node.
    """
    # Check if this is a server-to-server request (load-balanced from another posterchanai node)
    is_server_request = lb_auth.is_internal(request)
    if not is_server_request and current_user is None:
        raise HTTPException(status_code=401, detail="Not authenticated")
    
    if not is_server_request:
        # Verify username matches for user requests
        if current_user.username != username:
            raise HTTPException(status_code=403, detail="Access denied")
    
    storage = StorageService(db)
    user_path = storage.get_user_path(username)
    
    # Sanitize and validate path
    try:
        safe_path = Path(*[_sanitize_path_component(p) for p in file_path.split('/') if p])
        full_path = user_path / safe_path
    except ValueError as e:
        raise HTTPException(status_code=400, detail=f"Invalid path: {e}")
    
    # Validate path is within user directory
    if not _validate_path_within_base(full_path, user_path):
        raise HTTPException(status_code=403, detail="Access denied")
    
    if not full_path.exists() or not full_path.is_file():
        raise HTTPException(status_code=404, detail="File not found")
    
    # Try to use stored thumbnail first, then generate if needed
    from app.routers.files import generate_thumbnail
    from app.services.thumbnail_service import (
        get_thumbnail_if_exists, 
        generate_thumbnail_for_image,
        generate_thumbnail_for_video_file,
        is_image_file,
        is_video_file
    )
    try:
        # Check if file is image or video
        is_image = is_image_file(full_path)
        is_video = is_video_file(full_path)
        
        if not is_image and not is_video:
            raise HTTPException(status_code=400, detail="File is not an image or video")
        
        # Prefer stored thumbnail; for images only, generate on-the-fly if missing (Android Photos / web)
        thumbnail_path = get_thumbnail_if_exists(user_path, full_path)
        if thumbnail_path and thumbnail_path.exists():
            thumbnail_data = await asyncio.to_thread(generate_thumbnail, thumbnail_path, (size, size))
            if thumbnail_data:
                return JSONResponse({"thumbnail": thumbnail_data})
        # No stored thumbnail: generate on-the-fly for images only
        if is_image:
            thumbnail_data = await asyncio.to_thread(generate_thumbnail, full_path, (size, size))
            if thumbnail_data:
                return JSONResponse({"thumbnail": thumbnail_data})
        raise HTTPException(status_code=404, detail="Thumbnail not found.")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error generating thumbnail: {e}")


@router.post("/move-files")
async def move_files(
    request: FastAPIRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_optional)
):
    """
    Move files or folders. Called by client nodes when proxying file move operations.
    Only accessible on storage server node.
    """
    # Check if this is a server-to-server request

    # Parse JSON body (server-to-server requests use JSON)
    try:
        body = await request.json()
        username = body.get("username")
        file_paths = body.get("file_paths", [])
        destination = body.get("destination", "")
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Invalid request body: {e}")
    
    # Check if this is a server-to-server request (load-balanced from another posterchanai node)
    is_server_request = lb_auth.is_internal(request)
    if not is_server_request and current_user is None:
        raise HTTPException(status_code=401, detail="Not authenticated")
    
    if not is_server_request:
        # Verify username matches for user requests
        if current_user.username != username:
            raise HTTPException(status_code=403, detail="Access denied")
    
    storage = StorageService(db)
    user_path = storage.get_user_path(username)
    
    # Sanitize and validate destination path
    try:
        if destination and destination.strip():
            safe_dest = Path(*[_sanitize_path_component(p) for p in destination.split('/') if p])
            dest_path = user_path / safe_dest
        else:
            # Empty destination means root directory
            dest_path = user_path
        
        # Validate destination is within user directory
        if not _validate_path_within_base(dest_path, user_path):
            raise HTTPException(status_code=403, detail="Invalid destination path")
        
        # Destination must exist and be a directory
        if not dest_path.exists():
            raise HTTPException(status_code=404, detail="Destination directory does not exist")
        
        if not dest_path.is_dir():
            raise HTTPException(status_code=400, detail="Destination must be a directory")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=f"Invalid destination path: {e}")
    
    # Run move operations in thread pool
    def _move_files_sync():
        moved = []
        errors = []
        
        for file_path in file_paths:
            try:
                # Sanitize source path
                safe_path = Path(*[_sanitize_path_component(p) for p in file_path.split('/') if p])
                source_path = user_path / safe_path
                
                # Validate source is within user directory
                if not _validate_path_within_base(source_path, user_path):
                    errors.append(f"{file_path}: Access denied")
                    continue
                
                if not source_path.exists():
                    errors.append(f"{file_path}: Not found")
                    continue
                
                # Check if destination already has a file/folder with the same name
                target_path = dest_path / source_path.name
                if target_path.exists():
                    errors.append(f"{file_path}: Destination already contains '{source_path.name}'")
                    continue
                
                # Move the file/folder
                import shutil
                shutil.move(str(source_path), str(target_path))
                moved.append(file_path)
            except Exception as e:
                errors.append(f"{file_path}: {str(e)}")
        
        return moved, errors
    
    try:
        moved, errors = await asyncio.to_thread(_move_files_sync)
        
        # Invalidate file cache
        try:
            from app.routers.files import get_file_cache
            cache = get_file_cache(db)
            cache.invalidate(f"{username}:")
            # Invalidate both source and destination directories
            for file_path in file_paths:
                parent_path = '/'.join(file_path.split('/')[:-1]) if '/' in file_path else ""
                cache.invalidate(f"{username}:{parent_path}")
            cache.invalidate(f"{username}:{destination}")
        except Exception as e:
            logger.warning(f"Failed to invalidate cache: {e}")
        
        return {
            "message": f"Moved {len(moved)} item(s)",
            "moved": moved,
            "errors": errors
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/save-text-file")
async def save_text_file(
    request: FastAPIRequest,
    username: str = Form(...),
    path: str = Form(...),
    content: str = Form(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_optional)
):
    """
    Save a text file to a specific path. Used by DAV storage proxy for .ics and .vcf files.
    Only accessible on storage server node.
    """
    # Check if this is a server-to-server request (load-balanced from another posterchanai node)
    is_server_request = lb_auth.is_internal(request)
    if not is_server_request and current_user is None:
        raise HTTPException(status_code=401, detail="Not authenticated")
    
    if not is_server_request:
        # Verify username matches for user requests
        if current_user.username != username:
            raise HTTPException(status_code=403, detail="Access denied")
    
    storage = StorageService(db)
    user_path = storage.get_user_path(username)
    
    # Sanitize and validate path
    try:
        safe_path = Path(*[_sanitize_path_component(p) for p in path.split('/') if p])
        full_path = user_path / safe_path
        
        # Validate path is within user directory
        if not _validate_path_within_base(full_path, user_path):
            raise HTTPException(status_code=403, detail="Access denied")
        
        # Create parent directories if needed
        full_path.parent.mkdir(parents=True, exist_ok=True)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=f"Invalid path: {e}")
    
    # Run file write in thread pool
    def _save_text_file_sync():
        try:
            full_path.write_text(content, encoding='utf-8')
            return str(full_path.relative_to(user_path))
        except Exception as e:
            raise Exception(f"Error saving file: {e}")
    
    try:
        relative_path = await asyncio.to_thread(_save_text_file_sync)
        
        # Invalidate file cache
        try:
            from app.routers.files import get_file_cache
            cache = get_file_cache(db)
            parent_path = '/'.join(path.split('/')[:-1]) if '/' in path else ""
            cache.invalidate(f"{username}:")
            if parent_path:
                cache.invalidate(f"{username}:{parent_path}")
        except Exception as e:
            logger.warning(f"Failed to invalidate cache: {e}")
        
        return {"message": "File saved successfully", "path": relative_path}
    except Exception as e:
        logger.error(f"Error saving text file: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/delete-files-bulk")
async def delete_files_bulk(
    request: FastAPIRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_optional)
):
    """
    Delete multiple files or directories. Called by client nodes when proxying bulk delete operations.
    Only accessible on storage server node.
    """
    # Check if this is a server-to-server request

    # Parse JSON body (server-to-server requests use JSON)
    try:
        body = await request.json()
        username = body.get("username")
        file_paths = body.get("file_paths", [])
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Invalid request body: {e}")
    
    # Check if this is a server-to-server request (load-balanced from another posterchanai node)
    is_server_request = lb_auth.is_internal(request)
    if not is_server_request and current_user is None:
        raise HTTPException(status_code=401, detail="Not authenticated")
    
    if not is_server_request:
        # Verify username matches for user requests
        if current_user.username != username:
            raise HTTPException(status_code=403, detail="Access denied")
    
    storage = StorageService(db)
    user_path = storage.get_user_path(username)
    
    # Run deletions in thread pool
    def _delete_files_sync():
        deleted = []
        errors = []
        
        logger.info(f"[STORAGE] Starting deletion of {len(file_paths)} file(s) for user {username}")
        
        for file_path in file_paths:
            try:
                logger.debug(f"[STORAGE] Processing deletion of: {file_path}")
                # Sanitize and validate path
                safe_path = Path(*[_sanitize_path_component(p) for p in file_path.split('/') if p])
                full_path = user_path / safe_path
                
                # Validate path is within user directory
                if not _validate_path_within_base(full_path, user_path):
                    error_msg = f"{file_path}: Access denied"
                    logger.warning(f"[STORAGE] {error_msg}")
                    errors.append(error_msg)
                    continue
                
                if not full_path.exists():
                    error_msg = f"{file_path}: Not found"
                    logger.warning(f"[STORAGE] {error_msg}")
                    errors.append(error_msg)
                    continue
                
                # Delete the file or directory
                if full_path.is_file():
                    logger.info(f"[STORAGE] Deleting file: {full_path}")
                    # Delete thumbnail if it's an image
                    try:
                        from app.services.thumbnail_service import is_image_file, delete_thumbnail
                        if is_image_file(full_path):
                            delete_thumbnail(user_path, full_path)
                            logger.debug(f"[STORAGE] Deleted thumbnail for {file_path}")
                    except Exception as e:
                        logger.warning(f"Failed to delete thumbnail for {file_path}: {e}")
                    
                    full_path.unlink()
                    logger.info(f"[STORAGE] ✓ Successfully deleted file: {file_path}")
                elif full_path.is_dir():
                    logger.info(f"[STORAGE] Deleting directory: {full_path}")
                    import shutil
                    shutil.rmtree(full_path)
                    logger.info(f"[STORAGE] ✓ Successfully deleted directory: {file_path}")
                else:
                    error_msg = f"{file_path}: Neither file nor directory"
                    logger.warning(f"[STORAGE] {error_msg}")
                    errors.append(error_msg)
                    continue
                
                deleted.append(file_path)
            except Exception as e:
                error_msg = f"{file_path}: {str(e)}"
                logger.error(f"[STORAGE] Error deleting {file_path}: {e}", exc_info=True)
                errors.append(error_msg)
        
        logger.info(f"[STORAGE] Deletion complete: {len(deleted)} deleted, {len(errors)} errors")
        return deleted, errors
    
    try:
        deleted, errors = await asyncio.to_thread(_delete_files_sync)
        
        # Invalidate file cache
        try:
            from app.routers.files import get_file_cache
            cache = get_file_cache(db)
            cache.invalidate(f"{username}:")
            # Invalidate parent directories
            for file_path in file_paths:
                parent_path = '/'.join(file_path.split('/')[:-1]) if '/' in file_path else ""
                cache.invalidate(f"{username}:{parent_path}")
        except Exception as e:
            logger.warning(f"Failed to invalidate cache: {e}")
        
        return {
            "message": f"Deleted {len(deleted)} item(s)",
            "deleted": deleted,
            "errors": errors
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/search")
async def search_files_storage(
    query: str = Query(..., description="Search query (filename or path)"),
    username: str = Query(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_optional),
    request: FastAPIRequest = None
):
    """Search for files by name or path on storage server. Returns matching files with metadata."""
    # Check if this is a server-to-server request
    # Check if this is a server-to-server request (load-balanced from another posterchanai node)
    is_server_request = lb_auth.is_internal(request)
    if not is_server_request and current_user is None:
        raise HTTPException(status_code=401, detail="Not authenticated")
    
    if not is_server_request:
        # Verify username matches for user requests
        if current_user and current_user.username != username:
            raise HTTPException(status_code=403, detail="Access denied")
    
    storage = StorageService(db)
    user_path = storage.get_user_path(username)
    
    # Run file search in thread pool to prevent blocking
    def _search_files_sync():
        """Synchronous file search function."""
        results = []
        query_lower = query.lower()
        
        try:
            # Recursively search through user's files
            for item in user_path.rglob('*'):
                try:
                    # Skip directories
                    if item.is_dir():
                        continue
                    
                    # Check if filename or path matches query
                    filename = item.name.lower()
                    relative_path = str(item.relative_to(user_path)).lower()
                    
                    if query_lower in filename or query_lower in relative_path:
                        stat = item.stat()
                        is_dir = item.is_dir()
                        
                        item_info = {
                            "name": item.name,
                            "path": str(item.relative_to(user_path)),
                            "is_directory": is_dir,
                            "size": stat.st_size if not is_dir else 0,
                            "modified": stat.st_mtime,
                        }
                        
                        results.append(item_info)
                except Exception as e:
                    logger.warning(f"Error processing file {item}: {e}")
                    continue
        except Exception as e:
            logger.error(f"Error searching files: {e}")
            raise Exception(f"Error searching files: {e}")
        
        # Sort by modified time (newest first)
        results.sort(key=lambda x: x.get('modified', 0), reverse=True)
        return results
    
    try:
        results = await asyncio.to_thread(_search_files_sync)
        return {
            "query": query,
            "results": results,
            "count": len(results)
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# Also register under /api/files for compatibility
@files_router.get("/search")
async def search_files_files(
    query: str = Query(..., description="Search query (filename or path)"),
    username: str = Query(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_optional),
    request: FastAPIRequest = None
):
    """Search endpoint under /api/files for compatibility with main server proxy."""
    return await search_files_storage(query, username, db, current_user, request)
