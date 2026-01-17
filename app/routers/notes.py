"""
Notes Router - API endpoints for notes management.
Supports both local storage and remote storage server proxying via storage_server_url.
"""
from fastapi import APIRouter, Depends, Request, HTTPException, UploadFile, File
from fastapi.responses import JSONResponse, FileResponse
from starlette.requests import Request as StarletteRequest
from sqlalchemy.orm import Session
from sqlalchemy import func, or_
from typing import List, Optional
from pathlib import Path
import logging
import asyncio
from datetime import datetime
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
        # Try Pydantic v2 with JSON mode to ensure datetime serialization
        note_dict = NoteResponse.model_validate(note).model_dump(mode='json')
    except (AttributeError, ValidationError, TypeError):
        # Pydantic v1 fallback or if mode='json' not supported
        try:
            note_dict = NoteResponse.from_orm(note).dict()
            # Ensure datetime objects are serialized to strings
            if isinstance(note_dict.get('created_at'), datetime):
                note_dict['created_at'] = note_dict['created_at'].isoformat()
            if isinstance(note_dict.get('updated_at'), datetime):
                note_dict['updated_at'] = note_dict['updated_at'].isoformat()
        except Exception as e:
            logger.error(f"Error serializing note {note.id}: {e}", exc_info=True)
            # Fallback: manual dict construction with datetime serialization
            note_dict = {
                "id": note.id,
                "title": note.title,
                "content": note.content or "",
                "folder_id": note.folder_id,
                "tags": note.tags,
                "attachments": note.attachments,
                "is_pinned": note.is_pinned,
                "created_at": note.created_at.isoformat() if note.created_at else None,
                "updated_at": note.updated_at.isoformat() if note.updated_at else None
            }
    
    result = {**note_dict}
    if folder_name is not None:
        result["folder_name"] = folder_name
    # Add username for frontend attachment rendering
    result["username"] = note.user.username
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
    
    # Return with no-cache headers to prevent browser caching
    from fastapi.responses import JSONResponse
    return JSONResponse(
        content=result,
        headers={
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Pragma": "no-cache",
            "Expires": "0"
        }
    )


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
    db.query(Note).filter(Note.folder_id == folder_id).update({"folder_id": None})
    
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
            # Make search case-insensitive by converting both search term and fields to lowercase
            # This works across all databases (SQLite, PostgreSQL, MySQL)
            search_term = f"%{search.lower()}%"
            query = query.filter(
                or_(
                    func.lower(Note.title).like(search_term),
                    func.lower(Note.content).like(search_term)
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
        
        # Return with no-cache headers to prevent browser caching
        # Use Response with proper JSON serialization to handle datetime objects
        from fastapi.responses import Response
        import json
        
        # Custom JSON encoder for datetime objects
        class DateTimeEncoder(json.JSONEncoder):
            def default(self, obj):
                if isinstance(obj, datetime):
                    return obj.isoformat()
                return super().default(obj)
        
        json_content = json.dumps(result, cls=DateTimeEncoder)
        return Response(
            content=json_content,
            media_type="application/json",
            headers={
                "Cache-Control": "no-cache, no-store, must-revalidate",
                "Pragma": "no-cache",
                "Expires": "0"
            }
        )
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
    
    # Delete attachments (storage service handles proxying if needed)
    from app.services.storage_service import StorageService
    from app.database import SessionLocal
    
    username = current_user.username
    
    def _delete_attachments_sync():
        # Create a new database session for the thread
        thread_db = SessionLocal()
        try:
            storage = StorageService(thread_db)
            return storage.delete_note_attachments(username, note_id)
        finally:
            thread_db.close()
    
    # Run in thread pool to avoid blocking
    await asyncio.to_thread(_delete_attachments_sync)
    
    db.delete(note)
    db.commit()
    
    return {"success": True, "message": "Note deleted"}


@router.api_route("/files/{username}/{note_id}/{filename}", methods=["GET", "HEAD"])
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
    try:
        # Decode URL-encoded username (handles @ symbols, etc.)
        from urllib.parse import unquote
        try:
            decoded_username = unquote(username)
        except:
            decoded_username = username
        
        # Verify user owns this file (username must match)
        # Allow both exact match and URL-encoded match
        if current_user.username != decoded_username:
            # Try URL-encoding the current username to see if it matches
            from urllib.parse import quote
            encoded_current = quote(current_user.username, safe='')
            if encoded_current != username and current_user.username != username:
                logger.warning(
                    f"Username mismatch: current_user={current_user.username}, "
                    f"decoded_username={decoded_username}, url_username={username}"
                )
                raise HTTPException(status_code=403, detail="Access denied")
        
        # Check if storage server is configured - but check local file first
        # (Note: When proxying, the main server has already verified the note exists)
        storage_server_url = db.query(Setting).filter(Setting.key == "storage_server_url").first()
        
        # Get file path to check if it exists locally
        from app.services.storage_service import StorageService, _sanitize_path_component, _validate_path_within_base
        from urllib.parse import unquote
        storage = StorageService(db)
        
        # FastAPI automatically URL-decodes path parameters, but handle both encoded and unencoded filenames
        # Decode filename if it's URL-encoded (handles cases where frontend sends encoded, but import script might not)
        try:
            decoded_filename = unquote(filename)
        except:
            decoded_filename = filename
        
        # Sanitize filename to prevent path traversal attacks
        try:
            safe_filename = _sanitize_path_component(decoded_filename)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=f"Invalid filename: {str(e)}")
        
        # Construct path using sanitized components
        file_path = Path(storage.upload_path) / current_user.username / "notes" / str(note_id) / safe_filename
        
        # Validate path is within expected directory (defense in depth)
        base_path = Path(storage.upload_path) / current_user.username / "notes" / str(note_id)
        if not _validate_path_within_base(file_path, base_path):
            raise HTTPException(status_code=403, detail="Invalid file path")
        
        # If file exists locally, serve it locally (don't proxy)
        # Only proxy if file doesn't exist locally and storage server is configured
        if not file_path.exists() and storage_server_url and storage_server_url.value:
            # Validate storage_server_url has protocol before proxying
            base_url = storage_server_url.value.strip()
            if base_url.startswith(('http://', 'https://')):
                # File doesn't exist locally, try proxying to storage server
                from app.services.storage_proxy import proxy_storage_request
                # Use the actual request method (GET or HEAD)
                method = request.method
                try:
                    return await proxy_storage_request(
                        db=db,
                        request=request,
                        endpoint=f"/api/notes/files/{username}/{note_id}/{filename}",
                        method=method,
                        stream=True
                    )
                except HTTPException as e:
                    # If proxy fails, log and fall through to 404
                    logger.warning(f"Failed to proxy note file to storage server: {e.detail}")
                    # Fall through to return 404 below
            else:
                # Invalid storage_server_url configuration - log but don't fail the request
                logger.error(f"Invalid storage_server_url (missing protocol): {base_url}")
                # Fall through to return 404 below
        
        # Local file serving (file exists locally or no storage server configured)
        # Note: For storage servers, we may not have notes in the database.
        # The main server verifies note ownership before proxying, so we can
        # serve files if they exist on disk without checking the database.
        # Optionally verify note exists if we have it in the database (for local setups)
        note = db.query(Note).filter(
            Note.id == note_id,
            Note.user_id == current_user.id
        ).first()
        # If note doesn't exist in DB, we'll still try to serve the file if it exists on disk
        # (This allows storage servers to serve files without notes in their database)
        # File path was already constructed above, just verify it exists
        if not file_path.exists():
            # Log detailed error for debugging
            logger.warning(
                f"Note file not found: username={current_user.username}, note_id={note_id}, "
                f"filename={filename}, decoded_filename={decoded_filename}, safe_filename={safe_filename}, "
                f"file_path={file_path}, base_path={base_path}, base_exists={base_path.exists()}"
            )
            if base_path.exists():
                # List files in the directory to help debug
                try:
                    files_in_dir = [f.name for f in base_path.iterdir() if f.is_file()]
                    logger.warning(f"Files in note directory: {files_in_dir[:10]}")
                    
                    # Try multiple matching strategies:
                    # 1. Case-insensitive exact match
                    for f in files_in_dir:
                        if f.lower() == safe_filename.lower():
                            logger.warning(f"Found case-insensitive match: {f} vs {safe_filename}")
                            file_path = base_path / f
                            break
                    
                    # 2. If still not found, try matching by base filename (without extension)
                    # This handles cases where files were saved with timestamps
                    # e.g., "image.png" -> "image_20260116_203122_329081.png"
                    if not file_path.exists():
                        safe_base = Path(safe_filename).stem  # filename without extension
                        safe_ext = Path(safe_filename).suffix.lower()  # extension
                        
                        for f in files_in_dir:
                            f_base = Path(f).stem
                            f_ext = Path(f).suffix.lower()
                            
                            # Match if:
                            # 1. Base name exactly matches (case-insensitive)
                            # 2. Actual file starts with requested base name + underscore (timestamped)
                            # 3. Requested base name starts with actual base name + underscore (reverse)
                            # 4. Extension must match
                            if f_ext == safe_ext:
                                if (f_base.lower() == safe_base.lower() or 
                                    f_base.lower().startswith(safe_base.lower() + '_') or
                                    safe_base.lower().startswith(f_base.lower() + '_')):
                                    logger.warning(f"Found base name match: {f} vs {safe_filename} (base: {safe_base})")
                                    file_path = base_path / f
                                    break
                    
                    # 3. If still not found, try partial match (filename contains the requested name)
                    # This handles cases where files were saved with additional timestamps
                    # e.g., "20220415_123623.jpg" -> "20220415_123623_20260116_203124_299386.jpg"
                    if not file_path.exists():
                        safe_base = Path(safe_filename).stem.lower()
                        safe_ext = Path(safe_filename).suffix.lower()
                        
                        # Try to find a file that starts with the base name (for timestamped files)
                        best_match = None
                        best_match_score = 0
                        
                        for f in files_in_dir:
                            f_base = Path(f).stem.lower()
                            f_ext = Path(f).suffix.lower()
                            
                            if f_ext != safe_ext:
                                continue
                            
                            # Score matches based on how well they match
                            # Exact match gets highest score
                            if f_base == safe_base:
                                best_match = f
                                best_match_score = 100
                                break
                            # Starts with requested base + underscore (timestamped) gets high score
                            elif f_base.startswith(safe_base + '_'):
                                score = len(safe_base) / len(f_base) * 90  # Prefer shorter matches
                                if score > best_match_score:
                                    best_match = f
                                    best_match_score = score
                            # Requested starts with file base (reverse) gets medium score
                            elif safe_base.startswith(f_base + '_'):
                                score = len(f_base) / len(safe_base) * 70
                                if score > best_match_score:
                                    best_match = f
                                    best_match_score = score
                            # Contains match gets lower score
                            elif safe_base in f_base or f_base in safe_base:
                                score = min(len(safe_base), len(f_base)) / max(len(safe_base), len(f_base)) * 50
                                if score > best_match_score:
                                    best_match = f
                                    best_match_score = score
                        
                        if best_match and best_match_score >= 50:  # Only use if score is reasonable
                            logger.warning(f"Found partial match (score: {best_match_score:.1f}): {best_match} vs {safe_filename}")
                            file_path = base_path / best_match
                                
                except Exception as e:
                    logger.warning(f"Error listing directory: {e}")
            
            # If still not found after all matching attempts, raise 404
            if not file_path.exists():
                raise HTTPException(status_code=404, detail=f"File not found: {safe_filename}")
        
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
            ".tif": "image/tiff",
            ".ico": "image/x-icon",
            # Videos
            ".mp4": "video/mp4",
            ".mpeg": "video/mpeg",
            ".mpg": "video/mpeg",
            ".mov": "video/quicktime",
            ".avi": "video/x-msvideo",
            ".webm": "video/webm",
            ".mkv": "video/x-matroska",
            ".flv": "video/x-flv",
            ".wmv": "video/x-ms-wmv",
            ".3gp": "video/3gpp",
            ".ogv": "video/ogg",
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
        
        # For images, set Content-Disposition to inline so they display instead of downloading
        headers = {}
        image_extensions = [".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".bmp", ".tiff", ".tif", ".ico"]
        if suffix in image_extensions:
            headers["Content-Disposition"] = "inline"
        
        # For HEAD requests, return headers only (no body)
        if request.method == "HEAD":
            from fastapi.responses import Response
            try:
                response_headers = {
                    "Content-Type": media_type,
                    "Content-Length": str(file_path.stat().st_size),
                }
                response_headers.update(headers)
                return Response(headers=response_headers, status_code=200)
            except Exception as e:
                logger.error(f"Error serving note file (HEAD): {e}", exc_info=True)
                raise HTTPException(status_code=500, detail=f"Error reading file: {str(e)}")
        
        try:
            return FileResponse(file_path, media_type=media_type, headers=headers)
        except Exception as e:
            logger.error(f"Error serving note file: {e}", exc_info=True)
            raise HTTPException(status_code=500, detail=f"Error serving file: {str(e)}")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Unexpected error in serve_note_file: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Internal server error: {str(e)}")


@router.post("/{note_id}/attachments")
async def upload_note_attachment(
    note_id: int,
    file: UploadFile = File(...),
    request: StarletteRequest = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Upload an attachment file for a note."""
    # Verify note belongs to user
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
    
    # Save attachment (storage service handles proxying if needed)
    from app.services.storage_service import StorageService
    from app.database import SessionLocal
    
    username = current_user.username
    
    # Get Authorization header to forward to storage server if needed
    auth_header = request.headers.get("Authorization", "") if request else ""
    
    def _save_attachment_sync():
        # Create a new database session for the thread
        thread_db = SessionLocal()
        try:
            storage = StorageService(thread_db)
            # Pass auth header if available (for proxying)
            # Note: This is a workaround - ideally we'd pass the request, but that's not serializable
            # The storage service will use storage_server_token if available, otherwise it may fail
            # In that case, we need to ensure storage_server_token is set or the storage server accepts unauthenticated requests
            try:
                return storage.save_note_attachment(username, note_id, content, original_name)
            except Exception as storage_error:
                # If proxy fails, try saving locally
                logger.warning(f"Failed to save attachment via proxy, trying local: {storage_error}")
                return storage.save_note_attachment(username, note_id, content, original_name, bypass_proxy=True)
        finally:
            thread_db.close()
    
    try:
        filename = await asyncio.to_thread(_save_attachment_sync)
    except Exception as e:
        logger.error(f"Error saving attachment: {e}", exc_info=True)
        # Provide more specific error message
        error_msg = str(e)
        if "Storage server" in error_msg or "proxy" in error_msg.lower():
            raise HTTPException(status_code=503, detail=f"Storage service unavailable: {error_msg}")
        else:
            raise HTTPException(status_code=500, detail=f"Failed to save attachment: {error_msg}")
    
    # Update note's attachments list
    import json
    attachments = []
    if note.attachments:
        try:
            attachments = json.loads(note.attachments) if isinstance(note.attachments, str) else note.attachments
        except:
            attachments = []
    
    if filename not in attachments:
        attachments.append(filename)
        note.attachments = json.dumps(attachments)
        db.commit()
    
    return {"filename": filename, "message": "Attachment uploaded"}


@router.delete("/{note_id}/attachments/{filename}")
async def delete_note_attachment(
    note_id: int,
    filename: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Delete an attachment file from a note."""
    # Verify note belongs to user
    note = db.query(Note).filter(
        Note.id == note_id,
        Note.user_id == current_user.id
    ).first()
    if not note:
        raise HTTPException(status_code=404, detail="Note not found")
    
    # Update note's attachments list
    import json
    attachments = []
    if note.attachments:
        try:
            attachments = json.loads(note.attachments) if isinstance(note.attachments, str) else note.attachments
        except:
            attachments = []
    
    if filename in attachments:
        attachments.remove(filename)
        note.attachments = json.dumps(attachments) if attachments else None
        db.commit()
        
        # Delete the actual file
        from app.services.storage_service import StorageService
        from app.database import SessionLocal
        
        username = current_user.username
        
        def _delete_attachment_sync():
            # Create a new database session for the thread
            thread_db = SessionLocal()
            try:
                storage = StorageService(thread_db)
                return storage.delete_note_attachment(username, note_id, filename)
            finally:
                thread_db.close()
        
        await asyncio.to_thread(_delete_attachment_sync)
    
    return {"message": "Attachment deleted"}
