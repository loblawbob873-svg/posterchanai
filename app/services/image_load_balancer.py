"""
Image Load Balancer Service - Round-robin load balancing for image generation across posterchanai servers.
With health checking to avoid sending requests to failing servers.
"""
import asyncio
import httpx
import logging
import time
from itertools import cycle
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# Global round-robin state for image servers
_image_server_cycle: Optional[cycle] = None
_image_server_list: List[str] = []
_image_cycle_lock = asyncio.Lock()

# Counter for local vs remote decision (for fair distribution including local server)
_image_request_counter: int = 0
_image_counter_lock = asyncio.Lock()

# Server health tracking
_image_server_health: Dict[str, Tuple[bool, float]] = {}  # server -> (is_healthy, last_check_time)
_image_health_lock = asyncio.Lock()
IMAGE_HEALTH_CHECK_INTERVAL = 60  # Re-check unhealthy servers after 60 seconds
IMAGE_HEALTH_CHECK_TIMEOUT = 5.0  # Quick timeout for health checks


async def check_image_server_health(server: str) -> bool:
    """
    Quick health check - verify server responds to /api/generate-image endpoint.
    Returns True if healthy, False otherwise.
    """
    try:
        async with httpx.AsyncClient(timeout=IMAGE_HEALTH_CHECK_TIMEOUT) as client:
            # Just check if the endpoint exists (OPTIONS or quick GET)
            response = await client.get(f"{server}/api/health")
            if response.status_code in (200, 404, 405):  # 404/405 means endpoint exists but wrong method
                return True
            logger.warning(f"Image health check failed for {server}: status {response.status_code}")
            return False
    except Exception as e:
        logger.warning(f"Image health check failed for {server}: {str(e)[:100]}")
        return False


async def get_healthy_image_server(servers: List[str]) -> Optional[str]:
    """
    Get next healthy image server using round-robin.
    Skips unhealthy servers, re-checks them after IMAGE_HEALTH_CHECK_INTERVAL.
    Returns None if no healthy servers available.
    """
    global _image_server_cycle, _image_server_list, _image_server_health

    if not servers:
        return None

    current_time = time.time()

    async with _image_cycle_lock:
        # Reset cycle if server list changed
        if _image_server_cycle is None or _image_server_list != servers:
            _image_server_list = servers.copy()
            _image_server_cycle = cycle(servers)
            logger.info(f"Image load balancer initialized with {len(servers)} server(s): {servers}")

    # Try each server in round-robin order
    tried = set()
    while len(tried) < len(servers):
        async with _image_cycle_lock:
            server = next(_image_server_cycle)

        if server in tried:
            continue
        tried.add(server)

        # Check cached health status
        async with _image_health_lock:
            if server in _image_server_health:
                is_healthy, last_check = _image_server_health[server]

                # If healthy, use it
                if is_healthy:
                    logger.info(f"Selected healthy image server: {server}")
                    return server

                # If unhealthy but check is stale, re-check
                if current_time - last_check < IMAGE_HEALTH_CHECK_INTERVAL:
                    logger.info(f"Skipping unhealthy image server: {server} (checked {current_time - last_check:.0f}s ago)")
                    continue
            else:
                # First time seeing this server, assume healthy
                _image_server_health[server] = (True, current_time)
                logger.info(f"Selected image server (first use): {server}")
                return server

        # Do health check for previously failed server
        logger.info(f"Health checking image server {server}...")
        is_healthy = await check_image_server_health(server)

        async with _image_health_lock:
            _image_server_health[server] = (is_healthy, current_time)

        if is_healthy:
            logger.info(f"Image server {server} is now healthy")
            return server
        else:
            logger.warning(f"Image server {server} still unhealthy")

    logger.warning("No healthy image servers available")
    return None


async def mark_image_server_unhealthy(server: str):
    """Mark an image server as unhealthy (call after a failed request)"""
    global _image_server_health
    async with _image_health_lock:
        _image_server_health[server] = (False, time.time())
    logger.warning(f"Marked image server unhealthy: {server}")


