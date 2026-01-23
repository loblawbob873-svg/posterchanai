"""
WebDAV Storage Client - Client for storing files on user-configured WebDAV servers.
Provides the same interface as local storage but uses WebDAV protocol.
"""
import logging
import httpx
import base64
from typing import Optional, List, Dict, BinaryIO
from pathlib import Path
from urllib.parse import urljoin, quote
from sqlalchemy.orm import Session
from app.models import UserSetting
import json

logger = logging.getLogger(__name__)


class WebDAVStorageClient:
    """Client for WebDAV storage operations."""
    
    def __init__(self, db: Session, user_id: int):
        """
        Initialize WebDAV storage client for a user.
        
        Args:
            db: Database session
            user_id: User ID to get WebDAV config for
        """
        self.db = db
        self.user_id = user_id
        self.base_url = None
        self.username = None
        self.password = None
        self.enabled = False
        
        self._load_config()
    
    def _load_config(self):
        """Load WebDAV configuration from user settings."""
        try:
            # Check if WebDAV storage is enabled
            enabled_setting = self.db.query(UserSetting).filter(
                UserSetting.user_id == self.user_id,
                UserSetting.key == "webdav_storage_enabled"
            ).first()
            
            if not enabled_setting or enabled_setting.value != "true":
                self.enabled = False
                return
            
            # Get WebDAV URL
            url_setting = self.db.query(UserSetting).filter(
                UserSetting.user_id == self.user_id,
                UserSetting.key == "webdav_storage_url"
            ).first()
            
            if not url_setting or not url_setting.value:
                self.enabled = False
                logger.warning(f"[WebDAV] User {self.user_id} has WebDAV enabled but no URL configured")
                return
            
            # Get username
            username_setting = self.db.query(UserSetting).filter(
                UserSetting.user_id == self.user_id,
                UserSetting.key == "webdav_storage_username"
            ).first()
            
            # Get password
            password_setting = self.db.query(UserSetting).filter(
                UserSetting.user_id == self.user_id,
                UserSetting.key == "webdav_storage_password"
            ).first()
            
            self.base_url = url_setting.value.rstrip('/')
            self.username = username_setting.value if username_setting else ""
            self.password = password_setting.value if password_setting else ""
            self.enabled = True
            
            logger.debug(f"[WebDAV] Loaded config for user {self.user_id}: {self.base_url}")
        except Exception as e:
            logger.error(f"[WebDAV] Error loading config for user {self.user_id}: {e}", exc_info=True)
            self.enabled = False
    
    def is_enabled(self) -> bool:
        """Check if WebDAV storage is enabled and configured."""
        result = self.enabled and self.base_url is not None
        logger.debug(f"[WebDAV] is_enabled() for user {self.user_id}: enabled={self.enabled}, base_url={self.base_url}, result={result}")
        return result
    
    def _get_auth(self) -> httpx.BasicAuth:
        """Get HTTP basic auth for WebDAV requests."""
        return httpx.BasicAuth(self.username, self.password)
    
    def _build_path(self, *path_parts: str) -> str:
        """Build a WebDAV path from parts, ensuring proper encoding."""
        # Join parts and encode each component
        path = '/'.join(quote(str(part), safe='') for part in path_parts if part)
        # Ensure path starts with /
        if not path.startswith('/'):
            path = '/' + path
        return path
    
    def _get_url(self, path: str) -> str:
        """Build full WebDAV URL from path."""
        if not path.startswith('/'):
            path = '/' + path
        return urljoin(self.base_url + '/', path.lstrip('/'))
    
    async def save_file(self, file_path: str, content: bytes, content_type: str = "application/octet-stream") -> str:
        """
        Save a file to WebDAV storage.
        
        Args:
            file_path: Path relative to user root (e.g., "chat/123/image.png")
            content: File content as bytes
            content_type: MIME type of the file
        
        Returns:
            Full WebDAV URL of saved file
        """
        if not self.is_enabled():
            raise ValueError("WebDAV storage is not enabled or configured")
        
        # Build WebDAV path
        dav_path = self._build_path(file_path)
        url = self._get_url(dav_path)
        
        # Ensure parent directory exists
        parent_path = str(Path(file_path).parent)
        if parent_path and parent_path != '.':
            await self._ensure_directory(parent_path)
        
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(60.0, connect=10.0)) as client:
                response = await client.put(
                    url,
                    content=content,
                    headers={
                        "Content-Type": content_type,
                        "Content-Length": str(len(content))
                    },
                    auth=self._get_auth()
                )
                response.raise_for_status()
                logger.debug(f"[WebDAV] Saved file: {url}")
                return url
        except httpx.HTTPStatusError as e:
            logger.error(f"[WebDAV] Failed to save file {url}: {e.response.status_code} - {e.response.text}")
            raise Exception(f"WebDAV PUT failed: {e.response.status_code}")
        except Exception as e:
            logger.error(f"[WebDAV] Error saving file {url}: {e}", exc_info=True)
            raise
    
    async def get_file(self, file_path: str) -> bytes:
        """
        Retrieve a file from WebDAV storage.
        
        Args:
            file_path: Path relative to user root
        
        Returns:
            File content as bytes
        """
        if not self.is_enabled():
            raise ValueError("WebDAV storage is not enabled or configured")
        
        dav_path = self._build_path(file_path)
        url = self._get_url(dav_path)
        
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(60.0, connect=10.0)) as client:
                response = await client.get(url, auth=self._get_auth())
                response.raise_for_status()
                return response.content
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                raise FileNotFoundError(f"File not found: {file_path}")
            logger.error(f"[WebDAV] Failed to get file {url}: {e.response.status_code}")
            raise Exception(f"WebDAV GET failed: {e.response.status_code}")
        except Exception as e:
            logger.error(f"[WebDAV] Error getting file {url}: {e}", exc_info=True)
            raise
    
    async def delete_file(self, file_path: str) -> bool:
        """
        Delete a file from WebDAV storage.
        
        Args:
            file_path: Path relative to user root
        
        Returns:
            True if deleted successfully
        """
        if not self.is_enabled():
            raise ValueError("WebDAV storage is not enabled or configured")
        
        dav_path = self._build_path(file_path)
        url = self._get_url(dav_path)
        
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(60.0, connect=10.0)) as client:
                response = await client.delete(url, auth=self._get_auth())
                # 204 No Content or 200 OK are success
                if response.status_code in (200, 204):
                    logger.debug(f"[WebDAV] Deleted file: {url}")
                    return True
                elif response.status_code == 404:
                    logger.warning(f"[WebDAV] File not found for deletion: {url}")
                    return False
                else:
                    logger.error(f"[WebDAV] Failed to delete file {url}: {response.status_code}")
                    raise Exception(f"WebDAV DELETE failed: {response.status_code}")
        except Exception as e:
            logger.error(f"[WebDAV] Error deleting file {url}: {e}", exc_info=True)
            raise
    
    async def list_files(self, directory: str = "", recursive: bool = False) -> List[Dict]:
        """
        List files in a directory using PROPFIND.
        
        Args:
            directory: Directory path relative to user root (empty = root)
            recursive: Whether to list recursively
        
        Returns:
            List of file/directory info dicts with keys: name, path, is_directory, size, modified
        """
        if not self.is_enabled():
            raise ValueError("WebDAV storage is not enabled or configured")
        
        # For root directory, use "/" directly (don't encode empty string)
        if directory:
            dav_path = self._build_path(directory)
        else:
            dav_path = "/"
        url = self._get_url(dav_path)
        
        logger.info(f"[WebDAV] list_files: directory='{directory}', dav_path='{dav_path}', url='{url}', base_url='{self.base_url}'")
        
        # PROPFIND request body
        depth = "infinity" if recursive else "1"
        propfind_body = """<?xml version="1.0" encoding="utf-8"?>
<d:propfind xmlns:d="DAV:">
    <d:prop>
        <d:displayname/>
        <d:resourcetype/>
        <d:getcontentlength/>
        <d:getlastmodified/>
    </d:prop>
</d:propfind>"""
        
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(60.0, connect=10.0)) as client:
                response = await client.request(
                    "PROPFIND",
                    url,
                    content=propfind_body,
                    headers={
                        "Content-Type": "application/xml",
                        "Depth": depth
                    },
                    auth=self._get_auth()
                )
                response.raise_for_status()
                logger.debug(f"[WebDAV] PROPFIND response status: {response.status_code}, content length: {len(response.text)}")
                
                # Parse XML response
                import xml.etree.ElementTree as ET
                root = ET.fromstring(response.text)
                
                items = []
                # Find all response elements
                for response_elem in root.findall(".//{DAV:}response"):
                    href_elem = response_elem.find("{DAV:}href")
                    if href_elem is None:
                        continue
                    
                    href = href_elem.text
                    logger.debug(f"[WebDAV] Processing href: '{href}'")
                    
                    # Extract relative path from href
                    # href might be absolute URL or relative path
                    if href.startswith('http://') or href.startswith('https://'):
                        # Absolute URL - extract path part
                        from urllib.parse import urlparse
                        parsed = urlparse(href)
                        rel_path = parsed.path
                        # Remove base_url path if it matches
                        if self.base_url and rel_path.startswith(self.base_url):
                            rel_path = rel_path[len(self.base_url):]
                    elif href.startswith(self.base_url):
                        # Starts with base_url but not full URL
                        rel_path = href[len(self.base_url):]
                    else:
                        # Relative path
                        rel_path = href
                    
                    # Normalize path (remove leading/trailing slashes for comparison)
                    rel_path_normalized = rel_path.strip('/')
                    dav_path_normalized = dav_path.strip('/')
                    
                    logger.debug(f"[WebDAV] rel_path='{rel_path}' (normalized: '{rel_path_normalized}'), dav_path='{dav_path}' (normalized: '{dav_path_normalized}')")
                    
                    # Skip the directory itself (exact match after normalization)
                    if rel_path_normalized == dav_path_normalized:
                        logger.debug(f"[WebDAV] Skipping directory itself: '{rel_path}'")
                        continue
                    
                    # For root listing, also skip if rel_path is empty or just "/"
                    if not directory and (not rel_path_normalized or rel_path_normalized == ''):
                        logger.debug(f"[WebDAV] Skipping empty path in root listing")
                        continue
                    
                    # Get properties
                    propstat = response_elem.find(".//{DAV:}propstat")
                    if propstat is None:
                        continue
                    
                    prop = propstat.find("{DAV:}prop")
                    if prop is None:
                        continue
                    
                    # Check if it's a directory
                    resourcetype = prop.find("{DAV:}resourcetype")
                    is_directory = resourcetype is not None and resourcetype.find("{DAV:}collection") is not None
                    
                    # Get size
                    contentlength = prop.find("{DAV:}getcontentlength")
                    size = int(contentlength.text) if contentlength is not None and contentlength.text else 0
                    
                    # Get modified time
                    lastmodified = prop.find("{DAV:}getlastmodified")
                    modified = 0
                    if lastmodified is not None and lastmodified.text:
                        from email.utils import parsedate_to_datetime
                        try:
                            dt = parsedate_to_datetime(lastmodified.text)
                            modified = dt.timestamp()
                        except:
                            pass
                    
                    # Extract filename
                    name = Path(rel_path).name
                    
                    item_data = {
                        "name": name,
                        "path": rel_path.lstrip('/'),
                        "is_directory": is_directory,
                        "size": size,
                        "modified": modified
                    }
                    logger.debug(f"[WebDAV] Adding item: {item_data}")
                    items.append(item_data)
                
                logger.info(f"[WebDAV] list_files returning {len(items)} items")
                return items
        except httpx.HTTPStatusError as e:
            logger.error(f"[WebDAV] Failed to list files {url}: {e.response.status_code}")
            raise Exception(f"WebDAV PROPFIND failed: {e.response.status_code}")
        except Exception as e:
            logger.error(f"[WebDAV] Error listing files {url}: {e}", exc_info=True)
            raise
    
    async def _ensure_directory(self, directory: str) -> bool:
        """
        Ensure a directory exists (create if needed).
        
        Args:
            directory: Directory path relative to user root
        
        Returns:
            True if directory exists or was created
        """
        if not directory or directory == '.':
            return True
        
        dav_path = self._build_path(directory)
        url = self._get_url(dav_path)
        
        try:
            # Try to create directory with MKCOL
            async with httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=10.0)) as client:
                response = await client.request("MKCOL", url, auth=self._get_auth())
                # 201 Created = success, 405 Method Not Allowed = already exists
                if response.status_code in (201, 405):
                    return True
                elif response.status_code == 409:
                    # Conflict - parent doesn't exist, create it first
                    parent = str(Path(directory).parent)
                    if parent and parent != directory:
                        await self._ensure_directory(parent)
                        # Retry
                        response = await client.request("MKCOL", url, auth=self._get_auth())
                        return response.status_code in (201, 405)
                else:
                    logger.warning(f"[WebDAV] MKCOL returned {response.status_code} for {url}")
                    return False
        except Exception as e:
            logger.debug(f"[WebDAV] Directory may already exist: {url} ({e})")
            return True
    
    async def file_exists(self, file_path: str) -> bool:
        """
        Check if a file exists.
        
        Args:
            file_path: Path relative to user root
        
        Returns:
            True if file exists
        """
        if not self.is_enabled():
            return False
        
        dav_path = self._build_path(file_path)
        url = self._get_url(dav_path)
        
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(10.0, connect=5.0)) as client:
                response = await client.head(url, auth=self._get_auth())
                return response.status_code == 200
        except:
            return False
