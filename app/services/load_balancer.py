"""
Load Balancer Service - Round-robin load balancing across posterchanai servers.
With health checking to avoid sending requests to unhealthy/slow servers.
"""
import asyncio
import httpx
import json
import logging
import time
from itertools import cycle
from typing import AsyncGenerator, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# Global round-robin state
_server_cycle: Optional[cycle] = None
_server_list: List[str] = []
_cycle_lock = asyncio.Lock()

# Counter for local vs remote decision (for fair distribution including local server)
_request_counter: int = 0
_counter_lock = asyncio.Lock()

# Server health tracking
_server_health: Dict[str, Tuple[bool, float]] = {}  # server -> (is_healthy, last_check_time)
_health_lock = asyncio.Lock()
HEALTH_CHECK_INTERVAL = 30  # Re-check unhealthy servers after 30 seconds
HEALTH_CHECK_TIMEOUT = 3.0  # Quick timeout for health checks


async def check_server_health(server: str, api_key: Optional[str] = None) -> bool:
    """
    Quick health check - verify server responds to /v1/models within timeout.
    Returns True if healthy, False otherwise.
    """
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    try:
        async with httpx.AsyncClient(timeout=HEALTH_CHECK_TIMEOUT) as client:
            response = await client.get(f"{server}/v1/models", headers=headers)
            if response.status_code == 200:
                return True
            logger.warning(f"Health check failed for {server}: status {response.status_code}")
            return False
    except Exception as e:
        logger.warning(f"Health check failed for {server}: {str(e)[:100]}")
        return False


async def get_healthy_server(servers: List[str], api_key: Optional[str] = None) -> Optional[str]:
    """
    Get next healthy server using round-robin.
    Skips unhealthy servers, re-checks them after HEALTH_CHECK_INTERVAL.
    Ensures proper round-robin distribution across all configured nodes.
    Returns None if no healthy servers available.
    """
    global _server_cycle, _server_list, _server_health

    if not servers:
        return None

    current_time = time.time()

    async with _cycle_lock:
        # Reset cycle if server list changed
        if _server_cycle is None or _server_list != servers:
            _server_list = servers.copy()
            _server_cycle = cycle(servers)
            logger.info(f"Load balancer initialized with {len(servers)} server(s): {servers}")

    # Prefer self URLs first to avoid unnecessary remote calls when local is available
    # Check self servers first (before round-robin)
    for server in servers:
        if is_self_url(server):
            logger.info(f"Selected self URL (local inference): {server}")
            return server
    
    # Then try remote servers in round-robin order (one full cycle through all servers)
    tried = set()
    start_server = None
    
    while len(tried) < len(servers):
        # Get next server from round-robin cycle
        async with _cycle_lock:
            server = next(_server_cycle)
            # Track starting point to detect full cycle
            if start_server is None:
                start_server = server

        # Skip self URLs (already tried above)
        if is_self_url(server):
            continue

        # If we've tried all servers, break
        if server in tried:
            # We've completed a full cycle without finding a healthy server
            break
        
        tried.add(server)

        # Check cached health status
        async with _health_lock:
            if server in _server_health:
                is_healthy, last_check = _server_health[server]

                # If healthy, use it (round-robin maintained)
                if is_healthy:
                    logger.info(f"Selected healthy server: {server} (round-robin)")
                    return server

                # If unhealthy but check is stale, re-check
                if current_time - last_check < HEALTH_CHECK_INTERVAL:
                    logger.debug(f"Skipping unhealthy server: {server} (checked {current_time - last_check:.0f}s ago)")
                    continue

        # Do health check for this server
        logger.info(f"Health checking {server}...")
        is_healthy = await check_server_health(server, api_key)

        async with _health_lock:
            _server_health[server] = (is_healthy, current_time)

        if is_healthy:
            logger.info(f"Server {server} is healthy (round-robin)")
            return server
        else:
            logger.warning(f"Server {server} marked unhealthy, trying next server")

    logger.warning("No healthy remote servers available after checking all servers")
    return None


