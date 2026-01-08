"""
Load Balancer Service - Round-robin load balancing across posterchanai servers.
"""
import asyncio
import httpx
import json
import logging
from itertools import cycle
from typing import AsyncGenerator, List, Optional

logger = logging.getLogger(__name__)

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
            logger.info(f"Load balancer initialized with {len(servers)} server(s)")
        return next(_server_cycle)


def parse_server_urls(urls_string: str) -> List[str]:
    """Parse comma-separated server URLs into a list"""
    if not urls_string or not urls_string.strip():
        return []
    servers = []
    for url in urls_string.split(','):
        url = url.strip().rstrip('/')
        if url:
            servers.append(url)
    return servers


class LoadBalancer:
    """Simple round-robin load balancer for posterchanai servers"""

    def __init__(self, servers: List[str], timeout: float = 120.0):
        self.servers = servers
        self.timeout = timeout

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
        logger.info(f"Load balancing request to: {server}")

        async with httpx.AsyncClient(timeout=self.timeout) as client:
            try:
                async with client.stream(
                    "POST",
                    f"{server}/v1/chat/completions",
                    json={
                        "messages": messages,
                        "stream": True,
                        "temperature": temperature,
                        "top_p": top_p,
                        "max_tokens": max_tokens
                    }
                ) as response:
                    response.raise_for_status()

                    async for line in response.aiter_lines():
                        line = line.strip()
                        if not line:
                            continue
                        # Pass through SSE data
                        if line.startswith("data: "):
                            yield line

            except httpx.HTTPStatusError as e:
                logger.error(f"Load balancer error from {server}: {e}")
                yield f'data: {{"error": {{"message": "Server {server} returned {e.response.status_code}"}}}}'
            except Exception as e:
                logger.error(f"Load balancer error: {e}")
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
        logger.info(f"Load balancing request to: {server}")

        async with httpx.AsyncClient(timeout=self.timeout) as client:
            try:
                response = await client.post(
                    f"{server}/v1/chat/completions",
                    json={
                        "messages": messages,
                        "stream": False,
                        "temperature": temperature,
                        "top_p": top_p,
                        "max_tokens": max_tokens
                    }
                )
                response.raise_for_status()
                return response.json()

            except httpx.HTTPStatusError as e:
                return {"error": {"message": f"Server {server} returned {e.response.status_code}"}}
            except Exception as e:
                return {"error": {"message": str(e)}}
