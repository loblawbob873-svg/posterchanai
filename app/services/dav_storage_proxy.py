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
                        storage_token_setting = db.query(Setting).filter(Setting.key == "storage_server_token").first()
                    else:
                        raise RuntimeError("Session test query failed")
                else:
                    raise RuntimeError("Session not bound")
            except (AttributeError, RuntimeError, IndexError, Exception) as session_error:
                # Session is invalid, create a fresh one
                logger.debug(f"[{dav_type.upper()}] Session invalid, creating fresh session: {type(session_error).__name__}")
                fresh_db = SessionLocal()
                storage_url_setting = fresh_db.query(Setting).filter(Setting.key == "storage_server_url").first()
                storage_token_setting = fresh_db.query(Setting).filter(Setting.key == "storage_server_token").first()
            
            self.storage_url = storage_url_setting.value if storage_url_setting and storage_url_setting.value else None
            self.storage_token = storage_token_setting.value if storage_token_setting and storage_token_setting.value else None
            self.use_proxy = bool(self.storage_url)
            
            if self.use_proxy:
                logger.info(f"[{dav_type.upper()}] Using storage proxy: {self.storage_url}")
        except Exception as e:
            logger.error(f"[{dav_type.upper()}] Error loading storage proxy config: {e}", exc_info=True)
            # Final fallback: disable proxy
            self.storage_url = None
            self.storage_token = None
            self.use_proxy = False
        finally:
            # Always close fresh session if we created one
            if fresh_db is not None:
                try:
                    fresh_db.close()
                except:
                    pass
    
    def _get_headers(self) -> Dict[str, str]:
        """Get auth headers for storage server requests."""
        headers = {}
        if self.storage_token:
            headers["Authorization"] = f"Bearer {self.storage_token}"
        return headers
    
    def list_files(self, subpath: str = "") -> List[Dict[str, any]]:
        """List files in DAV directory."""
        if not self.use_proxy:
            # No fallback to local storage - storage proxy must be configured
            logger.error(f"[{self.dav_type.upper()}] Storage proxy not configured - cannot list files")
            return []
        
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
            # No fallback to local storage - storage proxy must be configured
            logger.error(f"[{self.dav_type.upper()}] Storage proxy not configured - cannot read file: {filepath}")
            return None
        
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
            # No fallback to local storage - storage proxy must be configured
            logger.error(f"[{self.dav_type.upper()}] Storage proxy not configured - cannot write file: {filepath}")
            return False
        
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
            # No fallback to local storage - storage proxy must be configured
            logger.error(f"[{self.dav_type.upper()}] Storage proxy not configured - cannot delete file: {filepath}")
            return False
        
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
            # No fallback to local storage - storage proxy must be configured
            logger.error(f"[{self.dav_type.upper()}] Storage proxy not configured - cannot check file existence: {filepath}")
            return False
        
        # Try to read the file - if successful, it exists
        content = self.read_file(filepath)
        return content is not None
