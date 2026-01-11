"""
Main Textual Application for Posterchanai TUI.
"""

from textual.app import App
from textual.binding import Binding

from tui.config import Config
from tui.api.client import APIClient


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
        # Try to restore session from saved token
        token = self.config.load_token()
        if token:
            self.api.token = token
            try:
                user = await self.api.get_current_user()
                self.current_user = user
                # Go to main screen
                from tui.screens.main import MainScreen
                self.push_screen(MainScreen(user))
                return
            except Exception:
                # Token expired or invalid
                self.config.clear_token()

        # Show login screen
        from tui.screens.login import LoginScreen
        self.push_screen(LoginScreen())

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
        """Quit application."""
        self.exit()

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
