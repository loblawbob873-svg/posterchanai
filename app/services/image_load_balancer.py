"""
Image Load Balancer Service - Round-robin load balancing for image generation across posterchanai servers.
"""
import asyncio
import httpx
import logging
import time
from itertools import cycle
from typing import List, Optional

logger = logging.getLogger(__name__)

# Global round-robin state for image servers
_image_server_cycle: Optional[cycle] = None
_image_server_list: List[str] = []
_image_cycle_lock = asyncio.Lock()

# Counter for local vs remote decision (for fair distribution including local server)
_image_request_counter: int = 0
_image_counter_lock = asyncio.Lock()


async def should_use_remote_image(num_remote_servers: int) -> bool:
    """
    Decide if this request should go to a remote server or stay local.
    Distributes requests evenly: with 1 remote server, alternates 50/50.
    With 2 remote servers, goes remote 2/3 of the time, local 1/3.
    """
    global _image_request_counter
    async with _image_counter_lock:
        _image_request_counter += 1
        count = _image_request_counter

    # Total slots = local (1) + remote servers
    total_slots = 1 + num_remote_servers
    use_remote = (count % total_slots) != 0  # Slot 0 = local, others = remote

    logger.info(f"Image Request #{count}: {'REMOTE' if use_remote else 'LOCAL'} (total_slots={total_slots})")
    return use_remote


async def _get_next_image_server(servers: List[str]) -> str:
    """Get next image server using round-robin (async-safe)"""
    global _image_server_cycle, _image_server_list
    async with _image_cycle_lock:
        if _image_server_cycle is None or _image_server_list != servers:
            _image_server_list = servers.copy()
            _image_server_cycle = cycle(servers)
            logger.info(f"Image load balancer initialized with {len(servers)} server(s): {servers}")
        server = next(_image_server_cycle)
        logger.info(f"Selected image server: {server}")
        return server


def parse_image_server_urls(urls_string: str, exclude_self: bool = True, current_port: int = 3051) -> List[str]:
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


class ImageLoadBalancer:
    """Simple round-robin load balancer for image generation on posterchanai servers"""

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

        server = await _get_next_image_server(self.servers)
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
                    return None

                image_data = result.get("image")
                if image_data:
                    logger.info(f"IMAGE COMPLETE from {server} | total_time={time.time()-start_time:.2f}s")
                    return image_data
                else:
                    logger.error(f"IMAGE ERROR from {server} | no image in response")
                    return None

            except httpx.HTTPStatusError as e:
                error_body = ""
                try:
                    error_body = e.response.text[:500]
                except Exception:
                    pass
                logger.error(f"IMAGE ERROR from {server} | status={e.response.status_code} | body={error_body}")
                return None
            except Exception as e:
                logger.error(f"IMAGE EXCEPTION | server={server} | error={str(e)}")
                return None
