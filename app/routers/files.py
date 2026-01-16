"""
File Manager Router - Web-based file manager with image thumbnails and viewer.
Includes configurable memory cache for file listings, email, and public sharing.
All blocking I/O operations are run in thread pools to prevent blocking.
"""
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse
from sqlalchemy.orm import Session
from pathlib import Path
from typing import List, Optional, Dict, Tuple
from collections import OrderedDict
from pydantic import BaseModel
from datetime import datetime, timedelta
import os
import base64
from PIL import Image
import io
import logging
import time
import threading
import secrets
import asyncio

from app.database import get_db
from app.auth import get_current_user
from app.models import User, Setting, SharedFile
from app.services.storage_service import get_storage_service, _sanitize_path_component, _validate_path_within_base

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/files", tags=["files"])


class FileListingCache:
    """LRU cache for file listings with TTL."""
    
    def __init__(self, max_size: int = 1000, ttl: int = 300):
        self.max_size = max_size
        self.ttl = ttl  # Time to live in seconds
        self.cache: OrderedDict[str, Tuple[float, dict]] = OrderedDict()
    
    def get(self, key: str) -> Optional[dict]:
        """Get cached listing if not expired."""
        if key not in self.cache:
            return None
        
        timestamp, data = self.cache[key]
        if time.time() - timestamp > self.ttl:
            # Expired, remove it
            del self.cache[key]
            return None
        
        # Move to end (most recently used)
        self.cache.move_to_end(key)
        return data
    
    def set(self, key: str, value: dict):
        """Cache a listing."""
        # Remove oldest if at capacity
        if len(self.cache) >= self.max_size:
            self.cache.popitem(last=False)  # Remove oldest
        
        self.cache[key] = (time.time(), value)
        self.cache.move_to_end(key)
    
    def invalidate(self, pattern: str = None):
        """Invalidate cache entries. If pattern provided, only invalidate matching keys."""
        if pattern:
            keys_to_remove = [k for k in self.cache.keys() if pattern in k]
            for key in keys_to_remove:
                del self.cache[key]
        else:
            self.cache.clear()
    
    def clear(self):
        """Clear all cache."""
        self.cache.clear()


# Global cache instance (will be initialized with settings)
_file_cache: Optional[FileListingCache] = None
_cache_lock = threading.Lock()


class NoOpCache:
    """No-op cache implementation when caching is disabled."""
    def get(self, key): return None
    def set(self, key, value): pass
    def invalidate(self, pattern=None): pass
    def clear(self): pass


def get_file_cache(db: Session, force_reload: bool = False) -> FileListingCache:
    """Get or create file cache with current settings."""
    global _file_cache
    
    # Load cache settings
    cache_enabled = db.query(Setting).filter(Setting.key == "file_cache_enabled").first()
    if cache_enabled and cache_enabled.value.lower() == "false":
        return NoOpCache()
    
    # Load TTL and max_size settings
    ttl_setting = db.query(Setting).filter(Setting.key == "file_cache_ttl").first()
    max_size_setting = db.query(Setting).filter(Setting.key == "file_cache_max_size").first()
    
    # Validate and parse settings with defaults
    try:
        ttl = max(60, min(3600, int(ttl_setting.value))) if ttl_setting and ttl_setting.value else 300
    except (ValueError, TypeError):
        ttl = 300
    
    try:
        max_size = max(100, min(10000, int(max_size_setting.value))) if max_size_setting and max_size_setting.value else 1000
    except (ValueError, TypeError):
        max_size = 1000
    
    # Thread-safe cache initialization/update
    with _cache_lock:
        # Check if we need to reload settings (if cache exists but settings changed)
        if _file_cache is not None and not force_reload:
            # If settings changed, recreate cache
            if _file_cache.ttl != ttl or _file_cache.max_size != max_size:
                logger.info(f"[FileCache] Settings changed, recreating cache (TTL={ttl}s, max_size={max_size})")
                _file_cache = FileListingCache(max_size=max_size, ttl=ttl)
        
        if _file_cache is None or force_reload:
            _file_cache = FileListingCache(max_size=max_size, ttl=ttl)
            logger.info(f"[FileCache] Initialized with TTL={ttl}s, max_size={max_size}")
    
    return _file_cache


