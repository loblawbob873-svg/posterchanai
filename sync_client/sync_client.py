#!/usr/bin/env python3
"""
PosterchanAI Sync Client - Desktop sync daemon with system tray GUI
Syncs local directories with remote PosterchanAI storage
"""
import os
import sys
import json
import logging
import hashlib
import time
import threading
from pathlib import Path
from datetime import datetime
from typing import Dict, List, Optional, Set
from dataclasses import dataclass, asdict
from queue import Queue
import requests
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler, FileSystemEvent
import pystray
from PIL import Image, ImageDraw, ImageFont

# Optional GUI imports - only needed for interactive mode
try:
    import tkinter as tk
    from tkinter import scrolledtext, messagebox
    import tkinter.ttk as ttk
    GUI_AVAILABLE = True
except ImportError:
    GUI_AVAILABLE = False

# Wayland support via StatusNotifierItem (PyGObject)
WAYLAND_TRAY_AVAILABLE = False
try:
    from gi.repository import Gio, GLib
    WAYLAND_TRAY_AVAILABLE = True
except ImportError:
    pass
    # Create dummy classes to avoid NameError
    class tk:
        class Tk:
            def __init__(self, *args, **kwargs): pass
            def withdraw(self): pass
            def mainloop(self): pass
        class Toplevel:
            def __init__(self, *args, **kwargs): pass
        class END: pass
        class BOTH: pass
    class messagebox:
        @staticmethod
        def showinfo(*args, **kwargs): logger.info(f"GUI: {args[0] if args else ''}")
        @staticmethod
        def showwarning(*args, **kwargs): logger.warning(f"GUI: {args[0] if args else ''}")
        @staticmethod
        def showerror(*args, **kwargs): logger.error(f"GUI: {args[0] if args else ''}")
    scrolledtext = None
    ttk = None

# Setup logging
log_dir = Path.home() / ".config" / "posterchanai-sync" / "logs"
log_dir.mkdir(parents=True, exist_ok=True)
log_file = log_dir / f"sync_{datetime.now().strftime('%Y%m%d')}.log"

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.FileHandler(log_file),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

CONFIG_DIR = Path.home() / ".config" / "posterchanai-sync"
CONFIG_FILE = CONFIG_DIR / "config.json"
STATE_FILE = CONFIG_DIR / "state.json"
CONFLICTS_FILE = CONFIG_DIR / "conflicts.json"


@dataclass
class FileState:
    """Track file state for sync"""
    path: str
    mtime: float
    size: int
    hash: Optional[str] = None
    synced: bool = False
    last_sync: Optional[float] = None


class SyncConfig:
    """Configuration manager"""
    def __init__(self):
        self.config = self.load_config()
    
    def load_config(self) -> Dict:
        """Load configuration from file"""
        default_config = {
            "server_url": "http://localhost:8000",
            "api_key": "",
            "sync_dir": str(Path.home() / "PosterchanAI-Sync"),
            "exclude_patterns": [
                "**/.*",  # Hidden files
                "**/__pycache__/**",
                "**/*.pyc",
                "**/node_modules/**",
                "**/.git/**",
                "**/.DS_Store",
                "**/Thumbs.db",
                "**/*.backup",  # Backup files (prevent infinite backup chains)
                "**/*.backup.*"  # Backup files with extensions
            ],
            "poll_interval": 30,  # seconds
            "conflict_resolution": "ask",  # ask, local, remote, newer
            "auto_sync": True
        }
        
        if CONFIG_FILE.exists():
            try:
                with open(CONFIG_FILE, 'r') as f:
                    user_config = json.load(f)
                    default_config.update(user_config)
            except Exception as e:
                logger.error(f"Error loading config: {e}")
        
        return default_config
    
    def save_config(self):
        """Save configuration to file"""
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        try:
            with open(CONFIG_FILE, 'w') as f:
                json.dump(self.config, f, indent=2)
        except Exception as e:
            logger.error(f"Error saving config: {e}")
    
    def get_exclude_patterns(self) -> List[str]:
        """Get exclude patterns"""
        return self.config.get("exclude_patterns", [])


class ConflictHandler:
    """Handle file conflicts"""
    def __init__(self):
        self.conflicts: List[Dict] = []
        self.load_conflicts()
    
    def load_conflicts(self):
        """Load conflicts from file"""
        if CONFLICTS_FILE.exists():
            try:
                with open(CONFLICTS_FILE, 'r') as f:
                    self.conflicts = json.load(f)
            except Exception as e:
                logger.error(f"Error loading conflicts: {e}")
    
    def save_conflicts(self):
        """Save conflicts to file"""
        try:
            with open(CONFLICTS_FILE, 'w') as f:
                json.dump(self.conflicts, f, indent=2)
        except Exception as e:
            logger.error(f"Error saving conflicts: {e}")
    
    def add_conflict(self, file_path: str, local_mtime: float, remote_mtime: float, 
                     local_size: int, remote_size: int):
        """Add a conflict to the list (or update existing one)"""
        # Check if conflict already exists for this file
        existing_conflict = None
        for i, c in enumerate(self.conflicts):
            if c.get("path") == file_path and not c.get("resolved", False):
                existing_conflict = i
                break
        
        conflict = {
            "path": file_path,
            "local_mtime": local_mtime,
            "remote_mtime": remote_mtime,
            "local_size": local_size,
            "remote_size": remote_size,
            "timestamp": time.time(),
            "resolved": False
        }
        
        if existing_conflict is not None:
            # Update existing conflict instead of adding duplicate
            self.conflicts[existing_conflict] = conflict
            logger.debug(f"Updated existing conflict for {file_path}")
        else:
            # Add new conflict
            self.conflicts.append(conflict)
            logger.warning(f"CONFLICT: {file_path} - Local: {datetime.fromtimestamp(local_mtime)}, Remote: {datetime.fromtimestamp(remote_mtime)}")
        
        self.save_conflicts()
    
    def get_unresolved_conflicts(self) -> List[Dict]:
        """Get unresolved conflicts (deduplicated by path)"""
        unresolved = [c for c in self.conflicts if not c.get("resolved", False)]
        # Deduplicate by path - keep only the most recent conflict for each path
        seen_paths = {}
        for conflict in unresolved:
            path = conflict.get("path")
            if path:
                if path not in seen_paths or conflict.get("timestamp", 0) > seen_paths[path].get("timestamp", 0):
                    seen_paths[path] = conflict
        return list(seen_paths.values())
    
    def resolve_conflict(self, file_path: str):
        """Mark a conflict as resolved"""
        for conflict in self.conflicts:
            if conflict.get("path") == file_path:
                conflict["resolved"] = True
        self.save_conflicts()


class FileWatcher(FileSystemEventHandler):
    """Watch for file system changes"""
    def __init__(self, sync_client):
        self.sync_client = sync_client
        self.pending_changes: Dict[str, float] = {}  # path -> timestamp
        self.debounce_time = 2.0  # seconds
    
    def should_exclude(self, path: str) -> bool:
        """Check if path should be excluded"""
        from fnmatch import fnmatch
        for pattern in self.sync_client.config.get_exclude_patterns():
            if fnmatch(path, pattern) or fnmatch(os.path.basename(path), pattern):
                return True
        return False
    
    def on_modified(self, event: FileSystemEvent):
        if event.is_directory:
            return
        self.handle_change(event.src_path)
    
    def on_created(self, event: FileSystemEvent):
        if event.is_directory:
            return
        self.handle_change(event.src_path)
    
    def on_deleted(self, event: FileSystemEvent):
        if event.is_directory:
            return
        self.handle_change(event.src_path, deleted=True)
    
    def on_moved(self, event: FileSystemEvent):
        if event.is_directory:
            return
        self.handle_change(event.dest_path)
        if event.src_path:
            self.handle_change(event.src_path, deleted=True)
    
    def handle_change(self, path: str, deleted: bool = False):
        """Handle file change with debouncing"""
        if self.should_exclude(path):
            return
        
        # Debounce rapid changes
        self.pending_changes[path] = time.time()
        
        # Schedule sync after debounce period
        def sync_after_delay():
            time.sleep(self.debounce_time)
            if path in self.pending_changes:
                if time.time() - self.pending_changes[path] >= self.debounce_time:
                    del self.pending_changes[path]
                    if not deleted:
                        self.sync_client.queue_sync(path)
                    else:
                        self.sync_client.queue_delete(path)
        
        threading.Thread(target=sync_after_delay, daemon=True).start()


