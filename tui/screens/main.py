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
from tui.screens.file_picker import FilePickerScreen
from tui.screens.news_picker import NewsPickerScreen


class MainScreen(Screen):
    """Main chat interface."""

    BINDINGS = [
        Binding("escape", "stop_generation", "Stop", show=False),
        # Vim-style sidebar navigation
        Binding("n", "next_conversation", "Next", show=False),
        Binding("N", "prev_conversation", "Prev", show=False),
        Binding("h", "hide_sidebar", "Hide Sidebar", show=False),
        Binding("l", "show_sidebar", "Show Sidebar", show=False),
        Binding("o", "open_urls", "Open URL", show=False),
        # Music controls
        Binding("alt+p", "music_play_pause", "Play/Pause", show=False),
        Binding("alt+f", "music_next", "Next Track", show=False),
        Binding("alt+r", "music_prev", "Prev Track", show=False),
        Binding("alt+m", "music_minimize", "Minimize Player", show=False),
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
        """Load conversations and settings on mount."""
        self.load_conversations()  # @work decorator handles async
        self.load_mail_accounts()  # Load mail accounts for autocomplete

    @work(exclusive=True)
    async def load_conversations(self):
        """Load conversation list."""
        try:
            self.conversations = await self.app.api.list_conversations()
            sidebar = self.query_one("#sidebar", ConversationSidebar)
            sidebar.update_conversations(self.conversations)
            if not self.conversations:
                self.notify("No conversations found", severity="warning")
        except Exception as e:
            self.notify(f"Failed to load conversations: {e}", severity="error")

    @work(exclusive=True)
    async def load_mail_accounts(self):
        """Load mail accounts for autocomplete."""
        try:
            settings = await self.app.api.get_user_settings()
            mail_accounts = settings.get("mail_accounts", [])
            if mail_accounts:
                chat_input = self.query_one("#chat-input", ChatInput)
                chat_input.set_mail_accounts(mail_accounts)
        except Exception as e:
            # Silently fail - autocomplete just won't have account hints
            pass

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

    async def send_message(self, content: str, attachments: list[str] = None):
        """Send a message with optional file attachments."""
        import base64
        import mimetypes
        import os

        if not self.current_conversation_id:
            # Create new conversation first
            await self.create_new_chat()

        if not self.chat_ws or not self.chat_ws.connected:
            await self.connect_websocket(self.current_conversation_id)

        # Process attachments
        image_data = None
        file_content = None
        pdf_data = None
        document_data = None
        attachment_names = []

        if attachments:
            for file_path in attachments:
                if not os.path.isfile(file_path):
                    continue

                filename = os.path.basename(file_path)
                attachment_names.append(filename)
                mime_type, _ = mimetypes.guess_type(file_path)

                try:
                    with open(file_path, "rb") as f:
                        data = f.read()

                    # Categorize by file type
                    if mime_type and mime_type.startswith("image/"):
                        image_data = base64.b64encode(data).decode("utf-8")
                    elif mime_type == "application/pdf" or filename.lower().endswith(".pdf"):
                        pdf_data = base64.b64encode(data).decode("utf-8")
                    elif filename.lower().endswith((".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx")):
                        document_data = base64.b64encode(data).decode("utf-8")
                    elif mime_type and mime_type.startswith("text/") or filename.lower().endswith((".txt", ".md", ".py", ".js", ".json", ".xml", ".csv")):
                        file_content = data.decode("utf-8", errors="replace")
                    else:
                        # Try as text, fallback to base64
                        try:
                            file_content = data.decode("utf-8")
                        except UnicodeDecodeError:
                            document_data = base64.b64encode(data).decode("utf-8")
                except Exception as e:
                    self.notify(f"Failed to read {filename}: {e}", severity="error")

        # Build display message
        display_msg = content
        if attachment_names:
            display_msg = f"{content}\n\n📎 Attached: {', '.join(attachment_names)}" if content else f"📎 {', '.join(attachment_names)}"

        # Add user message to view
        chat_view = self.query_one("#chat-view", ChatView)
        chat_view.add_message("user", display_msg)

        # Start streaming state
        self.is_streaming = True
        self.streaming_content = ""
        chat_view.start_streaming()

        # Send via WebSocket with attachments
        try:
            await self.chat_ws.send_message(
                content,
                image_data=image_data,
                file_content=file_content,
                pdf_data=pdf_data,
                document_data=document_data,
            )
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
        final_content = self.streaming_content
        self.streaming_content = ""

        # Defer heavy markdown rendering to not block event loop
        def finish():
            chat_view = self.query_one("#chat-view", ChatView)
            chat_view.finish_streaming(final_content)

        self.call_later(finish)

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

        # Debug logging
        import logging
        logging.getLogger("tui").info(f"Response type={msg_type}, content_len={len(content)}")

        # Defer the expensive message rendering to not block the event loop
        def add_msg():
            if msg_type == "text":
                chat_view.add_message("assistant", content)
            elif msg_type == "search":
                chat_view.add_message("assistant", content)
            elif msg_type == "images":
                chat_view.add_message("assistant", content)
            elif msg_type == "generated_image":
                # Get image URL if available
                image_data = data.get("image", "")
                prompt = data.get("prompt", "")
                if image_data:
                    chat_view.add_message("assistant", f"{content}\n\n**Image generated!**\nPrompt: {prompt}")
                    self.notify("Image generated! Opening in browser...", severity="information")
                    # Open image in browser
                    self._open_generated_image(image_data)
                else:
                    chat_view.add_message("assistant", f"{content}\n\n[Image generation completed]")
            else:
                chat_view.add_message("assistant", content)

        # Use call_later to yield control back to event loop before heavy rendering
        self.call_later(add_msg)

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
        music_player.remove_class("--hidden")
        track = data.get("track", {})
        if track:
            # Delay playback to not block response handling
            self.set_timer(0.1, lambda: music_player.play_track(track))

    def handle_music_playlist(self, data: dict):
        """Handle music playlist command."""
        # Stop streaming state first
        self.is_streaming = False
        chat_view = self.query_one("#chat-view", ChatView)
        chat_view.stop_streaming()

        # Display the track list content if present
        content = data.get("content", "")
        if content:
            self.call_later(lambda: chat_view.add_message("assistant", content))

        # Show music player and load playlist
        self.music_visible = True
        music_player = self.query_one("#music-player", MusicPlayerWidget)
        music_player.remove_class("--hidden")
        tracks = data.get("tracks", [])
        if tracks:
            # Stop current playback before loading new playlist
            music_player.stop_current()
            music_player.load_playlist(tracks)
            # Delay playback to not block response handling
            # keep_playlist=True so we don't overwrite the loaded playlist
            self.set_timer(0.1, lambda: music_player.play_track(tracks[0], keep_playlist=True))

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
        self.music_visible = False

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
        self._send_message_worker(event.content, event.attachments)

    def on_conversation_sidebar_delete_requested(self, event):
        """Handle delete request from sidebar."""
        self._delete_conversation_worker(event.conversation_id)

    def on_message_widget_command_clicked(self, event):
        """Handle command button clicks from messages."""
        command = event.command
        if command:
            # Commands ending with space need user input (e.g., mail reply)
            if command.endswith(' '):
                # Populate input field for user to complete
                chat_input = self.query_one("#chat-input", ChatInput)
                input_widget = chat_input.query_one("#message-input", Input)
                input_widget.value = command
                input_widget.focus()
                self.notify("Type your message and press Enter", timeout=3)
            else:
                # Send the command as a message
                self._send_message_worker(command)

    def on_chat_input_open_links_requested(self, event):
        """Handle open links request from quick buttons."""
        self.action_open_urls()

    def on_chat_input_attach_file_requested(self, event):
        """Handle attach file request from input area."""
        def handle_file_selected(file_path: str | None):
            if file_path:
                chat_input = self.query_one("#chat-input", ChatInput)
                chat_input.add_attachment(file_path)
                self.notify(f"Attached: {file_path.split('/')[-1]}")

        self.app.push_screen(FilePickerScreen(title="Attach File"), handle_file_selected)

    def on_chat_input_news_picker_requested(self, event):
        """Handle news picker request from input area."""
        def handle_news_selected(source_url: str | None):
            if source_url:
                if source_url == "dailynews":
                    # All sources - use dailynews command
                    self._send_message_worker("dailynews")
                else:
                    # Specific source
                    self._send_message_worker(f"news {source_url}")

        self.app.push_screen(NewsPickerScreen(), handle_news_selected)

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
    async def _send_message_worker(self, content: str, attachments: list[str] = None):
        """Worker to send message."""
        await self.send_message(content, attachments)

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

    def action_open_urls(self):
        """Open URLs from recent messages in browser."""
        import webbrowser
        from tui.utils.markdown import extract_urls

        # Get chat view and find URLs in recent messages
        try:
            chat_view = self.query_one("#chat-view", ChatView)
            container = chat_view.query_one("#messages-container")

            all_urls = []
            # Get last few messages
            for widget in list(container.children)[-5:]:
                if hasattr(widget, 'content'):
                    urls = extract_urls(widget.content)
                    all_urls.extend(urls)

            # Remove duplicates while preserving order
            seen = set()
            unique_urls = []
            for url in all_urls:
                if url not in seen:
                    seen.add(url)
                    unique_urls.append(url)

            if not unique_urls:
                self.notify("No URLs found in recent messages", severity="warning")
                return

            if len(unique_urls) == 1:
                # Open single URL directly
                webbrowser.open(unique_urls[0])
                self.notify(f"Opened: {unique_urls[0][:50]}...")
            else:
                # Open all URLs and show count
                for url in unique_urls[:5]:  # Limit to 5
                    webbrowser.open(url)
                self.notify(f"Opened {min(len(unique_urls), 5)} URLs in browser")

        except Exception as e:
            self.notify(f"Failed to open URLs: {e}", severity="error")

    # Music control actions
    def action_music_play_pause(self):
        """Toggle music play/pause (Alt+P)."""
        music_player = self.query_one("#music-player", MusicPlayerWidget)
        music_player.toggle_playback()

    def action_music_next(self):
        """Skip to next track (Alt+F)."""
        music_player = self.query_one("#music-player", MusicPlayerWidget)
        music_player.next_track()

    def action_music_prev(self):
        """Skip to previous track (Alt+R)."""
        music_player = self.query_one("#music-player", MusicPlayerWidget)
        music_player.prev_track()

    def action_music_minimize(self):
        """Toggle music player minimize (Alt+M)."""
        music_player = self.query_one("#music-player", MusicPlayerWidget)
        music_player.toggle_minimize()

    def _open_generated_image(self, image_data: str):
        """Open generated image in browser."""
        import base64
        import tempfile
        import webbrowser
        import os

        try:
            # Decode base64 image
            image_bytes = base64.b64decode(image_data)

            # Determine format from header
            ext = "png"
            if image_bytes[:3] == b'\xff\xd8\xff':
                ext = "jpg"
            elif image_bytes[:4] == b'GIF8':
                ext = "gif"

            # Save to temp file
            with tempfile.NamedTemporaryFile(suffix=f".{ext}", delete=False) as f:
                f.write(image_bytes)
                temp_path = f.name

            # Open in browser
            webbrowser.open(f"file://{temp_path}")

        except Exception as e:
            import logging
            logging.getLogger("tui").error(f"Failed to open image: {e}")
