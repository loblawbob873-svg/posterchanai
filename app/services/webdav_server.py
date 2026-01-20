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
    """Filesystem provider with quota checking."""
    
    def __init__(self, root_path: Path, db: Session):
        super().__init__(root_path)
        self.db = db
        self.storage = get_storage_service(db)
        # Check if we need to proxy to remote storage
        self.storage_server_url = None
        storage_setting = db.query(Setting).filter(Setting.key == "storage_server_url").first()
        if storage_setting and storage_setting.value:
            url = storage_setting.value.strip()
            if url.startswith(('http://', 'https://')):
                self.storage_server_url = url
                logger.info(f"[WebDAV] Remote storage server configured: {url}")
                logger.warning(f"[WebDAV] WebDAV will only show local files. Remote files must be accessed via File Manager API.")
    
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
        # Path format: /username/...
        parts = path.strip('/').split('/')
        if parts and parts[0]:
            return parts[0]
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
        """Override to check quota before writing and ensure files are created, not directories."""
        # Normalize path - remove trailing slash if present (files shouldn't have trailing slashes)
        normalized_path = path.rstrip('/')
        
        # If path ends with a file extension, it's definitely a file, not a directory
        # Ensure we don't have a directory with this name
        username = self._get_username_from_path(normalized_path)
        if username:
            # Check quota
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
        """Override to potentially proxy to remote storage or list local files."""
        # If remote storage is configured, we can't list remote files via WebDAV
        # WebDAV only works with local filesystem
        # Remote files must be accessed via the File Manager API
        return super().get_resource_list(path, depth, environ)
    
    def get_resource_info(self, path: str, environ: dict = None):
        """Override to ensure correct resource type detection."""
        # Normalize path - remove trailing slash for files
        normalized_path = path.rstrip('/')
        
        # Get resource info from parent
        info = super().get_resource_info(normalized_path, environ)
        
        if info:
            # Check if this is actually a file but reported as directory
            fs_path = self._locate_file_path(normalized_path)
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
        elif normalized_path:
            # If parent didn't return info, check filesystem directly
            fs_path = self._locate_file_path(normalized_path)
            if fs_path and fs_path.exists():
                # Create info dict from filesystem
                stat = fs_path.stat()
                info = {
                    'iscollection': fs_path.is_dir(),
                    'size': 0 if fs_path.is_dir() else stat.st_size,
                    'modified': stat.st_mtime,
                }
                logger.debug(f"[WebDAV] Created resource info from filesystem for {normalized_path}")
        
        return info
    
    def delete(self, path: str):
        """Override to invalidate cache on delete."""
        username = self._get_username_from_path(path)
        result = super().delete(path)
        
        # Invalidate file cache for parent directory
        if username:
            self._invalidate_cache_for_path(username, path)
        
        return result
    
    def move(self, src_path: str, dst_path: str):
        """Override to invalidate cache on move."""
        username = self._get_username_from_path(src_path)
        result = super().move(src_path, dst_path)
        
        # Invalidate cache for both source and destination directories
        if username:
            self._invalidate_cache_for_path(username, src_path)
            self._invalidate_cache_for_path(username, dst_path)
        
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
                    When mounted at /webdav, FastAPI strips this prefix,
                    so the provider still sees paths like /username/
    """
    storage = get_storage_service(db)
    
    # Check if storage is on a remote server
    storage_server_url = db.query(Setting).filter(Setting.key == "storage_server_url").first()
    if storage_server_url and storage_server_url.value:
        url = storage_server_url.value.strip()
        if url.startswith(('http://', 'https://')):
            # Files are on remote storage server - WebDAV can't access them directly
            # We need to either proxy or mount the remote storage
            logger.warning(f"[WebDAV] Storage is on remote server ({url}), but WebDAV only supports local storage")
            logger.warning(f"[WebDAV] Files on remote server won't be accessible via WebDAV")
            logger.warning(f"[WebDAV] Consider using local storage or implementing WebDAV proxy to remote storage")
    
    root_path = Path(storage.upload_path)
    root_path.mkdir(parents=True, exist_ok=True)
    logger.info(f"[WebDAV] Using storage path: {root_path}")
    
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
