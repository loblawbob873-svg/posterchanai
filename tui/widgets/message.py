"""
Message widget for displaying chat messages.
"""
from __future__ import annotations

from textual.app import ComposeResult
from textual.widget import Widget
from textual.widgets import Static, Button
from textual.containers import Vertical, Horizontal
from textual.message import Message as TextualMessage
from rich.text import Text

from tui.utils.markdown import parse_markdown, parse_cmd_links


class MessageWidget(Widget):
    """Widget displaying a single chat message."""

    class CommandClicked(TextualMessage, bubble=True):
        """Posted when a command button is clicked."""
        def __init__(self, command: str):
            self.command = command
            super().__init__()

    def __init__(
        self,
        role: str,
        content: str,
        message_id: int | None = None,
        is_streaming: bool = False,
    ):
        super().__init__()
        self.role = role
        self.content = content
        self.message_id = message_id
        self.is_streaming = is_streaming
        self._cmd_links = []

        # Set class based on role
        self.add_class(f"message-{role}")
        if is_streaming:
            self.add_class("message-streaming")

    def compose(self) -> ComposeResult:
        role_label = self.get_role_label()
        yield Vertical(
            Static(role_label, classes="message-role"),
            Static(id="message-content", classes="message-body"),
            Horizontal(id="message-buttons"),
            classes="message-inner"
        )

    def on_mount(self):
        """Render content on mount."""
        self.update_content(self.content)

    def get_role_label(self) -> str:
        """Get display label for role."""
        labels = {
            "user": "YOU",
            "assistant": "AI",
            "system": "SYSTEM",
        }
        return labels.get(self.role, self.role.upper())

    def update_content(self, content: str):
        """Update message content."""
        self.content = content
        content_widget = self.query_one("#message-content", Static)

        # For streaming, show raw text for speed
        if self.is_streaming:
            content_widget.update(content + " _")
        else:
            # Parse and extract cmd links
            self._cmd_links = parse_cmd_links(content)

            # Parse markdown for display
            try:
                rendered = parse_markdown(content)
                content_widget.update(rendered)
            except Exception:
                # Fallback to plain text with cmd links cleaned up
                from tui.utils.markdown import strip_markdown
                content_widget.update(strip_markdown(content))

            # Render action buttons
            self._render_buttons()

    def _render_buttons(self):
        """Render cmd: link buttons for essential actions."""
        try:
            button_container = self.query_one("#message-buttons", Horizontal)
            button_container.remove_children()

            # Only show buttons for actionable commands (mail, calendar, torrents)
            # Skip if too many links (like music/search results)
            if len(self._cmd_links) > 8:
                return

            # Filter to essential action buttons
            essential_prefixes = ("mail ", "cal ", "torrents ", "todo ", "news ", "miniflux ", "nyaa ", "music ")
            actionable = [
                (label, cmd) for label, cmd, _, _ in self._cmd_links
                if any(cmd.startswith(p) for p in essential_prefixes)
            ]

            if actionable:
                buttons_to_mount = []
                for label, command in actionable[:6]:  # Max 6 buttons
                    btn = Button(label, classes="cmd-button")
                    btn.command = command
                    buttons_to_mount.append(btn)

                if buttons_to_mount:
                    button_container.mount_all(buttons_to_mount)
        except Exception:
            pass

    def on_button_pressed(self, event: Button.Pressed):
        """Handle action button clicks."""
        button = event.button
        if hasattr(button, 'command') and button.command:
            # Post command to be handled by main screen
            self.post_message(self.CommandClicked(button.command))
            event.stop()

    def finish_streaming(self):
        """Mark streaming as complete and re-render."""
        self.is_streaming = False
        self.remove_class("message-streaming")
        self.update_content(self.content)
