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

            # For performance: skip heavy markdown parsing if content has many cmd links
            # (indicates it's a list like music tracks)
            if len(self._cmd_links) > 20:
                # Simple display without full markdown parsing
                content_widget.update(content)
            else:
                # Parse markdown for display
                try:
                    rendered = parse_markdown(content)
                    content_widget.update(rendered)
                except Exception:
                    # Fallback to plain text
                    content_widget.update(content)

            # Render action buttons
            self._render_buttons()

    def _render_buttons(self):
        """Render cmd: link buttons."""
        try:
            button_container = self.query_one("#message-buttons", Horizontal)
            button_container.remove_children()

            if self._cmd_links:
                # Limit buttons to avoid UI performance issues
                # Only show first 10 buttons - user can still use clickable text
                max_buttons = 10
                buttons_to_mount = []

                for label, command, _, _ in self._cmd_links[:max_buttons]:
                    btn = Button(label, classes="cmd-button")
                    btn.command = command  # Store command on button
                    buttons_to_mount.append(btn)

                # Batch mount all buttons at once for better performance
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
