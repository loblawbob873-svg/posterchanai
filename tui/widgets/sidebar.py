"""
Conversation sidebar widget.
"""
from __future__ import annotations

from textual.app import ComposeResult
from textual.widget import Widget
from textual.widgets import Static, Button, ListView, ListItem
from textual.containers import Vertical, ScrollableContainer
from textual.message import Message
from textual.reactive import reactive
from textual.binding import Binding

from tui.api.models import Conversation


class ConversationItem(ListItem):
    """Single conversation in the list."""

    def __init__(self, conversation: Conversation):
        super().__init__()
        self.conversation = conversation

    def compose(self) -> ComposeResult:
        title = self.conversation.title or "New Chat"
        # Truncate long titles
        if len(title) > 25:
            title = title[:22] + "..."
        # Escape brackets to prevent Rich markup parsing errors
        title = title.replace("[", "\\[").replace("]", "\\]")
        yield Static(title, classes="conversation-title")


class ConversationSidebar(Widget):
    """Sidebar showing conversation list."""

    BINDINGS = [
        Binding("d", "delete_selected", "Delete", show=False),
        Binding("x", "delete_selected", "Delete", show=False),
        Binding("delete", "delete_selected", "Delete", show=False),
        # Vim-style navigation: j=down, k=up, l=select/enter, h=back/collapse
        Binding("j", "cursor_down", "Down", show=False),
        Binding("k", "cursor_up", "Up", show=False),
        Binding("l", "select_cursor", "Select", show=False),
        Binding("enter", "select_cursor", "Select", show=False),
        # Arrow keys as alternative
        Binding("down", "cursor_down", "Down", show=False),
        Binding("up", "cursor_up", "Up", show=False),
        Binding("right", "select_cursor", "Select", show=False),
    ]

    class ConversationSelected(Message):
        """Posted when a conversation is selected."""
        def __init__(self, conversation_id: int):
            self.conversation_id = conversation_id
            super().__init__()

    class NewChatRequested(Message):
        """Posted when new chat is requested."""
        pass

    class DeleteRequested(Message):
        """Posted when delete is requested for selected conversation."""
        def __init__(self, conversation_id: int):
            self.conversation_id = conversation_id
            super().__init__()

    selected_id = reactive(None)

    def compose(self) -> ComposeResult:
        yield Vertical(
            Static("CONVERSATIONS", id="sidebar-title"),
            Button("+ New Chat", id="new-chat-btn", variant="primary"),
            ScrollableContainer(
                ListView(id="conversation-list"),
                id="conversation-scroll"
            ),
            id="sidebar-content"
        )

    def update_conversations(self, conversations: list[Conversation]):
        """Update the conversation list."""
        list_view = self.query_one("#conversation-list", ListView)
        list_view.clear()

        for conv in conversations:
            item = ConversationItem(conv)
            if conv.id == self.selected_id:
                item.add_class("--selected")
            list_view.append(item)

    def select_conversation(self, conversation_id: int):
        """Mark a conversation as selected."""
        self.selected_id = conversation_id

        # Update visual selection
        list_view = self.query_one("#conversation-list", ListView)
        for item in list_view.children:
            if isinstance(item, ConversationItem):
                if item.conversation.id == conversation_id:
                    item.add_class("--selected")
                else:
                    item.remove_class("--selected")

    def on_button_pressed(self, event: Button.Pressed):
        """Handle new chat button."""
        if event.button.id == "new-chat-btn":
            self.post_message(self.NewChatRequested())

    def on_list_view_selected(self, event: ListView.Selected):
        """Handle conversation selection."""
        item = event.item
        if isinstance(item, ConversationItem):
            self.post_message(self.ConversationSelected(item.conversation.id))

    def action_delete_selected(self):
        """Request deletion of selected conversation."""
        if self.selected_id is not None:
            self.post_message(self.DeleteRequested(self.selected_id))

    def action_cursor_down(self):
        """Move cursor down in list (j)."""
        try:
            list_view = self.query_one("#conversation-list", ListView)
            list_view.action_cursor_down()
        except Exception:
            pass

    def action_cursor_up(self):
        """Move cursor up in list (k)."""
        try:
            list_view = self.query_one("#conversation-list", ListView)
            list_view.action_cursor_up()
        except Exception:
            pass

    def action_select_cursor(self):
        """Select item at cursor (Enter)."""
        try:
            list_view = self.query_one("#conversation-list", ListView)
            list_view.action_select_cursor()
        except Exception:
            pass
