"""
Shared proxy configuration utilities for services that require Tor proxy.
"""
import logging
from typing import Optional

logger = logging.getLogger(__name__)


def get_proxy_config() -> Optional[dict]:
    """
    Get HTTP proxy configuration for Tor.
    
    Checks in order:
    1. bt_proxy_host setting (primary)
    2. Built-in HTTP proxy if enabled (fallback)
    
    Returns:
        Proxy config dict for httpx, or None if not configured
    """
    try:
        from app.database import get_setting
        proxy_host = get_setting("bt_proxy_host")
        proxy_port = get_setting("bt_proxy_port", "8118")
        
        # If proxy is configured, use it
        if proxy_host:
            return {
                "http://": f"http://{proxy_host}:{proxy_port}",
                "https://": f"http://{proxy_host}:{proxy_port}",
            }
        
        # Fallback to built-in proxy if enabled
        proxy_enabled = get_setting("proxy_enabled", "false").lower() == "true"
        if proxy_enabled:
            proxy_listen_host = get_setting("proxy_listen_host", "127.0.0.1")
            proxy_listen_port = get_setting("proxy_listen_port", "8118")
            return {
                "http://": f"http://{proxy_listen_host}:{proxy_listen_port}",
                "https://": f"http://{proxy_listen_host}:{proxy_listen_port}",
            }
    except Exception as e:
        logger.debug(f"Could not get proxy config: {e}")
    
    return None


def require_proxy(service_name: str) -> dict:
    """
    Get proxy config and raise ValueError if not configured.
    
    Args:
        service_name: Name of service (for error message)
    
    Returns:
        Proxy config dict for httpx
    
    Raises:
        ValueError: If proxy is not configured
    """
    proxy_config = get_proxy_config()
    if not proxy_config:
        error_msg = f"{service_name} requires HTTP proxy to Tor. Please configure proxy in Admin Settings."
        logger.error(f"{error_msg} Configure bt_proxy_host or enable built-in proxy.")
        raise ValueError(error_msg)
    return proxy_config
