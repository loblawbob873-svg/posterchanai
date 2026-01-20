#!/usr/bin/env python3
"""
PosterchanAI WebDAV Mount Daemon
Pure Python implementation - no FUSE required!
Creates a local directory that stays in sync with WebDAV storage
"""
import os
import sys
import json
import logging
import subprocess
import time
import signal
import threading
import socket
import urllib.parse
import shutil
from pathlib import Path
from typing import Optional, Tuple, Dict, List
from datetime import datetime, timedelta
import errno
import stat
import hashlib

# WebDAV client using requests
import requests
from requests.auth import HTTPBasicAuth
from xml.etree import ElementTree as ET

class WebDAVError(Exception):
    pass

# Setup logging
log_dir = Path.home() / ".config" / "posterchanai-sync" / "logs"
log_dir.mkdir(parents=True, exist_ok=True)
log_file = log_dir / f"webdav_mount_{datetime.now().strftime('%Y%m%d')}.log"

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.FileHandler(log_file),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

CONFIG_DIR = Path.home() / ".config" / "posterchanai-sync"
CONFIG_FILE = CONFIG_DIR / "config.json"
CACHE_DIR = CONFIG_DIR / "cache"
CACHE_METADATA = CACHE_DIR / "metadata.json"

# Global state
_running = False
_last_suspend_check = time.time()
_suspend_detected = False
_online = True


