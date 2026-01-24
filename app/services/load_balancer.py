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
    # Use X-API-Key header for server-to-server authentication (matches image API)
    headers = {}
    if api_key:
        headers["X-API-Key"] = api_key
        # Also support Bearer token for compatibility
        headers["Authorization"] = f"Bearer {api_key}"
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


async def get_healthy_server(servers: List[str]) -> Optional[str]:
    """
    Get next server using simple round-robin (50/50 distribution).
    No health checking - just pure round-robin alternation.
    Server-to-server requests don't require authentication.
    """
    global _server_cycle, _server_list

    if not servers:
        return None

    async with _cycle_lock:
        # Reset cycle only if server list actually changed (compare as tuples to ensure content comparison, preserve order)
        servers_tuple = tuple(servers)
        current_list_tuple = tuple(_server_list) if _server_list else ()
        
        # Only reset if cycle is None OR if the server list has actually changed
        if _server_cycle is None:
            _server_list = servers.copy()
            _server_cycle = cycle(servers)
            logger.info(f"Load balancer initialized with {len(servers)} server(s): {servers}")
        elif current_list_tuple != servers_tuple:
            # Server list changed - reset cycle
            _server_list = servers.copy()
            _server_cycle = cycle(servers)
            logger.info(f"Load balancer reinitialized with {len(servers)} server(s): {servers} (list changed)")
        else:
            # Cycle exists and list hasn't changed - just advance it
            logger.debug(f"Cycle exists, list unchanged, advancing round-robin (current list: {_server_list})")

        # Simple round-robin - get next server (this advances the cycle)
        server = next(_server_cycle)
        logger.info(f"[LOAD BALANCER] Selected server (round-robin): {server} (from {len(_server_list)} servers: {_server_list})")
        return server


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

        # Sanitize URL to remove emojis and invalid characters
        sanitized_url = sanitize_server_url(url)
        if sanitized_url != url:
            logger.warning(f"[LOAD BALANCER] Sanitized server URL (removed invalid characters): {url} -> {sanitized_url}")
            url = sanitized_url
        
        if not url:
            logger.warning(f"[LOAD BALANCER] Skipping empty URL after sanitization")
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

    def __init__(self, servers: List[str], timeout: float = 120.0, model: str = "default"):
        self.servers = servers
        self.timeout = timeout
        self.model = model
        # Server-to-server requests don't need authentication - use load-balanced header
        self.headers = {
            "X-Posterchanai-Load-Balanced": "true"
        }

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
        Simple round-robin: select one server and stream from it.
        Yields SSE-formatted chunks.
        """
        if not self.servers:
            raise ValueError("No servers configured for load balancing")

        # Simple round-robin selection
        server = await get_healthy_server(self.servers)
        if not server:
            logger.warning("No healthy remote servers - signaling to use local")
            raise NoHealthyServersError("No healthy remote servers available")

        start_time = time.time()
        logger.info(f"[LOAD BALANCER] STREAM REQUEST to {server} | model={self.model} | messages={len(messages)} | temp={temperature} | all_servers={self.servers}")

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
                    logger.info(f"STREAM RESPONSE from {server} | status={response.status_code} | headers={dict(response.headers)} | time={time.time()-start_time:.2f}s")
                    response.raise_for_status()
                    
                    # Check content-type to verify it's a stream
                    content_type = response.headers.get("content-type", "")
                    if "text/event-stream" not in content_type and "stream" not in content_type.lower():
                        logger.warning(f"STREAM WARNING from {server} | Unexpected content-type: {content_type}")

                    chunk_count = 0
                    content_chunk_count = 0  # Count chunks that actually have content
                    first_chunk_time = None
                    empty_stream = True
                    non_data_lines = []
                    error_detected = False
                    async for line in response.aiter_lines():
                        line = line.strip()
                        if not line:
                            continue
                        if line.startswith("data: "):
                            chunk_count += 1
                            empty_stream = False
                            if first_chunk_time is None:
                                first_chunk_time = time.time()
                                logger.info(f"[LOAD BALANCER] STREAM first chunk from {server} after {first_chunk_time - start_time:.2f}s")
                            
                            # Check if this chunk has actual content (not just [DONE] or empty)
                            data_str = line[6:].strip()
                            if data_str and data_str != "[DONE]":
                                try:
                                    import json
                                    data = json.loads(data_str)
                                    # Check if there's actual content in the chunk
                                    if data.get("choices") and len(data["choices"]) > 0:
                                        delta = data["choices"][0].get("delta", {})
                                        content = delta.get("content", "")
                                        if content:
                                            content_chunk_count += 1
                                            if content_chunk_count == 1:
                                                logger.info(f"[LOAD BALANCER] First content chunk from {server}: {repr(content[:50])}")
                                    # Log errors from remote server and raise exception to trigger fallback
                                    if "error" in data:
                                        error_msg = data.get('error', {})
                                        error_text = error_msg.get('message', str(error_msg)) if isinstance(error_msg, dict) else str(error_msg)
                                        logger.error(f"[LOAD BALANCER] Error in response from {server}: {error_text}")
                                        error_detected = True
                                        # Raise exception to trigger fallback to local
                                        raise NoHealthyServersError(f"Server {server} returned error: {error_text}")
                                except json.JSONDecodeError as e:
                                    logger.warning(f"[LOAD BALANCER] Failed to parse chunk from {server}: {data_str[:100]}")
                                except Exception as e:
                                    logger.warning(f"[LOAD BALANCER] Error processing chunk from {server}: {e}")
                            
                            # Ensure proper SSE format with \n\n
                            yield line + "\n\n" if not line.endswith("\n\n") else line
                        else:
                            # Collect non-data lines for debugging
                            non_data_lines.append(line[:100])
                            logger.debug(f"STREAM non-data line from {server}: {line[:100]}")

                    total_time = time.time() - start_time
                    if chunk_count == 0:
                        # Log details about what we received (or didn't receive) at debug level
                        if non_data_lines:
                            logger.warning(f"STREAM COMPLETE from {server} | chunks=0 | Received {len(non_data_lines)} non-data lines: {non_data_lines[:3]}")
                        else:
                            logger.warning(f"STREAM COMPLETE from {server} | chunks=0 | No data lines received (empty response body), falling back to local")
                        # Don't mark as unhealthy immediately - remote may be processing but stream format issue
                        # Instead, raise exception to trigger fallback to local, but keep server in rotation
                        # Use a simple message - this is expected behavior, fallback is automatic
                        raise NoHealthyServersError("")
                    elif content_chunk_count == 0:
                        # Got chunks but no actual content - server returned empty response
                        logger.warning(f"STREAM COMPLETE from {server} | chunks={chunk_count} but content_chunks=0 | Server returned empty content, falling back to local")
                        raise NoHealthyServersError("Server returned empty content")
                    else:
                        logger.info(f"STREAM COMPLETE from {server} | chunks={chunk_count} | content_chunks={content_chunk_count} | total_time={total_time:.2f}s")
                        # Mark as healthy if we got content chunks
                        async with _health_lock:
                            _server_health[server] = (True, time.time())

            except NoHealthyServersError:
                # Re-raise to trigger fallback to local
                raise
            except httpx.ConnectError as e:
                logger.info(f"STREAM CONNECTION ERROR from {server} | Server unreachable (may be down or network issue), falling back to local")
                await mark_server_unhealthy_async(server)
                raise NoHealthyServersError(f"Server {server} is unreachable (falling back to local)")
            except httpx.TimeoutException as e:
                logger.info(f"STREAM TIMEOUT from {server} | Request timed out, falling back to local")
                await mark_server_unhealthy_async(server)
                raise NoHealthyServersError(f"Server {server} timed out (falling back to local)")
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
                logger.info(f"STREAM EXCEPTION from {server} | {type(e).__name__}: {str(e)[:100]} | falling back to local")
                await mark_server_unhealthy_async(server)
                # Re-raise to trigger fallback to local instead of yielding error
                raise NoHealthyServersError(f"Stream error from {server}: {str(e)[:100]}")

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

        # Always use round-robin selection, even for single server (for consistency)
        server = await get_healthy_server(self.servers)
        if not server:
            logger.warning("No healthy remote servers - signaling to use local")
            raise NoHealthyServersError("No healthy remote servers available")

        start_time = time.time()
        logger.info(f"[LOAD BALANCER] CHAT REQUEST to {server} | model={self.model} | messages={len(messages)} | temp={temperature} | servers={self.servers}")

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
                
                # If remote returns error status, raise NoHealthyServersError to trigger fallback to local
                if response.status_code >= 400:
                    error_body = ""
                    try:
                        error_body = response.text[:500]
                    except Exception:
                        pass
                    logger.warning(f"CHAT ERROR from {server} | status={response.status_code} | body={error_body[:200]}, falling back to local")
                    await mark_server_unhealthy_async(server)
                    raise NoHealthyServersError(f"Server {server} returned {response.status_code}")
                
                response.raise_for_status()
                result = response.json()
                logger.info(f"CHAT COMPLETE from {server} | total_time={time.time()-start_time:.2f}s")
                return result

            except httpx.ConnectError as e:
                logger.info(f"CHAT CONNECTION ERROR from {server} | Server unreachable (may be down or network issue), falling back to local")
                await mark_server_unhealthy_async(server)
                raise NoHealthyServersError(f"Server {server} is unreachable (falling back to local)")
            except httpx.TimeoutException as e:
                logger.info(f"CHAT TIMEOUT from {server} | Request timed out, falling back to local")
                await mark_server_unhealthy_async(server)
                raise NoHealthyServersError(f"Server {server} timed out (falling back to local)")
            except httpx.HTTPStatusError as e:
                error_body = ""
                try:
                    error_body = e.response.text[:500]
                except Exception:
                    pass
                logger.warning(f"CHAT ERROR from {server} | status={e.response.status_code} | body={error_body[:200]}, falling back to local")
                await mark_server_unhealthy_async(server)
                raise NoHealthyServersError(f"Server {server} returned {e.response.status_code}")
            except Exception as e:
                logger.info(f"CHAT EXCEPTION from {server} | {type(e).__name__}: {str(e)[:100]} | falling back to local")
                await mark_server_unhealthy_async(server)
                raise NoHealthyServersError(f"Chat error from {server}: {str(e)[:100]}")
