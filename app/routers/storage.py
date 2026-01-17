"""
Storage Router - Internal API endpoints for storage server operations.
These endpoints are called by client nodes when proxying file operations.
All blocking I/O operations are run in thread pools to prevent blocking.
"""
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form, Request, Query
from fastapi import Request as FastAPIRequest
from starlette.requests import Request as StarletteRequest
from sqlalchemy.orm import Session
from pathlib import Path
import os
from app.database import get_db
from app.auth import get_current_user, get_current_user_optional
from app.models import User
from app.services.storage_service import StorageService, _sanitize_path_component, _validate_path_within_base
import logging
import asyncio

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/storage", tags=["storage"])


@router.post("/save-image")
async def save_image(
    file: UploadFile = File(...),
    username: str = Form(...),
    conversation_id: int = Form(...),
    prefix: str = Form("img"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    Save an image file. Called by client nodes when proxying file uploads.
    Only accessible on storage server node.
    """
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
    
    # Read file content
    content = await file.read()
    
    # Convert to base64 for storage service
    import base64
    image_base64 = base64.b64encode(content).decode('utf-8')
    
    # Run blocking file I/O in thread pool to prevent blocking other requests
    def _save_image_sync():
        storage = StorageService(db)
        return storage.save_image(username, conversation_id, image_base64, prefix)
    
    file_path = await asyncio.to_thread(_save_image_sync)
    
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
    def _save_avatar_sync():
        storage = StorageService(db)
        return storage.save_avatar(username, content, ext)
    
    filename = await asyncio.to_thread(_save_avatar_sync)
    
    # Avatars don't affect file listings, so no cache invalidation needed
    
    return {"filename": filename}


@router.post("/save-file")
async def save_file(
    file: UploadFile = File(...),
    username: str = Form(...),
    conversation_id: int = Form(...),
    original_name: str = Form("file.txt"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    Save a text file. Called by client nodes when proxying file uploads.
    Only accessible on storage server node.
    """
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
            return storage.save_file(username, conversation_id, text_content, original_name)
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


@router.post("/save-note-attachment")
async def save_note_attachment(
    request: FastAPIRequest,
    file: UploadFile = File(...),
    username: str = Form(...),
    note_id: int = Form(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_optional)
):
    """
    Save a note attachment file. Called by client nodes when proxying note attachment uploads.
    Only accessible on storage server node.
    
    Note: For proxied requests, current_user may be None if using server token auth.
    In that case, we trust the main server and skip user verification.
    """
    # Check if this is a server-to-server request
    # Either current_user is None OR we have a valid storage_server_token
    is_server_request = current_user is None
    if not is_server_request:
        # Check if this is a server token request
        from app.models import Setting
        storage_server_token = db.query(Setting).filter(Setting.key == "storage_server_token").first()
        if storage_server_token and storage_server_token.value:
            # Check if the request has the server token
            auth_header = request.headers.get("Authorization", "")
            if auth_header.startswith("Bearer ") and auth_header[7:] == storage_server_token.value:
                is_server_request = True
    
    if not is_server_request:
        # Verify username matches for user requests
        if current_user.username != username:
            raise HTTPException(status_code=403, detail="Access denied")
        
        # Verify note belongs to user (for user requests)
        from app.models import Note
        note = db.query(Note).filter(
            Note.id == note_id,
            Note.user_id == current_user.id
        ).first()
        if not note:
            raise HTTPException(status_code=404, detail="Note not found")
    # For server-to-server requests, we skip note verification (main server already verified)
    
    # Read file content
    content = await file.read()
    
    # Get original filename
    original_name = file.filename or "attachment"
    
    # Run blocking file I/O - handle both async and sync contexts
    # Bypass proxy since we're already on the storage server endpoint
    def _save_attachment_sync():
        storage = StorageService(db)
        return storage.save_note_attachment(username, note_id, content, original_name, bypass_proxy=True)
    
    # Try to get running event loop
    try:
        loop = asyncio.get_running_loop()
        # Use executor to run blocking I/O
        filename = await loop.run_in_executor(None, _save_attachment_sync)
    except RuntimeError:
        # No running event loop - we're likely in a thread pool
        # Just run synchronously since we're already in a separate thread
        filename = _save_attachment_sync()
    
    # Invalidate file cache for notes directory (non-blocking)
    try:
        from app.routers.files import get_file_cache
        cache = get_file_cache(db)
        cache.invalidate(f"{username}:")
        cache.invalidate(f"{username}:notes")
        cache.invalidate(f"{username}:notes/{note_id}")
    except Exception as e:
        logger.warning(f"Failed to invalidate cache: {e}")
    
    return {"filename": filename}


@router.post("/save-mail-attachment")
async def save_mail_attachment(
    file: UploadFile = File(...),
    username: str = Form(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    Save a mail attachment file. Called by client nodes when proxying mail attachment saves.
    Only accessible on storage server node.
    """
    # Verify username matches
    if current_user.username != username:
        raise HTTPException(status_code=403, detail="Access denied")
    
    # Read file content
    content = await file.read()
    
    # Get original filename from form or file
    original_name = file.filename or "attachment"
    
    # Run blocking file I/O in thread pool to prevent blocking other requests
    def _save_mail_attachment_sync():
        storage = StorageService(db)
        return storage.save_mail_attachment(username, content, original_name, bypass_proxy=True)
    
    filename = await asyncio.to_thread(_save_mail_attachment_sync)
    
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
    
    Note: For proxied requests, current_user may be None if using server token auth.
    In that case, we trust the main server and skip user verification.
    """
    # Check if this is a server-to-server request
    is_server_request = current_user is None
    if not is_server_request:
        # Check if this is a server token request
        from app.models import Setting
        storage_server_token = db.query(Setting).filter(Setting.key == "storage_server_token").first()
        if storage_server_token and storage_server_token.value:
            # Check if the request has the server token
            auth_header = request.headers.get("Authorization", "")
            if auth_header.startswith("Bearer ") and auth_header[7:] == storage_server_token.value:
                is_server_request = True
        
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
        
        # Check if file already exists
        if full_file_path.exists():
            # Add number suffix
            base_name = full_file_path.stem
            extension = full_file_path.suffix
            counter = 1
            while full_file_path.exists():
                full_file_path = target_path / f"{base_name}_{counter}{extension}"
                counter += 1
        
        # Write file
        with open(full_file_path, 'wb') as f:
            f.write(content)
        
        return str(full_file_path.relative_to(user_path)), safe_filename
    
    relative_path, safe_filename = await asyncio.to_thread(_upload_file_sync)
    
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


@router.post("/delete-note-attachments")
async def delete_note_attachments(
    username: str = Form(...),
    note_id: int = Form(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Delete all attachments for a note (storage server endpoint)."""
    # Verify user owns this note
    if current_user.username != username:
        raise HTTPException(status_code=403, detail="Access denied")
    
    # Run blocking file I/O - handle both async and sync contexts
    # Bypass proxy since we're already on the storage server endpoint
    def _delete_attachments_sync():
        storage = StorageService(db)
        return storage.delete_note_attachments(username, note_id, bypass_proxy=True)
    
    # Try to get running event loop
    try:
        loop = asyncio.get_running_loop()
        # Use executor to run blocking I/O
        success = await loop.run_in_executor(None, _delete_attachments_sync)
    except RuntimeError:
        # No running event loop - we're likely in a thread pool
        # Just run synchronously since we're already in a separate thread
        success = _delete_attachments_sync()
    
    if success:
        # Invalidate file cache for notes directory (non-blocking)
        try:
            from app.routers.files import get_file_cache
            cache = get_file_cache(db)
            cache.invalidate(f"{username}:notes/{note_id}")
            cache.invalidate(f"{username}:notes")
        except Exception as e:
            logger.warning(f"Failed to invalidate cache: {e}")
        
        return {"success": True, "message": "Attachments deleted"}
    else:
        raise HTTPException(status_code=500, detail="Failed to delete attachments")


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
    # Check if this is a server-to-server request
    from app.models import Setting
    storage_server_token = db.query(Setting).filter(Setting.key == "storage_server_token").first()
    is_server_request = current_user is None
    
    if not is_server_request and storage_server_token and storage_server_token.value:
        # Check if the request has the server token
        auth_header = request.headers.get("Authorization", "")
        if auth_header.startswith("Bearer ") and auth_header[7:] == storage_server_token.value:
            is_server_request = True
    
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
                    stat = item.stat()
                    is_dir = item.is_dir()
                    item_path = str(item.relative_to(user_path))
                    
                    item_info = {
                        "name": item.name,
                        "path": item_path,
                        "is_directory": is_dir,
                        "size": stat.st_size if not is_dir else 0,
                        "modified": stat.st_mtime,
                        "is_external": False,
                    }
                    
                    # Generate thumbnail for images (skip for now to avoid circular import)
                    # Thumbnails can be generated on the client node if needed
                    
                    items.append(item_info)
                except Exception as e:
                    logger.warning(f"Error reading item {item}: {e}")
                    continue
        except Exception as e:
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
        
        return {
            "items": items,
            "path": path,
            "current_usage": current_usage,
            "quota": 0  # Will be set by the calling endpoint
        }
    except Exception as e:
        logger.error(f"Error listing files: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/list-files")
async def list_files(
    username: str = Query(..., description="Username"),
    path: str = Query("", description="Directory path relative to user root"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_optional)
):
    """
    List files and directories. Called by client nodes when proxying file listings.
    Only accessible on storage server node.
    """
    # Check if this is a server-to-server request
    from app.models import Setting
    storage_server_token = db.query(Setting).filter(Setting.key == "storage_server_token").first()
    is_server_request = current_user is None
    
    if not is_server_request and storage_server_token and storage_server_token.value:
        # Check if the request has the server token
        from fastapi import Request as FastAPIRequest
        request = FastAPIRequest
        auth_header = request.headers.get("Authorization", "")
        if auth_header.startswith("Bearer ") and auth_header[7:] == storage_server_token.value:
            is_server_request = True
    
    if not is_server_request:
        # Verify username matches for user requests
        if current_user.username != username:
            raise HTTPException(status_code=403, detail="Access denied")
    
    storage = StorageService(db)
    user_path = storage.get_user_path(username)
    
    # Handle path
    target_path = user_path
    if path:
        try:
            safe_path = Path(*[_sanitize_path_component(p) for p in path.split('/') if p])
            target_path = user_path / safe_path
            if not _validate_path_within_base(target_path, user_path):
                raise HTTPException(status_code=403, detail="Access denied")
        except ValueError as e:
            raise HTTPException(status_code=400, detail=f"Invalid path: {e}")
    
    if not target_path.exists():
        raise HTTPException(status_code=404, detail="Path not found")
    if not target_path.is_dir():
        raise HTTPException(status_code=400, detail="Path is not a directory")
    
    # List directory
    def _list_sync():
        items = []
        for item in sorted(target_path.iterdir()):
            try:
                stat = item.stat()
                is_dir = item.is_dir()
                item_path = str(item.relative_to(user_path))
                
                item_info = {
                    "name": item.name,
                    "path": item_path,
                    "is_directory": is_dir,
                    "size": stat.st_size if not is_dir else 0,
                    "modified": stat.st_mtime,
                    "is_external": False,
                }
                items.append(item_info)
            except Exception as e:
                logger.warning(f"Error reading item {item}: {e}")
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
                except:
                    pass
        return total
    
    current_usage = await asyncio.to_thread(_calc_usage)
    
    return {
        "items": items,
        "path": path,
        "current_usage": current_usage,
        "quota": 0  # Quota is managed on main server
    }