class SyncClient:
    """Main sync client"""
    def __init__(self, config: SyncConfig):
        self.config = config
        self.api_key = config.config.get("api_key", "")
        self.server_url = config.config.get("server_url", "").rstrip('/')
        self.sync_dir = Path(config.config.get("sync_dir", ""))
        self.sync_dir.mkdir(parents=True, exist_ok=True)
        self.username = config.config.get("username", "")  # Get username from config or API
        
        # Thread safety: Lock for state dictionary access
        self._state_lock = threading.RLock()
        self.state: Dict[str, FileState] = {}
        self.load_state()
        
        self.observer: Optional[Observer] = None
        self.watcher: Optional[FileWatcher] = None
        self.is_paused = False
        self.is_syncing = False
        self.sync_queue = Queue()
        self.conflict_handler = ConflictHandler()
        
        self.log_window: Optional[tk.Toplevel] = None
        
        # Get username from API if not in config
        if not self.username:
            self.username = self.get_username_from_api()
            # Save username to config if we fetched it
            if self.username:
                self.config.config["username"] = self.username
                self.config.save_config()
                logger.info(f"Saved username to config: {self.username}")
        
        # Verify API connection
        if not self.verify_connection():
            logger.error("Cannot connect to PosterchanAI server. Check server_url and api_key in config.")
        else:
            # If username was set during verification, save it
            if self.username and not self.config.config.get("username"):
                self.config.config["username"] = self.username
                self.config.save_config()
    
    def verify_connection(self) -> bool:
        """Verify API connection"""
        try:
            response = requests.get(
                f"{self.server_url}/api/auth/settings",
                headers=self.get_headers(),
                timeout=5
            )
            if response.status_code == 200:
                # Try to get username from response
                data = response.json()
                if not self.username:
                    # Try username field first
                    username = data.get("username", "")
                    if username:
                        self.username = username
                    else:
                        # Fallback: extract from email
                        email = data.get("notification_email", "")
                        if email and "@" in email:
                            self.username = email.split("@")[0]
                            logger.info(f"Extracted username from email: {self.username}")
                    # Save to config if we got it
                    if self.username:
                        self.config.config["username"] = self.username
                        self.config.save_config()
                return True
            return False
        except Exception as e:
            logger.error(f"Connection verification failed: {e}")
            return False
    
    def get_username_from_api(self) -> str:
        """Get username from API (extract from email if username not available)"""
        try:
            response = requests.get(
                f"{self.server_url}/api/auth/settings",
                headers=self.get_headers(),
                timeout=5
            )
            if response.status_code == 200:
                data = response.json()
                # Try username field first
                username = data.get("username", "")
                if username:
                    return username
                # Fallback: extract from email (e.g., "user@domain.com" -> "user")
                email = data.get("notification_email", "")
                if email and "@" in email:
                    username = email.split("@")[0]
                    logger.info(f"Extracted username from email: {username}")
                    return username
        except Exception as e:
            logger.error(f"Error getting username: {e}")
        return ""
    
    def get_headers(self) -> Dict[str, str]:
        """Get request headers with authentication"""
        return {"Authorization": f"Bearer {self.api_key}"}
    
    def load_state(self):
        """Load sync state from file"""
        if STATE_FILE.exists():
            try:
                with open(STATE_FILE, 'r') as f:
                    state_data = json.load(f)
                    with self._state_lock:
                        self.state = {
                            path: FileState(**data)
                            for path, data in state_data.items()
                        }
            except json.JSONDecodeError as e:
                logger.error(f"Error parsing state file (corrupted?): {e}")
                # Try to backup corrupted file
                backup_file = STATE_FILE.with_suffix('.bak')
                try:
                    import shutil
                    shutil.copy2(STATE_FILE, backup_file)
                    logger.info(f"Backed up corrupted state file to {backup_file}")
                except Exception:
                    pass
            except Exception as e:
                logger.error(f"Error loading state: {e}")
    
    def save_state(self):
        """Save sync state to file (atomic write to prevent corruption)"""
        import tempfile
        import shutil
        try:
            with self._state_lock:
                state_data = {
                    path: asdict(state)
                    for path, state in self.state.items()
                }
            
            # Atomic write: write to temp file, then rename
            temp_file = STATE_FILE.with_suffix('.tmp')
            try:
                with open(temp_file, 'w') as f:
                    json.dump(state_data, f, indent=2)
                # Atomic rename (on most filesystems)
                shutil.move(temp_file, STATE_FILE)
            except Exception as e:
                # Clean up temp file on error
                if temp_file.exists():
                    try:
                        temp_file.unlink()
                    except Exception:
                        pass
                raise
        except Exception as e:
            logger.error(f"Error saving state: {e}")
    
    def calculate_file_hash(self, file_path: Path) -> str:
        """Calculate MD5 hash of file"""
        hash_md5 = hashlib.md5()
        try:
            with open(file_path, "rb") as f:
                for chunk in iter(lambda: f.read(4096), b""):
                    hash_md5.update(chunk)
            return hash_md5.hexdigest()
        except Exception as e:
            logger.error(f"Error calculating hash for {file_path}: {e}")
            return ""
    
    def should_exclude(self, path: Path) -> bool:
        """Check if path should be excluded"""
        from fnmatch import fnmatch
        path_str = str(path)
        for pattern in self.config.get_exclude_patterns():
            if fnmatch(path_str, pattern) or fnmatch(path.name, pattern):
                return True
        return False
    
    def verify_file_exists_on_server(self, remote_path: str) -> bool:
        """Verify a file actually exists on the server (server bug: list-files sometimes returns non-existent files)"""
        try:
            response = requests.head(
                f"{self.server_url}/api/storage/view-file",
                params={"username": self.username, "file_path": remote_path},
                headers=self.get_headers(),
                timeout=10
            )
            return response.status_code == 200
        except Exception as e:
            logger.debug(f"Error verifying file existence for {remote_path}: {e}")
            # If HEAD fails, try GET but don't download
            try:
                response = requests.get(
                    f"{self.server_url}/api/storage/view-file",
                    params={"username": self.username, "file_path": remote_path},
                    headers=self.get_headers(),
                    timeout=10,
                    stream=True
                )
                # Close the stream immediately without reading
                response.close()
                return response.status_code == 200
            except Exception:
                return False
    
    def get_remote_file_info(self, remote_path: str, retries: int = 3) -> Optional[Dict]:
        """Get remote file information with retry logic"""
        dir_path = os.path.dirname(remote_path) if os.path.dirname(remote_path) else ""
        file_name = os.path.basename(remote_path)
        
        for attempt in range(retries):
            try:
                # Use longer timeout for file listing operations (can be slow with many files)
                response = requests.get(
                    f"{self.server_url}/api/storage/list-files",
                    params={"username": self.username, "path": dir_path},
                    headers=self.get_headers(),
                    timeout=30  # Increased from 10 to 30 seconds
                )
                if response.status_code == 200:
                    data = response.json()
                    # Handle different response formats
                    files = data.get("items", data if isinstance(data, list) else [])
                    for file_info in files:
                        if file_info.get("name") == file_name:
                            return file_info
                    # File not found in listing
                    return None
                else:
                    logger.warning(f"Failed to list files: {response.status_code}")
            except requests.exceptions.Timeout as e:
                if attempt < retries - 1:
                    wait_time = 2 ** attempt  # Exponential backoff: 1s, 2s, 4s
                    logger.warning(f"Timeout getting remote file info (attempt {attempt + 1}/{retries}), retrying in {wait_time}s...")
                    time.sleep(wait_time)
                else:
                    logger.error(f"Error getting remote file info after {retries} attempts: {e}")
            except Exception as e:
                if attempt < retries - 1:
                    wait_time = 2 ** attempt
                    logger.warning(f"Error getting remote file info (attempt {attempt + 1}/{retries}): {e}, retrying in {wait_time}s...")
                    time.sleep(wait_time)
                else:
                    logger.error(f"Error getting remote file info after {retries} attempts: {e}")
        return None
    
    def upload_file(self, local_path: Path, remote_path: str, retries: int = 3) -> bool:
        """Upload file to server with retry logic"""
        for attempt in range(retries):
            try:
                # Ensure parent directory exists on server
                dir_path = os.path.dirname(remote_path)
                if dir_path:
                    try:
                        requests.post(
                            f"{self.server_url}/api/storage/mkdir",
                            params={"username": self.username, "path": dir_path},
                            headers=self.get_headers(),
                            timeout=10
                        )
                    except requests.exceptions.RequestException as e:
                        # Directory might already exist, or network error
                        logger.debug(f"Directory creation failed (may already exist): {e}")
                    except Exception as e:
                        logger.debug(f"Unexpected error creating directory: {e}")
            
                with open(local_path, 'rb') as f:
                    files = {'file': (local_path.name, f, 'application/octet-stream')}
                    data = {'username': self.username, 'path': remote_path}
                    response = requests.post(
                        f"{self.server_url}/api/storage/upload-file",
                        files=files,
                        data=data,
                        headers=self.get_headers(),
                        timeout=60
                    )
                    if response.status_code == 200:
                        logger.info(f"Uploaded: {local_path} -> {remote_path}")
                        return True
                    else:
                        logger.warning(f"Upload failed: {response.status_code} - {response.text[:200]}")
                        if attempt < retries - 1:
                            wait_time = 2 ** attempt
                            logger.info(f"Retrying upload in {wait_time}s... (attempt {attempt + 1}/{retries})")
                            time.sleep(wait_time)
                        else:
                            logger.error(f"Upload failed after {retries} attempts")
            except requests.exceptions.Timeout as e:
                if attempt < retries - 1:
                    wait_time = 2 ** attempt
                    logger.warning(f"Upload timeout (attempt {attempt + 1}/{retries}), retrying in {wait_time}s...")
                    time.sleep(wait_time)
                else:
                    logger.error(f"Upload timeout after {retries} attempts: {e}")
            except requests.exceptions.RequestException as e:
                if attempt < retries - 1:
                    wait_time = 2 ** attempt
                    logger.warning(f"Upload network error (attempt {attempt + 1}/{retries}): {e}, retrying in {wait_time}s...")
                    time.sleep(wait_time)
                else:
                    logger.error(f"Upload network error after {retries} attempts: {e}")
            except Exception as e:
                logger.error(f"Error uploading {local_path}: {e}")
                if attempt < retries - 1:
                    wait_time = 2 ** attempt
                    time.sleep(wait_time)
                else:
                    return False
        return False
    
    def download_file(self, remote_path: str, local_path: Path, retries: int = 3) -> bool:
        """Download file from server with retry logic"""
        for attempt in range(retries):
            try:
                # If local_path exists and is a directory, we have a conflict
                if local_path.exists() and local_path.is_dir():
                    logger.error(f"Cannot download {remote_path}: local path is a directory: {local_path}")
                    return False
                
                # If local_path exists and is a file, check if we should overwrite
                if local_path.exists() and local_path.is_file():
                    # File already exists - this is handled by sync logic, but log it
                    logger.debug(f"File already exists locally: {local_path}")
                    return False
                
                # Check if parent directory exists as a file (conflict: server has dir, local has file)
                parent_path = local_path.parent
                if parent_path.exists() and parent_path.is_file():
                    # Parent exists as a file but we need it as a directory
                    # This means server has parent as directory, but local has it as file
                    # Just delete the conflicting file - server's structure takes precedence
                    logger.warning(f"Conflict: Server has '{parent_path.name}' as directory, but local is file. Deleting local file.")
                    try:
                        parent_path.unlink()
                        logger.info(f"Deleted conflicting file {parent_path}")
                    except Exception as e:
                        logger.error(f"Cannot delete conflicting file {parent_path}: {e}")
                        return False
                
                # Now create parent directories
                try:
                    local_path.parent.mkdir(parents=True, exist_ok=True)
                except OSError as e:
                    if e.errno == 17:  # File exists
                        logger.error(f"Cannot create directory {local_path.parent}: path exists as file. This indicates a conflict.")
                        return False
                    raise
                logger.debug(f"Downloading: {remote_path} -> {local_path}")
                response = requests.get(
                    f"{self.server_url}/api/storage/view-file",
                    params={"username": self.username, "file_path": remote_path},
                    headers=self.get_headers(),
                    timeout=60,
                    stream=True
                )
                logger.debug(f"Download response status: {response.status_code} for {remote_path}")
                if response.status_code == 200:
                    with open(local_path, 'wb') as f:
                        for chunk in response.iter_content(chunk_size=8192):
                            f.write(chunk)
                    # Update mtime to match remote
                    try:
                        remote_info = self.get_remote_file_info(remote_path)
                        if remote_info and remote_info.get("modified"):
                            import os
                            os.utime(local_path, (remote_info["modified"], remote_info["modified"]))
                    except OSError as e:
                        logger.warning(f"Could not set file timestamp for {local_path}: {e}")
                    except Exception as e:
                        logger.debug(f"Could not update file timestamp: {e}")
                    logger.debug(f"Downloaded: {remote_path} -> {local_path}")
                    return True
                elif response.status_code == 404:
                    logger.warning(f"File not found on server: {remote_path}")
                    return False  # Don't retry 404 errors
                else:
                    logger.warning(f"Download failed: {response.status_code}")
                    if attempt < retries - 1:
                        wait_time = 2 ** attempt
                        logger.info(f"Retrying download in {wait_time}s... (attempt {attempt + 1}/{retries})")
                        time.sleep(wait_time)
                    else:
                        logger.error(f"Download failed after {retries} attempts")
            except requests.exceptions.Timeout as e:
                if attempt < retries - 1:
                    wait_time = 2 ** attempt
                    logger.warning(f"Download timeout (attempt {attempt + 1}/{retries}), retrying in {wait_time}s...")
                    time.sleep(wait_time)
                else:
                    logger.error(f"Download timeout after {retries} attempts: {e}")
            except requests.exceptions.RequestException as e:
                if attempt < retries - 1:
                    wait_time = 2 ** attempt
                    logger.warning(f"Download network error (attempt {attempt + 1}/{retries}): {e}, retrying in {wait_time}s...")
                    time.sleep(wait_time)
                else:
                    logger.error(f"Download network error after {retries} attempts: {e}")
            except Exception as e:
                logger.error(f"Error downloading {remote_path}: {e}")
                if attempt < retries - 1:
                    wait_time = 2 ** attempt
                    time.sleep(wait_time)
                else:
                    return False
        return False
    
    def delete_remote_file(self, remote_path: str) -> bool:
        """Delete file from server"""
        try:
            response = requests.delete(
                f"{self.server_url}/api/storage/delete-file",
                params={"username": self.username, "file_path": remote_path},
                headers=self.get_headers(),
                timeout=10
            )
            if response.status_code == 200:
                logger.info(f"Deleted remote: {remote_path}")
                return True
        except Exception as e:
            logger.error(f"Error deleting remote {remote_path}: {e}")
        return False
    
    def sync_file(self, local_path: Path, force: bool = False):
        """Sync a single file"""
        if self.is_paused:
            return
        
        if self.should_exclude(local_path):
            return
        
        if not local_path.exists():
            return
        
        # Fix path traversal vulnerability
        try:
            rel_path = local_path.relative_to(self.sync_dir)
        except ValueError:
            logger.warning(f"File {local_path} is outside sync directory, skipping")
            return
        
        remote_path = str(rel_path).replace('\\', '/')
        
        stat = local_path.stat()
        local_mtime = stat.st_mtime
        local_size = stat.st_size
        
        # Optimize hash calculation - only calculate if file changed
        with self._state_lock:
            state = self.state.get(str(rel_path))
        
        if state and state.mtime == local_mtime and state.size == local_size:
            # File hasn't changed, use cached hash
            local_hash = state.hash or self.calculate_file_hash(local_path)
        else:
            # File changed, recalculate hash
            local_hash = self.calculate_file_hash(local_path)
        
        # Get remote file info
        remote_info = self.get_remote_file_info(remote_path)
        
        if remote_info:
            # Check if remote is actually a directory (API bug)
            is_remote_dir = remote_info.get("is_dir", False) or remote_info.get("is_directory", False)
            if is_remote_dir and local_path.is_file():
                # Server has it as directory but local is a file - treat as if remote doesn't exist
                logger.warning(f"Server has {remote_path} as directory but local is file. Will upload to replace.")
                remote_info = None
            else:
                remote_mtime = remote_info.get("modified", remote_info.get("mtime", 0))
                remote_size = remote_info.get("size", 0)
                
                # Check for conflicts - use hash comparison for better accuracy
                if state and state.synced:
                    # Get remote hash if available, or use time+size as fallback
                    remote_hash = remote_info.get("hash")
                    if remote_hash and state.hash:
                        # Use hash comparison for accurate conflict detection
                        is_conflict = state.hash != remote_hash
                    else:
                        # Fallback to time+size comparison
                        is_conflict = abs(local_mtime - remote_mtime) > 1 and local_size != remote_size
                    
                    if is_conflict:
                        # Conflict detected
                        self.conflict_handler.add_conflict(
                            str(rel_path), local_mtime, remote_mtime, local_size, remote_size
                        )
                        # Resolve based on config
                        resolution = self.config.config.get("conflict_resolution", "ask")
                        if resolution == "newer":
                            if local_mtime > remote_mtime:
                                self.upload_file(local_path, remote_path)
                            else:
                                self.download_file(remote_path, local_path)
                        elif resolution == "local":
                            self.upload_file(local_path, remote_path)
                        elif resolution == "remote":
                            self.download_file(remote_path, local_path)
                        elif resolution == "ask":
                            # In headless mode, default to "newer" for "ask"
                            logger.info(f"Conflict resolution is 'ask', defaulting to 'newer' in headless mode")
                            if local_mtime > remote_mtime:
                                self.upload_file(local_path, remote_path)
                            else:
                                self.download_file(remote_path, local_path)
                        return
                
                # Upload if local is newer or doesn't exist remotely
                if not state or not state.synced or local_mtime > remote_mtime:
                    if self.upload_file(local_path, remote_path):
                        with self._state_lock:
                            self.state[str(rel_path)] = FileState(
                                path=str(rel_path),
                                mtime=local_mtime,
                                size=local_size,
                                hash=local_hash,
                                synced=True,
                                last_sync=time.time()
                            )
                        self.save_state()
                return
        
        # If we get here, remote_info is None (file doesn't exist on server)
            
        # File doesn't exist remotely (or was treated as non-existent due to directory mismatch) - upload it
        if self.upload_file(local_path, remote_path):
            with self._state_lock:
                self.state[str(rel_path)] = FileState(
                    path=str(rel_path),
                    mtime=local_mtime,
                    size=local_size,
                    hash=local_hash,
                    synced=True,
                    last_sync=time.time()
                )
            self.save_state()
    
    def sync_directory(self):
        """Sync entire directory (bidirectional)"""
        if self.is_paused:
            return
        
        self.is_syncing = True
        logger.info("Starting directory sync...")
        
        try:
            # First, sync local files to remote (upload)
            for root, dirs, files in os.walk(self.sync_dir):
                # Filter excluded directories
                dirs[:] = [d for d in dirs if not self.should_exclude(Path(root) / d)]
                
                for file in files:
                    local_path = Path(root) / file
                    if not self.should_exclude(local_path):
                        self.sync_file(local_path)
            
            # Then, check remote files and download missing ones (download)
            if not self.username:
                logger.warning("Username not set, skipping remote file download")
            else:
                try:
                    self._sync_remote_files("")
                except Exception as e:
                    logger.error(f"Error syncing remote files: {e}")
        except Exception as e:
            logger.error(f"Error during directory sync: {e}")
        finally:
            self.is_syncing = False
            logger.info("Directory sync complete")
            # Send desktop notification if available (useful when tray icon doesn't work)
            send_notification("PosterchanAI Sync", "Directory sync complete", "info")
    
    def _sync_remote_files(self, remote_dir: str):
        """Recursively sync remote files (download missing files)"""
        # Retry logic for file listing (can be slow with many files)
        max_retries = 3
        for attempt in range(max_retries):
            try:
                logger.debug(f"Listing remote files in: '{remote_dir}'")
                response = requests.get(
                    f"{self.server_url}/api/storage/list-files",
                    params={"username": self.username, "path": remote_dir},
                    headers=self.get_headers(),
                    timeout=30  # Increased from 10 to 30 seconds for file listings
                )
                if response.status_code == 200:
                    data = response.json()
                    # Handle different response formats
                    files = data.get("items", data if isinstance(data, list) else [])
                    logger.debug(f"Found {len(files)} items in remote directory '{remote_dir}'")
                    
                    # Process all files in the listing
                    files_to_download = []
                    dirs_to_sync = []
                    
                    for file_info in files:
                        file_name = file_info.get("name", "")
                        file_path = file_info.get("path", file_name)
                        # Check both is_dir and is_directory (API might use either)
                        is_dir = file_info.get("is_dir", False) or file_info.get("is_directory", False)
                        file_size = file_info.get("size", 0)
                        
                        if not file_name:
                            continue
                        
                        # Build local and remote paths
                        if remote_dir:
                            remote_path = f"{remote_dir}/{file_name}".replace("//", "/")
                        else:
                            remote_path = file_name
                        
                        local_path = self.sync_dir / remote_path.replace("/", os.sep)
                        
                        # Skip if excluded
                        if self.should_exclude(local_path):
                            continue
                        
                        # Better directory detection:
                        # Trust the server's is_directory flag, but use heuristics as fallback
                        # The server API sometimes incorrectly marks files as directories, but
                        # we should trust it when it says something is a directory (especially if size=0)
                        original_is_dir = is_dir
                        
                        # If server says it's a directory AND size is 0, trust it (it's likely a directory)
                        # Only override if we have strong evidence it's actually a file
                        if is_dir and file_size == 0:
                            # Server says directory with size 0 - likely correct, but check local
                            if local_path.exists():
                                if local_path.is_file():
                                    # Local is a file but server says directory - conflict!
                                    # Just delete the local file - server's directory structure takes precedence
                                    logger.warning(f"{file_name}: Server has as directory but local is file. Deleting local file to resolve conflict.")
                                    try:
                                        local_path.unlink()
                                        logger.info(f"Deleted conflicting file {local_path}")
                                        # Remove from state since we're replacing it with a directory
                                        rel_path_str = str(local_path.relative_to(self.sync_dir))
                                        with self._state_lock:
                                            self.state.pop(rel_path_str, None)
                                        self.save_state()
                                    except Exception as e:
                                        logger.error(f"Cannot delete conflicting file {local_path}: {e}")
                                    # Keep as directory to sync its contents
                                    is_dir = True
                                elif local_path.is_dir():
                                    # Both agree it's a directory
                                    is_dir = True
                            else:
                                # Local doesn't exist - trust server (it's a directory)
                                is_dir = True
                                logger.debug(f"{file_name}: Server says directory (size=0), treating as directory")
                        elif file_size > 0:
                            # Size > 0 means it's definitely a file (directories have size 0)
                            is_dir = False
                            logger.debug(f"{file_name}: size={file_size} > 0, treating as file")
                        elif local_path.exists():
                            # If local path exists, check what it is
                            if local_path.is_file():
                                # Local is a file - but if server says directory, there's a conflict
                                if is_dir:
                                    # Server says directory but local is file - resolve conflict
                                    # Just delete the local file - server's directory structure takes precedence
                                    logger.warning(f"{file_name}: Server has as directory but local is file. Deleting local file to resolve conflict.")
                                    try:
                                        local_path.unlink()
                                        logger.info(f"Deleted conflicting file {local_path}")
                                        # Remove from state
                                        rel_path_str = str(local_path.relative_to(self.sync_dir))
                                        with self._state_lock:
                                            self.state.pop(rel_path_str, None)
                                        self.save_state()
                                    except Exception as e:
                                        logger.error(f"Cannot delete conflicting file {local_path}: {e}")
                                    is_dir = True
                                else:
                                    is_dir = False
                                logger.debug(f"{file_name}: local exists as file, treating as {'directory' if is_dir else 'file'}")
                            elif local_path.is_dir():
                                # Local is a directory - remote is probably a directory
                                is_dir = True
                                logger.debug(f"{file_name}: local exists as directory, treating as directory")
                        else:
                            # Local doesn't exist - trust server's designation
                            logger.debug(f"{file_name}: local doesn't exist, trusting server (is_dir={is_dir})")
                        
                        # Don't override directory designation if server says it's a directory
                        # The server knows better than file extension heuristics
                        
                        if is_dir:
                            # Queue directory for recursive sync
                            dirs_to_sync.append(remote_path)
                        else:
                            # It's a file - check if we need to download it
                            if not local_path.exists():
                                # File doesn't exist locally - queue for download
                                files_to_download.append((remote_path, local_path, file_info))
                            else:
                                # File exists locally - check if remote is newer
                                local_mtime = local_path.stat().st_mtime
                                remote_mtime = file_info.get("modified", file_info.get("mtime", 0))
                                rel_path_str = str(local_path.relative_to(self.sync_dir))
                                with self._state_lock:
                                    state = self.state.get(rel_path_str)
                                
                                # Download if remote is newer and we haven't synced, or if conflict resolution says so
                                if remote_mtime > local_mtime and (not state or not state.synced):
                                    resolution = self.config.config.get("conflict_resolution", "ask")
                                    if resolution in ["remote", "newer"]:
                                        files_to_download.append((remote_path, local_path, file_info))
                    
                    # Log summary before downloading
                    if files_to_download:
                        logger.info(f"Downloading {len(files_to_download)} file(s) from '{remote_dir or 'root'}'...")
                    
                    # Download all queued files
                    downloaded_count = 0
                    failed_count = 0
                    for remote_path, local_path, file_info in files_to_download:
                        # Verify file actually exists on server before downloading
                        # (Server bug: list-files sometimes returns non-existent files)
                        if not self.verify_file_exists_on_server(remote_path):
                            logger.warning(f"File {remote_path} listed but doesn't exist on server, skipping")
                            failed_count += 1
                            continue
                        
                        if self.download_file(remote_path, local_path):
                            downloaded_count += 1
                            # Update state
                            remote_mtime = file_info.get("modified", file_info.get("mtime", time.time()))
                            rel_path_str = str(local_path.relative_to(self.sync_dir))
                            with self._state_lock:
                                self.state[rel_path_str] = FileState(
                                    path=rel_path_str,
                                    mtime=remote_mtime,
                                    size=file_info.get("size", 0),
                                    synced=True,
                                    last_sync=time.time()
                                )
                            self.save_state()
                        else:
                            failed_count += 1
                    
                    if failed_count > 0:
                        logger.warning(f"Failed to download {failed_count} file(s) from '{remote_dir or 'root'}' (may not exist on server)")
                    
                    if downloaded_count > 0:
                        logger.info(f"Downloaded {downloaded_count}/{len(files_to_download)} file(s) from '{remote_dir or 'root'}'")
                    
                    # Recursively sync subdirectories
                    for dir_path in dirs_to_sync:
                        self._sync_remote_files(dir_path)
                    # Successfully processed the listing, break out of retry loop
                    break
                else:
                    logger.warning(f"Failed to list remote files: {response.status_code}")
                    if attempt < max_retries - 1:
                        wait_time = 2 ** attempt
                        logger.info(f"Retrying in {wait_time}s...")
                        time.sleep(wait_time)
            except requests.exceptions.Timeout as e:
                if attempt < max_retries - 1:
                    wait_time = 2 ** attempt
                    logger.warning(f"Timeout listing remote files (attempt {attempt + 1}/{max_retries}), retrying in {wait_time}s...")
                    time.sleep(wait_time)
                else:
                    logger.error(f"Error listing remote files after {max_retries} attempts: {e}")
            except Exception as e:
                if attempt < max_retries - 1:
                    wait_time = 2 ** attempt
                    logger.warning(f"Error listing remote files (attempt {attempt + 1}/{max_retries}): {e}, retrying in {wait_time}s...")
                    time.sleep(wait_time)
                else:
                    logger.error(f"Error listing remote files after {max_retries} attempts: {e}")
    
    def queue_sync(self, path: str):
        """Queue a file for syncing"""
        self.sync_queue.put(("sync", path))
    
    def queue_delete(self, path: str):
        """Queue a file for deletion"""
        self.sync_queue.put(("delete", path))
    
    def start_watcher(self):
        """Start file system watcher"""
        if self.observer:
            return
        
        self.watcher = FileWatcher(self)
        self.observer = Observer()
        self.observer.schedule(self.watcher, str(self.sync_dir), recursive=True)
        self.observer.start()
        logger.info(f"Started watching: {self.sync_dir}")
    
    def stop_watcher(self):
        """Stop file system watcher"""
        if self.observer:
            self.observer.stop()
            self.observer.join()
            self.observer = None
            logger.info("Stopped file watcher")
    
    def pause(self):
        """Pause syncing"""
        self.is_paused = True
        logger.info("Sync paused")
    
    def resume(self):
        """Resume syncing"""
        self.is_paused = False
        logger.info("Sync resumed")
    
    def quit(self):
        """Quit sync client"""
        self.stop_watcher()
        self.save_state()
        logger.info("Sync client shutting down")
        sys.exit(0)


