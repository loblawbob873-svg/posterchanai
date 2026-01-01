import asyncio
import httpx
import json
import re
import time
import uuid
from typing import AsyncGenerator, Optional, Dict, Any, List
from sqlalchemy.orm import Session
from app.models import Setting


# Global semaphore for request limiting (shared across instances)
_request_semaphore: Optional[asyncio.Semaphore] = None
_semaphore_limit: int = 2


def _get_semaphore(max_concurrent: int) -> asyncio.Semaphore:
    """Get or create global semaphore for request limiting"""
    global _request_semaphore, _semaphore_limit
    if _request_semaphore is None or _semaphore_limit != max_concurrent:
        _request_semaphore = asyncio.Semaphore(max_concurrent)
        _semaphore_limit = max_concurrent
    return _request_semaphore


class OllamaService:
    def __init__(self, db: Session):
        self.db = db
        self._load_settings()

    def _load_settings(self):
        settings = {s.key: s.value for s in self.db.query(Setting).all()}

        # Ollama connection settings
        self.ollama_url = settings.get("ollama_url", "http://localhost:11434")
        self.default_model = settings.get("ollama_model", "llama3")
        self.timeout = int(settings.get("ollama_timeout", "120000")) / 1000
        self.max_concurrent = int(settings.get("ollama_max_concurrent", "2"))
        self.system_prompt = settings.get("ollama_system_prompt", "You are a helpful, friendly AI assistant.")

        # Advanced model settings
        self.temperature = float(settings.get("ollama_temperature", "0.7"))
        self.top_p = float(settings.get("ollama_top_p", "0.9"))
        self.top_k = int(settings.get("ollama_top_k", "40"))
        self.repeat_penalty = float(settings.get("ollama_repeat_penalty", "1.1"))
        self.num_ctx = int(settings.get("ollama_num_ctx", "4096"))
        self.num_predict = int(settings.get("ollama_num_predict", "2048"))
        # keep_alive: -1 = forever, 0 = unload immediately, positive = seconds
        keep_alive_str = settings.get("ollama_keep_alive", "-1")
        self.keep_alive = int(keep_alive_str) if keep_alive_str.lstrip('-').isdigit() else -1
        self.stop_sequences = [s.strip() for s in settings.get("ollama_stop", "").split(",") if s.strip()]

        # Additional advanced settings
        seed_str = settings.get("ollama_seed", "")
        self.seed = int(seed_str) if seed_str.strip() else None
        self.mirostat = int(settings.get("ollama_mirostat", "0"))
        self.mirostat_eta = float(settings.get("ollama_mirostat_eta", "0.1"))
        self.mirostat_tau = float(settings.get("ollama_mirostat_tau", "5.0"))
        self.tfs_z = float(settings.get("ollama_tfs_z", "1.0"))

        # API key for external access
        self.api_key = settings.get("openai_api_key", "")

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
        matches = list(re.finditer(r'</think(?:ing)?>', response, re.IGNORECASE))
        if matches:
            last_match = matches[-1]
            return response[last_match.end():].strip()
        return response

    async def list_models(self) -> List[Dict[str, Any]]:
        """List available Ollama models"""
        async with httpx.AsyncClient(timeout=30) as client:
            try:
                response = await client.get(f"{self.ollama_url}/api/tags")
                response.raise_for_status()
                data = response.json()
                return data.get("models", [])
            except Exception as e:
                print(f"[OLLAMA] Failed to list models: {e}")
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
        """
        model = model or self.default_model
        options = self.get_model_options(**kwargs)

        # Acquire semaphore for rate limiting
        semaphore = _get_semaphore(self.max_concurrent)

        async with semaphore:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                try:
                    # Use Ollama's OpenAI-compatible endpoint
                    response = await client.post(
                        f"{self.ollama_url}/v1/chat/completions",
                        json={
                            "model": model,
                            "messages": messages,
                            "stream": False,
                            "options": options,
                            "keep_alive": self.keep_alive
                        }
                    )
                    response.raise_for_status()
                    data = response.json()

                    # Strip thinking tags from content
                    if "choices" in data and len(data["choices"]) > 0:
                        content = data["choices"][0].get("message", {}).get("content", "")
                        data["choices"][0]["message"]["content"] = self.strip_thinking_tags(content)

                    return data

                except httpx.HTTPStatusError as e:
                    return {
                        "error": {
                            "message": f"Ollama returned status {e.response.status_code}",
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
        """
        model = model or self.default_model
        options = self.get_model_options(**kwargs)

        # Generate unique ID for this completion
        completion_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"
        created = int(time.time())

        # Acquire semaphore for rate limiting
        semaphore = _get_semaphore(self.max_concurrent)

        async with semaphore:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                try:
                    async with client.stream(
                        "POST",
                        f"{self.ollama_url}/v1/chat/completions",
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
                            if line.startswith("data: "):
                                data_str = line[6:]
                                if data_str.strip() == "[DONE]":
                                    yield "data: [DONE]\n\n"
                                    break

                                try:
                                    data = json.loads(data_str)
                                    if "choices" in data and len(data["choices"]) > 0:
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
                                                        # Yield chunk with content after thinking
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
                                                # Normal content, just forward it
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
        """
        model = model or self.default_model
        options = self.get_model_options(**kwargs)

        semaphore = _get_semaphore(self.max_concurrent)

        async with semaphore:
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
                        f"{self.ollama_url}/api/generate",
                        json=payload
                    )
                    response.raise_for_status()
                    return response.json()

                except Exception as e:
                    return {"error": str(e)}


def get_ollama_service(db: Session) -> OllamaService:
    return OllamaService(db)