class WebDAVClient:
    """Simple WebDAV client using requests"""
    
    def __init__(self, base_url: str, username: str, password: str):
        self.base_url = base_url.rstrip('/')
        self.auth = HTTPBasicAuth(username, password)
        self.session = requests.Session()
        self.session.auth = self.auth
    
    def _url(self, path: str) -> str:
        """Build full URL from path"""
        path = path.lstrip('/')
        url = f"{self.base_url}/{path}" if path else self.base_url
        # For directories, ensure URL ends with / for proper PROPFIND handling
        # But don't add / if it's clearly a file (has extension and no trailing slash)
        if not url.endswith('/'):
            # Check if it looks like a file (has extension)
            path_parts = path.split('/')
            if path_parts and '.' in path_parts[-1] and path_parts[-1].split('.')[-1] in ['log', 'db', 'ics', 'csv', 'jpeg', 'xbel', 'shm', 'wal']:
                pass  # It's a file, don't add /
            # For directories, we'll let the server handle it
        return url
    
    def info(self, path: str) -> dict:
        """Get file/directory info via PROPFIND"""
        # Try with and without trailing slash for directories
        urls_to_try = [self._url(path)]
        if not path.endswith('/'):
            urls_to_try.append(self._url(path + '/'))
        
        for url in urls_to_try:
            try:
                response = self.session.request('PROPFIND', url, headers={'Depth': '0'}, timeout=10)
                response.raise_for_status()
                
                # Parse XML response
                root = ET.fromstring(response.content)
                ns = {'D': 'DAV:'}
                
                # Find the response element that matches our requested path
                # The server might return child items, so we need to find the right one
                requested_path = path.rstrip('/').lstrip('/')
                response_elem = None
                for elem in root.findall('.//D:response', ns):
                    href = elem.find('D:href', ns)
                    if href is not None:
                        href_path = href.text.rstrip('/').lstrip('/')
                        # Check if this response matches our requested path
                        if href_path == requested_path or href_path.endswith('/' + requested_path):
                            response_elem = elem
                            break
                
                # If no matching response found, check if any response indicates the parent is a directory
                # (The server might return a child item, but if it has collection tag, the parent is a directory)
                parent_is_dir = False
                if response_elem is None:
                    logger.debug(f"[WebDAV Client] No direct match for {requested_path}, checking for child items...")
                    for elem in root.findall('.//D:response', ns):
                        href = elem.find('D:href', ns)
                        if href is not None:
                            href_path = href.text.rstrip('/').lstrip('/')
                            # Check if this response is a child of the requested path
                            # href_path might be like "verita84@poster.place/chat/chat/1"
                            # requested_path is "verita84@poster.place/chat"
                            # So we check if href_path starts with requested_path + "/"
                            if href_path.startswith(requested_path + '/'):
                                logger.debug(f"[WebDAV Client] Found child item: {href_path}, checking if it's a directory...")
                                # Check if this item is a directory (collection)
                                propstat = elem.find('D:propstat', ns)
                                if propstat is not None:
                                    prop = propstat.find('D:prop', ns)
                                    if prop is not None:
                                        resourcetype = prop.find('D:resourcetype', ns)
                                        if resourcetype is not None:
                                            # Check for collection child element first (proper XML structure)
                                            collection = resourcetype.find('D:collection', ns)
                                            if collection is not None:
                                                parent_is_dir = True
                                                response_elem = elem
                                                logger.info(f"[WebDAV Client] ✓ Detected parent directory: child {href_path} has collection element")
                                                break
                                            
                                            # Check resourcetype text (may be HTML-encoded like &lt;D:collection/&gt;)
                                            resourcetype_text = resourcetype.text
                                            if resourcetype_text:
                                                import html
                                                decoded = html.unescape(resourcetype_text.strip())
                                                if 'collection' in decoded.lower() or decoded.strip() == '<D:collection/>':
                                                    parent_is_dir = True
                                                    response_elem = elem
                                                    logger.info(f"[WebDAV Client] ✓ Detected parent directory: child {href_path} has collection in resourcetype text: {decoded}")
                                                    break
                                            
                                            # Also check if resourcetype has any child elements (collection tag as child)
                                            children = list(resourcetype)
                                            if len(children) > 0:
                                                # Check if any child is a collection tag
                                                for child in children:
                                                    if 'collection' in child.tag.lower() or child.tag.endswith('collection'):
                                                        parent_is_dir = True
                                                        response_elem = elem
                                                        logger.info(f"[WebDAV Client] ✓ Detected parent directory: child {href_path} has collection child element: {child.tag}")
                                                        break
                                                if parent_is_dir:
                                                    break
                                            
                                            # Final check: serialize the resourcetype element and check for collection
                                            # The server returns HTML-encoded XML like &lt;D:collection/&gt; in the text
                                            # When parsed, this becomes text content, so we need to check the serialized XML
                                            if not parent_is_dir:
                                                try:
                                                    full_xml = ET.tostring(resourcetype, encoding='unicode')
                                                    # Check if collection appears anywhere in the XML (text, attributes, children)
                                                    if 'collection' in full_xml.lower():
                                                        parent_is_dir = True
                                                        response_elem = elem
                                                        logger.info(f"[WebDAV Client] ✓ Detected parent directory: child {href_path} has collection in resourcetype XML")
                                                        break
                                                except Exception as e:
                                                    logger.debug(f"[WebDAV Client] Error serializing resourcetype: {e}")
                                                    pass
                
                # If still no match, use the first response
                if response_elem is None:
                    response_elem = root.find('.//D:response', ns)
                
                if response_elem is None:
                    continue  # Try next URL
                
                # Get href (path)
                href = response_elem.find('D:href', ns)
                if href is None:
                    continue  # Try next URL
                
                # Get properties
                propstat = response_elem.find('D:propstat', ns)
                if propstat is None:
                    continue  # Try next URL
                
                prop = propstat.find('D:prop', ns)
                if prop is None:
                    continue  # Try next URL
                
                # Extract info
                resourcetype = prop.find('D:resourcetype', ns)
                # Start with parent_is_dir (set above if child is a directory)
                # If parent_is_dir is True, the requested path is a directory regardless of resourcetype
                isdir = parent_is_dir
                if parent_is_dir:
                    logger.info(f"[WebDAV Client] Detected directory via parent_is_dir flag for {path} (child is a directory)")
                    # If we detected via parent_is_dir, we can return early - no need to check resourcetype
                    # But we still need to get size and modified time, so continue
                
                # Only check resourcetype if parent_is_dir is False
                if not isdir and resourcetype is not None:
                    # Check for collection child element (proper XML structure)
                    collection = resourcetype.find('D:collection', ns)
                    if collection is not None:
                        isdir = True
                    else:
                        # Check if resourcetype text contains collection (may be HTML-encoded XML like &lt;D:collection/&gt;)
                        resourcetype_text = resourcetype.text or ''
                        if resourcetype_text:
                            # Decode HTML entities (e.g., &lt; becomes <)
                            import html
                            decoded_text = html.unescape(resourcetype_text.strip())
                            # Check if decoded text contains collection tag or is exactly <D:collection/>
                            if 'collection' in decoded_text.lower() or decoded_text.strip() == '<D:collection/>':
                                isdir = True
                                logger.debug(f"Detected directory via resourcetype text: {decoded_text}")
                        # Also check if there are any child elements (collection tag)
                        if len(list(resourcetype)) > 0:
                            isdir = True
                
                # Final fallback: if we can list the path and get results, it's definitely a directory
                # This is the most reliable method when PROPFIND doesn't properly identify directories
                # NOTE: This is slow (makes an extra PROPFIND request), so we try to avoid it
                # by using isdir from ls() listing when available
                if not isdir:
                    try:
                        # Try to list the path - if it succeeds and returns items, it's a directory
                        test_list = self.ls(path, depth=1, retry_attempts=1, retry_delay=1)
                        # If listing returns items, it's a directory
                        if test_list and len(test_list) > 0:
                            isdir = True
                            logger.debug(f"[WebDAV Client] Detected directory via listing fallback for {path} ({len(test_list)} items found)")
                    except Exception as e:
                        # If listing fails, it might be a file or the path doesn't exist
                        logger.debug(f"[WebDAV Client] Listing fallback failed for {path}: {e}")
                        pass
                
                contentlength = prop.find('D:getcontentlength', ns)
                size = int(contentlength.text) if contentlength is not None and contentlength.text else 0
                
                getlastmodified = prop.find('D:getlastmodified', ns)
                mtime = time.time()
                if getlastmodified is not None and getlastmodified.text:
                    from email.utils import parsedate_to_datetime
                    try:
                        mtime = parsedate_to_datetime(getlastmodified.text).timestamp()
                    except:
                        pass
                
                return {
                    'isdir': isdir,
                    'size': size,
                    'modified': mtime
                }
            except Exception as e:
                logger.debug(f"PROPFIND failed for {url}: {e}")
                continue  # Try next URL
        
        # If all URLs failed, return None
        logger.debug(f"PROPFIND failed for all URLs for {path}")
        return None
    
    def ls(self, path: str, depth: int = 1, retry_attempts: int = 3, retry_delay: int = 2) -> list:
        """List directory contents with retry logic for network disconnects
        
        Args:
            path: Remote path to list
            depth: Depth of listing (0=self, 1=children, infinity=all descendants)
            retry_attempts: Number of retry attempts on network failure
            retry_delay: Initial delay between retries (seconds)
        """
        url = self._url(path)
        if not url.endswith('/'):
            url += '/'
        
        last_error = None
        for attempt in range(retry_attempts):
            try:
                # Use Depth header - '1' for immediate children, 'infinity' for all descendants
                depth_header = 'infinity' if depth > 1 else str(depth)
                response = self.session.request('PROPFIND', url, headers={'Depth': depth_header}, timeout=30)
                response.raise_for_status()
                
                # Parse XML
                root = ET.fromstring(response.content)
                ns = {'D': 'DAV:'}
                
                files = []
                seen_paths = set()  # Avoid duplicates
                
                for response_elem in root.findall('.//D:response', ns):
                    href = response_elem.find('D:href', ns)
                    if href is None:
                        continue
                    
                    # Keep original href for directory detection (don't strip / yet)
                    href_text = href.text
                    # URL-decode the path (server returns URL-encoded paths like verita84%40poster.place)
                    from urllib.parse import unquote
                    file_path = unquote(href_text).rstrip('/')
                    
                    # Remove base URL to get relative path
                    if file_path.startswith(self.base_url):
                        file_path = file_path[len(self.base_url):].lstrip('/')
                    elif file_path.startswith('/'):
                        # Absolute path - remove leading slash
                        file_path = file_path.lstrip('/')
                    
                    # Skip the directory itself
                    normalized_path = path.rstrip('/').lstrip('/')
                    # Also URL-decode normalized_path for comparison
                    normalized_path = unquote(normalized_path) if normalized_path else normalized_path
                    if file_path == normalized_path or file_path == f'/{normalized_path}':
                        continue
                    
                    # Avoid duplicates
                    if file_path in seen_paths:
                        continue
                    seen_paths.add(file_path)
                    
                    # Get properties to determine if it's a directory
                    propstat = response_elem.find('D:propstat', ns)
                    isdir = False
                    
                    # First check: if original href ends with / (and it's not the parent directory), it's a directory
                    if href_text.endswith('/') and file_path != normalized_path:
                        isdir = True
                    
                    # Second check: look for resourcetype/collection in properties
                    if not isdir and propstat is not None:
                        prop = propstat.find('D:prop', ns)
                        if prop is not None:
                            resourcetype = prop.find('D:resourcetype', ns)
                            if resourcetype is not None:
                                collection = resourcetype.find('D:collection', ns)
                                if collection is not None:
                                    isdir = True
                            
                            # Third check: if there's no contentlength AND no resourcetype, 
                            # we need to use info() to check (but that's expensive, so we'll do it in sync_from_remote)
                            # For now, if href doesn't end with / and no collection tag, assume it's a file
                    
                    # Extract mtime from getlastmodified if available (for faster sync without info() calls)
                    mtime = None
                    if propstat is not None:
                        prop = propstat.find('D:prop', ns)
                        if prop is not None:
                            getlastmodified = prop.find('D:getlastmodified', ns)
                            if getlastmodified is not None and getlastmodified.text:
                                from email.utils import parsedate_to_datetime
                                try:
                                    mtime = parsedate_to_datetime(getlastmodified.text).timestamp()
                                except:
                                    pass
                    
                    # Get size from getcontentlength if available
                    size = -1
                    if propstat is not None:
                        prop = propstat.find('D:prop', ns)
                        if prop is not None:
                            getcontentlength = prop.find('D:getcontentlength', ns)
                            if getcontentlength is not None and getcontentlength.text:
                                try:
                                    size = int(getcontentlength.text)
                                except:
                                    pass
                    
                    # Get just the filename for 'name', but keep full path for 'path'
                    filename = file_path.split('/')[-1]
                    if filename:
                        file_data = {'name': filename, 'path': file_path, 'isdir': isdir}
                        if mtime is not None:
                            file_data['modified'] = mtime
                        if size >= 0:
                            file_data['size'] = size
                        files.append(file_data)
                
                return files
            except (requests.exceptions.ConnectionError, requests.exceptions.Timeout,
                    requests.exceptions.RequestException) as e:
                last_error = e
                if attempt < retry_attempts - 1:
                    logger.warning(f"List failed (attempt {attempt + 1}/{retry_attempts}): {e}, retrying in {retry_delay}s...")
                    time.sleep(retry_delay)
                    retry_delay *= 2  # Exponential backoff
                else:
                    logger.error(f"List failed after {retry_attempts} attempts: {e}")
            except Exception as e:
                # Non-network errors, don't retry
                raise WebDAVError(f"Failed to list directory: {e}")
        
        raise WebDAVError(f"Failed to list directory after {retry_attempts} attempts: {last_error}")
    
    def download(self, path: str, retry_attempts: int = 3, retry_delay: int = 2) -> bytes:
        """Download file content with retry logic for network disconnects"""
        url = self._url(path)
        last_error = None
        
        for attempt in range(retry_attempts):
            try:
                response = self.session.get(url, timeout=30)
                response.raise_for_status()
                return response.content
            except (requests.exceptions.ConnectionError, requests.exceptions.Timeout, 
                    requests.exceptions.RequestException) as e:
                last_error = e
                if attempt < retry_attempts - 1:
                    logger.warning(f"Download failed (attempt {attempt + 1}/{retry_attempts}): {e}, retrying in {retry_delay}s...")
                    time.sleep(retry_delay)
                    retry_delay *= 2  # Exponential backoff
                else:
                    logger.error(f"Download failed after {retry_attempts} attempts: {e}")
            except Exception as e:
                # Non-network errors, don't retry
                raise WebDAVError(f"Failed to download: {e}")
        
        raise WebDAVError(f"Failed to download after {retry_attempts} attempts: {last_error}")
    
    def upload(self, path: str, content: bytes):
        """Upload file content"""
        url = self._url(path)
        try:
            response = self.session.put(url, data=content, timeout=30)
            response.raise_for_status()
        except Exception as e:
            raise WebDAVError(f"Failed to upload: {e}")
    
    def mkdir(self, path: str):
        """Create directory"""
        url = self._url(path)
        if not url.endswith('/'):
            url += '/'
        try:
            response = self.session.request('MKCOL', url, timeout=10)
            response.raise_for_status()
        except Exception as e:
            raise WebDAVError(f"Failed to create directory: {e}")
    
    def delete(self, path: str):
        """Delete file or directory"""
        url = self._url(path)
        try:
            response = self.session.delete(url, timeout=10)
            response.raise_for_status()
        except Exception as e:
            raise WebDAVError(f"Failed to delete: {e}")
    
    def mv(self, old_path: str, new_path: str):
        """Move/rename file or directory"""
        old_url = self._url(old_path)
        new_url = self._url(new_path)
        try:
            response = self.session.request('MOVE', old_url, headers={'Destination': new_url}, timeout=10)
            response.raise_for_status()
        except Exception as e:
            raise WebDAVError(f"Failed to move: {e}")