def send_notification(title: str, message: str, urgency: str = "normal"):
    """Send desktop notification using notify-send"""
    try:
        import subprocess
        urgency_map = {"info": "normal", "warning": "normal", "error": "critical", "normal": "normal"}
        subprocess.run(
            ["notify-send", "-u", urgency_map.get(urgency, "normal"), title, message],
            timeout=2,
            capture_output=True
        )
    except:
        pass  # Silently fail if notify-send is not available


def is_wayland() -> bool:
    """Check if running on Wayland"""
    wayland_display = os.environ.get("WAYLAND_DISPLAY")
    xdg_session = os.environ.get("XDG_SESSION_TYPE")
    is_wayland_env = bool(wayland_display) or xdg_session == "wayland"
    logger.debug(f"Wayland detection: WAYLAND_DISPLAY={wayland_display}, XDG_SESSION_TYPE={xdg_session}, result={is_wayland_env}")
    return is_wayland_env

def is_headless_mode() -> bool:
    """Check if we should run in headless mode (no GUI)"""
    # Check if GUI is available
    if not GUI_AVAILABLE:
        return True
    
    # Check if DISPLAY or WAYLAND_DISPLAY is set - if so, try GUI mode
    # (Even systemd services can show GUI if display is available)
    has_display = os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")
    
    if not has_display:
        return True  # No display available, must run headless
    
    # If we have a display, try GUI mode even if running as systemd service
    # (The tray icon will fail gracefully if it can't connect)
    return False


