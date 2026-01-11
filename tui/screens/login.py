"""
Login screen for authentication.
"""

from textual.app import ComposeResult
from textual.screen import Screen
from textual.widgets import Static, Input, Button
from textual.containers import Container, Horizontal
from textual.binding import Binding
from textual import work


class LoginScreen(Screen):
    """Login screen with username/password form."""

    BINDINGS = [
        Binding("enter", "submit", "Login", show=False),
        Binding("escape", "quit", "Quit"),
    ]

    def compose(self) -> ComposeResult:
        yield Container(
            Static("POSTERCHANAI", id="login-title"),
            Static("Terminal Client", id="login-subtitle"),
            Static("", id="login-error"),
            Horizontal(
                Static("Username:", classes="form-label"),
                Input(placeholder="Enter username", id="username-input", classes="form-input"),
                classes="form-row"
            ),
            Horizontal(
                Static("Password:", classes="form-label"),
                Input(placeholder="Enter password", password=True, id="password-input", classes="form-input"),
                classes="form-row"
            ),
            Horizontal(
                Static("Server:", classes="form-label"),
                Input(placeholder="http://localhost:3051", id="server-input", classes="form-input"),
                classes="form-row"
            ),
            Button("Login", id="login-btn", variant="primary"),
            id="login-container"
        )

    def on_mount(self):
        """Set initial values from config."""
        config = self.app.config

        # Pre-fill server URL
        server_input = self.query_one("#server-input", Input)
        server_input.value = config.server_url

        # Pre-fill username if saved
        if config.username:
            username_input = self.query_one("#username-input", Input)
            username_input.value = config.username
            # Focus password field
            self.query_one("#password-input", Input).focus()
        else:
            # Focus username field
            self.query_one("#username-input", Input).focus()

    def on_button_pressed(self, event: Button.Pressed):
        """Handle login button press."""
        if event.button.id == "login-btn":
            self.action_submit()

    def on_input_submitted(self, event: Input.Submitted):
        """Handle Enter key in input fields."""
        self.action_submit()

    def action_submit(self):
        """Submit login form."""
        self.do_login()

    @work(exclusive=True)
    async def do_login(self):
        """Perform login request."""
        username = self.query_one("#username-input", Input).value.strip()
        password = self.query_one("#password-input", Input).value
        server = self.query_one("#server-input", Input).value.strip()

        error_widget = self.query_one("#login-error", Static)

        if not username or not password:
            error_widget.update("Please enter username and password")
            return

        if not server:
            error_widget.update("Please enter server URL")
            return

        # Update config
        self.app.config.server_url = server
        self.app.api.base_url = server.rstrip("/")

        # Show loading state
        login_btn = self.query_one("#login-btn", Button)
        login_btn.disabled = True
        login_btn.label = "Logging in..."
        error_widget.update("")

        try:
            token = await self.app.api.login(username, password)

            # Save credentials
            self.app.config.username = username
            self.app.config.save()
            self.app.config.save_token(token)

            # Get user info
            user = await self.app.api.get_current_user()

            # Switch to main screen
            from .main import MainScreen
            self.app.push_screen(MainScreen(user))

        except Exception as e:
            error_msg = str(e)
            if "401" in error_msg or "Invalid" in error_msg:
                error_widget.update("Invalid username or password")
            elif "Connection" in error_msg or "connect" in error_msg.lower():
                error_widget.update(f"Cannot connect to server")
            else:
                error_widget.update(f"Error: {error_msg[:50]}")

            login_btn.disabled = False
            login_btn.label = "Login"

    def action_quit(self):
        """Quit application."""
        self.app.exit()
