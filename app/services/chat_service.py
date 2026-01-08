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
from app.services.load_balancer import LoadBalancer, parse_server_urls

if TYPE_CHECKING:
    from app.models import User

logger = logging.getLogger(__name__)

# RAG context injection template
RAG_CONTEXT_TEMPLATE = """

---
The following is relevant context from the user's indexed codebase/documents that may help answer their question:

{context}

---
Use this context to provide accurate and helpful responses. If the context doesn't seem relevant to the question, you can ignore it."""

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

    def _get_load_balancer(self) -> Optional[LoadBalancer]:
        """Get load balancer if chat servers are configured"""
        chat_server_urls = self._settings.get("chat_server_urls", "")
        servers = parse_server_urls(chat_server_urls)
        if servers:
            timeout = int(self._settings.get("ollama_timeout", "120000")) / 1000
            api_key = self._settings.get("chat_server_api_key", "")
            model = self._settings.get("ollama_model", "default")
            return LoadBalancer(servers, timeout=timeout, api_key=api_key if api_key else None, model=model)
        return None

    def _get_rag_context(self, user_message: str) -> str:
        """Get RAG context for the user message if enabled."""
        def log(msg):
            with open("/tmp/rag_debug.log", "a") as f:
                f.write(f"{msg}\n")

        if not self.user:
            log("[RAG] No user, skipping RAG")
            return ""

        # Check if RAG is enabled
        rag_enabled = self._settings.get("rag_enabled", "true").lower() == "true"
        auto_context = self._settings.get("rag_auto_context", "true").lower() == "true"

        log(f"[RAG] user={self.user.id}, enabled={rag_enabled}, auto={auto_context}")

        if not rag_enabled or not auto_context:
            return ""

        try:
            from app.services.rag_service import get_rag_service
            log(f"[RAG] Getting RAG service...")
            rag_service = get_rag_service(self.db, self.user.id)
            log(f"[RAG] Querying: {user_message[:100]}...")
            results = rag_service.query(user_message)
            log(f"[RAG] Query returned {len(results)} results")

            if results:
                context = rag_service.format_context(results)
                log(f"[RAG] Injecting {len(context)} chars of context")
                return RAG_CONTEXT_TEMPLATE.format(context=context)
        except Exception as e:
            log(f"[RAG] ERROR: {e}")
            import traceback
            log(traceback.format_exc())

        return ""

    def _inject_rag_context(self, messages: list[dict], user_message: str) -> list[dict]:
        """Inject RAG context into messages if available."""
        rag_context = self._get_rag_context(user_message)
        if not rag_context:
            return messages

        # Find and enhance the system message - put RAG context BEFORE the persona
        enhanced_messages = []
        system_found = False

        for msg in messages:
            if msg.get("role") == "system" and not system_found:
                # Put RAG context at the START so model sees it first
                enhanced_messages.append({
                    "role": "system",
                    "content": rag_context + "\n\n" + msg["content"]
                })
                system_found = True
            else:
                enhanced_messages.append(msg)

        # Log what we're sending
        with open("/tmp/rag_debug.log", "a") as f:
            f.write(f"[INJECT] System message length: {len(enhanced_messages[0]['content']) if enhanced_messages else 0}\n")

        # If no system message was found, add one with RAG context
        if not system_found:
            enhanced_messages.insert(0, {
                "role": "system",
                "content": f"You are a helpful AI assistant.{rag_context}"
            })

        return enhanced_messages

    def strip_thinking_tags(self, response: str) -> str:
        """Strip thinking tags from AI response"""
        from app.services.text_utils import strip_thinking_tags
        return strip_thinking_tags(response)

    async def chat(self, messages: list[dict]) -> str:
        """Non-streaming chat completion using inference factory or custom AI service"""
        try:
            # Extract user message for RAG query (last user message)
            user_message = ""
            for msg in reversed(messages):
                if msg.get("role") == "user":
                    user_message = msg.get("content", "")
                    break

            # Inject RAG context if available
            messages = self._inject_rag_context(messages, user_message)

            # Check for site-wide load balancer first
            load_balancer = self._get_load_balancer()
            if load_balancer:
                result = await load_balancer.chat(
                    messages=messages,
                    temperature=self.temperature,
                    top_p=self.top_p,
                    max_tokens=self.num_predict
                )
                if "error" in result:
                    return f"Error: {result['error'].get('message', 'Unknown error')}"
                content = result["choices"][0]["message"]["content"]
                return self.strip_thinking_tags(content)

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
        with open("/tmp/rag_debug.log", "a") as f:
            f.write(f"[STREAM] chat_stream called, user={self.user.id if self.user else None}\n")
        try:
            # Extract user message for RAG query (last user message)
            user_message = ""
            for msg in reversed(messages):
                if msg.get("role") == "user":
                    user_message = msg.get("content", "")
                    break

            with open("/tmp/rag_debug.log", "a") as f:
                f.write(f"[STREAM] user_message={user_message[:50]}...\n")

            # Inject RAG context if available
            messages = self._inject_rag_context(messages, user_message)

            # Check for site-wide load balancer first
            load_balancer = self._get_load_balancer()
            if load_balancer:
                # Stream from load-balanced server with thinking tag filtering
                buffer = ""
                thinking_done = False

                async for chunk in load_balancer.chat_stream(
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
                            if "error" in data:
                                yield f"Error: {data['error'].get('message', 'Unknown error')}"
                                return
                            content = data.get("choices", [{}])[0].get("delta", {}).get("content", "")
                            if content:
                                buffer += content

                                if not thinking_done:
                                    match = re.search(r'</think(?:ing)?>', buffer, re.IGNORECASE)
                                    if match:
                                        thinking_done = True
                                        after_think = buffer[match.end():]
                                        buffer = ""
                                        if after_think.strip():
                                            yield after_think
                                    elif len(buffer) > 50 and '<think' not in buffer.lower():
                                        thinking_done = True
                                        yield buffer
                                        buffer = ""
                                else:
                                    yield content
                        except json.JSONDecodeError:
                            continue

                if buffer:
                    clean = self.strip_thinking_tags(buffer)
                    if clean:
                        yield clean
                return

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
                        # Check if model started with <think> tag (ignore leading whitespace)
                        buffer_stripped = buffer.lstrip()
                        if buffer_stripped.lower().startswith('<think'):
                            thinking_mode = True
                        elif len(buffer_stripped) > 30:
                            # No think tag in first 30 non-whitespace chars - assume no thinking
                            thinking_mode = False
                            yield buffer
                            buffer = ""

                    elif thinking_mode is True:
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
                                    # Check if model started with <think> tag (ignore leading whitespace)
                                    buffer_stripped = buffer.lstrip()
                                    if buffer_stripped.lower().startswith('<think'):
                                        thinking_mode = True
                                    elif len(buffer_stripped) > 30:
                                        # No think tag in first 30 non-whitespace chars - assume no thinking
                                        thinking_mode = False
                                        yield buffer
                                        buffer = ""

                                elif thinking_mode is True:
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