def create_icon_image() -> Image.Image:
    """Create cyberpunk-style icon image"""
    width = height = 64
    image = Image.new('RGB', (width, height), color='#000000')
    draw = ImageDraw.Draw(image)
    
    # Draw cyberpunk-style logo (simple geometric design)
    # Main circle
    draw.ellipse([10, 10, 54, 54], fill='#00ff00', outline='#00ffff', width=2)
    # Inner pattern
    draw.ellipse([20, 20, 44, 44], fill='#000000', outline='#00ff00', width=1)
    # Lines
    draw.line([32, 10, 32, 54], fill='#00ffff', width=2)
    draw.line([10, 32, 54, 32], fill='#00ffff', width=2)
    
    return image


class WaylandTrayIcon:
    """StatusNotifierItem-based tray icon for Wayland"""
    def __init__(self, sync_client: SyncClient):
        self.sync_client = sync_client
        self.loop = None
        self.icon_path = None
        self._setup_icon_image()
    
    def _setup_icon_image(self):
        """Save icon image to temporary file for StatusNotifierItem"""
        import tempfile
        image = create_icon_image()
        # Save as PNG
        self.icon_path = os.path.join(tempfile.gettempdir(), "posterchanai-sync-icon.png")
        image.save(self.icon_path, "PNG")
    
    def _create_menu_model(self):
        """Create Gio.MenuModel for the tray icon menu"""
        menu = Gio.Menu()
        
        # Start Sync
        menu.append("Start Sync", "app.start-sync")
        
        # Pause/Resume Sync
        pause_label = "Resume Sync" if self.sync_client.is_paused else "Pause Sync"
        menu.append(pause_label, "app.toggle-pause")
        
        menu.append("View Logs", "app.view-logs")
        menu.append("View Conflicts", "app.view-conflicts")
        menu.append("Settings...", "app.settings")
        menu.append("Quit", "app.quit")
        
        return menu
    
    def _handle_action(self, action, param):
        """Handle menu actions"""
        action_name = action.get_name()
        
        if action_name == "start-sync":
            threading.Thread(target=self.sync_client.sync_directory, daemon=True).start()
        elif action_name == "toggle-pause":
            if self.sync_client.is_paused:
                self.sync_client.resume()
            else:
                self.sync_client.pause()
        elif action_name == "view-logs":
            if GUI_AVAILABLE:
                if self.sync_client.log_window is None or not self.sync_client.log_window.winfo_exists():
                    self.sync_client.log_window = create_log_window(self.sync_client)
                else:
                    self.sync_client.log_window.lift()
        elif action_name == "view-conflicts":
            if GUI_AVAILABLE:
                conflicts = self.sync_client.conflict_handler.get_unresolved_conflicts()
                if conflicts:
                    message = f"Found {len(conflicts)} unresolved conflicts:\n\n"
                    for c in conflicts[:10]:
                        message += f"• {c['path']}\n"
                    if len(conflicts) > 10:
                        message += f"\n... and {len(conflicts) - 10} more"
                    messagebox.showinfo("File Conflicts", message)
                else:
                    messagebox.showinfo("File Conflicts", "No conflicts found!")
        elif action_name == "settings":
            if GUI_AVAILABLE:
                try:
                    from setup_wizard import check_and_run_setup
                    def run_wizard():
                        if check_and_run_setup(force=True):
                            self.sync_client.config = SyncConfig()
                            self.sync_client.api_key = self.sync_client.config.config.get("api_key", "")
                            self.sync_client.server_url = self.sync_client.config.config.get("server_url", "").rstrip('/')
                            self.sync_client.sync_dir = Path(self.sync_client.config.config.get("sync_dir", ""))
                            self.sync_client.sync_dir.mkdir(parents=True, exist_ok=True)
                            if not self.sync_client.username:
                                self.sync_client.username = self.sync_client.get_username_from_api()
                            if self.sync_client.verify_connection():
                                messagebox.showinfo("Settings", "Configuration updated successfully!")
                            else:
                                messagebox.showwarning("Settings", "Configuration saved but cannot verify connection.")
                        else:
                            messagebox.showinfo("Settings", "Configuration update cancelled.")
                    threading.Thread(target=run_wizard, daemon=True).start()
                except Exception as e:
                    logger.error(f"Error opening settings: {e}")
                    if GUI_AVAILABLE:
                        messagebox.showerror("Error", f"Failed to open settings: {e}")
        elif action_name == "quit":
            self.sync_client.quit()
            if self.loop:
                self.loop.quit()
    
    def run(self):
        """Start the StatusNotifierItem tray icon"""
        if not WAYLAND_TRAY_AVAILABLE:
            raise RuntimeError("PyGObject not available for Wayland tray icon")
        
        try:
            from gi.repository import Gio, GLib
            
            # StatusNotifierItem XML interface definition
            STATUS_NOTIFIER_ITEM_XML = """
            <node>
              <interface name="org.kde.StatusNotifierItem">
                <method name="Activate">
                  <arg type="i" direction="in" name="x"/>
                  <arg type="i" direction="in" name="y"/>
                </method>
                <method name="ContextMenu">
                  <arg type="i" direction="in" name="x"/>
                  <arg type="i" direction="in" name="y"/>
                </method>
                <method name="Scroll">
                  <arg type="i" direction="in" name="delta"/>
                  <arg type="s" direction="in" name="orientation"/>
                </method>
                <method name="SecondaryActivate">
                  <arg type="i" direction="in" name="x"/>
                  <arg type="i" direction="in" name="y"/>
                </method>
                <property name="Category" type="s" access="read"/>
                <property name="Id" type="s" access="read"/>
                <property name="Title" type="s" access="read"/>
                <property name="Status" type="s" access="read"/>
                <property name="WindowId" type="i" access="read"/>
                <property name="IconName" type="s" access="read"/>
                <property name="IconPixmap" type="a(iiay)" access="read"/>
                <property name="OverlayIconName" type="s" access="read"/>
                <property name="OverlayIconPixmap" type="a(iiay)" access="read"/>
                <property name="AttentionIconName" type="s" access="read"/>
                <property name="AttentionIconPixmap" type="a(iiay)" access="read"/>
                <property name="AttentionMovieName" type="s" access="read"/>
                <property name="ToolTip" type="(sa(iiay)ss)" access="read"/>
                <property name="IconThemePath" type="s" access="read"/>
                <property name="Menu" type="o" access="read"/>
                <property name="ItemIsMenu" type="b" access="read"/>
                <signal name="NewTitle"/>
                <signal name="NewIcon"/>
                <signal name="NewAttentionIcon"/>
                <signal name="NewOverlayIcon"/>
                <signal name="NewToolTip"/>
                <signal name="NewStatus">
                  <arg type="s" name="status"/>
                </signal>
              </interface>
            </node>
            """
            
            # Get DBus connection
            connection = Gio.bus_get_sync(Gio.BusType.SESSION, None)
            
            # Register service name
            service_name = "com.posterchanai.sync"
            item_path = "/StatusNotifierItem"
            
            def on_name_acquired(connection, name):
                logger.info(f"Acquired DBus name: {name}")
            
            def on_name_lost(connection, name):
                logger.warning(f"Lost DBus name: {name}")
            
            bus_id = Gio.bus_own_name(
                Gio.BusType.SESSION,
                service_name,
                Gio.BusNameOwnerFlags.NONE,
                on_name_acquired,
                on_name_lost,
                None
            )
            
            # Create StatusNotifierItem interface
            class StatusNotifierItemImpl(GObject.GObject):
                __gtype_name__ = "StatusNotifierItemImpl"
                
                def __init__(self, tray_icon):
                    super().__init__()
                    self.tray_icon = tray_icon
                    self._status = "Active"
                    self._category = "ApplicationStatus"
                    self._id = "posterchanai-sync"
                    self._title = "PosterchanAI Sync"
                    self._window_id = 0
                    self._icon_name = ""
                    self._icon_theme_path = ""
                    self._item_is_menu = False
                    self._menu_path = None
                
                def _get_icon_pixmap(self):
                    """Convert icon image to DBus pixmap format"""
                    try:
                        from PIL import Image
                        image = Image.open(self.tray_icon.icon_path)
                        image = image.convert("RGBA")
                        width, height = image.size
                        pixels = list(image.getdata())
                        # Convert to format: [(width, height, [r, g, b, a, ...])]
                        pixel_data = []
                        for r, g, b, a in pixels:
                            pixel_data.extend([r, g, b, a])
                        return [(width, height, pixel_data)]
                    except Exception as e:
                        logger.debug(f"Could not convert icon to pixmap: {e}")
                        return []
                
                def do_get_property(self, prop):
                    """Handle property get requests"""
                    if prop.name == "Category":
                        return self._category
                    elif prop.name == "Id":
                        return self._id
                    elif prop.name == "Title":
                        return self._title
                    elif prop.name == "Status":
                        return self._status
                    elif prop.name == "WindowId":
                        return self._window_id
                    elif prop.name == "IconName":
                        return self._icon_name
                    elif prop.name == "IconPixmap":
                        return self._get_icon_pixmap()
                    elif prop.name == "IconThemePath":
                        return self._icon_theme_path
                    elif prop.name == "ItemIsMenu":
                        return self._item_is_menu
                    elif prop.name == "Menu":
                        return self._menu_path or ""
                    elif prop.name == "ToolTip":
                        # ToolTip format: (icon_name, icon_pixmap, title, description)
                        status_text = "Paused" if self.tray_icon.sync_client.is_paused else "Syncing"
                        return ("", [], f"PosterchanAI Sync - {status_text}", "")
                    return None
                
                def do_activate(self, x, y):
                    """Handle left-click activation"""
                    logger.info(f"Tray icon activated at ({x}, {y})")
                    # Show main GUI window if available
                    if GUI_AVAILABLE:
                        root = self.tray_icon.sync_client.root
                        if root:
                            root.deiconify()
                            root.lift()
                            root.focus_force()
                
                def do_context_menu(self, x, y):
                    """Handle right-click context menu"""
                    logger.info(f"Context menu requested at ({x}, {y})")
                    # For now, just show the GUI - full menu implementation would require D-Bus menu
                    if GUI_AVAILABLE:
                        root = self.tray_icon.sync_client.root
                        if root:
                            root.deiconify()
                            root.lift()
                
                def do_scroll(self, delta, orientation):
                    """Handle scroll events"""
                    logger.debug(f"Scroll: {delta} {orientation}")
            
            # Simplified approach: Register service name only
            # Full StatusNotifierItem vtable registration has PyGObject callback translation issues
            # The vtable.callback assignment fails with "Cannot translate Python object to callback type"
            # Many compositors (including Hyprland with snixembed) can discover services by name
            # For full functionality, use 'posterchanai-sync --gui' or install snixembed
            logger.info("Using simplified StatusNotifierItem registration (service name only)")
            logger.info("Note: Full DBus interface registration skipped due to PyGObject limitations")
            logger.info("For tray icon support, install snixembed: https://github.com/KDE/snixembed")
            logger.info("Or use 'posterchanai-sync --gui' for GUI interface (recommended for ashell)")
            
            # The service name is already registered above, which is sufficient for basic discovery
            # We skip the full interface registration to avoid the callback translation error
            registration_id = None
            
            # Register with StatusNotifierWatcher
            watcher_path = "/StatusNotifierWatcher"
            watcher_interface = "org.kde.StatusNotifierWatcher"
            
            def register_with_watcher():
                try:
                    # Wait a bit for the service to be fully registered
                    time.sleep(0.5)
                    watcher_proxy = Gio.DBusProxy.new_sync(
                        connection,
                        Gio.DBusProxyFlags.NONE,
                        None,
                        "org.kde.StatusNotifierWatcher",
                        watcher_path,
                        watcher_interface,
                        None
                    )
                    # Register with full service path: service_name + object_path
                    service_path = service_name + item_path
                    watcher_proxy.call_sync(
                        "RegisterStatusNotifierItem",
                        GLib.Variant("(s)", (service_path,)),
                        Gio.DBusCallFlags.NONE,
                        -1,
                        None
                    )
                    logger.info(f"Registered StatusNotifierItem with watcher: {service_path}")
                except Exception as e:
                    logger.warning(f"Could not register with StatusNotifierWatcher: {e}")
                    logger.info("Tray icon may still work if compositor supports StatusNotifierItem directly")
                    import traceback
                    logger.debug(traceback.format_exc())
            
            # Try to register with watcher (may not exist on all systems)
            # Use a longer delay to ensure service is registered first
            GLib.timeout_add(500, register_with_watcher)
            
            logger.info("Wayland tray icon service registered (StatusNotifierItem)")
            logger.info(f"Service: {service_name}, Path: {item_path}")
            logger.info(f"Full service path: {service_name}{item_path}")
            
            # Run main loop in a separate thread to keep the service alive
            # Note: GLib.MainLoop must run in a non-daemon thread for DBus to work properly
            def run_loop():
                try:
                    self.loop = GLib.MainLoop()
                    logger.info("Starting GLib.MainLoop for tray icon...")
                    self.loop.run()
                except Exception as e:
                    logger.error(f"GLib.MainLoop error: {e}")
                    import traceback
                    logger.error(traceback.format_exc())
            
            # Use non-daemon thread so it doesn't get killed
            loop_thread = threading.Thread(target=run_loop, daemon=False)
            loop_thread.start()
            
            # Keep references to prevent garbage collection
            self._bus_id = bus_id
            self._connection = connection
            self._impl = impl
            self._registration_id = registration_id
            
        except Exception as e:
            logger.error(f"Failed to create Wayland tray icon: {e}")
            import traceback
            logger.error(traceback.format_exc())
            raise


