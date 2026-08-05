import os
import shutil
import base64
import logging
import httpx
from app.utils import lb_auth
import socket
from pathlib import Path
from datetime import datetime
from urllib.parse import urlparse
from sqlalchemy.orm import Session
from app.services import settings_store

logger = logging.getLogger(__name__)


def _is_same_machine_url(url: str, current_port: int = None) -> bool:
    """
    Check if a storage server URL points to the same machine.
    This prevents proxying to localhost when storage should be on a different machine.
    """
    try:
        parsed = urlparse(url)
        host = parsed.hostname
        if not host:
            return False
        
        # Get local IPs and hostnames
        local_ips = {'127.0.0.1', 'localhost', '0.0.0.0', '::1'}
        try:
            hostname = socket.gethostname()
            local_ips.add(hostname.lower())
            local_ips.add(socket.gethostbyname(hostname))
            # Get all IP addresses for this host
            for info in socket.getaddrinfo(hostname, None):
                ip = info[4][0]
                if ip and not ip.startswith('::'):
                    local_ips.add(ip)
        except Exception:
            pass
        
        # Check if host is a local IP/hostname
        is_local = host.lower() in local_ips or host in local_ips
        
        # If current_port is provided, also check if port matches
        if current_port and is_local:
            port = parsed.port or (443 if parsed.scheme == 'https' else 80)
            if port == current_port:
                logger.warning(f"[STORAGE] storage_server_url points to same machine and port: {url}")
                return True
        
        # If it's localhost/127.0.0.1, it's the same machine regardless of port
        if is_local and host.lower() in ('localhost', '127.0.0.1', '::1'):
            logger.warning(f"[STORAGE] storage_server_url points to same machine (localhost): {url}")
            return True
        
        return False
    except Exception as e:
        logger.warning(f"[STORAGE] Error checking if URL is same machine: {url}, error: {e}")
        return False


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


