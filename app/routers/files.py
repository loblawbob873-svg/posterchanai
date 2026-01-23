"""
File Manager Router - Web-based file manager with image thumbnails and viewer.
Includes configurable memory cache for file listings, email, and public sharing.
All blocking I/O operations are run in thread pools to prevent blocking.
"""
from fastapi import APIRouter, Depends, HTTPException, Query, UploadFile, File, Form
from fastapi.responses import FileResponse, JSONResponse
import json
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
from app.utils.image_validation import validate_and_clean_image_data, validate_and_filter_images, ensure_serializable_image

logger = logging.getLogger(__name__)


def _is_same_server(storage_url: str) -> bool:
    """
    Check if storage_server_url points to the same server (localhost/127.0.0.1).
    When True, we can use local storage instead of HTTP requests.
    """
    if not storage_url:
        return False
    
    try:
        from urllib.parse import urlparse
        parsed = urlparse(storage_url)
        host = parsed.hostname or ""
        # Check if it's localhost or 127.0.0.1
        return host.lower() in ('localhost', '127.0.0.1', '::1', '0.0.0.0') or host == ''
    except Exception:
        return False


def safe_query_setting(db: Session, key: str) -> Optional[Setting]:
    """Safely query a Setting, handling IndexError and other database errors."""
    try:
        return db.query(Setting).filter(Setting.key == key).first()
    except (IndexError, AttributeError) as e:
        logger.warning(f"Error querying setting '{key}': {e}")
        return None
    except Exception as e:
        logger.error(f"Unexpected error querying setting '{key}': {e}", exc_info=True)
        return None

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
    cache_enabled = safe_query_setting(db, "file_cache_enabled")
    if cache_enabled and cache_enabled.value and cache_enabled.value.lower() == "false":
        return NoOpCache()
    
    # Load TTL and max_size settings
    ttl_setting = safe_query_setting(db, "file_cache_ttl")
    max_size_setting = safe_query_setting(db, "file_cache_max_size")
    
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
    # Check if storage server is configured - proxy search if so
    storage_server_url = db.query(Setting).filter(Setting.key == "storage_server_url").first()
    if storage_server_url and storage_server_url.value:
        url = storage_server_url.value.strip()
        if url.startswith(('http://', 'https://')):
            # If same server, skip HTTP proxy and use local storage directly
            if _is_same_server(url):
                logger.debug(f"[FILES] Storage URL points to same server - using local search")
                # Fall through to local search below
            else:
                try:
                    import httpx
                    storage_token = db.query(Setting).filter(Setting.key == "storage_server_token").first()
                    headers = {"X-Username": current_user.username}
                    if storage_token and storage_token.value:
                        headers["Authorization"] = f"Bearer {storage_token.value}"
                    
                    async with httpx.AsyncClient(timeout=60.0) as client:
                        # Try /api/files/search first (if files_router is registered), then fall back to /api/storage/search
                        search_urls = [
                            f"{url.rstrip('/')}/api/files/search",
                            f"{url.rstrip('/')}/api/storage/search"
                        ]
                        response = None
                        for search_url in search_urls:
                            try:
                                response = await client.get(
                                    search_url,
                                    params={"query": query, "username": current_user.username},
                                    headers=headers
                                )
                                if response.status_code == 200:
                                    break
                            except Exception as e:
                                logger.debug(f"Tried {search_url}, got error: {e}")
                                continue
                        
                        if response and response.status_code == 200:
                            return response.json()
                        else:
                            if response:
                                logger.warning(f"Storage server search failed: {response.status_code}, falling back to local search")
                            else:
                                logger.warning(f"Storage server search failed: no response, falling back to local search")
                except Exception as e:
                    logger.warning(f"Failed to proxy search to storage server: {e}, falling back to local search")
    
    # Fall through to local search if proxy fails or storage server doesn't have search endpoint
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
                        
                        # Get thumbnail for images (use stored thumbnail if available)
                        if not is_dir and item.suffix.lower() in ['.jpg', '.jpeg', '.png', '.gif', '.webp', '.bmp']:
                            try:
                                from app.services.thumbnail_service import get_thumbnail_if_exists, generate_thumbnail_for_image
                                
                                # Check for stored thumbnail (user_path is already defined in outer scope)
                                thumbnail_path = get_thumbnail_if_exists(user_path, item)
                                if thumbnail_path and thumbnail_path.exists():
                                    # Use stored thumbnail
                                    thumbnail = generate_thumbnail(thumbnail_path, max_size=(100, 100))
                                    if thumbnail:
                                        item_info["thumbnail"] = thumbnail
                                # If no stored thumbnail exists, don't generate on-the-fly
                                # This prevents performance issues when browsing files
                            except Exception as e:
                                # Silently skip if thumbnail doesn't exist
                                pass
                        
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