def create_tray_icon(sync_client: SyncClient):
    """Create system tray icon (works on both X11 and Wayland)"""
    if not GUI_AVAILABLE:
        raise RuntimeError("Cannot create tray icon: GUI not available")
    
    # Check Wayland detection
    wayland_detected = is_wayland()
    logger.info(f"Wayland detection: {wayland_detected} (WAYLAND_DISPLAY={os.environ.get('WAYLAND_DISPLAY')}, XDG_SESSION_TYPE={os.environ.get('XDG_SESSION_TYPE')})")
    logger.info(f"Wayland tray available: {WAYLAND_TRAY_AVAILABLE}")
    
    # Use Wayland StatusNotifierItem if on Wayland and available
    if wayland_detected:
        if WAYLAND_TRAY_AVAILABLE:
            logger.info("Creating Wayland StatusNotifierItem tray icon")
            try:
                return WaylandTrayIcon(sync_client)
            except Exception as e:
                logger.error(f"Failed to create Wayland tray icon: {e}")
                import traceback
                logger.error(traceback.format_exc())
                # Don't fall back to pystray on Wayland - it won't work
                raise RuntimeError(f"Cannot create Wayland tray icon: {e}. Use 'posterchanai-sync --gui' for GUI interface.")
        else:
            logger.warning("Wayland detected but PyGObject not available. Cannot create tray icon.")
            logger.warning("Install PyGObject (python3-gi system package) for Wayland tray support.")
            raise RuntimeError("Wayland detected but PyGObject not available. Install PyGObject (python3-gi) for Wayland tray support. Use 'posterchanai-sync --gui' for GUI interface.")
    
    # Otherwise use pystray (X11)
    # Double-check: if we're on Wayland but detection failed, warn and don't use pystray
    if os.environ.get("WAYLAND_DISPLAY") or os.environ.get("XDG_SESSION_TYPE") == "wayland":
        logger.error("Wayland environment detected but is_wayland() returned False. This is a bug.")
        logger.error("WAYLAND_DISPLAY=" + str(os.environ.get("WAYLAND_DISPLAY")))
        logger.error("XDG_SESSION_TYPE=" + str(os.environ.get("XDG_SESSION_TYPE")))
        raise RuntimeError("Wayland detected but detection logic failed. Use 'posterchanai-sync --gui' for GUI interface.")
    
    logger.info("Creating X11 pystray icon (X11 detected)")
    image = create_icon_image()
    
    def show_logs(icon=None, item=None):
        """Show log window"""
        if not GUI_AVAILABLE:
            logger.info("GUI not available. View logs with: journalctl --user -u posterchanai-sync -f")
            return
        if sync_client.log_window is None or not sync_client.log_window.winfo_exists():
            sync_client.log_window = create_log_window(sync_client)
            if sync_client.log_window is None:
                return
        else:
            sync_client.log_window.lift()
    
    def toggle_pause(icon=None, item=None):
        """Toggle pause/resume"""
        if sync_client.is_paused:
            sync_client.resume()
            if item:
                item.text = "Pause Sync"
        else:
            sync_client.pause()
            if item:
                item.text = "Resume Sync"
    
    def show_conflicts(icon=None, item=None):
        """Show conflicts"""
        conflicts = sync_client.conflict_handler.get_unresolved_conflicts()
        if conflicts:
            message = f"Found {len(conflicts)} unresolved conflicts:\n\n"
            for c in conflicts[:10]:  # Show first 10
                message += f"• {c['path']}\n"
            if len(conflicts) > 10:
                message += f"\n... and {len(conflicts) - 10} more"
            messagebox.showinfo("File Conflicts", message)
        else:
            messagebox.showinfo("File Conflicts", "No conflicts found!")
    
    def show_settings(icon=None, item=None):
        """Show settings wizard"""
        try:
            from setup_wizard import check_and_run_setup
            # Run wizard in a separate thread to avoid blocking
            def run_wizard():
                if check_and_run_setup(force=True):
                    # Config was updated, reload it
                    sync_client.config = SyncConfig()
                    sync_client.api_key = sync_client.config.config.get("api_key", "")
                    sync_client.server_url = sync_client.config.config.get("server_url", "").rstrip('/')
                    sync_client.sync_dir = Path(sync_client.config.config.get("sync_dir", ""))
                    sync_client.sync_dir.mkdir(parents=True, exist_ok=True)
                    
                    # Get username if changed
                    if not sync_client.username:
                        sync_client.username = sync_client.get_username_from_api()
                    
                    # Verify connection
                    if sync_client.verify_connection():
                        messagebox.showinfo("Settings", "Configuration updated successfully!")
                    else:
                        messagebox.showwarning("Settings", "Configuration saved but cannot verify connection. Check your settings.")
                else:
                    messagebox.showinfo("Settings", "Configuration update cancelled.")
            
            threading.Thread(target=run_wizard, daemon=True).start()
        except ImportError:
            messagebox.showerror("Error", "Setup wizard not available")
        except Exception as e:
            logger.error(f"Error opening settings: {e}")
            messagebox.showerror("Error", f"Failed to open settings: {e}")
    
    menu = pystray.Menu(
        pystray.MenuItem("Start Sync", lambda: sync_client.sync_directory(), default=True),
        pystray.MenuItem("Pause Sync", toggle_pause),
        pystray.MenuItem("View Logs", show_logs),
        pystray.MenuItem("View Conflicts", show_conflicts),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Settings...", show_settings),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Quit", lambda: sync_client.quit())
    )
    
    icon = pystray.Icon("PosterchanAI Sync", image, "PosterchanAI Sync Client", menu)
    return icon


