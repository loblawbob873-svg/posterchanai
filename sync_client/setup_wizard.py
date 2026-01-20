#!/usr/bin/env python3
"""
Setup Wizard for PosterchanAI Sync Client
Prompts user for server URL and API key on first run
"""
import tkinter as tk
from tkinter import ttk, messagebox
import json
from pathlib import Path

CONFIG_DIR = Path.home() / ".config" / "posterchanai-sync"
CONFIG_FILE = CONFIG_DIR / "config.json"


class SetupWizard:
    """First-run setup wizard"""
    def __init__(self):
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
        
        # Load existing config or create default
        default_config = {
            "server_url": url,
            "api_key": api_key,
            "username": "",
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


def check_and_run_setup(force: bool = False) -> bool:
    """Check if setup is needed and run wizard if so
    
    Args:
        force: If True, run wizard even if config exists
    """
    # Check if setup is needed (unless forced)
    if not force:
        if CONFIG_FILE.exists():
            try:
                with open(CONFIG_FILE, 'r') as f:
                    config = json.load(f)
                    # Check if we have the minimum required fields
                    if config.get("server_url") and config.get("api_key"):
                        return False  # Setup not needed
            except:
                pass  # Config file exists but invalid, need setup
    
    # Run setup wizard
    wizard = SetupWizard()
    return wizard.run()


if __name__ == "__main__":
    check_and_run_setup()
