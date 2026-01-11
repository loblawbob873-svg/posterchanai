"""
Settings screen with tabbed interface.
"""
from __future__ import annotations

from textual.app import ComposeResult
from textual.screen import Screen
from textual.widgets import Static, Input, Button, TabbedContent, TabPane, Switch, Label
from textual.containers import Container, Horizontal, Vertical, ScrollableContainer
from textual.binding import Binding
from textual import work


class SettingsScreen(Screen):
    """Settings screen with tabs for different config sections."""

    BINDINGS = [
        Binding("escape", "close", "Close"),
    ]

    def __init__(self):
        super().__init__()
        self.settings_data = {}

    def compose(self) -> ComposeResult:
        yield Container(
            Static("SETTINGS", id="settings-title"),
            id="settings-header"
        )

        with TabbedContent(id="settings-tabs"):
            with TabPane("Music", id="tab-music"):
                yield ScrollableContainer(
                    MusicSettingsPane(),
                    id="music-settings-scroll"
                )
            with TabPane("Calendar", id="tab-calendar"):
                yield ScrollableContainer(
                    CalendarSettingsPane(),
                    id="calendar-settings-scroll"
                )
            with TabPane("App", id="tab-app"):
                yield ScrollableContainer(
                    AppSettingsPane(),
                    id="app-settings-scroll"
                )

        yield Horizontal(
            Button("Save", id="save-btn", variant="primary"),
            Button("Cancel", id="cancel-btn"),
            id="settings-buttons"
        )

    def on_mount(self):
        """Load settings on mount."""
        self.load_settings()

    @work(exclusive=True)
    async def load_settings(self):
        """Load settings from API."""
        try:
            self.settings_data = await self.app.api.get_user_settings()
            self.populate_fields()
        except Exception as e:
            self.notify(f"Failed to load settings: {e}", severity="error")

    def populate_fields(self):
        """Populate form fields with settings data."""
        settings = self.settings_data

        # Music settings
        self._set_input("webdav-url", settings.get("webdav_url", ""))
        self._set_input("webdav-music-path", settings.get("webdav_music_path", "/music"))
        self._set_input("webdav-username", settings.get("webdav_username", ""))
        self._set_input("webdav-password", settings.get("webdav_password", ""))

        # Calendar settings
        self._set_input("caldav-url", settings.get("caldav_url", ""))
        self._set_input("caldav-username", settings.get("caldav_username", ""))
        self._set_input("caldav-password", settings.get("caldav_password", ""))
        self._set_input("carddav-url", settings.get("carddav_url", ""))
        self._set_input("carddav-username", settings.get("carddav_username", ""))
        self._set_input("carddav-password", settings.get("carddav_password", ""))

        # App settings
        self._set_input("server-url", self.app.config.server_url)

    def _set_input(self, input_id: str, value: str):
        """Safely set input value."""
        try:
            inp = self.query_one(f"#{input_id}", Input)
            inp.value = value or ""
        except Exception:
            pass

    def _get_input(self, input_id: str) -> str:
        """Safely get input value."""
        try:
            inp = self.query_one(f"#{input_id}", Input)
            return inp.value
        except Exception:
            return ""

    def on_button_pressed(self, event: Button.Pressed):
        """Handle button presses."""
        if event.button.id == "save-btn":
            self.save_settings()
        elif event.button.id == "cancel-btn":
            self.action_close()

    @work(exclusive=True)
    async def save_settings(self):
        """Save settings to API."""
        try:
            # Collect settings from form
            settings = {
                "webdav_url": self._get_input("webdav-url"),
                "webdav_music_path": self._get_input("webdav-music-path"),
                "webdav_username": self._get_input("webdav-username"),
                "webdav_password": self._get_input("webdav-password"),
                "caldav_url": self._get_input("caldav-url"),
                "caldav_username": self._get_input("caldav-username"),
                "caldav_password": self._get_input("caldav-password"),
                "carddav_url": self._get_input("carddav-url"),
                "carddav_username": self._get_input("carddav-username"),
                "carddav_password": self._get_input("carddav-password"),
            }

            # Filter out empty values
            settings = {k: v for k, v in settings.items() if v}

            await self.app.api.update_user_settings(settings)

            # Update local config if server URL changed
            new_server = self._get_input("server-url")
            if new_server and new_server != self.app.config.server_url:
                self.app.config.server_url = new_server
                self.app.config.save()
                self.app.api.base_url = new_server.rstrip("/")

            self.notify("Settings saved", severity="information")
            self.action_close()

        except Exception as e:
            self.notify(f"Failed to save: {e}", severity="error")

    def action_close(self):
        """Close settings screen."""
        self.app.pop_screen()