def is_self_url(url: str, current_port: int = 3051) -> bool:
    """Check if a URL points to THIS instance (same host AND port)."""
    import os
    import socket
    from urllib.parse import urlparse

    current_port = int(os.environ.get("POSTERCHANAI_PORT", str(current_port)))

    # Get local IPs - include all network interfaces
    local_ips = {'127.0.0.1', 'localhost', '0.0.0.0'}
    try:
        hostname = socket.gethostname()
        local_ips.add(hostname)
        local_ips.add(socket.gethostbyname(hostname))
        # Get all IP addresses for this host (including router IPs, VLAN IPs, etc.)
        for info in socket.getaddrinfo(hostname, None):
            ip = info[4][0]
            if ip and not ip.startswith('::'):  # Skip IPv6 for now
                local_ips.add(ip)
    except Exception:
        pass

    try:
        parsed = urlparse(url)
        host = parsed.hostname
        port = parsed.port or (443 if parsed.scheme == 'https' else 80)
        
        # Check if host matches any local IP
        is_local = host in local_ips
        port_match = port == current_port
        
        if is_local and port_match:
            logger.debug(f"Detected self URL: {url} (host={host}, port={port}, current_port={current_port}, local_ips={local_ips})")
            return True
        
        logger.debug(f"Not self URL: {url} (host={host}, port={port}, current_port={current_port}, is_local={is_local}, port_match={port_match})")
        return False
    except Exception as e:
        logger.warning(f"Error checking if URL is self: {url}, error: {e}")
        return False


async def should_use_remote(num_remote_servers: int) -> bool:
    """
    Always use remote/load-balanced path when servers configured.
    The actual local vs remote decision happens in get_healthy_server.
    """
    return num_remote_servers > 0


async def _get_next_server(servers: List[str]) -> str:
    """Get next server using round-robin (async-safe) - legacy, use get_healthy_server instead"""
    global _server_cycle, _server_list
    async with _cycle_lock:
        if _server_cycle is None or _server_list != servers:
            _server_list = servers.copy()
            _server_cycle = cycle(servers)
            logger.info(f"Load balancer initialized with {len(servers)} server(s): {servers}")
        server = next(_server_cycle)
        logger.info(f"Selected server: {server}")
        return server


def parse_server_urls(urls_string: str, exclude_self: bool = False, current_port: int = 3051) -> List[str]:
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
            logger.warning(f"Skipping invalid URL (missing protocol): {url}")
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
                    logger.debug(f"Skipping self URL: {url} (same host and port)")
                    continue
                elif host in local_ips:
                    logger.info(f"Allowing local URL on different port: {url}")
            except Exception:
                pass

        servers.append(url)

    return servers


class NoHealthyServersError(Exception):
    """Raised when no healthy remote servers are available"""
    pass


def mark_server_unhealthy(server: str):
    """Mark a server as unhealthy (call after a failed request)"""
    global _server_health
    # Use sync version for non-async contexts
    _server_health[server] = (False, time.time())
    logger.warning(f"Marked server unhealthy: {server}")


async def mark_server_unhealthy_async(server: str):
    """Mark a server as unhealthy (async version)"""
    global _server_health
    async with _health_lock:
        _server_health[server] = (False, time.time())
    logger.warning(f"Marked server unhealthy: {server}")


