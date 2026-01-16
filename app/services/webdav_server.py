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
        """Override to check quota before writing."""
        username = self._get_username_from_path(path)
        if username:
            allowed, error = self._check_quota(username, len(content))
            if not allowed:
                raise Exception(error or "Quota exceeded")
        
        result = super().write_file_content(path, content, etag=etag)
        
        # Invalidate file cache for parent directory
        self._invalidate_cache_for_path(username, path)
        
        return result
    
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


def create_webdav_app(db: Session) -> WsgiDAVApp:
    """Create WebDAV WSGI application."""
    storage = get_storage_service(db)
    root_path = Path(storage.upload_path)
    root_path.mkdir(parents=True, exist_ok=True)
    
    provider = QuotaFilesystemProvider(root_path, db)
    
    config = {
        "provider_mapping": {
            "/": provider,
        },
        "http_authenticator": {
            "domain_controller": PosterchanaiDomainController(db),
        },
        "simple_dc": {
            "user_mapping": {
                "*": True,  # Accept all authenticated users
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
