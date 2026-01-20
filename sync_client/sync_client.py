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
                "**/Thumbs.db"
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
        """Add a conflict to the list"""
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
        return [c for c in self.conflicts if not c.get("resolved", False)]


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
        
        # Verify API connection
        if not self.verify_connection():
            logger.error("Cannot connect to PosterchanAI server. Check server_url and api_key in config.")
    
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
                if not self.username and "username" in data:
                    self.username = data["username"]
                return True
            return False
        except Exception as e:
            logger.error(f"Connection verification failed: {e}")
            return False
    
    def get_username_from_api(self) -> str:
        """Get username from API"""
        try:
            response = requests.get(
                f"{self.server_url}/api/auth/settings",
                headers=self.get_headers(),
                timeout=5
            )
            if response.status_code == 200:
                data = response.json()
                return data.get("username", "")
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
                    self.state = {
                        path: FileState(**data)
                        for path, data in state_data.items()
                    }
            except Exception as e:
                logger.error(f"Error loading state: {e}")
    
    def save_state(self):
        """Save sync state to file"""
        try:
            state_data = {
                path: asdict(state)
                for path, state in self.state.items()
            }
            with open(STATE_FILE, 'w') as f:
                json.dump(state_data, f, indent=2)
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
    
    def get_remote_file_info(self, remote_path: str) -> Optional[Dict]:
        """Get remote file information"""
        try:
            # Get directory listing
            dir_path = os.path.dirname(remote_path) if os.path.dirname(remote_path) else ""
            file_name = os.path.basename(remote_path)
            
            response = requests.get(
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
        except Exception as e:
            logger.error(f"Error getting remote file info: {e}")
        return None
    
    def upload_file(self, local_path: Path, remote_path: str) -> bool:
        """Upload file to server"""
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
                except:
                    pass  # Directory might already exist
            
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
                    logger.error(f"Upload failed: {response.status_code} - {response.text}")
        except Exception as e:
            logger.error(f"Error uploading {local_path}: {e}")
        return False
    
    def download_file(self, remote_path: str, local_path: Path) -> bool:
        """Download file from server"""
        try:
            local_path.parent.mkdir(parents=True, exist_ok=True)
            response = requests.get(
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
                        import os
                        os.utime(local_path, (remote_info["modified"], remote_info["modified"]))
                except:
                    pass
                logger.info(f"Downloaded: {remote_path} -> {local_path}")
                return True
            else:
                logger.error(f"Download failed: {response.status_code}")
        except Exception as e:
            logger.error(f"Error downloading {remote_path}: {e}")
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
        
        rel_path = local_path.relative_to(self.sync_dir)
        remote_path = str(rel_path).replace('\\', '/')
        
        stat = local_path.stat()
        local_mtime = stat.st_mtime
        local_size = stat.st_size
        local_hash = self.calculate_file_hash(local_path)
        
        # Get remote file info
        remote_info = self.get_remote_file_info(remote_path)
        
        # Check state
        state = self.state.get(str(rel_path))
        
        if remote_info:
            remote_mtime = remote_info.get("modified", remote_info.get("mtime", 0))
            remote_size = remote_info.get("size", 0)
            
            # Check for conflicts
            if state and state.synced:
                if abs(local_mtime - remote_mtime) > 1 and local_size != remote_size:
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
                    # "ask" - user will be notified via conflict handler
                    return
            
            # Upload if local is newer or doesn't exist remotely
            if not state or not state.synced or local_mtime > remote_mtime:
                if self.upload_file(local_path, remote_path):
                    self.state[str(rel_path)] = FileState(
                        path=str(rel_path),
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
        """Sync entire directory"""
        if self.is_paused:
            return
        
        self.is_syncing = True
        logger.info("Starting directory sync...")
        
        try:
            # Walk local directory
            for root, dirs, files in os.walk(self.sync_dir):
                # Filter excluded directories
                dirs[:] = [d for d in dirs if not self.should_exclude(Path(root) / d)]
                
                for file in files:
                    local_path = Path(root) / file
                    if not self.should_exclude(local_path):
                        self.sync_file(local_path)
        except Exception as e:
            logger.error(f"Error during directory sync: {e}")
        finally:
            self.is_syncing = False
            logger.info("Directory sync complete")
            # Send desktop notification if available (useful when tray icon doesn't work)
            send_notification("PosterchanAI Sync", "Directory sync complete", "info")
    
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


def create_tray_icon(sync_client: SyncClient) -> pystray.Icon:
    """Create system tray icon"""
    if not GUI_AVAILABLE:
        raise RuntimeError("Cannot create tray icon: GUI not available")
    # Create icon image (cyberpunk style)
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
    
    def show_logs(icon, item):
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
    
    def toggle_pause(icon, item):
        """Toggle pause/resume"""
        if sync_client.is_paused:
            sync_client.resume()
            item.text = "Pause Sync"
        else:
            sync_client.pause()
            item.text = "Resume Sync"
    
    def show_conflicts(icon, item):
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
    
    def show_settings(icon, item):
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
    window.geometry("800x600")
    
    # Cyberpunk theme colors
    bg_color = "#0a0a0a"
    fg_color = "#00ff00"
    text_bg = "#1a1a1a"
    
    window.configure(bg=bg_color)
    
    # Log text area
    text_area = scrolledtext.ScrolledText(
        window,
        bg=text_bg,
        fg=fg_color,
        font=("Courier", 10),
        insertbackground=fg_color,
        selectbackground="#003300"
    )
    text_area.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)
    
    # Read log file
    try:
        with open(log_file, 'r') as f:
            lines = f.readlines()
            text_area.insert(tk.END, ''.join(lines[-500:]))  # Last 500 lines
            text_area.see(tk.END)
    except Exception as e:
        text_area.insert(tk.END, f"Error reading log: {e}\n")
    
    # Refresh button
    def refresh_logs():
        text_area.delete(1.0, tk.END)
        try:
            with open(log_file, 'r') as f:
                lines = f.readlines()
                text_area.insert(tk.END, ''.join(lines[-500:]))
                text_area.see(tk.END)
        except Exception as e:
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
    
    return window


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
        elif sys.argv[1] in ["--help", "-h"]:
            print("PosterchanAI Sync Client")
            print("\nUsage:")
            print("  posterchanai-sync              Start the sync client")
            print("  posterchanai-sync --setup     Run setup wizard")
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
        while True:
            try:
                action, path = sync_client.sync_queue.get(timeout=1)
                if action == "sync":
                    sync_client.sync_file(Path(path))
                elif action == "delete":
                    # Handle deletion
                    pass
            except:
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
                
                # Run tray icon in separate thread, but keep tkinter root alive
                def run_tray():
                    try:
                        logger.info("Starting system tray icon...")
                        icon.run()
                    except Exception as e:
                        logger.warning(f"Tray icon failed to start: {e}")
                        logger.info("Note: System tray may not be supported on Wayland without StatusNotifierItem.")
                        logger.info("Continuing in headless mode. Use 'posterchanai-sync --status' for status.")
                
                tray_thread = threading.Thread(target=run_tray, daemon=True)
                tray_thread.start()
                
                # Give it a moment to start, then check if it's actually running
                time.sleep(0.5)
                if not tray_thread.is_alive():
                logger.warning("Tray icon thread exited immediately. System tray may not be available.")
                logger.info("On Wayland, pystray requires StatusNotifierItem support (e.g., waybar with tray support).")
                logger.info("Desktop notifications will be used as fallback. Use 'posterchanai-sync --status' for status.")
                send_notification("PosterchanAI Sync", "Running without system tray icon. Use 'posterchanai-sync --status' for status.", "info")
            except Exception as e:
                logger.warning(f"Could not create tray icon: {e}")
                logger.info("Running without system tray. Use 'posterchanai-sync --status' for status.")
                logger.info("View logs with: journalctl --user -u posterchanai-sync -f")
            
            # Keep main thread alive for tkinter GUI components
            try:
                root.mainloop()
            except KeyboardInterrupt:
                sync_client.quit()


if __name__ == "__main__":
    main()
