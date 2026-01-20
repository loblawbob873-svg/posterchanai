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
        return f"{self.base_url}/{path}" if path else self.base_url
    
    def info(self, path: str) -> dict:
        """Get file/directory info via PROPFIND"""
        url = self._url(path)
        try:
            response = self.session.request('PROPFIND', url, headers={'Depth': '0'}, timeout=10)
            response.raise_for_status()
            
            # Parse XML response
            root = ET.fromstring(response.content)
            ns = {'D': 'DAV:'}
            
            # Find the response element
            response_elem = root.find('.//D:response', ns)
            if response_elem is None:
                return None
            
            # Get href (path)
            href = response_elem.find('D:href', ns)
            if href is None:
                return None
            
            # Get properties
            propstat = response_elem.find('D:propstat', ns)
            if propstat is None:
                return None
            
            prop = propstat.find('D:prop', ns)
            if prop is None:
                return None
            
            # Extract info
            resourcetype = prop.find('D:resourcetype', ns)
            collection = resourcetype.find('D:collection', ns) if resourcetype is not None else None
            isdir = collection is not None
            
            contentlength = prop.find('D:getcontentlength', ns)
            size = int(contentlength.text) if contentlength is not None and contentlength.text else 0
            
            # If it has a size > 0, it's definitely a file, not a directory
            # Some WebDAV servers incorrectly report files as directories
            if size > 0:
                isdir = False
            
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
            logger.debug(f"PROPFIND failed for {path}: {e}")
            return None
    
    def ls(self, path: str, depth: int = 1) -> list:
        """List directory contents
        
        Args:
            path: Remote path to list
            depth: Depth of listing (0=self, 1=children, infinity=all descendants)
        """
        url = self._url(path)
        if not url.endswith('/'):
            url += '/'
        
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
                
                file_path = href.text.rstrip('/')
                # Remove base URL to get relative path
                if file_path.startswith(self.base_url):
                    file_path = file_path[len(self.base_url):].lstrip('/')
                elif file_path.startswith('/'):
                    # Absolute path - remove leading slash
                    file_path = file_path.lstrip('/')
                
                # Skip the directory itself
                normalized_path = path.rstrip('/').lstrip('/')
                if file_path == normalized_path or file_path == f'/{normalized_path}':
                    continue
                
                # Avoid duplicates
                if file_path in seen_paths:
                    continue
                seen_paths.add(file_path)
                
                # Get just the filename for 'name', but keep full path for 'path'
                filename = file_path.split('/')[-1]
                if filename:
                    files.append({'name': filename, 'path': file_path})
            
            return files
        except Exception as e:
            logger.error(f"Error listing {path}: {e}")
            raise WebDAVError(f"Failed to list directory: {e}")
    
    def download(self, path: str) -> bytes:
        """Download file content"""
        url = self._url(path)
        try:
            response = self.session.get(url, timeout=30)
            response.raise_for_status()
            return response.content
        except Exception as e:
            raise WebDAVError(f"Failed to download: {e}")
    
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
    """Manages offline cache for WebDAV files"""
    
    def __init__(self, cache_dir: Path):
        self.cache_dir = cache_dir
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.metadata_file = CACHE_METADATA
        self.metadata = self._load_metadata()
        self._lock = threading.Lock()
    
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
    
    def cache_file(self, remote_path: str, content: bytes, mtime: float = None):
        """Cache a file locally"""
        with self._lock:
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
            except Exception as e:
                logger.warning(f"Error caching file {remote_path}: {e}")
    
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


