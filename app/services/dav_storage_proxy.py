"""
Storage proxy helper for CalDAV/CardDAV operations.
Handles file operations through the storage server API.
"""
import logging
import httpx
from typing import List, Optional, Dict
from pathlib import Path
from sqlalchemy.orm import Session
from app.models import Setting

logger = logging.getLogger(__name__)


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
        # Use a fresh session to avoid session issues with async contexts
        try:
            # Try to use the provided session first
            try:
                storage_url_setting = db.query(Setting).filter(Setting.key == "storage_server_url").first()
                storage_token_setting = db.query(Setting).filter(Setting.key == "storage_server_token").first()
            except (AttributeError, RuntimeError, Exception) as session_error:
                # Session is invalid (closed, detached, or in wrong context), create a new one
                logger.debug(f"[{dav_type.upper()}] Session invalid, creating fresh session: {session_error}")
                from app.database import SessionLocal
                fresh_db = SessionLocal()
                try:
                    storage_url_setting = fresh_db.query(Setting).filter(Setting.key == "storage_server_url").first()
                    storage_token_setting = fresh_db.query(Setting).filter(Setting.key == "storage_server_token").first()
                finally:
                    fresh_db.close()
            
            self.storage_url = storage_url_setting.value if storage_url_setting and storage_url_setting.value else None
            self.storage_token = storage_token_setting.value if storage_token_setting and storage_token_setting.value else None
            self.use_proxy = bool(self.storage_url)
            
            if self.use_proxy:
                logger.info(f"[{dav_type.upper()}] Using storage proxy: {self.storage_url}")
        except Exception as e:
            logger.error(f"[{dav_type.upper()}] Error loading storage proxy config: {e}", exc_info=True)
            # Final fallback: try with a completely fresh session
            try:
                from app.database import SessionLocal
                fresh_db = SessionLocal()
                try:
                    storage_url_setting = fresh_db.query(Setting).filter(Setting.key == "storage_server_url").first()
                    storage_token_setting = fresh_db.query(Setting).filter(Setting.key == "storage_server_token").first()
                    self.storage_url = storage_url_setting.value if storage_url_setting and storage_url_setting.value else None
                    self.storage_token = storage_token_setting.value if storage_token_setting and storage_token_setting.value else None
                    self.use_proxy = bool(self.storage_url)
                finally:
                    fresh_db.close()
            except Exception as e2:
                logger.error(f"[{dav_type.upper()}] Error with fresh session fallback: {e2}", exc_info=True)
                self.storage_url = None
                self.storage_token = None
                self.use_proxy = False
    
    def _get_headers(self) -> Dict[str, str]:
        """Get auth headers for storage server requests."""
        headers = {}
        if self.storage_token:
            headers["Authorization"] = f"Bearer {self.storage_token}"
        return headers
    
    def list_files(self, subpath: str = "") -> List[Dict[str, any]]:
        """List files in DAV directory."""
        if not self.use_proxy:
            # Fallback to local filesystem
            return self._list_files_local(subpath)
        
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
        except Exception as e:
            logger.error(f"[{self.dav_type.upper()}] Error listing files: {e}", exc_info=True)
            return []
    
    def read_file(self, filepath: str) -> Optional[str]:
        """Read file content from storage."""
        if not self.use_proxy:
            return self._read_file_local(filepath)
        
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
        if not self.use_proxy:
            return self._write_file_local(filepath, content)
        
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
        if not self.use_proxy:
            return self._delete_file_local(filepath)
        
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
        if not self.use_proxy:
            return self._file_exists_local(filepath)
        
        # Try to read the file - if successful, it exists
        # read_file already handles fallback to local
        content = self.read_file(filepath)
        return content is not None
    
    # Local filesystem fallback methods (for when storage proxy is not configured)
    
    def _list_files_local(self, subpath: str = "") -> List[Dict[str, any]]:
        """List files from local filesystem."""
        from app.services.storage_service import get_storage_service
        storage = get_storage_service(self.db)
        user_path = storage.get_user_path(self.username)
        dav_path = user_path / self.dav_type / subpath
        
        if not dav_path.exists():
            dav_path.mkdir(parents=True, exist_ok=True)
            return []
        
        items = []
        for item in dav_path.iterdir():
            items.append({
                'name': item.name,
                'is_directory': item.is_dir(),
                'size': item.stat().st_size if item.is_file() else 0,
                'modified': item.stat().st_mtime
            })
        return items
    
    def _read_file_local(self, filepath: str) -> Optional[str]:
        """Read file from local filesystem."""
        from app.services.storage_service import get_storage_service
        storage = get_storage_service(self.db)
        user_path = storage.get_user_path(self.username)
        file_path = user_path / self.dav_type / filepath
        
        if file_path.exists():
            return file_path.read_text(encoding='utf-8')
        return None
    
    def _write_file_local(self, filepath: str, content: str) -> bool:
        """Write file to local filesystem."""
        from app.services.storage_service import get_storage_service
        storage = get_storage_service(self.db)
        user_path = storage.get_user_path(self.username)
        file_path = user_path / self.dav_type / filepath
        
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(content, encoding='utf-8')
        return True
    
    def _delete_file_local(self, filepath: str) -> bool:
        """Delete file from local filesystem."""
        from app.services.storage_service import get_storage_service
        storage = get_storage_service(self.db)
        user_path = storage.get_user_path(self.username)
        file_path = user_path / self.dav_type / filepath
        
        if file_path.exists():
            file_path.unlink()
            return True
        return False
    
    def _file_exists_local(self, filepath: str) -> bool:
        """Check if file exists on local filesystem."""
        from app.services.storage_service import get_storage_service
        storage = get_storage_service(self.db)
        user_path = storage.get_user_path(self.username)
        file_path = user_path / self.dav_type / filepath
        return file_path.exists()
