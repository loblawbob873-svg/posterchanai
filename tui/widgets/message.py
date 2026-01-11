"""
Message widget for displaying chat messages.
"""
from __future__ import annotations

from textual.app import ComposeResult
from textual.widget import Widget
from textual.widgets import Static
from textual.containers import Vertical
from rich.text import Text
from rich.markdown import Markdown

from tui.utils.markdown import parse_markdown, parse_cmd_links


class MessageWidget(Widget):
    """Widget displaying a single chat message."""

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

        # Set class based on role
        self.add_class(f"message-{role}")
        if is_streaming:
            self.add_class("message-streaming")

    def compose(self) -> ComposeResult:
        role_label = self.get_role_label()
        yield Vertical(
            Static(role_label, classes="message-role"),
            Static(id="message-content", classes="message-body"),
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
            # Parse markdown for final display
            try:
                rendered = parse_markdown(content)
                content_widget.update(rendered)
            except Exception:
                # Fallback to plain text
                content_widget.update(content)

    def finish_streaming(self):
        """Mark streaming as complete and re-render."""
        self.is_streaming = False
        self.remove_class("message-streaming")
        self.update_content(self.content)