def create_log_window(sync_client: SyncClient):
    """Create log viewer window"""
    if not GUI_AVAILABLE:
        logger.info("GUI not available. View logs with: journalctl --user -u posterchanai-sync -f")
        return None
    window = tk.Toplevel()
    window.title("PosterchanAI Sync - Logs")
    window.geometry("900x700")
    window.resizable(True, True)
    
    # Enhanced Cyberpunk theme colors
    bg_color = "#000000"
    bg_dark = "#0a0a0a"
    fg_color = "#00ff41"
    accent_color = "#00ffff"
    text_bg = "#0d0d0d"
    border_color = "#00ff41"
    
    window.configure(bg=bg_color)
    
    # Header frame
    header_frame = tk.Frame(window, bg=border_color, height=50)
    header_frame.pack(fill=tk.X, padx=2, pady=2)
    
    header_inner = tk.Frame(header_frame, bg=bg_dark)
    header_inner.pack(fill=tk.BOTH, expand=True, padx=2, pady=2)
    
    title = tk.Label(
        header_inner,
        text="[ SYSTEM LOGS ]",
        font=("Courier New", 14, "bold"),
        bg=bg_dark,
        fg=accent_color
    )
    title.pack(pady=10)
    
    # Log text area with border
    text_container = tk.Frame(window, bg=border_color)
    text_container.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)
    
    text_area = scrolledtext.ScrolledText(
        text_container,
        bg=text_bg,
        fg=fg_color,
        font=("Courier New", 9),
        insertbackground=fg_color,
        selectbackground="#003300",
        selectforeground=fg_color,
        relief=tk.FLAT,
        borderwidth=0
    )
    text_area.pack(fill=tk.BOTH, expand=True, padx=2, pady=2)
    
    # Read log file
    try:
        with open(log_file, 'r') as f:
            lines = f.readlines()
            text_area.insert(tk.END, ''.join(lines[-500:]))  # Last 500 lines
            text_area.see(tk.END)
    except Exception as e:
        text_area.insert(tk.END, f"Error reading log: {e}\n")
    
    # Refresh button with cyberpunk styling
    def refresh_logs():
        text_area.delete(1.0, tk.END)
        try:
            with open(log_file, 'r') as f:
                lines = f.readlines()
                text_area.insert(tk.END, ''.join(lines[-500:]))
                text_area.see(tk.END)
        except Exception as e:
            text_area.insert(tk.END, f"[ERROR] Error reading log: {e}\n")
    
    button_frame = tk.Frame(window, bg=bg_color)
    button_frame.pack(pady=10)
    
    refresh_btn = tk.Button(
        button_frame,
        text="[ REFRESH ]",
        command=refresh_logs,
        bg="#001a00",
        fg=fg_color,
        activebackground="#003300",
        activeforeground=fg_color,
        font=("Courier New", 10, "bold"),
        relief=tk.FLAT,
        borderwidth=0,
        padx=20,
        pady=5,
        cursor="hand2"
    )
    refresh_btn.pack()
    
    # Auto-refresh every 2 seconds
    def auto_refresh():
        refresh_logs()
        window.after(2000, auto_refresh)
    
    window.after(2000, auto_refresh)
    
    return window


