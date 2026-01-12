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
            # Popup menus (positioned above quick-actions)
            Horizontal(
                Horizontal(
                    Button("Mail", id="pim-mail", classes="dropdown-item"),
                    Button("News", id="pim-news", classes="dropdown-item"),
                    Button("Cal", id="pim-cal", classes="dropdown-item"),
                    Button("Todo", id="pim-todo", classes="dropdown-item"),
                    id="pim-menu",
                    classes="dropdown-menu --hidden"
                ),
                Horizontal(
                    Button("List", id="torrent-list", classes="dropdown-item"),
                    Button("Torrent", id="torrent-main", classes="dropdown-item"),
                    id="torrent-menu",
                    classes="dropdown-menu --hidden"
                ),
                id="dropdown-row",
                classes="--hidden"
            ),
            Horizontal(
                Button("PIM ▾", id="quick-pim", classes="quick-btn dropdown-toggle"),
                Button("Music", id="quick-music", classes="quick-btn"),
                Button("Torrent ▾", id="quick-torrent-toggle", classes="quick-btn dropdown-toggle"),
                Button("Weather", id="quick-weather", classes="quick-btn"),
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
        # PIM dropdown toggle
        elif btn_id == "quick-pim":
            self.toggle_dropdown("pim-menu")
        # PIM menu items
        elif btn_id == "pim-mail":
            self.send_command("mail")
            self.hide_all_dropdowns()
        elif btn_id == "pim-news":
            self.send_command("dailynews")
            self.hide_all_dropdowns()
        elif btn_id == "pim-cal":
            self.send_command("cal")
            self.hide_all_dropdowns()
        elif btn_id == "pim-todo":
            self.send_command("todo")
            self.hide_all_dropdowns()
        # Torrent dropdown toggle
        elif btn_id == "quick-torrent-toggle":
            self.toggle_dropdown("torrent-menu")
        # Torrent menu items
        elif btn_id == "torrent-list":
            self.send_command("torrents list")
            self.hide_all_dropdowns()
        elif btn_id == "torrent-main":
            self.send_command("torrents")
            self.hide_all_dropdowns()
        # Other quick buttons
        elif btn_id == "quick-music":
            self.send_command("music")
        elif btn_id == "quick-weather":
            self.send_command("weather")

    def toggle_dropdown(self, menu_id: str):
        """Toggle a dropdown menu visibility."""
        try:
            dropdown_row = self.query_one("#dropdown-row")
            menu = self.query_one(f"#{menu_id}")

            # Check if this menu is currently visible
            is_visible = not menu.has_class("--hidden")

            # Hide all menus first
            for dropdown_id in ["pim-menu", "torrent-menu"]:
                try:
                    dropdown = self.query_one(f"#{dropdown_id}")
                    dropdown.add_class("--hidden")
                except Exception:
                    pass

            if is_visible:
                # Was visible, now hide the row
                dropdown_row.add_class("--hidden")
            else:
                # Show the row and this menu
                dropdown_row.remove_class("--hidden")
                menu.remove_class("--hidden")
        except Exception:
            pass

    def hide_all_dropdowns(self):
        """Hide all dropdown menus."""
        try:
            dropdown_row = self.query_one("#dropdown-row")
            dropdown_row.add_class("--hidden")
        except Exception:
            pass
        for dropdown_id in ["pim-menu", "torrent-menu"]:
            try:
                dropdown = self.query_one(f"#{dropdown_id}")
                dropdown.add_class("--hidden")
            except Exception:
                pass

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
