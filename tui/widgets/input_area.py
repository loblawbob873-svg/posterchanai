"""
Chat input area with autocomplete.
"""

from typing import List, Optional
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
    "mail", "mail unread", "mail inbox", "mail sent", "mail send", "mail compose",
    "mail folders", "mail folder", "mail attachment", "mail search", "mail read",
    "mail summary", "mail sum", "mail translate", "mail reply", "mail delete",
    "mail archive",
    "cal", "cal today", "cal week", "cal month", "cal add",
    "contacts", "contacts search", "contacts add",
    "music", "music browse", "music search", "music play", "music mood",
    "music stop", "music next", "music prev",
    "weather",
    "news", "dailynews",
    "torrents", "torrents list", "torrents add", "torrents download",
    "torrents pause", "torrents resume", "torrents rm", "torrents info", "torrents purge",
    "bt", "bt list", "bt add", "bt pause", "bt resume", "bt rm", "bt info", "bt purge",
    "nyaa",
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
        def __init__(self, content: str, attachments: Optional[List[str]] = None):
            self.content = content
            self.attachments = attachments or []
            super().__init__()

    class AttachFileRequested(Message):
        """Posted when user wants to attach a file."""
        pass

    class NewsPickerRequested(Message):
        """Posted when user wants to select a news source."""
        pass

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
        self.pending_attachments: List[str] = []
        # Dynamic subcommands (populated from settings)
        self.mail_accounts: List[str] = []  # Account short names (e.g., "john", "work")
        self.subcommands: dict[str, List[str]] = {}

    class OpenLinksRequested(Message):
        """Posted when user wants to open links."""
        pass

    def set_mail_accounts(self, accounts: List[dict]):
        """Set mail accounts for autocomplete."""
        # Extract short names from emails (part before @)
        self.mail_accounts = []
        for acc in accounts:
            email = acc.get("email", "")
            if "@" in email:
                short = email.split("@")[0].lower()
                self.mail_accounts.append(short)

        # Update subcommands that need account hints
        account_commands = [
            "mail folders", "mail folder", "mail search", "mail read",
            "mail summary", "mail sum", "mail translate", "mail reply",
            "mail delete", "mail archive", "mail send",
        ]
        for cmd in account_commands:
            self.subcommands[cmd] = self.mail_accounts

    def compose(self) -> ComposeResult:
        yield Vertical(
            # Popup menus (positioned above quick-actions)
            Horizontal(
                Horizontal(
                    Button("Mail", id="pim-mail", classes="dropdown-item"),
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
                Button("News", id="quick-news", classes="quick-btn"),
                Button("Music", id="quick-music", classes="quick-btn"),
                Button("Torrent ▾", id="quick-torrent-toggle", classes="quick-btn dropdown-toggle"),
                id="quick-actions"
            ),
            Static("", id="attachments-display", classes="--hidden"),
            Static("", id="autocomplete-hint", classes="--hidden"),
            Horizontal(
                Button("📎", id="attach-btn", classes="attach-btn"),
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
        elif btn_id == "attach-btn":
            self.post_message(self.AttachFileRequested())
        # PIM dropdown toggle
        elif btn_id == "quick-pim":
            self.toggle_dropdown("pim-menu")
        # PIM menu items
        elif btn_id == "pim-mail":
            self.send_command("mail")
            self.hide_all_dropdowns()
        elif btn_id == "quick-news":
            self.post_message(self.NewsPickerRequested())
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

        if not content and not self.pending_attachments:
            return

        # Add to history
        if content and (not self.history or self.history[-1] != content):
            self.history.append(content)
        self.history_index = -1

        # Clear input
        input_widget.value = ""

        # Post message with attachments
        self.post_message(self.MessageSubmitted(content, self.pending_attachments.copy()))

        # Clear attachments
        self.pending_attachments.clear()
        self.update_attachments_display()

        # Hide autocomplete
        self.hide_autocomplete()

    def update_autocomplete(self, prefix: str):
        """Update autocomplete suggestions."""
        if not prefix:
            self.hide_autocomplete()
            return

        prefix_lower = prefix.lower()
        matches = []

        # Check if we have a multi-word command that needs subcommand hints
        words = prefix_lower.split()
        if len(words) >= 2:
            # Check for subcommand hints (e.g., "mail folders" -> account hints)
            base_cmd = " ".join(words[:-1])  # All but last word
            partial = words[-1]  # Last word (partial)

            if base_cmd in self.subcommands:
                # Filter subcommand hints by partial match
                hints = self.subcommands[base_cmd]
                sub_matches = [f"{base_cmd} {h}" for h in hints if h.startswith(partial)]
                matches.extend(sub_matches[:5])

        # Also match against static commands
        cmd_matches = [cmd for cmd in COMMANDS if cmd.lower().startswith(prefix_lower)]
        matches.extend(cmd_matches)

        # Dedupe and limit
        seen = set()
        unique_matches = []
        for m in matches:
            if m not in seen:
                seen.add(m)
                unique_matches.append(m)
                if len(unique_matches) >= 5:
                    break

        if unique_matches:
            self.autocomplete_suggestions = unique_matches
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

    def add_attachment(self, file_path: str):
        """Add a file to pending attachments."""
        if file_path and file_path not in self.pending_attachments:
            self.pending_attachments.append(file_path)
            self.update_attachments_display()

    def remove_attachment(self, file_path: str):
        """Remove a file from pending attachments."""
        if file_path in self.pending_attachments:
            self.pending_attachments.remove(file_path)
            self.update_attachments_display()

    def clear_attachments(self):
        """Clear all pending attachments."""
        self.pending_attachments.clear()
        self.update_attachments_display()

    def update_attachments_display(self):
        """Update the attachments display widget."""
        display = self.query_one("#attachments-display", Static)
        if self.pending_attachments:
            import os
            names = [os.path.basename(p) for p in self.pending_attachments]
            display.update(f"📎 Attachments: {', '.join(names)}")
            display.remove_class("--hidden")
        else:
            display.update("")
            display.add_class("--hidden")
