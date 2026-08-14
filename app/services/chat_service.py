import json
import os
import asyncio
import logging
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import AsyncGenerator, Optional, TYPE_CHECKING
from sqlalchemy.orm import Session
from app.services.inference_factory import get_inference_service
from app.services.load_balancer import LoadBalancer, NoHealthyServersError, parse_server_urls

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
        from app.database import safe_query_settings
        self._settings = safe_query_settings(self.db)
        default_prompt = """You are a capable AI assistant with intelligent action capabilities. When writing code, use markdown code blocks with the language specified.

INTELLIGENT ACTIONS:
The system automatically detects when you want to perform an action and executes it. Just describe what you want naturally:
- "Send an email to john@example.com saying I'll be late"
- "Search for the latest AI news"
- "Generate an image of a sunset over mountains"

The system will parse your request, extract relevant data, and take action automatically.

AVAILABLE COMMANDS (can also be typed directly):

EMAIL: mail, mail unread, mail send <to> <msg>, mail read/delete/archive <acct> <id>

NEWS: news, dailynews

SEARCH: search <query>, images <query>

GENERATE: geni <prompt>

YOUTUBE: yt <url> (summarize), ytdl <url> (download)

TORRENTS: torrents, torrents list, torrents download/pause/resume/rm <num>

TRANSLATE: translate <language>

SYSTEM: logs, help

Provide clear, concise responses. Keep confirmations brief and professional."""
        self.system_prompt = self._settings.get("ollama_system_prompt") or default_prompt
        # Helper to get setting with fallback for empty strings
        def get_setting(key: str, default: str) -> str:
            val = self._settings.get(key, default)
            return val if val else default
        # These are used for chat_stream kwargs
        self.temperature = float(get_setting("ollama_temperature", "0.2"))
        self.top_p = float(get_setting("ollama_top_p", "0.9"))
        self.num_predict = int(get_setting("ollama_num_predict", "2048"))
        # Stop token(s) - can be comma-separated for multiple
        stop_setting = get_setting("ollama_stop", "").strip()
        self.stop = [s.strip() for s in stop_setting.split(",") if s.strip()] if stop_setting else None

    def _get_load_balancer(self) -> Optional[LoadBalancer]:
        """Get load balancer if chat servers are configured"""
        chat_server_urls = self._settings.get("chat_server_urls", "")
        # Use all servers from admin UI - round-robin between all configured servers
        servers = parse_server_urls(chat_server_urls, exclude_self=False)
        if servers:
            timeout_str = self._settings.get("ollama_timeout", "300000")
            timeout = int(timeout_str if timeout_str else "300000") / 1000
            # Use llm_model_path like the IPEX service does (extract filename from full path)
            llm_path = self._settings.get("llm_model_path", "")
            if llm_path:
                model = os.path.basename(llm_path)
            else:
                model = "default"
            logger.info(f"[CHAT SERVICE] Creating LoadBalancer with {len(servers)} server(s): {servers}, model={model!r} (from llm_model_path), timeout={timeout}s")
            return LoadBalancer(servers, timeout=timeout, model=model)
        else:
            logger.debug(f"[CHAT SERVICE] No chat servers configured (chat_server_urls='{chat_server_urls}')")
        return None

    def strip_thinking_tags(self, response: str) -> str:
        """Strip thinking tags from AI response"""
        from app.services.text_utils import strip_thinking_tags
        return strip_thinking_tags(response)

    @staticmethod
    def _ensure_alternating_roles(messages: list[dict]) -> list[dict]:
        """Ensure messages alternate user/assistant/user/assistant...
        Merges consecutive same-role messages and removes invalid sequences."""
        if not messages:
            return messages

        result = []
        prev_role = None
        for msg in messages:
            role = msg.get("role", "user")
            content = msg.get("content", "")

            if role == "system":
                result.append(msg)
                continue

            # tool/function result messages sit between assistant and user turns and must
            # pass through intact (each has its own tool_call_id). Reset prev_role so the
            # next assistant turn isn't incorrectly merged with the one before the tool block.
            if role in ("tool", "function"):
                result.append(msg)
                prev_role = None
                continue

            if role == prev_role and result:
                last = result[-1]
                if isinstance(last.get("content"), str) and isinstance(content, str):
                    result[-1]["content"] = last["content"] + "\n\n" + content
                elif isinstance(last.get("content"), str):
                    result[-1]["content"] = last["content"]
                continue

            result.append(msg)
            prev_role = role
        
        if result and result[0].get("role") not in ("system", "user"):
            result.insert(0, {"role": "user", "content": "Please respond."})
        
        return result

    async def _try_provider(self, messages: list[dict]) -> Optional[str]:
        """CONSUMER: on a shared peer's turn in the round-robin, offload this chat to that machine over
        Nostr and return the finished (thinking-stripped) reply; None to run locally (our turn, no peers,
        or the peer failed/empty). Non-streaming — Nostr has no token stream yet."""
        from app.services import nostr_dvm
        provider = nostr_dvm.pick_provider(self._settings)
        if not provider:
            return None
        completion = await nostr_dvm.offload_chat(
            messages, provider, self._settings, temperature=self.temperature, top_p=self.top_p,
            max_tokens=self.num_predict, stop=self.stop)
        if not completion:
            return None
        content = self.strip_thinking_tags(
            (completion.get("choices") or [{}])[0].get("message", {}).get("content", "") or "")
        return content or None

    async def chat(self, messages: list[dict]) -> str:
        """Non-streaming chat completion using inference factory or custom AI service"""
        try:
            # Ensure messages alternate user/assistant properly
            messages = self._ensure_alternating_roles(messages)

            # Consumer: on a shared peer's turn, offload to it; else run locally / via the IP LB below.
            reply = await self._try_provider(messages)
            if reply is not None:
                return reply

            # Check for site-wide load balancer first
            load_balancer = self._get_load_balancer()
            if load_balancer:
                try:
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
                except NoHealthyServersError:
                    logger.info("Load balancer unavailable, using local inference for chat()")
                except Exception as e:
                    logger.warning(f"Load balancer error in chat(): {e}, falling back to local", exc_info=True)

            # Use the server's default AI service. The VRAM swap is NOT done here: it happens
            # inside `_ensure_model_loaded`, under the GPU lock `chat_completion` takes. Doing it
            # here meant swapping with no lock held at all.
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
        from app.services.load_balancer import NoHealthyServersError

        try:
            # Ensure messages alternate user/assistant properly
            messages = self._ensure_alternating_roles(messages)

            # Consumer: on a shared peer's turn, offload to it (one thinking-stripped chunk); else local.
            reply = await self._try_provider(messages)
            if reply is not None:
                yield reply
                return

            # Check for site-wide load balancer first
            load_balancer = self._get_load_balancer()
            if load_balancer:
                try:
                    # Stream from load-balanced server with thinking tag filtering
                    buffer = ""
                    thinking_done = False
                    received_any_content = False

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
                                    error_msg = data['error'].get('message', 'Unknown error')
                                    logger.error(f"[CHAT SERVICE] Error from load balancer: {error_msg}")
                                    # Raise exception to trigger fallback to local
                                    raise NoHealthyServersError(f"Remote server returned error: {error_msg}")
                                content = data.get("choices", [{}])[0].get("delta", {}).get("content", "")
                                if content:
                                    received_any_content = True
                                    if not thinking_done:
                                        buffer += content
                                        match = THINKING_CLOSE_PATTERN.search(buffer)
                                        if match:
                                            thinking_done = True
                                            after_think = buffer[match.end():]
                                            buffer = ""
                                            if after_think.strip():
                                                yield after_think
                                        elif len(buffer) >= 50 and not has_thinking_open(buffer):
                                            thinking_done = True
                                            yield buffer
                                            buffer = ""
                                    else:
                                        yield content
                            except json.JSONDecodeError:
                                continue

                    # Yield any remaining buffer (important for short responses < 50 chars)
                    if buffer:
                        clean = self.strip_thinking_tags(buffer)
                        if clean:
                            yield clean
                            received_any_content = True
                            logger.debug(f"Yielded remaining buffer: {len(clean)} chars")
                    
                    # If we received content, we're done
                    if received_any_content:
                        return
                    else:
                        # No content received - raise exception to trigger fallback
                        logger.warning("Load balancer returned no content, falling back to local")
                        raise NoHealthyServersError("No content from load balancer")
                except NoHealthyServersError:
                    # Load balancer failed - fall back to local inference
                    logger.info("Load balancer unavailable, using local inference")
                except Exception as e:
                    logger.warning(f"Load balancer error: {e}, falling back to local", exc_info=True)

            # Use server's default AI service
            service = get_inference_service(self.db)

            # Use direct content streaming for native backend
            if hasattr(service, 'stream_chat_content'):
                """THE GPU LOCK COVERS THE VRAM SWAP AND THE WHOLE GENERATION, and it used to cover
                neither. This is the web UI's chat path — `prepare_vram_for_llm` ran here, outside
                any lock, and `stream_chat_content` is a plain generator that takes none either. So
                a chat message sent while a `geni` was rendering unloaded the image model out from
                under a generation that HELD the lock: the image job died with
                UR_RESULT_ERROR_OUT_OF_HOST_MEMORY and llama.cpp aborted on the wreckage, taking the
                whole service down with it (2026-08-14 09:13 — one core dump, one lost chat, one
                lost geni). The same shape is written up in GPUResourceLockSync's docstring for the
                download path; this is that bug on the hottest path in the app.

                The lock is held across the yields ON PURPOSE — it is released when this generator
                finishes or the client disconnects and it is closed, exactly like
                `chat_completion_stream`. And nothing inside may take it again: `flock` blocks a
                second acquisition from the SAME process on a different fd, so adding a lock to
                `stream_chat_content` as well would deadlock every web-UI chat."""
                from app.services.locks import GPUResourceLock
                _req_id = f"CHAT-{uuid.uuid4().hex[:8]}"
                async with GPUResourceLock("LLM", _req_id,
                                           cpu_mode=getattr(service, "cpu_mode", False)):
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

                    # Stream with thinking tag filtering (same logic as load balancer path)
                    buffer = ""
                    thinking_done = False
                    while True:
                        content = await queue.get()
                        if content is None:
                            break

                        if content.startswith("Error:"):
                            yield content
                            return

                        if not thinking_done:
                            buffer += content
                            match = THINKING_CLOSE_PATTERN.search(buffer)
                            if match:
                                thinking_done = True
                                after_think = buffer[match.end():]
                                buffer = ""
                                if after_think.strip():
                                    yield after_think
                            elif len(buffer) >= 50 and not has_thinking_open(buffer):
                                thinking_done = True
                                yield buffer
                                buffer = ""
                        else:
                            yield content

                    if buffer:
                        clean = self.strip_thinking_tags(buffer)
                        if clean:
                            yield clean
            else:
                # `chat_completion_stream` takes the GPU lock itself and swaps inside it
                # (`_ensure_model_loaded`), so there is deliberately nothing to do here.
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

