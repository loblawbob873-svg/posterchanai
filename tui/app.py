"""
Main Textual Application for Posterchanai TUI.
"""

import os
import asyncio
from pathlib import Path
from textual.app import App
from textual.binding import Binding
from textual import work

from tui.config import Config
from tui.api.client import APIClient

# Control file for global music shortcuts
MUSIC_CONTROL_FILE = Path("/tmp/posterchanai-music-control")


class ChatApp(App):
    """Posterchanai Terminal Chat Client."""

    CSS_PATH = "theme.tcss"
    TITLE = "Posterchanai"
    SUB_TITLE = "Terminal Client"

    BINDINGS = [
        Binding("ctrl+n", "new_chat", "New Chat"),
        Binding("ctrl+b", "toggle_sidebar", "Sidebar"),
        Binding("ctrl+m", "toggle_music", "Music"),
        Binding("ctrl+s", "settings", "Settings"),
        Binding("ctrl+h", "help", "Help"),
        Binding("ctrl+q", "quit", "Quit"),
        # Vim-style bindings
        Binding("j", "scroll_down", "Down", show=False),
        Binding("k", "scroll_up", "Up", show=False),
        Binding("g", "scroll_top", "Top", show=False),
        Binding("G", "scroll_bottom", "Bottom", show=False),
        Binding("ctrl+d", "page_down", "Page Down", show=False),
        Binding("ctrl+u", "page_up", "Page Up", show=False),
        Binding("i", "focus_input", "Input", show=False),
        Binding("/", "focus_search", "Search", show=False),
    ]

    def __init__(self, config: Config):
        super().__init__()
        self.config = config
        self.api = APIClient(config.server_url)
        self.current_user = None

    async def on_mount(self):
        """Initialize app on mount."""
        # Start music control file watcher for global shortcuts
        self._start_music_control_watcher()

        # Try to restore session from saved token
        token = self.config.load_token()
        if token:
            self.api.token = token
            # Retry connection indefinitely if server is temporarily down
            retry_delay = 2
            attempt = 0
            while True:
                try:
                    user = await self.api.get_current_user()
                    self.current_user = user
                    # Go to main screen
                    from tui.screens.main import MainScreen
                    self.push_screen(MainScreen(user))
                    return
                except Exception as e:
                    error_msg = str(e).lower()
                    error_str = str(e)
                    # Check if it's a connection error (server down) vs auth error
                    if "401" in error_str or "unauthorized" in error_msg or ("invalid" in error_msg and "token" in error_msg):
                        # Auth error - token is invalid, clear and go to login
                        self.config.clear_token()
                        break
                    elif any(x in error_msg for x in ["connect", "connection", "timeout", "refused"]) or \
                         any(x in error_str for x in ["502", "503", "504", "500"]):
                        # Connection/server error - server might be starting up, retry indefinitely
                        attempt += 1
                        self.notify(f"Server unavailable, retrying in {retry_delay}s... (attempt {attempt})")
                        await asyncio.sleep(retry_delay)
                        retry_delay = min(retry_delay * 1.5, 30)  # Gradual backoff, max 30s
                    else:
                        # Unknown error - log but don't clear token, retry
                        attempt += 1
                        self.notify(f"Error: {error_str[:50]}... retrying")
                        await asyncio.sleep(retry_delay)
                        retry_delay = min(retry_delay * 1.5, 30)

        # Show login screen
        from tui.screens.login import LoginScreen
        self.push_screen(LoginScreen())

    @work(exclusive=True, group="music_control")
    async def _start_music_control_watcher(self):
        """Watch control file for global music shortcuts (Hyprland/Wayland)."""
        # Clear any stale control file
        if MUSIC_CONTROL_FILE.exists():
            try:
                MUSIC_CONTROL_FILE.unlink()
            except:
                pass

        while True:
            try:
                if MUSIC_CONTROL_FILE.exists():
                    command = MUSIC_CONTROL_FILE.read_text().strip().lower()
                    MUSIC_CONTROL_FILE.unlink()

                    if command:
                        self._handle_music_command(command)
            except Exception:
                pass

            await asyncio.sleep(0.2)  # Poll every 200ms

    def _handle_music_command(self, command: str):
        """Handle music control command from file."""
        from tui.screens.main import MainScreen
        screen = self.screen
        if not isinstance(screen, MainScreen):
            return

        try:
            music_player = screen.query_one("#music-player")
            if command == "toggle":
                music_player.toggle_playback()
            elif command == "next":
                music_player.next_track()
            elif command == "prev":
                music_player.prev_track()
        except Exception:
            pass

    async def action_new_chat(self):
        """Create new conversation."""
        from tui.screens.main import MainScreen
        screen = self.screen
        if isinstance(screen, MainScreen):
            await screen.create_new_chat()

    def action_toggle_sidebar(self):
        """Toggle sidebar visibility."""
        from tui.screens.main import MainScreen
        screen = self.screen
        if isinstance(screen, MainScreen):
            screen.toggle_sidebar()

    def action_toggle_music(self):
        """Toggle music player visibility."""
        from tui.screens.main import MainScreen
        screen = self.screen
        if isinstance(screen, MainScreen):
            screen.toggle_music_player()

    def action_settings(self):
        """Show settings screen."""
        from tui.screens.main import MainScreen
        screen = self.screen
        if isinstance(screen, MainScreen):
            screen.show_settings()

    def action_help(self):
        """Show help screen."""
        from tui.screens.main import MainScreen
        screen = self.screen
        if isinstance(screen, MainScreen):
            screen.show_help()

    def action_quit(self):
        """Quit application - stop music first."""
        self._cleanup_before_exit()
        self.exit()

    def _cleanup_before_exit(self):
        """Stop music and cleanup before exiting."""
        from tui.screens.main import MainScreen
        try:
            screen = self.screen
            if isinstance(screen, MainScreen):
                try:
                    music_player = screen.query_one("#music-player")
                    if music_player.player:
                        music_player.player.stop()
                except Exception:
                    pass
        except Exception:
            # No screen on stack - that's fine
            pass

        # Also clean up control file
        if MUSIC_CONTROL_FILE.exists():
            try:
                MUSIC_CONTROL_FILE.unlink()
            except:
                pass

    async def on_unmount(self):
        """Cleanup when app is unmounted (e.g., terminal closed)."""
        try:
            self._cleanup_before_exit()
        except Exception:
            pass

    # Vim-style navigation actions
    def action_scroll_down(self):
        """Scroll down (vim j)."""
        from tui.screens.main import MainScreen
        screen = self.screen
        if isinstance(screen, MainScreen):
            chat_view = screen.query_one("#chat-view")
            chat_view.scroll_down()

    def action_scroll_up(self):
        """Scroll up (vim k)."""
        from tui.screens.main import MainScreen
        screen = self.screen
        if isinstance(screen, MainScreen):
            chat_view = screen.query_one("#chat-view")
            chat_view.scroll_up()

    def action_scroll_top(self):
        """Scroll to top (vim g)."""
        from tui.screens.main import MainScreen
        screen = self.screen
        if isinstance(screen, MainScreen):
            chat_view = screen.query_one("#chat-view")
            chat_view.scroll_home()

    def action_scroll_bottom(self):
        """Scroll to bottom (vim G)."""
        from tui.screens.main import MainScreen
        screen = self.screen
        if isinstance(screen, MainScreen):
            chat_view = screen.query_one("#chat-view")
            chat_view.scroll_end()

    def action_page_down(self):
        """Page down (vim ctrl+d)."""
        from tui.screens.main import MainScreen
        screen = self.screen
        if isinstance(screen, MainScreen):
            chat_view = screen.query_one("#chat-view")
            chat_view.scroll_page_down()

    def action_page_up(self):
        """Page up (vim ctrl+u)."""
        from tui.screens.main import MainScreen
        screen = self.screen
        if isinstance(screen, MainScreen):
            chat_view = screen.query_one("#chat-view")
            chat_view.scroll_page_up()

    def action_focus_input(self):
        """Focus input (vim i for insert mode)."""
        from tui.screens.main import MainScreen
        screen = self.screen
        if isinstance(screen, MainScreen):
            input_widget = screen.query_one("#message-input")
            input_widget.focus()

    def action_focus_search(self):
        """Focus input with / prefix (vim search)."""
        from tui.screens.main import MainScreen
        from textual.widgets import Input
        screen = self.screen
        if isinstance(screen, MainScreen):
            input_widget = screen.query_one("#message-input", Input)
            input_widget.value = "/"
            input_widget.focus()
