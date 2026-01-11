"""
Chat view widget for displaying messages.
"""
from __future__ import annotations

from textual.app import ComposeResult
from textual.widget import Widget
from textual.widgets import Static
from textual.containers import ScrollableContainer

from tui.api.models import Message
from tui.widgets.message import MessageWidget


class ChatView(Widget):
    """Main chat message view."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.streaming_message: MessageWidget | None = None

    def compose(self) -> ComposeResult:
        yield ScrollableContainer(id="messages-container")
        yield Static("", id="typing-indicator", classes="--hidden")

    def load_messages(self, messages: list[Message]):
        """Load messages into view."""
        container = self.query_one("#messages-container", ScrollableContainer)

        # Clear existing messages
        container.remove_children()

        # Add messages
        for msg in messages:
            widget = MessageWidget(
                role=msg.role,
                content=msg.content,
                message_id=msg.id
            )
            container.mount(widget)

        # Scroll to bottom
        self.scroll_to_bottom()

    def add_message(self, role: str, content: str, message_id: int | None = None):
        """Add a new message to the view."""
        container = self.query_one("#messages-container", ScrollableContainer)

        widget = MessageWidget(
            role=role,
            content=content,
            message_id=message_id
        )
        container.mount(widget)
        self.scroll_to_bottom()

    def start_streaming(self):
        """Start streaming a new assistant message."""
        container = self.query_one("#messages-container", ScrollableContainer)

        # Create streaming message widget
        self.streaming_message = MessageWidget(
            role="assistant",
            content="",
            is_streaming=True
        )
        container.mount(self.streaming_message)

        # Show typing indicator
        indicator = self.query_one("#typing-indicator", Static)
        indicator.update("AI is thinking...")
        indicator.remove_class("--hidden")

        self.scroll_to_bottom()

    def update_streaming(self, content: str):
        """Update the streaming message content."""
        if self.streaming_message:
            self.streaming_message.update_content(content)
            self.scroll_to_bottom()

    def finish_streaming(self, final_content: str):
        """Finish streaming and render final content."""
        if self.streaming_message:
            self.streaming_message.update_content(final_content)
            self.streaming_message.finish_streaming()
            self.streaming_message = None

        # Hide typing indicator
        indicator = self.query_one("#typing-indicator", Static)
        indicator.add_class("--hidden")

    def stop_streaming(self):
        """Stop streaming without completing."""
        if self.streaming_message:
            self.streaming_message.finish_streaming()
            self.streaming_message = None

        indicator = self.query_one("#typing-indicator", Static)
        indicator.add_class("--hidden")

    def clear_streaming(self):
        """Clear the current streaming message (for regeneration)."""
        if self.streaming_message:
            self.streaming_message.update_content("")

    def scroll_to_bottom(self):
        """Scroll to the bottom of messages."""
        container = self.query_one("#messages-container", ScrollableContainer)
        container.scroll_end(animate=False)
