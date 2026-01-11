"""
Settings screen with tabbed interface.
"""

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

    def compose(self) -> ComposeResult:
        yield Container(
            Static("SETTINGS", id="settings-title"),
            id="settings-header"
        )

        with TabbedContent(id="settings-tabs"):
            with TabPane("Mail", id="tab-mail"):
                yield ScrollableContainer(
                    MailSettingsPane(),
                    id="mail-settings-scroll"
                )
            with TabPane("Calendar", id="tab-calendar"):
                yield ScrollableContainer(
                    CalendarSettingsPane(),
                    id="calendar-settings-scroll"
                )
            with TabPane("Music", id="tab-music"):
                yield ScrollableContainer(
                    MusicSettingsPane(),
                    id="music-settings-scroll"
                )
            with TabPane("AI", id="tab-ai"):
                yield ScrollableContainer(
                    AISettingsPane(),
                    id="ai-settings-scroll"
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

    def on_button_pressed(self, event: Button.Pressed):
        """Handle button presses."""
        if event.button.id == "save-btn":
            self.save_settings()
        elif event.button.id == "cancel-btn":
            self.action_close()

    @work
    async def save_settings(self):
        """Save all settings."""
        self.notify("Settings saved", severity="information")
        self.action_close()

    def action_close(self):
        """Close settings screen."""
        self.app.pop_screen()


class MailSettingsPane(Static):
    """Mail account settings."""

    def compose(self) -> ComposeResult:
        yield Vertical(
            Static("Email Accounts", classes="settings-section-title"),
            Static("Configure IMAP/SMTP accounts for mail features.", classes="settings-description"),

            # Account list placeholder
            Container(
                Static("No accounts configured", id="mail-accounts-list"),
                id="mail-accounts-container"
            ),

            Button("+ Add Account", id="add-mail-account-btn"),

            Static("", classes="settings-spacer"),

            Static("Default Mail Settings", classes="settings-section-title"),

            Horizontal(
                Label("Check interval (min):", classes="settings-label"),
                Input(placeholder="5", id="mail-check-interval", classes="settings-input-small"),
                classes="settings-row"
            ),

            Horizontal(
                Label("Show notifications:", classes="settings-label"),
                Switch(value=True, id="mail-notifications"),
                classes="settings-row"
            ),

            id="mail-settings-content"
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

            Button("Test Connection", id="test-caldav-btn"),

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

            Button("Test Connection", id="test-carddav-btn"),

            id="calendar-settings-content"
        )


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

            Button("Test Connection", id="test-webdav-btn"),

            Static("", classes="settings-spacer"),

            Static("Playback Settings", classes="settings-section-title"),

            Horizontal(
                Label("Transcode audio:", classes="settings-label"),
                Switch(value=True, id="transcode-enabled"),
                classes="settings-row"
            ),

            Horizontal(
                Label("Bitrate:", classes="settings-label"),
                Input(placeholder="192", id="transcode-bitrate", classes="settings-input-small"),
                Static("kbps", classes="settings-suffix"),
                classes="settings-row"
            ),

            id="music-settings-content"
        )


class AISettingsPane(Static):
    """AI model settings."""

    def compose(self) -> ComposeResult:
        yield Vertical(
            Static("AI Model Configuration", classes="settings-section-title"),
            Static("Configure custom AI models and API keys.", classes="settings-description"),

            Horizontal(
                Label("Default Model:", classes="settings-label"),
                Input(placeholder="gpt-4", id="default-model", classes="settings-input"),
                classes="settings-row"
            ),

            Horizontal(
                Label("OpenAI API Key:", classes="settings-label"),
                Input(placeholder="sk-...", password=True, id="openai-key", classes="settings-input"),
                classes="settings-row"
            ),

            Horizontal(
                Label("Anthropic API Key:", classes="settings-label"),
                Input(placeholder="sk-ant-...", password=True, id="anthropic-key", classes="settings-input"),
                classes="settings-row"
            ),

            Static("", classes="settings-spacer"),

            Static("Generation Settings", classes="settings-section-title"),

            Horizontal(
                Label("Max tokens:", classes="settings-label"),
                Input(placeholder="4096", id="max-tokens", classes="settings-input-small"),
                classes="settings-row"
            ),

            Horizontal(
                Label("Temperature:", classes="settings-label"),
                Input(placeholder="0.7", id="temperature", classes="settings-input-small"),
                classes="settings-row"
            ),

            Horizontal(
                Label("Stream responses:", classes="settings-label"),
                Switch(value=True, id="stream-enabled"),
                classes="settings-row"
            ),

            id="ai-settings-content"
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

            Static("Display Settings", classes="settings-section-title"),

            Horizontal(
                Label("Show timestamps:", classes="settings-label"),
                Switch(value=True, id="show-timestamps"),
                classes="settings-row"
            ),

            Horizontal(
                Label("Compact messages:", classes="settings-label"),
                Switch(value=False, id="compact-messages"),
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
