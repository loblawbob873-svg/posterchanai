"""
WebSocket client for real-time chat streaming.
"""

import json
import asyncio
import logging
from typing import Optional, Callable, Any
import websockets
from websockets.client import WebSocketClientProtocol

logger = logging.getLogger(__name__)


class ChatWebSocket:
    """
    WebSocket client for chat streaming.

    Usage:
        ws = ChatWebSocket(ws_url, token)
        ws.on_stream_chunk = lambda content: print(content, end="")
        ws.on_stream_end = lambda: print()
        await ws.connect(conversation_id)
        await ws.send_message("Hello!")
    """

    def __init__(self, ws_url: str, token: str):
        self.ws_url = ws_url.rstrip("/")
        self.token = token
        self.ws: Optional[WebSocketClientProtocol] = None
        self._receive_task: Optional[asyncio.Task] = None
        self._connected = False
        self._conversation_id: Optional[int] = None

        # Callbacks
        self.on_stream_chunk: Optional[Callable[[str], Any]] = None
        self.on_stream_end: Optional[Callable[[], Any]] = None
        self.on_stream_clear: Optional[Callable[[], Any]] = None
        self.on_response: Optional[Callable[[dict], Any]] = None
        self.on_error: Optional[Callable[[str], Any]] = None
        self.on_disconnect: Optional[Callable[[], Any]] = None

        # Music callbacks
        self.on_music_play: Optional[Callable[[dict], Any]] = None
        self.on_music_playlist: Optional[Callable[[dict], Any]] = None
        self.on_music_next: Optional[Callable[[], Any]] = None
        self.on_music_prev: Optional[Callable[[], Any]] = None
        self.on_music_stop: Optional[Callable[[], Any]] = None

    @property
    def connected(self) -> bool:
        return self._connected and self.ws is not None

    async def connect(self, conversation_id: int):
        """Connect to chat WebSocket."""
        self._conversation_id = conversation_id
        url = f"{self.ws_url}/api/ws/chat/{conversation_id}?token={self.token}"
        logger.info(f"Connecting to WebSocket: {url[:50]}...")

        try:
            self.ws = await websockets.connect(
                url,
                ping_interval=60,
                ping_timeout=120,  # Allow long operations like image generation
            )
            self._connected = True
            logger.info("WebSocket connected")

            # Start receive loop
            self._receive_task = asyncio.create_task(self._receive_loop())

        except Exception as e:
            logger.error(f"WebSocket connection failed: {e}")
            self._connected = False
            raise

    async def disconnect(self):
        """Disconnect from WebSocket."""
        self._connected = False

        if self._receive_task:
            self._receive_task.cancel()
            try:
                await self._receive_task
            except asyncio.CancelledError:
                pass
            self._receive_task = None

        if self.ws:
            await self.ws.close()
            self.ws = None

        logger.info("WebSocket disconnected")

    async def send_message(
        self,
        content: str,
        image_data: Optional[str] = None,
        file_content: Optional[str] = None,
        pdf_data: Optional[str] = None,
        document_data: Optional[str] = None,
    ):
        """Send a chat message."""
        if not self.ws:
            raise RuntimeError("Not connected to WebSocket")

        payload = {
            "type": "message",
            "content": content,
        }

        if image_data:
            payload["image_data"] = image_data
        if file_content:
            payload["file_content"] = file_content
        if pdf_data:
            payload["pdf_data"] = pdf_data
        if document_data:
            payload["document_data"] = document_data

        await self.ws.send(json.dumps(payload))
        logger.debug(f"Sent message: {content[:50]}...")

    async def stop_generation(self):
        """Request to stop current generation."""
        if not self.ws:
            return

        await self.ws.send(json.dumps({"type": "stop"}))
        logger.debug("Sent stop request")

    async def _receive_loop(self):
        """Receive and handle messages from WebSocket."""
        try:
            async for message in self.ws:
                await self._handle_message(message)
        except websockets.ConnectionClosed as e:
            logger.warning(f"WebSocket closed: {e}")
            self._connected = False
            # Try to reconnect
            if self._conversation_id:
                await self._try_reconnect()
            elif self.on_disconnect:
                self.on_disconnect()
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"WebSocket error: {e}")
            self._connected = False
            if self.on_error:
                self.on_error(str(e))

    async def _try_reconnect(self, max_attempts: int = 3):
        """Try to reconnect to WebSocket."""
        for attempt in range(max_attempts):
            try:
                logger.info(f"Reconnect attempt {attempt + 1}/{max_attempts}")
                await asyncio.sleep(1)  # Wait before retry
                await self.connect(self._conversation_id)
                logger.info("Reconnected successfully")
                return
            except Exception as e:
                logger.warning(f"Reconnect failed: {e}")

        # All attempts failed
        if self.on_disconnect:
            self.on_disconnect()

    async def _handle_message(self, raw_message: str):
        """Handle incoming WebSocket message."""
        try:
            data = json.loads(raw_message)
        except json.JSONDecodeError:
            logger.error(f"Invalid JSON: {raw_message[:100]}")
            return

        msg_type = data.get("type", "")
        logger.debug(f"Received message type: {msg_type}")

        match msg_type:
            case "stream":
                if self.on_stream_chunk:
                    self.on_stream_chunk(data.get("content", ""))

            case "stream_end":
                if self.on_stream_end:
                    self.on_stream_end()

            case "stream_clear":
                if self.on_stream_clear:
                    self.on_stream_clear()

            case "response":
                # Command results are wrapped in {"type": "response", "data": {...}}
                inner_data = data.get("data", data)
                inner_type = inner_data.get("type", "")

                # Check for music commands inside response
                if inner_type == "music_play":
                    if self.on_music_play:
                        self.on_music_play(inner_data)
                elif inner_type == "music_playlist":
                    if self.on_music_playlist:
                        self.on_music_playlist(inner_data)
                elif inner_type == "music_next":
                    if self.on_music_next:
                        self.on_music_next()
                elif inner_type == "music_prev":
                    if self.on_music_prev:
                        self.on_music_prev()
                elif inner_type == "music_stop":
                    if self.on_music_stop:
                        self.on_music_stop()
                elif self.on_response:
                    self.on_response(inner_data)

            case "error":
                if self.on_error:
                    self.on_error(data.get("message", "Unknown error"))

            case "music_play":
                if self.on_music_play:
                    self.on_music_play(data)

            case "music_playlist":
                if self.on_music_playlist:
                    self.on_music_playlist(data)

            case "music_next":
                if self.on_music_next:
                    self.on_music_next()

            case "music_prev":
                if self.on_music_prev:
                    self.on_music_prev()

            case "music_stop":
                if self.on_music_stop:
                    self.on_music_stop()

            case _:
                # Treat unknown types as generic responses
                if self.on_response:
                    self.on_response(data)