class CacheManager:
    """Manages offline cache for WebDAV files with size limits and directory-level caching"""
    
    def __init__(self, cache_dir: Path, max_size_mb: int = 10240, max_age_days: int = 30, cache_directories: list = None):
        self.cache_dir = cache_dir
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.metadata_file = CACHE_METADATA
        self.sync_state_file = self.cache_dir / 'sync_state.json'  # Track files from last sync
        self.metadata = self._load_metadata()
        self.sync_state = {}  # {remote_path: {'mtime': float, 'size': int, 'hash': str, 'local_path': str}}
        self._lock = threading.Lock()
        self.max_size_bytes = max_size_mb * 1024 * 1024  # Convert MB to bytes
        self.max_age_seconds = max_age_days * 24 * 60 * 60  # Convert days to seconds
        self.cache_directories = cache_directories or []  # Directories to always cache
        self._load_sync_state()
        logger.info(f"CacheManager initialized: max_size={max_size_mb}MB, max_age={max_age_days}days, cache_dirs={self.cache_directories}")
    
    def _load_metadata(self) -> dict:
        """Load cache metadata"""
        if self.metadata_file.exists():
            try:
                with open(self.metadata_file, 'r') as f:
                    return json.load(f)
            except:
                return {}
        return {}
    
    def _save_metadata(self):
        """Save cache metadata"""
        try:
            with open(self.metadata_file, 'w') as f:
                json.dump(self.metadata, f, indent=2)
        except Exception as e:
            logger.warning(f"Failed to save cache metadata: {e}")
    
    def get_cache_path(self, remote_path: str) -> Path:
        """Get local cache path for a remote file"""
        safe_path = remote_path.replace('/', '_').replace('\\', '_')
        if len(safe_path) > 200:
            hash_suffix = hashlib.md5(remote_path.encode()).hexdigest()[:8]
            safe_path = safe_path[:192] + hash_suffix
        return self.cache_dir / safe_path
    
    def is_cached(self, remote_path: str) -> bool:
        """Check if file is cached"""
        cache_path = self.get_cache_path(remote_path)
        return cache_path.exists() and remote_path in self.metadata
    
    def get_cached_content(self, remote_path: str) -> Optional[bytes]:
        """Get cached file content"""
        if not self.is_cached(remote_path):
            return None
        
        cache_path = self.get_cache_path(remote_path)
        try:
            with open(cache_path, 'rb') as f:
                return f.read()
        except Exception as e:
            logger.warning(f"Error reading cache for {remote_path}: {e}")
            return None
    
    def cache_file(self, remote_path: str, content: bytes, mtime: float = None, force: bool = False):
        """Cache a file locally with size management
        
        Args:
            remote_path: Remote path of the file
            content: File content to cache
            mtime: Modification time
            force: If True, cache even if directory is not in cache_directories
        """
        # Check if this directory should be cached
        if not force and self.cache_directories:
            path_parts = remote_path.strip('/').split('/')
            if len(path_parts) > 1:  # Has directory component
                dir_name = path_parts[0]  # First directory after username
                if dir_name not in self.cache_directories:
                    logger.debug(f"Skipping cache for {remote_path} (directory {dir_name} not in cache_directories)")
                    return
        
        with self._lock:
            # Check cache size and evict old files if needed
            self._enforce_cache_limits(len(content))
            
            cache_path = self.get_cache_path(remote_path)
            try:
                cache_path.parent.mkdir(parents=True, exist_ok=True)
                with open(cache_path, 'wb') as f:
                    f.write(content)
                
                self.metadata[remote_path] = {
                    'mtime': mtime or time.time(),
                    'size': len(content),
                    'cached_at': time.time()
                }
                self._save_metadata()
                logger.debug(f"Cached {remote_path} ({len(content)} bytes)")
            except Exception as e:
                logger.warning(f"Error caching file {remote_path}: {e}")
    
    def _enforce_cache_limits(self, new_file_size: int):
        """Enforce cache size limits by evicting old files"""
        if self.max_size_bytes <= 0:
            return  # No size limit
        
        # Calculate current cache size
        current_size = sum(meta.get('size', 0) for meta in self.metadata.values())
        
        # If adding this file would exceed limit, evict old files
        if current_size + new_file_size > self.max_size_bytes:
            logger.info(f"Cache size limit reached ({current_size / (1024*1024):.1f}MB), evicting old files...")
            
            # Sort files by access time (oldest first)
            files_by_age = []
            for remote_path, meta in self.metadata.items():
                # Prioritize files in cache_directories (don't evict them first)
                is_priority = False
                if self.cache_directories:
                    path_parts = remote_path.strip('/').split('/')
                    if len(path_parts) > 1:
                        dir_name = path_parts[0]
                        is_priority = dir_name in self.cache_directories
                
                last_access = meta.get('cached_at', 0)
                files_by_age.append((last_access, is_priority, remote_path, meta.get('size', 0)))
            
            # Sort: non-priority first, then by age (oldest first)
            files_by_age.sort(key=lambda x: (x[1], x[0]))  # False (non-priority) sorts before True
            
            # Evict files until we have enough space
            evicted_size = 0
            for last_access, is_priority, remote_path, file_size in files_by_age:
                if current_size + new_file_size - evicted_size <= self.max_size_bytes:
                    break
                
                # Evict this file
                cache_path = self.get_cache_path(remote_path)
                try:
                    if cache_path.exists():
                        cache_path.unlink()
                    del self.metadata[remote_path]
                    evicted_size += file_size
                    logger.debug(f"Evicted {remote_path} from cache ({file_size} bytes)")
                except Exception as e:
                    logger.warning(f"Error evicting {remote_path}: {e}")
            
            if evicted_size > 0:
                self._save_metadata()
                logger.info(f"Evicted {evicted_size / (1024*1024):.1f}MB from cache")
    
    def cleanup_old_cache(self):
        """Remove files older than max_age_days"""
        if self.max_age_seconds <= 0:
            return  # No age limit
        
        current_time = time.time()
        cutoff_time = current_time - self.max_age_seconds
        
        with self._lock:
            to_remove = []
            for remote_path, meta in self.metadata.items():
                cached_at = meta.get('cached_at', 0)
                if cached_at < cutoff_time:
                    to_remove.append(remote_path)
            
            for remote_path in to_remove:
                cache_path = self.get_cache_path(remote_path)
                try:
                    if cache_path.exists():
                        cache_path.unlink()
                    del self.metadata[remote_path]
                except Exception as e:
                    logger.warning(f"Error removing old cache {remote_path}: {e}")
            
            if to_remove:
                self._save_metadata()
                logger.info(f"Cleaned up {len(to_remove)} old cache files")
    
    def get_cache_size(self) -> int:
        """Get total cache size in bytes"""
        return sum(meta.get('size', 0) for meta in self.metadata.values())
    
    def should_cache_directory(self, remote_path: str) -> bool:
        """Check if a directory should be cached based on cache_directories config"""
        if not self.cache_directories:
            return True  # Cache everything if no specific directories configured
        
        path_parts = remote_path.strip('/').split('/')
        if path_parts:
            dir_name = path_parts[0]
            return dir_name in self.cache_directories
        return False
    
    def mark_dirty(self, remote_path: str):
        """Mark file as modified (needs sync)"""
        if remote_path in self.metadata:
            self.metadata[remote_path]['dirty'] = True
            self.metadata[remote_path]['modified_at'] = time.time()
            self._save_metadata()
    
    def mark_clean(self, remote_path: str):
        """Mark file as synced"""
        if remote_path in self.metadata:
            self.metadata[remote_path]['dirty'] = False
            if 'modified_at' in self.metadata[remote_path]:
                del self.metadata[remote_path]['modified_at']
            self._save_metadata()
    
    def get_pending_changes(self) -> dict:
        """Get files that have been modified locally but not synced"""
        pending = {}
        for remote_path, meta in self.metadata.items():
            if meta.get('dirty', False):
                pending[remote_path] = meta
        return pending
    
    def _load_sync_state(self):
        """Load sync state from disk"""
        if self.sync_state_file.exists():
            try:
                with open(self.sync_state_file, 'r') as f:
                    self.sync_state = json.load(f)
            except Exception as e:
                logger.warning(f"Failed to load sync state: {e}")
                self.sync_state = {}
        else:
            self.sync_state = {}
    
    def _save_sync_state(self):
        """Save sync state to disk"""
        try:
            with open(self.sync_state_file, 'w') as f:
                json.dump(self.sync_state, f, indent=2)
        except Exception as e:
            logger.warning(f"Failed to save sync state: {e}")
    
    def update_sync_state(self, remote_path: str, local_path: str, mtime: float, size: int, content_hash: str = None):
        """Update sync state for a file"""
        with self._lock:
            self.sync_state[remote_path] = {
                'mtime': mtime,
                'size': size,
                'hash': content_hash,
                'local_path': str(local_path)
            }
            self._save_sync_state()
    
    def remove_from_sync_state(self, remote_path: str):
        """Remove file from sync state (was deleted)"""
        with self._lock:
            if remote_path in self.sync_state:
                del self.sync_state[remote_path]
                self._save_sync_state()
    
    def get_sync_state(self, remote_path: str) -> dict:
        """Get sync state for a file"""
        return self.sync_state.get(remote_path, {})
    
    def get_all_sync_state_paths(self) -> set:
        """Get all remote paths in sync state"""
        return set(self.sync_state.keys())