@router.get("/search")
async def search_files(
    query: str = Query(..., description="Search query (filename or path)"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Search for files by name or path. Returns matching files with metadata."""
    storage = get_storage_service(db)
    user_path = storage.get_user_path(current_user.username)
    
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
                        
                        # Generate thumbnail for images
                        if not is_dir and item.suffix.lower() in ['.jpg', '.jpeg', '.png', '.gif', '.webp', '.bmp']:
                            try:
                                thumbnail = generate_thumbnail(item, max_size=(100, 100))
                                if thumbnail:
                                    item_info["thumbnail"] = thumbnail
                            except Exception as e:
                                logger.debug(f"Failed to generate thumbnail for {item}: {e}")
                        
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


@router.get("/list")
async def list_files(
    path: str = Query("", description="Directory path relative to user root"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """List files and directories in user's storage. Uses memory cache if enabled."""
    storage = get_storage_service(db)
    user_path = storage.get_user_path(current_user.username)
    
    # Sanitize and validate path
    if path:
        try:
            safe_path = Path(*[_sanitize_path_component(p) for p in path.split('/') if p])
            full_path = user_path / safe_path
        except ValueError as e:
            raise HTTPException(status_code=400, detail=f"Invalid path: {e}")
    else:
        full_path = user_path
    
    # Validate path is within user directory
    if not _validate_path_within_base(full_path, user_path):
        raise HTTPException(status_code=403, detail="Access denied")
    
    if not full_path.exists():
        raise HTTPException(status_code=404, detail="Path not found")
    
    if not full_path.is_dir():
        raise HTTPException(status_code=400, detail="Path is not a directory")
    
    # Check cache (normalize path to avoid cache key inconsistencies)
    cache = get_file_cache(db)
    normalized_path = path.strip('/') if path else ""
    cache_key = f"{current_user.username}:{normalized_path}"
    cached_result = cache.get(cache_key)
    
    if cached_result:
        logger.debug(f"[FileCache] Cache hit for {cache_key}")
        return cached_result
    
    logger.debug(f"[FileCache] Cache miss for {cache_key}, generating listing")
    
    # Run blocking file I/O operations in thread pool to prevent blocking other requests
    def _list_directory_sync():
        """Synchronous directory listing function."""
        items = []
        try:
            for item in sorted(full_path.iterdir()):
                try:
                    stat = item.stat()
                    is_dir = item.is_dir()
                    
                    item_info = {
                        "name": item.name,
                        "path": str(item.relative_to(user_path)),
                        "is_directory": is_dir,
                        "size": stat.st_size if not is_dir else 0,
                        "modified": stat.st_mtime,
                    }
                    
                    # Generate thumbnail for images (this is CPU-intensive, but we'll do it in thread pool)
                    if not is_dir and item.suffix.lower() in ['.jpg', '.jpeg', '.png', '.gif', '.webp', '.bmp']:
                        try:
                            thumbnail = generate_thumbnail(item, max_size=(200, 200))
                            if thumbnail:
                                item_info["thumbnail"] = thumbnail
                        except Exception as e:
                            logger.warning(f"Failed to generate thumbnail for {item}: {e}")
                    
                    items.append(item_info)
                except Exception as e:
                    logger.warning(f"Error reading item {item}: {e}")
                    continue
        except Exception as e:
            raise Exception(f"Error listing directory: {e}")
        return items
    
    # Run directory listing in thread pool
    try:
        items = await asyncio.to_thread(_list_directory_sync)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    
    # Calculate storage usage in thread pool (this can be slow for large directories)
    user_quota = current_user.storage_quota
    current_usage = await asyncio.to_thread(calculate_directory_size, user_path)
    
    result = {
        "items": items,
        "path": str(full_path.relative_to(user_path)) if path else "",
        "storage": {
            "used": current_usage,
            "quota": user_quota,
            "quota_mb": user_quota / (1024 * 1024) if user_quota > 0 else 0,
            "used_mb": current_usage / (1024 * 1024),
            "unlimited": user_quota == 0
        }
    }
    
    # Cache the result
    cache.set(cache_key, result)
    
    return result


@router.get("/view/{file_path:path}")
async def view_file(
    file_path: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """View/download a file. Returns image viewer HTML for images. Invalidates cache for parent directory."""
    storage = get_storage_service(db)
    user_path = storage.get_user_path(current_user.username)
    
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
    
    # Note: We don't invalidate cache on view - viewing doesn't mean the file changed.
    # Cache will expire naturally via TTL, and actual file modifications (upload/delete)
    # will trigger invalidation.
    
    # Determine media type
    suffix = full_path.suffix.lower()
    media_types = {
        '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.png': 'image/png',
        '.gif': 'image/gif', '.webp': 'image/webp', '.bmp': 'image/bmp',
        '.pdf': 'application/pdf',
        '.txt': 'text/plain', '.md': 'text/markdown',
        '.html': 'text/html', '.css': 'text/css', '.js': 'text/javascript',
    }
    media_type = media_types.get(suffix, 'application/octet-stream')
    
    return FileResponse(
        full_path,
        media_type=media_type,
        filename=full_path.name
    )


@router.get("/thumbnail/{file_path:path}")
async def get_thumbnail(
    file_path: str,
    size: int = Query(200, description="Thumbnail size"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Get thumbnail for an image file."""
    storage = get_storage_service(db)
    user_path = storage.get_user_path(current_user.username)
    
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
    
    # Generate thumbnail in thread pool (image processing is CPU-intensive)
    try:
        thumbnail_data = await asyncio.to_thread(generate_thumbnail, full_path, (size, size))
        if thumbnail_data:
            return JSONResponse({"thumbnail": thumbnail_data})
        else:
            raise HTTPException(status_code=400, detail="File is not an image")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error generating thumbnail: {e}")


def generate_thumbnail(file_path: Path, max_size: tuple = (200, 200)) -> Optional[str]:
    """Generate base64-encoded thumbnail for an image."""
    try:
        with Image.open(file_path) as img:
            img.thumbnail(max_size, Image.Resampling.LANCZOS)
            
            # Convert to RGB if necessary (for formats like PNG with transparency)
            if img.mode in ('RGBA', 'LA', 'P'):
                background = Image.new('RGB', img.size, (255, 255, 255))
                if img.mode == 'P':
                    img = img.convert('RGBA')
                background.paste(img, mask=img.split()[-1] if img.mode in ('RGBA', 'LA') else None)
                img = background
            elif img.mode != 'RGB':
                img = img.convert('RGB')
            
            # Save to bytes
            buffer = io.BytesIO()
            img.save(buffer, format='JPEG', quality=85)
            buffer.seek(0)
            
            # Encode as base64
            thumbnail_b64 = base64.b64encode(buffer.read()).decode('utf-8')
            return f"data:image/jpeg;base64,{thumbnail_b64}"
    except Exception as e:
        logger.error(f"Error generating thumbnail: {e}")
        return None


def calculate_directory_size(path: Path) -> int:
    """Calculate total size of directory in bytes."""
    if not path.exists():
        return 0
    
    total = 0
    try:
        for item in path.rglob('*'):
            if item.is_file():
                total += item.stat().st_size
    except Exception as e:
        logger.warning(f"Error calculating directory size: {e}")
    
    return total


@router.post("/invalidate-cache")
async def invalidate_file_cache(
    path: Optional[str] = Query(None, description="Path to invalidate (all if not provided)"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Invalidate file cache for current user. Admin can invalidate all."""
    cache = get_file_cache(db)
    
    if path:
        # Normalize path to match cache key format
        normalized_path = path.strip('/')
        cache_key = f"{current_user.username}:{normalized_path}"
        cache.invalidate(cache_key)
        logger.info(f"[FileCache] Invalidated cache for {cache_key}")
    else:
        # Invalidate all entries for this user
        cache.invalidate(f"{current_user.username}:")
        logger.info(f"[FileCache] Invalidated all cache for user {current_user.username}")
    
    return {"message": "Cache invalidated"}


# Pydantic models for email and sharing
class EmailFileRequest(BaseModel):
    file_paths: List[str]  # List of file paths to email
    to: str  # Recipient email
    subject: str = "Shared files"
    body: str = "Please find the attached files."
    account_email: Optional[str] = None  # Which mail account to use (first if not specified)


class CreateShareRequest(BaseModel):
    file_path: str
    expires_hours: Optional[int] = None  # None = never expires
    max_accesses: Optional[int] = None  # None = unlimited


@router.post("/email")
async def email_files(
    request: EmailFileRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Email one or more files as attachments."""
    from app.services.mail_service import get_user_mail_accounts, send_email
    
    # Get user's mail accounts
    accounts = get_user_mail_accounts(current_user.id, db)
    if not accounts:
        raise HTTPException(status_code=400, detail="No email accounts configured. Please configure email in User Settings.")
    
    # Select account
    account = next((a for a in accounts if a.email == request.account_email), None) if request.account_email else accounts[0]
    if not account:
        raise HTTPException(status_code=400, detail="Email account not found")
    
    storage = get_storage_service(db)
    user_path = storage.get_user_path(current_user.username)
    
    # Validate and read files (run file I/O in thread pool)
    attachments = []
    for file_path in request.file_paths:
        try:
            # Sanitize and validate path
            safe_path = Path(*[_sanitize_path_component(p) for p in file_path.split('/') if p])
            full_path = user_path / safe_path
            
            # Validate path is within user directory
            if not _validate_path_within_base(full_path, user_path):
                raise HTTPException(status_code=403, detail=f"Access denied: {file_path}")
            
            if not full_path.exists() or not full_path.is_file():
                raise HTTPException(status_code=404, detail=f"File not found: {file_path}")
            
            # Read file in thread pool to prevent blocking
            def _read_file_sync():
                with open(full_path, 'rb') as f:
                    return f.read()
            
            file_data = await asyncio.to_thread(_read_file_sync)
            
            # Determine content type
            suffix = full_path.suffix.lower()
            content_types = {
                '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.png': 'image/png',
                '.gif': 'image/gif', '.webp': 'image/webp', '.bmp': 'image/bmp',
                '.pdf': 'application/pdf',
                '.txt': 'text/plain', '.md': 'text/markdown',
                '.html': 'text/html', '.css': 'text/css', '.js': 'text/javascript',
                '.zip': 'application/zip', '.tar': 'application/x-tar', '.gz': 'application/gzip',
            }
            content_type = content_types.get(suffix, 'application/octet-stream')
            
            attachments.append((full_path.name, file_data, content_type))
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Error reading file {file_path}: {e}")
            raise HTTPException(status_code=500, detail=f"Error reading file: {file_path}")
    
    if not attachments:
        raise HTTPException(status_code=400, detail="No valid files to email")
    
    # Send email
    success = send_email(
        account=account,
        to=request.to,
        subject=request.subject,
        body=request.body,
        attachments=attachments
    )
    
    if not success:
        raise HTTPException(status_code=500, detail="Failed to send email. Check logs for details.")
    
    return {"message": f"Email sent successfully with {len(attachments)} file(s)"}


@router.post("/share")
async def create_share(
    request: CreateShareRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Create a public sharing URL for a file."""
    storage = get_storage_service(db)
    user_path = storage.get_user_path(current_user.username)
    
    # Validate file path
    try:
        safe_path = Path(*[_sanitize_path_component(p) for p in request.file_path.split('/') if p])
        full_path = user_path / safe_path
        
        # Validate path is within user directory
        if not _validate_path_within_base(full_path, user_path):
            raise HTTPException(status_code=403, detail="Access denied")
        
        if not full_path.exists() or not full_path.is_file():
            raise HTTPException(status_code=404, detail="File not found")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=f"Invalid path: {e}")
    
    # Generate secure token
    token = secrets.token_urlsafe(32)
    
    # Calculate expiration
    expires_at = None
    if request.expires_hours:
        expires_at = datetime.utcnow() + timedelta(hours=request.expires_hours)
    
    # Create share record
    share = SharedFile(
        user_id=current_user.id,
        token=token,
        file_path=request.file_path,
        filename=full_path.name,
        expires_at=expires_at,
        max_accesses=request.max_accesses,
        is_active=True
    )
    db.add(share)
    db.commit()
    
    # Generate share URL (frontend will construct full URL)
    share_url = f"/api/files/shared/{token}"
    
    return {
        "token": token,
        "share_url": share_url,
        "expires_at": expires_at.isoformat() if expires_at else None,
        "max_accesses": request.max_accesses
    }


@router.get("/shared/{token}")
async def get_shared_file(
    token: str,
    db: Session = Depends(get_db)
):
    """Get a shared file by token."""
    share = db.query(SharedFile).filter(
        SharedFile.token == token,
        SharedFile.is_active == True
    ).first()
    
    if not share:
        raise HTTPException(status_code=404, detail="Share not found or expired")
    
    # Check expiration
    if share.expires_at and share.expires_at < datetime.utcnow():
        share.is_active = False
        db.commit()
        raise HTTPException(status_code=404, detail="Share has expired")
    
    # Check access limit
    if share.max_accesses and share.access_count >= share.max_accesses:
        share.is_active = False
        db.commit()
        raise HTTPException(status_code=404, detail="Share access limit reached")
    
    # Get user and file
    user = db.query(User).filter(User.id == share.user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    
    storage = get_storage_service(db)
    user_path = storage.get_user_path(user.username)
    
    # Validate and serve file
    try:
        safe_path = Path(*[_sanitize_path_component(p) for p in share.file_path.split('/') if p])
        full_path = user_path / safe_path
        
        if not _validate_path_within_base(full_path, user_path):
            raise HTTPException(status_code=403, detail="Access denied")
        
        if not full_path.exists() or not full_path.is_file():
            raise HTTPException(status_code=404, detail="File not found")
        
        # Increment access count
        share.access_count += 1
        db.commit()
        
        # Determine media type
        suffix = full_path.suffix.lower()
        media_types = {
            '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.png': 'image/png',
            '.gif': 'image/gif', '.webp': 'image/webp', '.bmp': 'image/bmp',
            '.pdf': 'application/pdf',
            '.txt': 'text/plain', '.md': 'text/markdown',
            '.html': 'text/html', '.css': 'text/css', '.js': 'text/javascript',
        }
        media_type = media_types.get(suffix, 'application/octet-stream')
        
        return FileResponse(
            full_path,
            media_type=media_type,
            filename=share.filename
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=f"Invalid path: {e}")


@router.get("/shares")
async def list_shares(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """List all active shares for current user."""
    shares = db.query(SharedFile).filter(
        SharedFile.user_id == current_user.id,
        SharedFile.is_active == True
    ).order_by(SharedFile.created_at.desc()).all()
    
    result = []
    for share in shares:
        # Check if expired
        is_expired = share.expires_at and share.expires_at < datetime.utcnow()
        is_limit_reached = share.max_accesses and share.access_count >= share.max_accesses
        
        result.append({
            "id": share.id,
            "token": share.token,
            "file_path": share.file_path,
            "filename": share.filename,
            "created_at": share.created_at.isoformat(),
            "expires_at": share.expires_at.isoformat() if share.expires_at else None,
            "access_count": share.access_count,
            "max_accesses": share.max_accesses,
            "is_expired": is_expired,
            "is_limit_reached": is_limit_reached,
            "share_url": f"/api/files/shared/{share.token}"
        })
    
    return {"shares": result}


@router.delete("/shares/{share_id}")
async def revoke_share(
    share_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Revoke a share (deactivate it)."""
    share = db.query(SharedFile).filter(
        SharedFile.id == share_id,
        SharedFile.user_id == current_user.id
    ).first()
    
    if not share:
        raise HTTPException(status_code=404, detail="Share not found")
    
    share.is_active = False
    db.commit()
    
    return {"message": "Share revoked"}


@router.delete("/delete")
async def delete_file(
    file_path: str = Query(..., description="File path relative to user root"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Delete a file or directory."""
    storage = get_storage_service(db)
    user_path = storage.get_user_path(current_user.username)
    
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
            cache = get_file_cache(db)
            cache.invalidate(f"{current_user.username}:")
            # Invalidate parent directory cache
            if file_path:
                parent_path = '/'.join(file_path.split('/')[:-1]) if '/' in file_path else ""
                cache.invalidate(f"{current_user.username}:{parent_path}")
        except Exception as e:
            logger.warning(f"Failed to invalidate cache: {e}")
        
        return {"message": "File deleted successfully"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
