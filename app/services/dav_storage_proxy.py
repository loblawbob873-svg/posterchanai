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
    
    def _get_webdav_client(self):
        """Get WebDAV client for user if enabled."""
        from app.services.webdav_storage_client import WebDAVStorageClient
        from app.models import User
        
        user = self.db.query(User).filter(User.username == self.username).first()
        if not user:
            return None
        
        try:
            client = WebDAVStorageClient(self.db, user.id)
            if client.is_enabled():
                return client
        except Exception as e:
            logger.debug(f"[{self.dav_type.upper()}] WebDAV not available for user {self.username}: {e}")
        return None
    
    def _get_local_dav_path(self, subpath: str = "") -> Path:
        """Get local filesystem path for DAV directory (deprecated - use WebDAV instead)."""
        from app.services.storage_service import StorageService
        storage = StorageService(self.db)
        user_path = storage.get_user_path(self.username)
        dav_path = user_path / self.base_path
        if subpath:
            dav_path = dav_path / subpath
        return dav_path
    
    def list_files(self, subpath: str = "") -> List[Dict[str, any]]:
        """List files in DAV directory."""
        if not self.use_proxy:
            # On storage server: Use WebDAV backend (replaces local filesystem)
            webdav_client = self._get_webdav_client()
            if webdav_client:
                try:
                    import asyncio
                    # Build WebDAV path: caldav/subpath or carddav/subpath
                    dav_path = f"{self.base_path}/{subpath}" if subpath else self.base_path
                    dav_path = dav_path.rstrip('/')
                    
                    async def _list_from_webdav():
                        items = await webdav_client.list_files(dav_path, recursive=False)
                        # Convert to expected format
                        # WebDAV returns paths relative to root, but we need paths relative to DAV directory
                        result = []
                        for item in items:
                            # Strip base_path prefix from path (e.g., "caldav/calendar/event.ics" -> "calendar/event.ics")
                            item_path = item["path"]
                            if item_path.startswith(f"{self.base_path}/"):
                                item_path = item_path[len(f"{self.base_path}/"):]
                            elif item_path == self.base_path:
                                item_path = ""
                            
                            result.append({
                                "name": item["name"],
                                "path": item_path,
                                "is_directory": item["is_directory"],
                                "size": item["size"],
                                "modified": item["modified"],
                            })
                        return result
                    
                    # Run async in sync context
                    try:
                        loop = asyncio.get_event_loop()
                        if loop.is_running():
                            import concurrent.futures
                            def _run_in_new_loop():
                                new_loop = asyncio.new_event_loop()
                                asyncio.set_event_loop(new_loop)
                                try:
                                    return new_loop.run_until_complete(_list_from_webdav())
                                finally:
                                    new_loop.close()
                            
                            with concurrent.futures.ThreadPoolExecutor() as executor:
                                future = executor.submit(_run_in_new_loop)
                                return future.result()
                        else:
                            return loop.run_until_complete(_list_from_webdav())
                    except Exception as e:
                        logger.error(f"[{self.dav_type.upper()}] Error listing from WebDAV: {e}", exc_info=True)
                        return []
                except Exception as e:
                    logger.error(f"[{self.dav_type.upper()}] WebDAV not configured - cannot list files: {e}")
                    return []
            
            # WebDAV not configured - log at debug level since this is expected when DAV services aren't configured
            logger.debug(f"[{self.dav_type.upper()}] Storage proxy and WebDAV not configured - cannot list files (this is normal if {self.dav_type} is not enabled)")
            return []
        
        # If same server, use WebDAV backend (replaces local filesystem)
        if self.is_same_server:
            webdav_client = self._get_webdav_client()
            if webdav_client:
                try:
                    import asyncio
                    # Build WebDAV path: caldav/subpath or carddav/subpath
                    dav_path = f"{self.base_path}/{subpath}" if subpath else self.base_path
                    dav_path = dav_path.rstrip('/')
                    
                    async def _list_from_webdav():
                        items = await webdav_client.list_files(dav_path, recursive=False)
                        # Convert to expected format
                        # WebDAV returns paths relative to root, but we need paths relative to DAV directory
                        result = []
                        for item in items:
                            # Strip base_path prefix from path (e.g., "caldav/calendar/event.ics" -> "calendar/event.ics")
                            item_path = item["path"]
                            if item_path.startswith(f"{self.base_path}/"):
                                item_path = item_path[len(f"{self.base_path}/"):]
                            elif item_path == self.base_path:
                                item_path = ""
                            
                            result.append({
                                "name": item["name"],
                                "path": item_path,
                                "is_directory": item["is_directory"],
                                "size": item["size"],
                                "modified": item["modified"],
                            })
                        return result
                    
                    # Run async in sync context
                    try:
                        loop = asyncio.get_event_loop()
                        if loop.is_running():
                            import concurrent.futures
                            def _run_in_new_loop():
                                new_loop = asyncio.new_event_loop()
                                asyncio.set_event_loop(new_loop)
                                try:
                                    return new_loop.run_until_complete(_list_from_webdav())
                                finally:
                                    new_loop.close()
                            
                            with concurrent.futures.ThreadPoolExecutor() as executor:
                                future = executor.submit(_run_in_new_loop)
                                return future.result()
                        else:
                            return loop.run_until_complete(_list_from_webdav())
                    except Exception as e:
                        logger.error(f"[{self.dav_type.upper()}] Error listing from WebDAV: {e}", exc_info=True)
                        return []
                except Exception as e:
                    logger.error(f"[{self.dav_type.upper()}] Error using WebDAV: {e}", exc_info=True)
                    return []
            
            # WebDAV not configured - this is an error on storage server
            logger.error(f"[{self.dav_type.upper()}] WebDAV storage not configured for user {self.username}. CalDAV/CardDAV requires WebDAV storage.")
            return []
        
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
        if not self.use_proxy:
            # On storage server: Use WebDAV backend (replaces local filesystem)
            webdav_client = self._get_webdav_client()
            if webdav_client:
                try:
                    import asyncio
                    # Build WebDAV path: caldav/filepath or carddav/filepath
                    dav_path = f"{self.base_path}/{filepath}".replace('//', '/')
                    
                    async def _read_from_webdav():
                        file_data = await webdav_client.get_file(dav_path)
                        return file_data.decode('utf-8')
                    
                    # Run async in sync context
                    try:
                        loop = asyncio.get_event_loop()
                        if loop.is_running():
                            import concurrent.futures
                            def _run_in_new_loop():
                                new_loop = asyncio.new_event_loop()
                                asyncio.set_event_loop(new_loop)
                                try:
                                    return new_loop.run_until_complete(_read_from_webdav())
                                finally:
                                    new_loop.close()
                            
                            with concurrent.futures.ThreadPoolExecutor() as executor:
                                future = executor.submit(_run_in_new_loop)
                                return future.result()
                        else:
                            return loop.run_until_complete(_read_from_webdav())
                    except FileNotFoundError:
                        logger.warning(f"[{self.dav_type.upper()}] File not found: {filepath}")
                        return None
                    except Exception as e:
                        logger.error(f"[{self.dav_type.upper()}] Error reading from WebDAV: {e}", exc_info=True)
                        return None
                except Exception as e:
                    logger.error(f"[{self.dav_type.upper()}] WebDAV not configured - cannot read file: {e}")
                    return None
            
            # WebDAV not configured
            logger.debug(f"[{self.dav_type.upper()}] Storage proxy and WebDAV not configured - cannot read file: {filepath} (this is normal if {self.dav_type} is not enabled)")
            return None
        
        # If same server, use WebDAV backend (replaces local filesystem)
        if self.is_same_server:
            webdav_client = self._get_webdav_client()
            if webdav_client:
                try:
                    import asyncio
                    # Build WebDAV path: caldav/filepath or carddav/filepath
                    dav_path = f"{self.base_path}/{filepath}".replace('//', '/')
                    
                    async def _read_from_webdav():
                        file_data = await webdav_client.get_file(dav_path)
                        return file_data.decode('utf-8')
                    
                    # Run async in sync context
                    try:
                        loop = asyncio.get_event_loop()
                        if loop.is_running():
                            import concurrent.futures
                            def _run_in_new_loop():
                                new_loop = asyncio.new_event_loop()
                                asyncio.set_event_loop(new_loop)
                                try:
                                    return new_loop.run_until_complete(_read_from_webdav())
                                finally:
                                    new_loop.close()
                            
                            with concurrent.futures.ThreadPoolExecutor() as executor:
                                future = executor.submit(_run_in_new_loop)
                                return future.result()
                        else:
                            return loop.run_until_complete(_read_from_webdav())
                    except FileNotFoundError:
                        logger.warning(f"[{self.dav_type.upper()}] File not found: {filepath}")
                        return None
                    except Exception as e:
                        logger.error(f"[{self.dav_type.upper()}] Error reading from WebDAV: {e}", exc_info=True)
                        return None
                except Exception as e:
                    logger.error(f"[{self.dav_type.upper()}] Error using WebDAV: {e}", exc_info=True)
                    return None
            
            # WebDAV not configured - this is an error on storage server
            logger.error(f"[{self.dav_type.upper()}] WebDAV storage not configured for user {self.username}. CalDAV/CardDAV requires WebDAV storage.")
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
        if not self.use_proxy:
            # On storage server: Use WebDAV backend (replaces local filesystem)
            webdav_client = self._get_webdav_client()
            if webdav_client:
                try:
                    import asyncio
                    # Build WebDAV path: caldav/filepath or carddav/filepath
                    dav_path = f"{self.base_path}/{filepath}".replace('//', '/')
                    
                    async def _write_to_webdav():
                        # Determine content type based on file extension
                        if filepath.endswith('.ics'):
                            content_type = 'text/calendar'
                        elif filepath.endswith('.vcf'):
                            content_type = 'text/vcard'
                        else:
                            content_type = 'text/plain'
                        
                        file_data = content.encode('utf-8')
                        await webdav_client.save_file(dav_path, file_data, content_type)
                        return True
                    
                    # Run async in sync context
                    try:
                        loop = asyncio.get_event_loop()
                        if loop.is_running():
                            import concurrent.futures
                            def _run_in_new_loop():
                                new_loop = asyncio.new_event_loop()
                                asyncio.set_event_loop(new_loop)
                                try:
                                    return new_loop.run_until_complete(_write_to_webdav())
                                finally:
                                    new_loop.close()
                            
                            with concurrent.futures.ThreadPoolExecutor() as executor:
                                future = executor.submit(_run_in_new_loop)
                                success = future.result()
                                if success:
                                    logger.info(f"[{self.dav_type.upper()}] Saved {filepath} to WebDAV")
                                return success
                        else:
                            success = loop.run_until_complete(_write_to_webdav())
                            if success:
                                logger.info(f"[{self.dav_type.upper()}] Saved {filepath} to WebDAV")
                            return success
                    except Exception as e:
                        logger.error(f"[{self.dav_type.upper()}] Error writing to WebDAV: {e}", exc_info=True)
                        return False
                except Exception as e:
                    logger.error(f"[{self.dav_type.upper()}] WebDAV not configured - cannot write file: {e}")
                    return False
            
            # WebDAV not configured
            logger.debug(f"[{self.dav_type.upper()}] Storage proxy and WebDAV not configured - cannot write file: {filepath} (this is normal if {self.dav_type} is not enabled)")
            return False
        
        # If same server, use WebDAV backend (replaces local filesystem)
        if self.is_same_server:
            webdav_client = self._get_webdav_client()
            if webdav_client:
                try:
                    import asyncio
                    # Build WebDAV path: caldav/filepath or carddav/filepath
                    dav_path = f"{self.base_path}/{filepath}".replace('//', '/')
                    
                    async def _write_to_webdav():
                        # Determine content type based on file extension
                        if filepath.endswith('.ics'):
                            content_type = 'text/calendar'
                        elif filepath.endswith('.vcf'):
                            content_type = 'text/vcard'
                        else:
                            content_type = 'text/plain'
                        
                        file_data = content.encode('utf-8')
                        await webdav_client.save_file(dav_path, file_data, content_type)
                        return True
                    
                    # Run async in sync context
                    try:
                        loop = asyncio.get_event_loop()
                        if loop.is_running():
                            import concurrent.futures
                            def _run_in_new_loop():
                                new_loop = asyncio.new_event_loop()
                                asyncio.set_event_loop(new_loop)
                                try:
                                    return new_loop.run_until_complete(_write_to_webdav())
                                finally:
                                    new_loop.close()
                            
                            with concurrent.futures.ThreadPoolExecutor() as executor:
                                future = executor.submit(_run_in_new_loop)
                                success = future.result()
                                if success:
                                    logger.info(f"[{self.dav_type.upper()}] Saved {filepath} to WebDAV")
                                return success
                        else:
                            success = loop.run_until_complete(_write_to_webdav())
                            if success:
                                logger.info(f"[{self.dav_type.upper()}] Saved {filepath} to WebDAV")
                            return success
                    except Exception as e:
                        logger.error(f"[{self.dav_type.upper()}] Error writing to WebDAV: {e}", exc_info=True)
                        return False
                except Exception as e:
                    logger.error(f"[{self.dav_type.upper()}] Error using WebDAV: {e}", exc_info=True)
                    return False
            
            # WebDAV not configured - this is an error on storage server
            logger.error(f"[{self.dav_type.upper()}] WebDAV storage not configured for user {self.username}. CalDAV/CardDAV requires WebDAV storage.")
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
        if not self.use_proxy:
            # On storage server: Use WebDAV backend (replaces local filesystem)
            webdav_client = self._get_webdav_client()
            if webdav_client:
                try:
                    import asyncio
                    # Build WebDAV path: caldav/filepath or carddav/filepath
                    dav_path = f"{self.base_path}/{filepath}".replace('//', '/')
                    
                    async def _delete_from_webdav():
                        return await webdav_client.delete_file(dav_path)
                    
                    # Run async in sync context
                    try:
                        loop = asyncio.get_event_loop()
                        if loop.is_running():
                            import concurrent.futures
                            def _run_in_new_loop():
                                new_loop = asyncio.new_event_loop()
                                asyncio.set_event_loop(new_loop)
                                try:
                                    return new_loop.run_until_complete(_delete_from_webdav())
                                finally:
                                    new_loop.close()
                            
                            with concurrent.futures.ThreadPoolExecutor() as executor:
                                future = executor.submit(_run_in_new_loop)
                                success = future.result()
                                if success:
                                    logger.info(f"[{self.dav_type.upper()}] Deleted {filepath} from WebDAV")
                                return success
                        else:
                            success = loop.run_until_complete(_delete_from_webdav())
                            if success:
                                logger.info(f"[{self.dav_type.upper()}] Deleted {filepath} from WebDAV")
                            return success
                    except FileNotFoundError:
                        logger.warning(f"[{self.dav_type.upper()}] File not found for deletion: {filepath}")
                        return False
                    except Exception as e:
                        logger.error(f"[{self.dav_type.upper()}] Error deleting from WebDAV: {e}", exc_info=True)
                        return False
                except Exception as e:
                    logger.error(f"[{self.dav_type.upper()}] WebDAV not configured - cannot delete file: {e}")
                    return False
            
            # WebDAV not configured
            logger.debug(f"[{self.dav_type.upper()}] Storage proxy and WebDAV not configured - cannot delete file: {filepath} (this is normal if {self.dav_type} is not enabled)")
            return False
        
        # If same server, use WebDAV backend (replaces local filesystem)
        if self.is_same_server:
            webdav_client = self._get_webdav_client()
            if webdav_client:
                try:
                    import asyncio
                    # Build WebDAV path: caldav/filepath or carddav/filepath
                    dav_path = f"{self.base_path}/{filepath}".replace('//', '/')
                    
                    async def _delete_from_webdav():
                        return await webdav_client.delete_file(dav_path)
                    
                    # Run async in sync context
                    try:
                        loop = asyncio.get_event_loop()
                        if loop.is_running():
                            import concurrent.futures
                            def _run_in_new_loop():
                                new_loop = asyncio.new_event_loop()
                                asyncio.set_event_loop(new_loop)
                                try:
                                    return new_loop.run_until_complete(_delete_from_webdav())
                                finally:
                                    new_loop.close()
                            
                            with concurrent.futures.ThreadPoolExecutor() as executor:
                                future = executor.submit(_run_in_new_loop)
                                success = future.result()
                                if success:
                                    logger.info(f"[{self.dav_type.upper()}] Deleted {filepath} from WebDAV")
                                return success
                        else:
                            success = loop.run_until_complete(_delete_from_webdav())
                            if success:
                                logger.info(f"[{self.dav_type.upper()}] Deleted {filepath} from WebDAV")
                            return success
                    except FileNotFoundError:
                        logger.warning(f"[{self.dav_type.upper()}] File not found for deletion: {filepath}")
                        return False
                    except Exception as e:
                        logger.error(f"[{self.dav_type.upper()}] Error deleting from WebDAV: {e}", exc_info=True)
                        return False
                except Exception as e:
                    logger.error(f"[{self.dav_type.upper()}] Error using WebDAV: {e}", exc_info=True)
                    return False
            
            # WebDAV not configured - this is an error on storage server
            logger.error(f"[{self.dav_type.upper()}] WebDAV storage not configured for user {self.username}. CalDAV/CardDAV requires WebDAV storage.")
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
        if not self.use_proxy:
            # On storage server: Use WebDAV backend (replaces local filesystem)
            webdav_client = self._get_webdav_client()
            if webdav_client:
                try:
                    import asyncio
                    # Build WebDAV path: caldav/filepath or carddav/filepath
                    dav_path = f"{self.base_path}/{filepath}".replace('//', '/')
                    
                    async def _check_webdav():
                        return await webdav_client.file_exists(dav_path)
                    
                    # Run async in sync context
                    try:
                        loop = asyncio.get_event_loop()
                        if loop.is_running():
                            import concurrent.futures
                            def _run_in_new_loop():
                                new_loop = asyncio.new_event_loop()
                                asyncio.set_event_loop(new_loop)
                                try:
                                    return new_loop.run_until_complete(_check_webdav())
                                finally:
                                    new_loop.close()
                            
                            with concurrent.futures.ThreadPoolExecutor() as executor:
                                future = executor.submit(_run_in_new_loop)
                                return future.result()
                        else:
                            return loop.run_until_complete(_check_webdav())
                    except Exception:
                        return False
                except Exception:
                    return False
            
            # WebDAV not configured
            logger.debug(f"[{self.dav_type.upper()}] Storage proxy and WebDAV not configured - cannot check file existence: {filepath} (this is normal if {self.dav_type} is not enabled)")
            return False
        
        # If same server, use WebDAV backend (replaces local filesystem)
        if self.is_same_server:
            webdav_client = self._get_webdav_client()
            if webdav_client:
                try:
                    import asyncio
                    # Build WebDAV path: caldav/filepath or carddav/filepath
                    dav_path = f"{self.base_path}/{filepath}".replace('//', '/')
                    
                    async def _check_webdav():
                        return await webdav_client.file_exists(dav_path)
                    
                    # Run async in sync context
                    try:
                        loop = asyncio.get_event_loop()
                        if loop.is_running():
                            import concurrent.futures
                            def _run_in_new_loop():
                                new_loop = asyncio.new_event_loop()
                                asyncio.set_event_loop(new_loop)
                                try:
                                    return new_loop.run_until_complete(_check_webdav())
                                finally:
                                    new_loop.close()
                            
                            with concurrent.futures.ThreadPoolExecutor() as executor:
                                future = executor.submit(_run_in_new_loop)
                                return future.result()
                        else:
                            return loop.run_until_complete(_check_webdav())
                    except Exception:
                        return False
                except Exception:
                    return False
            
            # WebDAV not configured
            return False
        
        # Otherwise, try to read the file - if successful, it exists
        content = self.read_file(filepath)
        return content is not None