class WebDAVSync:
    """Pure Python WebDAV sync - no FUSE required!"""
    
    def __init__(self, webdav_client: WebDAVClient, local_dir: Path, remote_base: str, cache: CacheManager = None, network_retry_attempts: int = 5, network_retry_delay: int = 5, conflict_resolution: str = 'last_write_wins'):
        self.webdav = webdav_client
        self.local_dir = Path(local_dir)
        self.remote_base = remote_base.rstrip('/')
        self.cache = cache
        self._lock = threading.Lock()
        self._sync_in_progress = False
        self.network_retry_attempts = network_retry_attempts
        self.network_retry_delay = network_retry_delay
        self.conflict_resolution = conflict_resolution  # Options: 'last_write_wins', 'manual', 'local_wins', 'remote_wins'
    
    def _remote_path(self, local_path: Path) -> str:
        """Convert local path to remote path"""
        rel_path = local_path.relative_to(self.local_dir)
        return f"{self.remote_base}/{rel_path}" if str(rel_path) != '.' else self.remote_base
    
    def _local_path(self, remote_path: str) -> Path:
        """Convert remote path to local path"""
        # Remove leading slash and remote_base prefix if present
        rel_path = remote_path.lstrip('/').rstrip('/')
        if rel_path.startswith(self.remote_base + '/'):
            rel_path = rel_path[len(self.remote_base) + 1:]
        elif rel_path == self.remote_base:
            rel_path = ''
        
        # Ensure we never create paths outside the mount point
        if rel_path:
            result = self.local_dir / rel_path
        else:
            result = self.local_dir
        
        # Safety check: ensure result is within local_dir (use string comparison to avoid resolve() issues)
        result_str = str(result)
        local_dir_str = str(self.local_dir)
        if not result_str.startswith(local_dir_str):
            # Path is outside mount point - return mount point instead
            logger.warning(f"Path {remote_path} would be outside mount point ({result_str}), using mount point")
            return self.local_dir
        
        return result
    
    def sync_from_remote(self, path: str = ""):
        """Sync files from remote to local"""
        # Wrap entire method to catch and handle permission errors gracefully
        try:
            # Ensure path doesn't start with / to avoid absolute path issues
            path = path.lstrip('/')
            # Build remote path relative to base_path
            # remote_base is the user's root directory (e.g., "verita84@poster.place")
            # For the root, pass empty string to ls() since base_url already includes /webdav
            # For subdirectories, pass the relative path
            if path:
                # Path is relative to remote_base
                ls_path = f"{self.remote_base}/{path}" if self.remote_base else path
            else:
                # Empty path means root - pass remote_base to ls()
                ls_path = self.remote_base if self.remote_base else ""
            logger.debug(f"sync_from_remote called with path='{path}', ls_path='{ls_path}' (remote_base: '{self.remote_base}')")
            
            # List remote directory (with retry logic)
            # ls_path is relative to base_url, so pass it directly to ls()
            try:
                # Ensure ls_path doesn't have leading slash (WebDAVClient._url handles it)
                ls_path = ls_path.lstrip('/')
                logger.debug(f"Calling ls() with path: '{ls_path}'")
                files = self.webdav.ls(ls_path, depth=1, retry_attempts=self.network_retry_attempts, retry_delay=self.network_retry_delay)
            except Exception as e:
                # If listing fails, log but try to continue
                error_str = str(e).lower()
                if 'permission denied' in error_str:
                    logger.debug(f"Permission error during listing (known issue, continuing): {e}")
                    files = []
                elif 'connection' in error_str or 'timeout' in error_str or 'network' in error_str:
                    logger.warning(f"Network error listing {ls_path}: {e}")
                    # If we have cache, try to use cached file list
                    files = []
                    if self.cache:
                        logger.info("Network unavailable, using cache if available")
                else:
                    logger.warning(f"Error listing {ls_path}: {e}")
                    files = []
            
            for file_info in files:
                try:
                    file_remote_path = file_info['path']
                    
                    # Fix path duplication bug: if we're syncing a subdirectory and the path is duplicated
                    # e.g., if path='Joplin' and file_remote_path='verita84@poster.place/Joplin/Joplin/file.md'
                    # we should correct it to 'verita84@poster.place/Joplin/file.md'
                    if path:
                        # We're in a subdirectory, check for duplication
                        expected_prefix = f"{self.remote_base}/{path}"
                        duplicated_prefix = f"{expected_prefix}/{path}"
                        
                        # Check if path starts with duplicated prefix (e.g., "verita84@poster.place/Joplin/Joplin/")
                        if file_remote_path.startswith(duplicated_prefix + '/'):
                            # Path is duplicated, fix it by removing one instance of the directory name
                            # file_remote_path = 'verita84@poster.place/Joplin/Joplin/file.md'
                            # We want: 'verita84@poster.place/Joplin/file.md'
                            # Simply replace the duplicated prefix with the expected prefix
                            file_remote_path = expected_prefix + file_remote_path[len(duplicated_prefix):]
                            logger.info(f"[WebDAV Sync] Fixed duplicated path: {file_info['path']} -> {file_remote_path}")
                        elif not file_remote_path.startswith(expected_prefix + '/'):
                            # Path doesn't start with expected prefix, might be from wrong directory
                            logger.debug(f"Skipping path outside current directory: {file_remote_path} (expected prefix: {expected_prefix})")
                            continue
                    
                    # Check if this is a directory from the listing (if available)
                    is_dir_from_listing = file_info.get('isdir', False)
                    
                    # Skip the base directory itself (it's the mount point, not a file to sync)
                    normalized_remote = file_remote_path.rstrip('/')
                    if normalized_remote == self.remote_base or normalized_remote == f'/{self.remote_base}':
                        logger.debug(f"Skipping base directory: {file_remote_path}")
                        continue
                    
                    # If we're in a subdirectory (path is not empty), the file_remote_path from ls()
                    # will be relative to the root (e.g., "verita84@poster.place/Joplin/file.md")
                    # We need to check if it's actually within our current path context
                    # For root sync (path=''), file_remote_path should start with remote_base
                    # For subdirectory sync (path='Joplin'), file_remote_path should start with remote_base/Joplin
                    # But ls() returns paths relative to root, so we need to handle this correctly
                    
                    file_local_path = self._local_path(file_remote_path)
                    logger.debug(f"Processing: {file_remote_path} -> {file_local_path} (current path context: '{path}')")
                    
                    # Ensure we're not trying to create paths outside the mount point
                    # Use string-based check to avoid permission errors on resolve()
                    local_str = str(file_local_path)
                    mount_str = str(self.local_dir)
                    if not local_str.startswith(mount_str):
                        logger.warning(f"Skipping path outside mount point: {file_remote_path} -> {file_local_path}")
                        continue
                    
                    # Use isdir from listing if available (MUCH faster than calling info() for each file)
                    # The ls() listing already provides isdir and mtime, so we trust it to avoid slow info() calls
                    isdir = is_dir_from_listing
                    
                    # Get mtime from listing if available (ls() includes getlastmodified in PROPFIND response)
                    listing_mtime = file_info.get('modified', None)
                    file_size = file_info.get('size', -1)
                    
                    # Only call info() if:
                    # 1. Listing didn't provide isdir AND size is 0 (might be a directory), OR
                    # 2. We need mtime for file comparison AND listing doesn't have it
                    info = None
                    need_info = False
                    
                    if isdir:
                        # For directories, we don't need info() - create minimal info from listing
                        info = {
                            'isdir': True,
                            'size': 0,
                            'modified': listing_mtime or time.time()
                        }
                    elif not is_dir_from_listing and file_size == 0:
                        # Size is 0 and not marked as directory - might be a directory, check with info()
                        need_info = True
                    elif file_local_path.exists() and listing_mtime is None:
                        # File exists locally and we need mtime to compare, but listing doesn't have it
                        need_info = True
                    else:
                        # We have enough info from listing - use it!
                        info = {
                            'isdir': False,
                            'size': file_size if file_size >= 0 else 0,
                            'modified': listing_mtime or time.time()
                        }
                    
                    # Only call info() if really needed (rare case)
                    if need_info:
                        try:
                            info_path = file_remote_path
                            if not info_path.startswith('/'):
                                info_path = '/' + info_path
                            logger.debug(f"Calling info() for {info_path} (needed)")
                            info = self.webdav.info(info_path)
                            if info:
                                isdir = info.get('isdir', isdir)
                        except Exception as e:
                            logger.debug(f"Error getting info for {file_remote_path}: {e}, using listing result")
                            # Create fallback info from listing
                            if info is None:
                                info = {
                                    'isdir': isdir,
                                    'size': file_size if file_size >= 0 else 0,
                                    'modified': listing_mtime or time.time()
                                }
                    
                    if info is None:
                        logger.debug(f"No info available for {file_remote_path}, skipping")
                        continue
                    
                    logger.debug(f"[WebDAV Sync] {file_remote_path}: isdir={isdir}, size={info.get('size')}, mtime={info.get('modified')} (from {'info()' if need_info else 'listing'})")
                    
                    if isdir:
                        # It's really a directory - create locally and recurse
                        logger.info(f"[WebDAV Sync] Creating directory: {file_local_path}")
                        try:
                            # Double-check path is valid before creating
                            path_str = str(file_local_path)
                            mount_str = str(self.local_dir)
                            if not path_str.startswith(mount_str):
                                logger.warning(f"Skipping invalid path: {file_remote_path} -> {file_local_path}")
                                continue
                            
                            # If a file exists at this path, remove it first (it should be a directory)
                            if file_local_path.exists() and file_local_path.is_file():
                                logger.info(f"Removing file that should be directory: {file_local_path}")
                                file_local_path.unlink()
                            
                            logger.info(f"Creating directory: {file_local_path}")
                            # Create parent directories first, then the directory itself
                            file_local_path.parent.mkdir(parents=True, exist_ok=True)
                            file_local_path.mkdir(exist_ok=True)
                        except OSError as e:
                            if 'Permission denied' in str(e):
                                logger.error(f"Permission error creating {file_local_path}: {e}")
                                logger.error(f"  file_remote_path: {file_remote_path}")
                                logger.error(f"  file_local_path: {file_local_path}")
                                logger.error(f"  local_dir: {self.local_dir}")
                                import traceback
                                logger.error(traceback.format_exc())
                            continue
                        except Exception as e:
                            logger.error(f"Cannot create directory {file_local_path}: {e}")
                            import traceback
                            logger.error(traceback.format_exc())
                            continue
                        
                        # Calculate relative path for recursive sync
                        # file_remote_path from ls() is always relative to root, e.g., "verita84@poster.place/Documents"
                        # If we're currently syncing path='', then rel_path should be "Documents"
                        # If we're currently syncing path='Joplin', then file_remote_path might be "verita84@poster.place/Joplin/file.md"
                        #   and we should NOT recurse (we're already in Joplin)
                        #   OR if it's "verita84@poster.place/Joplin/subdir", rel_path should be "Joplin/subdir"
                        
                        rel_path = file_remote_path.lstrip('/')
                        if rel_path.startswith(self.remote_base + '/'):
                            # Remove the remote_base prefix
                            rel_path = rel_path[len(self.remote_base) + 1:]
                        elif rel_path == self.remote_base:
                            continue  # Already syncing base
                        
                        # If we're in a subdirectory (path is not empty), check if this item is within our current path
                        if path:
                            # We're syncing a subdirectory, e.g., path='Joplin'
                            # file_remote_path should be like "verita84@poster.place/Joplin/file.md" or "verita84@poster.place/Joplin/subdir"
                            # rel_path after removing remote_base would be "Joplin/file.md" or "Joplin/subdir"
                            # We need to check if rel_path starts with our current path
                            if rel_path.startswith(path + '/'):
                                # This is a subdirectory within our current path, e.g., "Joplin/subdir"
                                # Extract just the subdirectory name relative to current path
                                rel_path = rel_path[len(path) + 1:]
                                # Find the first directory component
                                if '/' in rel_path:
                                    rel_path = path + '/' + rel_path.split('/')[0]
                                else:
                                    rel_path = path + '/' + rel_path
                            elif rel_path == path:
                                # This is the directory we're currently syncing, skip recursion
                                continue
                            else:
                                # This item is not within our current path context, skip
                                logger.debug(f"Skipping {file_remote_path} - not in current path context '{path}'")
                                continue
                        
                        # Recurse into directory to get files inside
                        if rel_path:  # Only recurse if there's a subpath
                            logger.debug(f"Recursing into directory: {rel_path} (from {file_remote_path}, current path: '{path}')")
                            self.sync_from_remote(rel_path)
                    else:
                        # File - download if newer or missing
                        should_download = True
                        if file_local_path.exists():
                            try:
                                local_mtime = file_local_path.stat().st_mtime
                                if local_mtime >= info.get('modified', 0):
                                    should_download = False
                            except OSError:
                                # File might have been deleted, download it
                                should_download = True
                        
                        if should_download:
                            # Check cache first
                            cached_content = None
                            if self.cache:
                                cached_content = self.cache.get_cached_content(file_remote_path)
                                if cached_content:
                                    logger.debug(f"Using cached content for {file_remote_path}")
                                    content = cached_content
                                    # Write cached content to local file
                                    file_local_path.parent.mkdir(parents=True, exist_ok=True)
                                    file_local_path.write_bytes(content)
                                    os.utime(file_local_path, (info['modified'], info['modified']))
                                    logger.debug(f"Restored from cache: {file_remote_path}")
                                    continue  # Skip download, already have it
                            
                            # Try to download (with retry logic)
                            try:
                                # Normalize path for download
                                download_path = file_remote_path
                                if not download_path.startswith('/'):
                                    download_path = '/' + download_path
                                content = self.webdav.download(download_path, retry_attempts=self.network_retry_attempts, retry_delay=self.network_retry_delay)
                                file_local_path.parent.mkdir(parents=True, exist_ok=True)
                                file_local_path.write_bytes(content)
                                # Set mtime
                                os.utime(file_local_path, (info['modified'], info['modified']))
                                
                                # Cache it if cache is enabled and directory should be cached
                                if self.cache:
                                    should_cache = self.cache.should_cache_directory(file_remote_path) if self.cache.cache_directories else True
                                    if should_cache:
                                        self.cache.cache_file(file_remote_path, content, info['modified'], force=True)
                                    else:
                                        logger.debug(f"Skipping cache for {file_remote_path} (directory not in cache_directories)")
                                    # Update sync state (always, regardless of cache setting)
                                    content_hash = hashlib.md5(content).hexdigest()
                                    self.cache.update_sync_state(file_remote_path, file_local_path, info['modified'], len(content), content_hash)
                                
                                logger.debug(f"Downloaded: {file_remote_path}")
                            except WebDAVError as e:
                                error_str = str(e).lower()
                                if 'connection' in error_str or 'timeout' in error_str or 'network' in error_str:
                                    logger.warning(f"Network error downloading {file_remote_path}: {e}")
                                    # Use cached content if available
                                    if cached_content:
                                        logger.info(f"Using cached content for {file_remote_path} due to network error")
                                        file_local_path.parent.mkdir(parents=True, exist_ok=True)
                                        file_local_path.write_bytes(cached_content)
                                        os.utime(file_local_path, (info['modified'], info['modified']))
                                    else:
                                        logger.warning(f"No cached content available for {file_remote_path}, skipping")
                                else:
                                    logger.warning(f"Failed to download {file_remote_path}: {e}")
                            except Exception as e:
                                logger.warning(f"Failed to download {file_remote_path}: {e}")
                except OSError as e:
                    # Handle permission errors specifically
                    if 'Permission denied' in str(e):
                        logger.error(f"Permission denied: {e}")
                        import traceback
                        logger.error(f"Traceback: {traceback.format_exc()}")
                        continue  # Skip this file and continue
                    else:
                        raise
                except Exception as e:
                    logger.warning(f"Error processing file: {e}")
                    continue  # Skip this file and continue
        except OSError as e:
            # Handle permission errors - don't fail completely, just log and continue
            if 'Permission denied' in str(e) and '/verita84' in str(e):
                # This is the known permission error - log but continue
                logger.debug(f"Permission error (known issue, continuing): {e}")
                # Don't return - continue processing files
                pass
            elif 'Permission denied' in str(e):
                logger.warning(f"Permission error during sync: {e}")
                import traceback
                logger.debug(f"Traceback: {traceback.format_exc()}")
                # Don't re-raise - continue with sync
            else:
                raise
        except Exception as e:
            import traceback
            # Only log as error if it's not the known permission issue
            if 'Permission denied' in str(e) and '/verita84' in str(e):
                logger.debug(f"Known permission error (continuing): {e}")
                # Don't return - allow sync to continue
            else:
                logger.error(f"Error syncing from remote {path}: {e}")
                logger.debug(f"Traceback: {traceback.format_exc()}")
                # For other errors, log but don't return - allow sync to complete
    
    def sync_to_remote(self, local_path: Path):
        """Sync a local file to remote"""
        remote_path = self._remote_path(local_path)
        
        try:
            if local_path.is_file():
                content = local_path.read_bytes()
                self.webdav.upload(remote_path, content)
                # Update cache and sync state
                local_mtime = local_path.stat().st_mtime
                if self.cache:
                    self.cache.cache_file(remote_path, content, local_mtime)
                    self.cache.mark_clean(remote_path)
                    # Update sync state
                    content_hash = hashlib.md5(content).hexdigest()
                    self.cache.update_sync_state(remote_path, local_path, local_mtime, len(content), content_hash)
                logger.debug(f"Uploaded: {remote_path}")
            elif local_path.is_dir():
                self.webdav.mkdir(remote_path)
                logger.debug(f"Created directory: {remote_path}")
        except Exception as e:
            logger.error(f"Error syncing to remote {local_path}: {e}")
    
    def sync_pending_changes(self):
        """Sync all pending changes from cache"""
        if not self.cache:
            return
        
        pending = self.cache.get_pending_changes()
        if not pending:
            return
        
        logger.info(f"Syncing {len(pending)} pending changes...")
        for remote_path, meta in list(pending.items()):
            try:
                content = self.cache.get_cached_content(remote_path)
                if content is None:
                    continue
                
                self.webdav.upload(remote_path, content)
                self.cache.mark_clean(remote_path)
                # Update sync state
                if 'modified_at' in meta:
                    content_hash = hashlib.md5(content).hexdigest()
                    local_path = self._local_path(remote_path)
                    self.cache.update_sync_state(remote_path, local_path, meta['modified_at'], len(content), content_hash)
                logger.debug(f"Synced: {remote_path}")
            except Exception as e:
                logger.warning(f"Failed to sync {remote_path}: {e}")
    
    def detect_local_changes(self) -> List[Path]:
        """Detect locally modified files by polling (comparing mtimes)"""
        if not self.cache:
            return []
        
        changed_files = []
        
        # Walk local directory
        for local_path in self.local_dir.rglob('*'):
            if not local_path.is_file():
                continue
            
            try:
                remote_path = self._remote_path(local_path)
                local_stat = local_path.stat()
                local_mtime = local_stat.st_mtime
                local_size = local_stat.st_size
                
                # Get sync state
                sync_state = self.cache.get_sync_state(remote_path)
                
                if sync_state:
                    # File was synced before - check if it changed
                    cached_mtime = sync_state.get('mtime', 0)
                    cached_size = sync_state.get('size', 0)
                    
                    if local_mtime > cached_mtime or local_size != cached_size:
                        # File was modified locally
                        logger.debug(f"Detected local change: {local_path} (mtime: {local_mtime} > {cached_mtime})")
                        changed_files.append(local_path)
                        # Mark as dirty for upload
                        self.cache.mark_dirty(remote_path)
                else:
                    # New file - mark for upload
                    logger.debug(f"Detected new local file: {local_path}")
                    changed_files.append(local_path)
                    # Add to metadata if not present
                    if remote_path not in self.cache.metadata:
                        self.cache.metadata[remote_path] = {}
                    self.cache.mark_dirty(remote_path)
                    
            except Exception as e:
                logger.debug(f"Error checking {local_path}: {e}")
                continue
        
        return changed_files
    
    def detect_deletions(self, current_remote_files: set) -> Tuple[List[str], List[Path]]:
        """Detect deleted files in both directions
        
        Returns:
            (remote_deletions, local_deletions)
        """
        if not self.cache:
            return [], []
        
        remote_deletions = []
        local_deletions = []
        
        # Get files from last sync
        last_sync_paths = self.cache.get_all_sync_state_paths()
        
        # Remote deletions: files that existed in last sync but not in current remote
        for remote_path in last_sync_paths:
            if remote_path not in current_remote_files:
                sync_state = self.cache.get_sync_state(remote_path)
                local_path_str = sync_state.get('local_path')
                if local_path_str:
                    local_path = Path(local_path_str)
                    if local_path.exists():
                        # File exists locally but not remotely - was deleted on remote
                        remote_deletions.append(remote_path)
                        logger.info(f"Detected remote deletion: {remote_path}")
        
        # Local deletions: files that exist remotely but not locally (and were synced before)
        for remote_path in current_remote_files:
            sync_state = self.cache.get_sync_state(remote_path)
            if sync_state:
                local_path_str = sync_state.get('local_path')
                if local_path_str:
                    local_path = Path(local_path_str)
                    if not local_path.exists():
                        # File exists remotely but not locally - was deleted locally
                        local_deletions.append(local_path)
                        logger.info(f"Detected local deletion: {local_path} (remote: {remote_path})")
        
        return remote_deletions, local_deletions
    
    def detect_moves(self, current_remote_files: dict) -> List[Tuple[str, str]]:
        """Detect moved/renamed files by comparing content hashes
        
        Args:
            current_remote_files: dict of {remote_path: file_info} from current sync
            
        Returns:
            List of (old_remote_path, new_remote_path) tuples
        """
        if not self.cache:
            return []
        
        moves = []
        
        # Build hash map from sync state (files we've seen before)
        sync_state_hashes = {}  # {hash: remote_path}
        for remote_path, sync_state in self.cache.sync_state.items():
            content_hash = sync_state.get('hash')
            if content_hash:
                if content_hash not in sync_state_hashes:
                    sync_state_hashes[content_hash] = []
                sync_state_hashes[content_hash].append(remote_path)
        
        # For each current remote file, check if we can compute its hash
        # and see if it matches a file from sync state with different path
        for remote_path, file_info in current_remote_files.items():
            if file_info.get('isdir', False):
                continue
            
            # Check if this file exists locally (we can compute hash)
            sync_state = self.cache.get_sync_state(remote_path)
            if not sync_state:
                # New file - check if it matches a deleted file's hash
                local_path = self._local_path(remote_path)
                if local_path.exists() and local_path.is_file():
                    try:
                        content = local_path.read_bytes()
                        content_hash = hashlib.md5(content).hexdigest()
                        
                        # Check if this hash matches a file that was in sync state but is now missing
                        if content_hash in sync_state_hashes:
                            for old_remote_path in sync_state_hashes[content_hash]:
                                if old_remote_path != remote_path:
                                    # Check if old file still exists remotely
                                    old_sync_state = self.cache.get_sync_state(old_remote_path)
                                    if old_sync_state and old_remote_path not in current_remote_files:
                                        # Old file was deleted, new file has same hash - likely a move!
                                        moves.append((old_remote_path, remote_path))
                                        logger.info(f"Detected move/rename: {old_remote_path} -> {remote_path} (hash match)")
                    except Exception as e:
                        logger.debug(f"Error computing hash for {local_path}: {e}")
        
        return moves
    
    def resolve_conflict(self, remote_path: str, local_path: Path, remote_mtime: float, local_mtime: float) -> str:
        """Resolve conflict when both local and remote files were modified
        
        Returns:
            'local', 'remote', or 'manual'
        """
        if self.conflict_resolution == 'last_write_wins':
            if local_mtime > remote_mtime:
                return 'local'
            else:
                return 'remote'
        elif self.conflict_resolution == 'local_wins':
            return 'local'
        elif self.conflict_resolution == 'remote_wins':
            return 'remote'
        else:  # 'manual'
            # Create conflict file
            conflict_path = local_path.with_suffix(local_path.suffix + '.conflict')
            if local_path.exists():
                shutil.copy2(local_path, conflict_path)
                logger.warning(f"Conflict detected for {remote_path}: created {conflict_path}")
            return 'remote'  # Use remote version, user can manually merge
    
    def sync_bidirectional(self, path: str = ""):
        """Perform bidirectional sync: detect changes in both directions and sync accordingly"""
        # Step 1: Detect local changes
        logger.debug("Detecting local changes...")
        local_changes = self.detect_local_changes()
        if local_changes:
            logger.info(f"Detected {len(local_changes)} locally modified files")
        
        # Step 2: Get current remote file list
        try:
            path = path.lstrip('/')
            if path:
                ls_path = f"{self.remote_base}/{path}" if self.remote_base else path
            else:
                ls_path = self.remote_base if self.remote_base else ""
            ls_path = ls_path.lstrip('/')
            
            files = self.webdav.ls(ls_path, depth=1, retry_attempts=self.network_retry_attempts, retry_delay=self.network_retry_delay)
            current_remote_files = {f['path']: f for f in files}
            current_remote_paths = set(current_remote_files.keys())
        except Exception as e:
            logger.warning(f"Error listing remote files: {e}")
            current_remote_files = {}
            current_remote_paths = set()
        
        # Step 3: Detect moves/renames (before deletions, as moves might look like delete+create)
        moves = self.detect_moves(current_remote_files)
        for old_remote_path, new_remote_path in moves:
            try:
                # Use WebDAV MOVE to handle the rename on server
                self.webdav.mv(old_remote_path, new_remote_path)
                # Update sync state
                old_sync_state = self.cache.get_sync_state(old_remote_path)
                if old_sync_state:
                    # Move sync state to new path
                    new_local_path = self._local_path(new_remote_path)
                    self.cache.update_sync_state(new_remote_path, new_local_path, 
                                               old_sync_state.get('mtime', time.time()),
                                               old_sync_state.get('size', 0),
                                               old_sync_state.get('hash'))
                    self.cache.remove_from_sync_state(old_remote_path)
                    # Also move local file if it exists
                    old_local_path_str = old_sync_state.get('local_path')
                    if old_local_path_str:
                        old_local_path = Path(old_local_path_str)
                        if old_local_path.exists():
                            new_local_path.parent.mkdir(parents=True, exist_ok=True)
                            if old_local_path.is_file():
                                shutil.move(str(old_local_path), str(new_local_path))
                            elif old_local_path.is_dir():
                                shutil.move(str(old_local_path), str(new_local_path))
                            logger.info(f"Moved local file: {old_local_path} -> {new_local_path}")
                logger.info(f"Handled move/rename: {old_remote_path} -> {new_remote_path}")
            except Exception as e:
                logger.warning(f"Error handling move {old_remote_path} -> {new_remote_path}: {e}")
        
        # Step 4: Detect deletions (after moves, as moves might have been detected as deletions)
        remote_deletions, local_deletions = self.detect_deletions(current_remote_paths)
        
        # Step 5: Handle remote deletions (delete local files)
        for remote_path in remote_deletions:
            sync_state = self.cache.get_sync_state(remote_path)
            local_path_str = sync_state.get('local_path')
            if local_path_str:
                local_path = Path(local_path_str)
                try:
                    if local_path.exists():
                        if local_path.is_file():
                            local_path.unlink()
                        elif local_path.is_dir():
                            shutil.rmtree(local_path)
                        logger.info(f"Deleted local file (remote was deleted): {local_path}")
                    self.cache.remove_from_sync_state(remote_path)
                except Exception as e:
                    logger.warning(f"Error deleting local file {local_path}: {e}")
        
        # Step 6: Handle local deletions (delete remote files)
        for local_path in local_deletions:
            remote_path = self._remote_path(local_path)
            try:
                self.webdav.delete(remote_path)
                logger.info(f"Deleted remote file (local was deleted): {remote_path}")
                self.cache.remove_from_sync_state(remote_path)
            except Exception as e:
                logger.warning(f"Error deleting remote file {remote_path}: {e}")
        
        # Step 7: Sync from remote (with conflict detection)
        self.sync_from_remote_with_conflicts(path, current_remote_files)
        
        # Step 8: Sync local changes to remote
        for local_path in local_changes:
            remote_path = self._remote_path(local_path)
            sync_state = self.cache.get_sync_state(remote_path)
            
            # Check for conflicts
            if sync_state:
                remote_mtime = sync_state.get('mtime', 0)
                local_mtime = local_path.stat().st_mtime
                
                # Check if remote was also modified
                if remote_path in current_remote_files:
                    remote_info = current_remote_files[remote_path]
                    remote_current_mtime = remote_info.get('modified', 0)
                    
                    if remote_current_mtime > remote_mtime and local_mtime > remote_mtime:
                        # Both were modified - conflict!
                        resolution = self.resolve_conflict(remote_path, local_path, remote_current_mtime, local_mtime)
                        if resolution == 'remote':
                            # Use remote version, skip local upload
                            logger.info(f"Conflict resolved: using remote version for {remote_path}")
                            continue
                        # else: use local version (upload it)
            
            # Upload local file
            try:
                self.sync_to_remote(local_path)
            except Exception as e:
                logger.warning(f"Error uploading {local_path}: {e}")
    
    def sync_from_remote_with_conflicts(self, path: str = "", current_remote_files: dict = None):
        """Sync from remote with conflict detection"""
        # Use existing sync_from_remote but track sync state
        # This is a wrapper that adds sync state tracking
        if current_remote_files is None:
            # Fallback to regular sync_from_remote
            self.sync_from_remote(path)
            return
        
        # Enhanced version that tracks sync state
        # For now, call regular sync_from_remote and update sync state
        self.sync_from_remote(path)
        
        # Update sync state for all synced files
        # This would be done in sync_from_remote, but we'll add it here for now