def ascii_safe_header_filename(name: str) -> str:
    """
    Return a filename safe for HTTP headers (Content-Disposition).
    Headers are often encoded as latin-1; Unicode (e.g. U+2019) causes 500.
    """
    if not name or not name.strip():
        return "download"
    out = []
    for c in str(name).strip():
        out.append(c if ord(c) < 128 else "_")
    return "".join(out) or "download"


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
    def __init__(self, db: Session, user_id: int = None):
        self.db = db
        self.user_id = user_id
        self._load_settings()

    def _load_settings(self):
        from app.database import safe_query_settings
        settings = safe_query_settings(self.db)
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
        """Get the upload directory for a specific conversation (in chat subfolder)"""
        safe_conv_id = _sanitize_path_component(str(conversation_id))
        # Store chat attachments in username/chat/conversation_id/
        conv_path = self.get_user_path(username) / "chat" / safe_conv_id

        # Verify path is within upload directory
        if not _validate_path_within_base(conv_path, Path(self.upload_path)):
            raise ValueError(f"Invalid conversation path: {conversation_id}")

        conv_path.mkdir(parents=True, exist_ok=True)
        return conv_path

    def save_image(self, username: str, conversation_id: int, image_base64: str, prefix: str = "img", bypass_proxy: bool = False) -> str:
        """Save a base64 image. Proxies to storage server if configured, otherwise saves to local disk."""
        # Check if storage server is configured - proxy request if so (unless bypass_proxy is True)
        if not bypass_proxy:
            storage_server_url = settings_store.get("storage_server_url", "")
            if storage_server_url:
                # Validate URL has protocol before proxying
                url = storage_server_url.strip()
                if url.startswith(('http://', 'https://')):
                    # Always proxy when storage_server_url is set - no local fallback
                    try:
                        return self._proxy_save_image(url, username, conversation_id, image_base64, prefix)
                    except Exception as e:
                        # If proxy fails, raise error instead of silently falling back
                        logger.error(f"[STORAGE] Failed to proxy save_image to {url}: {e}")
                        raise Exception(f"Failed to save image to storage server: {e}")
                else:
                    # Invalid URL - raise error
                    raise ValueError(f"Invalid storage_server_url (missing protocol): {url}")
        
        # Local file saving (only on storage server node when bypass_proxy=True)
        try:
            conv_path = self.get_conversation_path(username, conversation_id)
            conv_path.mkdir(parents=True, exist_ok=True)  # Ensure directory exists
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
            filename = f"{prefix}_{timestamp}.png"
            filepath = conv_path / filename

            image_data = base64.b64decode(image_base64)
            with open(filepath, "wb") as f:
                f.write(image_data)

            return str(filepath.relative_to(Path(self.upload_path)))
        except Exception as e:
            logger.error(f"[STORAGE] Error saving image locally: {e}", exc_info=True)
            raise
    
    def _proxy_save_image(self, storage_server_url: str, username: str, conversation_id: int, image_base64: str, prefix: str) -> str:
        """Proxy image save to storage server"""
        import asyncio
        try:
            # Server-to-server requests don't need authentication
            url = f"{storage_server_url.rstrip('/')}/api/storage/save-image"
            headers = lb_auth.headers()
            
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
                # Use asyncio.create_task or run in executor
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
            else:
                return loop.run_until_complete(_async_proxy())
        except Exception as e:
            logger.error(f"[STORAGE] Error proxying save_image: {e}", exc_info=True)
            raise

    def save_avatar(self, username: str, image_data: bytes, ext: str = ".png", bypass_proxy: bool = False) -> str:
        """Save user avatar image and return the filename. Proxies to storage server if configured, otherwise saves to local disk."""
        # Check if storage server is configured - proxy request if so (unless bypass_proxy is True)
        if not bypass_proxy:
            storage_server_url = settings_store.get("storage_server_url", "")
            if storage_server_url:
                # Validate URL has protocol before proxying
                url = storage_server_url.strip()
                if url.startswith(('http://', 'https://')):
                    # Always proxy when storage_server_url is set - no local fallback
                    try:
                        return self._proxy_save_avatar(url, username, image_data, ext)
                    except Exception as e:
                        # If proxy fails, raise error instead of silently falling back
                        logger.error(f"[STORAGE] Failed to proxy save_avatar to {url}: {e}")
                        raise Exception(f"Failed to save avatar to storage server: {e}")
                else:
                    # Invalid URL - raise error
                    raise ValueError(f"Invalid storage_server_url (missing protocol): {url}")
        
        # Local file saving (only on storage server node when bypass_proxy=True)
        try:
            user_path = self.get_user_path(username)
            user_path.mkdir(parents=True, exist_ok=True)  # Ensure directory exists
            
            # Delete old avatar files with different extensions
            for old_ext in ['.png', '.jpg', '.jpeg', '.gif', '.webp']:
                old_avatar = user_path / f"avatar{old_ext}"
                if old_avatar.exists():
                    old_avatar.unlink()
            
            # Save new avatar
            avatar_path = user_path / f"avatar{ext}"
            with open(avatar_path, "wb") as f:
                f.write(image_data)
            
            return f"avatar{ext}"  # Return just filename for compatibility
        except Exception as e:
            logger.error(f"[STORAGE] Error saving avatar locally: {e}", exc_info=True)
            raise
    
    def _proxy_save_avatar(self, storage_server_url: str, username: str, image_data: bytes, ext: str) -> str:
        """Proxy avatar save to storage server"""
        import asyncio
        try:
            # Server-to-server requests don't need authentication
            url = f"{storage_server_url.rstrip('/')}/api/storage/save-avatar"
            headers = lb_auth.headers()
            
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
                # If we're in an async context, use executor
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
            else:
                return loop.run_until_complete(_async_proxy())
        except Exception as e:
            logger.error(f"[STORAGE] Error proxying save_avatar: {e}", exc_info=True)
            raise

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

    def save_file(self, username: str, conversation_id: int, content: str, original_name: str = "file.txt", bypass_proxy: bool = False) -> str:
        """Save a text file. Proxies to storage server if configured, otherwise saves to local disk."""
        # Check if storage server is configured - proxy request if so (unless bypass_proxy is True)
        if not bypass_proxy:
            storage_server_url = settings_store.get("storage_server_url", "")
            if storage_server_url:
                url = storage_server_url.strip()
                # Validate URL has protocol before proxying
                if url.startswith(('http://', 'https://')):
                    # Always proxy when storage_server_url is set - no local fallback
                    try:
                        return self._proxy_save_file(url, username, conversation_id, content, original_name)
                    except Exception as e:
                        # If proxy fails, raise error instead of silently falling back
                        logger.error(f"[STORAGE] Failed to proxy save_file to {url}: {e}")
                        raise Exception(f"Failed to save file to storage server: {e}")
                else:
                    # Invalid URL - raise error
                    raise ValueError(f"Invalid storage_server_url (missing protocol): {url}")
        
        # Local file saving (only on storage server node when bypass_proxy=True)
        try:
            conv_path = self.get_conversation_path(username, conversation_id)
            conv_path.mkdir(parents=True, exist_ok=True)  # Ensure directory exists
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
            ext = Path(original_name).suffix or ".txt"
            filename = f"file_{timestamp}{ext}"
            filepath = conv_path / filename

            with open(filepath, "w", encoding="utf-8") as f:
                f.write(content)

            return str(filepath.relative_to(Path(self.upload_path)))
        except Exception as e:
            logger.error(f"[STORAGE] Error saving file locally: {e}", exc_info=True)
            raise

    def save_file_bytes(self, username: str, conversation_id: int, data: bytes, original_name: str = "file.bin") -> str:
        """Save binary file locally. Returns path relative to upload_path."""
        try:
            from datetime import datetime
            conv_path = self.get_conversation_path(username, conversation_id)
            conv_path.mkdir(parents=True, exist_ok=True)
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
            ext = Path(original_name).suffix or ".bin"
            stem = Path(original_name).stem
            filename = f"{stem}_{timestamp}{ext}"
            filepath = conv_path / filename
            with open(filepath, "wb") as f:
                f.write(data)
            return str(filepath.relative_to(Path(self.upload_path)))
        except Exception as e:
            logger.error(f"[STORAGE] Error saving binary file locally: {e}", exc_info=True)
            raise


    def _proxy_save_file(self, storage_server_url: str, username: str, conversation_id: int, content: str, original_name: str) -> str:
        """Proxy file save to storage server"""
        import asyncio
        try:
            # Server-to-server requests don't need authentication
            url = f"{storage_server_url.rstrip('/')}/api/storage/save-file"
            headers = lb_auth.headers()
            
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
                # If we're in an async context, use executor
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
            else:
                return loop.run_until_complete(_async_proxy())
        except Exception as e:
            logger.error(f"[STORAGE] Error proxying save_file: {e}", exc_info=True)
            raise

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
            # Use get_conversation_path to ensure correct path structure (username/chat/conversation_id)
            conv_path = self.get_conversation_path(username, conversation_id)

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
            if conversation_id:
                # Use get_conversation_path for conversation-specific count
                target_path = self.get_conversation_path(username, conversation_id)
            else:
                # Count all files in user directory
                safe_username = _sanitize_path_component(username)
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
        """Load image from URL path and return as base64. Uses storage proxy if configured."""
        from urllib.parse import unquote, quote
        import re

        # Log the input to help debug emoji issues
        if image_url:
            logger.debug(f"[STORAGE] load_image_as_base64 called with image_url: {repr(image_url)} (length={len(image_url)})")

        # Sanitize image_url to remove emojis and other non-URL-safe characters
        def sanitize_url_path(path: str) -> str:
            """Remove emojis and invalid URL characters from path"""
            if not path:
                return path
            # Remove emojis and other non-ASCII characters
            # Keep only ASCII printable characters, forward slashes, and URL-encoded sequences (%XX)
            # First, preserve URL-encoded sequences
            parts = []
            i = 0
            while i < len(path):
                if path[i] == '%' and i + 2 < len(path) and path[i+1:i+3].isalnum():
                    # Preserve URL-encoded sequences
                    parts.append(path[i:i+3])
                    i += 3
                elif ord(path[i]) < 128 and (path[i].isprintable() or path[i] == '/'):
                    # Keep ASCII printable characters and forward slashes
                    parts.append(path[i])
                    i += 1
                else:
                    # Skip emojis and other non-ASCII characters
                    i += 1
            sanitized = ''.join(parts)
            return sanitized.strip()

        # Check if storage server is configured - proxy request if so
        storage_server_url = settings_store.get("storage_server_url", "")
        if storage_server_url:
            url = storage_server_url.strip()
            if url.startswith(('http://', 'https://')):
                try:
                    # Proxy the file request to storage server
                    import httpx
                    headers = lb_auth.headers()
                    
                    # If image_url is already a full URL, use it directly (but still sanitize)
                    if image_url.startswith(('http://', 'https://')):
                        # Already a full URL - sanitize and use as-is
                        sanitized_image_url = sanitize_url_path(image_url)
                        if not sanitized_image_url or not sanitized_image_url.startswith(('http://', 'https://')):
                            logger.warning(f"[STORAGE PROXY] Invalid full URL after sanitization: {image_url} -> {sanitized_image_url}")
                            return None
                        file_url = sanitized_image_url
                        if file_url != image_url:
                            logger.warning(f"[STORAGE PROXY] Sanitized URL changed: {image_url} -> {file_url}")
                    else:
                        # Relative path - sanitize and append to storage server URL
                        sanitized_image_url = sanitize_url_path(image_url)
                        if not sanitized_image_url:
                            logger.warning(f"[STORAGE PROXY] Invalid image_url after sanitization (empty result): {image_url}")
                            return None
                        if sanitized_image_url != image_url:
                            logger.warning(f"[STORAGE PROXY] Sanitized image_url (removed invalid characters): {image_url} -> {sanitized_image_url}")
                        # Ensure the path starts with / if it's a relative path
                        if not sanitized_image_url.startswith('/'):
                            sanitized_image_url = '/' + sanitized_image_url
                        file_url = f"{url.rstrip('/')}{sanitized_image_url}"
                    
                    logger.info(f"[STORAGE PROXY] Loading image from: {file_url}")
                    
                    # Validate URL before making request
                    try:
                        from urllib.parse import urlparse
                        parsed = urlparse(file_url)
                        if not parsed.scheme or not parsed.netloc:
                            logger.error(f"[STORAGE PROXY] Invalid URL structure: {file_url}")
                            return None
                        # Check for invalid characters in the URL
                        if any(ord(c) > 127 for c in file_url if c not in '%'):
                            logger.error(f"[STORAGE PROXY] URL contains non-ASCII characters: {file_url}")
                            return None
                    except Exception as url_err:
                        logger.error(f"[STORAGE PROXY] URL validation error for {file_url}: {url_err}")
                        return None
                    
                    with httpx.Client(timeout=30.0) as client:
                        response = client.get(file_url, headers=headers)
                        if response.status_code == 200:
                            # Convert to base64
                            return base64.b64encode(response.content).decode('utf-8')
                        else:
                            # Log warning instead of error - this is expected if file doesn't exist or server is down
                            logger.warning(f"[STORAGE PROXY] Failed to load image from {file_url}: HTTP {response.status_code}")
                            # Don't log response body as it might be large
                            return None
                except httpx.HTTPStatusError as e:
                    # Handle HTTP errors gracefully
                    logger.warning(f"[STORAGE PROXY] HTTP error loading image from storage server: {e.response.status_code}")
                    return None
                except httpx.RequestError as e:
                    # Handle connection/timeout errors gracefully
                    logger.warning(f"[STORAGE PROXY] Connection error loading image from storage server: {e}")
                    return None
                except Exception as e:
                    # Catch any other exceptions to prevent 500 errors
                    logger.warning(f"[STORAGE PROXY] Error loading image from storage server: {e}")
                    return None

        # On storage server: Use local filesystem
        # URL is like /api/files/username/conv_id/filename.png
        # Extract path parts
        try:
            parts = image_url.strip('/').split('/')
            if len(parts) >= 4 and parts[0] == 'api' and parts[1] == 'files':
                username = unquote(parts[2])
                conv_id = int(parts[3])
                filename = unquote(parts[4]) if len(parts) > 4 else None

                if filename:
                    # Load from local filesystem
                    # Use get_conversation_path to get correct path structure
                    conv_path = self.get_conversation_path(username, conv_id)
                    safe_filename = _sanitize_path_component(filename)
                    file_path = conv_path / safe_filename

                    # Verify path is within upload directory (already checked by get_conversation_path)
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

    def save_mail_attachment(self, username: str, file_data: bytes, original_name: str, bypass_proxy: bool = False) -> str:
        """Save a mail attachment file to Mail Attachments folder and return the filename. Proxies to storage server if configured."""
        # Check if storage server is configured - proxy request if so (unless bypass_proxy is True)
        if not bypass_proxy:
            storage_server_url = settings_store.get("storage_server_url", "")
            if storage_server_url:
                # Validate URL has protocol before proxying
                url = storage_server_url.strip()
                if url.startswith(('http://', 'https://')):
                    # Check if URL points to same machine - this is likely a misconfiguration
                    if _is_same_machine_url(url):
                        logger.error(f"[STORAGE] storage_server_url points to same machine: {url}. This will cause files to be saved locally. Please use a different machine's URL or leave storage_server_url empty for local storage.")
                    try:
                        # Proxy to storage server
                        return self._proxy_save_mail_attachment(url, username, file_data, original_name)
                    except Exception as e:
                        # If proxy fails, raise error instead of silently falling back
                        logger.error(f"[STORAGE] Failed to proxy save_mail_attachment to {url}: {e}")
                        raise Exception(f"Failed to save mail attachment to storage server: {e}")
                else:
                    # Invalid URL - raise error
                    raise ValueError(f"Invalid storage_server_url (missing protocol): {url}")
        
        # Local file saving (storage server node or when bypassing proxy)
        try:
            user_path = self.get_user_path(username)
            # Save to "Mail Attachments" folder instead of temp/mail_attachments so it shows up in file manager
            mail_attachments_dir = user_path / "Mail Attachments"
            mail_attachments_dir.mkdir(parents=True, exist_ok=True)
            
            # Use the original filename (already sanitized by caller)
            filepath = mail_attachments_dir / original_name
            
            with open(filepath, "wb") as f:
                f.write(file_data)
            
            logger.info(f"[STORAGE] Saved mail attachment to: {filepath.relative_to(user_path)}")
            return str(filepath.relative_to(user_path))
        except Exception as e:
            logger.error(f"[STORAGE] Error saving mail attachment locally: {e}", exc_info=True)
            raise
    
    def _proxy_save_mail_attachment(self, storage_server_url: str, username: str, file_data: bytes, original_name: str) -> str:
        """Proxy mail attachment save to storage server - uses synchronous requests to avoid event loop issues"""
        import requests
        try:
            # Get server-to-server API token
            # Server-to-server requests don't need authentication
            url = f"{storage_server_url.rstrip('/')}/api/storage/save-mail-attachment"
            headers = lb_auth.headers()
            
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
                # Result now returns relative path like "Mail Attachments/filename.ext"
                return result.get("filename", original_name)
            else:
                logger.error(f"[STORAGE] Failed to proxy save_mail_attachment: {response.status_code} - {response.text}")
                raise Exception(f"Storage server error: {response.status_code}")
        except Exception as e:
            logger.error(f"[STORAGE] Error proxying mail attachment: {e}", exc_info=True)
            raise
    
    def save_generated_image(self, username: str, image_base64: str, prompt: str = "", bypass_proxy: bool = False) -> str:
        """Save a generated image to Generated Images folder. Proxies to storage server if configured."""
        # Check if storage server is configured - proxy request if so (unless bypass_proxy is True)
        if not bypass_proxy:
            storage_server_url = settings_store.get("storage_server_url", "")
            if storage_server_url:
                # Validate URL has protocol before proxying
                url = storage_server_url.strip()
                if url.startswith(('http://', 'https://')):
                    try:
                        return self._proxy_save_generated_image(url, username, image_base64, prompt)
                    except Exception as e:
                        logger.error(f"[STORAGE] Failed to proxy save_generated_image to {url}: {e}")
                        raise Exception(f"Failed to save generated image to storage server: {e}")
                else:
                    raise ValueError(f"Invalid storage_server_url (missing protocol): {url}")
        
        # Local file saving (storage server node or when bypassing proxy)
        try:
            user_path = self.get_user_path(username)
            generated_images_dir = user_path / "Generated Images"
            generated_images_dir.mkdir(parents=True, exist_ok=True)
            
            # Create filename with timestamp and sanitized prompt
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
            if prompt:
                # Sanitize prompt for filename (first 50 chars, alphanumeric + spaces/underscores only)
                safe_prompt = "".join(c if c.isalnum() or c in (' ', '_', '-') else '_' for c in prompt[:50])
                safe_prompt = safe_prompt.strip().replace(' ', '_')
                filename = f"generated_{timestamp}_{safe_prompt}.png"
            else:
                filename = f"generated_{timestamp}.png"
            
            filepath = generated_images_dir / filename
            
            image_data = base64.b64decode(image_base64)
            with open(filepath, "wb") as f:
                f.write(image_data)
            
            logger.info(f"[STORAGE] Saved generated image to: {filepath.relative_to(user_path)}")
            return str(filepath.relative_to(user_path))
        except Exception as e:
            logger.error(f"[STORAGE] Error saving generated image locally: {e}", exc_info=True)
            raise
    
    def _proxy_save_generated_image(self, storage_server_url: str, username: str, image_base64: str, prompt: str = "") -> str:
        """Proxy generated image save to storage server - uses synchronous requests to avoid event loop issues"""
        import requests
        try:
            # Server-to-server requests don't need authentication
            url = f"{storage_server_url.rstrip('/')}/api/storage/save-generated-image"
            headers = lb_auth.headers()
            
            # Use form data (not JSON) to match the endpoint signature
            data = {
                "username": username,
                "image_base64": image_base64,
                "prompt": prompt or ""
            }
            
            response = requests.post(url, headers=headers, data=data, timeout=60)
            
            if response.status_code == 200:
                result = response.json()
                return result.get("filename", "")
            else:
                logger.error(f"[STORAGE] Failed to proxy save_generated_image: {response.status_code} - {response.text}")
                raise Exception(f"Storage server error: {response.status_code}")
        except Exception as e:
            logger.error(f"[STORAGE] Error proxying generated image: {e}", exc_info=True)
            raise

    # Note attachment methods removed - notes feature was removed


def get_storage_service(db: Session) -> StorageService:
    return StorageService(db)
