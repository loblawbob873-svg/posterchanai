"""
Load Balancer Service - Round-robin load balancing across posterchanai servers.
"""
import asyncio
import httpx
import json
import logging
import time
from itertools import cycle
from typing import AsyncGenerator, List, Optional

logger = logging.getLogger(__name__)

# Also log to a dedicated file for easier troubleshooting
def _log_lb(msg: str, level: str = "info"):
    """Log to both standard logger and dedicated load balancer log file"""
    getattr(logger, level)(msg)
    try:
        with open("/tmp/loadbalancer.log", "a") as f:
            f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] [{level.upper()}] {msg}\n")
    except Exception:
        pass

# Global round-robin state
_server_cycle: Optional[cycle] = None
_server_list: List[str] = []
_cycle_lock = asyncio.Lock()


async def _get_next_server(servers: List[str]) -> str:
    """Get next server using round-robin (async-safe)"""
    global _server_cycle, _server_list
    async with _cycle_lock:
        if _server_cycle is None or _server_list != servers:
            _server_list = servers.copy()
            _server_cycle = cycle(servers)
            _log_lb(f"Load balancer initialized with {len(servers)} server(s): {servers}")
        server = next(_server_cycle)
        _log_lb(f"Selected server: {server}")
        return server


def parse_server_urls(urls_string: str) -> List[str]:
    """Parse comma-separated server URLs into a list"""
    if not urls_string or not urls_string.strip():
        return []
    servers = []
    for url in urls_string.split(','):
        url = url.strip().rstrip('/')
        if url and (url.startswith('http://') or url.startswith('https://')):
            servers.append(url)
        elif url:
            logger.warning(f"Skipping invalid URL (missing protocol): {url}")
    return servers


class LoadBalancer:
    """Simple round-robin load balancer for posterchanai servers"""

    def __init__(self, servers: List[str], timeout: float = 120.0, model: str = "default"):
        self.servers = servers
        self.timeout = timeout
        self.model = model

    async def chat_stream(
        self,
        messages: List[dict],
        temperature: float = 0.7,
        top_p: float = 0.9,
        max_tokens: int = 2048
    ) -> AsyncGenerator[str, None]:
        """
        Stream chat completion from a load-balanced server.
        Yields SSE-formatted chunks.
        """
        if not self.servers:
            raise ValueError("No servers configured for load balancing")

        server = await _get_next_server(self.servers)
        start_time = time.time()
        _log_lb(f"STREAM REQUEST to {server} | model={self.model} | messages={len(messages)} | temp={temperature}")

        async with httpx.AsyncClient(timeout=self.timeout) as client:
            try:
                async with client.stream(
                    "POST",
                    f"{server}/v1/chat/completions",
                    json={
                        "model": self.model,
                        "messages": messages,
                        "stream": True,
                        "temperature": temperature,
                        "top_p": top_p,
                        "max_tokens": max_tokens
                    }
                ) as response:
                    _log_lb(f"STREAM RESPONSE from {server} | status={response.status_code} | time={time.time()-start_time:.2f}s")
                    response.raise_for_status()

                    chunk_count = 0
                    async for line in response.aiter_lines():
                        line = line.strip()
                        if not line:
                            continue
                        if line.startswith("data: "):
                            chunk_count += 1
                            yield line

                    _log_lb(f"STREAM COMPLETE from {server} | chunks={chunk_count} | total_time={time.time()-start_time:.2f}s")

            except httpx.HTTPStatusError as e:
                error_body = ""
                try:
                    error_body = e.response.text[:500]
                except Exception:
                    pass
                _log_lb(f"STREAM ERROR from {server} | status={e.response.status_code} | body={error_body}", "error")
                yield f'data: {{"error": {{"message": "Server {server} returned {e.response.status_code}"}}}}'
            except Exception as e:
                _log_lb(f"STREAM EXCEPTION | server={server} | error={str(e)}", "error")
                yield f'data: {{"error": {{"message": "{str(e)}"}}}}'

    async def chat(
        self,
        messages: List[dict],
        temperature: float = 0.7,
        top_p: float = 0.9,
        max_tokens: int = 2048
    ) -> dict:
        """
        Non-streaming chat completion from a load-balanced server.
        """
        if not self.servers:
            raise ValueError("No servers configured for load balancing")

        server = await _get_next_server(self.servers)
        start_time = time.time()
        _log_lb(f"CHAT REQUEST to {server} | model={self.model} | messages={len(messages)} | temp={temperature}")

        async with httpx.AsyncClient(timeout=self.timeout) as client:
            try:
                response = await client.post(
                    f"{server}/v1/chat/completions",
                    json={
                        "model": self.model,
                        "messages": messages,
                        "stream": False,
                        "temperature": temperature,
                        "top_p": top_p,
                        "max_tokens": max_tokens
                    }
                )
                _log_lb(f"CHAT RESPONSE from {server} | status={response.status_code} | time={time.time()-start_time:.2f}s")
                response.raise_for_status()
                result = response.json()
                _log_lb(f"CHAT COMPLETE from {server} | total_time={time.time()-start_time:.2f}s")
                return result

            except httpx.HTTPStatusError as e:
                error_body = ""
                try:
                    error_body = e.response.text[:500]
                except Exception:
                    pass
                _log_lb(f"CHAT ERROR from {server} | status={e.response.status_code} | body={error_body}", "error")
                return {"error": {"message": f"Server {server} returned {e.response.status_code}"}}
            except Exception as e:
                _log_lb(f"CHAT EXCEPTION | server={server} | error={str(e)}", "error")
                return {"error": {"message": str(e)}}
