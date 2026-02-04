import asyncio
import httpx
import json
import logging
import re
import time
import uuid
from typing import AsyncGenerator, Optional, Dict, Any, List
from sqlalchemy.orm import Session
from app.models import Setting

logger = logging.getLogger(__name__)


# Global semaphore for request limiting (shared across instances)
_request_semaphore: Optional[asyncio.Semaphore] = None
_semaphore_limit: int = 2
_semaphore_lock = asyncio.Lock()

# Global HTTP client pool for connection reuse (performance optimization)
_http_client: Optional[httpx.AsyncClient] = None
_http_client_lock = asyncio.Lock()


async def _get_semaphore(max_concurrent: int) -> asyncio.Semaphore:
    """Get or create global semaphore for request limiting (async-safe)"""
    global _request_semaphore, _semaphore_limit
    async with _semaphore_lock:
        if _request_semaphore is None or _semaphore_limit != max_concurrent:
            _request_semaphore = asyncio.Semaphore(max_concurrent)
            _semaphore_limit = max_concurrent
        return _request_semaphore


async def _get_http_client(timeout: float = 120) -> httpx.AsyncClient:
    """Get or create global HTTP client with connection pooling (async-safe)"""
    global _http_client
    async with _http_client_lock:
        if _http_client is None or _http_client.is_closed:
            _http_client = httpx.AsyncClient(
                timeout=httpx.Timeout(timeout, connect=10.0),
                limits=httpx.Limits(max_keepalive_connections=10, max_connections=20)
            )
        return _http_client


