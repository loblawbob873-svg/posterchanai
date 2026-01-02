"""
Custom AI Service for user-defined endpoints.
Supports both Ollama and OpenAI-compatible APIs (Open-WebUI, Posterchanai, etc.)
"""
import asyncio
import httpx
import json
import re
import time
import uuid
from typing import AsyncGenerator, Optional, Dict, Any, List


class CustomAIService:
    """
    A lightweight AI service that connects to user-defined endpoints.
    Supports both Ollama API and OpenAI-compatible APIs.
    """

    def __init__(
        self,
        api_type: str,
        url: str,
        model: str,
        api_key: Optional[str] = None,
        timeout: float = 120.0
    ):
        """
        Initialize the custom AI service.

        Args:
            api_type: "ollama" or "openai"
            url: Base URL of the AI service (e.g., http://192.168.1.100:11434)
            model: Model name to use
            api_key: Optional API key for authentication
            timeout: Request timeout in seconds
        """
        self.api_type = api_type
        self.url = url.rstrip('/')
        self.default_model = model
        self.api_key = api_key
        self.timeout = timeout

        # Default model options
        self.temperature = 0.7
        self.top_p = 0.9
        self.top_k = 40
        self.repeat_penalty = 1.1
        self.num_ctx = 4096
        self.num_predict = 2048

    def get_model_options(self, **overrides) -> Dict[str, Any]:
        """Get model options, allowing overrides from request"""
        return {
            "temperature": overrides.get("temperature", self.temperature),
            "top_p": overrides.get("top_p", self.top_p),
            "top_k": overrides.get("top_k", self.top_k),
            "repeat_penalty": overrides.get("repeat_penalty", self.repeat_penalty),
            "num_ctx": overrides.get("num_ctx", self.num_ctx),
            "num_predict": overrides.get("max_tokens", self.num_predict),
        }

    def strip_thinking_tags(self, response: str) -> str:
        """Strip thinking tags from AI response"""
        matches = list(re.finditer(r'</think(?:ing)?>', response, re.IGNORECASE))
        if matches:
            last_match = matches[-1]
            return response[last_match.end():].strip()
        return response

    async def chat_stream(
        self,
        messages: List[Dict[str, str]],
        model: Optional[str] = None,
        **kwargs
    ) -> AsyncGenerator[str, None]:
        """
        Streaming chat completion.
        Yields SSE-formatted chunks compatible with OpenAI API.
        """
        model = model or self.default_model
        completion_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"
        created = int(time.time())

        try:
            if self.api_type == "ollama":
                async for chunk in self._stream_ollama(messages, model, completion_id, created, **kwargs):
                    yield chunk
            else:
                async for chunk in self._stream_openai(messages, model, completion_id, created, **kwargs):
                    yield chunk

        except httpx.ConnectError:
            error_chunk = {
                "error": {
                    "message": f"Could not connect to custom AI service at {self.url}",
                    "type": "connection_error"
                }
            }
            yield f"data: {json.dumps(error_chunk)}\n\n"
        except httpx.TimeoutException:
            error_chunk = {
                "error": {
                    "message": "Request to custom AI service timed out",
                    "type": "timeout_error"
                }
            }
            yield f"data: {json.dumps(error_chunk)}\n\n"
        except Exception as e:
            error_chunk = {
                "error": {
                    "message": f"Custom AI service error: {str(e)}",
                    "type": "api_error"
                }
            }
            yield f"data: {json.dumps(error_chunk)}\n\n"

    async def _stream_ollama(
        self,
        messages: List[Dict[str, str]],
        model: str,
        completion_id: str,
        created: int,
        **kwargs
    ) -> AsyncGenerator[str, None]:
        """Stream from Ollama API"""
        options = self.get_model_options(**kwargs)

        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(self.timeout, connect=10.0)) as client:
                async with client.stream(
                    "POST",
                    f"{self.url}/api/chat",
                    json={
                        "model": model,
                        "messages": messages,
                        "stream": True,
                        "options": options
                    }
                ) as response:
                    if response.status_code != 200:
                        yield f"data: {json.dumps({'error': {'message': f'Ollama returned status {response.status_code}', 'type': 'api_error'}})}\n\n"
                        return

                    buffer = ""
                    thinking_done = False

                    async for line in response.aiter_lines():
                        if not line.strip():
                            continue

                        try:
                            data = json.loads(line)
                            content = data.get("message", {}).get("content", "")

                            if content:
                                buffer += content

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

                            if data.get("done", False):
                                yield "data: [DONE]\n\n"
                                break

                        except json.JSONDecodeError:
                            continue

        except httpx.ConnectError:
            yield f"data: {json.dumps({'error': {'message': f'Could not connect to Ollama at {self.url}', 'type': 'connection_error'}})}\n\n"
        except httpx.TimeoutException:
            yield f"data: {json.dumps({'error': {'message': 'Ollama request timed out', 'type': 'timeout_error'}})}\n\n"
        except httpx.RemoteProtocolError:
            # Connection closed during streaming - send DONE to gracefully end
            yield "data: [DONE]\n\n"
        except Exception as e:
            yield f"data: {json.dumps({'error': {'message': f'Ollama error: {str(e)}', 'type': 'api_error'}})}\n\n"

    async def _stream_openai(
        self,
        messages: List[Dict[str, str]],
        model: str,
        completion_id: str,
        created: int,
        **kwargs
    ) -> AsyncGenerator[str, None]:
        """Stream from OpenAI-compatible API (Open-WebUI, Posterchanai, etc.)"""
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        payload = {
            "model": model,
            "messages": messages,
            "stream": True,
            "temperature": kwargs.get("temperature", self.temperature),
            "top_p": kwargs.get("top_p", self.top_p),
        }

        if kwargs.get("max_tokens"):
            payload["max_tokens"] = kwargs["max_tokens"]

        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(self.timeout, connect=10.0)) as client:
                async with client.stream(
                    "POST",
                    f"{self.url}/v1/chat/completions",
                    headers=headers,
                    json=payload
                ) as response:
                    if response.status_code == 401:
                        yield f"data: {json.dumps({'error': {'message': 'Authentication failed. Check your API key.', 'type': 'auth_error'}})}\n\n"
                        return
                    elif response.status_code != 200:
                        yield f"data: {json.dumps({'error': {'message': f'API returned status {response.status_code}', 'type': 'api_error'}})}\n\n"
                        return

                    buffer = ""
                    thinking_done = False

                    async for line in response.aiter_lines():
                        if not line.strip():
                            continue

                        # Handle SSE format
                        if line.startswith("data: "):
                            line = line[6:]

                        if line == "[DONE]":
                            yield "data: [DONE]\n\n"
                            break

                        try:
                            data = json.loads(line)

                            # Check for error in response
                            if "error" in data:
                                yield f"data: {json.dumps(data)}\n\n"
                                break

                            # Extract content from the chunk
                            choices = data.get("choices", [])
                            if choices:
                                delta = choices[0].get("delta", {})
                                content = delta.get("content", "")

                                if content:
                                    buffer += content

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
                                        # Forward the chunk as-is (already in correct format)
                                        yield f"data: {line}\n\n"

                                # Check for finish reason
                                finish_reason = choices[0].get("finish_reason")
                                if finish_reason:
                                    yield "data: [DONE]\n\n"
                                    break

                        except json.JSONDecodeError:
                            continue

        except httpx.ConnectError:
            yield f"data: {json.dumps({'error': {'message': f'Could not connect to API at {self.url}', 'type': 'connection_error'}})}\n\n"
        except httpx.TimeoutException:
            yield f"data: {json.dumps({'error': {'message': 'API request timed out', 'type': 'timeout_error'}})}\n\n"
        except httpx.RemoteProtocolError:
            # Connection closed during streaming - send DONE to gracefully end
            yield "data: [DONE]\n\n"
        except Exception as e:
            yield f"data: {json.dumps({'error': {'message': f'API error: {str(e)}', 'type': 'api_error'}})}\n\n"

    async def chat(
        self,
        messages: List[Dict[str, str]],
        model: Optional[str] = None,
        **kwargs
    ) -> str:
        """
        Non-streaming chat completion.
        Returns the complete response text.
        """
        model = model or self.default_model

        try:
            if self.api_type == "ollama":
                return await self._chat_ollama(messages, model, **kwargs)
            else:
                return await self._chat_openai(messages, model, **kwargs)
        except Exception as e:
            return f"Error connecting to custom AI service: {str(e)}"

    async def _chat_ollama(
        self,
        messages: List[Dict[str, str]],
        model: str,
        **kwargs
    ) -> str:
        """Non-streaming chat via Ollama API"""
        options = self.get_model_options(**kwargs)

        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.post(
                f"{self.url}/api/chat",
                json={
                    "model": model,
                    "messages": messages,
                    "stream": False,
                    "options": options
                }
            )
            response.raise_for_status()
            data = response.json()
            content = data.get("message", {}).get("content", "")
            return self.strip_thinking_tags(content)

    async def _chat_openai(
        self,
        messages: List[Dict[str, str]],
        model: str,
        **kwargs
    ) -> str:
        """Non-streaming chat via OpenAI-compatible API"""
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        payload = {
            "model": model,
            "messages": messages,
            "stream": False,
            "temperature": kwargs.get("temperature", self.temperature),
            "top_p": kwargs.get("top_p", self.top_p),
        }

        if kwargs.get("max_tokens"):
            payload["max_tokens"] = kwargs["max_tokens"]

        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.post(
                f"{self.url}/v1/chat/completions",
                headers=headers,
                json=payload
            )
            response.raise_for_status()
            data = response.json()
            choices = data.get("choices", [])
            if choices:
                content = choices[0].get("message", {}).get("content", "")
                return self.strip_thinking_tags(content)
            return ""
