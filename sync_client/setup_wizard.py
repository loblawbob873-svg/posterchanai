#!/usr/bin/env python3
"""
Setup Wizard for PosterchanAI Sync Client
Prompts user for server URL and API key on first run
"""
import os
import sys
import json
from pathlib import Path

# Try to import tkinter for GUI mode
try:
    import tkinter as tk
    from tkinter import ttk, messagebox
    GUI_AVAILABLE = True
except ImportError:
    GUI_AVAILABLE = False

CONFIG_DIR = Path.home() / ".config" / "posterchanai-sync"
CONFIG_FILE = CONFIG_DIR / "config.json"


def setup_cli() -> bool:
    """Command-line setup wizard (fallback when GUI is not available)"""
    # Check if we have an interactive terminal
    try:
        if not os.isatty(sys.stdin.fileno()):
            print("ERROR: Not running in an interactive terminal.", file=sys.stderr)
            print("Cannot run interactive setup. Please create config file manually:", file=sys.stderr)
            print(f"  {CONFIG_FILE}", file=sys.stderr)
            return False
    except:
        print("ERROR: Cannot access stdin. Please create config file manually:", file=sys.stderr)
        print(f"  {CONFIG_FILE}", file=sys.stderr)
        return False
    
    print("\n" + "="*60)
    print("PosterchanAI Sync Client - Setup")
    print("="*60 + "\n")
    
    # Load existing config if it exists
    existing_url = "http://localhost:8000"
    existing_key = ""
    existing_dir = str(Path.home() / "PosterchanAI-Sync")
    
    if CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE, 'r') as f:
                existing_config = json.load(f)
                existing_url = existing_config.get("server_url", existing_url)
                existing_key = existing_config.get("api_key", existing_key)
                existing_dir = existing_config.get("sync_dir", existing_dir)
        except:
            pass
    
    # Get server URL
    print("Enter your PosterchanAI server details:")
    print()
    url = input(f"Server URL [{existing_url}]: ").strip()
    if not url:
        url = existing_url
    
    if not url.startswith(("http://", "https://")):
        print("ERROR: Server URL must start with http:// or https://", file=sys.stderr)
        return False
    
    # Get API key
    import getpass
    if existing_key:
        key = getpass.getpass("API Key [leave blank to keep existing]: ").strip()
        if not key:
            key = existing_key
    else:
        key = getpass.getpass("API Key: ").strip()
    
    if not key:
        print("ERROR: API Key is required", file=sys.stderr)
        return False
    
    # Try to fetch username from API to verify connection
    username = ""
    print("\nVerifying API connection...")
    try:
        import requests
        response = requests.get(
            f"{url.rstrip('/')}/api/auth/settings",
            headers={"Authorization": f"Bearer {key}"},
            timeout=5
        )
        if response.status_code == 200:
            data = response.json()
            username = data.get("username", "")
            if username:
                print(f"✓ Connected! Username: {username}")
            else:
                print("✓ Connected (username will be fetched automatically)")
        else:
            print(f"⚠ Warning: API returned status {response.status_code}")
            print("  Username will be fetched automatically when sync starts")
    except Exception as e:
        print(f"⚠ Warning: Could not verify connection: {e}")
        print("  Username will be fetched automatically when sync starts")
    
    # Get sync directory
    sync_dir = input(f"Sync Directory [{existing_dir}]: ").strip()
    if not sync_dir:
        sync_dir = existing_dir
    
    # Expand ~ in path
    sync_dir = os.path.expanduser(sync_dir)
    
    # Try to create directory
    try:
        Path(sync_dir).mkdir(parents=True, exist_ok=True)
    except Exception as e:
        print(f"ERROR: Cannot create sync directory: {e}", file=sys.stderr)
        return False
    
    # Create config
    default_config = {
        "server_url": url.rstrip('/'),
        "api_key": key,
        "username": username,  # Use fetched username if available, otherwise empty (will be auto-fetched)
        "sync_dir": sync_dir,
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
    
    # Merge with existing config if it exists
    if CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE, 'r') as f:
                existing = json.load(f)
                default_config.update(existing)
                default_config["server_url"] = url.rstrip('/')
                default_config["api_key"] = key
                default_config["sync_dir"] = sync_dir
        except:
            pass
    
    # Save config
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    try:
        with open(CONFIG_FILE, 'w') as f:
            json.dump(default_config, f, indent=2)
        print("\n✓ Configuration saved successfully!")
        print(f"  Config file: {CONFIG_FILE}")
        print(f"  Server URL: {url}")
        if username:
            print(f"  Username: {username}")
        else:
            print(f"  Username: (will be fetched automatically)")
        print(f"  Sync Directory: {sync_dir}")
        print()
        return True
    except Exception as e:
        print(f"ERROR: Failed to save configuration: {e}", file=sys.stderr)
        return False