class LoadBalancer:
    """Simple round-robin load balancer for posterchanai servers with health checking"""

    def __init__(self, servers: List[str], timeout: float = 120.0, model: str = "default", api_key: Optional[str] = None):
        self.servers = servers
        self.timeout = timeout
        self.model = model
        self.api_key = api_key
        self.headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        # Add header to indicate this is a load-balanced request (prevents loops)
        self.headers["X-Posterchanai-Load-Balanced"] = "true"

    async def chat_stream(
        self,
        messages: List[dict],
        temperature: float = 0.7,
        top_p: float = 0.9,
        max_tokens: int = 2048,
        stop: List[str] = None
    ) -> AsyncGenerator[str, None]:
        """
        Stream chat completion from a load-balanced server.
        Yields SSE-formatted chunks.
        """
        if not self.servers:
            raise ValueError("No servers configured for load balancing")

        # Get a healthy server
        server = await get_healthy_server(self.servers, self.api_key)
        if not server:
            logger.warning("No healthy remote servers - signaling to use local")
            raise NoHealthyServersError("No healthy remote servers available")

        start_time = time.time()
        logger.info(f"STREAM REQUEST to {server} | model={self.model} | messages={len(messages)} | temp={temperature}")

        request_body = {
            "model": self.model,
            "messages": messages,
            "stream": True,
            "temperature": temperature,
            "top_p": top_p,
            "max_tokens": max_tokens
        }
        if stop:
            request_body["stop"] = stop

        async with httpx.AsyncClient(timeout=self.timeout) as client:
            try:
                async with client.stream(
                    "POST",
                    f"{server}/v1/chat/completions",
                    headers=self.headers,
                    json=request_body
                ) as response:
                    logger.info(f"STREAM RESPONSE from {server} | status={response.status_code} | time={time.time()-start_time:.2f}s")
                    response.raise_for_status()

                    chunk_count = 0
                    first_chunk_time = None
                    empty_stream = True
                    async for line in response.aiter_lines():
                        line = line.strip()
                        if not line:
                            continue
                        if line.startswith("data: "):
                            chunk_count += 1
                            empty_stream = False
                            if first_chunk_time is None:
                                first_chunk_time = time.time()
                                logger.debug(f"STREAM first chunk from {server} after {first_chunk_time - start_time:.2f}s")
                            # Ensure proper SSE format with \n\n
                            yield line + "\n\n" if not line.endswith("\n\n") else line
                        else:
                            # Log non-data lines for debugging
                            logger.debug(f"STREAM non-data line from {server}: {line[:100]}")

                    total_time = time.time() - start_time
                    if chunk_count == 0:
                        logger.warning(f"STREAM COMPLETE from {server} | chunks=0 | total_time={total_time:.2f}s | WARNING: No chunks received! This may indicate a load balancing loop or the remote server doesn't have updated code.")
                        # Mark server as unhealthy and raise exception to trigger fallback to local
                        await mark_server_unhealthy_async(server)
                        raise NoHealthyServersError(f"Remote server {server} returned empty stream (likely load balancing loop or missing code update)")
                    else:
                        logger.info(f"STREAM COMPLETE from {server} | chunks={chunk_count} | total_time={total_time:.2f}s")

            except NoHealthyServersError:
                # Re-raise to trigger fallback to local
                raise
            except httpx.HTTPStatusError as e:
                error_body = ""
                try:
                    error_body = e.response.text[:500]
                except Exception:
                    pass
                logger.error(f"STREAM ERROR from {server} | status={e.response.status_code} | body={error_body}")
                await mark_server_unhealthy_async(server)
                # Re-raise to trigger fallback to local instead of yielding error
                raise NoHealthyServersError(f"Server {server} returned {e.response.status_code}")
            except Exception as e:
                logger.error(f"STREAM EXCEPTION | server={server} | error={str(e)}")
                await mark_server_unhealthy_async(server)
                # Re-raise to trigger fallback to local instead of yielding error
                raise NoHealthyServersError(f"Stream error from {server}: {str(e)}")

    async def chat(
        self,
        messages: List[dict],
        temperature: float = 0.7,
        top_p: float = 0.9,
        max_tokens: int = 2048,
        stop: List[str] = None
    ) -> dict:
        """
        Non-streaming chat completion from a load-balanced server.
        """
        if not self.servers:
            raise ValueError("No servers configured for load balancing")

        # Get a healthy server
        server = await get_healthy_server(self.servers, self.api_key)
        if not server:
            logger.warning("No healthy remote servers - signaling to use local")
            raise NoHealthyServersError("No healthy remote servers available")

        start_time = time.time()
        logger.info(f"CHAT REQUEST to {server} | model={self.model} | messages={len(messages)} | temp={temperature}")

        request_json = {
            "model": self.model,
            "messages": messages,
            "stream": False,
            "temperature": temperature,
            "top_p": top_p,
            "max_tokens": max_tokens
        }
        if stop:
            request_json["stop"] = stop

        async with httpx.AsyncClient(timeout=self.timeout) as client:
            try:
                response = await client.post(
                    f"{server}/v1/chat/completions",
                    headers=self.headers,
                    json=request_json
                )
                logger.info(f"CHAT RESPONSE from {server} | status={response.status_code} | time={time.time()-start_time:.2f}s")
                response.raise_for_status()
                result = response.json()
                logger.info(f"CHAT COMPLETE from {server} | total_time={time.time()-start_time:.2f}s")
                return result

            except httpx.HTTPStatusError as e:
                error_body = ""
                try:
                    error_body = e.response.text[:500]
                except Exception:
                    pass
                logger.error(f"CHAT ERROR from {server} | status={e.response.status_code} | body={error_body}")
                await mark_server_unhealthy_async(server)
                return {"error": {"message": f"Server {server} returned {e.response.status_code}"}}
            except Exception as e:
                logger.error(f"CHAT EXCEPTION | server={server} | error={str(e)}")
                await mark_server_unhealthy_async(server)
                return {"error": {"message": str(e)}}
