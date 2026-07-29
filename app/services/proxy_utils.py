"""
Shared proxy configuration utilities for services that require Tor proxy.
"""
import time
import logging
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

# Connection-level failures ONLY — these fire before the request body is delivered through the proxy,
# so retrying the same request DIRECT is safe even for non-idempotent POSTs (nothing was sent). We do
# NOT fall back on ReadTimeout / response-phase errors, which could mean the request already landed
# (e.g. a Pleroma status post) and would double-fire.
_FALLBACK_ERRORS = (httpx.ConnectError, httpx.ConnectTimeout, httpx.ProxyError, httpx.PoolTimeout)


class _AsyncProxyFallback(httpx.AsyncBaseTransport):
    """httpx transport: send through the built-in proxy FIRST, then fall back to a DIRECT connection
    if the proxy can't be reached. The app-wide 'proxy-first, fall back to direct' for external
    outbound HTTP — drop into httpx.AsyncClient(transport=...) in place of proxy=."""
    def __init__(self, proxy_url: str):
        self._proxy = httpx.AsyncHTTPTransport(proxy=proxy_url, retries=0)
        self._direct = httpx.AsyncHTTPTransport(retries=0)

    async def handle_async_request(self, request):
        try:
            return await self._proxy.handle_async_request(request)
        except _FALLBACK_ERRORS as e:
            logger.debug("[proxy] %s %s via proxy failed (%s) — retrying direct", request.method, request.url, e)
            return await self._direct.handle_async_request(request)

    async def aclose(self):
        try:
            await self._proxy.aclose()
        finally:
            await self._direct.aclose()


def afallback_transport() -> httpx.AsyncBaseTransport:
    """Async httpx transport doing proxy-first-then-direct, or a plain direct transport when no proxy
    is configured. Use: `httpx.AsyncClient(transport=afallback_transport(), timeout=..., ...)` instead
    of `proxy=get_outbound_proxy()`, and outbound traffic prefers the proxy but survives it being down."""
    px = get_outbound_proxy()
    return _AsyncProxyFallback(px) if px else httpx.AsyncHTTPTransport(retries=0)

# Short-TTL cache: get_proxy_config is called on every social/bot HTTP client and relay
# connect (~8 per Nostr op, ~14 per Misskey op). The proxy setting changes very rarely, so
# caching the resolved value for a few seconds avoids opening a DB session on every call.
_CACHE_TTL = 30.0
_cache: dict = {"value": None, "ts": 0.0}


def get_proxy_config() -> Optional[str]:
    """Cached wrapper around _resolve_proxy_config (see TTL note above)."""
    now = time.monotonic()
    if now - _cache["ts"] < _CACHE_TTL:
        return _cache["value"]
    val = _resolve_proxy_config()
    _cache["value"], _cache["ts"] = val, now
    return val


