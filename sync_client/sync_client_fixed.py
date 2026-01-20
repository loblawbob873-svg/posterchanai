#!/usr/bin/env python3
"""
PosterchanAI Sync Client - Desktop sync daemon with system tray GUI
Syncs local directories with remote PosterchanAI storage

FIXED VERSION - Addresses code review issues
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
from queue import Queue, Empty
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler, FileSystemEvent
import pystray
from PIL import Image, ImageDraw
import tkinter as tk
from tkinter import scrolledtext, messagebox

# Setup logging with rotation
log_dir = Path.home() / ".config" / "posterchanai-sync" / "logs"
log_dir.mkdir(parents=True, exist_ok=True)
log_file = log_dir / f"sync_{datetime.now().strftime('%Y%m%d')}.log"

# Rotate old logs (keep last 7 days)
for old_log in log_dir.glob("sync_*.log"):
    try:
        if (datetime.now() - datetime.fromtimestamp(old_log.stat().st_mtime)).days > 7:
            old_log.unlink()
    except:
        pass

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

# Constants
DEBOUNCE_TIME = 2.0  # seconds
MAX_RETRIES = 3
RETRY_BACKOFF = 1.5  # exponential backoff multiplier
CONFLICT_TIME_THRESHOLD = 2.0  # seconds - time difference to consider conflict


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
        self.validate_config()
    
    def load_config(self) -> Dict:
        """Load configuration from file"""
        default_config = {
            "server_url": "http://localhost:8000",
            "api_key": "",
            "sync_dir": str(Path.home() / "PosterchanAI-Sync"),
            "exclude_patterns": [
                "**/.*",
                "**/__pycache__/**",
                "**/*.pyc",
                "**/node_modules/**",
                "**/.git/**",
                "**/.DS_Store",
                "**/Thumbs.db"
            ],
            "poll_interval": 30,
            "conflict_resolution": "ask",
            "auto_sync": True
        }
        
        if CONFIG_FILE.exists():
            try:
                with open(CONFIG_FILE, 'r') as f:
                    user_config = json.load(f)
                    default_config.update(user_config)
            except (json.JSONDecodeError, IOError) as e:
                logger.error(f"Error loading config: {e}")
        
        return default_config
    
    def validate_config(self):
        """Validate configuration values"""
        if not self.config.get("server_url"):
            raise ValueError("server_url is required in config")
        
        server_url = self.config.get("server_url", "")
        if not server_url.startswith(("http://", "https://")):
            raise ValueError("server_url must start with http:// or https://")
        
        if not self.config.get("api_key"):
            logger.warning("api_key is empty - sync will fail")
        
        sync_dir = Path(self.config.get("sync_dir", ""))
        if not sync_dir.exists():
            try:
                sync_dir.mkdir(parents=True, exist_ok=True)
            except Exception as e:
                raise ValueError(f"Cannot create sync_dir: {e}")
    
    def save_config(self):
        """Save configuration to file"""
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        try:
            with open(CONFIG_FILE, 'w') as f:
                json.dump(self.config, f, indent=2)
        except IOError as e:
            logger.error(f"Error saving config: {e}")
    
    def get_exclude_patterns(self) -> List[str]:
        """Get exclude patterns"""
        return self.config.get("exclude_patterns", [])


class ConflictHandler:
    """Handle file conflicts"""
    def __init__(self):
        self.conflicts: List[Dict] = []
        self.lock = threading.Lock()
        self.load_conflicts()
    
    def load_conflicts(self):
        """Load conflicts from file"""
        if CONFLICTS_FILE.exists():
            try:
                with open(CONFLICTS_FILE, 'r') as f:
                    self.conflicts = json.load(f)
            except (json.JSONDecodeError, IOError) as e:
                logger.error(f"Error loading conflicts: {e}")
    
    def save_conflicts(self):
        """Save conflicts to file"""
        try:
            # Atomic write
            temp_file = CONFLICTS_FILE.with_suffix('.tmp')
            with open(temp_file, 'w') as f:
                json.dump(self.conflicts, f, indent=2)
            temp_file.replace(CONFLICTS_FILE)
        except IOError as e:
            logger.error(f"Error saving conflicts: {e}")
    
    def add_conflict(self, file_path: str, local_mtime: float, remote_mtime: float, 
                     local_size: int, remote_size: int):
        """Add a conflict to the list"""
        with self.lock:
            conflict = {
                "path": file_path,
                "local_mtime": local_mtime,
                "remote_mtime": remote_mtime,
                "local_size": local_size,
                "remote_size": remote_size,
                "timestamp": time.time(),
                "resolved": False
            }
            self.conflicts.append(conflict)
            self.save_conflicts()
        logger.warning(f"CONFLICT: {file_path} - Local: {datetime.fromtimestamp(local_mtime)}, Remote: {datetime.fromtimestamp(remote_mtime)}")
    
    def get_unresolved_conflicts(self) -> List[Dict]:
        """Get unresolved conflicts"""
        with self.lock:
            return [c for c in self.conflicts if not c.get("resolved", False)]


class FileWatcher(FileSystemEventHandler):
    """Watch for file system changes with debouncing"""
    def __init__(self, sync_client):
        self.sync_client = sync_client
        self.pending_changes: Dict[str, float] = {}
        self.lock = threading.Lock()
        self.debounce_timer: Optional[threading.Timer] = None
    
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
        
        with self.lock:
            self.pending_changes[path] = (time.time(), deleted)
            
            # Cancel existing timer
            if self.debounce_timer:
                self.debounce_timer.cancel()
            
            # Create new timer
            self.debounce_timer = threading.Timer(DEBOUNCE_TIME, self._process_pending_changes)
            self.debounce_timer.start()
    
    def _process_pending_changes(self):
        """Process all pending changes after debounce"""
        with self.lock:
            changes = self.pending_changes.copy()
            self.pending_changes.clear()
            self.debounce_timer = None
        
        for path, (timestamp, deleted) in changes.items():
            if deleted:
                self.sync_client.queue_delete(path)
            else:
                self.sync_client.queue_sync(path)


class SyncClient:
    """Main sync client"""
    def __init__(self, config: SyncConfig):
        self.config = config
        self.api_key = config.config.get("api_key", "")
        self.server_url = config.config.get("server_url", "").rstrip('/')
        self.sync_dir = Path(config.config.get("sync_dir", ""))
        self.sync_dir.mkdir(parents=True, exist_ok=True)
        self.username = config.config.get("username", "")
        
        self.state: Dict[str, FileState] = {}
        self.state_lock = threading.Lock()
        self.load_state()
        
        self.observer: Optional[Observer] = None
        self.watcher: Optional[FileWatcher] = None
        self.is_paused = False
        self.is_syncing = False
        self.sync_queue = Queue()
        self.conflict_handler = ConflictHandler()
        
        self.log_window: Optional[tk.Toplevel] = None
        self.root: Optional[tk.Tk] = None  # Store root for GUI operations
        
        # Create requests session with retry
        self.session = requests.Session()
        retry_strategy = Retry(
            total=MAX_RETRIES,
            backoff_factor=RETRY_BACKOFF,
            status_forcelist=[429, 500, 502, 503, 504]
        )
        adapter = HTTPAdapter(max_retries=retry_strategy)
        self.session.mount("http://", adapter)
        self.session.mount("https://", adapter)
        
        # Get username from API if not in config
        if not self.username:
            self.username = self.get_username_from_api()
        
        if not self.username:
            raise ValueError("Cannot determine username. Set in config or ensure API key is valid.")
        
        # Verify API connection
        if not self.verify_connection():
            logger.error("Cannot connect to PosterchanAI server. Check server_url and api_key in config.")
    
    def verify_connection(self) -> bool:
        """Verify API connection"""
        try:
            response = self.session.get(
                f"{self.server_url}/api/auth/settings",
                headers=self.get_headers(),
                timeout=5
            )
            if response.status_code == 200:
                data = response.json()
                if not self.username and "username" in data:
                    self.username = data["username"]
                return True
            return False
        except (requests.RequestException, ValueError) as e:
            logger.error(f"Connection verification failed: {e}")
            return False
    
    def get_username_from_api(self) -> str:
        """Get username from API"""
        try:
            response = self.session.get(
                f"{self.server_url}/api/auth/settings",
                headers=self.get_headers(),
                timeout=5
            )
            if response.status_code == 200:
                data = response.json()
                return data.get("username", "")
        except (requests.RequestException, ValueError) as e:
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
                    with self.state_lock:
                        self.state = {
                            path: FileState(**data)
                            for path, data in state_data.items()
                        }
            except (json.JSONDecodeError, IOError, TypeError) as e:
                logger.error(f"Error loading state: {e}")
    
    def save_state(self):
        """Save sync state to file (atomic write)"""
        try:
            with self.state_lock:
                state_data = {
                    path: asdict(state)
                    for path, state in self.state.items()
                }
            
            # Atomic write
            temp_file = STATE_FILE.with_suffix('.tmp')
            with open(temp_file, 'w') as f:
                json.dump(state_data, f, indent=2)
            temp_file.replace(STATE_FILE)
        except IOError as e:
            logger.error(f"Error saving state: {e}")
    
    def calculate_file_hash(self, file_path: Path) -> str:
        """Calculate MD5 hash of file"""
        hash_md5 = hashlib.md5()
        try:
            with open(file_path, "rb") as f:
                for chunk in iter(lambda: f.read(4096), b""):
                    hash_md5.update(chunk)
            return hash_md5.hexdigest()
        except IOError as e:
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
    
    def get_remote_file_info(self, remote_path: str) -> Optional[Dict]:
        """Get remote file information"""
        try:
            dir_path = os.path.dirname(remote_path) if os.path.dirname(remote_path) else ""
            file_name = os.path.basename(remote_path)
            
            response = self.session.get(
                f"{self.server_url}/api/storage/list-files",
                params={"username": self.username, "path": dir_path},
                headers=self.get_headers(),
                timeout=10
            )
            if response.status_code == 200:
                files = response.json()
                for file_info in files:
                    if file_info.get("name") == file_name:
                        return file_info
        except (requests.RequestException, ValueError) as e:
            logger.error(f"Error getting remote file info: {e}")
        return None
    
    def upload_file(self, local_path: Path, remote_path: str) -> bool:
        """Upload file to server"""
        try:
            # Ensure parent directory exists on server
            dir_path = os.path.dirname(remote_path)
            if dir_path:
                try:
                    self.session.post(
                        f"{self.server_url}/api/storage/mkdir",
                        data={"username": self.username, "path": dir_path},
                        headers=self.get_headers(),
                        timeout=10
                    )
                except requests.RequestException:
                    pass  # Directory might already exist
            
            with open(local_path, 'rb') as f:
                files = {'file': (local_path.name, f, 'application/octet-stream')}
                data = {'username': self.username, 'path': remote_path}
                response = self.session.post(
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
                    logger.error(f"Upload failed: {response.status_code} - {response.text}")
        except (IOError, requests.RequestException) as e:
            logger.error(f"Error uploading {local_path}: {e}")
        return False
    
    def download_file(self, remote_path: str, local_path: Path) -> bool:
        """Download file from server"""
        try:
            local_path.parent.mkdir(parents=True, exist_ok=True)
            response = self.session.get(
                f"{self.server_url}/api/storage/view-file",
                params={"username": self.username, "file_path": remote_path},
                headers=self.get_headers(),
                timeout=60,
                stream=True
            )
            if response.status_code == 200:
                with open(local_path, 'wb') as f:
                    for chunk in response.iter_content(chunk_size=8192):
                        f.write(chunk)
                # Update mtime to match remote
                try:
                    remote_info = self.get_remote_file_info(remote_path)
                    if remote_info and remote_info.get("modified"):
                        os.utime(local_path, (remote_info["modified"], remote_info["modified"]))
                except Exception:
                    pass
                logger.info(f"Downloaded: {remote_path} -> {local_path}")
                return True
            else:
                logger.error(f"Download failed: {response.status_code}")
        except (IOError, requests.RequestException) as e:
            logger.error(f"Error downloading {remote_path}: {e}")
        return False
    
    def delete_remote_file(self, remote_path: str) -> bool:
        """Delete file from server"""
        try:
            response = self.session.delete(
                f"{self.server_url}/api/storage/delete-file",
                params={"username": self.username, "file_path": remote_path},
                headers=self.get_headers(),
                timeout=10
            )
            if response.status_code == 200:
                logger.info(f"Deleted remote: {remote_path}")
                return True
        except requests.RequestException as e:
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
        
        # Validate path is within sync_dir
        try:
            rel_path = local_path.relative_to(self.sync_dir)
        except ValueError:
            logger.warning(f"File outside sync_dir: {local_path}")
            return
        
        remote_path = str(rel_path).replace('\\', '/')
        
        try:
            stat = local_path.stat()
        except OSError as e:
            logger.error(f"Cannot stat file {local_path}: {e}")
            return
        
        local_mtime = stat.st_mtime
        local_size = stat.st_size
        
        # Only calculate hash if needed (check state first)
        state_key = str(rel_path)
        with self.state_lock:
            state = self.state.get(state_key)
            if state and state.mtime == local_mtime and state.size == local_size:
                # File unchanged, skip hash calculation
                local_hash = state.hash
            else:
                local_hash = self.calculate_file_hash(local_path)
        
        # Get remote file info
        remote_info = self.get_remote_file_info(remote_path)
        
        if remote_info:
            remote_mtime = remote_info.get("modified", remote_info.get("mtime", 0))
            remote_size = remote_info.get("size", 0)
            
            # Check for conflicts using hash comparison (more reliable)
            if state and state.synced and state.hash:
                if local_hash and local_hash != state.hash:
                    # Local changed
                    if abs(local_mtime - remote_mtime) > CONFLICT_TIME_THRESHOLD and local_size != remote_size:
                        # Conflict detected
                        self.conflict_handler.add_conflict(
                            state_key, local_mtime, remote_mtime, local_size, remote_size
                        )
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
                        return
            
            # Upload if local is newer or doesn't exist remotely
            if not state or not state.synced or local_mtime > remote_mtime:
                if self.upload_file(local_path, remote_path):
                    with self.state_lock:
                        self.state[state_key] = FileState(
                            path=state_key,
                            mtime=local_mtime,
                            size=local_size,
                            hash=local_hash,
                            synced=True,
                            last_sync=time.time()
                        )
                    self.save_state()
        else:
            # File doesn't exist remotely - upload it
            if self.upload_file(local_path, remote_path):
                with self.state_lock:
                    self.state[state_key] = FileState(
                        path=state_key,
                        mtime=local_mtime,
                        size=local_size,
                        hash=local_hash,
                        synced=True,
                        last_sync=time.time()
                    )
                self.save_state()
    
    def delete_file(self, local_path: Path):
        """Handle file deletion"""
        try:
            rel_path = local_path.relative_to(self.sync_dir)
        except ValueError:
            return
        
        remote_path = str(rel_path).replace('\\', '/')
        
        # Delete from remote
        if self.delete_remote_file(remote_path):
            # Remove from state
            with self.state_lock:
                self.state.pop(str(rel_path), None)
            self.save_state()
            logger.info(f"Deleted: {remote_path}")
    
    def sync_directory(self):
        """Sync entire directory"""
        if self.is_paused:
            return
        
        self.is_syncing = True
        logger.info("Starting directory sync...")
        
        try:
            for root, dirs, files in os.walk(self.sync_dir):
                dirs[:] = [d for d in dirs if not self.should_exclude(Path(root) / d)]
                
                for file in files:
                    local_path = Path(root) / file
                    if not self.should_exclude(local_path):
                        self.sync_file(local_path)
        except Exception as e:
            logger.error(f"Error during directory sync: {e}", exc_info=True)
        finally:
            self.is_syncing = False
            logger.info("Directory sync complete")
    
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
        if self.session:
            self.session.close()
        sys.exit(0)
    
    def schedule_gui_operation(self, func, *args, **kwargs):
        """Schedule GUI operation on main thread"""
        if self.root:
            self.root.after(0, lambda: func(*args, **kwargs))


def create_tray_icon(sync_client: SyncClient) -> pystray.Icon:
    """Create system tray icon"""
    width = height = 64
    image = Image.new('RGB', (width, height), color='#000000')
    draw = ImageDraw.Draw(image)
    
    draw.ellipse([10, 10, 54, 54], fill='#00ff00', outline='#00ffff', width=2)
    draw.ellipse([20, 20, 44, 44], fill='#000000', outline='#00ff00', width=1)
    draw.line([32, 10, 32, 54], fill='#00ffff', width=2)
    draw.line([10, 32, 54, 32], fill='#00ffff', width=2)
    
    def show_logs(icon, item):
        """Show log window (scheduled on main thread)"""
        sync_client.schedule_gui_operation(
            lambda: sync_client.log_window if sync_client.log_window and sync_client.log_window.winfo_exists() 
            else create_log_window(sync_client)
        )
    
    def toggle_pause(icon, item):
        """Toggle pause/resume"""
        if sync_client.is_paused:
            sync_client.resume()
            item.text = "Pause Sync"
        else:
            sync_client.pause()
            item.text = "Resume Sync"
    
    def show_conflicts(icon, item):
        """Show conflicts (scheduled on main thread)"""
        def show():
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
        sync_client.schedule_gui_operation(show)
    
    def show_settings(icon, item):
        """Show settings wizard (scheduled on main thread)"""
        def show():
            try:
                from setup_wizard import check_and_run_setup
                # Run wizard in a separate thread to avoid blocking
                def run_wizard():
                    if check_and_run_setup(force=True):
                        # Config was updated, reload it
                        # Reload config from file
                        sync_client.config = SyncConfig()
                        sync_client.api_key = sync_client.config.config.get("api_key", "")
                        sync_client.server_url = sync_client.config.config.get("server_url", "").rstrip('/')
                        sync_client.sync_dir = Path(sync_client.config.config.get("sync_dir", ""))
                        sync_client.sync_dir.mkdir(parents=True, exist_ok=True)
                        
                        # Update session headers
                        sync_client.session.headers.update(sync_client.get_headers())
                        
                        # Get username if changed
                        if not sync_client.username:
                            sync_client.username = sync_client.get_username_from_api()
                        
                        # Verify connection
                        def show_result():
                            if sync_client.verify_connection():
                                messagebox.showinfo("Settings", "Configuration updated successfully!")
                            else:
                                messagebox.showwarning("Settings", "Configuration saved but cannot verify connection. Check your settings.")
                        sync_client.schedule_gui_operation(show_result)
                    else:
                        def show_cancelled():
                            messagebox.showinfo("Settings", "Configuration update cancelled.")
                        sync_client.schedule_gui_operation(show_cancelled)
                
                threading.Thread(target=run_wizard, daemon=True).start()
            except ImportError:
                messagebox.showerror("Error", "Setup wizard not available")
            except Exception as e:
                logger.error(f"Error opening settings: {e}")
                messagebox.showerror("Error", f"Failed to open settings: {e}")
        sync_client.schedule_gui_operation(show)
    
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


def create_log_window(sync_client: SyncClient) -> tk.Toplevel:
    """Create log viewer window"""
    window = tk.Toplevel()
    window.title("PosterchanAI Sync - Logs")
    window.geometry("800x600")
    
    bg_color = "#0a0a0a"
    fg_color = "#00ff00"
    text_bg = "#1a1a1a"
    
    window.configure(bg=bg_color)
    
    text_area = scrolledtext.ScrolledText(
        window,
        bg=text_bg,
        fg=fg_color,
        font=("Courier", 10),
        insertbackground=fg_color,
        selectbackground="#003300"
    )
    text_area.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)
    
    try:
        with open(log_file, 'r') as f:
            lines = f.readlines()
            text_area.insert(tk.END, ''.join(lines[-500:]))
            text_area.see(tk.END)
    except IOError as e:
        text_area.insert(tk.END, f"Error reading log: {e}\n")
    
    def refresh_logs():
        text_area.delete(1.0, tk.END)
        try:
            with open(log_file, 'r') as f:
                lines = f.readlines()
                text_area.insert(tk.END, ''.join(lines[-500:]))
                text_area.see(tk.END)
        except IOError as e:
            text_area.insert(tk.END, f"Error reading log: {e}\n")
    
    refresh_btn = tk.Button(
        window,
        text="Refresh",
        command=refresh_logs,
        bg="#003300",
        fg=fg_color,
        activebackground="#004400",
        activeforeground=fg_color,
        font=("Arial", 10, "bold")
    )
    refresh_btn.pack(pady=5)
    
    sync_client.log_window = window
    return window


def main():
    """Main entry point"""
    # Check if setup is needed
    try:
        from setup_wizard import check_and_run_setup
        if not check_and_run_setup():
            # Setup was cancelled or failed
            logger.error("Setup cancelled or failed. Cannot start sync client.")
            sys.exit(1)
    except ImportError:
        # Setup wizard not available, continue with existing config
        pass
    except Exception as e:
        logger.error(f"Error during setup: {e}")
        sys.exit(1)
    
    try:
        config = SyncConfig()
    except ValueError as e:
        logger.error(f"Configuration error: {e}")
        sys.exit(1)
    
    try:
        sync_client = SyncClient(config)
    except ValueError as e:
        logger.error(f"Initialization error: {e}")
        sys.exit(1)
    
    sync_client.start_watcher()
    
    if config.config.get("auto_sync", True):
        threading.Thread(target=sync_client.sync_directory, daemon=True).start()
    
    def process_queue():
        while True:
            try:
                action, path = sync_client.sync_queue.get(timeout=1)
                if action == "sync":
                    sync_client.sync_file(Path(path))
                elif action == "delete":
                    sync_client.delete_file(Path(path))
            except Empty:
                continue
            except Exception as e:
                logger.error(f"Error processing queue: {e}", exc_info=True)
    
    threading.Thread(target=process_queue, daemon=True).start()
    
    def periodic_sync():
        while True:
            time.sleep(config.config.get("poll_interval", 30))
            if not sync_client.is_paused and not sync_client.is_syncing:
                threading.Thread(target=sync_client.sync_directory, daemon=True).start()
    
    threading.Thread(target=periodic_sync, daemon=True).start()
    
    root = tk.Tk()
    root.withdraw()
    sync_client.root = root
    
    icon = create_tray_icon(sync_client)
    
    def run_tray():
        icon.run()
    
    tray_thread = threading.Thread(target=run_tray, daemon=True)
    tray_thread.start()
    
    try:
        root.mainloop()
    except KeyboardInterrupt:
        sync_client.quit()


if __name__ == "__main__":
    main()
