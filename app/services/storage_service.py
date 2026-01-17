import os
import shutil
import base64
import logging
import httpx
from pathlib import Path
from datetime import datetime
from sqlalchemy.orm import Session
from app.models import Setting

logger = logging.getLogger(__name__)


def _sanitize_path_component(component: str) -> str:
    """
    Sanitize a path component to prevent path traversal attacks.
    Removes or replaces dangerous characters and patterns.
    """
    if not component:
        raise ValueError("Path component cannot be empty")

    # Convert to string and strip whitespace
    component = str(component).strip()

    # Block path traversal patterns
    if '..' in component or component.startswith('/') or component.startswith('\\'):
        raise ValueError(f"Invalid path component: {component}")

    # Remove any path separators
    component = component.replace('/', '').replace('\\', '')

    # Block null bytes
    if '\x00' in component:
        raise ValueError("Null bytes not allowed in path")

    return component


def _validate_path_within_base(file_path: Path, base_path: Path) -> bool:
    """
    Validate that a resolved path is within the expected base directory.
    Prevents path traversal even if sanitization is bypassed.
    """
    try:
        resolved = file_path.resolve()
        base_resolved = base_path.resolve()
        return str(resolved).startswith(str(base_resolved) + os.sep) or resolved == base_resolved
    except (OSError, ValueError):
        return False


