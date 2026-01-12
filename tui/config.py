"""
Configuration management for Posterchanai TUI.

Handles server URL, token storage, and user preferences.
"""

import os
import json
from pathlib import Path
from dataclasses import dataclass, field
from typing import Optional

KEYRING_AVAILABLE = False
_keyring = None

def _check_keyring():
    """Check if keyring is available, suppressing all output."""
    global KEYRING_AVAILABLE, _keyring
    import os
    import sys
    import warnings
    import logging

    # Redirect stderr to devnull
    old_stderr_fd = os.dup(2)
    devnull = os.open(os.devnull, os.O_WRONLY)
    os.dup2(devnull, 2)
    os.close(devnull)

    # Suppress keyring logger
    keyring_logger = logging.getLogger('keyring')
    old_level = keyring_logger.level
    keyring_logger.setLevel(logging.CRITICAL + 1)

    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            import keyring as kr_module
            _keyring = kr_module
            # Test if a backend is actually available
            kr = _keyring.get_keyring()
            backend_name = type(kr).__name__.lower()
            if 'fail' not in backend_name and 'null' not in backend_name:
                KEYRING_AVAILABLE = True
    except Exception:
        pass
    finally:
        # Restore stderr
        os.dup2(old_stderr_fd, 2)
        os.close(old_stderr_fd)
        keyring_logger.setLevel(old_level)

try:
    _check_keyring()
except Exception:
    pass


# Config directory
CONFIG_DIR = Path.home() / ".config" / "posterchanai-tui"
CONFIG_FILE = CONFIG_DIR / "config.json"
TOKEN_SERVICE = "posterchanai-tui"


@dataclass
class Config:
    """Application configuration."""
    server_url: str = "http://localhost:3051"
    username: Optional[str] = None
    # Token stored separately in keyring for security

    @property
    def ws_url(self) -> str:
        """Get WebSocket URL from server URL."""
        return self.server_url.replace("http://", "ws://").replace("https://", "wss://")

    def save(self):
        """Save config to file."""
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        with open(CONFIG_FILE, "w") as f:
            json.dump({
                "server_url": self.server_url,
                "username": self.username,
            }, f, indent=2)

    @classmethod
    def load(cls) -> "Config":
        """Load config from file and environment."""
        config = cls()

        # Load from file if exists
        if CONFIG_FILE.exists():
            try:
                with open(CONFIG_FILE) as f:
                    data = json.load(f)
                    config.server_url = data.get("server_url", config.server_url)
                    config.username = data.get("username")
            except (json.JSONDecodeError, IOError):
                pass

        # Override with environment variables
        if env_server := os.getenv("POSTERCHANAI_SERVER"):
            config.server_url = env_server
        if env_user := os.getenv("POSTERCHANAI_USER"):
            config.username = env_user

        return config

    def save_token(self, token: str):
        """Save auth token securely."""
        saved = False
        if KEYRING_AVAILABLE and _keyring:
            try:
                _keyring.set_password(TOKEN_SERVICE, self.username or "default", token)
                saved = True
            except Exception:
                pass  # Fall through to file storage

        if not saved:
            # Fallback to file (less secure)
            token_file = CONFIG_DIR / ".token"
            CONFIG_DIR.mkdir(parents=True, exist_ok=True)
            token_file.write_text(token)
            token_file.chmod(0o600)

    def load_token(self) -> Optional[str]:
        """Load auth token."""
        if KEYRING_AVAILABLE and _keyring and self.username:
            try:
                return _keyring.get_password(TOKEN_SERVICE, self.username)
            except Exception:
                pass

        # Fallback to file
        token_file = CONFIG_DIR / ".token"
        if token_file.exists():
            try:
                return token_file.read_text().strip()
            except IOError:
                pass

        return None

    def clear_token(self):
        """Clear stored token."""
        if KEYRING_AVAILABLE and _keyring and self.username:
            try:
                _keyring.delete_password(TOKEN_SERVICE, self.username)
            except Exception:
                pass

        token_file = CONFIG_DIR / ".token"
        if token_file.exists():
            token_file.unlink()


def load_config() -> Config:
    """Load configuration."""
    return Config.load()