class WebDAVMount:
    """WebDAV mount manager - pure Python, no FUSE!"""
    
    def __init__(self):
        self.config = self.load_config()
        self.mount_point = Path(self.config.get("mount_point", str(Path.home() / "PosterchanAI-Mount")))
        self.webdav_url = self.config.get("webdav_url", "")
        self.username = self.config.get("username", "")
        self.password = self.config.get("password", "")
        self.enable_cache = self.config.get("enable_cache", True)
        self.sync_interval = self.config.get("sync_interval", 30)
        self._setup_webdav_client()
        self._setup_cache()
        self._sync = None
    
    def load_config(self) -> dict:
        """Load configuration from file"""
        default_config = {
            "webdav_url": "",
            "username": "",
            "password": "",
            "mount_point": str(Path.home() / "PosterchanAI-Mount"),
            "enable_cache": True,
            "sync_interval": 10,  # Reduced from 30s for faster sync
            "cache_max_size_mb": 204800,  # Default 200GB cache
            "cache_max_age_days": 30,
            "cache_directories": [],  # List of directories to always cache (e.g., ["documents", "images"])
            "network_retry_attempts": 5,
            "network_retry_delay": 5,  # seconds
            "offline_mode": False,  # If True, only use cache, don't try to sync
            "conflict_resolution": "last_write_wins"  # Options: 'last_write_wins', 'local_wins', 'remote_wins', 'manual'
        }
        
        if CONFIG_FILE.exists():
            try:
                with open(CONFIG_FILE, 'r') as f:
                    user_config = json.load(f)
                    default_config.update(user_config)
            except Exception as e:
                logger.error(f"Error loading config: {e}")
        
        return default_config
    
    def _setup_webdav_client(self):
        """Setup WebDAV client connection"""
        if not self.webdav_url or not self.username or not self.password:
            self.webdav = None
            return
        
        try:
            parsed = urllib.parse.urlparse(self.webdav_url)
            
            path = parsed.path.rstrip('/') or ''
            path_parts = path.strip('/').split('/') if path else []
            
            # Extract base_path - skip 'webdav' if present, use username
            if path_parts:
                # If path contains 'webdav', skip it and use the next part (username)
                if 'webdav' in path_parts:
                    webdav_idx = path_parts.index('webdav')
                    if webdav_idx + 1 < len(path_parts):
                        # Use everything after 'webdav' as base_path
                        self.base_path = '/'.join(path_parts[webdav_idx + 1:])
                    else:
                        # webdav is last, use username
                        self.base_path = self.username
                else:
                    # No 'webdav' in path, use all path parts
                    self.base_path = '/'.join(path_parts)
                # Base URL is always scheme://netloc/webdav (if webdav is in path)
                if 'webdav' in path_parts:
                    base_url = f"{parsed.scheme}://{parsed.netloc}/webdav"
                else:
                    base_url = f"{parsed.scheme}://{parsed.netloc}"
            else:
                self.base_path = self.username
                base_url = f"{parsed.scheme}://{parsed.netloc}/webdav"
            
            self.webdav = WebDAVClient(base_url, self.username, self.password)
            logger.debug(f"WebDAV client configured: {base_url} (base_path: {self.base_path})")
        except Exception as e:
            logger.error(f"Error setting up WebDAV client: {e}")
            self.webdav = None
    
    def _setup_cache(self):
        """Setup cache manager with configuration"""
        if self.enable_cache:
            cache_max_size_mb = self.config.get("cache_max_size_mb", 10240)  # Default 10GB
            cache_max_age_days = self.config.get("cache_max_age_days", 30)
            cache_directories = self.config.get("cache_directories", [])
            self.cache = CacheManager(
                CACHE_DIR,
                max_size_mb=cache_max_size_mb,
                max_age_days=cache_max_age_days,
                cache_directories=cache_directories
            )
            logger.info(f"Offline cache enabled: max_size={cache_max_size_mb}MB, max_age={cache_max_age_days}days, cache_dirs={cache_directories}")
        else:
            self.cache = None
    
    def _check_mount(self) -> bool:
        """Check if mount point exists and is accessible"""
        return self.mount_point.exists() and self.mount_point.is_dir()
    
    def _check_network_connectivity(self) -> Tuple[bool, str]:
        """Check if network is available and WebDAV server is reachable"""
        network_interface_up = False
        try:
            result = subprocess.run(
                ["ip", "link", "show", "up"],
                capture_output=True,
                text=True,
                timeout=3
            )
            if result.returncode == 0 and result.stdout.strip():
                network_interface_up = True
        except:
            try:
                net_dir = Path("/sys/class/net")
                if net_dir.exists():
                    for interface in net_dir.iterdir():
                        if interface.is_dir():
                            operstate = (interface / "operstate").read_text().strip()
                            if operstate == "up":
                                network_interface_up = True
                                break
            except:
                pass
        
        if not network_interface_up:
            return False, "No network interfaces are up"
        
        try:
            parsed = urllib.parse.urlparse(self.webdav_url)
            hostname = parsed.hostname
            port = parsed.port or (443 if parsed.scheme == 'https' else 80)
            
            if not hostname:
                return False, "Invalid WebDAV URL (no hostname)"
            
            try:
                socket.gethostbyname(hostname)
            except socket.gaierror as e:
                return False, f"Cannot resolve hostname {hostname}: {e}"
            
            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.settimeout(3)
                result = sock.connect_ex((hostname, port))
                sock.close()
                
                if result != 0:
                    return False, f"Cannot connect to {hostname}:{port}"
            except socket.timeout:
                return False, f"Connection to {hostname}:{port} timed out"
            except Exception as e:
                return False, f"Connection test failed: {e}"
            
            return True, ""
        except Exception as e:
            return False, f"Network check error: {e}"
    
    def _detect_suspend_resume(self) -> bool:
        """Detect if system was suspended/resumed"""
        global _last_suspend_check, _suspend_detected
        
        current_time = time.time()
        time_diff = current_time - _last_suspend_check
        
        if time_diff > 300:
            if not _suspend_detected:
                logger.info(f"Suspend/resume detected (time jump: {time_diff:.1f}s)")
                _suspend_detected = True
                _last_suspend_check = current_time
                return True
        else:
            _suspend_detected = False
        
        _last_suspend_check = current_time
        return False
    
    def mount(self) -> bool:
        """Initialize sync (create mount point and do initial sync)"""
        if not self.webdav_url or not self.username or not self.password:
            logger.error("WebDAV URL, username, or password not configured")
            return False
        
        if not self.webdav:
            logger.error("WebDAV client not initialized")
            return False
        
        # Create mount point
        try:
            self.mount_point.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            logger.error(f"Cannot create mount point {self.mount_point}: {e}")
            return False
        
        logger.info(f"Initializing sync: {self.webdav_url} -> {self.mount_point}")
        
        # Get network retry settings
        network_retry_attempts = self.config.get("network_retry_attempts", 5)
        network_retry_delay = self.config.get("network_retry_delay", 5)
        
        # Get conflict resolution strategy
        conflict_resolution = self.config.get("conflict_resolution", "last_write_wins")
        
        # Create sync manager
        self._sync = WebDAVSync(
            self.webdav, 
            self.mount_point, 
            self.base_path, 
            self.cache,
            network_retry_attempts=network_retry_attempts,
            network_retry_delay=network_retry_delay,
            conflict_resolution=conflict_resolution
        )
        
        # Check if offline mode is enabled
        offline_mode = self.config.get("offline_mode", False)
        
        # Do initial sync
        network_ok, network_error = self._check_network_connectivity()
        if network_ok and not offline_mode:
            logger.info("Network available, performing initial sync...")
            try:
                # Use bidirectional sync for initial sync too
                self._sync.sync_bidirectional()
                # Cleanup old cache after successful sync
                if self.cache:
                    self.cache.cleanup_old_cache()
                # Check if files were actually synced
                synced_files = list(self.mount_point.rglob('*'))
                file_count = len([f for f in synced_files if f.is_file()])
                if file_count > 0:
                    logger.info(f"Initial sync complete - {file_count} files synced")
                else:
                    logger.warning("Initial sync completed but no files found")
            except Exception as e:
                # Log but don't fail - sync will retry on next interval
                error_str = str(e).lower()
                if 'permission denied' in error_str:
                    logger.debug(f"Initial sync permission error (known issue, will retry): {e}")
                elif 'connection' in error_str or 'timeout' in error_str or 'network' in error_str:
                    logger.warning(f"Network error during initial sync: {e}, will use cache if available")
                else:
                    logger.warning(f"Initial sync had errors (will retry): {e}")
                # Still check if any files were synced despite the error
                synced_files = list(self.mount_point.rglob('*'))
                file_count = len([f for f in synced_files if f.is_file()])
                if file_count > 0:
                    logger.info(f"Initial sync attempt complete - {file_count} files synced despite errors")
                else:
                    logger.info("Initial sync attempt complete (no files synced)")
        elif offline_mode:
            logger.info("Offline mode enabled, skipping sync")
        else:
            logger.warning(f"Network not available: {network_error}, using cache if available")
        
        return True
    
    def monitor(self):
        """Monitor and sync periodically"""
        global _running, _online
        
        logger.info("Starting sync monitor (pure Python, no FUSE!)")
        
        last_sync = 0
        last_network_check = 0
        network_check_interval = 60
        
        while _running:
            try:
                current_time = time.time()
                
                # Detect suspend/resume
                if self._detect_suspend_resume():
                    logger.info("System resume detected")
                    last_sync = 0  # Force sync after resume
                
                # Check network periodically
                if current_time - last_network_check >= network_check_interval:
                    network_ok, network_error = self._check_network_connectivity()
                    was_online = _online
                    _online = network_ok
                    last_network_check = current_time
                    
                    if _online and not was_online:
                        logger.info("Network reconnected - syncing...")
                        if self._sync:
                            self._sync.sync_bidirectional()
                        last_sync = 0  # Force sync
                    elif not _online:
                        logger.debug(f"Network offline: {network_error}")
                
                # Sync if online and interval has passed (or offline mode is disabled)
                offline_mode = self.config.get("offline_mode", False)
                if not offline_mode and _online and self._sync and (current_time - last_sync >= self.sync_interval):
                    try:
                        # Perform bidirectional sync
                        # This handles: local changes, remote changes, deletes, conflicts
                        self._sync.sync_bidirectional()
                        # Cleanup old cache periodically
                        if self.cache and (current_time % 3600 < self.sync_interval):  # Once per hour
                            self.cache.cleanup_old_cache()
                        last_sync = current_time
                    except Exception as e:
                        error_str = str(e).lower()
                        if 'connection' in error_str or 'timeout' in error_str or 'network' in error_str:
                            logger.warning(f"Network error during sync: {e}, will retry")
                        else:
                            logger.error(f"Error during sync: {e}")
                
                time.sleep(10)  # Check every 10 seconds
                
            except KeyboardInterrupt:
                raise
            except Exception as e:
                logger.error(f"Error in monitor loop: {e}", exc_info=True)
                time.sleep(30)


