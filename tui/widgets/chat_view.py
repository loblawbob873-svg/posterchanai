"""
Chat view widget for displaying messages.
"""

from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import ScrollableContainer
from textual.widget import Widget
from textual.widgets import Static

from tui.api.models import Message
from tui.widgets.message import MessageWidget


class ChatView(Widget):
    """Main chat message view."""

    can_focus = True

    BINDINGS = [
        # Vim-style: j=down, k=up, h/l for future link navigation
        Binding("j", "scroll_down", "Scroll Down", show=False),
        Binding("k", "scroll_up", "Scroll Up", show=False),
        Binding("g", "scroll_home", "Top", show=False),
        Binding("G", "scroll_end", "Bottom", show=False),
        # Arrow keys as alternative
        Binding("up", "scroll_up", "Scroll Up", show=False),
        Binding("down", "scroll_down", "Scroll Down", show=False),
        Binding("pageup", "page_up", "Page Up", show=False),
        Binding("pagedown", "page_down", "Page Down", show=False),
        Binding("home", "scroll_home", "Home", show=False),
        Binding("end", "scroll_end", "End", show=False),
        Binding("o", "open_urls", "Open URL", show=True),
    ]

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
            widget = MessageWidget(role=msg.role, content=msg.content, message_id=msg.id)
            container.mount(widget)

        # Scroll to bottom
        self.scroll_to_bottom()

    def add_message(self, role: str, content: str, message_id: int | None = None):
        """Add a new message to the view."""
        container = self.query_one("#messages-container", ScrollableContainer)

        widget = MessageWidget(role=role, content=content, message_id=message_id)
        container.mount(widget)
        self.scroll_to_bottom()

    def start_streaming(self):
        """Start streaming a new assistant message."""
        container = self.query_one("#messages-container", ScrollableContainer)

        # Create streaming message widget with thinking indicator
        self.streaming_message = MessageWidget(role="assistant", content="Thinking...", is_streaming=True)
        container.mount(self.streaming_message)

        # Show typing indicator bar at bottom
        indicator = self.query_one("#typing-indicator", Static)
        indicator.update("[ AI is generating response... ]")
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

    # Keyboard scroll actions
    def action_scroll_up(self):
        """Scroll up one line."""
        container = self.query_one("#messages-container", ScrollableContainer)
        container.scroll_up(animate=False)

    def action_scroll_down(self):
        """Scroll down one line."""
        container = self.query_one("#messages-container", ScrollableContainer)
        container.scroll_down(animate=False)

    def action_page_up(self):
        """Scroll up one page."""
        container = self.query_one("#messages-container", ScrollableContainer)
        container.scroll_page_up(animate=False)

    def action_page_down(self):
        """Scroll down one page."""
        container = self.query_one("#messages-container", ScrollableContainer)
        container.scroll_page_down(animate=False)

    def action_scroll_home(self):
        """Scroll to top."""
        container = self.query_one("#messages-container", ScrollableContainer)
        container.scroll_home(animate=False)

    def action_scroll_end(self):
        """Scroll to bottom."""
        container = self.query_one("#messages-container", ScrollableContainer)
        container.scroll_end(animate=False)