class SetupWizard:
    """First-run setup wizard (GUI mode)"""
    def __init__(self):
        if not GUI_AVAILABLE:
            raise RuntimeError("GUI not available. Use setup_cli() instead.")
        self.root = tk.Tk()
        self.root.title("PosterchanAI Sync - Setup")
        self.root.geometry("600x500")
        self.root.resizable(False, False)
        
        # Cyberpunk theme colors
        bg_color = "#0a0a0a"
        fg_color = "#00ff00"
        accent_color = "#00ffff"
        entry_bg = "#1a1a1a"
        
        self.root.configure(bg=bg_color)
        
        # Check if editing existing config
        self.is_editing = CONFIG_FILE.exists()
        
        # Try to load existing config values
        existing_url = "http://localhost:8000"
        existing_key = ""
        existing_dir = str(Path.home() / "PosterchanAI-Sync")
        
        if self.is_editing:
            try:
                with open(CONFIG_FILE, 'r') as f:
                    existing_config = json.load(f)
                    existing_url = existing_config.get("server_url", existing_url)
                    existing_key = existing_config.get("api_key", existing_key)
                    existing_dir = existing_config.get("sync_dir", existing_dir)
            except:
                pass
        
        # Title
        title_text = "◈ POSTERCHANAI SYNC SETTINGS ◈" if self.is_editing else "◈ POSTERCHANAI SYNC SETUP ◈"
        title_label = tk.Label(
            self.root,
            text=title_text,
            font=("Arial", 18, "bold"),
            bg=bg_color,
            fg=accent_color
        )
        title_label.pack(pady=20)
        
        # Instructions
        info_text = "Update your PosterchanAI server settings" if self.is_editing else "Enter your PosterchanAI server details to begin syncing"
        info_label = tk.Label(
            self.root,
            text=info_text,
            font=("Arial", 10),
            bg=bg_color,
            fg=fg_color
        )
        info_label.pack(pady=10)
        
        # Frame for inputs
        input_frame = tk.Frame(self.root, bg=bg_color)
        input_frame.pack(pady=20, padx=40, fill=tk.BOTH, expand=True)
        
        # Server URL
        url_label = tk.Label(
            input_frame,
            text="Server URL:",
            font=("Arial", 10, "bold"),
            bg=bg_color,
            fg=fg_color,
            anchor="w"
        )
        url_label.pack(fill=tk.X, pady=(0, 5))
        
        self.url_entry = tk.Entry(
            input_frame,
            font=("Arial", 11),
            bg=entry_bg,
            fg=fg_color,
            insertbackground=fg_color,
            relief=tk.SOLID,
            borderwidth=1
        )
        self.url_entry.pack(fill=tk.X, pady=(0, 15))
        self.url_entry.insert(0, existing_url)
        
        # API Key
        key_label = tk.Label(
            input_frame,
            text="API Key:",
            font=("Arial", 10, "bold"),
            bg=bg_color,
            fg=fg_color,
            anchor="w"
        )
        key_label.pack(fill=tk.X, pady=(0, 5))
        
        self.key_entry = tk.Entry(
            input_frame,
            font=("Arial", 11),
            bg=entry_bg,
            fg=fg_color,
            insertbackground=fg_color,
            show="*",
            relief=tk.SOLID,
            borderwidth=1
        )
        self.key_entry.pack(fill=tk.X, pady=(0, 15))
        if existing_key:
            self.key_entry.insert(0, existing_key)
        
        # Help text
        help_text = tk.Text(
            input_frame,
            height=4,
            font=("Arial", 9),
            bg=entry_bg,
            fg="#888888",
            relief=tk.SOLID,
            borderwidth=1,
            wrap=tk.WORD
        )
        help_text.insert("1.0", "To get your API key:\n1. Log into PosterchanAI web interface\n2. Go to Settings → API Keys\n3. Create a new API key and copy it here")
        help_text.config(state=tk.DISABLED)
        help_text.pack(fill=tk.X, pady=(0, 15))
        
        # Sync Directory
        dir_label = tk.Label(
            input_frame,
            text="Sync Directory:",
            font=("Arial", 10, "bold"),
            bg=bg_color,
            fg=fg_color,
            anchor="w"
        )
        dir_label.pack(fill=tk.X, pady=(0, 5))
        
        dir_frame = tk.Frame(input_frame, bg=bg_color)
        dir_frame.pack(fill=tk.X, pady=(0, 15))
        
        self.dir_entry = tk.Entry(
            dir_frame,
            font=("Arial", 11),
            bg=entry_bg,
            fg=fg_color,
            insertbackground=fg_color,
            relief=tk.SOLID,
            borderwidth=1
        )
        self.dir_entry.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self.dir_entry.insert(0, existing_dir)
        
        browse_btn = tk.Button(
            dir_frame,
            text="Browse...",
            command=self.browse_directory,
            bg="#003300",
            fg=fg_color,
            activebackground="#004400",
            activeforeground=fg_color,
            font=("Arial", 9),
            relief=tk.SOLID,
            borderwidth=1
        )
        browse_btn.pack(side=tk.LEFT, padx=(5, 0))
        
        # Buttons
        button_frame = tk.Frame(self.root, bg=bg_color)
        button_frame.pack(pady=20)
        
        cancel_btn = tk.Button(
            button_frame,
            text="Cancel",
            command=self.cancel,
            bg="#330000",
            fg="#ff0000",
            activebackground="#440000",
            activeforeground="#ff0000",
            font=("Arial", 10, "bold"),
            width=12,
            relief=tk.SOLID,
            borderwidth=1
        )
        cancel_btn.pack(side=tk.LEFT, padx=10)
        
        save_text = "Save" if self.is_editing else "Save & Start"
        save_btn = tk.Button(
            button_frame,
            text=save_text,
            command=self.save_config,
            bg="#003300",
            fg=fg_color,
            activebackground="#004400",
            activeforeground=fg_color,
            font=("Arial", 10, "bold"),
            width=12,
            relief=tk.SOLID,
            borderwidth=1
        )
        save_btn.pack(side=tk.LEFT, padx=10)
        
        # Focus on URL entry
        self.url_entry.focus()
        self.url_entry.select_range(0, tk.END)
        
        # Center window
        self.root.update_idletasks()
        x = (self.root.winfo_screenwidth() // 2) - (self.root.winfo_width() // 2)
        y = (self.root.winfo_screenheight() // 2) - (self.root.winfo_height() // 2)
        self.root.geometry(f"+{x}+{y}")
        
        self.config_saved = False
    
    def browse_directory(self):
        """Browse for sync directory"""
        from tkinter import filedialog
        directory = filedialog.askdirectory(
            title="Select Sync Directory",
            initialdir=self.dir_entry.get()
        )
        if directory:
            self.dir_entry.delete(0, tk.END)
            self.dir_entry.insert(0, directory)
    
    def validate_inputs(self) -> bool:
        """Validate user inputs"""
        url = self.url_entry.get().strip()
        api_key = self.key_entry.get().strip()
        sync_dir = self.dir_entry.get().strip()
        
        if not url:
            messagebox.showerror("Error", "Server URL is required")
            return False
        
        if not url.startswith(("http://", "https://")):
            messagebox.showerror("Error", "Server URL must start with http:// or https://")
            return False
        
        if not api_key:
            messagebox.showerror("Error", "API Key is required")
            return False
        
        if not sync_dir:
            messagebox.showerror("Error", "Sync directory is required")
            return False
        
        # Try to create directory
        try:
            Path(sync_dir).mkdir(parents=True, exist_ok=True)
        except Exception as e:
            messagebox.showerror("Error", f"Cannot create sync directory: {e}")
            return False
        
        return True
    
    def save_config(self):
        """Save configuration and close wizard"""
        if not self.validate_inputs():
            return
        
        url = self.url_entry.get().strip().rstrip('/')
        api_key = self.key_entry.get().strip()
        sync_dir = self.dir_entry.get().strip()
        
        # Try to fetch username from API to verify connection
        username = ""
        try:
            import requests
            response = requests.get(
                f"{url}/api/auth/settings",
                headers={"Authorization": f"Bearer {api_key}"},
                timeout=5
            )
            if response.status_code == 200:
                data = response.json()
                username = data.get("username", "")
        except:
            pass  # Username will be fetched automatically when sync starts
        
        # Load existing config or create default
        default_config = {
            "server_url": url,
            "api_key": api_key,
            "username": username,  # Use fetched username if available
            "sync_dir": sync_dir,
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
        
        # Merge with existing config if it exists
        if CONFIG_FILE.exists():
            try:
                with open(CONFIG_FILE, 'r') as f:
                    existing = json.load(f)
                    default_config.update(existing)
                    # Update only the fields we're setting
                    default_config["server_url"] = url
                    default_config["api_key"] = api_key
                    default_config["username"] = username  # Update username if fetched
                    default_config["sync_dir"] = sync_dir
            except:
                pass
        
        # Save config
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        try:
            with open(CONFIG_FILE, 'w') as f:
                json.dump(default_config, f, indent=2)
            self.config_saved = True
            self.root.destroy()
        except Exception as e:
            messagebox.showerror("Error", f"Failed to save configuration: {e}")
    
    def cancel(self):
        """Cancel setup"""
        if messagebox.askyesno("Cancel Setup", "Are you sure you want to cancel? The sync client cannot run without configuration."):
            self.root.destroy()
            exit(0)
    
    def run(self) -> bool:
        """Run the wizard and return True if config was saved"""
        self.root.mainloop()
        return self.config_saved


def is_interactive_environment() -> bool:
    """Check if we're running in an interactive environment with GUI access"""
    # Check if we're in a systemd service (most reliable indicator)
    if os.environ.get("INVOCATION_ID") or os.environ.get("JOURNAL_STREAM"):
        return False
    
    # Check if we have a TTY (interactive terminal) - if not, likely a service
    try:
        if not os.isatty(sys.stdin.fileno()):
            return False
    except:
        # If we can't check stdin, assume non-interactive
        return False
    
    # Check if DISPLAY is set
    display = os.environ.get("DISPLAY")
    if not display:
        return False
    
    # Try to check if X server is accessible (optional check)
    # If xdpyinfo is available, use it; otherwise assume DISPLAY is valid
    try:
        import subprocess
        result = subprocess.run(
            ["xdpyinfo", "-display", display],
            capture_output=True,
            timeout=1
        )
        return result.returncode == 0
    except (subprocess.TimeoutExpired, FileNotFoundError):
        # xdpyinfo not available or timed out, but DISPLAY is set and we have TTY
        # Assume it's interactive (tkinter will fail gracefully if not)
        return True
    except Exception:
        # Other error, assume non-interactive to be safe
        return False


def check_and_run_setup(force: bool = False) -> bool:
    """Check if setup is needed and run wizard if so
    
    Returns:
        True if setup is complete/not needed, False if setup failed or was cancelled
    """
    # Check if setup is needed (unless forced)
    setup_needed = True
    if not force:
        if CONFIG_FILE.exists():
            try:
                with open(CONFIG_FILE, 'r') as f:
                    config = json.load(f)
                    # Check if we have the minimum required fields
                    if config.get("server_url") and config.get("api_key"):
                        return True  # Setup not needed, config is valid
            except:
                pass  # Config file exists but invalid, need setup
    
    # If setup is needed but we're not in an interactive environment, fail gracefully
    if setup_needed and not is_interactive_environment():
        error_msg = f"""ERROR: Configuration file not found and cannot show setup wizard.
The sync client is running in a non-interactive environment (systemd service).

Please run the setup wizard manually first:
  ~/.local/bin/posterchanai-sync --setup

Or create the config file manually at:
  {CONFIG_FILE}

Example config.json:
  {{
    "server_url": "http://localhost:8000",
    "api_key": "your-api-key-here",
    "sync_dir": "~/PosterchanAI-Sync"
  }}
"""
        print(error_msg, file=sys.stderr)
        return False
    
    # Run setup wizard - use CLI if GUI is not available
    if not GUI_AVAILABLE:
        return setup_cli()
    
    # Use GUI wizard
    wizard = SetupWizard()
    return wizard.run()


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="PosterchanAI Sync Client Setup Wizard")
    parser.add_argument("--force", action="store_true", help="Force setup even if config exists")
    parser.add_argument("--cli", action="store_true", help="Force command-line mode (skip GUI)")
    args = parser.parse_args()
    
    # If CLI flag is set or GUI is not available, use CLI mode
    if args.cli or not GUI_AVAILABLE:
        if not GUI_AVAILABLE:
            print("Note: GUI not available, using command-line mode.\n", file=sys.stderr)
        success = setup_cli()
        sys.exit(0 if success else 1)
    
    # Otherwise use normal check_and_run_setup which will choose GUI or CLI
    success = check_and_run_setup(force=args.force)
    sys.exit(0 if success else 1)
