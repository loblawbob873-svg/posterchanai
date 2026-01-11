"""
Main chat screen with sidebar, messages, and input.
"""
from __future__ import annotations

from textual.app import ComposeResult
from textual.screen import Screen
from textual.widgets import Static, Input, Button, Footer
from textual.containers import Container, Horizontal, Vertical, ScrollableContainer
from textual.binding import Binding
from textual.reactive import reactive
from textual import work

from tui.api.models import User, Conversation
from tui.api.websocket import ChatWebSocket
from tui.widgets.sidebar import ConversationSidebar
from tui.widgets.chat_view import ChatView
from tui.widgets.input_area import ChatInput
from tui.widgets.music_player import MusicPlayerWidget


class MainScreen(Screen):
    """Main chat interface."""

    BINDINGS = [
        Binding("escape", "stop_generation", "Stop", show=False),
        # Vim-style sidebar navigation
        Binding("n", "next_conversation", "Next", show=False),
        Binding("N", "prev_conversation", "Prev", show=False),
        Binding("h", "hide_sidebar", "Hide Sidebar", show=False),
        Binding("l", "show_sidebar", "Show Sidebar", show=False),
    ]

    # Reactive state
    current_conversation_id = reactive(None)
    is_streaming = reactive(False)
    sidebar_visible = reactive(True)
    music_visible = reactive(False)

    def __init__(self, user: User):
        super().__init__()
        self.user = user
        self.conversations: list[Conversation] = []
        self.chat_ws: ChatWebSocket | None = None
        self.streaming_content = ""

    def compose(self) -> ComposeResult:
        yield Horizontal(
            ConversationSidebar(id="sidebar"),
            Vertical(
                Container(
                    Static(f"Select or create a conversation", id="chat-title"),
                    id="chat-header"
                ),
                ChatView(id="chat-view"),
                ChatInput(id="chat-input"),
                id="chat-container"
            ),
            id="main-container"
        )
        yield MusicPlayerWidget(id="music-player")
        yield Footer()

    def on_mount(self):
        """Load conversations on mount."""
        self.load_conversations()  # @work decorator handles async

    @work(exclusive=True)
    async def load_conversations(self):
        """Load conversation list."""
        try:
            self.conversations = await self.app.api.list_conversations()
            sidebar = self.query_one("#sidebar", ConversationSidebar)
            sidebar.update_conversations(self.conversations)
        except Exception as e:
            self.notify(f"Failed to load conversations: {e}", severity="error")

    async def create_new_chat(self):
        """Create a new conversation."""
        try:
            conversation = await self.app.api.create_conversation()
            self.conversations.insert(0, conversation)
            sidebar = self.query_one("#sidebar", ConversationSidebar)
            sidebar.update_conversations(self.conversations)
            await self.select_conversation(conversation.id)
        except Exception as e:
            self.notify(f"Failed to create chat: {e}", severity="error")

    async def select_conversation(self, conversation_id: int):
        """Select and load a conversation."""
        self.current_conversation_id = conversation_id

        # Update sidebar selection
        sidebar = self.query_one("#sidebar", ConversationSidebar)
        sidebar.select_conversation(conversation_id)

        # Disconnect existing WebSocket
        if self.chat_ws:
            await self.chat_ws.disconnect()

        # Load conversation messages
        try:
            conversation = await self.app.api.get_conversation(conversation_id)

            # Update header
            title = self.query_one("#chat-title", Static)
            title.update(conversation.title)

            # Update chat view
            chat_view = self.query_one("#chat-view", ChatView)
            chat_view.load_messages(conversation.messages)

            # Connect WebSocket
            await self.connect_websocket(conversation_id)

        except Exception as e:
            self.notify(f"Failed to load conversation: {e}", severity="error")

    async def connect_websocket(self, conversation_id: int):
        """Connect to chat WebSocket."""
        self.chat_ws = ChatWebSocket(
            ws_url=self.app.config.ws_url,
            token=self.app.api.token
        )

        # Set up callbacks
        self.chat_ws.on_stream_chunk = self.handle_stream_chunk
        self.chat_ws.on_stream_end = self.handle_stream_end
        self.chat_ws.on_stream_clear = self.handle_stream_clear
        self.chat_ws.on_response = self.handle_response
        self.chat_ws.on_error = self.handle_error
        self.chat_ws.on_disconnect = self.handle_disconnect
        self.chat_ws.on_music_play = self.handle_music_play
        self.chat_ws.on_music_playlist = self.handle_music_playlist
        self.chat_ws.on_music_next = self.handle_music_next
        self.chat_ws.on_music_prev = self.handle_music_prev
        self.chat_ws.on_music_stop = self.handle_music_stop

        try:
            await self.chat_ws.connect(conversation_id)
        except Exception as e:
            self.notify(f"Failed to connect: {e}", severity="error")

    async def send_message(self, content: str):
        """Send a message."""
        if not self.current_conversation_id:
            # Create new conversation first
            await self.create_new_chat()

        if not self.chat_ws or not self.chat_ws.connected:
            await self.connect_websocket(self.current_conversation_id)

        # Add user message to view
        chat_view = self.query_one("#chat-view", ChatView)
        chat_view.add_message("user", content)

        # Start streaming state
        self.is_streaming = True
        self.streaming_content = ""
        chat_view.start_streaming()

        # Send via WebSocket
        try:
            await self.chat_ws.send_message(content)
        except Exception as e:
            self.is_streaming = False
            chat_view.stop_streaming()
            self.notify(f"Failed to send: {e}", severity="error")

    def handle_stream_chunk(self, content: str):
        """Handle streaming chunk."""
        self.streaming_content += content
        chat_view = self.query_one("#chat-view", ChatView)
        chat_view.update_streaming(self.streaming_content)

    def handle_stream_end(self):
        """Handle stream end."""
        self.is_streaming = False
        chat_view = self.query_one("#chat-view", ChatView)
        chat_view.finish_streaming(self.streaming_content)
        self.streaming_content = ""

    def handle_stream_clear(self):
        """Handle stream clear (regeneration)."""
        self.streaming_content = ""
        chat_view = self.query_one("#chat-view", ChatView)
        chat_view.clear_streaming()

    def handle_response(self, data: dict):
        """Handle non-streaming response."""
        self.is_streaming = False
        chat_view = self.query_one("#chat-view", ChatView)
        chat_view.stop_streaming()

        msg_type = data.get("type", "text")
        content = data.get("content", "")

        if msg_type == "text":
            chat_view.add_message("assistant", content)
        elif msg_type == "search":
            chat_view.add_message("assistant", content)
            # TODO: Add search results widget
        elif msg_type == "images":
            chat_view.add_message("assistant", content)
            # TODO: Add image grid widget
        elif msg_type == "generated_image":
            chat_view.add_message("assistant", f"{content}\n\n[Image generated - view in web UI]")
        else:
            chat_view.add_message("assistant", content)

    def handle_error(self, error: str):
        """Handle WebSocket error."""
        self.is_streaming = False
        chat_view = self.query_one("#chat-view", ChatView)
        chat_view.stop_streaming()
        chat_view.add_message("system", f"Error: {error}")

    def handle_disconnect(self):
        """Handle WebSocket disconnect."""
        self.is_streaming = False
        self.notify("Disconnected from server", severity="warning")

    def handle_music_play(self, data: dict):
        """Handle music play command."""
        self.music_visible = True
        music_player = self.query_one("#music-player", MusicPlayerWidget)
        music_player.play_track(data.get("track", {}))

    def handle_music_playlist(self, data: dict):
        """Handle music playlist command."""
        self.music_visible = True
        music_player = self.query_one("#music-player", MusicPlayerWidget)
        tracks = data.get("tracks", [])
        if tracks:
            music_player.load_playlist(tracks)
            music_player.play_track(tracks[0])

    def handle_music_next(self):
        """Handle music next command."""
        music_player = self.query_one("#music-player", MusicPlayerWidget)
        music_player.next_track()

    def handle_music_prev(self):
        """Handle music prev command."""
        music_player = self.query_one("#music-player", MusicPlayerWidget)
        music_player.prev_track()

    def handle_music_stop(self):
        """Handle music stop command."""
        music_player = self.query_one("#music-player", MusicPlayerWidget)
        music_player.stop()

    async def action_stop_generation(self):
        """Stop current generation."""
        if self.is_streaming and self.chat_ws:
            await self.chat_ws.stop_generation()

    def toggle_sidebar(self):
        """Toggle sidebar visibility."""
        self.sidebar_visible = not self.sidebar_visible
        sidebar = self.query_one("#sidebar", ConversationSidebar)
        sidebar.display = self.sidebar_visible

    def toggle_music_player(self):
        """Toggle music player visibility."""
        self.music_visible = not self.music_visible
        music_player = self.query_one("#music-player", MusicPlayerWidget)
        if self.music_visible:
            music_player.remove_class("--hidden")
        else:
            music_player.add_class("--hidden")

    def show_settings(self):
        """Show settings screen."""
        from tui.screens.settings import SettingsScreen
        self.app.push_screen(SettingsScreen())

    def show_help(self):
        """Show help screen."""
        from tui.screens.help import HelpScreen
        self.app.push_screen(HelpScreen())

    def on_conversation_sidebar_conversation_selected(self, event):
        """Handle conversation selection from sidebar."""
        self._select_conversation_worker(event.conversation_id)

    def on_conversation_sidebar_new_chat_requested(self, event):
        """Handle new chat request from sidebar."""
        self._create_new_chat_worker()

    def on_chat_input_message_submitted(self, event):
        """Handle message submission from input."""
        self._send_message_worker(event.content)

    def on_conversation_sidebar_delete_requested(self, event):
        """Handle delete request from sidebar."""
        self._delete_conversation_worker(event.conversation_id)

    @work(exclusive=True)
    async def _delete_conversation_worker(self, conversation_id: int):
        """Worker to delete conversation with confirmation."""
        # Find conversation title for confirmation
        conv_title = "this conversation"
        for conv in self.conversations:
            if conv.id == conversation_id:
                conv_title = conv.title or "New Chat"
                break

        # Simple confirmation via notify - delete on 'd' press
        try:
            await self.app.api.delete_conversation(conversation_id)

            # Remove from local list
            self.conversations = [c for c in self.conversations if c.id != conversation_id]

            # Update sidebar
            sidebar = self.query_one("#sidebar", ConversationSidebar)
            sidebar.update_conversations(self.conversations)

            # If we deleted current conversation, clear the view
            if self.current_conversation_id == conversation_id:
                self.current_conversation_id = None
                if self.chat_ws:
                    await self.chat_ws.disconnect()
                    self.chat_ws = None

                # Clear chat view
                title = self.query_one("#chat-title", Static)
                title.update("Select or create a conversation")
                chat_view = self.query_one("#chat-view", ChatView)
                chat_view.load_messages([])

            self.notify(f"Deleted: {conv_title}", severity="information")

        except Exception as e:
            self.notify(f"Failed to delete: {e}", severity="error")

    @work(exclusive=True)
    async def _select_conversation_worker(self, conversation_id: int):
        """Worker to select conversation."""
        await self.select_conversation(conversation_id)

    @work(exclusive=True)
    async def _create_new_chat_worker(self):
        """Worker to create new chat."""
        await self.create_new_chat()

    @work(exclusive=True)
    async def _send_message_worker(self, content: str):
        """Worker to send message."""
        await self.send_message(content)

    # Vim-style actions
    def action_next_conversation(self):
        """Select next conversation (vim n)."""
        if not self.conversations:
            return

        current_idx = -1
        for i, conv in enumerate(self.conversations):
            if conv.id == self.current_conversation_id:
                current_idx = i
                break

        next_idx = (current_idx + 1) % len(self.conversations)
        self._select_conversation_worker(self.conversations[next_idx].id)

    def action_prev_conversation(self):
        """Select previous conversation (vim N)."""
        if not self.conversations:
            return

        current_idx = 0
        for i, conv in enumerate(self.conversations):
            if conv.id == self.current_conversation_id:
                current_idx = i
                break

        prev_idx = (current_idx - 1) % len(self.conversations)
        self._select_conversation_worker(self.conversations[prev_idx].id)

    def action_hide_sidebar(self):
        """Hide sidebar (vim h)."""
        if self.sidebar_visible:
            self.toggle_sidebar()

    def action_show_sidebar(self):
        """Show sidebar (vim l)."""
        if not self.sidebar_visible:
            self.toggle_sidebar()
