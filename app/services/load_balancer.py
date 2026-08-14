"""
Load Balancer Service - Round-robin load balancing across posterchanai servers.
With health checking to avoid sending requests to unhealthy/slow servers.
"""
import asyncio
from app.utils import lb_auth
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


async def check_server_health(server: str) -> bool:
    """
    Quick health check - verify server responds to /v1/models within timeout.
    Returns True if healthy, False otherwise.
    Server-to-server requests use load-balanced header authentication.
    """
    # Server-to-server requests use load-balanced header
    headers = lb_auth.headers()
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
    Get next server using round-robin with health checking.
    Skips unhealthy servers but auto-recovers them after HEALTH_CHECK_INTERVAL.
    """
    global _server_cycle, _server_list

    if not servers:
        return None

    async with _cycle_lock:
        # Reset cycle only if server list actually changed (compare as sets to ignore order)
        servers_set = set(servers)
        current_set = set(_server_list) if _server_list else set()
        
        # Only reset if cycle is None OR if the server list has actually changed (different servers)
        if _server_cycle is None:
            _server_list = servers.copy()
            _server_cycle = cycle(servers)
            logger.info(f"Load balancer initialized with {len(servers)} server(s): {servers}")
        elif servers_set != current_set:
            # Server list changed (different servers) - reset cycle
            _server_list = servers.copy()
            _server_cycle = cycle(servers)
            logger.info(f"Load balancer reinitialized with {len(servers)} server(s): {servers} (list changed)")

        # Try to find a healthy server (with auto-recovery after HEALTH_CHECK_INTERVAL)
        current_time = time.time()
        attempts = 0
        max_attempts = len(_server_list)
        
        while attempts < max_attempts:
            server = next(_server_cycle)
            attempts += 1
            
            # Check health status
            if server in _server_health:
                is_healthy, last_check = _server_health[server]
                if not is_healthy:
                    # Auto-recover after interval
                    if current_time - last_check > HEALTH_CHECK_INTERVAL:
                        logger.info(f"[LOAD BALANCER] Auto-recovering server {server} after {HEALTH_CHECK_INTERVAL}s")
                        _server_health[server] = (True, current_time)
                    else:
                        # Still unhealthy, skip
                        logger.debug(f"[LOAD BALANCER] Skipping unhealthy server {server}")
                        continue
            
            # Server is healthy (or unknown = assumed healthy)
            return server
        
        # All servers unhealthy - return first one anyway (let it fail and trigger local fallback)
        logger.warning(f"[LOAD BALANCER] All {len(_server_list)} servers unhealthy, returning first one")
        return _server_list[0] if _server_list else None


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


def normalize_node_url(entry: str) -> str:
    """Normalize a node entry to a full URL. Accepts a bare IP/host ("192.168.0.2"), a host:port
    ("192.168.0.2:3051" / "localhost:3052"), or a full http(s):// URL. Bare entries are assumed to be
    posterchanai nodes: prefixed with http:// and given the posterchanai port (POSTERCHANAI_PORT,
    default 3051) when no port is present. Returns "" for blank input."""
    import os
    e = (entry or "").strip().rstrip("/")
    if not e:
        return ""
    if "://" not in e:
        if ":" not in e:
            e = f"{e}:{os.environ.get('POSTERCHANAI_PORT', '3051')}"
        e = "http://" + e
    return e


def parse_server_urls(urls_string: str, exclude_self: bool = False, current_port: int = 3051) -> List[str]:
    """Parse comma-separated server entries into a list of full URLs.

    Entries may be bare IPs/hosts ("192.168.0.2"), host:port, or full URLs — all normalized via
    normalize_node_url. If exclude_self is True, removes URLs pointing to THIS instance (same host
    AND port). URLs on different ports (like localhost:3052) are kept.
    """
    import os
    import socket

    if not urls_string or not urls_string.strip():
        return []

    # Get current port from env or default
    current_port = int(os.environ.get("POSTERCHANAI_PORT", str(current_port)))

    # Get local IPs - include all network interfaces
    local_ips = {'127.0.0.1', 'localhost', '0.0.0.0'}
    if exclude_self:
        try:
            hostname = socket.gethostname()
            local_ips.add(hostname)
            try:
                local_ips.add(socket.gethostbyname(hostname))
            except Exception:
                pass
            # Get all local IPs from all network interfaces
            for info in socket.getaddrinfo(hostname, None):
                ip = info[4][0]
                if ip and not ip.startswith('::'):  # Skip IPv6
                    local_ips.add(ip)
            # Also get all IPs from network interfaces using socket
            try:
                # Connect to a remote address to get local IP
                s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                s.connect(('8.8.8.8', 80))
                local_ip = s.getsockname()[0]
                s.close()
                local_ips.add(local_ip)
            except Exception:
                pass
            # Try to get all interface IPs using netifaces if available, or ip command
            try:
                import subprocess
                # Try 'ip addr' command (works on most Linux systems)
                result = subprocess.run(['ip', 'addr', 'show'], capture_output=True, text=True, timeout=2)
                if result.returncode == 0:
                    import re
                    # Extract IPv4 addresses from ip addr output
                    for match in re.finditer(r'inet (\d+\.\d+\.\d+\.\d+)', result.stdout):
                        local_ips.add(match.group(1))
            except Exception:
                pass
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

        # Accept bare IPs/hosts as well as full URLs — normalize to http://<host>:<port>.
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
                    logger.info(f"[LOAD BALANCER] Skipping self URL: {url} (host={host} in local_ips={local_ips}, port={port} == current_port={current_port})")
                    continue
                elif host in local_ips:
                    logger.info(f"[LOAD BALANCER] Allowing local URL on different port: {url} (host={host} in local_ips, but port {port} != {current_port})")
            except Exception as e:
                logger.debug(f"[LOAD BALANCER] Error checking if URL is self: {url}, error: {e}")

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
            **lb_auth.headers()
        }

    async def chat_stream(
        self,
        messages: List[dict],
        temperature: float = 0.7,
        top_p: float = 0.9,
        max_tokens: int = 2048,
        stop: List[str] = None,
        tools: List[dict] = None,
        tool_choice=None
    ) -> AsyncGenerator[str, None]:
        """
        Stream chat completion from a load-balanced server.
        Simple round-robin: select one server and stream from it.
        Yields SSE-formatted chunks.
        """
        if not self.servers:
            raise ValueError("No servers configured for load balancing")

        # Simple round-robin selection - use all servers including self
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
        # Forward tool definitions so the remote node can do function-calling locally.
        if tools:
            request_body["tools"] = tools
        if tool_choice is not None:
            request_body["tool_choice"] = tool_choice

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
                                        # A tool-call response has no text content - count tool_calls
                                        # too, otherwise a valid function call is misread as "empty"
                                        # and the stream is aborted (opencode just stops).
                                        if content or delta.get("tool_calls"):
                                            content_chunk_count += 1
                                            if content_chunk_count == 1:
                                                logger.info(f"[LOAD BALANCER] First content chunk from {server} "
                                                            f"({len(content or '')} chars{'' if content else ', tool_call'})")
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
        stop: List[str] = None,
        tools: List[dict] = None,
        tool_choice=None
    ) -> dict:
        """
        Non-streaming chat completion from a load-balanced server.
        """
        if not self.servers:
            raise ValueError("No servers configured for load balancing")

        # Always use round-robin selection - use all servers including self
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
        if tools:
            request_json["tools"] = tools
        if tool_choice is not None:
            request_json["tool_choice"] = tool_choice

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