class WebDAVSync:
    """Pure Python WebDAV sync - no FUSE required!"""
    
    def __init__(self, webdav_client: WebDAVClient, local_dir: Path, remote_base: str, cache: CacheManager = None):
        self.webdav = webdav_client
        self.local_dir = Path(local_dir)
        self.remote_base = remote_base.rstrip('/')
        self.cache = cache
        self._lock = threading.Lock()
        self._sync_in_progress = False
    
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
            # Don't sync if path would be outside mount point
            if path and not path.startswith(self.remote_base):
                # Build full remote path
                remote_path = f"{self.remote_base}/{path}" if path else self.remote_base
            else:
                remote_path = self.remote_base if not path or path == self.remote_base else path
            logger.debug(f"sync_from_remote called with path='{path}', remote_path='{remote_path}'")
            
            # List remote directory
            try:
                files = self.webdav.ls(remote_path)
            except Exception as e:
                # If listing fails, log but try to continue
                if 'Permission denied' in str(e) and '/verita84' in str(e):
                    logger.debug(f"Permission error during listing (known issue, continuing): {e}")
                    # Don't return - try to continue with empty list
                    files = []
                else:
                    logger.warning(f"Error listing {remote_path}: {e}")
                    # For other errors, also try to continue
                    files = []
            
            for file_info in files:
                try:
                    file_remote_path = file_info['path']
                    
                    # Skip the base directory itself (it's the mount point, not a file to sync)
                    normalized_remote = file_remote_path.rstrip('/')
                    if normalized_remote == self.remote_base or normalized_remote == f'/{self.remote_base}':
                        logger.debug(f"Skipping base directory: {file_remote_path}")
                        continue
                    
                    file_local_path = self._local_path(file_remote_path)
                    logger.debug(f"Processing: {file_remote_path} -> {file_local_path}")
                    
                    # Ensure we're not trying to create paths outside the mount point
                    # Use string-based check to avoid permission errors on resolve()
                    local_str = str(file_local_path)
                    mount_str = str(self.local_dir)
                    if not local_str.startswith(mount_str):
                        logger.warning(f"Skipping path outside mount point: {file_remote_path} -> {file_local_path}")
                        continue
                    
                    # Get file info - ensure path is in correct format
                    try:
                        # Normalize path for info() call
                        info_path = file_remote_path
                        if not info_path.startswith('/'):
                            # Add leading slash if missing
                            info_path = '/' + info_path
                        info = self.webdav.info(info_path)
                    except Exception as e:
                        logger.warning(f"Error getting info for {file_remote_path}: {e}")
                        continue
                    if not info:
                        continue
                    
                    # If server reports it as a directory but it has a file extension,
                    # try downloading it first to verify it's actually a file
                    # (Some WebDAV servers incorrectly report files as directories)
                    has_extension = '.' in Path(file_remote_path).name and Path(file_remote_path).suffix
                    if info['isdir'] and has_extension:
                        # Try to download it as a file first
                        try:
                            # Normalize path for download
                            download_path = file_remote_path
                            if not download_path.startswith('/'):
                                download_path = '/' + download_path
                            content = self.webdav.download(download_path)
                            # Check if the content is HTML (directory listing) - if so, it's a directory with files inside
                            if content.startswith(b'<!DOCTYPE') or content.startswith(b'<html') or b'<html>' in content[:200]:
                                # It's a directory that returns HTML - but it might contain files!
                                # Don't skip it - treat it as a directory and recurse into it
                                logger.debug(f"{file_remote_path} returns HTML (directory listing) - treating as directory and recursing")
                                # Fall through to directory handling below
                            else:
                                # Download succeeded and it's real file content!
                                file_local_path.parent.mkdir(parents=True, exist_ok=True)
                                file_local_path.write_bytes(content)
                                os.utime(file_local_path, (info['modified'], info['modified']))
                                if self.cache:
                                    self.cache.cache_file(file_remote_path, content, info['modified'])
                                logger.debug(f"Downloaded file (server incorrectly reported as dir): {file_remote_path}")
                                continue
                        except Exception as e:
                            # Download failed - might be a directory, doesn't exist, or is actually a directory
                            # If it's a 404 or similar, skip it (file doesn't exist)
                            error_str = str(e).lower()
                            if '404' in error_str or 'not found' in error_str:
                                logger.debug(f"File {file_remote_path} does not exist, skipping")
                                continue
                            # Otherwise, treat as directory and recurse
                            logger.debug(f"Could not download {file_remote_path} as file: {e}, treating as directory")
                    
                    if info['isdir']:
                        # It's really a directory - create locally and recurse
                        try:
                            # Double-check path is valid before creating
                            path_str = str(file_local_path)
                            mount_str = str(self.local_dir)
                            if not path_str.startswith(mount_str):
                                logger.warning(f"Skipping invalid path: {file_remote_path} -> {file_local_path}")
                                continue
                            logger.debug(f"Creating directory: {file_local_path}")
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
                        rel_path = file_remote_path.lstrip('/')
                        if rel_path.startswith(self.remote_base + '/'):
                            rel_path = rel_path[len(self.remote_base) + 1:]
                        elif rel_path == self.remote_base:
                            continue  # Already syncing base
                        # Recurse into directory to get files inside (even if it has a file-like name)
                        if rel_path:  # Only recurse if there's a subpath
                            logger.debug(f"Recursing into directory: {rel_path}")
                            self.sync_from_remote(rel_path)
                    else:
                        # File - download if newer or missing
                        should_download = True
                        if file_local_path.exists():
                            local_mtime = file_local_path.stat().st_mtime
                            if local_mtime >= info['modified']:
                                should_download = False
                        
                        if should_download:
                            try:
                                # Normalize path for download
                                download_path = file_remote_path
                                if not download_path.startswith('/'):
                                    download_path = '/' + download_path
                                content = self.webdav.download(download_path)
                                file_local_path.parent.mkdir(parents=True, exist_ok=True)
                                file_local_path.write_bytes(content)
                                # Set mtime
                                os.utime(file_local_path, (info['modified'], info['modified']))
                                
                                # Cache it
                                if self.cache:
                                    self.cache.cache_file(file_remote_path, content, info['modified'])
                                
                                logger.debug(f"Downloaded: {file_remote_path}")
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
                # Update cache
                if self.cache:
                    self.cache.cache_file(remote_path, content, local_path.stat().st_mtime)
                    self.cache.mark_clean(remote_path)
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
                logger.debug(f"Synced: {remote_path}")
            except Exception as e:
                logger.warning(f"Failed to sync {remote_path}: {e}")


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
            "sync_interval": 30
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
        """Setup cache manager"""
        if self.enable_cache:
            self.cache = CacheManager(CACHE_DIR)
            logger.info("Offline cache enabled")
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
        
        # Create sync manager
        self._sync = WebDAVSync(self.webdav, self.mount_point, self.base_path, self.cache)
        
        # Do initial sync
        network_ok, network_error = self._check_network_connectivity()
        if network_ok:
            logger.info("Network available, performing initial sync...")
            try:
                self._sync.sync_from_remote()
                # Check if files were actually synced
                synced_files = list(self.mount_point.rglob('*'))
                file_count = len([f for f in synced_files if f.is_file()])
                if file_count > 0:
                    logger.info(f"Initial sync complete - {file_count} files synced")
                else:
                    logger.warning("Initial sync completed but no files found")
            except Exception as e:
                # Log but don't fail - sync will retry on next interval
                if 'Permission denied' in str(e) and '/verita84' in str(e):
                    logger.debug(f"Initial sync permission error (known issue, will retry): {e}")
                else:
                    logger.warning(f"Initial sync had errors (will retry): {e}")
                # Still check if any files were synced despite the error
                synced_files = list(self.mount_point.rglob('*'))
                file_count = len([f for f in synced_files if f.is_file()])
                if file_count > 0:
                    logger.info(f"Initial sync attempt complete - {file_count} files synced despite errors")
                else:
                    logger.info("Initial sync attempt complete (no files synced)")
        else:
            logger.warning(f"Network not available: {network_error}")
            logger.info("Will sync when network becomes available")
        
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
                            self._sync.sync_pending_changes()
                            self._sync.sync_from_remote()
                        last_sync = 0  # Force sync
                    elif not _online:
                        logger.debug(f"Network offline: {network_error}")
                
                # Sync if online and interval has passed
                if _online and self._sync and (current_time - last_sync >= self.sync_interval):
                    try:
                        # Sync pending changes first
                        self._sync.sync_pending_changes()
                        # Then sync from remote
                        self._sync.sync_from_remote()
                        last_sync = current_time
                    except Exception as e:
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