class MusicSettingsPane(Static):
    """Music/WebDAV settings."""

    def compose(self) -> ComposeResult:
        yield Vertical(
            Static("WebDAV Music Library", classes="settings-section-title"),
            Static("Configure WebDAV server for music streaming.", classes="settings-description"),

            Horizontal(
                Label("Server URL:", classes="settings-label"),
                Input(placeholder="https://webdav.example.com", id="webdav-url", classes="settings-input"),
                classes="settings-row"
            ),

            Horizontal(
                Label("Music Path:", classes="settings-label"),
                Input(placeholder="/music", id="webdav-music-path", classes="settings-input"),
                classes="settings-row"
            ),

            Horizontal(
                Label("Username:", classes="settings-label"),
                Input(placeholder="username", id="webdav-username", classes="settings-input"),
                classes="settings-row"
            ),

            Horizontal(
                Label("Password:", classes="settings-label"),
                Input(placeholder="password", password=True, id="webdav-password", classes="settings-input"),
                classes="settings-row"
            ),

            id="music-settings-content"
        )


class CalendarSettingsPane(Static):
    """Calendar settings."""

    def compose(self) -> ComposeResult:
        yield Vertical(
            Static("CalDAV Settings", classes="settings-section-title"),
            Static("Configure CalDAV server for calendar features.", classes="settings-description"),

            Horizontal(
                Label("Server URL:", classes="settings-label"),
                Input(placeholder="https://caldav.example.com", id="caldav-url", classes="settings-input"),
                classes="settings-row"
            ),

            Horizontal(
                Label("Username:", classes="settings-label"),
                Input(placeholder="username", id="caldav-username", classes="settings-input"),
                classes="settings-row"
            ),

            Horizontal(
                Label("Password:", classes="settings-label"),
                Input(placeholder="password", password=True, id="caldav-password", classes="settings-input"),
                classes="settings-row"
            ),

            Static("", classes="settings-spacer"),

            Static("CardDAV Settings", classes="settings-section-title"),

            Horizontal(
                Label("Server URL:", classes="settings-label"),
                Input(placeholder="https://carddav.example.com", id="carddav-url", classes="settings-input"),
                classes="settings-row"
            ),

            Horizontal(
                Label("Username:", classes="settings-label"),
                Input(placeholder="username", id="carddav-username", classes="settings-input"),
                classes="settings-row"
            ),

            Horizontal(
                Label("Password:", classes="settings-label"),
                Input(placeholder="password", password=True, id="carddav-password", classes="settings-input"),
                classes="settings-row"
            ),

            id="calendar-settings-content"
        )


class AppSettingsPane(Static):
    """Application settings."""

    def compose(self) -> ComposeResult:
        yield Vertical(
            Static("Server Connection", classes="settings-section-title"),

            Horizontal(
                Label("Server URL:", classes="settings-label"),
                Input(placeholder="http://localhost:3051", id="server-url", classes="settings-input"),
                classes="settings-row"
            ),

            Static("", classes="settings-spacer"),

            Static("Account", classes="settings-section-title"),

            Button("Logout", id="logout-btn", variant="warning"),

            id="app-settings-content"
        )

    def on_button_pressed(self, event: Button.Pressed):
        """Handle logout."""
        if event.button.id == "logout-btn":
            self.app.config.clear_token()
            from .login import LoginScreen
            self.app.switch_screen(LoginScreen())