def signal_handler(signum, frame):
    """Handle shutdown signals"""
    global _running
    logger.info(f"Received signal {signum}, shutting down...")
    _running = False
    sys.exit(0)


def main():
    """Main daemon function"""
    global _running, _online
    _online = True
    
    if not CONFIG_FILE.exists():
        logger.error("Configuration file not found. Please run setup first:")
        logger.error("  python3 webdav_mount.py --setup")
        sys.exit(1)
    
    mount = WebDAVMount()
    
    if not mount.webdav_url or not mount.username or not mount.password:
        logger.error("WebDAV configuration incomplete. Please run setup:")
        logger.error("  python3 webdav_mount.py --setup")
        sys.exit(1)
    
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    
    # Initialize mount (creates directory and does initial sync)
    if not mount.mount():
        logger.error("Failed to initialize WebDAV sync")
        sys.exit(1)
    
    _running = True
    monitor_thread = threading.Thread(target=mount.monitor, daemon=True)
    monitor_thread.start()
    
    logger.info("WebDAV sync daemon started (pure Python, no FUSE!)")
    logger.info(f"Local directory: {mount.mount_point}")
    logger.info(f"WebDAV URL: {mount.webdav_url}")
    logger.info(f"Sync interval: {mount.sync_interval}s")
    
    try:
        while _running:
            time.sleep(1)
    except KeyboardInterrupt:
        signal_handler(signal.SIGINT, None)


if __name__ == "__main__":
    import argparse
    
    parser = argparse.ArgumentParser(description="PosterchanAI WebDAV Sync Daemon (Pure Python)")
    parser.add_argument("--setup", action="store_true", help="Run setup wizard")
    parser.add_argument("--mount", action="store_true", help="Initialize sync")
    parser.add_argument("--status", action="store_true", help="Check sync status")
    
    args = parser.parse_args()
    
    if args.setup:
        from setup_wizard import check_and_run_setup
        if check_and_run_setup(force=True):
            sys.exit(0)
        else:
            sys.exit(1)
    elif args.mount:
        mount = WebDAVMount()
        if mount.mount():
            sys.exit(0)
        else:
            sys.exit(1)
    elif args.status:
        mount = WebDAVMount()
        if mount._check_mount():
            print(f"Synced directory: {mount.mount_point}")
            if mount.cache:
                pending = mount.cache.get_pending_changes()
                if pending:
                    print(f"Pending changes: {len(pending)} files")
                else:
                    print("No pending changes")
            sys.exit(0)
        else:
            print("Not initialized")
            sys.exit(1)
    else:
        main()