def create_main_gui_window(sync_client: Optional[SyncClient] = None):
    """Create a standalone GUI window for accessing sync client features"""
    if not GUI_AVAILABLE:
        print("GUI not available. Install tkinter to use the GUI interface.")
        return None
    
    # Load config if sync_client not provided
    if sync_client is None:
        config = SyncConfig()
        sync_client = SyncClient(config)
    
    root = tk.Tk()
    root.title("PosterchanAI Sync Client")
    root.geometry("700x650")
    root.resizable(False, False)
    
    # Enhanced Cyberpunk theme colors
    bg_color = "#000000"  # Pure black
    bg_dark = "#0a0a0a"   # Slightly lighter black
    fg_color = "#00ff41"  # Matrix green
    accent_color = "#00ffff"  # Cyan
    accent_pink = "#ff00ff"  # Magenta
    warning_color = "#ffaa00"  # Amber
    error_color = "#ff0040"  # Red
    border_color = "#00ff41"  # Green border
    button_bg = "#001a00"  # Dark green
    button_hover = "#003300"  # Lighter green
    text_bg = "#0d0d0d"  # Slightly lighter for text areas
    
    root.configure(bg=bg_color)
    
    # Create a canvas for scanline effect
    canvas = tk.Canvas(root, bg=bg_color, highlightthickness=0)
    canvas.pack(fill=tk.BOTH, expand=True)
    
    # Draw scanline effect
    def draw_scanlines():
        canvas.delete("scanline")
        for i in range(0, 700, 4):
            canvas.create_line(0, i, 700, i, fill="#001100", width=1, tags="scanline", stipple="gray25")
        root.after(100, draw_scanlines)
    
    draw_scanlines()
    
    # Main container frame
    main_frame = tk.Frame(canvas, bg=bg_color)
    canvas.create_window(0, 0, window=main_frame, anchor="nw", width=700, height=650)
    
    # Title with glow effect
    title_frame = tk.Frame(main_frame, bg=bg_color)
    title_frame.pack(pady=30)
    
    # Glowing title
    title_shadow = tk.Label(
        title_frame,
        text="╔═══════════════════════════════════════╗",
        font=("Courier New", 10),
        bg=bg_color,
        fg="#003300"
    )
    title_shadow.pack()
    
    title = tk.Label(
        title_frame,
        text="◈ POSTERCHANAI SYNC CLIENT ◈",
        font=("Courier New", 18, "bold"),
        bg=bg_color,
        fg=accent_color
    )
    title.pack()
    
    title_shadow2 = tk.Label(
        title_frame,
        text="╚═══════════════════════════════════════╝",
        font=("Courier New", 10),
        bg=bg_color,
        fg="#003300"
    )
    title_shadow2.pack()
    
    # Subtitle
    subtitle = tk.Label(
        title_frame,
        text="[SYSTEM STATUS: ONLINE]",
        font=("Courier New", 9),
        bg=bg_color,
        fg=fg_color
    )
    subtitle.pack(pady=(5, 0))
    
    # Status frame with border
    status_container = tk.Frame(main_frame, bg=border_color)
    status_container.pack(pady=15, padx=30, fill=tk.X)
    
    status_frame = tk.Frame(status_container, bg=bg_dark, padx=15, pady=10)
    status_frame.pack(fill=tk.BOTH, expand=True, padx=2, pady=2)
    
    status_label = tk.Label(
        status_frame,
        text="[STATUS]",
        font=("Courier New", 11, "bold"),
        bg=bg_dark,
        fg=accent_color
    )
    status_label.pack(side=tk.LEFT)
    
    # Status indicator dot
    status_dot = tk.Label(
        status_frame,
        text="●",
        font=("Arial", 16),
        bg=bg_dark,
        fg=fg_color
    )
    status_dot.pack(side=tk.LEFT, padx=(10, 5))
    
    # Status text that can be updated
    status_text = tk.Label(
        status_frame,
        text="RUNNING" if sync_client else "NOT CONNECTED",
        font=("Courier New", 11, "bold"),
        bg=bg_dark,
        fg=fg_color
    )
    status_text.pack(side=tk.LEFT, padx=10)
    
    # Function to update status (will be defined after buttons are created)
    def update_status_base():
        if sync_client:
            if sync_client.is_paused:
                status_text.config(text="PAUSED", fg=warning_color)
                status_dot.config(fg=warning_color)
            elif sync_client.is_syncing:
                status_text.config(text="SYNCING...", fg=accent_color)
                status_dot.config(fg=accent_color)
            else:
                status_text.config(text="RUNNING", fg=fg_color)
                status_dot.config(fg=fg_color)
        else:
            status_text.config(text="NOT CONNECTED", fg=error_color)
            status_dot.config(fg=error_color)
    
    # Buttons frame with border
    buttons_container = tk.Frame(main_frame, bg=border_color)
    buttons_container.pack(pady=15, padx=30, fill=tk.BOTH, expand=True)
    
    buttons_frame = tk.Frame(buttons_container, bg=bg_dark, padx=10, pady=10)
    buttons_frame.pack(fill=tk.BOTH, expand=True, padx=2, pady=2)
    
    def open_logs():
        if sync_client:
            window = create_log_window(sync_client)
            if window:
                window.lift()
        else:
            messagebox.showinfo("Logs", "Sync client not connected")
    
    def open_settings():
        try:
            from setup_wizard import SetupWizard
            # Create wizard as a child window of the main GUI
            wizard = SetupWizard(parent=root)
            if wizard.run():
                messagebox.showinfo("Settings", "Configuration updated successfully!")
                # Reload config if sync_client exists
                if sync_client:
                    # SyncConfig is already imported at module level
                    sync_client.config = SyncConfig()
                    sync_client.api_key = sync_client.config.config.get("api_key", "")
                    sync_client.server_url = sync_client.config.config.get("server_url", "").rstrip('/')
                    sync_client.sync_dir = Path(sync_client.config.config.get("sync_dir", ""))
                    sync_client.sync_dir.mkdir(parents=True, exist_ok=True)
                    # Get username if changed
                    if not sync_client.username:
                        sync_client.username = sync_client.get_username_from_api()
            else:
                messagebox.showinfo("Settings", "Configuration update cancelled.")
        except Exception as e:
            logger.error(f"Error opening settings: {e}")
            import traceback
            logger.error(traceback.format_exc())
            messagebox.showerror("Error", f"Failed to open settings: {e}")
    
    def show_conflicts_gui():
        if sync_client:
            conflicts = sync_client.conflict_handler.get_unresolved_conflicts()
            if conflicts:
                message = f"Found {len(conflicts)} unresolved conflicts:\n\n"
                for c in conflicts[:10]:
                    message += f"• {c['path']}\n"
                if len(conflicts) > 10:
                    message += f"\n... and {len(conflicts) - 10} more"
                messagebox.showinfo("File Conflicts", message)
            else:
                messagebox.showinfo("File Conflicts", "No conflicts found!")
        else:
            messagebox.showinfo("Conflicts", "Sync client not connected")
    
    def trigger_sync():
        if sync_client:
            threading.Thread(target=sync_client.sync_directory, daemon=True).start()
            messagebox.showinfo("Sync", "Sync started")
            update_status()
        else:
            messagebox.showinfo("Sync", "Sync client not connected")
    
    # Create pause button separately so we can update its text
    pause_btn = None
    
    def toggle_pause():
        if sync_client:
            if sync_client.is_paused:
                sync_client.resume()
                if pause_btn:
                    pause_btn.config(text="Pause Sync")
                messagebox.showinfo("Sync", "Sync resumed")
            else:
                sync_client.pause()
                if pause_btn:
                    pause_btn.config(text="Resume Sync")
                messagebox.showinfo("Sync", "Sync paused")
            update_status()
        else:
            messagebox.showinfo("Sync", "Sync client not connected")
    
    def stop_sync():
        if sync_client:
            if messagebox.askyesno("Stop Sync", "Are you sure you want to stop the sync client?"):
                sync_client.quit()
                root.quit()
        else:
            messagebox.showinfo("Sync", "Sync client not connected")
    
    # Helper function to create cyberpunk buttons
    def create_cyber_button(parent, text, command, bg_color=button_bg, fg_color=fg_color, hover_color=button_hover):
        btn_frame = tk.Frame(parent, bg=border_color)
        btn_frame.pack(pady=8, padx=5, fill=tk.X)
        
        btn = tk.Button(
            btn_frame,
            text=f"[ {text} ]",
            command=command,
            bg=bg_color,
            fg=fg_color,
            activebackground=hover_color,
            activeforeground=fg_color,
            font=("Courier New", 11, "bold"),
            width=25,
            height=2,
            relief=tk.FLAT,
            borderwidth=0,
            cursor="hand2"
        )
        btn.pack(fill=tk.BOTH, expand=True, padx=2, pady=2)
        
        # Hover effect simulation
        def on_enter(e):
            btn.config(bg=hover_color)
        def on_leave(e):
            btn.config(bg=bg_color)
        btn.bind("<Enter>", on_enter)
        btn.bind("<Leave>", on_leave)
        
        return btn
    
    # Buttons with cyberpunk styling
    buttons = [
        ("VIEW LOGS", open_logs, button_bg, fg_color),
        ("SETTINGS", open_settings, button_bg, accent_color),
        ("VIEW CONFLICTS", show_conflicts_gui, button_bg, warning_color),
        ("START SYNC", trigger_sync, button_bg, fg_color),
    ]
    
    for text, command, bg, fg in buttons:
        create_cyber_button(buttons_frame, text, command, bg, fg)
    
    # Pause/Resume button (dynamic text)
    pause_btn_text = "RESUME SYNC" if (sync_client and sync_client.is_paused) else "PAUSE SYNC"
    pause_btn = create_cyber_button(
        buttons_frame,
        pause_btn_text,
        toggle_pause,
        "#332200",
        warning_color,
        "#443300"
    )
    
    # Update status function that also updates pause button
    def update_status():
        update_status_base()
        if pause_btn and sync_client:
            if sync_client.is_paused:
                pause_btn.config(text="[ RESUME SYNC ]")
            else:
                pause_btn.config(text="[ PAUSE SYNC ]")
        # Schedule next update
        root.after(1000, update_status)
    
    update_status()  # Start status updates
    
    # Stop button (destructive action)
    stop_btn = create_cyber_button(
        buttons_frame,
        "STOP SYNC CLIENT",
        stop_sync,
        "#220000",
        error_color,
        "#330000"
    )
    
    # Footer with info
    footer_frame = tk.Frame(main_frame, bg=bg_color)
    footer_frame.pack(pady=15, fill=tk.X)
    
    info_text = tk.Label(
        footer_frame,
        text="[ SYSTEM READY ]",
        font=("Courier New", 8),
        bg=bg_color,
        fg="#003300"
    )
    info_text.pack()
    
    version_text = tk.Label(
        footer_frame,
        text="v1.0.0 | [ONLINE]",
        font=("Courier New", 7),
        bg=bg_color,
        fg="#001100"
    )
    version_text.pack()
    
    # Update canvas scroll region
    def update_scroll_region(event):
        canvas.configure(scrollregion=canvas.bbox("all"))
    
    main_frame.bind("<Configure>", update_scroll_region)
    
    return root