class OllamaService:
    def __init__(self, db: Session):
        self.db = db
        self._load_settings()

    def _load_settings(self):
        from app.database import safe_query_settings
        settings = safe_query_settings(self.db)
        
        # Helper to get setting with fallback for empty strings
        def get_setting(key: str, default: str) -> str:
            val = settings.get(key, default)
            return val if val else default

        # Ollama connection settings
        self.ollama_url = get_setting("ollama_url", "http://localhost:11434").rstrip('/')

        self.default_model = get_setting("ollama_model", "llama3")
        self.timeout = int(get_setting("ollama_timeout", "300000")) / 1000
        self.max_concurrent = int(get_setting("ollama_max_concurrent", "1"))
        self.system_prompt = get_setting("ollama_system_prompt", "You are a helpful, friendly AI assistant.")
        # API format: "ollama" for /api/chat, "openai" for /v1/chat/completions
        self.api_format = get_setting("ollama_api_format", "ollama")

        # Advanced model settings
        self.temperature = float(get_setting("ollama_temperature", "0.7"))
        self.top_p = float(get_setting("ollama_top_p", "0.9"))
        self.top_k = int(get_setting("ollama_top_k", "40"))
        self.repeat_penalty = float(get_setting("ollama_repeat_penalty", "1.1"))
        self.num_ctx = int(get_setting("ollama_num_ctx", "4096"))
        self.num_predict = int(get_setting("ollama_num_predict", "2048"))
        # keep_alive: -1 = forever, 0 = unload immediately, positive = seconds
        keep_alive_str = get_setting("ollama_keep_alive", "-1")
        self.keep_alive = int(keep_alive_str) if keep_alive_str.lstrip('-').isdigit() else -1
        self.stop_sequences = [s.strip() for s in get_setting("ollama_stop", "").split(",") if s.strip()]

        # Additional advanced settings
        seed_str = get_setting("ollama_seed", "")
        self.seed = int(seed_str) if seed_str.strip() else None
        self.mirostat = int(get_setting("ollama_mirostat", "0"))
        self.mirostat_eta = float(get_setting("ollama_mirostat_eta", "0.1"))
        self.mirostat_tau = float(get_setting("ollama_mirostat_tau", "5.0"))
        self.tfs_z = float(get_setting("ollama_tfs_z", "1.0"))

    def get_model_options(self, **overrides) -> Dict[str, Any]:
        """Get model options, allowing overrides from request"""
        options = {
            "temperature": overrides.get("temperature", self.temperature),
            "top_p": overrides.get("top_p", self.top_p),
            "top_k": overrides.get("top_k", self.top_k),
            "repeat_penalty": overrides.get("repeat_penalty", self.repeat_penalty),
            "num_ctx": overrides.get("num_ctx", self.num_ctx),
            "num_predict": overrides.get("max_tokens", self.num_predict),
            "mirostat": self.mirostat,
            "mirostat_eta": self.mirostat_eta,
            "mirostat_tau": self.mirostat_tau,
            "tfs_z": self.tfs_z,
        }

        # Add seed if set
        if self.seed is not None:
            options["seed"] = self.seed

        # Add stop sequences
        stop = overrides.get("stop", self.stop_sequences)
        if stop:
            options["stop"] = stop if isinstance(stop, list) else [stop]

        return options

    def strip_thinking_tags(self, response: str) -> str:
        """Strip thinking tags from AI response"""
        from app.services.text_utils import strip_thinking_tags
        return strip_thinking_tags(response)

    async def list_models(self) -> List[Dict[str, Any]]:
        """List available Ollama models"""
        client = await _get_http_client(timeout=30)
        try:
            response = await client.get(f"{self.ollama_url}/api/tags")
            response.raise_for_status()
            data = response.json()
            return data.get("models", [])
        except Exception as e:
            logger.error(f"Failed to list Ollama models: {e}")
            return []

    async def chat_completion(
        self,
        messages: List[Dict[str, Any]],
        model: Optional[str] = None,
        stream: bool = False,
        **kwargs
    ) -> Dict[str, Any]:
        """
        Non-streaming chat completion.
        Returns OpenAI-compatible response format.
        Uses round-robin load balancing across configured hosts.
        Supports both Ollama API (/api/chat) and OpenAI API (/v1/chat/completions).
        """
        model = model or self.default_model
        options = self.get_model_options(**kwargs)

        # Acquire shared GPU lock to prevent LLM and image from running simultaneously
        from app.services.locks import GPUResourceLock
        import uuid
        request_id = f"OLLAMA-{uuid.uuid4().hex[:8]}"
        async with GPUResourceLock("LLM", request_id):
            # Acquire semaphore for rate limiting
            semaphore = await _get_semaphore(self.max_concurrent)

            async with semaphore:
                host = self.ollama_url
                client = await _get_http_client(timeout=self.timeout)
                try:
                    if self.api_format == "openai":
                        # OpenAI-compatible API format (works with posterchanai, vLLM, etc.)
                        response = await client.post(
                            f"{host}/v1/chat/completions",
                            json={
                                "model": model,
                                "messages": messages,
                                "stream": False,
                                "temperature": options.get("temperature", 0.7),
                                "top_p": options.get("top_p", 0.9),
                                "max_tokens": options.get("num_predict", 2048),
                                "stop": options.get("stop", []) or None
                            }
                        )
                        response.raise_for_status()
                        data = response.json()

                        # Strip thinking tags from response
                        if "choices" in data and data["choices"]:
                            content = data["choices"][0].get("message", {}).get("content", "")
                            data["choices"][0]["message"]["content"] = self.strip_thinking_tags(content)

                        return data
                    else:
                        # Ollama native API format
                        response = await client.post(
                            f"{host}/api/chat",
                            json={
                                "model": model,
                                "messages": messages,
                                "stream": False,
                                "options": options,
                                "keep_alive": self.keep_alive
                            }
                        )
                        response.raise_for_status()
                        ollama_data = response.json()

                        # Convert to OpenAI format
                        content = ollama_data.get("message", {}).get("content", "")
                        content = self.strip_thinking_tags(content)

                        data = {
                            "id": f"chatcmpl-{int(time.time())}",
                            "object": "chat.completion",
                            "created": int(time.time()),
                            "model": model,
                            "system_fingerprint": "fp_ollama",
                            "choices": [{
                                "index": 0,
                                "message": {"role": "assistant", "content": content},
                                "finish_reason": "stop"
                            }],
                            "usage": {
                                "prompt_tokens": ollama_data.get("prompt_eval_count", 0),
                                "completion_tokens": ollama_data.get("eval_count", 0),
                                "total_tokens": ollama_data.get("prompt_eval_count", 0) + ollama_data.get("eval_count", 0)
                            }
                        }

                        return data

                except httpx.HTTPStatusError as e:
                    return {
                        "error": {
                            "message": f"API returned status {e.response.status_code}",
                            "type": "api_error",
                            "code": e.response.status_code
                        }
                    }
                except Exception as e:
                    return {
                        "error": {
                            "message": str(e),
                            "type": "api_error"
                        }
                    }

    async def chat_completion_stream(
        self,
        messages: List[Dict[str, Any]],
        model: Optional[str] = None,
        **kwargs
    ) -> AsyncGenerator[str, None]:
        """
        Streaming chat completion.
        Yields SSE-formatted chunks compatible with OpenAI API.
        Uses round-robin load balancing across configured hosts.
        Supports both Ollama API (/api/chat) and OpenAI API (/v1/chat/completions).
        """
        model = model or self.default_model
        options = self.get_model_options(**kwargs)

        # Generate unique ID for this completion
        completion_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"
        created = int(time.time())

        # Acquire shared GPU lock to prevent LLM and image from running simultaneously
        from app.services.locks import GPUResourceLock
        request_id = f"OLLAMA-STREAM-{completion_id[:8]}"
        async with GPUResourceLock("LLM", request_id):
            # Acquire semaphore for rate limiting
            semaphore = await _get_semaphore(self.max_concurrent)

            async with semaphore:
                host = self.ollama_url
                async with httpx.AsyncClient(timeout=self.timeout) as client:
                    try:
                        if self.api_format == "openai":
                            # OpenAI-compatible API format
                            async with client.stream(
                                "POST",
                                f"{host}/v1/chat/completions",
                                json={
                                    "model": model,
                                    "messages": messages,
                                    "stream": True,
                                    "temperature": options.get("temperature", 0.7),
                                    "top_p": options.get("top_p", 0.9),
                                    "max_tokens": options.get("num_predict", 2048),
                                    "stop": options.get("stop", []) or None
                                }
                            ) as response:
                                response.raise_for_status()

                                buffer = ""
                                thinking_done = False

                                async for line in response.aiter_lines():
                                    line = line.strip()
                                    if not line:
                                        continue

                                    # OpenAI SSE format: "data: {...}" or "data: [DONE]"
                                    if line.startswith("data: "):
                                        data_str = line[6:]  # Remove "data: " prefix
                                        if data_str == "[DONE]":
                                            yield "data: [DONE]\n\n"
                                            break

                                        try:
                                            data = json.loads(data_str)
                                            content = ""
                                            if "choices" in data and data["choices"]:
                                                delta = data["choices"][0].get("delta", {})
                                                content = delta.get("content", "")

                                            if content:
                                                buffer += content

                                                # Handle thinking tags
                                                if not thinking_done:
                                                    match = re.search(r'</think(?:ing)?>', buffer, re.IGNORECASE)
                                                    if match:
                                                        thinking_done = True
                                                        after_think = buffer[match.end():]
                                                        if after_think:
                                                            chunk = {
                                                                "id": completion_id,
                                                                "object": "chat.completion.chunk",
                                                                "created": created,
                                                                "model": model,
                                                                "choices": [{
                                                                    "index": 0,
                                                                    "delta": {"content": after_think},
                                                                    "finish_reason": None
                                                                }]
                                                            }
                                                            yield f"data: {json.dumps(chunk)}\n\n"
                                                        buffer = after_think
                                                else:
                                                    # Pass through the chunk
                                                    yield f"{line}\n\n"

                                        except json.JSONDecodeError:
                                            continue
                        else:
                            # Ollama native API format
                            async with client.stream(
                                "POST",
                                f"{host}/api/chat",
                                json={
                                    "model": model,
                                    "messages": messages,
                                    "stream": True,
                                    "options": options,
                                    "keep_alive": self.keep_alive
                                }
                            ) as response:
                                response.raise_for_status()

                                buffer = ""
                                thinking_done = False

                                async for line in response.aiter_lines():
                                    if not line.strip():
                                        continue

                                    try:
                                        # Parse native Ollama JSON response
                                        data = json.loads(line)
                                        content = data.get("message", {}).get("content", "")

                                        if content:
                                            buffer += content

                                            # Handle thinking tags
                                            if not thinking_done:
                                                match = re.search(r'</think(?:ing)?>', buffer, re.IGNORECASE)
                                                if match:
                                                    thinking_done = True
                                                    after_think = buffer[match.end():]
                                                    if after_think:
                                                        chunk = {
                                                            "id": completion_id,
                                                            "object": "chat.completion.chunk",
                                                            "created": created,
                                                            "model": model,
                                                            "choices": [{
                                                                "index": 0,
                                                                "delta": {"content": after_think},
                                                                "finish_reason": None
                                                            }]
                                                        }
                                                        yield f"data: {json.dumps(chunk)}\n\n"
                                                    buffer = after_think
                                            else:
                                                # Normal content, convert to OpenAI SSE format
                                                chunk = {
                                                    "id": completion_id,
                                                    "object": "chat.completion.chunk",
                                                    "created": created,
                                                    "model": model,
                                                    "choices": [{
                                                        "index": 0,
                                                        "delta": {"content": content},
                                                        "finish_reason": None
                                                    }]
                                                }
                                                yield f"data: {json.dumps(chunk)}\n\n"

                                        # Check if done
                                        if data.get("done", False):
                                            yield "data: [DONE]\n\n"
                                            break

                                    except json.JSONDecodeError:
                                        continue

                    except httpx.HTTPStatusError as e:
                        error_chunk = {
                            "error": {
                                "message": f"Ollama returned status {e.response.status_code}",
                                "type": "api_error"
                            }
                        }
                        yield f"data: {json.dumps(error_chunk)}\n\n"
                    except Exception as e:
                        error_chunk = {
                            "error": {
                                "message": str(e),
                                "type": "api_error"
                            }
                        }
                        yield f"data: {json.dumps(error_chunk)}\n\n"

    async def generate(
        self,
        prompt: str,
        model: Optional[str] = None,
        system: Optional[str] = None,
        **kwargs
    ) -> Dict[str, Any]:
        """
        Legacy generate endpoint (Ollama native format).
        Uses round-robin load balancing across configured hosts.
        """
        model = model or self.default_model
        options = self.get_model_options(**kwargs)

        # Acquire shared GPU lock to prevent LLM and image from running simultaneously
        from app.services.locks import GPUResourceLock
        import uuid
        request_id = f"OLLAMA-GEN-{uuid.uuid4().hex[:8]}"
        async with GPUResourceLock("LLM", request_id):
            semaphore = await _get_semaphore(self.max_concurrent)

            async with semaphore:
                host = self.ollama_url
                async with httpx.AsyncClient(timeout=self.timeout) as client:
                    try:
                        payload = {
                            "model": model,
                            "prompt": prompt,
                            "stream": False,
                            "options": options,
                            "keep_alive": self.keep_alive
                        }
                        if system:
                            payload["system"] = system

                        response = await client.post(
                            f"{host}/api/generate",
                            json=payload
                        )
                        response.raise_for_status()
                        return response.json()

                    except Exception as e:
                        return {"error": str(e)}


def get_ollama_service(db: Session) -> OllamaService:
    return OllamaService(db)
