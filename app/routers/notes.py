"""
Notes Router - API endpoints for notes management.
Supports both local storage and remote storage server proxying via storage_server_url.
"""
from fastapi import APIRouter, Depends, Request, HTTPException
from fastapi.responses import JSONResponse, FileResponse
from starlette.requests import Request as StarletteRequest
from sqlalchemy.orm import Session
from sqlalchemy import func, or_
from typing import List, Optional
from pathlib import Path
import logging
from pydantic import ValidationError

from app.database import get_db
from app.auth import get_current_user
from app.models import User, Note, NoteFolder, Setting
from app.schemas import (
    NoteCreate, NoteUpdate, NoteResponse,
    NoteFolderCreate, NoteFolderUpdate, NoteFolderResponse
)

router = APIRouter(prefix="/api/notes", tags=["notes"])
logger = logging.getLogger(__name__)


def _serialize_note_response(note: Note, folder_name: Optional[str] = None) -> dict:
    """
    Serialize a Note model to NoteResponse dict with fallback handling.
    Used across all endpoints for consistent error handling.
    """
    try:
        note_dict = NoteResponse.model_validate(note).model_dump()
    except (AttributeError, ValidationError):
        # Pydantic v1 fallback
        try:
            note_dict = NoteResponse.from_orm(note).dict()
        except Exception as e:
            logger.error(f"Error serializing note {note.id}: {e}", exc_info=True)
            # Fallback: manual dict construction
            note_dict = {
                "id": note.id,
                "title": note.title,
                "content": note.content or "",
                "folder_id": note.folder_id,
                "tags": note.tags,
                "attachments": note.attachments,
                "is_pinned": note.is_pinned,
                "created_at": note.created_at,
                "updated_at": note.updated_at
            }
    
    result = {**note_dict}
    if folder_name is not None:
        result["folder_name"] = folder_name
    return result


# Note Folders endpoints

