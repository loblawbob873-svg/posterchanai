"""
Built-in WebDAV Server - Serves user files via WebDAV protocol.
Uses the storage configuration and respects storage quotas.
"""
import logging
import threading
import os
from pathlib import Path
from typing import Optional
from sqlalchemy.orm import Session
from wsgidav.wsgidav_app import WsgiDAVApp
from wsgidav.fs_dav_provider import FilesystemProvider
from cheroot.wsgi import Server as WSGIServer

from app.models import User, Setting
from app.services.storage_service import StorageService, get_storage_service
from app.auth import verify_password

logger = logging.getLogger(__name__)

# Global server instance
_webdav_server: Optional[WSGIServer] = None
_webdav_thread: Optional[threading.Thread] = None


class QuotaFilesystemProvider(FilesystemProvider):
    """Filesystem provider with quota checking and remote storage support."""
    
    def __init__(self, root_path: Path, db: Session):
        super().__init__(root_path)
        self.db = db
        self.storage = get_storage_service(db)
        # Check if we need to proxy to remote storage
        self.storage_server_url = None
        self.storage_server_token = None
        storage_setting = db.query(Setting).filter(Setting.key == "storage_server_url").first()
        if storage_setting and storage_setting.value:
            url = storage_setting.value.strip()
            if url.startswith(('http://', 'https://')):
                self.storage_server_url = url
                # Get server-to-server token if available
                token_setting = db.query(Setting).filter(Setting.key == "storage_server_token").first()
                if token_setting and token_setting.value:
                    self.storage_server_token = token_setting.value
                    logger.info(f"[WebDAV] Remote storage server configured: {url} (with token)")
                else:
                    logger.warning(f"[WebDAV] Remote storage server configured: {url} (NO TOKEN - authentication may fail)")
                logger.info(f"[WebDAV] WebDAV will proxy ALL file operations to remote storage server")
                logger.info(f"[WebDAV] Local filesystem at {root_path} will NOT be used when remote storage is configured")
        
        # Log storage path and verify it's correct
        logger.info(f"[WebDAV] QuotaFilesystemProvider initialized with root_path: {root_path}")
        if root_path.exists():
            try:
                # Count files to verify this is the right location (only if local storage)
                if not self.storage_server_url:
                    file_count = sum(1 for _ in root_path.rglob('*') if _.is_file())
                    logger.info(f"[WebDAV] Root path contains {file_count} files")
            except Exception as e:
                logger.warning(f"[WebDAV] Could not count files in root_path: {e}")
    
    def create_collection(self, path: str):
        """Override to prevent creating directories with file names."""
        # Normalize path
        normalized_path = path.rstrip('/')
        
        # Check if this looks like a file (has extension)
        from pathlib import Path as PathLib
        path_obj = PathLib(normalized_path)
        if path_obj.suffix and path_obj.name != path_obj.stem:
            # This has a file extension - don't allow creating it as a directory
            logger.warning(f"[WebDAV] Attempted to create directory with file name: {normalized_path}")
            raise Exception(f"Cannot create directory with file name: {normalized_path}")
        
        # Check if a file exists at this path
        fs_path = self._locate_file_path(normalized_path)
        if fs_path and fs_path.exists() and fs_path.is_file():
            logger.warning(f"[WebDAV] File exists at directory path {normalized_path}")
            raise Exception(f"File exists at path: {normalized_path}")
        
        return super().create_collection(normalized_path)
    
    def _get_username_from_path(self, path: str) -> Optional[str]:
        """Extract username from WebDAV path."""
        # Path format: /username/... or /webdav/username/...
        # Username can contain @ (e.g., verita84@poster.place)
        # Strip /webdav prefix if present (WSGI middleware might not strip it)
        normalized_path = path.strip('/')
        if normalized_path.startswith('webdav/'):
            normalized_path = normalized_path[7:]  # Remove 'webdav/'
        
        # Username might contain @, so we need to be careful with splitting
        # The username is everything before the first /, but it might contain @
        if '/' in normalized_path:
            username = normalized_path.split('/', 1)[0]  # Split only on first /
        else:
            username = normalized_path
        
        if username:
            logger.debug(f"[WebDAV] Extracted username '{username}' from path '{path}' (normalized: '{normalized_path}')")
            return username
        logger.debug(f"[WebDAV] Could not extract username from path '{path}' (normalized: '{normalized_path}')")
        return None
    
    def _check_quota(self, username: str, additional_bytes: int = 0) -> tuple[bool, Optional[str]]:
        """Check if user has enough quota."""
        user = self.db.query(User).filter(User.username == username).first()
        if not user:
            return False, "User not found"
        
        # 0 means unlimited
        if user.storage_quota == 0:
            return True, None
        
        # Calculate current usage
        user_path = self.root_path / username
        current_usage = self._calculate_directory_size(user_path)
        
        if current_usage + additional_bytes > user.storage_quota:
            used_mb = current_usage / (1024 * 1024)
            quota_mb = user.storage_quota / (1024 * 1024)
            return False, f"Storage quota exceeded ({used_mb:.1f}MB / {quota_mb:.1f}MB)"
        
        return True, None
    
    def _calculate_directory_size(self, path: Path) -> int:
        """Calculate total size of directory in bytes."""
        if not path.exists():
            return 0
        
        total = 0
        try:
            for item in path.rglob('*'):
                if item.is_file():
                    total += item.stat().st_size
        except Exception as e:
            logger.warning(f"Error calculating directory size for {path}: {e}")
        
        return total
    
    def write_file_content(self, path: str, content: bytes, *, etag: Optional[str] = None):
        """Override to check quota and proxy to remote storage if configured."""
        # Strip /webdav prefix if present
        path_stripped = path.strip('/')
        if path_stripped.startswith('webdav/'):
            path_stripped = '/' + path_stripped[7:]
        else:
            path_stripped = path
        # Normalize path - remove trailing slash if present (files shouldn't have trailing slashes)
        normalized_path = path_stripped.rstrip('/')
        
        username = self._get_username_from_path(normalized_path)
        if username:
            # If remote storage is configured, ALWAYS proxy - never use local filesystem
            if self.storage_server_url:
                # Extract relative path
                rel_path = normalized_path.lstrip('/')
                if rel_path.startswith(username + '/'):
                    rel_path = rel_path[len(username) + 1:]
                elif rel_path == username:
                    rel_path = ''
                
                # Proxy upload - this is the ONLY way when remote storage is configured
                try:
                    self._proxy_upload_file(username, rel_path, content)
                    # Invalidate cache
                    self._invalidate_cache_for_path(username, normalized_path)
                    logger.debug(f"[WebDAV] Proxied file upload to storage server: {normalized_path} ({len(content)} bytes)")
                    return  # Success, don't write locally
                except Exception as e:
                    logger.error(f"[WebDAV] Failed to proxy upload to storage server: {e}")
                    # Don't fall back to local - raise the error
                    raise
            
            # Check quota (for local storage)
            allowed, error = self._check_quota(username, len(content))
            if not allowed:
                raise Exception(error or "Quota exceeded")
            
            # Get the actual filesystem path
            fs_path = self._locate_file_path(normalized_path)
            if fs_path and fs_path.exists() and fs_path.is_dir():
                # A directory exists with this name - remove it and create a file instead
                logger.warning(f"[WebDAV] Directory exists at file path {normalized_path}, removing and creating file")
                try:
                    fs_path.rmdir()  # Only works if directory is empty
                except OSError:
                    # Directory not empty - this is a problem, but try to continue
                    logger.error(f"[WebDAV] Cannot remove non-empty directory at {normalized_path}")
        
        result = super().write_file_content(normalized_path, content, etag=etag)
        
        # Invalidate file cache for parent directory
        if username:
            self._invalidate_cache_for_path(username, normalized_path)
        
        return result
    
    def _proxy_upload_file(self, username: str, file_path: str, content: bytes):
        """Proxy file upload - calls local API which automatically proxies to storage server."""
        import requests
        
        # Call the LOCAL API endpoint - it will automatically proxy to 192.168.0.85
        url = "http://localhost:3051/api/storage/upload-file"
        headers = {}
        if self.storage_server_token:
            headers["Authorization"] = f"Bearer {self.storage_server_token}"
        
        # Determine content type
        from pathlib import Path
        ext = Path(file_path).suffix
        content_type = 'application/octet-stream'
        if ext == '.txt' or ext == '.md':
            content_type = 'text/plain'
        elif ext == '.json':
            content_type = 'application/json'
        elif ext in ['.jpg', '.jpeg']:
            content_type = 'image/jpeg'
        elif ext == '.png':
            content_type = 'image/png'
        
        files = {
            'file': (Path(file_path).name, content, content_type)
        }
        data = {
            'username': username,
            'file_path': file_path
        }
        
        response = requests.post(url, headers=headers, files=files, data=data, timeout=60)
        response.raise_for_status()
        return response.json()
    
    def _locate_file_path(self, path: str) -> Optional[Path]:
        """Get the filesystem path for a WebDAV path."""
        try:
            # Remove leading slash and split
            parts = path.strip('/').split('/')
            if not parts or not parts[0]:
                return None
            
            # Build filesystem path
            fs_path = self.root_path
            for part in parts:
                if not part:  # Skip empty parts
                    continue
                fs_path = fs_path / part
            
            return fs_path
        except Exception:
            return None
    
    def get_resource_list(self, path: str, depth: int = 1, environ: dict = None):
        """Override to list files, with support for remote storage proxying."""
        # Strip /webdav prefix if present (may be added multiple times)
        original_path = path
        normalized_path = path.strip('/')
        # Remove all /webdav/ prefixes (in case it's duplicated)
        while normalized_path.startswith('webdav/'):
            normalized_path = normalized_path[7:]  # Remove 'webdav/'
        # Restore leading / if we have a path
        if normalized_path:
            normalized_path = '/' + normalized_path
        else:
            normalized_path = '/'
        
        logger.info(f"[WebDAV] get_resource_list CALLED: original={original_path}, normalized={normalized_path}, depth={depth}")
        logger.info(f"[WebDAV] storage_server_url={self.storage_server_url}, storage_server_token={'Yes' if self.storage_server_token else 'No'}")
        
        # If remote storage is configured, ALWAYS proxy - never use local filesystem
        if self.storage_server_url:
            username = self._get_username_from_path(normalized_path)
            logger.info(f"[WebDAV] Remote storage configured: {self.storage_server_url}, username={username}, path={normalized_path}")
            if username:
                # Extract relative path from WebDAV path
                # Path format: /username/subdir -> subdir
                rel_path = normalized_path.lstrip('/')
                if rel_path.startswith(username + '/'):
                    rel_path = rel_path[len(username) + 1:]
                elif rel_path == username:
                    rel_path = ''
                
                # Proxy to storage server - this is the ONLY way when remote storage is configured
                try:
                    result = self._proxy_list_files(username, rel_path)
                    logger.info(f"[WebDAV] Proxied list returned {len(result)} items for {username}/{rel_path}")
                    if len(result) == 0:
                        logger.warning(f"[WebDAV] Proxy returned 0 items - check if storage_server_url ({self.storage_server_url}) is correct")
                    return result
                except Exception as e:
                    logger.error(f"[WebDAV] Failed to proxy list to storage server: {e}", exc_info=True)
                    # Don't fall back to local - raise the error
                    raise
        
        # Only use local filesystem if remote storage is NOT configured
        return super().get_resource_list(normalized_path, depth, environ)
    
    def _proxy_list_files(self, username: str, path: str):
        """Proxy file listing - uses the same proxying mechanism as files router."""
        import requests
        
        # Use the same proxying logic as /api/files/list
        # Call storage_server_url/api/storage/list-files with server token
        if not self.storage_server_url:
            raise Exception("storage_server_url not configured")
        
        url = f"{self.storage_server_url.rstrip('/')}/api/storage/list-files"
        headers = {}
        if self.storage_server_token:
            headers["Authorization"] = f"Bearer {self.storage_server_token}"
        else:
            logger.warning(f"[WebDAV] No storage_server_token configured - authentication may fail")
        
        params = {
            "username": username,
            "path": path
        }
        
        logger.info(f"[WebDAV] Proxying to storage server: {url} for username={username}, path={path}")
        logger.info(f"[WebDAV] Using token: {'Yes' if self.storage_server_token else 'No'}")
        try:
            response = requests.get(url, headers=headers, params=params, timeout=30)
            logger.info(f"[WebDAV] Storage server response: status={response.status_code}, content_length={len(response.content)}")
            
            if response.status_code != 200:
                logger.error(f"[WebDAV] Storage server error: {response.status_code} - {response.text[:200]}")
            
            response.raise_for_status()
            data = response.json()
            
            # Convert File Manager format to WebDAV format
            items = data.get('items', [])
            logger.info(f"[WebDAV] Storage server returned {len(items)} items for {username}/{path}")
            if len(items) == 0:
                logger.warning(f"[WebDAV] Storage server returned 0 items - check if files exist on storage server")
            webdav_resources = []
            
            for item in items:
                item_path = item.get('path', item.get('name', ''))
                # Build full WebDAV path
                if path:
                    full_path = f"/{username}/{path}/{item_path}" if item_path else f"/{username}/{path}"
                else:
                    full_path = f"/{username}/{item_path}" if item_path else f"/{username}"
                
                # Normalize path (remove double slashes)
                full_path = full_path.replace('//', '/')
                
                # Create WebDAV resource info
                resource = {
                    'path': full_path,
                    'name': item.get('name', item_path),
                    'iscollection': item.get('is_directory', False),
                    'size': item.get('size', 0) if not item.get('is_directory', False) else 0,
                    'modified': item.get('modified', 0),
                }
                webdav_resources.append(resource)
            
            logger.info(f"[WebDAV] Proxied list from storage server: {len(webdav_resources)} items for {username}/{path}")
            if len(webdav_resources) == 0:
                logger.warning(f"[WebDAV] Proxy returned 0 items - this might indicate an issue with the storage server or path")
            return webdav_resources
            
        except Exception as e:
            logger.error(f"[WebDAV] Failed to proxy list to storage server: {e}", exc_info=True)
            raise
    
    def get_resource_info(self, path: str, environ: dict = None):
        """Override to ensure correct resource type detection, with remote storage support."""
        # Strip /webdav prefix if present
        path_stripped = path.strip('/')
        if path_stripped.startswith('webdav/'):
            path_stripped = '/' + path_stripped[7:]
        else:
            path_stripped = path
        # Normalize path - remove trailing slash for files
        normalized_path = path_stripped.rstrip('/')
        
        # If remote storage is configured, ALWAYS proxy - never use local filesystem
        if self.storage_server_url:
            username = self._get_username_from_path(normalized_path)
            if username:
                # Extract relative path
                rel_path = normalized_path.lstrip('/')
                if rel_path.startswith(username + '/'):
                    rel_path = rel_path[len(username) + 1:]
                elif rel_path == username:
                    rel_path = ''
                
                # Get info from storage server - this is the ONLY way when remote storage is configured
                try:
                    info = self._proxy_get_info(username, rel_path)
                    if info:
                        logger.debug(f"[WebDAV] Got resource info from storage server for {normalized_path}")
                        return info
                    # If not found, return None (404)
                    return None
                except Exception as e:
                    logger.error(f"[WebDAV] Failed to get info from storage server: {e}")
                    # Don't fall back to local - raise the error
                    raise
        
        # Always check filesystem directly first for accurate info
        fs_path = self._locate_file_path(normalized_path)
        if fs_path and fs_path.exists():
            # Get info directly from filesystem - most accurate
            stat = fs_path.stat()
            info = {
                'iscollection': fs_path.is_dir(),
                'size': 0 if fs_path.is_dir() else stat.st_size,
                'modified': stat.st_mtime,
            }
            logger.debug(f"[WebDAV] Resource info from filesystem for {normalized_path}: isdir={fs_path.is_dir()}, size={info['size']}")
            return info
        
        # If filesystem path doesn't exist, try parent method
        info = super().get_resource_info(normalized_path, environ)
        
        if info:
            # Double-check against filesystem if possible
            if fs_path and fs_path.exists():
                if fs_path.is_file() and info.get('iscollection', False):
                    # File is incorrectly reported as collection - fix it
                    info['iscollection'] = False
                    info['size'] = fs_path.stat().st_size
                    logger.debug(f"[WebDAV] Fixed resource type for {normalized_path}: file, not directory")
                elif fs_path.is_dir() and not info.get('iscollection', False):
                    # Directory is incorrectly reported as file - fix it
                    info['iscollection'] = True
                    info['size'] = 0
                    logger.debug(f"[WebDAV] Fixed resource type for {normalized_path}: directory, not file")
        
        return info
    
    def _proxy_get_info(self, username: str, path: str):
        """Get file info - calls local API which automatically proxies to storage server."""
        import requests
        
        # Call the LOCAL API endpoint - it will automatically proxy to 192.168.0.85
        url = "http://localhost:3051/api/storage/list-files"
        headers = {}
        if self.storage_server_token:
            headers["Authorization"] = f"Bearer {self.storage_server_token}"
        
        # Get parent directory and filename
        if path:
            path_parts = path.split('/')
            parent_path = '/'.join(path_parts[:-1]) if len(path_parts) > 1 else ''
            filename = path_parts[-1]
        else:
            parent_path = ''
            filename = ''
        
        params = {
            "username": username,
            "path": parent_path
        }
        
        try:
            response = requests.get(url, headers=headers, params=params, timeout=30)
            response.raise_for_status()
            data = response.json()
            
            # Find the file in the listing
            items = data.get('items', [])
            for item in items:
                if item.get('name') == filename or item.get('path') == path:
                    # Found it - convert to WebDAV format
                    full_path = f"/{username}/{path}" if path else f"/{username}"
                    return {
                        'path': full_path,
                        'name': item.get('name', filename),
                        'iscollection': item.get('is_directory', False),
                        'size': item.get('size', 0) if not item.get('is_directory', False) else 0,
                        'modified': item.get('modified', 0),
                    }
        except Exception as e:
            logger.debug(f"[WebDAV] Failed to get info from storage server: {e}")
        
        return None
    
    def read_file_content(self, path: str):
        """Override to proxy file downloads from remote storage if configured."""
        # Strip /webdav prefix if present
        path_stripped = path.strip('/')
        if path_stripped.startswith('webdav/'):
            path_stripped = '/' + path_stripped[7:]
        else:
            path_stripped = path
        normalized_path = path_stripped
        
        # If remote storage is configured, try to proxy the download
        if self.storage_server_url:
            username = self._get_username_from_path(normalized_path)
            if username:
                # Extract relative path
                rel_path = normalized_path.lstrip('/')
                if rel_path.startswith(username + '/'):
                    rel_path = rel_path[len(username) + 1:]
                elif rel_path == username:
                    rel_path = ''
                
                # Try to proxy download
                try:
                    content = self._proxy_download_file(username, rel_path)
                    logger.debug(f"[WebDAV] Proxied file download from storage server: {normalized_path}")
                    return content
                except Exception as e:
                    logger.debug(f"[WebDAV] Failed to proxy download: {e}, trying local")
                    # Fall through to local read
        
        # Use parent method to read local file
        return super().read_file_content(normalized_path)
    
    def _proxy_download_file(self, username: str, file_path: str) -> bytes:
        """Proxy file download - calls local API which automatically proxies to storage server."""
        import requests
        
        # Call the LOCAL API endpoint - it will automatically proxy to 192.168.0.85
        url = "http://localhost:3051/api/storage/download-file"
        headers = {}
        if self.storage_server_token:
            headers["Authorization"] = f"Bearer {self.storage_server_token}"
        
        params = {
            'username': username,
            'file_path': file_path
        }
        
        response = requests.get(url, headers=headers, params=params, timeout=60, stream=True)
        response.raise_for_status()
        return response.content
    
    def delete(self, path: str):
        """Override to invalidate cache on delete."""
        # Strip /webdav prefix if present
        path_stripped = path.strip('/')
        if path_stripped.startswith('webdav/'):
            path_stripped = '/' + path_stripped[7:]
        else:
            path_stripped = path
        normalized_path = path_stripped
        
        username = self._get_username_from_path(normalized_path)
        result = super().delete(normalized_path)
        
        # Invalidate file cache for parent directory
        if username:
            self._invalidate_cache_for_path(username, normalized_path)
        
        return result
    
    def move(self, src_path: str, dst_path: str):
        """Override to invalidate cache on move."""
        # Strip /webdav prefix if present
        def normalize(p):
            p_stripped = p.strip('/')
            if p_stripped.startswith('webdav/'):
                return '/' + p_stripped[7:]
            return p
        
        normalized_src = normalize(src_path)
        normalized_dst = normalize(dst_path)
        
        username = self._get_username_from_path(normalized_src)
        result = super().move(normalized_src, normalized_dst)
        
        # Invalidate cache for both source and destination directories
        if username:
            self._invalidate_cache_for_path(username, normalized_src)
            self._invalidate_cache_for_path(username, normalized_dst)
        
        return result
    
    def _invalidate_cache_for_path(self, username: str, path: str):
        """Invalidate file cache for a given path."""
        try:
            from app.routers.files import get_file_cache
            
            # Extract parent directory path relative to user root
            # Path format: /username/subdir/file.txt -> subdir
            # WebDAV paths are absolute, so we need to extract relative path
            path_parts = path.strip('/').split('/')
            if len(path_parts) > 1:
                # Remove username (first part) and filename (last part if file)
                # If it's a directory, include it; if it's a file, get its parent
                if len(path_parts) > 2:
                    # File: /username/subdir/file.txt -> subdir
                    parent_parts = path_parts[1:-1]
                else:
                    # Directory or file in root: /username/file.txt or /username/subdir
                    parent_parts = path_parts[1:-1] if len(path_parts) == 2 and '.' in path_parts[-1] else path_parts[1:]
                parent_path = '/'.join(parent_parts) if parent_parts else ""
            else:
                parent_path = ""
            
            # Normalize path (remove trailing slashes)
            parent_path = parent_path.strip('/')
            
            # Get cache and invalidate parent directory and root
            cache = get_file_cache(self.db)
            if parent_path:
                cache.invalidate(f"{username}:{parent_path}")
            cache.invalidate(f"{username}:")  # Also invalidate root to be safe
            logger.debug(f"[WebDAV] Invalidated cache for {username}:{parent_path}")
        except Exception as e:
            logger.warning(f"[WebDAV] Failed to invalidate cache: {e}")


