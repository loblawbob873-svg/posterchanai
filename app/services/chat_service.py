import json
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
        default_prompt = """You are a helpful, friendly AI assistant with intelligent action capabilities. When writing code, use markdown code blocks with the language specified.

INTELLIGENT ACTIONS:
The system automatically detects when you want to perform an action and executes it. Just describe what you want naturally:
- "Add meeting with John tomorrow at 3pm to my calendar"
- "Here's an event from my email: [paste content] - add this to calendar"
- "Remind me to call mom" (adds to todo)
- "Send an email to john@example.com saying I'll be late"
- "Play some relaxing music"
- "Search for the latest AI news"
- "Generate an image of a sunset over mountains"

The system will parse your request, extract relevant data, and take action automatically.

AVAILABLE COMMANDS (can also be typed directly):

EMAIL: mail, mail unread, mail send <to> <msg>, mail read/delete/archive <acct> <id>

CALENDAR: cal, cal today, cal week, cal add <event> <time>

CONTACTS: contacts all, contacts <name>, contacts add <name> <phone>

TODO: todo, todo add <task>, todo rm <number>

MUSIC: music, music search <query>, music mood <vibe>, music random, music skip

NEWS: news, dailynews

SEARCH: search <query>, images <query>

GENERATE: geni <prompt>

YOUTUBE: yt <url> (summarize), ytdl <url> (download)

TORRENTS: torrents, torrents list, torrents download/pause/resume/rm <num>

TRANSLATE: translate <language>

SYSTEM: firewall, logs, help

For general questions, respond conversationally. The system handles action detection automatically."""
        self.system_prompt = self._settings.get("ollama_system_prompt") or default_prompt
        # These are used for chat_stream kwargs
        self.temperature = float(self._settings.get("ollama_temperature", "0.7"))
        self.top_p = float(self._settings.get("ollama_top_p", "0.9"))
        self.num_predict = int(self._settings.get("ollama_num_predict", "2048"))
        # Stop token(s) - can be comma-separated for multiple
        stop_setting = self._settings.get("ollama_stop", "").strip()
        self.stop = [s.strip() for s in stop_setting.split(",") if s.strip()] if stop_setting else None

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
            model = self._settings.get("ollama_model", "default")
            api_key = self._settings.get("chat_server_api_key", "")
            logger.debug(f"Creating LoadBalancer with {len(servers)} servers, model={model}")
            return LoadBalancer(servers, timeout=timeout, model=model, api_key=api_key if api_key else None)
        return None

    def _get_rag_context(self, user_message: str) -> str:
        """Get RAG context for the user message if enabled."""
        if not self.user:
            return ""

        # Check if RAG is enabled
        rag_enabled = self._settings.get("rag_enabled", "true").lower() == "true"
        auto_context = self._settings.get("rag_auto_context", "true").lower() == "true"

        if not rag_enabled or not auto_context:
            return ""

        try:
            from app.services.rag_service import get_rag_service
            rag_service = get_rag_service(self.db, self.user.id)
            results = rag_service.query(user_message)

            if results:
                context = rag_service.format_context(results)
                logger.debug(f"[RAG] Injecting {len(context)} chars of context")
                return RAG_CONTEXT_TEMPLATE.format(context=context)
        except Exception as e:
            logger.debug(f"[RAG] Error getting context: {e}")

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
                    max_tokens=self.num_predict,
                    stop=self.stop
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
                    max_tokens=self.num_predict,
                    stop=self.stop
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
                    max_tokens=self.num_predict,
                    stop=self.stop
                )
                if "error" in result:
                    return f"Error: {result['error'].get('message', 'Unknown error')}"
                content = result["choices"][0]["message"]["content"]
                return self.strip_thinking_tags(content)
        except Exception as e:
            return f"Error: {str(e)}"

    async def chat_stream(self, messages: list[dict]) -> AsyncGenerator[str, None]:
        """Streaming chat completion - uses async queue to avoid blocking event loop"""
        # Import thinking tag utilities from central location
        from app.services.text_utils import THINKING_CLOSE_PATTERN, THINKING_OPEN_PREFIXES, has_thinking_open

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
                # Stream from load-balanced server with thinking tag filtering
                buffer = ""
                thinking_done = False

                async for chunk in load_balancer.chat_stream(
                    messages=messages,
                    temperature=self.temperature,
                    top_p=self.top_p,
                    max_tokens=self.num_predict,
                    stop=self.stop
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
                                if not thinking_done:
                                    buffer += content
                                    match = THINKING_CLOSE_PATTERN.search(buffer)
                                    if match:
                                        thinking_done = True
                                        after_think = buffer[match.end():]
                                        buffer = ""  # Clear buffer - no longer needed
                                        if after_think.strip():
                                            yield after_think
                                    elif len(buffer) > 50 and not has_thinking_open(buffer):
                                        thinking_done = True
                                        yield buffer
                                        buffer = ""  # Clear buffer - no longer needed
                                else:
                                    # Thinking is done, yield content directly (don't buffer)
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
                    max_tokens=self.num_predict,
                    stop=self.stop
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
                                if not thinking_done:
                                    buffer += content
                                    # Look for end of thinking tag
                                    match = THINKING_CLOSE_PATTERN.search(buffer)
                                    if match:
                                        thinking_done = True
                                        after_think = buffer[match.end():]
                                        buffer = ""  # Clear buffer - no longer needed
                                        if after_think.strip():
                                            yield after_think
                                    # Also check if no think tag in first 50 chars - assume no thinking
                                    elif len(buffer) > 50 and not has_thinking_open(buffer):
                                        thinking_done = True
                                        yield buffer
                                        buffer = ""  # Clear buffer - no longer needed
                                else:
                                    # Thinking is done, yield content directly (don't buffer)
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

                # Stream content directly (thinking tags will be stripped on save)
                while True:
                    content = await queue.get()
                    if content is None:
                        break

                    if content.startswith("Error:"):
                        yield content
                        return

                    yield content
            else:
                # Fallback to SSE parsing for Ollama - with thinking tag filtering
                buffer = ""
                thinking_mode = None  # None=unknown, True=in thinking, False=no thinking

                async for chunk in service.chat_completion_stream(
                    messages=messages,
                    temperature=self.temperature,
                    top_p=self.top_p,
                    max_tokens=self.num_predict,
                    stop=self.stop
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
                                    # Check if model started with thinking tag (ignore leading whitespace)
                                    buffer_stripped = buffer.lstrip()
                                    lower_stripped = buffer_stripped.lower()
                                    if any(lower_stripped.startswith(p) for p in THINKING_OPEN_PREFIXES):
                                        thinking_mode = True
                                    elif len(buffer_stripped) > 30:
                                        # No think tag in first 30 non-whitespace chars - assume no thinking
                                        thinking_mode = False
                                        yield buffer
                                        buffer = ""

                                elif thinking_mode is True:
                                    # In thinking mode - look for end tag
                                    match = THINKING_CLOSE_PATTERN.search(buffer)
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