def show_status():
    """Show sync client status (CLI command)"""
    try:
        config = SyncConfig()
        print("PosterchanAI Sync Client Status")
        print("=" * 50)
        print(f"Server URL: {config.config.get('server_url', 'Not set')}")
        print(f"Sync Directory: {config.config.get('sync_dir', 'Not set')}")
        print(f"Username: {config.config.get('username', 'Not set (will be fetched)')}")
        print(f"Auto Sync: {config.config.get('auto_sync', True)}")
        print(f"Poll Interval: {config.config.get('poll_interval', 30)}s")
        
        # Check if service is running
        try:
            import subprocess
            result = subprocess.run(
                ["systemctl", "--user", "is-active", "posterchanai-sync"],
                capture_output=True,
                timeout=2
            )
            if result.returncode == 0:
                print("Service Status: Running")
            else:
                print("Service Status: Not running")
        except:
            print("Service Status: Unknown (systemctl not available)")
        
        print("\nView logs: journalctl --user -u posterchanai-sync -f")
        return True
    except Exception as e:
        print(f"Error getting status: {e}", file=sys.stderr)
        return False


def main():
    """Main entry point"""
    # Handle CLI commands
    if len(sys.argv) > 1:
        if sys.argv[1] == "--status":
            show_status()
            sys.exit(0)
        elif sys.argv[1] == "--gui":
            # Launch standalone GUI window
            if not GUI_AVAILABLE:
                print("GUI not available. Install tkinter to use the GUI interface.")
                sys.exit(1)
            config = SyncConfig()
            sync_client = SyncClient(config)
            root = create_main_gui_window(sync_client)
            if root:
                root.mainloop()
            sys.exit(0)
        elif sys.argv[1] in ["--help", "-h"]:
            print("PosterchanAI Sync Client")
            print("\nUsage:")
            print("  posterchanai-sync              Start the sync client (daemon)")
            print("  posterchanai-sync --gui        Open GUI window (standalone)")
            print("  posterchanai-sync --setup      Run setup wizard")
            print("  posterchanai-sync --status     Show status information")
            print("  posterchanai-sync --help       Show this help")
            sys.exit(0)
    
    # Check if setup is needed
    try:
        from setup_wizard import check_and_run_setup
        if not check_and_run_setup():
            # Setup was cancelled or failed
            logger.error("Setup cancelled or failed. Cannot start sync client.")
            logger.error("If running as a service, run 'posterchanai-sync --setup' manually first.")
            sys.exit(1)
    except ImportError:
        # Setup wizard not available, continue with existing config
        logger.warning("Setup wizard not available, continuing with existing config")
    except Exception as e:
        logger.error(f"Error during setup: {e}")
        logger.error("If running as a service, run 'posterchanai-sync --setup' manually first.")
        sys.exit(1)
    
    config = SyncConfig()
    sync_client = SyncClient(config)
    
    # Start file watcher
    sync_client.start_watcher()
    
    # Initial sync
    if config.config.get("auto_sync", True):
        threading.Thread(target=sync_client.sync_directory, daemon=True).start()
    
    # Process sync queue in background
    def process_queue():
        from queue import Empty
        while True:
            try:
                action, path = sync_client.sync_queue.get(timeout=1)
                if action == "sync":
                    sync_client.sync_file(Path(path))
                elif action == "delete":
                    # Handle deletion
                    try:
                        local_path = sync_client.sync_dir / path
                        if local_path.exists():
                            if local_path.is_file():
                                local_path.unlink()
                                logger.info(f"Deleted local file: {local_path}")
                            elif local_path.is_dir():
                                import shutil
                                shutil.rmtree(local_path)
                                logger.info(f"Deleted local directory: {local_path}")
                            
                            # Remove from state
                            rel_path = str(Path(path))
                            with sync_client._state_lock:
                                sync_client.state.pop(rel_path, None)
                            sync_client.save_state()
                            
                            # Delete from server
                            remote_path = path.replace('\\', '/')
                            sync_client.delete_remote_file(remote_path)
                    except Exception as e:
                        logger.error(f"Error deleting {path}: {e}")
                sync_client.sync_queue.task_done()
            except Empty:
                continue
            except Exception as e:
                logger.error(f"Error processing queue: {e}")
                continue
    
    threading.Thread(target=process_queue, daemon=True).start()
    
    # Periodic sync
    def periodic_sync():
        while True:
            time.sleep(config.config.get("poll_interval", 30))
            if not sync_client.is_paused and not sync_client.is_syncing:
                threading.Thread(target=sync_client.sync_directory, daemon=True).start()
    
    threading.Thread(target=periodic_sync, daemon=True).start()
    
    # Check if we should run in headless mode
    if is_headless_mode():
        logger.info("Running in headless mode (no GUI available)")
        logger.info("View logs with: journalctl --user -u posterchanai-sync -f")
        # Keep running until interrupted
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            logger.info("Shutting down...")
            sync_client.quit()
    else:
        # Initialize tkinter in main thread (required for GUI)
        if not GUI_AVAILABLE:
            logger.warning("GUI requested but tkinter not available. Running in headless mode.")
            try:
                while True:
                    time.sleep(1)
            except KeyboardInterrupt:
                sync_client.quit()
        else:
            root = tk.Tk()
            root.withdraw()  # Hide main window
            
            # Create and run tray icon (must run in main thread for tkinter compatibility)
            try:
                icon = create_tray_icon(sync_client)
                
                # Check what type of icon we got
                icon_type = type(icon).__name__
                logger.info(f"Created tray icon type: {icon_type}")
                
                # Run tray icon in separate thread, but keep tkinter root alive
                def run_tray():
                    try:
                        logger.info("Starting system tray icon...")
                        icon.run()
                    except (AssertionError, RuntimeError) as e:
                        # pystray assertion errors or Wayland tray errors - catch gracefully
                        error_msg = str(e)
                        logger.warning(f"System tray icon failed: {error_msg}")
                        if "Wayland" in error_msg or "StatusNotifier" in error_msg:
                            logger.info("Wayland tray icon failed. Use 'posterchanai-sync --gui' for GUI interface.")
                        else:
                            logger.info("If you're on Wayland, you may need StatusNotifierItem support.")
                            logger.info("Use 'posterchanai-sync --gui' for GUI interface or '--status' for CLI status.")
                    except Exception as e:
                        logger.warning(f"Tray icon failed to start: {e}")
                        import traceback
                        logger.debug(traceback.format_exc())
                        logger.info("Use 'posterchanai-sync --gui' for GUI interface or '--status' for CLI status.")
                
                tray_thread = threading.Thread(target=run_tray, daemon=True)
                tray_thread.start()
                
                # Give it a moment to start, then check if it's actually running
                time.sleep(2)
                if not tray_thread.is_alive():
                    logger.info("Tray icon thread exited. Use 'posterchanai-sync --gui' for GUI interface.")
                else:
                    logger.info("System tray icon started successfully")
            except RuntimeError as e:
                error_msg = str(e)
                logger.warning(f"Could not create tray icon: {error_msg}")
                if "Wayland" in error_msg:
                    logger.info("Wayland detected but tray icon creation failed. Use 'posterchanai-sync --gui' for GUI interface.")
                else:
                    logger.info("Use 'posterchanai-sync --gui' for GUI interface or '--status' for CLI status.")
            except Exception as e:
                logger.warning(f"Could not create tray icon: {e}")
                import traceback
                logger.debug(traceback.format_exc())
                logger.info("Use 'posterchanai-sync --gui' for GUI interface or '--status' for CLI status.")
            
            # Keep main thread alive for tkinter GUI components
            try:
                root.mainloop()
            except KeyboardInterrupt:
                sync_client.quit()


if __name__ == "__main__":
    main()
