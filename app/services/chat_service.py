import json
import re
import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor
from typing import AsyncGenerator, Optional, TYPE_CHECKING
from sqlalchemy.orm import Session
from app.models import Setting
from app.services.inference_factory import get_inference_service, prepare_vram_for_llm
from app.services.custom_ai_service import CustomAIService

if TYPE_CHECKING:
    from app.models import User

logger = logging.getLogger(__name__)

# Thread pool for running synchronous generators
_stream_executor = ThreadPoolExecutor(max_workers=4)


class ChatService:
    def __init__(self, db: Session, user: Optional["User"] = None):
        self.db = db
        self.user = user
        self._load_settings()

    def _load_settings(self):
        """Load settings - inference factory handles all backend-specific settings"""
        self._settings = {s.key: s.value for s in self.db.query(Setting).all()}
        default_prompt = "You are a helpful, friendly AI assistant. When writing code, always use markdown code blocks with the language specified (```python, ```bash, etc.) for proper syntax highlighting."
        self.system_prompt = self._settings.get("ollama_system_prompt") or default_prompt
        # These are used for chat_stream kwargs
        self.temperature = float(self._settings.get("ollama_temperature", "0.7"))
        self.top_p = float(self._settings.get("ollama_top_p", "0.9"))
        self.num_predict = int(self._settings.get("ollama_num_predict", "2048"))

    def _get_custom_ai_service(self) -> Optional[CustomAIService]:
        """Get custom AI service if user has it enabled, otherwise return None"""
        if (self.user and
            self.user.custom_ai_enabled and
            self.user.custom_ai_url and
            self.user.custom_ai_type):
            return CustomAIService(
                api_type=self.user.custom_ai_type,
                url=self.user.custom_ai_url,
                model=self.user.custom_ai_model or "default",
                api_key=self.user.custom_ai_api_key
            )
        return None

    def strip_thinking_tags(self, response: str) -> str:
        """Strip thinking tags from AI response"""
        from app.services.text_utils import strip_thinking_tags
        return strip_thinking_tags(response)

    async def chat(self, messages: list[dict]) -> str:
        """Non-streaming chat completion using inference factory or custom AI service"""
        try:
            # Check if user has custom AI service enabled
            custom_service = self._get_custom_ai_service()
            if custom_service:
                # Use user's custom AI service
                content = await custom_service.chat(
                    messages=messages,
                    temperature=self.temperature,
                    top_p=self.top_p,
                    max_tokens=self.num_predict
                )
                return content
            else:
                # Use server's default AI service
                prepare_vram_for_llm(self.db)
                service = get_inference_service(self.db)
                result = await service.chat_completion(
                    messages=messages,
                    temperature=self.temperature,
                    top_p=self.top_p,
                    max_tokens=self.num_predict
                )
                if "error" in result:
                    return f"Error: {result['error'].get('message', 'Unknown error')}"
                content = result["choices"][0]["message"]["content"]
                return self.strip_thinking_tags(content)
        except Exception as e:
            return f"Error: {str(e)}"

    async def chat_stream(self, messages: list[dict]) -> AsyncGenerator[str, None]:
        """Streaming chat completion - uses async queue to avoid blocking event loop"""
        try:
            # Check if user has custom AI service enabled
            custom_service = self._get_custom_ai_service()
            if custom_service:
                # Use user's custom AI service - stream using SSE parsing
                # With thinking tag filtering
                buffer = ""
                thinking_done = False

                async for chunk in custom_service.chat_stream(
                    messages=messages,
                    temperature=self.temperature,
                    top_p=self.top_p,
                    max_tokens=self.num_predict
                ):
                    if chunk.startswith("data: "):
                        data_str = chunk[6:].strip()
                        if data_str == "[DONE]":
                            break
                        try:
                            data = json.loads(data_str)
                            # Check for error
                            if "error" in data:
                                yield f"Error: {data['error'].get('message', 'Unknown error')}"
                                return
                            content = data.get("choices", [{}])[0].get("delta", {}).get("content", "")
                            if content:
                                buffer += content

                                if not thinking_done:
                                    # Look for end of thinking tag
                                    match = re.search(r'</think(?:ing)?>', buffer, re.IGNORECASE)
                                    if match:
                                        thinking_done = True
                                        after_think = buffer[match.end():]
                                        buffer = ""
                                        if after_think.strip():
                                            yield after_think
                                    # Also check if no think tag in first 50 chars - assume no thinking
                                    elif len(buffer) > 50 and '<think' not in buffer.lower():
                                        thinking_done = True
                                        yield buffer
                                        buffer = ""
                                else:
                                    yield content
                        except json.JSONDecodeError:
                            continue

                # Yield any remaining buffer
                if buffer:
                    clean = self.strip_thinking_tags(buffer)
                    if clean:
                        yield clean
                return

            # Use server's default AI service
            prepare_vram_for_llm(self.db)
            service = get_inference_service(self.db)

            # Use direct content streaming for native backend
            if hasattr(service, 'stream_chat_content'):
                queue = asyncio.Queue()
                loop = asyncio.get_event_loop()

                def run_streaming():
                    """Run synchronous generator in thread, put results in queue"""
                    try:
                        for content in service.stream_chat_content(
                            messages=messages,
                            temperature=self.temperature,
                            top_p=self.top_p,
                            max_tokens=self.num_predict
                        ):
                            loop.call_soon_threadsafe(queue.put_nowait, content)
                    except Exception as e:
                        loop.call_soon_threadsafe(queue.put_nowait, f"Error: {e}")
                    finally:
                        loop.call_soon_threadsafe(queue.put_nowait, None)  # Signal end

                # Start streaming in background thread
                _stream_executor.submit(run_streaming)

                # Process queue with think tag filtering
                buffer = ""
                thinking_mode = None  # None=unknown, True=in thinking, False=no thinking

                while True:
                    content = await queue.get()
                    if content is None:
                        break

                    if content.startswith("Error:"):
                        yield content
                        return

                    buffer += content

                    if thinking_mode is None:
                        # Check if model started with <think> tag
                        if '<think' in buffer.lower():
                            thinking_mode = True
                        elif len(buffer) > 20:
                            # No think tag in first 20 chars - assume no thinking
                            thinking_mode = False
                            yield buffer
                            buffer = ""

                    if thinking_mode is True:
                        # In thinking mode - look for end tag
                        match = re.search(r'</think(?:ing)?>', buffer, re.IGNORECASE)
                        if match:
                            thinking_mode = False
                            after_think = buffer[match.end():]
                            buffer = ""
                            if after_think.strip():
                                yield after_think
                    elif thinking_mode is False:
                        # Not thinking - stream directly
                        yield content
                        buffer = ""

                # Yield any remaining buffer
                if buffer:
                    # Strip think tags if present
                    clean = self.strip_thinking_tags(buffer)
                    if clean:
                        yield clean
            else:
                # Fallback to SSE parsing for Ollama - with thinking tag filtering
                buffer = ""
                thinking_mode = None  # None=unknown, True=in thinking, False=no thinking

                async for chunk in service.chat_completion_stream(
                    messages=messages,
                    temperature=self.temperature,
                    top_p=self.top_p,
                    max_tokens=self.num_predict
                ):
                    if chunk.startswith("data: "):
                        data_str = chunk[6:].strip()
                        if data_str == "[DONE]":
                            break
                        try:
                            data = json.loads(data_str)
                            content = data.get("choices", [{}])[0].get("delta", {}).get("content", "")
                            if content:
                                buffer += content

                                if thinking_mode is None:
                                    # Check if model started with <think> tag
                                    if '<think' in buffer.lower():
                                        thinking_mode = True
                                    elif len(buffer) > 20:
                                        # No think tag in first 20 chars - assume no thinking
                                        thinking_mode = False
                                        yield buffer
                                        buffer = ""

                                if thinking_mode is True:
                                    # In thinking mode - look for end tag
                                    match = re.search(r'</think(?:ing)?>', buffer, re.IGNORECASE)
                                    if match:
                                        thinking_mode = False
                                        after_think = buffer[match.end():]
                                        buffer = ""
                                        if after_think.strip():
                                            yield after_think
                                elif thinking_mode is False:
                                    # Not thinking - stream directly
                                    yield content
                                    buffer = ""
                        except json.JSONDecodeError:
                            continue

                # Yield any remaining buffer
                if buffer:
                    clean = self.strip_thinking_tags(buffer)
                    if clean:
                        yield clean
        except Exception as e:
            yield f"Error: {str(e)}"


def get_chat_service(db: Session) -> ChatService:
    return ChatService(db)