class PosterchanaiDomainController:
    """Domain controller for WebDAV authentication."""
    
    def __init__(self, db: Session):
        self.db = db
    
    def get_domain_realm(self, path_info: str, environ: dict) -> str:
        """Return the realm for the given path."""
        return "Posterchanai WebDAV"
    
    def require_authentication(self, realm: str, environ: dict) -> bool:
        """Return True if authentication is required."""
        return True
    
    def is_authenticated(self, realm: str, username: str, password: str, environ: dict) -> bool:
        """Check if username/password is valid."""
        user = self.db.query(User).filter(User.username == username).first()
        if not user:
            return False
        
        return verify_password(password, user.password_hash)
    
    def get_realm(self, path_info: str, environ: dict) -> str:
        """Return the realm."""
        return "Posterchanai WebDAV"


def create_webdav_app(db: Session, mount_path: str = "/") -> WsgiDAVApp:
    """Create WebDAV WSGI application.
    
    Args:
        db: Database session
        mount_path: The path where WebDAV is mounted (e.g., "/webdav").
                    When mounted at /webdav, FastAPI should strip this prefix,
                    but WSGI middleware might not, so we handle it in the provider.
    """
    storage = get_storage_service(db)
    
    # Check if storage is on a remote server
    storage_server_url = db.query(Setting).filter(Setting.key == "storage_server_url").first()
    if storage_server_url and storage_server_url.value:
        url = storage_server_url.value.strip()
        if url.startswith(('http://', 'https://')):
            # Files are on remote storage server - WebDAV can't access them directly
            # We need to either proxy or mount the remote storage
            # This warning is outdated - we now proxy to remote storage
            logger.info(f"[WebDAV] Storage is on remote server ({url}) - WebDAV will proxy all operations to it")
    
    root_path = Path(storage.upload_path)
    root_path.mkdir(parents=True, exist_ok=True)
    logger.info(f"[WebDAV] Using storage path: {root_path}")
    
    # Verify the path exists and log what's in it
    if root_path.exists():
        try:
            # Count files in storage to verify it's the right location
            # Only scan first level to avoid long delays
            total_files = 0
            total_size = 0
            user_dirs = []
            
            # Scan user directories
            for item in root_path.iterdir():
                if item.is_dir():
                    user_dirs.append(item.name)
                    # Count files in this user directory (limit depth to avoid timeout)
                    try:
                        user_file_count = 0
                        user_file_size = 0
                        # Use rglob but limit to reasonable depth
                        for file_item in item.rglob('*'):
                            if file_item.is_file():
                                user_file_count += 1
                                total_files += 1
                                try:
                                    size = file_item.stat().st_size
                                    user_file_size += size
                                    total_size += size
                                except:
                                    pass
                        if user_file_count > 0:
                            logger.info(f"[WebDAV] User '{item.name}': {user_file_count} files ({user_file_size / (1024**3):.2f} GB)")
                    except Exception as e:
                        logger.debug(f"[WebDAV] Could not scan {item.name}: {e}")
                elif item.is_file():
                    total_files += 1
                    try:
                        total_size += item.stat().st_size
                    except:
                        pass
            
            logger.info(f"[WebDAV] Storage path: {root_path}")
            logger.info(f"[WebDAV] Found {len(user_dirs)} user directories")
            logger.info(f"[WebDAV] Total files: {total_files} ({total_size / (1024**3):.2f} GB)")
            
            # Check verita84 specifically
            verita84_path = root_path / 'verita84'
            if verita84_path.exists():
                verita84_files = sum(1 for _ in verita84_path.rglob('*') if _.is_file())
                verita84_size = sum(f.stat().st_size for f in verita84_path.rglob('*') if f.is_file())
                logger.info(f"[WebDAV] User 'verita84': {verita84_files} files ({verita84_size / (1024**3):.2f} GB)")
            else:
                logger.warning(f"[WebDAV] User 'verita84' directory does not exist at {verita84_path}")
        except Exception as e:
            logger.warning(f"[WebDAV] Could not scan storage path: {e}")
            import traceback
            logger.debug(f"[WebDAV] Traceback: {traceback.format_exc()}")
    else:
        logger.warning(f"[WebDAV] Storage path does not exist: {root_path}")
    
    provider = QuotaFilesystemProvider(root_path, db)
    
    # Use simple_dc for authentication - it accepts all users
    # We'll handle authentication at the FastAPI level via middleware
    config = {
        "provider_mapping": {
            "/": provider,  # Handle all paths from root (after mount prefix is stripped)
        },
        "simple_dc": {
            "user_mapping": {
                "*": True,  # Accept all users (authentication handled by FastAPI)
            }
        },
        "verbose": 1,
        "hotfixes": {
            "emulate_win32_lastmod": False,
        },
    }
    
    app = WsgiDAVApp(config)
    return app


