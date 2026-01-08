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

# Counter for local vs remote decision (for fair distribution including local server)
_request_counter: int = 0
_counter_lock = asyncio.Lock()


async def should_use_remote(num_remote_servers: int) -> bool:
    """
    Decide if this request should go to a remote server or stay local.
    Distributes requests evenly: with 1 remote server, alternates 50/50.
    With 2 remote servers, goes remote 2/3 of the time, local 1/3.
    """
    global _request_counter
    async with _counter_lock:
        _request_counter += 1
        count = _request_counter

    # Total slots = local (1) + remote servers
    total_slots = 1 + num_remote_servers
    use_remote = (count % total_slots) != 0  # Slot 0 = local, others = remote

    _log_lb(f"Request #{count}: {'REMOTE' if use_remote else 'LOCAL'} (total_slots={total_slots})")
    return use_remote


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


def parse_server_urls(urls_string: str, exclude_self: bool = True, current_port: int = 3051) -> List[str]:
    """Parse comma-separated server URLs into a list.

    If exclude_self is True, removes URLs pointing to THIS instance (same host AND port).
    URLs on different ports (like localhost:3052) are kept.
    """
    import os
    import socket

    if not urls_string or not urls_string.strip():
        return []

    # Get current port from env or default
    current_port = int(os.environ.get("POSTERCHANAI_PORT", str(current_port)))

    # Get local IPs
    local_ips = {'127.0.0.1', 'localhost', '0.0.0.0'}
    if exclude_self:
        try:
            hostname = socket.gethostname()
            local_ips.add(hostname)
            local_ips.add(socket.gethostbyname(hostname))
            # Also get all local IPs
            for info in socket.getaddrinfo(hostname, None):
                local_ips.add(info[4][0])
        except Exception:
            pass

    servers = []
    for url in urls_string.split(','):
        url = url.strip().rstrip('/')
        if not url:
            continue

        if not (url.startswith('http://') or url.startswith('https://')):
            _log_lb(f"Skipping invalid URL (missing protocol): {url}", "warning")
            continue

        # Check if URL points to THIS instance (same host AND same port)
        if exclude_self:
            try:
                from urllib.parse import urlparse
                parsed = urlparse(url)
                host = parsed.hostname
                port = parsed.port or (443 if parsed.scheme == 'https' else 80)

                # Only skip if SAME host AND SAME port (i.e., pointing to ourselves)
                if host in local_ips and port == current_port:
                    _log_lb(f"Skipping self URL: {url} (same host and port)", "warning")
                    continue
                elif host in local_ips:
                    _log_lb(f"Allowing local URL on different port: {url}")
            except Exception:
                pass

        servers.append(url)

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