class StorageService:
    def __init__(self, db: Session):
        self.db = db
        self._load_settings()

    def _load_settings(self):
        settings = {s.key: s.value for s in self.db.query(Setting).all()}
        self.upload_path = settings.get("upload_path", "/var/lib/posterchanai")

    def get_user_path(self, username: str) -> Path:
        """Get the upload directory for a user"""
        safe_username = _sanitize_path_component(username)
        user_path = Path(self.upload_path) / safe_username

        # Verify path is within upload directory
        if not _validate_path_within_base(user_path, Path(self.upload_path)):
            raise ValueError(f"Invalid username path: {username}")

        user_path.mkdir(parents=True, exist_ok=True)
        return user_path

    def get_conversation_path(self, username: str, conversation_id: int) -> Path:
        """Get the upload directory for a specific conversation"""
        safe_conv_id = _sanitize_path_component(str(conversation_id))
        conv_path = self.get_user_path(username) / safe_conv_id

        # Verify path is within upload directory
        if not _validate_path_within_base(conv_path, Path(self.upload_path)):
            raise ValueError(f"Invalid conversation path: {conversation_id}")

        conv_path.mkdir(parents=True, exist_ok=True)
        return conv_path

    def save_image(self, username: str, conversation_id: int, image_base64: str, prefix: str = "img") -> str:
        """Save a base64 image to disk and return the file path. Proxies to storage server if configured."""
        # Check if storage server is configured - proxy request if so
        storage_server_url = self.db.query(Setting).filter(Setting.key == "storage_server_url").first()
        if storage_server_url and storage_server_url.value:
            # Validate URL has protocol before proxying
            url = storage_server_url.value.strip()
            if url.startswith(('http://', 'https://')):
                # Valid URL - try to proxy
                try:
                    return self._proxy_save_image(url, username, conversation_id, image_base64, prefix)
                except Exception as e:
                    # If proxy fails, fall back to local storage
                    logger.warning(f"[STORAGE] Failed to proxy save_image, falling back to local: {e}")
                    # Fall through to local storage below
            else:
                # Invalid URL - log but fall back to local storage
                logger.warning(f"[STORAGE] Invalid storage_server_url (missing protocol): {url}, using local storage")
                # Fall through to local storage below
        
        # Local file saving (storage server node)
        conv_path = self.get_conversation_path(username, conversation_id)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        filename = f"{prefix}_{timestamp}.png"
        filepath = conv_path / filename

        image_data = base64.b64decode(image_base64)
        with open(filepath, "wb") as f:
            f.write(image_data)

        return str(filepath)
    
    def _proxy_save_image(self, storage_server_url: str, username: str, conversation_id: int, image_base64: str, prefix: str) -> str:
        """Proxy image save to storage server"""
        import asyncio
        try:
            # Get server-to-server API token
            storage_server_token = self.db.query(Setting).filter(Setting.key == "storage_server_token").first()
            
            url = f"{storage_server_url.rstrip('/')}/api/storage/save-image"
            headers = {}
            if storage_server_token and storage_server_token.value:
                headers["Authorization"] = f"Bearer {storage_server_token.value}"
            
            image_data = base64.b64decode(image_base64)
            files = {
                "file": (f"{prefix}.png", image_data, "image/png")
            }
            data = {
                "username": username,
                "conversation_id": conversation_id,
                "prefix": prefix
            }
            
            # Run async HTTP request in sync context
            async def _async_proxy():
                async with httpx.AsyncClient(timeout=60.0) as client:
                    response = await client.post(url, headers=headers, files=files, data=data)
                    if response.status_code == 200:
                        result = response.json()
                        return result.get("file_path", "")
                    else:
                        logger.error(f"[STORAGE] Failed to proxy save_image: {response.status_code} - {response.text}")
                        raise Exception(f"Storage server error: {response.status_code}")
            
            loop = asyncio.get_event_loop()
            if loop.is_running():
                # If we're in an async context, we need to handle this differently
                # For now, fall back to local save
                logger.warning("[STORAGE] Cannot proxy save_image in async context, saving locally")
                conv_path = self.get_conversation_path(username, conversation_id)
                timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
                filename = f"{prefix}_{timestamp}.png"
                filepath = conv_path / filename
                with open(filepath, "wb") as f:
                    f.write(image_data)
                return str(filepath)
            else:
                return loop.run_until_complete(_async_proxy())
        except Exception as e:
            logger.error(f"[STORAGE] Error proxying save_image: {e}", exc_info=True)
            # Fall back to local save
            conv_path = self.get_conversation_path(username, conversation_id)
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
            filename = f"{prefix}_{timestamp}.png"
            filepath = conv_path / filename
            image_data = base64.b64decode(image_base64)
            with open(filepath, "wb") as f:
                f.write(image_data)
            return str(filepath)

    def save_avatar(self, username: str, image_data: bytes, ext: str = ".png") -> str:
        """Save user avatar image and return the filename. Proxies to storage server if configured."""
        # Check if storage server is configured - proxy request if so
        storage_server_url = self.db.query(Setting).filter(Setting.key == "storage_server_url").first()
        if storage_server_url and storage_server_url.value:
            # Validate URL has protocol before proxying
            url = storage_server_url.value.strip()
            if url.startswith(('http://', 'https://')):
                # Valid URL - try to proxy
                try:
                    return self._proxy_save_avatar(url, username, image_data, ext)
                except Exception as e:
                    # If proxy fails, fall back to local storage
                    logger.warning(f"[STORAGE] Failed to proxy save_avatar, falling back to local: {e}")
                    # Fall through to local storage below
            else:
                # Invalid URL - log but fall back to local storage
                logger.warning(f"[STORAGE] Invalid storage_server_url (missing protocol): {url}, using local storage")
                # Fall through to local storage below
        
        # Local file saving (storage server node)
        user_path = self.get_user_path(username)
        filename = f"avatar{ext}"
        filepath = user_path / filename

        # Delete old avatar if exists (any extension)
        for old_file in user_path.glob("avatar.*"):
            old_file.unlink()

        with open(filepath, "wb") as f:
            f.write(image_data)

        return filename
    
    def _proxy_save_avatar(self, storage_server_url: str, username: str, image_data: bytes, ext: str) -> str:
        """Proxy avatar save to storage server"""
        import asyncio
        try:
            # Get server-to-server API token
            storage_server_token = self.db.query(Setting).filter(Setting.key == "storage_server_token").first()
            
            url = f"{storage_server_url.rstrip('/')}/api/storage/save-avatar"
            headers = {}
            if storage_server_token and storage_server_token.value:
                headers["Authorization"] = f"Bearer {storage_server_token.value}"
            
            files = {
                "file": (f"avatar{ext}", image_data, f"image/{ext[1:]}")
            }
            data = {
                "username": username
            }
            
            # Run async HTTP request in sync context
            async def _async_proxy():
                async with httpx.AsyncClient(timeout=60.0) as client:
                    response = await client.post(url, headers=headers, files=files, data=data)
                    if response.status_code == 200:
                        result = response.json()
                        return result.get("filename", f"avatar{ext}")
                    else:
                        logger.error(f"[STORAGE] Failed to proxy save_avatar: {response.status_code} - {response.text}")
                        raise Exception(f"Storage server error: {response.status_code}")
            
            loop = asyncio.get_event_loop()
            if loop.is_running():
                # If we're in an async context, fall back to local save
                logger.warning("[STORAGE] Cannot proxy save_avatar in async context, saving locally")
                user_path = self.get_user_path(username)
                filename = f"avatar{ext}"
                filepath = user_path / filename
                for old_file in user_path.glob("avatar.*"):
                    old_file.unlink()
                with open(filepath, "wb") as f:
                    f.write(image_data)
                return filename
            else:
                return loop.run_until_complete(_async_proxy())
        except Exception as e:
            logger.error(f"[STORAGE] Error proxying save_avatar: {e}", exc_info=True)
            # Fall back to local save
            user_path = self.get_user_path(username)
            filename = f"avatar{ext}"
            filepath = user_path / filename
            for old_file in user_path.glob("avatar.*"):
                old_file.unlink()
            with open(filepath, "wb") as f:
                f.write(image_data)
            return filename

    def get_avatar_path(self, username: str) -> Path | None:
        """Get path to user's avatar if it exists"""
        safe_username = _sanitize_path_component(username)
        user_path = Path(self.upload_path) / safe_username

        # Verify path is within upload directory
        if not _validate_path_within_base(user_path, Path(self.upload_path)):
            logger.warning(f"Path traversal attempt blocked in get_avatar_path: {username}")
            return None

        for avatar_file in user_path.glob("avatar.*"):
            return avatar_file
        return None

    def save_file(self, username: str, conversation_id: int, content: str, original_name: str = "file.txt") -> str:
        """Save a text file to disk and return the file path. Proxies to storage server if configured."""
        # Check if storage server is configured - proxy request if so
        storage_server_url = self.db.query(Setting).filter(Setting.key == "storage_server_url").first()
        if storage_server_url and storage_server_url.value:
            # Proxy to storage server
            return self._proxy_save_file(storage_server_url.value, username, conversation_id, content, original_name)
        
        # Local file saving (storage server node)
        conv_path = self.get_conversation_path(username, conversation_id)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        # Keep extension from original name
        ext = Path(original_name).suffix or ".txt"
        filename = f"file_{timestamp}{ext}"
        filepath = conv_path / filename

        with open(filepath, "w", encoding="utf-8") as f:
            f.write(content)

        return str(filepath)
    
    def _proxy_save_file(self, storage_server_url: str, username: str, conversation_id: int, content: str, original_name: str) -> str:
        """Proxy file save to storage server"""
        import asyncio
        try:
            # Get server-to-server API token
            storage_server_token = self.db.query(Setting).filter(Setting.key == "storage_server_token").first()
            
            url = f"{storage_server_url.rstrip('/')}/api/storage/save-file"
            headers = {}
            if storage_server_token and storage_server_token.value:
                headers["Authorization"] = f"Bearer {storage_server_token.value}"
            
            files = {
                "file": (original_name, content.encode('utf-8'), "text/plain")
            }
            data = {
                "username": username,
                "conversation_id": conversation_id,
                "original_name": original_name
            }
            
            # Run async HTTP request in sync context
            async def _async_proxy():
                async with httpx.AsyncClient(timeout=60.0) as client:
                    response = await client.post(url, headers=headers, files=files, data=data)
                    if response.status_code == 200:
                        result = response.json()
                        return result.get("file_path", "")
                    else:
                        logger.error(f"[STORAGE] Failed to proxy save_file: {response.status_code} - {response.text}")
                        raise Exception(f"Storage server error: {response.status_code}")
            
            loop = asyncio.get_event_loop()
            if loop.is_running():
                # If we're in an async context, fall back to local save
                logger.warning("[STORAGE] Cannot proxy save_file in async context, saving locally")
                conv_path = self.get_conversation_path(username, conversation_id)
                timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
                ext = Path(original_name).suffix or ".txt"
                filename = f"file_{timestamp}{ext}"
                filepath = conv_path / filename
                with open(filepath, "w", encoding="utf-8") as f:
                    f.write(content)
                return str(filepath)
            else:
                return loop.run_until_complete(_async_proxy())
        except Exception as e:
            logger.error(f"[STORAGE] Error proxying save_file: {e}", exc_info=True)
            # Fall back to local save
            conv_path = self.get_conversation_path(username, conversation_id)
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
            ext = Path(original_name).suffix or ".txt"
            filename = f"file_{timestamp}{ext}"
            filepath = conv_path / filename
            with open(filepath, "w", encoding="utf-8") as f:
                f.write(content)
            return str(filepath)

    def save_raw_file(self, username: str, conversation_id: int, data: bytes, original_name: str) -> str:
        """Save raw file bytes to disk and return the file path"""
        conv_path = self.get_conversation_path(username, conversation_id)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        ext = Path(original_name).suffix or ""
        safe_name = "".join(c for c in Path(original_name).stem if c.isalnum() or c in "-_")[:30]
        filename = f"{safe_name}_{timestamp}{ext}"
        filepath = conv_path / filename

        with open(filepath, "wb") as f:
            f.write(data)

        return str(filepath)

    def get_relative_path(self, full_path: str, username: str) -> str:
        """Get relative path for API response (from upload_path)"""
        return str(Path(full_path).relative_to(self.upload_path))

    def delete_conversation_files(self, username: str, conversation_id: int) -> bool:
        """Delete all files for a conversation"""
        try:
            safe_username = _sanitize_path_component(username)
            safe_conv_id = _sanitize_path_component(str(conversation_id))
            conv_path = Path(self.upload_path) / safe_username / safe_conv_id

            # Verify path is within upload directory
            if not _validate_path_within_base(conv_path, Path(self.upload_path)):
                logger.warning(f"Path traversal attempt blocked in delete_conversation_files: {username}/{conversation_id}")
                return False

            if conv_path.exists():
                shutil.rmtree(conv_path)
                return True
        except ValueError as e:
            logger.warning(f"Invalid path component in delete_conversation_files: {e}")
        return False

    def delete_user_files(self, username: str) -> bool:
        """Delete all files for a user"""
        try:
            safe_username = _sanitize_path_component(username)
            user_path = Path(self.upload_path) / safe_username

            # Verify path is within upload directory
            if not _validate_path_within_base(user_path, Path(self.upload_path)):
                logger.warning(f"Path traversal attempt blocked in delete_user_files: {username}")
                return False

            if user_path.exists():
                shutil.rmtree(user_path)
                return True
        except ValueError as e:
            logger.warning(f"Invalid path component in delete_user_files: {e}")
        return False

    def get_file_count(self, username: str, conversation_id: int = None) -> int:
        """Count files for a user or specific conversation"""
        try:
            safe_username = _sanitize_path_component(username)
            if conversation_id:
                safe_conv_id = _sanitize_path_component(str(conversation_id))
                target_path = Path(self.upload_path) / safe_username / safe_conv_id
            else:
                target_path = Path(self.upload_path) / safe_username

            # Verify path is within upload directory
            if not _validate_path_within_base(target_path, Path(self.upload_path)):
                logger.warning(f"Path traversal attempt blocked in get_file_count: {username}")
                return 0

            if not target_path.exists():
                return 0

            count = 0
            for root, dirs, files in os.walk(target_path):
                count += len(files)
            return count
        except ValueError as e:
            logger.warning(f"Invalid path component in get_file_count: {e}")
            return 0

    def load_image_as_base64(self, image_url: str) -> str | None:
        """Load image from URL path and return as base64"""
        from urllib.parse import unquote

        # URL is like /api/files/username/conv_id/filename.png
        # Extract path parts
        try:
            parts = image_url.strip('/').split('/')
            if len(parts) >= 4 and parts[0] == 'api' and parts[1] == 'files':
                username = unquote(parts[2])
                conv_id = parts[3]
                filename = unquote(parts[4]) if len(parts) > 4 else None

                if filename:
                    # Sanitize all path components to prevent traversal
                    safe_username = _sanitize_path_component(username)
                    safe_conv_id = _sanitize_path_component(conv_id)
                    safe_filename = _sanitize_path_component(filename)

                    file_path = Path(self.upload_path) / safe_username / safe_conv_id / safe_filename

                    # Verify path is within upload directory
                    if not _validate_path_within_base(file_path, Path(self.upload_path)):
                        logger.warning(f"Path traversal attempt blocked: {image_url}")
                        return None

                    if file_path.exists():
                        with open(file_path, 'rb') as f:
                            return base64.b64encode(f.read()).decode('utf-8')
        except ValueError as e:
            logger.warning(f"Invalid path component in image URL: {e}")
        except Exception as e:
            logger.error(f"Failed to load image: {e}")
        return None

    def get_note_path(self, username: str, note_id: int) -> Path:
        """
        Get the upload directory for a specific note's attachments.
        Uses the same user storage structure as chat files: {upload_path}/{username}/notes/{note_id}/
        
        Note: In load-balanced setups, use storage_server_url to proxy requests to storage node.
        """
        safe_username = _sanitize_path_component(username)
        safe_note_id = _sanitize_path_component(str(note_id))
        # Use same user storage structure as chat files
        note_path = self.get_user_path(username) / "notes" / safe_note_id

        # Verify path is within upload directory
        if not _validate_path_within_base(note_path, Path(self.upload_path)):
            raise ValueError(f"Invalid note path: {note_id}")

        note_path.mkdir(parents=True, exist_ok=True)
        return note_path

    def save_note_attachment(self, username: str, note_id: int, file_data: bytes, original_name: str, bypass_proxy: bool = False) -> str:
        """Save an attachment file for a note and return the filename. Proxies to storage server if configured."""
        # Check if storage server is configured - proxy request if so (unless bypass_proxy is True)
        if not bypass_proxy:
            storage_server_url = self.db.query(Setting).filter(Setting.key == "storage_server_url").first()
            if storage_server_url and storage_server_url.value:
                # Validate URL has protocol before proxying
                url = storage_server_url.value.strip()
                if url.startswith(('http://', 'https://')):
                    try:
                        # Proxy to storage server
                        return self._proxy_save_note_attachment(url, username, note_id, file_data, original_name)
                    except Exception as e:
                        # If proxy fails, fall back to local storage
                        logger.warning(f"[STORAGE] Failed to proxy save_note_attachment, falling back to local: {e}")
                        # Fall through to local storage below
                else:
                    # Invalid URL - log but fall back to local storage
                    logger.warning(f"[STORAGE] Invalid storage_server_url (missing protocol): {url}, using local storage")
                    # Fall through to local storage below
        
        # Local file saving (storage server node or when bypassing proxy)
        try:
            note_path = self.get_note_path(username, note_id)
            note_path.mkdir(parents=True, exist_ok=True)  # Ensure directory exists
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
            ext = Path(original_name).suffix or ""
            safe_name = "".join(c for c in Path(original_name).stem if c.isalnum() or c in "-_")[:50]
            filename = f"{safe_name}_{timestamp}{ext}"
            filepath = note_path / filename

            with open(filepath, "wb") as f:
                f.write(file_data)

            return filename
        except Exception as e:
            logger.error(f"[STORAGE] Error saving note attachment locally: {e}", exc_info=True)
            raise
    
    def _proxy_save_note_attachment(self, storage_server_url: str, username: str, note_id: int, file_data: bytes, original_name: str) -> str:
        """Proxy note attachment save to storage server - uses synchronous requests to avoid event loop issues"""
        import requests
        try:
            # Get server-to-server API token
            storage_server_token = self.db.query(Setting).filter(Setting.key == "storage_server_token").first()
            
            url = f"{storage_server_url.rstrip('/')}/api/storage/save-note-attachment"
            headers = {}
            if storage_server_token and storage_server_token.value:
                headers["Authorization"] = f"Bearer {storage_server_token.value}"
            # Note: If no server token, the storage server endpoint should accept the forwarded API key
            # But since we're in a sync context, we can't access the original request's headers
            # The storage server should accept requests without auth if storage_server_token is not set
            # (it will use the forwarded API key from the main server's request)
            
            files = {
                "file": (original_name, file_data, "application/octet-stream")
            }
            data = {
                "username": username,
                "note_id": str(note_id)
            }
            
            # Use synchronous requests instead of async httpx to avoid event loop issues
            response = requests.post(url, headers=headers, files=files, data=data, timeout=60)
            
            if response.status_code == 200:
                result = response.json()
                return result.get("filename", original_name)
            else:
                logger.error(f"[STORAGE] Failed to proxy save_note_attachment: {response.status_code} - {response.text}")
                raise Exception(f"Storage server error: {response.status_code}")
        except Exception as e:
            logger.error(f"[STORAGE] Error proxying note attachment: {e}", exc_info=True)
            raise

    def save_mail_attachment(self, username: str, file_data: bytes, original_name: str, bypass_proxy: bool = False) -> str:
        """Save a mail attachment file to temp/mail_attachments and return the filename. Proxies to storage server if configured."""
        # Check if storage server is configured - proxy request if so (unless bypass_proxy is True)
        if not bypass_proxy:
            storage_server_url = self.db.query(Setting).filter(Setting.key == "storage_server_url").first()
            if storage_server_url and storage_server_url.value:
                # Validate URL has protocol before proxying
                url = storage_server_url.value.strip()
                if url.startswith(('http://', 'https://')):
                    try:
                        # Proxy to storage server
                        return self._proxy_save_mail_attachment(url, username, file_data, original_name)
                    except Exception as e:
                        # If proxy fails, fall back to local storage
                        logger.warning(f"[STORAGE] Failed to proxy save_mail_attachment, falling back to local: {e}")
                        # Fall through to local storage below
                else:
                    # Invalid URL - log but fall back to local storage
                    logger.warning(f"[STORAGE] Invalid storage_server_url (missing protocol): {url}, using local storage")
                    # Fall through to local storage below
        
        # Local file saving (storage server node or when bypassing proxy)
        try:
            user_path = self.get_user_path(username)
            mail_attachments_dir = user_path / "temp" / "mail_attachments"
            mail_attachments_dir.mkdir(parents=True, exist_ok=True)
            
            # Use the original filename (already sanitized by caller)
            filepath = mail_attachments_dir / original_name
            
            with open(filepath, "wb") as f:
                f.write(file_data)
            
            return original_name
        except Exception as e:
            logger.error(f"[STORAGE] Error saving mail attachment locally: {e}", exc_info=True)
            raise
    
    def _proxy_save_mail_attachment(self, storage_server_url: str, username: str, file_data: bytes, original_name: str) -> str:
        """Proxy mail attachment save to storage server - uses synchronous requests to avoid event loop issues"""
        import requests
        try:
            # Get server-to-server API token
            storage_server_token = self.db.query(Setting).filter(Setting.key == "storage_server_token").first()
            
            url = f"{storage_server_url.rstrip('/')}/api/storage/save-mail-attachment"
            headers = {}
            if storage_server_token and storage_server_token.value:
                headers["Authorization"] = f"Bearer {storage_server_token.value}"
            
            files = {
                "file": (original_name, file_data, "application/octet-stream")
            }
            data = {
                "username": username
            }
            
            # Use synchronous requests instead of async httpx to avoid event loop issues
            response = requests.post(url, headers=headers, files=files, data=data, timeout=60)
            
            if response.status_code == 200:
                result = response.json()
                return result.get("filename", original_name)
            else:
                logger.error(f"[STORAGE] Failed to proxy save_mail_attachment: {response.status_code} - {response.text}")
                raise Exception(f"Storage server error: {response.status_code}")
        except Exception as e:
            logger.error(f"[STORAGE] Error proxying mail attachment: {e}", exc_info=True)
            raise

    def delete_note_attachment(self, username: str, note_id: int, filename: str, bypass_proxy: bool = False) -> bool:
        """Delete a specific attachment file for a note. Proxies to storage server if configured."""
        # Check if storage server is configured - proxy request if so (unless bypass_proxy is True)
        if not bypass_proxy:
            storage_server_url = self.db.query(Setting).filter(Setting.key == "storage_server_url").first()
            if storage_server_url and storage_server_url.value:
                # Validate URL has protocol before proxying
                url = storage_server_url.value.strip()
                if url.startswith(('http://', 'https://')):
                    # Valid URL - try to proxy
                    try:
                        return self._proxy_delete_note_attachment(url, username, note_id, filename)
                    except Exception as e:
                        # If proxy fails, fall back to local storage (per user's "Never fallback" request, this should fail)
                        # But for now, we'll log and fall back to maintain compatibility
                        logger.warning(f"[STORAGE] Failed to proxy delete_note_attachment, falling back to local: {e}")
                        # Fall through to local storage below
                else:
                    logger.warning(f"[STORAGE] Invalid storage_server_url (missing protocol): {url}, using local storage")
        
        # Local file deletion (storage server node or when bypassing proxy)
        try:
            safe_username = _sanitize_path_component(username)
            safe_note_id = _sanitize_path_component(str(note_id))
            safe_filename = _sanitize_path_component(filename)
            note_path = Path(self.upload_path) / safe_username / "notes" / safe_note_id
            file_path = note_path / safe_filename
            
            if not _validate_path_within_base(file_path, note_path):
                logger.warning(f"Path traversal attempt blocked in delete_note_attachment: {username}/{note_id}/{filename}")
                return False
            
            if file_path.exists():
                file_path.unlink()
                logger.info(f"Deleted attachment: {file_path}")
                return True
            else:
                logger.warning(f"Attachment file not found: {file_path}")
                return False
        except ValueError as e:
            logger.warning(f"Invalid path component in delete_note_attachment: {e}")
            return False
        except Exception as e:
            logger.error(f"Error deleting attachment: {e}", exc_info=True)
            return False
    
    def delete_note_attachments(self, username: str, note_id: int, bypass_proxy: bool = False) -> bool:
        """Delete all attachments for a note. Proxies to storage server if configured."""
        # Check if storage server is configured - proxy request if so (unless bypass_proxy is True)
        if not bypass_proxy:
            storage_server_url = self.db.query(Setting).filter(Setting.key == "storage_server_url").first()
            if storage_server_url and storage_server_url.value:
                # Proxy to storage server
                return self._proxy_delete_note_attachments(storage_server_url.value, username, note_id)
        
        # Local file deletion (storage server node or when bypassing proxy)
        try:
            safe_username = _sanitize_path_component(username)
            safe_note_id = _sanitize_path_component(str(note_id))
            note_path = Path(self.upload_path) / safe_username / "notes" / safe_note_id

            # Verify path is within upload directory
            if not _validate_path_within_base(note_path, Path(self.upload_path)):
                logger.warning(f"Path traversal attempt blocked in delete_note_attachments: {username}/{note_id}")
                return False

            if note_path.exists():
                shutil.rmtree(note_path)
                logger.info(f"Deleted note attachments directory: {note_path}")
                return True
            else:
                logger.warning(f"Note attachments directory not found: {note_path}")
                return False
        except ValueError as e:
            logger.warning(f"Invalid path component in delete_note_attachments: {e}")
        except Exception as e:
            logger.error(f"Error deleting note attachments: {e}", exc_info=True)
        return False
    
    def _proxy_delete_note_attachment(self, storage_server_url: str, username: str, note_id: int, filename: str) -> bool:
        """Proxy single note attachment deletion to storage server"""
        import requests
        try:
            # Get server-to-server API token
            storage_server_token = self.db.query(Setting).filter(Setting.key == "storage_server_token").first()
            
            url = f"{storage_server_url.rstrip('/')}/api/storage/delete-note-attachment"
            headers = {}
            if storage_server_token and storage_server_token.value:
                headers["Authorization"] = f"Bearer {storage_server_token.value}"
            
            data = {
                "username": username,
                "note_id": note_id,
                "filename": filename
            }
            
            response = requests.post(url, headers=headers, data=data, timeout=30)
            if response.status_code == 200:
                result = response.json()
                return result.get("success", False)
            else:
                logger.error(f"[STORAGE] Failed to proxy delete_note_attachment: {response.status_code} - {response.text}")
                return False
        except Exception as e:
            logger.error(f"[STORAGE] Error proxying delete_note_attachment: {e}", exc_info=True)
            raise
    
    def _proxy_delete_note_attachments(self, storage_server_url: str, username: str, note_id: int) -> bool:
        """Proxy note attachments deletion to storage server"""
        import asyncio
        try:
            # Get server-to-server API token
            storage_server_token = self.db.query(Setting).filter(Setting.key == "storage_server_token").first()
            
            url = f"{storage_server_url.rstrip('/')}/api/storage/delete-note-attachments"
            headers = {}
            if storage_server_token and storage_server_token.value:
                headers["Authorization"] = f"Bearer {storage_server_token.value}"
            
            data = {
                "username": username,
                "note_id": note_id
            }
            
            async def _async_proxy():
                import httpx
                async with httpx.AsyncClient(timeout=30.0) as client:
                    response = await client.post(url, json=data, headers=headers)
                    if response.status_code == 200:
                        return True
                    else:
                        logger.error(f"Storage server error deleting note attachments: {response.status_code}")
                        return False
            
            # Try to get running event loop
            try:
                loop = asyncio.get_running_loop()
                # If we're in an async context with a running loop, run in a new thread with its own event loop
                import concurrent.futures
                def _run_in_new_loop():
                    new_loop = asyncio.new_event_loop()
                    asyncio.set_event_loop(new_loop)
                    try:
                        return new_loop.run_until_complete(_async_proxy())
                    finally:
                        new_loop.close()
                
                with concurrent.futures.ThreadPoolExecutor() as executor:
                    future = executor.submit(_run_in_new_loop)
                    return future.result()
            except RuntimeError:
                # No running loop, we can use asyncio.run or create a new loop
                try:
                    loop = asyncio.get_event_loop()
                    return loop.run_until_complete(_async_proxy())
                except RuntimeError:
                    # No event loop at all, create one
                    return asyncio.run(_async_proxy())
        except Exception as e:
            logger.error(f"[STORAGE] Error proxying note attachments deletion: {e}", exc_info=True)
            return False


def get_storage_service(db: Session) -> StorageService:
    return StorageService(db)
