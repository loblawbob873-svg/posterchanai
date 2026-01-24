"""
Storage proxy helper for CalDAV/CardDAV operations.
Handles file operations through the storage server API.
When storage_server_url points to the same server, uses local filesystem directly.
"""
import logging
import httpx
from typing import List, Optional, Dict
from pathlib import Path
from sqlalchemy.orm import Session
from app.models import Setting
from urllib.parse import urlparse

logger = logging.getLogger(__name__)


def _is_same_server(storage_url: str) -> bool:
    """
    Check if storage_server_url points to the same server (localhost/127.0.0.1).
    When True, we can use local filesystem instead of HTTP requests.
    """
    if not storage_url:
        return False
    
    try:
        parsed = urlparse(storage_url)
        host = parsed.hostname or ""
        # Check if it's localhost or 127.0.0.1
        return host.lower() in ('localhost', '127.0.0.1', '::1', '0.0.0.0') or host == ''
    except Exception:
        return False


class DAVStorageProxy:
    """Proxy for DAV file operations through storage server."""
    
    def __init__(self, db: Session, username: str, dav_type: str):
        """
        Initialize DAV storage proxy.
        
        Args:
            db: Database session
            username: User's username
            dav_type: 'caldav' or 'cardav'
        """
        self.db = db
        self.username = username
        self.dav_type = dav_type
        # Use 'carddav' (two d's) to match local filesystem convention
        # CalDAV uses 'caldav' (one d), CardDAV uses 'carddav' (two d's)
        if dav_type == 'cardav':
            self.base_path = "carddav"
        else:
            self.base_path = f"{dav_type}"
        
        # Load storage server config
        # Always use a fresh session to avoid async context issues
        # The provided session might be closed or in an invalid state
        from app.database import SessionLocal
        
        fresh_db = None
        try:
            # Try to use the provided session first (if it's valid)
            try:
                # Test if session is valid by checking if it's bound
                if db.bind is not None:
                    # Try a simple query to see if session works
                    from sqlalchemy import text
                    test_result = db.execute(text("SELECT 1")).scalar()
                    if test_result == 1:
                        # Session seems valid, use it
                        storage_url_setting = db.query(Setting).filter(Setting.key == "storage_server_url").first()
                    else:
                        raise RuntimeError("Session test query failed")
                else:
                    raise RuntimeError("Session not bound")
            except (AttributeError, RuntimeError, IndexError, Exception) as session_error:
                # Session is invalid, create a fresh one
                logger.debug(f"[{dav_type.upper()}] Session invalid, creating fresh session: {type(session_error).__name__}")
                fresh_db = SessionLocal()
                storage_url_setting = fresh_db.query(Setting).filter(Setting.key == "storage_server_url").first()
            
            self.storage_url = storage_url_setting.value if storage_url_setting and storage_url_setting.value else None
            self.use_proxy = bool(self.storage_url)
            self.is_same_server = _is_same_server(self.storage_url) if self.storage_url else False
            
            if self.use_proxy:
                if self.is_same_server:
                    logger.info(f"[{dav_type.upper()}] Storage URL points to same server - using local filesystem: {self.storage_url}")
                else:
                    logger.info(f"[{dav_type.upper()}] Using storage proxy: {self.storage_url}")
        except Exception as e:
            logger.error(f"[{dav_type.upper()}] Error loading storage proxy config: {e}", exc_info=True)
            # Final fallback: disable proxy
            self.storage_url = None
            self.use_proxy = False
        finally:
            # Always close fresh session if we created one
            if fresh_db is not None:
                try:
                    fresh_db.close()
                except:
                    pass
    
    def _get_headers(self) -> Dict[str, str]:
        """Get headers for storage server requests (server-to-server, no auth needed)."""
        return {
            "X-Posterchanai-Load-Balanced": "true"
        }
    
    def _get_local_dav_path(self, subpath: str = "") -> Path:
        """Get local filesystem path for DAV directory."""
        from app.services.storage_service import StorageService
        storage = StorageService(self.db)
        user_path = storage.get_user_path(self.username)
        dav_path = user_path / self.base_path
        if subpath:
            dav_path = dav_path / subpath
        return dav_path
    
    def list_files(self, subpath: str = "") -> List[Dict[str, any]]:
        """List files in DAV directory."""
        if not self.use_proxy or self.is_same_server:
            # On storage server: Use local filesystem
            dav_path = self._get_local_dav_path(subpath)
            
            if not dav_path.exists():
                # Auto-create caldav/carddav directories if they don't exist
                try:
                    dav_path.mkdir(parents=True, exist_ok=True)
                    logger.info(f"[{self.dav_type.upper()}] Auto-created missing DAV directory: {dav_path}")
                except Exception as e:
                    logger.error(f"[{self.dav_type.upper()}] Failed to auto-create DAV directory {dav_path}: {e}", exc_info=True)
                    return []
            
            if not dav_path.is_dir():
                logger.warning(f"[{self.dav_type.upper()}] DAV path is not a directory: {dav_path}")
                return []
            
            items = []
            try:
                for item in sorted(dav_path.iterdir()):
                    try:
                        stat = item.stat()
                        is_dir = item.is_dir()
                        
                        # Calculate relative path from DAV base
                        base_dav_path = self._get_local_dav_path("")
                        relative_path = str(item.relative_to(base_dav_path))
                        
                        items.append({
                            "name": item.name,
                            "path": relative_path,
                            "is_directory": is_dir,
                            "size": stat.st_size if not is_dir else 0,
                            "modified": stat.st_mtime,
                        })
                    except Exception as e:
                        logger.warning(f"[{self.dav_type.upper()}] Error reading item {item}: {e}")
                        continue
            except Exception as e:
                logger.error(f"[{self.dav_type.upper()}] Error listing directory: {e}", exc_info=True)
                return []
            
            return items
        
        # Otherwise, use HTTP proxy
        try:
            # Build path: caldav/subpath or carddav/subpath
            api_path = f"{self.base_path}/{subpath}" if subpath else self.base_path
            api_path = api_path.rstrip('/')
            
            # Use storage server API: /api/storage/list-files?username=...&path=...
            url = f"{self.storage_url}/api/storage/list-files"
            params = {
                "username": self.username,
                "path": api_path
            }
            logger.debug(f"[{self.dav_type.upper()}] Listing files: {url} with path={api_path}")
            
            with httpx.Client(timeout=10.0) as client:
                response = client.get(url, params=params, headers=self._get_headers())
                if response.status_code == 200:
                    data = response.json()
                    items = data.get('items', [])
                    return items
                elif response.status_code == 404:
                    # Path not found on storage server - return empty list (data needs to be migrated)
                    logger.warning(f"[{self.dav_type.upper()}] Storage server returned 404 for {api_path} - data may need migration")
                    return []
                else:
                    logger.error(f"[{self.dav_type.upper()}] Storage server returned {response.status_code}: {response.text[:200]}")
                    return []
        except httpx.ConnectError as e:
            error_msg = str(e)
            logger.error(f"[{self.dav_type.upper()}] Cannot connect to storage server: {error_msg}")
            logger.error(f"[{self.dav_type.upper()}] Storage server may be down. Check if it's running at {self.storage_url}")
            # Return empty list but log the error clearly
            return []
        except Exception as e:
            logger.error(f"[{self.dav_type.upper()}] Error listing files: {e}", exc_info=True)
            return []
    
    def read_file(self, filepath: str) -> Optional[str]:
        """Read file content from storage."""
        if not self.use_proxy or self.is_same_server:
            # On storage server: Use local filesystem
            dav_path = self._get_local_dav_path(filepath)
            
            if not dav_path.exists():
                logger.debug(f"[{self.dav_type.upper()}] File does not exist: {dav_path}")
                return None
            
            if dav_path.is_dir():
                logger.warning(f"[{self.dav_type.upper()}] Path is a directory, not a file: {dav_path}")
                return None
            
            try:
                with open(dav_path, 'r', encoding='utf-8') as f:
                    return f.read()
            except Exception as e:
                logger.error(f"[{self.dav_type.upper()}] Error reading file {filepath}: {e}", exc_info=True)
                return None
        
        # Otherwise, use HTTP proxy
        try:
            # Build full path
            api_path = f"{self.base_path}/{filepath}".replace('//', '/')
            
            # Use storage server API: /api/storage/view-file?username=...&file_path=...
            url = f"{self.storage_url}/api/storage/view-file"
            params = {
                "username": self.username,
                "file_path": api_path
            }
            logger.debug(f"[{self.dav_type.upper()}] Reading file: {url} with file_path={api_path}")
            
            with httpx.Client(timeout=10.0) as client:
                response = client.get(url, params=params, headers=self._get_headers())
                if response.status_code == 200:
                    return response.text
                elif response.status_code == 404:
                    # File not found on storage server
                    logger.warning(f"[{self.dav_type.upper()}] File not found on storage server: {filepath} - data may need migration")
                    return None
                else:
                    logger.error(f"[{self.dav_type.upper()}] Failed to read {filepath}: {response.status_code}")
                    return None
        except Exception as e:
            logger.error(f"[{self.dav_type.upper()}] Error reading file {filepath}: {e}", exc_info=True)
            return None
    
    def write_file(self, filepath: str, content: str) -> bool:
        """Write file content to storage."""
        if not self.use_proxy or self.is_same_server:
            # On storage server: Use local filesystem
            dav_path = self._get_local_dav_path(filepath)
            
            # Ensure parent directory exists
            dav_path.parent.mkdir(parents=True, exist_ok=True)
            
            try:
                with open(dav_path, 'w', encoding='utf-8') as f:
                    f.write(content)
                logger.debug(f"[{self.dav_type.upper()}] Saved {filepath} to local filesystem")
                return True
            except Exception as e:
                logger.error(f"[{self.dav_type.upper()}] Error writing file {filepath}: {e}", exc_info=True)
                return False
        
        # Otherwise, use HTTP proxy
        try:
            # Build full path
            api_path = f"{self.base_path}/{filepath}".replace('//', '/')
            
            # Use storage server API: /api/storage/save-text-file
            url = f"{self.storage_url}/api/storage/save-text-file"
            logger.debug(f"[{self.dav_type.upper()}] Writing file: {api_path}")
            
            # Storage server expects form data: username, path, content
            payload = {
                "username": self.username,
                "path": api_path,
                "content": content
            }
            
            with httpx.Client(timeout=10.0) as client:
                response = client.post(url, data=payload, headers=self._get_headers())
                if response.status_code in (200, 201):
                    logger.info(f"[{self.dav_type.upper()}] Saved {filepath}")
                    return True
                else:
                    logger.error(f"[{self.dav_type.upper()}] Failed to save {filepath}: {response.status_code} - {response.text[:200]}")
                    return False
        except Exception as e:
            logger.error(f"[{self.dav_type.upper()}] Error writing file {filepath}: {e}", exc_info=True)
            return False
    
    def delete_file(self, filepath: str) -> bool:
        """Delete file from storage."""
        if not self.use_proxy or self.is_same_server:
            # On storage server: Use local filesystem
            dav_path = self._get_local_dav_path(filepath)
            
            if not dav_path.exists():
                logger.debug(f"[{self.dav_type.upper()}] File does not exist for deletion: {dav_path}")
                return False
            
            try:
                dav_path.unlink()
                logger.debug(f"[{self.dav_type.upper()}] Deleted {filepath} from local filesystem")
                return True
            except Exception as e:
                logger.error(f"[{self.dav_type.upper()}] Error deleting file {filepath}: {e}", exc_info=True)
                return False
        
        # Otherwise, use HTTP proxy
        try:
            # Build full path
            api_path = f"{self.base_path}/{filepath}".replace('//', '/')
            
            # Use storage server API: /api/storage/delete-file?username=...&file_path=...
            url = f"{self.storage_url}/api/storage/delete-file"
            params = {
                "username": self.username,
                "file_path": api_path
            }
            logger.debug(f"[{self.dav_type.upper()}] Deleting file: {api_path}")
            
            with httpx.Client(timeout=10.0) as client:
                response = client.delete(url, params=params, headers=self._get_headers())
                if response.status_code in (200, 204):
                    logger.info(f"[{self.dav_type.upper()}] Deleted {filepath}")
                    return True
                else:
                    logger.error(f"[{self.dav_type.upper()}] Failed to delete {filepath}: {response.status_code} - {response.text[:200]}")
                    return False
        except Exception as e:
            logger.error(f"[{self.dav_type.upper()}] Error deleting file {filepath}: {e}", exc_info=True)
            return False
    
    def file_exists(self, filepath: str) -> bool:
        """Check if file exists."""
        if not self.use_proxy or self.is_same_server:
            # On storage server: Use local filesystem
            dav_path = self._get_local_dav_path(filepath)
            return dav_path.exists() and dav_path.is_file()
        
        # Otherwise, try to read the file - if successful, it exists
        content = self.read_file(filepath)
        return content is not None
