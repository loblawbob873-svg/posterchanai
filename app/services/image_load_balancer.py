"""
Image Load Balancer Service - Round-robin load balancing for image generation across posterchanai servers.
With health checking to avoid sending requests to failing servers.
"""
import asyncio
from app.utils import lb_auth
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
    Get next image server using simple round-robin (50/50 distribution).
    No health checking - just pure round-robin alternation.
    """
    global _image_server_cycle, _image_server_list

    if not servers:
        return None

    async with _image_cycle_lock:
        # Reset cycle only if server list actually changed (compare as tuples to ensure content comparison, preserve order)
        servers_tuple = tuple(servers)
        current_list_tuple = tuple(_image_server_list) if _image_server_list else ()
        
        # Only reset if cycle is None OR if the server list has actually changed
        if _image_server_cycle is None:
            _image_server_list = servers.copy()
            _image_server_cycle = cycle(servers)
            logger.info(f"[IMAGE LB] INIT: Cycle was None, initialized with {len(servers)} server(s): {servers}")
        elif current_list_tuple != servers_tuple:
            # Server list changed - reset cycle
            logger.warning(f"[IMAGE LB] RESET: Server list changed! Old: {current_list_tuple}, New: {servers_tuple}")
            _image_server_list = servers.copy()
            _image_server_cycle = cycle(servers)
            logger.info(f"[IMAGE LB] REINIT: Reinitialized with {len(servers)} server(s): {servers}")

        # Simple round-robin - get next server
        server = next(_image_server_cycle)
        logger.info(f"[IMAGE LB] Selected image server (round-robin): {server} (from {len(_image_server_list)} servers: {_image_server_list})")
        return server


async def mark_image_server_unhealthy(server: str):
    """Mark an image server as unhealthy (call after a failed request)"""
    global _image_server_health
    async with _image_health_lock:
        _image_server_health[server] = (False, time.time())
    logger.warning(f"Marked image server unhealthy: {server}")


async def should_use_remote_image(num_remote_servers: int) -> bool:
    """
    Always use remote servers when configured - pure load balancing.
    All requests go to the configured nodes (the unified chat_server_urls list).
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


def sanitize_server_url(url: str) -> str:
    """Remove emojis and invalid characters from server URL"""
    if not url:
        return url
    import re
    # Remove emojis and other non-ASCII characters
    # Keep only ASCII printable characters, forward slashes, colons, and URL-encoded sequences (%XX)
    parts = []
    i = 0
    while i < len(url):
        if url[i] == '%' and i + 2 < len(url) and url[i+1:i+3].isalnum():
            # Preserve URL-encoded sequences
            parts.append(url[i:i+3])
            i += 3
        elif ord(url[i]) < 128 and (url[i].isprintable() or url[i] in '/:.-'):
            # Keep ASCII printable characters, forward slashes, colons, dots, and hyphens
            parts.append(url[i])
            i += 1
        else:
            # Skip emojis and other non-ASCII characters
            i += 1
    sanitized = ''.join(parts)
    return sanitized.strip()


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

        # Sanitize URL to remove emojis and invalid characters
        sanitized_url = sanitize_server_url(url)
        if sanitized_url != url:
            logger.warning(f"[IMAGE LOAD BALANCER] Sanitized server URL (removed invalid characters): {url} -> {sanitized_url}")
            url = sanitized_url
        
        if not url:
            logger.warning(f"[IMAGE LOAD BALANCER] Skipping empty URL after sanitization")
            continue

        # Accept bare IPs/hosts as well as full URLs — normalize to http://<host>:<port>.
        from app.services.load_balancer import normalize_node_url
        url = normalize_node_url(url)
        if not url:
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
        Retries with other servers if one fails.
        Server-to-server requests don't require authentication.
        """
        if not self.servers:
            raise ValueError("No servers configured for image load balancing")

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

        # Server-to-server requests - no authentication needed
        headers = lb_auth.headers()

        # Try each server until one succeeds
        tried_servers = set()
        last_error = None

        while len(tried_servers) < len(self.servers):
            # Get a healthy server
            server = await get_healthy_image_server(self.servers)
            if not server or server in tried_servers:
                # No more healthy servers to try
                break

            tried_servers.add(server)
            start_time = time.time()
            logger.info(f"IMAGE REQUEST to {server} | prompt={len(prompt or '')} chars")

            async with httpx.AsyncClient(timeout=self.timeout) as client:
                try:
                    response = await client.post(
                        f"{server}/api/generate-image",
                        json=payload,
                        headers=headers
                    )
                    logger.info(f"IMAGE RESPONSE from {server} | status={response.status_code} | time={time.time()-start_time:.2f}s")
                    response.raise_for_status()

                    result = response.json()

                    if result.get("error"):
                        logger.error(f"IMAGE ERROR from {server} | error={result['error']}")
                        await mark_image_server_unhealthy(server)
                        last_error = result['error']
                        continue  # Try next server

                    image_data = result.get("image")
                    if image_data:
                        logger.info(f"IMAGE COMPLETE from {server} | total_time={time.time()-start_time:.2f}s")
                        return image_data
                    else:
                        logger.error(f"IMAGE ERROR from {server} | no image in response")
                        await mark_image_server_unhealthy(server)
                        last_error = "no image in response"
                        continue  # Try next server

                except httpx.HTTPStatusError as e:
                    error_body = ""
                    try:
                        error_body = e.response.text[:500]
                    except Exception:
                        pass
                    logger.error(f"IMAGE ERROR from {server} | status={e.response.status_code} | body={error_body}")
                    if e.response.status_code == 401:
                        logger.error(f"IMAGE AUTH ERROR | API key sent: {bool(headers.get('X-API-Key'))}, length: {len(headers.get('X-API-Key', ''))}")
                        logger.error(f"IMAGE AUTH ERROR | API key value (first 10 chars): {headers.get('X-API-Key', '')[:10]}...")
                    await mark_image_server_unhealthy(server)
                    last_error = f"HTTP {e.response.status_code}: {error_body[:100]}"
                    continue  # Try next server

                except Exception as e:
                    logger.error(f"IMAGE EXCEPTION | server={server} | error={str(e)}")
                    await mark_image_server_unhealthy(server)
                    last_error = str(e)
                    continue  # Try next server

        # All servers failed
        if tried_servers:
            logger.warning(f"All {len(tried_servers)} image servers failed. Last error: {last_error}")
        else:
            logger.warning("No healthy image servers available")
        raise NoHealthyImageServersError(f"No healthy image servers available (tried {len(tried_servers)}, last error: {last_error})")