@router.get("/all-images")
async def get_all_images(
    limit: int = Query(1000, description="Maximum number of images to return"),
    offset: int = Query(0, description="Offset for pagination"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Get all images and videos from user's storage recursively, sorted by newest first. Supports proxying to storage server."""
    # Check if storage server is configured - proxy request if so
    storage_server_url = safe_query_setting(db, "storage_server_url")
    
    if storage_server_url and storage_server_url.value:
        url = storage_server_url.value.strip()
        if url.startswith(('http://', 'https://')):
            # If same server, skip HTTP proxy and use local storage directly
            if _is_same_server(url):
                logger.debug(f"[FILES] Storage URL points to same server - using local all-images")
                # Fall through to local storage below
            else:
                logger.info(f"[FILES] Proxying get_all_images to storage server: {url}")
                # Proxy to storage server
                try:
                    import httpx
                    storage_server_token = safe_query_setting(db, "storage_server_token")
                    
                    headers = {}
                    if storage_server_token and storage_server_token.value:
                        headers["Authorization"] = f"Bearer {storage_server_token.value}"
                    
                    async with httpx.AsyncClient(timeout=300.0) as client:  # 5 minutes for large scans
                        response = await client.get(
                            f"{url}/api/storage/all-images",
                            params={"username": current_user.username, "limit": limit, "offset": offset},
                            headers=headers
                        )
                        if response.status_code == 200:
                            try:
                                # Get the JSON response from storage server
                                data = response.json()
                                
                                # Clean it to ensure no bytes slipped through
                                def _clean_proxy_response(obj, depth=0):
                                    if depth > 10:
                                        return ""
                                    if obj is None:
                                        return None
                                    elif isinstance(obj, bytes):
                                        logger.warning(f"[FILES] Found bytes in proxy response at depth {depth}, converting")
                                        return obj.decode('utf-8', errors='ignore')
                                    elif isinstance(obj, Path):
                                        logger.warning(f"[FILES] Found Path in proxy response at depth {depth}, converting")
                                        return str(obj)
                                    elif isinstance(obj, dict):
                                        # Preserve all dictionary keys and values
                                        cleaned = {}
                                        for k, v in obj.items():
                                            key_str = str(k) if not isinstance(k, (str, int, float, bool)) else k
                                            cleaned[key_str] = _clean_proxy_response(v, depth+1)
                                        return cleaned
                                    elif isinstance(obj, (list, tuple)):
                                        return [_clean_proxy_response(item, depth+1) for item in obj]
                                    elif isinstance(obj, (str, int, float, bool)):
                                        return obj
                                    else:
                                        # Unknown type - convert to string only as last resort
                                        logger.debug(f"[FILES] Converting unknown type {type(obj)} to string at depth {depth}")
                                        return str(obj)
                                
                                cleaned_data = _clean_proxy_response(data)
                                
                                # Verify that images have required fields and filter out invalid ones
                                if 'images' in cleaned_data and cleaned_data['images']:
                                    valid_images = validate_and_filter_images(cleaned_data['images'], source="proxy")
                                    
                                    # Ensure images are sorted by modified time (newest first)
                                    # Storage server should already sort, but verify and fix if needed
                                    # Also try to extract date from filename if modification time seems wrong
                                    import re
                                    current_time = time.time()
                                    
                                    for img in valid_images:
                                        if 'modified' in img:
                                            try:
                                                img['modified'] = float(img['modified'])
                                            except (ValueError, TypeError):
                                                img['modified'] = 0.0
                                            
                                            # If filename suggests a much older date than modification time,
                                            # try to extract date from filename and use that instead
                                            mtime = img['modified']
                                            if mtime > 0:
                                                mtime_age_days = (current_time - mtime) / 86400
                                                
                                                # Try to extract date from filename
                                                filename = img.get('name', '') or img.get('path', '')
                                                match = re.search(r'(\d{4})(\d{2})(\d{2})(?:[_-](\d{2})(\d{2})(\d{2}))?', filename)
                                                if match:
                                                    year, month, day = int(match.group(1)), int(match.group(2)), int(match.group(3))
                                                    hour, minute, second = 0, 0, 0
                                                    if match.group(4):  # Has time component
                                                        hour, minute, second = int(match.group(4)), int(match.group(5)), int(match.group(6))
                                                    try:
                                                        filename_date = datetime(year, month, day, hour, minute, second)
                                                        filename_timestamp = filename_date.timestamp()
                                                        filename_age_days = (current_time - filename_timestamp) / 86400
                                                        
                                                        # If filename date is significantly older (more than 30 days older than mtime),
                                                        # OR if mtime is very recent but filename suggests old date,
                                                        # use filename date instead. This catches files copied at any time.
                                                        mtime_is_recent = mtime_age_days < 7  # Modified within last week
                                                        filename_is_old = filename_age_days > 30  # Filename suggests date older than 30 days
                                                        
                                                        if filename_age_days > (mtime_age_days + 30) or (mtime_is_recent and filename_is_old):
                                                            img['modified'] = filename_timestamp
                                                            logger.info(f"[FILES] Proxy: Using filename date for {filename}: {filename_date} (mtime was {datetime.fromtimestamp(mtime)}, mtime_age={mtime_age_days:.1f}d, filename_age={filename_age_days:.1f}d)")
                                                    except (ValueError, OverflowError):
                                                        pass  # Invalid date, use mtime
                                    
                                    # Sort by modified time descending (newest first)
                                    # reverse=True means higher timestamps (newer files) come first
                                    # CRITICAL: Ensure all modified values are floats before sorting
                                    for img in valid_images:
                                        if 'modified' in img:
                                            try:
                                                img['modified'] = float(img['modified'])
                                            except (ValueError, TypeError):
                                                img['modified'] = 0.0
                                    
                                    # Sort by modified time descending (newest first)
                                    valid_images.sort(key=lambda x: float(x.get('modified', 0) or 0), reverse=True)
                                    
                                    # ALWAYS verify sort and log results
                                    if len(valid_images) > 1:
                                        first_ts = float(valid_images[0].get('modified', 0) or 0)
                                        last_ts = float(valid_images[-1].get('modified', 0) or 0)
                                        first_name = valid_images[0].get('name', 'unknown')
                                        last_name = valid_images[-1].get('name', 'unknown')
                                        
                                        if first_ts < last_ts:
                                            logger.error(f"[FILES] ❌ PROXY SORT ERROR: First timestamp ({first_ts}) is LOWER than last ({last_ts}) - sort is backwards!")
                                            logger.error(f"[FILES] First image: {first_name} (ts={first_ts}, date={datetime.fromtimestamp(first_ts).isoformat() if first_ts > 0 else 'N/A'})")
                                            logger.error(f"[FILES] Last image: {last_name} (ts={last_ts}, date={datetime.fromtimestamp(last_ts).isoformat() if last_ts > 0 else 'N/A'})")
                                            # Fix by reversing
                                            valid_images.reverse()
                                            logger.warning(f"[FILES] Fixed by reversing the list")
                                            # Verify fix
                                            new_first_ts = float(valid_images[0].get('modified', 0) or 0)
                                            new_last_ts = float(valid_images[-1].get('modified', 0) or 0)
                                            if new_first_ts < new_last_ts:
                                                logger.error(f"[FILES] ❌ REVERSE FIX FAILED! Still backwards after reverse!")
                                            else:
                                                logger.info(f"[FILES] ✓ Sort fixed: New first={new_first_ts} ({datetime.fromtimestamp(new_first_ts).isoformat() if new_first_ts > 0 else 'N/A'}), New last={new_last_ts} ({datetime.fromtimestamp(new_last_ts).isoformat() if new_last_ts > 0 else 'N/A'})")
                                        else:
                                            # Log verification with first few images
                                            logger.info(f"[FILES] ✓ Sort verified: First={first_ts} ({datetime.fromtimestamp(first_ts).isoformat() if first_ts > 0 else 'N/A'}, {first_name}), Last={last_ts} ({datetime.fromtimestamp(last_ts).isoformat() if last_ts > 0 else 'N/A'}, {last_name})")
                                            # Log first 3 images for debugging
                                            for i in range(min(3, len(valid_images))):
                                                img = valid_images[i]
                                                ts = float(img.get('modified', 0) or 0)
                                                logger.info(f"[FILES]   Image #{i+1}: {img.get('name', 'unknown')} - ts={ts}, date={datetime.fromtimestamp(ts).isoformat() if ts > 0 else 'N/A'}")
                                    
                                    # Update with filtered and sorted valid images
                                    cleaned_data['images'] = valid_images
                                    # Note: total should come from the storage server, not len(valid_images) which is just this page
                                    # Don't override total - use what storage server says
                                    
                                    # Log sample of first few images to verify sorting
                                    if len(valid_images) > 0:
                                        logger.info(f"[FILES] Proxy: Received {len(valid_images)} images, sorted by modified time (newest first)")
                                        # Log first 5 images with their timestamps
                                        for i, img in enumerate(valid_images[:5]):
                                            ts = float(img.get('modified', 0) or 0)
                                            date_str = datetime.fromtimestamp(ts).isoformat() if ts > 0 else 'N/A'
                                            logger.debug(f"[FILES] Image #{i+1}: {img.get('name', 'unknown')} - ts={ts}, date={date_str}")
                                    else:
                                        logger.warning(f"[FILES] Proxy: Received 0 valid images after filtering")
                                
                                # Test serialization before returning
                                try:
                                    import json
                                    # Use custom encoder
                                    class BytesSafeEncoder(json.JSONEncoder):
                                        def default(self, obj):
                                            if isinstance(obj, bytes):
                                                return obj.decode('utf-8', errors='ignore')
                                            elif isinstance(obj, Path):
                                                return str(obj)
                                            return super().default(obj)
                                    test_json = json.dumps(cleaned_data, cls=BytesSafeEncoder)
                                    logger.debug(f"[FILES] Proxy response cleaned and validated: {len(cleaned_data.get('images', []))} images")
                                except (TypeError, ValueError) as test_err:
                                    logger.error(f"[FILES] Proxy response still has serialization issues after cleaning: {test_err}")
                                    logger.error(f"[FILES] Error type: {type(test_err).__name__}")
                                    # Try to find the problem with detailed logging
                                    problematic_paths = []
                                    def find_problem(obj, path="root", depth=0):
                                        if depth > 10:
                                            return
                                        try:
                                            if isinstance(obj, bytes):
                                                problematic_paths.append(f"{path} (bytes: {obj[:50] if len(obj) > 50 else obj})")
                                                return
                                            elif isinstance(obj, Path):
                                                problematic_paths.append(f"{path} (Path: {obj})")
                                                return
                                            elif isinstance(obj, dict):
                                                for k, v in obj.items():
                                                    find_problem(v, f"{path}.{k}", depth+1)
                                            elif isinstance(obj, (list, tuple)):
                                                for i, item in enumerate(obj[:20]):
                                                    find_problem(item, f"{path}[{i}]", depth+1)
                                            else:
                                                # Try to serialize this value alone
                                                try:
                                                    json.dumps(obj)
                                                except (TypeError, ValueError) as e:
                                                    problematic_paths.append(f"{path} (type {type(obj).__name__}: {e})")
                                        except Exception as e:
                                            problematic_paths.append(f"{path} (error checking: {e})")
                                    find_problem(cleaned_data)
                                    if problematic_paths:
                                        logger.error(f"[FILES] Found {len(problematic_paths)} problematic paths:")
                                        for p in problematic_paths[:10]:
                                            logger.error(f"[FILES]   - {p}")
                                        # Return safe empty response
                                        return {"images": [], "total": 0, "limit": limit, "offset": offset, "has_more": False}
                                
                                return cleaned_data
                            except Exception as json_err:
                                logger.error(f"[FILES] Error parsing/cleaning storage server response: {json_err}", exc_info=True)
                                raise
                        else:
                            # Try to get error details from response
                            try:
                                error_data = response.json()
                                error_detail = error_data.get("detail", error_data.get("message", response.text))
                            except:
                                error_detail = response.text or f"HTTP {response.status_code}"
                            logger.error(f"[FILES] Storage server returned {response.status_code}: {error_detail}")
                            raise HTTPException(status_code=response.status_code, detail=error_detail)
                except httpx.TimeoutException:
                    logger.error(f"[FILES] Timeout proxying get_all_images to storage server")
                    raise HTTPException(status_code=504, detail="Storage server timeout")
                except httpx.ConnectError as e:
                    logger.error(f"[FILES] Cannot connect to storage server: {e}")
                    raise HTTPException(status_code=503, detail=f"Cannot reach storage server: {e}")
                except HTTPException:
                    raise  # Re-raise HTTP exceptions as-is
                except Exception as e:
                    logger.error(f"[FILES] Failed to proxy get_all_images: {e}", exc_info=True)
                    raise HTTPException(status_code=500, detail=f"Failed to get images from storage server: {str(e)}")
        else:
            raise HTTPException(status_code=500, detail="Invalid storage_server_url configuration")
    
    # Local file listing (storage server node)
    storage = get_storage_service(db)
    user_path = storage.get_user_path(current_user.username)
    
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
            # Use rglob to scan all subdirectories
            logger.info(f"[FILES] Starting recursive scan of {user_path}")
            scanned_count = 0
            for item in user_path.rglob('*'):
                scanned_count += 1
                if scanned_count % 1000 == 0:
                    logger.debug(f"[FILES] Scanned {scanned_count} items so far, found {len(images)} images/videos")
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
                    # Use mtime (modification time) which:
                    # - Is preserved by rsync when using -t or -a flags
                    # - Should match EXIF date after running the storage scan (/api/admin/storage/rescan)
                    # - Represents when the photo was taken, not when it was copied
                    # Only fall back to ctime if mtime is invalid
                    modified_time = stat.st_mtime if stat.st_mtime > 0 else (stat.st_ctime if stat.st_ctime > 0 else time.time())
                    
                    # Try to extract date from filename if modification time seems wrong
                    # Common patterns: YYYYMMDD, YYYYMMDD_HHMMSS, YY-MM-DD HH-MM-SS, etc.
                    filename_date = None
                    filename = item.name

                    import re

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
                        # use filename date instead. This handles cases where files were copied but have old photo dates.
                        current_time = time.time()
                        mtime_age_days = (current_time - modified_time) / 86400
                        filename_age_days = (current_time - filename_timestamp) / 86400

                        # Use filename date if it's significantly older than modification time (30+ days difference)
                        # OR if modification time is very recent (within last 7 days) but filename suggests older date
                        # This catches files that were copied at any time but have old photo dates
                        mtime_is_recent = mtime_age_days < 7  # Modified within last week
                        filename_is_old = filename_age_days > 30  # Filename suggests date older than 30 days

                        if filename_age_days > (mtime_age_days + 30) or (mtime_is_recent and filename_is_old):
                            # Filename suggests much older date than modification time
                            # Use filename date for sorting
                            modified_time = filename_timestamp
                            logger.info(f"[FILES] Using filename date for {filename}: {filename_date} (mtime was {datetime.fromtimestamp(stat.st_mtime)}, mtime_age={mtime_age_days:.1f}d, filename_age={filename_age_days:.1f}d)")
                    else:
                        # No filename date extracted - check if this is a recently copied file
                        current_time = time.time()
                        mtime_age_days = (current_time - modified_time) / 86400

                        # If mtime is very recent (< 7 days) and no filename date, this is likely
                        # a file copied via rsync with no EXIF data. Push to bottom by using a very old date.
                        if mtime_age_days < 7:
                            modified_time = 0.0  # Epoch time - will sort to bottom
                            logger.info(f"[FILES] No filename date for {filename}, recent mtime ({mtime_age_days:.1f}d old) - pushing to bottom")
                    
                    # Debug: Log if file timestamp seems suspiciously old (more than 1 year old)
                    # This helps identify files that might not have had EXIF restoration applied
                    file_age_days = (time.time() - modified_time) / 86400
                    if file_age_days > 365 and (is_image or is_video):
                        logger.debug(f"[FILES] Old file timestamp detected: {item.name} has mtime={datetime.fromtimestamp(modified_time).date()} ({file_age_days:.0f} days old) - may need EXIF restoration")
                    
                    # Note: For accurate sorting by photo date, run /api/admin/storage/rescan
                    # which restores file timestamps from EXIF metadata. Without this, files
                    # may be sorted by copy/upload date rather than when the photo was taken.
                    
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
                    # Check if filename suggests it's a thumbnail (e.g., thumb_, thumbnail_, _thumb, etc.)
                    name_lower = item.name.lower()
                    if any(pattern in name_lower for pattern in ['thumb', 'thumbnail', '_tn', '_small', '_mini', '_thumb']):
                        # But allow if it's in a normal directory (not .thumbnails)
                        try:
                            relative = item.relative_to(user_path)
                            if '.thumbnails' not in relative.parts:
                                # It's a regular file with "thumb" in the name, allow it
                                pass
                            else:
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
                            logger.warning(f"[FILES] Found bytes in image_info.{key}, converting")
                            image_info[key] = value.decode('utf-8', errors='ignore')
                        elif not isinstance(value, (str, int, float, bool, type(None))):
                            logger.warning(f"[FILES] Found non-serializable type {type(value)} in image_info.{key}, converting")
                            image_info[key] = str(value)
                    
                    # Validate and clean image data
                    cleaned_image_info = validate_and_clean_image_data(image_info, item_path=item)
                    if not cleaned_image_info:
                        continue
                    image_info = cleaned_image_info
                    
                    # Skip loading thumbnails during initial scan for performance
                    # Thumbnails will be loaded on-demand by the frontend via /thumbnail endpoint
                    # This dramatically speeds up the initial scan when there are thousands of files
                    
                    images.append(image_info)
                except Exception as e:
                    logger.warning(f"Error processing image {item}: {e}")
                    continue
        except Exception as e:
            logger.error(f"Error getting all images: {e}", exc_info=True)
            import traceback
            logger.error(f"[FILES] Traceback in _get_all_images_sync: {traceback.format_exc()}")
            raise Exception(f"Error getting all images: {e}")
        
        # Remove duplicates based on path FIRST (before sorting)
        # Keep the first occurrence of each path
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
        # Ensure modified is a number, not string, and convert to float explicitly
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
        # Use explicit reverse=True for clarity - higher timestamps (newer) come first
        def sort_key(img):
            modified = float(img.get('modified', 0) or 0)
            path = str(img.get('path', '')).lower()
            # Use a very large number for files with no timestamp to push them to the end
            if modified <= 0:
                modified = 0.0
            # Return tuple: (modified, path) - we'll use reverse=True to get descending order
            return (modified, path)
        
        # Sort the list - descending by modified time (newest first)
        # reverse=True means higher timestamps come first (newest first)
        images.sort(key=sort_key, reverse=True)
        
        # Verify sort is correct - first item should have highest timestamp
        if len(images) > 1:
            first_ts = float(images[0].get('modified', 0) or 0)
            last_ts = float(images[-1].get('modified', 0) or 0)
            first_name = images[0].get('name', 'unknown')
            last_name = images[-1].get('name', 'unknown')
            
            if first_ts < last_ts:
                logger.error(f"[FILES] ❌ SORT ERROR: First image timestamp ({first_ts}) is LOWER than last ({last_ts}) - sort is backwards!")
                logger.error(f"[FILES] First image: {first_name} (ts={first_ts}, date={datetime.fromtimestamp(first_ts).isoformat() if first_ts > 0 else 'N/A'})")
                logger.error(f"[FILES] Last image: {last_name} (ts={last_ts}, date={datetime.fromtimestamp(last_ts).isoformat() if last_ts > 0 else 'N/A'})")
                # Fix it by reversing
                images.reverse()
                logger.warning(f"[FILES] Fixed by reversing the list")
                # Verify fix worked
                new_first_ts = float(images[0].get('modified', 0) or 0)
                new_last_ts = float(images[-1].get('modified', 0) or 0)
                if new_first_ts < new_last_ts:
                    logger.error(f"[FILES] ❌ REVERSE FIX FAILED! Still backwards after reverse!")
                else:
                    logger.info(f"[FILES] ✓ Reverse fix verified: First={new_first_ts} ({datetime.fromtimestamp(new_first_ts).isoformat() if new_first_ts > 0 else 'N/A'}), Last={new_last_ts} ({datetime.fromtimestamp(new_last_ts).isoformat() if new_last_ts > 0 else 'N/A'})")
            else:
                # Log verification
                logger.info(f"[FILES] ✓ Sort verified: First={first_ts} ({datetime.fromtimestamp(first_ts).isoformat() if first_ts > 0 else 'N/A'}, {first_name}), Last={last_ts} ({datetime.fromtimestamp(last_ts).isoformat() if last_ts > 0 else 'N/A'}, {last_name})")
                # Log first 3 images for debugging
                for i in range(min(3, len(images))):
                    img = images[i]
                    ts = float(img.get('modified', 0) or 0)
                    logger.info(f"[FILES]   Image #{i+1}: {img.get('name', 'unknown')} - ts={ts}, date={datetime.fromtimestamp(ts).isoformat() if ts > 0 else 'N/A'}")
        
        # Immediate verification: check first 50 items are in correct order
        # Log detailed information about sorting for debugging
        if images:
            newest_ts = images[0].get('modified', 0)
            oldest_in_first_50_ts = images[min(49, len(images)-1)].get('modified', 0) if len(images) > 49 else newest_ts
            newest_date = datetime.fromtimestamp(newest_ts) if newest_ts > 0 else None
            oldest_in_first_50 = datetime.fromtimestamp(oldest_in_first_50_ts) if oldest_in_first_50_ts > 0 else None
            
            # Check if newest photo is suspiciously old (more than 1 year old)
            current_time = time.time()
            if newest_ts > 0:
                age_days = (current_time - newest_ts) / 86400
                if age_days > 365:
                    logger.warning(f"[FILES] ⚠️ WARNING: Newest photo is {age_days:.0f} days old ({newest_date.date() if newest_date else 'N/A'})")
                    logger.warning(f"[FILES] This suggests EXIF restoration may not have run, or files don't have EXIF dates")
                    logger.warning(f"[FILES] Run /api/admin/storage/rescan to restore timestamps from EXIF metadata")
            
            logger.info(f"[FILES] Sort verification: Newest={newest_date}, Oldest in first 50={oldest_in_first_50}, Total images={len(images)}")
        
        prev_ts = None
        sort_errors = []
        for i, img in enumerate(images[:50]):
            curr_ts = float(img.get('modified', 0) or 0)
            if prev_ts is not None and curr_ts > prev_ts:
                sort_errors.append((i, img.get('name'), curr_ts, prev_ts))
                logger.warning(f"[FILES] Sort issue at index {i}: {img.get('name')} (ts={curr_ts}) is NEWER than previous (ts={prev_ts})")
                # Log the actual dates for debugging
                try:
                    from datetime import datetime
                    curr_date = datetime.fromtimestamp(curr_ts).strftime('%Y-%m-%d %H:%M:%S')
                    prev_date = datetime.fromtimestamp(prev_ts).strftime('%Y-%m-%d %H:%M:%S')
                    logger.warning(f"[FILES]   Current: {curr_date}, Previous: {prev_date}")
                except:
                    pass
            prev_ts = curr_ts
        
        if sort_errors:
            logger.warning(f"[FILES] Found {len(sort_errors)} sorting issues in first 50 images")
            # Log first 5 errors with full details
            for idx, name, curr, prev in sort_errors[:5]:
                logger.warning(f"[FILES] Issue {idx}: {name} - current={curr}, previous={prev}, diff={curr-prev}")
            # If many errors, the sort might be reversed - try fixing it
            if len(sort_errors) > 10:
                logger.warning("[FILES] Too many sort issues - attempting to fix by re-sorting...")
                # CRITICAL: Must use reverse=True to keep newest first!
                images.sort(key=sort_key, reverse=True)  # Re-sort with reverse=True
                logger.warning("[FILES] Re-sorted array with reverse=True (newest first)")
        else:
            logger.info(f"[FILES] ✓ Sort verified: First 50 images in correct order (newest first)")
        
        # Log timestamp range for debugging
        if images:
            newest_ts = float(images[0].get('modified', 0) or 0)
            oldest_ts = float(images[-1].get('modified', 0) or 0) if len(images) > 1 else newest_ts
            try:
                from datetime import datetime
                newest_date = datetime.fromtimestamp(newest_ts).strftime('%Y-%m-%d %H:%M:%S')
                oldest_date = datetime.fromtimestamp(oldest_ts).strftime('%Y-%m-%d %H:%M:%S')
                logger.info(f"[FILES] Timestamp range: Newest={newest_date}, Oldest={oldest_date}")
            except:
                pass
        
        # Debug: log statistics
        total_scanned = len(images) + skipped_count
        logger.info(f"[FILES] Image scan complete:")
        logger.info(f"  - Total files scanned: {total_scanned}")
        logger.info(f"  - Valid images/videos: {len(images)}")
        logger.info(f"  - Files skipped: {skipped_count}")
        logger.info(f"  - Duplicates removed: {duplicates_removed}")
        if skipped_reasons:
            logger.info(f"  - Skip reasons: {dict(skipped_reasons)}")
        
        # Log sample of oldest and newest files for debugging
        if images:
            logger.info(f"[FILES] Sample files (first 5 - should be newest):")
            for i, img in enumerate(images[:5]):
                ts = img.get('modified', 0)
                date_str = datetime.fromtimestamp(ts).strftime('%Y-%m-%d') if ts > 0 else 'N/A'
                logger.info(f"    {i+1}. {img.get('name', 'unknown')} - {date_str} (ts={ts})")
            if len(images) > 5:
                logger.info(f"[FILES] Sample files (last 5 - should be oldest):")
                for i, img in enumerate(images[-5:]):
                    ts = img.get('modified', 0)
                    date_str = datetime.fromtimestamp(ts).strftime('%Y-%m-%d') if ts > 0 else 'N/A'
                    logger.info(f"    {len(images)-4+i}. {img.get('name', 'unknown')} - {date_str} (ts={ts})")
        if skipped_reasons:
            logger.info(f"  - Skip reasons breakdown: {skipped_reasons}")
        logger.info(f"[FILES] Final count returned to client: {len(images)} images/videos")
        
        # Debug: log first few images to verify sorting
        if images:
            logger.info(f"[FILES] Found {len(images)} images total. First 20 (newest first):")
            for i, img in enumerate(images[:20]):
                mod_time = float(img.get('modified', 0) or 0)
                mod_date = datetime.fromtimestamp(mod_time).isoformat() if mod_time > 0 else 'N/A'
                path_info = img.get('path', 'unknown')
                # Check if this looks like a thumbnail file
                is_thumbnail = '.thumbnails' in path_info.lower()
                thumb_marker = " [THUMBNAIL?]" if is_thumbnail else ""
                logger.info(f"  {i+1}. {img.get('name')} - modified: {mod_time} ({mod_date}) - path: {path_info}{thumb_marker}")
            
            # Verify sorting - check that newer files come before older files
            # For newest-first: each file should have timestamp <= previous file
            prev_time = None
            sorting_errors = []
            for i, img in enumerate(images[:200]):  # Check first 200
                curr_time = float(img.get('modified', 0) or 0)
                if prev_time is not None and curr_time > prev_time:
                    # Current file is newer than previous - this is WRONG for newest-first sorting
                    sorting_errors.append((i, img.get('name'), curr_time, prev_time, img.get('path', 'unknown')))
                    logger.error(f"[FILES] ❌ Sorting error at index {i}: {img.get('name')} (time: {curr_time}, path: {img.get('path', 'unknown')}) is NEWER than previous (time: {prev_time}) - should be OLDER or EQUAL!")
                prev_time = curr_time
            
            if not sorting_errors:
                logger.info(f"[FILES] ✓ Sorting verified: All {min(200, len(images))} checked images are in correct order (newest first)")
            else:
                logger.error(f"[FILES] ❌ Found {len(sorting_errors)} sorting errors in first 200 images!")
                logger.error(f"[FILES] First 5 errors: {sorting_errors[:5]}")
                # Log sample of timestamps to debug
                logger.error(f"[FILES] Sample timestamps from first 20 files:")
                for i, img in enumerate(images[:20]):
                    logger.error(f"  [{i}] {img.get('name')}: modified={float(img.get('modified', 0) or 0)}")
        
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
        # Recursively clean the entire result to ensure JSON serializability
        cleaned_result = _clean_for_json(result)
        
        # Double-check: ensure the result dict itself is clean
        if isinstance(cleaned_result, dict):
            # Ensure all top-level values are serializable
            final_result = {}
            for key, value in cleaned_result.items():
                if isinstance(key, bytes):
                    key = key.decode('utf-8', errors='ignore')
                if isinstance(value, bytes):
                    value = value.decode('utf-8', errors='ignore')
                elif isinstance(value, Path):
                    value = str(value)
                final_result[str(key)] = value
            cleaned_result = final_result
        
        # Custom JSON encoder for testing
        class BytesSafeEncoder(json.JSONEncoder):
            def default(self, obj):
                if isinstance(obj, bytes):
                    return obj.decode('utf-8', errors='ignore')
                elif isinstance(obj, Path):
                    return str(obj)
                return super().default(obj)
        
        # CRITICAL: Test serialization BEFORE returning to catch any remaining bytes
        try:
            test_json = json.dumps(cleaned_result, cls=BytesSafeEncoder)
            # If successful, parse it back to ensure it's valid
            json.loads(test_json)
            logger.debug(f"[FILES] Successfully serialized result: {len(cleaned_result.get('images', []))} images")
        except TypeError as json_err:
            # Find the problematic value
            logger.error(f"[FILES] JSON serialization test failed: {json_err}")
            # Try to find which field has bytes
            def find_bytes(obj, path="root", depth=0):
                if depth > 5:
                    return
                if isinstance(obj, bytes):
                    logger.error(f"[FILES] Found bytes at path: {path}, value: {obj[:50] if len(obj) > 50 else obj}")
                    return path
                elif isinstance(obj, dict):
                    for k, v in obj.items():
                        find_bytes(v, f"{path}.{k}", depth+1)
                elif isinstance(obj, (list, tuple)):
                    for i, item in enumerate(obj[:10]):  # Check first 10 items
                        find_bytes(item, f"{path}[{i}]", depth+1)
            find_bytes(cleaned_result)
            # Return minimal safe response
            return JSONResponse(content={"images": [], "total": 0, "limit": limit, "offset": offset, "has_more": False})
        
        # Return as JSONResponse - use custom encoder to handle any edge cases
        try:
            return JSONResponse(content=cleaned_result)
        except Exception as final_err:
            logger.error(f"[FILES] JSONResponse failed even after cleaning: {final_err}")
            # Last resort: manually serialize with our encoder
            try:
                json_str = json.dumps(cleaned_result, cls=BytesSafeEncoder, ensure_ascii=False)
                return JSONResponse(content=json.loads(json_str))
            except Exception as last_err:
                logger.error(f"[FILES] Manual JSON encoding also failed: {last_err}")
                return JSONResponse(content={"images": [], "total": 0, "limit": limit, "offset": offset, "has_more": False})
    except Exception as e:
        logger.error(f"[FILES] Error in get_all_images: {e}", exc_info=True)
        import traceback
        logger.error(f"[FILES] Traceback: {traceback.format_exc()}")
        # Ensure error message is also JSON serializable
        error_msg = str(e)
        if isinstance(e, bytes):
            error_msg = e.decode('utf-8', errors='ignore')
        raise HTTPException(status_code=500, detail=error_msg)


@router.get("/list")
async def list_files(
    path: str = Query("", description="Directory path relative to user root or external storage mount point"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """List files and directories in user's storage or external storage. Uses memory cache if enabled."""
    # Check if storage server is configured - proxy request if so (for user storage only, not external)
    storage_server_url = safe_query_setting(db, "storage_server_url")
    if storage_server_url and storage_server_url.value:
        url = storage_server_url.value.strip()
        if url.startswith(('http://', 'https://')):
            # If same server, skip HTTP proxy and use WebDAV backend (not local filesystem)
            if _is_same_server(url):
                logger.debug(f"[FILES] Storage URL points to same server - using WebDAV backend")
                # Fall through to WebDAV backend below (not local filesystem)
            else:
                # Check if this is an external storage path (don't proxy external storage)
                is_external = False
                if path:
                    path_parts = path.split('/')
                    if path_parts and path_parts[0]:
                        mount_point = path_parts[0]
                        external_storage = db.query(ExternalStorage).filter(
                            ExternalStorage.mount_point == mount_point,
                            ExternalStorage.is_active == True
                        ).first()
                        if external_storage:
                            is_external = True
                
                if not is_external:
                    # Proxy to storage server - no fallback
                    return await _proxy_list_files(url, current_user.username, path, db)
    
    # On storage server: Check for external storage first, then use WebDAV backend (replaces local filesystem)
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
    
    # Handle regular user storage path - use WebDAV instead of local filesystem
    if not external_storage:
        # Check cache first (but only if WebDAV is enabled - cache might have old local filesystem data)
        cache = get_file_cache(db)
        normalized_path = path.strip('/') if path else ""
        cache_key = f"{current_user.username}:{normalized_path}:user"
        
        # Use WebDAV backend for user storage
        from app.services.webdav_storage_client import WebDAVStorageClient
        webdav_client = WebDAVStorageClient(db, current_user.id)
        
        if not webdav_client.is_enabled():
            raise HTTPException(
                status_code=500, 
                detail=f"WebDAV storage not configured for user {current_user.username}. Please configure WebDAV storage in user settings."
            )
        
        # Check cache AFTER confirming WebDAV is enabled (to avoid returning old local filesystem cache)
        # IMPORTANT: Invalidate cache for user storage when WebDAV is enabled to prevent returning old local filesystem data
        cache.invalidate(f"{current_user.username}:*:user")
        logger.info(f"[FILES] Invalidated cache for user {current_user.username} (WebDAV enabled)")
        
        # Don't use cache for WebDAV - always fetch fresh data
        # (Cache might contain old local filesystem data)
        cached_result = None
        
        logger.debug(f"[FileCache] Cache miss for {cache_key}, fetching from WebDAV")
        
        # Use WebDAV to list files
        try:
            # Convert path to WebDAV path format
            webdav_path = path if path else ""
            logger.info(f"[FILES] Listing WebDAV files for user {current_user.username}, path: '{webdav_path}'")
            
            async def _list_from_webdav():
                logger.debug(f"[FILES] Calling webdav_client.list_files('{webdav_path}', recursive=False)")
                items = await webdav_client.list_files(webdav_path, recursive=False)
                logger.info(f"[FILES] WebDAV returned {len(items)} items")
                # Convert WebDAV items to file manager format
                result_items = []
                for item in items:
                    # Ensure path is relative (no leading slash) for file manager
                    item_path = item["path"].lstrip('/')
                    logger.debug(f"[FILES] Processing WebDAV item: name={item.get('name')}, path={item_path}, is_dir={item.get('is_directory')}")
                    result_items.append({
                        "name": item["name"],
                        "path": item_path,
                        "is_directory": item["is_directory"],
                        "size": item["size"],
                        "modified": item["modified"],
                        "is_external": False
                    })
                logger.info(f"[FILES] Converted {len(result_items)} items for file manager")
                return result_items
            
            items = await _list_from_webdav()
            logger.info(f"[FILES] Got {len(items)} items from WebDAV")
            
            # Calculate storage usage (WebDAV doesn't provide this easily, so return 0 for now)
            result = {
                "items": items,
                "path": path if path else "",
                "is_external": False,
                "external_name": None,
                "storage": {
                    "used": 0,  # WebDAV doesn't provide quota info easily
                    "quota": current_user.storage_quota,
                    "quota_mb": current_user.storage_quota / (1024 * 1024) if current_user.storage_quota > 0 else 0,
                    "used_mb": 0,
                    "unlimited": current_user.storage_quota == 0
                }
            }
            
            # Cache the result (cache was already retrieved above)
            cache.set(cache_key, result)
            
            return result
        except Exception as e:
            logger.error(f"[FILES] Failed to list files from WebDAV: {e}", exc_info=True)
            # Don't fall through to local filesystem - raise error instead
            raise HTTPException(status_code=500, detail=f"Failed to list files from WebDAV: {str(e)}")
    
    # External storage - use local filesystem (external storage is always local)
    # NOTE: This code should ONLY run for external storage mounts, NOT for user storage
    if not external_storage:
        # This should never happen - if we get here, it's a logic error
        logger.error(f"[FILES] ERROR: Reached external storage code path but external_storage is None for user {current_user.username}")
        raise HTTPException(status_code=500, detail="Internal error: invalid storage path")
    # Determine which path to use
    target_path = external_path
    base_path = Path(external_storage.mount_path)
    
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
                    
                    # Get thumbnail for images (use stored thumbnail if available, otherwise generate)
                    if not is_dir and item.suffix.lower() in ['.jpg', '.jpeg', '.png', '.gif', '.webp', '.bmp']:
                        try:
                            # Try to use stored thumbnail first
                            from app.services.thumbnail_service import get_thumbnail_if_exists, generate_thumbnail_for_image
                            
                            if not external_storage:
                                # For user storage, check for stored thumbnail
                                thumbnail_path = get_thumbnail_if_exists(user_path, item)
                                if thumbnail_path and thumbnail_path.exists():
                                    # Use stored thumbnail only
                                    thumbnail = generate_thumbnail(thumbnail_path, max_size=(200, 200))
                                    if thumbnail:
                                        item_info["thumbnail"] = thumbnail
                                # If no stored thumbnail exists, don't generate on-the-fly
                                # This prevents performance issues when browsing files
                            # For external storage, skip thumbnails (they're not stored)
                        except Exception as e:
                            # Silently skip if thumbnail doesn't exist
                            pass
                    
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
    """View/download a file. Returns image viewer HTML for images. Supports external storage. Proxies to storage server if configured (NO FALLBACK)."""
    # Check if this is an external storage path (don't proxy external storage)
    path_parts = file_path.split('/')
    is_external = False
    if path_parts and path_parts[0]:
        mount_point = path_parts[0]
        external_storage = db.query(ExternalStorage).filter(
            ExternalStorage.mount_point == mount_point,
            ExternalStorage.is_active == True
        ).first()
        if external_storage and current_user in external_storage.allowed_users:
            is_external = True
    
    # Check if storage server is configured - proxy request if so (for user storage only, not external)
    if not is_external:
        storage_server_url = safe_query_setting(db, "storage_server_url")
        if storage_server_url and storage_server_url.value:
            url = storage_server_url.value.strip()
            if url.startswith(('http://', 'https://')):
                logger.info(f"[FILES] Proxying view_file to storage server: {url}")
                # Proxy to storage server - NO FALLBACK
                return await _proxy_view_file(url, current_user.username, file_path, db)
            else:
                raise HTTPException(status_code=500, detail="Invalid storage_server_url configuration")
        else:
            # On storage server: Use WebDAV backend (replaces local file serving)
            from app.services.webdav_storage_client import WebDAVStorageClient
            webdav_client = WebDAVStorageClient(db, current_user.id)
            
            if not webdav_client.is_enabled():
                raise HTTPException(
                    status_code=500, 
                    detail=f"WebDAV storage not configured for user {current_user.username}. Please configure WebDAV storage in user settings."
                )
            
            # Get file from WebDAV
            try:
                file_data = await webdav_client.get_file(file_path)
                
                # Determine content type
                from mimetypes import guess_type
                content_type, _ = guess_type(file_path)
                if not content_type:
                    content_type = "application/octet-stream"
                
                # Check if it's an image (for image viewer)
                if content_type.startswith('image/'):
                    # Return image viewer HTML
                    from fastapi.responses import HTMLResponse
                    import base64
                    image_base64 = base64.b64encode(file_data).decode('utf-8')
                    html_content = f"""
                    <!DOCTYPE html>
                    <html>
                    <head>
                        <title>{file_path.split('/')[-1]}</title>
                        <style>
                            body {{ margin: 0; padding: 20px; background: #1a1a1a; color: #fff; text-align: center; }}
                            img {{ max-width: 100%; max-height: 90vh; border-radius: 8px; box-shadow: 0 4px 20px rgba(0,0,0,0.5); }}
                        </style>
                    </head>
                    <body>
                        <img src="data:{content_type};base64,{image_base64}" alt="{file_path.split('/')[-1]}" />
                    </body>
                    </html>
                    """
                    return HTMLResponse(content=html_content)
                else:
                    # Return file download
                    from fastapi.responses import Response
                    filename = file_path.split('/')[-1]
                    return Response(
                        content=file_data,
                        media_type=content_type,
                        headers={
                            "Content-Disposition": f'attachment; filename="{filename}"'
                        }
                    )
            except FileNotFoundError:
                raise HTTPException(status_code=404, detail="File not found")
            except Exception as e:
                logger.error(f"[FILES] Failed to get file from WebDAV: {e}", exc_info=True)
                raise HTTPException(status_code=500, detail=f"Failed to get file from WebDAV: {str(e)}")
    
    # Handle external storage (external storage is always local filesystem)
    storage = get_storage_service(db)
    user_path = storage.get_user_path(current_user.username)
    
    # Check if this is an external storage path
    external_storage = None
    external_file_path = None
    
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
            # This shouldn't happen - we already handled WebDAV above
            raise HTTPException(status_code=500, detail="Invalid file path")
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
    
    # For images, set headers to display inline instead of triggering download
    headers = {}
    if suffix in ['.jpg', '.jpeg', '.png', '.gif', '.webp', '.bmp']:
        headers['Content-Disposition'] = 'inline'
    
    return FileResponse(
        full_path,
        media_type=media_type,
        filename=full_path.name,
        headers=headers
    )


@router.get("/thumbnail/{file_path:path}")
async def get_thumbnail(
    file_path: str,
    size: int = Query(200, description="Thumbnail size"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Get thumbnail for an image file. Supports external storage. Proxies to storage server if configured (NO FALLBACK)."""
    # Check if this is an external storage path (don't proxy external storage)
    path_parts = file_path.split('/')
    is_external = False
    if path_parts and path_parts[0]:
        mount_point = path_parts[0]
        external_storage = db.query(ExternalStorage).filter(
            ExternalStorage.mount_point == mount_point,
            ExternalStorage.is_active == True
        ).first()
        if external_storage and current_user in external_storage.allowed_users:
            is_external = True
    
    # Check if storage server is configured - proxy request if so (for user storage only, not external)
    if not is_external:
        storage_server_url = safe_query_setting(db, "storage_server_url")
        if storage_server_url and storage_server_url.value:
            url = storage_server_url.value.strip()
            if url.startswith(('http://', 'https://')):
                logger.info(f"[FILES] Proxying get_thumbnail to storage server: {url}")
                # Proxy to storage server - NO FALLBACK
                return await _proxy_get_thumbnail(url, current_user.username, file_path, size, db)
            else:
                raise HTTPException(status_code=500, detail="Invalid storage_server_url configuration")
        else:
            raise HTTPException(status_code=500, detail="Storage server not configured. Cannot get thumbnail.")
    
    # Handle external storage or local fallback (only for external storage)
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
            # Regular user storage path (should not reach here if proxying is configured)
            raise HTTPException(status_code=500, detail="Storage server not configured. Cannot get thumbnail.")
    else:
        raise HTTPException(status_code=400, detail="Invalid file path")
    
    # Try to use stored thumbnail first, then generate if needed
    try:
        from app.services.thumbnail_service import (
            get_thumbnail_if_exists, 
            generate_thumbnail_for_image, 
            generate_thumbnail_for_video_file,
            is_image_file,
            is_video_file
        )
        
        # Check if file is image or video
        is_image = is_image_file(full_path)
        is_video = is_video_file(full_path)
        
        if not is_image and not is_video:
            raise HTTPException(status_code=400, detail="File is not an image or video")
        
        # CRITICAL: Only use stored thumbnails - never generate on-the-fly!
        # Thumbnails should be generated during upload, not when viewing gallery.
        # This prevents ffmpeg from running every time the gallery loads.
        if not external_storage:
            thumbnail_path = get_thumbnail_if_exists(user_path, full_path)
            if thumbnail_path and thumbnail_path.exists():
                # Use stored thumbnail only
                thumbnail_data = await asyncio.to_thread(generate_thumbnail, thumbnail_path, (size, size))
                if thumbnail_data:
                    return JSONResponse({"thumbnail": thumbnail_data})
        
        # No stored thumbnail exists - return 404 instead of generating on-the-fly
        # The frontend will handle this gracefully by showing a placeholder or the full image
        raise HTTPException(status_code=404, detail="Thumbnail not found. Thumbnails are generated during upload.")
    except HTTPException:
        raise
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
                        
                        # Get file from WebDAV
                        from app.services.webdav_storage_client import WebDAVStorageClient
                        webdav_client = WebDAVStorageClient(db, current_user.id)
                        
                        if not webdav_client.is_enabled():
                            raise HTTPException(
                                status_code=500, 
                                detail=f"WebDAV storage not configured for user {current_user.username}."
                            )
                        
                        webdav_path = f"notes/{note_id}/{filename}"
                        file_data = await webdav_client.get_file(webdav_path)
                        
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
                    else:
                        raise HTTPException(status_code=400, detail=f"Invalid file URL format: {file_url}")
                else:
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

        # Check file exists - support both local and remote storage
        file_exists = False
        storage_server_url = db.query(Setting).filter(Setting.key == "storage_server_url").first()
        if storage_server_url and storage_server_url.value and storage_server_url.value.strip():
            # For remote storage, trust that the file exists if path validation passed
            # (remote listing is expensive, share creation will fail on access if file doesn't exist)
            file_exists = True
        else:
            file_exists = full_path.exists() and full_path.is_file()

        if not file_exists:
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
    """Delete multiple files or directories. Proxies to storage server if configured (NO FALLBACK)."""
    logger.info(f"[FILES] Received delete_files_bulk request for user {current_user.username}: {len(request.file_paths)} file(s)")
    logger.debug(f"[FILES] Files to delete: {request.file_paths}")
    
    # Check if storage server is configured - proxy request if so (NO FALLBACK)
    storage_server_url = safe_query_setting(db, "storage_server_url")
    if storage_server_url and storage_server_url.value:
        url = storage_server_url.value.strip()
        if url.startswith(('http://', 'https://')):
            logger.info(f"[FILES] Proxying delete_files_bulk to storage server: {url}")
            # Proxy to storage server - NO FALLBACK
            try:
                result = await _proxy_delete_files_bulk(url, current_user.username, request.file_paths, db)
                logger.info(f"[FILES] Successfully proxied delete_files_bulk to storage server: {result.get('message', 'OK')}")
                logger.debug(f"[FILES] Deleted: {result.get('deleted', [])}, Errors: {result.get('errors', [])}")
                return result
            except Exception as e:
                logger.error(f"[FILES] Error proxying delete_files_bulk: {e}", exc_info=True)
                raise HTTPException(status_code=500, detail=f"Failed to delete files: {str(e)}")
        else:
            raise HTTPException(status_code=500, detail="Invalid storage_server_url configuration")
    else:
        raise HTTPException(status_code=500, detail="Storage server not configured. Cannot delete files.")


@router.delete("/delete")
async def delete_file(
    file_path: str = Query(..., description="File path relative to user root"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Delete a file or directory. Proxies to storage server if configured (NO FALLBACK)."""
    # Check if storage server is configured - proxy request if so (NO FALLBACK)
    storage_server_url = safe_query_setting(db, "storage_server_url")
    if storage_server_url and storage_server_url.value:
        url = storage_server_url.value.strip()
        if url.startswith(('http://', 'https://')):
            logger.info(f"[FILES] Proxying delete to storage server: {url}")
            # Proxy to storage server - NO FALLBACK
            result = await _proxy_delete_file(url, current_user.username, file_path, db)
            logger.info(f"[FILES] Successfully proxied delete to storage server")
            return result
        else:
            raise HTTPException(status_code=500, detail="Invalid storage_server_url configuration")
    else:
        raise HTTPException(status_code=500, detail="Storage server not configured. Cannot delete files.")


class MoveFilesRequest(BaseModel):
    file_paths: List[str]  # List of file/folder paths to move
    destination: str  # Destination directory path (relative to user root)


@router.post("/move")
async def move_files(
    request: MoveFilesRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Move files or folders to a different location. Proxies to storage server if configured (NO FALLBACK)."""
    # Check if storage server is configured - proxy request if so (NO FALLBACK)
    storage_server_url = safe_query_setting(db, "storage_server_url")
    if storage_server_url and storage_server_url.value:
        url = storage_server_url.value.strip()
        if url.startswith(('http://', 'https://')):
            logger.info(f"[FILES] Proxying move_files to storage server: {url}")
            # Proxy to storage server - NO FALLBACK
            result = await _proxy_move_files(url, current_user.username, request.file_paths, request.destination, db)
            logger.info(f"[FILES] Successfully proxied move_files to storage server")
            return result
        else:
            raise HTTPException(status_code=500, detail="Invalid storage_server_url configuration")
    else:
        raise HTTPException(status_code=500, detail="Storage server not configured. Cannot move files.")


@router.post("/upload")
async def upload_file(
    file: UploadFile = File(...),
    path: str = Form("", description="Target directory path (relative to user root)"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Upload a file to the user's storage. Proxies to storage server if configured."""
    # Read file content once (needed for both proxy and local storage)
    content = await file.read()
    filename = file.filename or "uploaded_file"
    content_type = file.content_type or "application/octet-stream"
    
    # Check if storage server is configured - proxy request if so
    storage_server_url = safe_query_setting(db, "storage_server_url")
    if storage_server_url and storage_server_url.value:
        url = storage_server_url.value.strip()
        if url.startswith(('http://', 'https://')):
            logger.info(f"[FILES] Proxying upload to storage server: {url}")
            # Proxy to storage server (pass content directly to avoid re-reading)
            result = await _proxy_upload_file(url, current_user.username, filename, content, content_type, path, db)
            logger.info(f"[FILES] Successfully proxied upload to storage server")
            return result
        else:
            raise HTTPException(status_code=500, detail="Invalid storage_server_url configuration")
    else:
        raise HTTPException(status_code=500, detail="Storage server not configured. Cannot upload files.")


async def _proxy_upload_file(storage_server_url: str, username: str, filename: str, content: bytes, content_type: str, path: str, db: Session):
    """Proxy file upload to storage server - uses synchronous requests to avoid event loop issues"""
    from app.models import Setting
    
    try:
        # Get server-to-server API token
        storage_server_token = safe_query_setting(db, "storage_server_token")
        
        url = f"{storage_server_url.rstrip('/')}/api/storage/upload-file"
        headers = {}
        if storage_server_token and storage_server_token.value:
            headers["Authorization"] = f"Bearer {storage_server_token.value}"
        
        files = {
            "file": (filename, content, content_type)
        }
        data = {
            "username": username,
            "path": path
        }
        
        # Use synchronous requests in thread pool (same approach as note attachments)
        # This avoids httpx connection issues
        def _sync_proxy():
            import requests
            response = requests.post(url, headers=headers, files=files, data=data, timeout=300)
            if response.status_code == 200:
                return response.json()
            else:
                logger.error(f"[FILES] Failed to proxy upload_file: {response.status_code} - {response.text}")
                raise Exception(f"Storage server error: {response.status_code}")
        
        return await asyncio.to_thread(_sync_proxy)
    except Exception as e:
        logger.error(f"[FILES] Error proxying upload_file: {e}", exc_info=True)
        raise


async def _proxy_list_files(storage_server_url: str, username: str, path: str, db: Session):
    """Proxy file listing to storage server"""
    from app.models import Setting
    import requests
    
    try:
        # Get server-to-server API token
        storage_server_token = safe_query_setting(db, "storage_server_token")
        
        url = f"{storage_server_url.rstrip('/')}/api/storage/list-files"
        headers = {}
        if storage_server_token and storage_server_token.value:
            headers["Authorization"] = f"Bearer {storage_server_token.value}"
        
        params = {
            "username": username,
            "path": path
        }
        
        # Use synchronous requests in thread pool
        def _sync_proxy():
            response = requests.get(url, headers=headers, params=params, timeout=30)
            if response.status_code == 200:
                return response.json()
            else:
                logger.error(f"[FILES] Failed to proxy list_files: {response.status_code} - {response.text}")
                raise Exception(f"Storage server error: {response.status_code}")
        
        return await asyncio.to_thread(_sync_proxy)
    except Exception as e:
        logger.error(f"[FILES] Error proxying list_files: {e}", exc_info=True)
        raise


async def _proxy_delete_file(storage_server_url: str, username: str, file_path: str, db: Session):
    """Proxy file deletion to storage server - uses synchronous requests to avoid event loop issues"""
    from app.models import Setting
    import requests
    
    try:
        # Get server-to-server API token
        storage_server_token = safe_query_setting(db, "storage_server_token")
        
        url = f"{storage_server_url.rstrip('/')}/api/storage/delete-file"
        headers = {}
        if storage_server_token and storage_server_token.value:
            headers["Authorization"] = f"Bearer {storage_server_token.value}"
        
        params = {
            "username": username,
            "file_path": file_path
        }
        
        # Use synchronous requests in thread pool
        def _sync_proxy():
            response = requests.delete(url, headers=headers, params=params, timeout=30)
            if response.status_code == 200:
                return response.json()
            else:
                logger.error(f"[FILES] Failed to proxy delete_file: {response.status_code} - {response.text}")
                raise Exception(f"Storage server error: {response.status_code}")
        
        return await asyncio.to_thread(_sync_proxy)
    except Exception as e:
        logger.error(f"[FILES] Error proxying delete_file: {e}", exc_info=True)
        raise


async def _proxy_mkdir(storage_server_url: str, username: str, path: str, db: Session):
    """Proxy directory creation to storage server - uses synchronous requests to avoid event loop issues"""
    from app.models import Setting
    import requests
    
    try:
        # Get server-to-server API token
        storage_server_token = safe_query_setting(db, "storage_server_token")
        
        url = f"{storage_server_url.rstrip('/')}/api/storage/mkdir"
        headers = {}
        if storage_server_token and storage_server_token.value:
            headers["Authorization"] = f"Bearer {storage_server_token.value}"
        
        data = {
            "username": username,
            "path": path
        }
        
        # Use synchronous requests in thread pool
        def _sync_proxy():
            response = requests.post(url, headers=headers, data=data, timeout=30)
            if response.status_code == 200:
                return response.json()
            else:
                logger.error(f"[FILES] Failed to proxy mkdir: {response.status_code} - {response.text}")
                raise Exception(f"Storage server error: {response.status_code}")
        
        return await asyncio.to_thread(_sync_proxy)
    except Exception as e:
        logger.error(f"[FILES] Error proxying mkdir: {e}", exc_info=True)
        raise


async def _proxy_view_file(storage_server_url: str, username: str, file_path: str, db: Session):
    """Proxy file view/download to storage server - uses synchronous requests to avoid event loop issues"""
    from app.models import Setting
    import requests
    from fastapi.responses import Response
    
    try:
        # Get server-to-server API token
        storage_server_token = safe_query_setting(db, "storage_server_token")
        
        url = f"{storage_server_url.rstrip('/')}/api/storage/view-file"
        headers = {}
        if storage_server_token and storage_server_token.value:
            headers["Authorization"] = f"Bearer {storage_server_token.value}"
        
        params = {
            "username": username,
            "file_path": file_path
        }
        
        # Use synchronous requests in thread pool
        def _sync_proxy():
            response = requests.get(url, headers=headers, params=params, timeout=300, stream=True)
            if response.status_code == 200:
                # Read the content
                content = response.content
                # Get content type and headers
                content_type = response.headers.get('Content-Type', 'application/octet-stream')
                response_headers = {}
                # Copy relevant headers
                for key, value in response.headers.items():
                    if key.lower() in ['content-disposition', 'content-type']:
                        response_headers[key] = value
                return {
                    "content": content,
                    "media_type": content_type,
                    "headers": response_headers
                }
            else:
                logger.error(f"[FILES] Failed to proxy view_file: {response.status_code} - {response.text}")
                raise Exception(f"Storage server error: {response.status_code}")
        
        result = await asyncio.to_thread(_sync_proxy)
        # Return Response with the content
        return Response(
            content=result["content"],
            media_type=result["media_type"],
            headers=result["headers"]
        )
    except Exception as e:
        logger.error(f"[FILES] Error proxying view_file: {e}", exc_info=True)
        raise


async def _proxy_get_thumbnail(storage_server_url: str, username: str, file_path: str, size: int, db: Session):
    """Proxy thumbnail generation to storage server - uses synchronous requests to avoid event loop issues"""
    from app.models import Setting
    import requests
    
    try:
        # Get server-to-server API token
        storage_server_token = safe_query_setting(db, "storage_server_token")
        
        url = f"{storage_server_url.rstrip('/')}/api/storage/thumbnail-file"
        headers = {}
        if storage_server_token and storage_server_token.value:
            headers["Authorization"] = f"Bearer {storage_server_token.value}"
        
        params = {
            "username": username,
            "file_path": file_path,
            "size": size
        }
        
        # Use synchronous requests in thread pool
        def _sync_proxy():
            try:
                from requests.exceptions import RequestException, Timeout, ConnectionError as RequestsConnectionError
                response = requests.get(url, headers=headers, params=params, timeout=60)
                if response.status_code == 200:
                    return response.json()
                else:
                    logger.error(f"[FILES] Failed to proxy get_thumbnail: {response.status_code} - {response.text[:500]}")
                    raise HTTPException(
                        status_code=response.status_code,
                        detail=f"Storage server error: {response.status_code}"
                    )
            except Timeout:
                logger.error(f"[FILES] Timeout proxying get_thumbnail to {url}")
                raise HTTPException(
                    status_code=504,
                    detail="Storage server timeout while generating thumbnail"
                )
            except RequestsConnectionError as e:
                logger.error(f"[FILES] Connection error proxying get_thumbnail to {url}: {e}")
                raise HTTPException(
                    status_code=503,
                    detail=f"Storage server connection error: {str(e)}"
                )
            except RequestException as e:
                logger.error(f"[FILES] Request error proxying get_thumbnail to {url}: {e}")
                raise HTTPException(
                    status_code=502,
                    detail=f"Storage server request error: {str(e)}"
                )
        
        return await asyncio.to_thread(_sync_proxy)
    except HTTPException:
        # Re-raise HTTP exceptions
        raise
    except Exception as e:
        logger.error(f"[FILES] Error proxying get_thumbnail: {e}", exc_info=True)
        raise HTTPException(
            status_code=500,
            detail=f"Error generating thumbnail: {str(e)}"
        )


async def _proxy_move_files(storage_server_url: str, username: str, file_paths: List[str], destination: str, db: Session):
    """Proxy file move operation to storage server - uses synchronous requests to avoid event loop issues"""
    from app.models import Setting
    import requests
    
    try:
        # Get server-to-server API token
        storage_server_token = safe_query_setting(db, "storage_server_token")
        
        url = f"{storage_server_url.rstrip('/')}/api/storage/move-files"
        headers = {}
        if storage_server_token and storage_server_token.value:
            headers["Authorization"] = f"Bearer {storage_server_token.value}"
        
        data = {
            "username": username,
            "file_paths": file_paths,
            "destination": destination
        }
        
        # Use synchronous requests in thread pool
        def _sync_proxy():
            response = requests.post(url, headers=headers, json=data, timeout=300)
            if response.status_code == 200:
                return response.json()
            else:
                logger.error(f"[FILES] Failed to proxy move_files: {response.status_code} - {response.text}")
                raise Exception(f"Storage server error: {response.status_code}")
        
        return await asyncio.to_thread(_sync_proxy)
    except Exception as e:
        logger.error(f"[FILES] Error proxying move_files: {e}", exc_info=True)
        raise


async def _proxy_delete_files_bulk(storage_server_url: str, username: str, file_paths: List[str], db: Session):
    """Proxy bulk file deletion to storage server - uses synchronous requests to avoid event loop issues"""
    from app.models import Setting
    import requests
    
    try:
        # Get server-to-server API token
        storage_server_token = safe_query_setting(db, "storage_server_token")
        
        url = f"{storage_server_url.rstrip('/')}/api/storage/delete-files-bulk"
        headers = {}
        if storage_server_token and storage_server_token.value:
            headers["Authorization"] = f"Bearer {storage_server_token.value}"
        
        data = {
            "username": username,
            "file_paths": file_paths
        }
        
        # Use synchronous requests in thread pool
        def _sync_proxy():
            response = requests.post(url, headers=headers, json=data, timeout=300)
            if response.status_code == 200:
                return response.json()
            else:
                logger.error(f"[FILES] Failed to proxy delete_files_bulk: {response.status_code} - {response.text}")
                raise Exception(f"Storage server error: {response.status_code}")
        
        return await asyncio.to_thread(_sync_proxy)
    except Exception as e:
        logger.error(f"[FILES] Error proxying delete_files_bulk: {e}", exc_info=True)
        raise


@router.post("/mkdir")
async def create_directory(
    path: str = Form(..., description="Directory path relative to user root"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Create a new directory. Proxies to storage server if configured (NO FALLBACK)."""
    # Check if storage server is configured - proxy request if so (NO FALLBACK)
    storage_server_url = safe_query_setting(db, "storage_server_url")
    if storage_server_url and storage_server_url.value:
        url = storage_server_url.value.strip()
        if url.startswith(('http://', 'https://')):
            logger.info(f"[FILES] Proxying mkdir to storage server: {url}")
            # Proxy to storage server - NO FALLBACK
            result = await _proxy_mkdir(url, current_user.username, path, db)
            logger.info(f"[FILES] Successfully proxied mkdir to storage server")
            return result
        else:
            raise HTTPException(status_code=500, detail="Invalid storage_server_url configuration")
    else:
        raise HTTPException(status_code=500, detail="Storage server not configured. Cannot create directory.")