async def should_use_remote_image(num_remote_servers: int) -> bool:
    """
    Always use remote servers when configured - pure load balancing.
    All requests go to the configured image_server_urls.
    """
    if num_remote_servers > 0:
        return True
    return False


async def _get_next_image_server(servers: List[str]) -> str:
    """Get next image server using round-robin (async-safe) - legacy, use get_healthy_image_server instead"""
    global _image_server_cycle, _image_server_list
    async with _image_cycle_lock:
        if _image_server_cycle is None or _image_server_list != servers:
            _image_server_list = servers.copy()
            _image_server_cycle = cycle(servers)
            logger.info(f"Image load balancer initialized with {len(servers)} server(s): {servers}")
        server = next(_image_server_cycle)
        logger.info(f"Selected image server: {server}")
        return server


def parse_image_server_urls(urls_string: str, exclude_self: bool = False, current_port: int = 3051) -> List[str]:
    """Parse comma-separated server URLs into a list.

    If exclude_self is True, removes URLs pointing to THIS instance (same host AND port).
    URLs on different ports (like localhost:3052 for image-only instance) are kept.
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


class NoHealthyImageServersError(Exception):
    """Raised when no healthy image servers are available"""
    pass


class ImageLoadBalancer:
    """Round-robin load balancer for image generation on posterchanai servers with health checking"""

    def __init__(self, servers: List[str], timeout: float = 300.0):
        self.servers = servers
        self.timeout = timeout

    async def generate_image(
        self,
        prompt: str,
        negative_prompt: str = "",
        width: Optional[int] = None,
        height: Optional[int] = None,
        steps: Optional[int] = None,
        cfg: Optional[float] = None,
    ) -> Optional[str]:
        """
        Generate image from a load-balanced server.
        Returns base64 encoded image or None on error.
        """
        if not self.servers:
            raise ValueError("No servers configured for image load balancing")

        # Get a healthy server
        server = await get_healthy_image_server(self.servers)
        if not server:
            logger.warning("No healthy image servers available")
            raise NoHealthyImageServersError("No healthy image servers available")

        start_time = time.time()
        logger.info(f"IMAGE REQUEST to {server} | prompt={prompt[:50]}...")

        # Build request payload
        payload = {
            "prompt": prompt,
            "negative_prompt": negative_prompt,
        }
        if width is not None:
            payload["width"] = width
        if height is not None:
            payload["height"] = height
        if steps is not None:
            payload["steps"] = steps
        if cfg is not None:
            payload["cfg"] = cfg

        async with httpx.AsyncClient(timeout=self.timeout) as client:
            try:
                response = await client.post(
                    f"{server}/api/generate-image",
                    json=payload
                )
                logger.info(f"IMAGE RESPONSE from {server} | status={response.status_code} | time={time.time()-start_time:.2f}s")
                response.raise_for_status()

                result = response.json()

                if result.get("error"):
                    logger.error(f"IMAGE ERROR from {server} | error={result['error']}")
                    await mark_image_server_unhealthy(server)
                    return None

                image_data = result.get("image")
                if image_data:
                    logger.info(f"IMAGE COMPLETE from {server} | total_time={time.time()-start_time:.2f}s")
                    return image_data
                else:
                    logger.error(f"IMAGE ERROR from {server} | no image in response")
                    await mark_image_server_unhealthy(server)
                    return None

            except httpx.HTTPStatusError as e:
                error_body = ""
                try:
                    error_body = e.response.text[:500]
                except Exception:
                    pass
                logger.error(f"IMAGE ERROR from {server} | status={e.response.status_code} | body={error_body}")
                await mark_image_server_unhealthy(server)
                return None
            except Exception as e:
                logger.error(f"IMAGE EXCEPTION | server={server} | error={str(e)}")
                await mark_image_server_unhealthy(server)
                return None
