"""
File Manager Router - Web-based file manager with image thumbnails and viewer.
Includes configurable memory cache for file listings, email, and public sharing.
All blocking I/O operations are run in thread pools to prevent blocking.
"""
from fastapi import APIRouter, Depends, HTTPException, Query, UploadFile, File, Form
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
from app.models import User, Setting, SharedFile, ExternalStorage
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
    path: str = Query("", description="Directory path relative to user root or external storage mount point"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """List files and directories in user's storage or external storage. Uses memory cache if enabled."""
    storage = get_storage_service(db)
    user_path = storage.get_user_path(current_user.username)
    
    # Check if this is an external storage path
    external_storage = None
    external_path = None
    
    if path:
        path_parts = path.split('/')
        # Check if first part is an external storage mount point
        if path_parts and path_parts[0]:
            mount_point = path_parts[0]
            external_storage = db.query(ExternalStorage).filter(
                ExternalStorage.mount_point == mount_point,
                ExternalStorage.is_active == True
            ).first()
            
            # Check if user has access to this external storage
            if external_storage and current_user in external_storage.allowed_users:
                # This is an external storage path
                # Build path relative to mount
                if len(path_parts) > 1:
                    relative_parts = path_parts[1:]
                    external_path = Path(external_storage.mount_path) / Path(*relative_parts)
                else:
                    external_path = Path(external_storage.mount_path)
                
                # Validate external path is within mount
                mount_path = Path(external_storage.mount_path).resolve()
                external_path_resolved = external_path.resolve()
                
                if not str(external_path_resolved).startswith(str(mount_path)):
                    raise HTTPException(status_code=403, detail="Access denied: path outside mount")
                
                if not external_path.exists():
                    raise HTTPException(status_code=404, detail="Path not found")
                
                if not external_path.is_dir():
                    raise HTTPException(status_code=400, detail="Path is not a directory")
    
    # Handle regular user storage path
    if not external_storage:
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
    
    # Determine which path to use
    target_path = external_path if external_storage else full_path
    base_path = Path(external_storage.mount_path) if external_storage else user_path
    
    # Check cache (normalize path to avoid cache key inconsistencies)
    cache = get_file_cache(db)
    normalized_path = path.strip('/') if path else ""
    cache_key = f"{current_user.username}:{normalized_path}:{'external' if external_storage else 'user'}"
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
            for item in sorted(target_path.iterdir()):
                try:
                    stat = item.stat()
                    is_dir = item.is_dir()
                    
                    # Calculate relative path
                    if external_storage:
                        # For external storage, path is mount_point/relative_path
                        relative_to_mount = item.relative_to(Path(external_storage.mount_path))
                        if str(relative_to_mount) == '.':
                            item_path = external_storage.mount_point
                        else:
                            item_path = f"{external_storage.mount_point}/{relative_to_mount}"
                    else:
                        item_path = str(item.relative_to(user_path))
                    
                    item_info = {
                        "name": item.name,
                        "path": item_path,
                        "is_directory": is_dir,
                        "size": stat.st_size if not is_dir else 0,
                        "modified": stat.st_mtime,
                        "is_external": external_storage is not None,
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
    
    # Calculate storage usage (only for user storage, not external)
    user_quota = current_user.storage_quota
    if not external_storage:
        current_usage = await asyncio.to_thread(calculate_directory_size, user_path)
    else:
        current_usage = 0  # External storage doesn't count toward quota
    
    result = {
        "items": items,
        "path": path if path else "",
        "is_external": external_storage is not None,
        "external_name": external_storage.name if external_storage else None,
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


@router.get("/external-storage")
async def get_external_storage_mounts(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Get list of active external storage mounts that the current user has access to."""
    # Get all active mounts
    all_mounts = db.query(ExternalStorage).filter(
        ExternalStorage.is_active == True
    ).order_by(ExternalStorage.name).all()
    
    # Filter mounts where user is in allowed_users list
    # If allowed_users is empty, no one has access (admin must explicitly grant access)
    accessible_mounts = []
    for mount in all_mounts:
        # Check if user is in allowed_users
        if current_user in mount.allowed_users:
            accessible_mounts.append({
                "id": mount.id,
                "name": mount.name,
                "mount_point": mount.mount_point,
                "description": mount.description,
                "mount_path": mount.mount_path
            })
    
    return {
        "mounts": accessible_mounts
    }


@router.get("/view/{file_path:path}")
async def view_file(
    file_path: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """View/download a file. Returns image viewer HTML for images. Supports external storage."""
    storage = get_storage_service(db)
    user_path = storage.get_user_path(current_user.username)
    
    # Check if this is an external storage path
    external_storage = None
    external_file_path = None
    
    path_parts = file_path.split('/')
    if path_parts and path_parts[0]:
        mount_point = path_parts[0]
        external_storage = db.query(ExternalStorage).filter(
            ExternalStorage.mount_point == mount_point,
            ExternalStorage.is_active == True
        ).first()
        
        # Check if user has access to this external storage
        if external_storage and current_user in external_storage.allowed_users:
            # This is an external storage file
            if len(path_parts) > 1:
                relative_parts = path_parts[1:]
                external_file_path = Path(external_storage.mount_path) / Path(*relative_parts)
            else:
                raise HTTPException(status_code=400, detail="Invalid file path")
            
            # Validate external path is within mount
            mount_path = Path(external_storage.mount_path).resolve()
            external_file_path_resolved = external_file_path.resolve()
            
            if not str(external_file_path_resolved).startswith(str(mount_path)):
                raise HTTPException(status_code=403, detail="Access denied: path outside mount")
            
            if not external_file_path.exists() or not external_file_path.is_file():
                raise HTTPException(status_code=404, detail="File not found")
            
            full_path = external_file_path
        elif external_storage:
            # User doesn't have access
            raise HTTPException(status_code=403, detail="Access denied: you don't have permission to access this storage")
        else:
            # Regular user storage path
            try:
                safe_path = Path(*[_sanitize_path_component(p) for p in path_parts if p])
                full_path = user_path / safe_path
            except ValueError as e:
                raise HTTPException(status_code=400, detail=f"Invalid path: {e}")
            
            # Validate path is within user directory
            if not _validate_path_within_base(full_path, user_path):
                raise HTTPException(status_code=403, detail="Access denied")
            
            if not full_path.exists() or not full_path.is_file():
                raise HTTPException(status_code=404, detail="File not found")
    else:
        raise HTTPException(status_code=400, detail="Invalid file path")
    
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
    """Get thumbnail for an image file. Supports external storage."""
    storage = get_storage_service(db)
    user_path = storage.get_user_path(current_user.username)
    
    # Check if this is an external storage path
    external_storage = None
    path_parts = file_path.split('/')
    if path_parts and path_parts[0]:
        mount_point = path_parts[0]
        external_storage = db.query(ExternalStorage).filter(
            ExternalStorage.mount_point == mount_point,
            ExternalStorage.is_active == True
        ).first()
        
        # Check if user has access to this external storage
        if external_storage and current_user in external_storage.allowed_users:
            # This is an external storage file
            if len(path_parts) > 1:
                from urllib.parse import unquote
                relative_parts = path_parts[1:]
                external_file_path = Path(external_storage.mount_path) / Path(*relative_parts)
            else:
                raise HTTPException(status_code=400, detail="Invalid file path")
            
            # Validate external path is within mount
            mount_path = Path(external_storage.mount_path).resolve()
            external_file_path_resolved = external_file_path.resolve()
            
            if not str(external_file_path_resolved).startswith(str(mount_path)):
                raise HTTPException(status_code=403, detail="Access denied: path outside mount")
            
            if not external_file_path.exists() or not external_file_path.is_file():
                raise HTTPException(status_code=404, detail="File not found")
            
            full_path = external_file_path
        elif external_storage:
            # User doesn't have access
            raise HTTPException(status_code=403, detail="Access denied: you don't have permission to access this storage")
        else:
            # Regular user storage path
            try:
                safe_path = Path(*[_sanitize_path_component(p) for p in path_parts if p])
                full_path = user_path / safe_path
            except ValueError as e:
                raise HTTPException(status_code=400, detail=f"Invalid path: {e}")
            
            # Validate path is within user directory
            if not _validate_path_within_base(full_path, user_path):
                raise HTTPException(status_code=403, detail="Access denied")
            
            if not full_path.exists() or not full_path.is_file():
                raise HTTPException(status_code=404, detail="File not found")
    else:
        raise HTTPException(status_code=400, detail="Invalid file path")
    
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
    file_paths: Optional[List[str]] = None  # List of file paths to email
    file_urls: Optional[List[str]] = None  # List of file URLs (for note attachments)
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
    
    # Handle file URLs (for note attachments)
    if request.file_urls:
        from urllib.parse import unquote
        for file_url in request.file_urls:
            try:
                # For note attachments, use the storage service to get the file
                if file_url.startswith('/api/notes/files/'):
                    # Parse note attachment URL: /api/notes/files/{username}/{note_id}/{filename}
                    parts = file_url.split('/')
                    if len(parts) >= 6:
                        note_id = int(parts[4])
                        encoded_filename = parts[5].split('?')[0]  # Remove query params
                        filename = unquote(encoded_filename)
                        
                        # Get file from storage service
                        note_path = storage.get_note_path(current_user.username, note_id)
                        file_path = note_path / filename
                        
                        if not file_path.exists():
                            raise HTTPException(status_code=404, detail=f"File not found: {filename}")
                        
                        # Read file
                        def _read_file_sync():
                            with open(file_path, 'rb') as f:
                                return f.read()
                        
                        file_data = await asyncio.to_thread(_read_file_sync)
                        
                        # Determine content type
                        suffix = file_path.suffix.lower()
                        content_types = {
                            '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.png': 'image/png',
                            '.gif': 'image/gif', '.webp': 'image/webp', '.bmp': 'image/bmp',
                            '.pdf': 'application/pdf',
                            '.txt': 'text/plain', '.md': 'text/markdown',
                            '.html': 'text/html', '.css': 'text/css', '.js': 'text/javascript',
                            '.zip': 'application/zip', '.tar': 'application/x-tar', '.gz': 'application/gzip',
                        }
                        content_type = content_types.get(suffix, 'application/octet-stream')
                        
                        attachments.append((filename, file_data, content_type))
                        continue
                
                raise HTTPException(status_code=400, detail=f"Invalid file URL format: {file_url}")
            except HTTPException:
                raise
            except Exception as e:
                logger.error(f"Error fetching file from URL {file_url}: {e}")
                raise HTTPException(status_code=500, detail=f"Error fetching file: {file_url}")
    
    # Handle file paths (regular files)
    if request.file_paths:
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
    
    # Validate we have either file_paths or file_urls
    if not request.file_paths and not request.file_urls:
        raise HTTPException(status_code=400, detail="Either file_paths or file_urls must be provided")
    
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


class DeleteFilesRequest(BaseModel):
    file_paths: List[str]  # List of file/folder paths to delete


@router.post("/delete-bulk")
async def delete_files_bulk(
    request: DeleteFilesRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Delete multiple files or directories."""
    storage = get_storage_service(db)
    user_path = storage.get_user_path(current_user.username)
    
    # Run deletions in thread pool
    def _delete_files_sync():
        deleted = []
        errors = []
        
        for file_path in request.file_paths:
            try:
                # Sanitize and validate path
                safe_path = Path(*[_sanitize_path_component(p) for p in file_path.split('/') if p])
                full_path = user_path / safe_path
                
                # Validate path is within user directory
                if not _validate_path_within_base(full_path, user_path):
                    errors.append(f"{file_path}: Access denied")
                    continue
                
                if not full_path.exists():
                    errors.append(f"{file_path}: Not found")
                    continue
                
                # Delete the file or directory
                if full_path.is_file():
                    full_path.unlink()
                elif full_path.is_dir():
                    import shutil
                    shutil.rmtree(full_path)
                else:
                    errors.append(f"{file_path}: Neither file nor directory")
                    continue
                
                deleted.append(file_path)
            except Exception as e:
                errors.append(f"{file_path}: {str(e)}")
        
        return deleted, errors
    
    try:
        deleted, errors = await asyncio.to_thread(_delete_files_sync)
        
        # Invalidate file cache
        try:
            cache = get_file_cache(db)
            cache.invalidate(f"{current_user.username}:")
            # Invalidate parent directories
            for file_path in request.file_paths:
                parent_path = '/'.join(file_path.split('/')[:-1]) if '/' in file_path else ""
                cache.invalidate(f"{current_user.username}:{parent_path}")
        except Exception as e:
            logger.warning(f"Failed to invalidate cache: {e}")
        
        return {
            "message": f"Deleted {len(deleted)} item(s)",
            "deleted": deleted,
            "errors": errors
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


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


class MoveFilesRequest(BaseModel):
    file_paths: List[str]  # List of file/folder paths to move
    destination: str  # Destination directory path (relative to user root)


@router.post("/move")
async def move_files(
    request: MoveFilesRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Move files or folders to a different location."""
    storage = get_storage_service(db)
    user_path = storage.get_user_path(current_user.username)
    
    # Sanitize and validate destination path
    try:
        if request.destination and request.destination.strip():
            safe_dest = Path(*[_sanitize_path_component(p) for p in request.destination.split('/') if p])
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
        
        for file_path in request.file_paths:
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
            cache = get_file_cache(db)
            cache.invalidate(f"{current_user.username}:")
            # Invalidate both source and destination directories
            for file_path in request.file_paths:
                parent_path = '/'.join(file_path.split('/')[:-1]) if '/' in file_path else ""
                cache.invalidate(f"{current_user.username}:{parent_path}")
            cache.invalidate(f"{current_user.username}:{request.destination}")
        except Exception as e:
            logger.warning(f"Failed to invalidate cache: {e}")
        
        return {
            "message": f"Moved {len(moved)} item(s)",
            "moved": moved,
            "errors": errors
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/upload")
async def upload_file(
    file: UploadFile = File(...),
    path: str = Form("", description="Target directory path (relative to user root)"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Upload a file to the user's storage."""
    storage = get_storage_service(db)
    user_path = storage.get_user_path(current_user.username)
    
    # Sanitize and validate target path
    target_path = user_path
    if path:
        try:
            safe_path = Path(*[_sanitize_path_component(p) for p in path.split('/') if p])
            target_path = user_path / safe_path
            
            # Validate path is within user directory
            if not _validate_path_within_base(target_path, user_path):
                raise HTTPException(status_code=403, detail="Access denied")
            
            # Create directory if it doesn't exist
            target_path.mkdir(parents=True, exist_ok=True)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=f"Invalid path: {e}")
    
    # Sanitize filename
    try:
        safe_filename = _sanitize_path_component(file.filename or "uploaded_file")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=f"Invalid filename: {e}")
    
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
    
    # Read file content
    content = await file.read()
    
    # Run file write in thread pool
    def _write_file_sync():
        try:
            with open(full_file_path, 'wb') as f:
                f.write(content)
            return str(full_file_path.relative_to(user_path))
        except Exception as e:
            raise Exception(f"Error writing file: {e}")
    
    try:
        relative_path = await asyncio.to_thread(_write_file_sync)
        
        # Invalidate file cache
        try:
            cache = get_file_cache(db)
            cache.invalidate(f"{current_user.username}:")
            if path:
                cache.invalidate(f"{current_user.username}:{path}")
        except Exception as e:
            logger.warning(f"Failed to invalidate cache: {e}")
        
        return {
            "message": "File uploaded successfully",
            "path": relative_path,
            "filename": safe_filename
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/mkdir")
async def create_directory(
    path: str = Form(..., description="Directory path relative to user root"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Create a new directory."""
    storage = get_storage_service(db)
    user_path = storage.get_user_path(current_user.username)
    
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
            cache = get_file_cache(db)
            cache.invalidate(f"{current_user.username}:")
            if path:
                parent_path = '/'.join(path.split('/')[:-1]) if '/' in path else ""
                cache.invalidate(f"{current_user.username}:{parent_path}")
        except Exception as e:
            logger.warning(f"Failed to invalidate cache: {e}")
        
        return {
            "message": "Directory created successfully",
            "path": relative_path
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
