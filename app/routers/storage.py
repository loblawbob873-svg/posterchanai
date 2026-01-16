"""
Storage Router - Internal API endpoints for storage server operations.
These endpoints are called by client nodes when proxying file operations.
All blocking I/O operations are run in thread pools to prevent blocking.
"""
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form
from sqlalchemy.orm import Session
from app.database import get_db
from app.auth import get_current_user
from app.models import User
from app.services.storage_service import StorageService
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
    file: UploadFile = File(...),
    username: str = Form(...),
    note_id: int = Form(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    Save a note attachment file. Called by client nodes when proxying note attachment uploads.
    Only accessible on storage server node.
    """
    # Verify username matches
    if current_user.username != username:
        raise HTTPException(status_code=403, detail="Access denied")
    
    # Verify note belongs to user
    from app.models import Note
    note = db.query(Note).filter(
        Note.id == note_id,
        Note.user_id == current_user.id
    ).first()
    if not note:
        raise HTTPException(status_code=404, detail="Note not found")
    
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
    except Exception as e:
        logger.warning(f"Failed to invalidate cache: {e}")
    
    return {"filename": filename}
