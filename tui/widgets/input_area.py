"""
Chat input area with autocomplete.
"""

from textual.app import ComposeResult
from textual.widget import Widget
from textual.widgets import Input, Button, Static
from textual.containers import Horizontal, Vertical
from textual.message import Message
from textual.binding import Binding
from textual.events import Key


class AutocompleteInput(Input):
    """Input with Tab disabled for focus navigation."""
    pass


# Available commands for autocomplete
COMMANDS = [
    "search", "s",
    "image", "img",
    "generate", "gen",
    "mail", "mail unread", "mail inbox", "mail sent", "mail compose",
    "mail folders", "mail folder",
    "cal", "cal today", "cal week", "cal month", "cal add",
    "contacts", "contacts search", "contacts add",
    "music", "music browse", "music search", "music play", "music mood",
    "music stop", "music next", "music prev",
    "weather",
    "news", "dailynews",
    "torrents", "torrents list", "torrents add", "torrents download",
    "torrents pause", "torrents resume", "torrents rm", "torrents info", "torrents purge",
    "yt", "yt dl", "ytdl",  # YouTube summarize and download
    "todo", "todo add", "todo rm",  # Todo list
    "budget", "budget bills",  # Budget
    "reminder", "reminder add", "reminder list",
    "note", "note add", "note search",
    "help", "settings",
]


class ChatInput(Widget):
    """Chat input area with send button and autocomplete."""

    class MessageSubmitted(Message):
        """Posted when a message is submitted."""
        def __init__(self, content: str):
            self.content = content
            super().__init__()

    BINDINGS = [
        Binding("tab", "autocomplete", "Autocomplete", show=False, priority=True),
        Binding("up", "history_prev", "Previous", show=False),
        Binding("down", "history_next", "Next", show=False),
    ]

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.history: list[str] = []
        self.history_index = -1
        self.autocomplete_suggestions: list[str] = []

    class OpenLinksRequested(Message):
        """Posted when user wants to open links."""
        pass

    def compose(self) -> ComposeResult:
        yield Vertical(
            Horizontal(
                Button("Mail", id="quick-mail", classes="quick-btn"),
                Button("News", id="quick-news", classes="quick-btn"),
                Button("Music", id="quick-music", classes="quick-btn"),
                Button("Torrent", id="quick-torrent", classes="quick-btn"),
                Button("Weather", id="quick-weather", classes="quick-btn"),
                Button("Cal", id="quick-cal", classes="quick-btn"),
                Button("Links", id="quick-links", classes="quick-btn"),
                id="quick-actions"
            ),
            Static("", id="autocomplete-hint", classes="--hidden"),
            Horizontal(
                AutocompleteInput(placeholder="Type a message or command...", id="message-input"),
                Button("SEND", id="send-btn", variant="primary"),
                id="input-row"
            ),
            id="input-container"
        )


    def on_mount(self):
        """Focus input on mount."""
        self.query_one("#message-input", Input).focus()

    def on_button_pressed(self, event: Button.Pressed):
        """Handle button presses."""
        btn_id = event.button.id
        if btn_id == "send-btn":
            self.submit_message()
        elif btn_id == "quick-mail":
            self.send_command("mail")
        elif btn_id == "quick-news":
            self.send_command("dailynews")
        elif btn_id == "quick-music":
            self.send_command("music")
        elif btn_id == "quick-torrent":
            self.send_command("torrents")
        elif btn_id == "quick-weather":
            self.send_command("weather")
        elif btn_id == "quick-cal":
            self.send_command("cal")
        elif btn_id == "quick-links":
            self.post_message(self.OpenLinksRequested())

    def send_command(self, command: str):
        """Send a command as a message."""
        self.post_message(self.MessageSubmitted(command))

    def on_input_submitted(self, event: Input.Submitted):
        """Handle Enter key in input."""
        self.submit_message()

    def on_input_changed(self, event: Input.Changed):
        """Handle input changes for autocomplete."""
        value = event.value.strip()

        # Show autocomplete for any command-like input (no / required)
        if value and not " " in value:
            self.update_autocomplete(value.lstrip("/"))
        elif value.startswith("/") or (value and value.split()[0].lower() in [c.split()[0] for c in COMMANDS]):
            # Also show for multi-word commands
            self.update_autocomplete(value.lstrip("/"))
        else:
            self.hide_autocomplete()

    def submit_message(self):
        """Submit the current message."""
        input_widget = self.query_one("#message-input", Input)
        content = input_widget.value.strip()

        if not content:
            return

        # Add to history
        if not self.history or self.history[-1] != content:
            self.history.append(content)
        self.history_index = -1

        # Clear input
        input_widget.value = ""

        # Post message
        self.post_message(self.MessageSubmitted(content))

        # Hide autocomplete
        self.hide_autocomplete()

    def update_autocomplete(self, prefix: str):
        """Update autocomplete suggestions."""
        if not prefix:
            self.hide_autocomplete()
            return

        prefix_lower = prefix.lower()
        matches = [cmd for cmd in COMMANDS if cmd.lower().startswith(prefix_lower)]

        if matches:
            self.autocomplete_suggestions = matches[:5]  # Limit to 5
            hint_text = " | ".join(self.autocomplete_suggestions)
            hint = self.query_one("#autocomplete-hint", Static)
            hint.update(hint_text)
            hint.remove_class("--hidden")
        else:
            self.hide_autocomplete()

    def hide_autocomplete(self):
        """Hide autocomplete hints."""
        hint = self.query_one("#autocomplete-hint", Static)
        hint.add_class("--hidden")
        self.autocomplete_suggestions = []

    def action_autocomplete(self):
        """Complete the current command."""
        if self.autocomplete_suggestions:
            input_widget = self.query_one("#message-input", Input)
            # Complete with first suggestion (no / prefix needed)
            input_widget.value = self.autocomplete_suggestions[0]
            input_widget.cursor_position = len(input_widget.value)
            self.hide_autocomplete()

    def action_history_prev(self):
        """Navigate to previous history item."""
        if not self.history:
            return

        if self.history_index == -1:
            self.history_index = len(self.history) - 1
        elif self.history_index > 0:
            self.history_index -= 1

        input_widget = self.query_one("#message-input", Input)
        input_widget.value = self.history[self.history_index]

    def action_history_next(self):
        """Navigate to next history item."""
        if self.history_index == -1:
            return

        if self.history_index < len(self.history) - 1:
            self.history_index += 1
            input_widget = self.query_one("#message-input", Input)
            input_widget.value = self.history[self.history_index]
        else:
            self.history_index = -1
            input_widget = self.query_one("#message-input", Input)
            input_widget.value = ""