@router.get("/folders", response_model=List[NoteFolderResponse])
async def get_folders(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Get all note folders for the current user."""
    folders = db.query(NoteFolder).filter(NoteFolder.user_id == current_user.id).all()
    
    result = []
    for folder in folders:
        notes_count = db.query(func.count(Note.id)).filter(Note.folder_id == folder.id).scalar() or 0
        
        # Use model_validate for Pydantic v2, fallback to from_orm for v1
        try:
            folder_dict = NoteFolderResponse.model_validate(folder).model_dump()
        except AttributeError:
            # Pydantic v1 fallback
            folder_dict = NoteFolderResponse.from_orm(folder).dict()
        
        result.append({
            **folder_dict,
            "notes_count": notes_count
        })
    
    return result


@router.post("/folders", response_model=NoteFolderResponse)
async def create_folder(
    folder_data: NoteFolderCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Create a new note folder."""
    # Validate parent folder if provided
    if folder_data.parent_id:
        parent = db.query(NoteFolder).filter(
            NoteFolder.id == folder_data.parent_id,
            NoteFolder.user_id == current_user.id
        ).first()
        if not parent:
            raise HTTPException(status_code=404, detail="Parent folder not found")
    
    folder = NoteFolder(
        user_id=current_user.id,
        name=folder_data.name,
        parent_id=folder_data.parent_id
    )
    db.add(folder)
    db.commit()
    db.refresh(folder)
    
    # Use model_validate for Pydantic v2, fallback to from_orm for v1
    try:
        return NoteFolderResponse.model_validate(folder)
    except AttributeError:
        return NoteFolderResponse.from_orm(folder)


@router.put("/folders/{folder_id}", response_model=NoteFolderResponse)
async def update_folder(
    folder_id: int,
    folder_data: NoteFolderUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Update a note folder."""
    folder = db.query(NoteFolder).filter(
        NoteFolder.id == folder_id,
        NoteFolder.user_id == current_user.id
    ).first()
    
    if not folder:
        raise HTTPException(status_code=404, detail="Folder not found")
    
    # Validate parent folder if provided
    if folder_data.parent_id is not None:
        if folder_data.parent_id == folder_id:
            raise HTTPException(status_code=400, detail="Folder cannot be its own parent")
        if folder_data.parent_id != 0:  # 0 means no parent
            parent = db.query(NoteFolder).filter(
                NoteFolder.id == folder_data.parent_id,
                NoteFolder.user_id == current_user.id
            ).first()
            if not parent:
                raise HTTPException(status_code=404, detail="Parent folder not found")
    
    if folder_data.name is not None:
        folder.name = folder_data.name
    if folder_data.parent_id is not None:
        folder.parent_id = folder_data.parent_id if folder_data.parent_id != 0 else None
    
    db.commit()
    db.refresh(folder)
    
    # Use model_validate for Pydantic v2, fallback to from_orm for v1
    try:
        return NoteFolderResponse.model_validate(folder)
    except AttributeError:
        return NoteFolderResponse.from_orm(folder)


@router.delete("/folders/{folder_id}")
async def delete_folder(
    folder_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Delete a note folder (moves notes to root if any)."""
    folder = db.query(NoteFolder).filter(
        NoteFolder.id == folder_id,
        NoteFolder.user_id == current_user.id
    ).first()
    
    if not folder:
        raise HTTPException(status_code=404, detail="Folder not found")
    
    # Move notes to root (folder_id = None)
    db.query(Note.id).filter(Note.folder_id == folder_id).update({"folder_id": None})
    
    db.delete(folder)
    db.commit()
    
    return {"success": True, "message": "Folder deleted"}


# Notes endpoints

@router.get("", response_model=List[NoteResponse])
async def get_notes(
    folder_id: Optional[int] = None,
    search: Optional[str] = None,
    tag: Optional[str] = None,
    limit: Optional[int] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Get notes for the current user. Can filter by folder, search query, or tag."""
    try:
        query = db.query(Note).filter(Note.user_id == current_user.id)
        
        if folder_id is not None:
            if folder_id == 0:  # 0 means root (no folder)
                query = query.filter(Note.folder_id.is_(None))
            else:
                query = query.filter(Note.folder_id == folder_id)
        
        if search:
            search_term = f"%{search}%"
            query = query.filter(
                or_(
                    Note.title.ilike(search_term),
                    Note.content.ilike(search_term)
                )
            )
        
        if tag:
            query = query.filter(Note.tags.contains(tag))
        
        # Order by pinned first, then updated_at desc
        query = query.order_by(Note.is_pinned.desc(), Note.updated_at.desc())
        
        # Apply limit if specified (for autocomplete)
        if limit:
            query = query.limit(limit)
        
        notes = query.all()
        
        result = []
        for note in notes:
            folder_name = None
            if note.folder_id:
                folder = db.query(NoteFolder).filter(NoteFolder.id == note.folder_id).first()
                folder_name = folder.name if folder else None
            
            result.append(_serialize_note_response(note, folder_name))
        
        return result
    except Exception as e:
        logger.error(f"Error fetching notes: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error fetching notes: {str(e)}")


@router.get("/{note_id}", response_model=NoteResponse)
async def get_note(
    note_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Get a single note by ID."""
    note = db.query(Note).filter(
        Note.id == note_id,
        Note.user_id == current_user.id
    ).first()
    
    if not note:
        raise HTTPException(status_code=404, detail="Note not found")
    
    folder_name = None
    if note.folder_id:
        folder = db.query(NoteFolder).filter(NoteFolder.id == note.folder_id).first()
        folder_name = folder.name if folder else None
    
    return _serialize_note_response(note, folder_name)


@router.post("", response_model=NoteResponse)
async def create_note(
    note_data: NoteCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Create a new note."""
    try:
        # Validate folder if provided
        if note_data.folder_id:
            folder = db.query(NoteFolder).filter(
                NoteFolder.id == note_data.folder_id,
                NoteFolder.user_id == current_user.id
            ).first()
            if not folder:
                raise HTTPException(status_code=404, detail="Folder not found")
        
        note = Note(
            user_id=current_user.id,
            title=note_data.title,
            content=note_data.content,
            folder_id=note_data.folder_id,
            tags=note_data.tags,
            is_pinned=note_data.is_pinned
        )
        db.add(note)
        db.commit()
        db.refresh(note)
        
        folder_name = None
        if note.folder_id:
            folder = db.query(NoteFolder).filter(NoteFolder.id == note.folder_id).first()
            folder_name = folder.name if folder else None
        
        return _serialize_note_response(note, folder_name)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error creating note: {e}", exc_info=True)
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Error creating note: {str(e)}")


@router.put("/{note_id}", response_model=NoteResponse)
async def update_note(
    note_id: int,
    note_data: NoteUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Update a note."""
    try:
        note = db.query(Note).filter(
            Note.id == note_id,
            Note.user_id == current_user.id
        ).first()
        
        if not note:
            raise HTTPException(status_code=404, detail="Note not found")
        
        # Validate folder if provided
        if note_data.folder_id is not None:
            if note_data.folder_id == 0:  # 0 means no folder
                note.folder_id = None
            else:
                folder = db.query(NoteFolder).filter(
                    NoteFolder.id == note_data.folder_id,
                    NoteFolder.user_id == current_user.id
                ).first()
                if not folder:
                    raise HTTPException(status_code=404, detail="Folder not found")
                note.folder_id = note_data.folder_id
        
        if note_data.title is not None:
            note.title = note_data.title
        if note_data.content is not None:
            note.content = note_data.content
        if note_data.tags is not None:
            note.tags = note_data.tags
        if note_data.is_pinned is not None:
            note.is_pinned = note_data.is_pinned
        
        db.commit()
        db.refresh(note)
        
        folder_name = None
        if note.folder_id:
            folder = db.query(NoteFolder).filter(NoteFolder.id == note.folder_id).first()
            folder_name = folder.name if folder else None
        
        return _serialize_note_response(note, folder_name)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error updating note: {e}", exc_info=True)
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Error updating note: {str(e)}")


@router.delete("/{note_id}")
async def delete_note(
    note_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Delete a note."""
    note = db.query(Note).filter(
        Note.id == note_id,
        Note.user_id == current_user.id
    ).first()
    
    if not note:
        raise HTTPException(status_code=404, detail="Note not found")
    
    # Delete attachments
    from app.services.storage_service import StorageService
    storage = StorageService(db)
    storage.delete_note_attachments(current_user.username, note_id)
    
    db.delete(note)
    db.commit()
    
    return {"success": True, "message": "Note deleted"}


@router.get("/files/{username}/{note_id}/{filename}")
async def serve_note_file(
    username: str,
    note_id: int,
    filename: str,
    request: StarletteRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    Serve an attachment file for a note.
    Proxies to storage server if storage_server_url is configured.
    """
    # Verify user owns this file (username must match)
    if current_user.username != username:
        raise HTTPException(status_code=403, detail="Access denied")
    
    # Verify note belongs to user
    note = db.query(Note).filter(
        Note.id == note_id,
        Note.user_id == current_user.id
    ).first()
    if not note:
        raise HTTPException(status_code=404, detail="Note not found")
    
    # Check if storage server is configured - proxy request if so
    storage_server_url = db.query(Setting).filter(Setting.key == "storage_server_url").first()
    if storage_server_url and storage_server_url.value:
        # Proxy to storage server (stream file response)
        from app.services.storage_proxy import proxy_storage_request
        return await proxy_storage_request(
            db=db,
            request=request,
            endpoint=f"/api/notes/files/{username}/{note_id}/{filename}",
            method="GET",
            stream=True
        )
    
    # Local file serving
    # Get file path with sanitization to prevent path traversal
    from app.services.storage_service import StorageService, _sanitize_path_component, _validate_path_within_base
    storage = StorageService(db)
    
    # Sanitize filename to prevent path traversal attacks
    try:
        safe_filename = _sanitize_path_component(filename)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=f"Invalid filename: {str(e)}")
    
    # Construct path using sanitized components
    file_path = Path(storage.upload_path) / current_user.username / "notes" / str(note_id) / safe_filename
    
    # Validate path is within expected directory (defense in depth)
    base_path = Path(storage.upload_path) / current_user.username / "notes" / str(note_id)
    if not _validate_path_within_base(file_path, base_path):
        raise HTTPException(status_code=403, detail="Invalid file path")
    
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="File not found")
    
    # Determine media type (comprehensive list)
    suffix = file_path.suffix.lower()
    media_types = {
        # Images
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".gif": "image/gif",
        ".webp": "image/webp",
        ".svg": "image/svg+xml",
        ".bmp": "image/bmp",
        ".tiff": "image/tiff",
        ".ico": "image/x-icon",
        # Documents
        ".pdf": "application/pdf",
        ".doc": "application/msword",
        ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        ".xls": "application/vnd.ms-excel",
        ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        ".ppt": "application/vnd.ms-powerpoint",
        ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        ".odt": "application/vnd.oasis.opendocument.text",
        ".ods": "application/vnd.oasis.opendocument.spreadsheet",
        ".odp": "application/vnd.oasis.opendocument.presentation",
        # Text
        ".txt": "text/plain",
        ".md": "text/markdown",
        ".html": "text/html",
        ".css": "text/css",
        ".js": "text/javascript",
        ".json": "application/json",
        ".xml": "text/xml",
        # Archives
        ".zip": "application/zip",
        ".rar": "application/x-rar-compressed",
        ".tar": "application/x-tar",
        ".gz": "application/gzip",
        ".7z": "application/x-7z-compressed",
        # Audio
        ".mp3": "audio/mpeg",
        ".wav": "audio/wav",
        ".ogg": "audio/ogg",
        ".m4a": "audio/mp4",
        ".webm": "audio/webm",
        # Video
        ".mp4": "video/mp4",
        ".mpeg": "video/mpeg",
        ".mov": "video/quicktime",
        ".avi": "video/x-msvideo",
        ".mkv": "video/x-matroska",
        # Code
        ".py": "text/x-python",
        ".java": "text/x-java",
        ".c": "text/x-c",
        ".cpp": "text/x-c++",
        ".cs": "text/x-csharp",
        ".sh": "application/x-sh",
    }
    media_type = media_types.get(suffix, "application/octet-stream")
    
    return FileResponse(file_path, media_type=media_type)