def start_webdav_server(db: Session, port: int = 8080) -> bool:
    """Start the WebDAV server in a background thread."""
    global _webdav_server, _webdav_thread
    
    if _webdav_server is not None:
        logger.warning("WebDAV server already running")
        return False
    
    try:
        app = create_webdav_app(db)
        
        # Create WSGI server
        _webdav_server = WSGIServer(
            bind_addr=('0.0.0.0', port),
            wsgi_app=app,
            numthreads=10
        )
        
        def run_server():
            try:
                logger.info(f"[WebDAV] Starting server on port {port}")
                _webdav_server.start()
            except Exception as e:
                logger.error(f"[WebDAV] Server error: {e}", exc_info=True)
        
        _webdav_thread = threading.Thread(target=run_server, daemon=True)
        _webdav_thread.start()
        
        logger.info(f"[WebDAV] Server started on port {port}")
        return True
    except Exception as e:
        logger.error(f"[WebDAV] Failed to start server: {e}", exc_info=True)
        return False


def stop_webdav_server():
    """Stop the WebDAV server."""
    global _webdav_server, _webdav_thread
    
    if _webdav_server is None:
        return
    
    try:
        _webdav_server.stop()
        _webdav_server = None
        if _webdav_thread:
            _webdav_thread.join(timeout=5)
            _webdav_thread = None
        logger.info("[WebDAV] Server stopped")
    except Exception as e:
        logger.error(f"[WebDAV] Error stopping server: {e}", exc_info=True)


def is_webdav_running() -> bool:
    """Check if WebDAV server is running."""
    return _webdav_server is not None and _webdav_server.ready