def _resolve_proxy_config() -> Optional[str]:
    """
    Get HTTP proxy configuration for Tor.
    
    For load-balanced/distributed setups, prioritizes bt_proxy_host (HTTP Proxy Host in admin UI)
    which is shared across nodes. For single-node setups, can use built-in HTTP proxy.
    
    Checks in order:
    1. bt_proxy_host setting (PRIMARY - required for load-balanced setups, shared across nodes)
    2. Built-in HTTP proxy if enabled (fallback - for single-node setups)
    3. Built-in Tor enabled - use default HTTP proxy port (if Tor is running, HTTP proxy should be too)
    
    Returns:
        Proxy URL string for httpx (e.g., "http://127.0.0.1:8118"), or None if not configured
    """
    try:
        from app.services import settings_store

        # PRIORITY 1: Check bt_proxy_host first (HTTP Proxy Host in admin UI - required for load-balanced setups)
        # This is the shared proxy that all nodes should use.
        bt_proxy = (settings_store.get("bt_proxy_host", "") or "").strip()
        if bt_proxy:
            # A bt_proxy_host pointing at THIS node's own built-in proxy (localhost) is only real when
            # that proxy is actually enabled — otherwise (proxy + Tor both off) it's a dead :8118 and
            # every outbound connect refuses (ECONNREFUSED). A REMOTE bt_proxy_host (a shared LB proxy)
            # is always honoured, since that's its whole purpose.
            _local = bt_proxy in ("127.0.0.1", "localhost", "::1", "[::1]")
            if (not _local) or settings_store.get_bool("proxy_enabled") or settings_store.get_bool("tor_enabled"):
                proxy_port = settings_store.get("bt_proxy_port", "") or "8118"
                logger.debug(f"Using bt_proxy_host (HTTP Proxy Host from admin UI): {bt_proxy}:{proxy_port}")
                # httpx 0.28.1 uses 'proxy' (string) not 'proxies' (dict)
                return f"http://{bt_proxy}:{proxy_port}"

        # PRIORITY 2: Check built-in HTTP proxy (for single-node setups)
        if settings_store.get_bool("proxy_enabled"):
            proxy_listen_host = settings_store.get("proxy_listen_host", "") or "127.0.0.1"
            proxy_listen_port = settings_store.get("proxy_listen_port", "") or "8118"
            logger.debug(f"Using built-in HTTP proxy (fallback): {proxy_listen_host}:{proxy_listen_port}")
            # httpx 0.28.1 uses 'proxy' (string) not 'proxies' (dict)
            return f"http://{proxy_listen_host}:{proxy_listen_port}"

        # PRIORITY 3: Check if Tor is enabled - if so, the HTTP proxy should be running
        if settings_store.get_bool("tor_enabled"):
            # If Tor is enabled, the HTTP proxy should be on default port
            # Use the built-in HTTP proxy settings (it should be running)
            proxy_listen_host = settings_store.get("proxy_listen_host", "") or "127.0.0.1"
            proxy_listen_port = settings_store.get("proxy_listen_port", "") or "8118"
            logger.info(f"Tor is enabled, using built-in HTTP proxy at {proxy_listen_host}:{proxy_listen_port}")
            # httpx 0.28.1 uses 'proxy' (string) not 'proxies' (dict)
            return f"http://{proxy_listen_host}:{proxy_listen_port}"
    except Exception as e:
        logger.error(f"Error getting proxy config: {e}", exc_info=True)

    return None


def get_outbound_proxy() -> Optional[str]:
    """Proxy URL for outbound social/bot traffic, resolved for BOTH process types:

    - **Bot subprocesses** get the proxy via injected env (HTTPS_PROXY/ALL_PROXY/…),
      which requests/httpx/websockets already honour — checked first so we don't open a
      DB the bot doesn't have.
    - **The app process** has no global proxy env (that would wrongly route the LB's LAN
      calls through Tor), so it falls back to the configured proxy from settings.

    Returns the URL (e.g. "http://192.168.0.2:8118") or None when no proxy is set.
    """
    import os
    for key in ("OUTBOUND_HTTP_PROXY", "HTTPS_PROXY", "https_proxy",
                "ALL_PROXY", "all_proxy", "HTTP_PROXY", "http_proxy"):
        val = os.environ.get(key)
        if val:
            return val
    try:
        return get_proxy_config()
    except Exception:
        return None


def require_proxy(service_name: str) -> str:
    """
    Get proxy config and raise ValueError if not configured.
    
    Args:
        service_name: Name of service (for error message)
    
    Returns:
        Proxy URL string for httpx (e.g., "http://127.0.0.1:8118")
    
    Raises:
        ValueError: If proxy is not configured
    """
    proxy_config = get_proxy_config()
    if not proxy_config:
        error_msg = f"{service_name} requires HTTP proxy to Tor. Please configure proxy in Admin Settings."
        logger.error(f"{error_msg} Configure HTTP Proxy Host in Admin → Network → HTTP Proxy (outbound) (required for load-balanced setups), or enable built-in HTTP proxy.")
        raise ValueError(error_msg)
    return proxy_config
