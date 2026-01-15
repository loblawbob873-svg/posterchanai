"""
Shared proxy configuration utilities for services that require Tor proxy.
"""
import logging
from typing import Optional

logger = logging.getLogger(__name__)


def get_proxy_config() -> Optional[str]:
    """
    Get HTTP proxy configuration for Tor.
    
    Uses the built-in HTTP proxy that torrents use (same proxy for all services).
    
    Checks in order:
    1. Built-in HTTP proxy if enabled (primary - same one torrents use)
    2. bt_proxy_host setting (fallback - if built-in proxy not enabled)
    3. Built-in Tor enabled - use default HTTP proxy port (if Tor is running, HTTP proxy should be too)
    
    Returns:
        Proxy URL string for httpx (e.g., "http://127.0.0.1:8118"), or None if not configured
    """
    try:
        from app.database import SessionLocal
        from app.models import Setting
        
        # Get settings from database directly
        db = SessionLocal()
        try:
            # PRIORITY 1: Check built-in HTTP proxy first (same one torrents use)
            proxy_enabled = db.query(Setting).filter(Setting.key == "proxy_enabled").first()
            if proxy_enabled and proxy_enabled.value and proxy_enabled.value.lower() == "true":
                proxy_listen_host_setting = db.query(Setting).filter(Setting.key == "proxy_listen_host").first()
                proxy_listen_port_setting = db.query(Setting).filter(Setting.key == "proxy_listen_port").first()
                proxy_listen_host = proxy_listen_host_setting.value if proxy_listen_host_setting and proxy_listen_host_setting.value else "127.0.0.1"
                proxy_listen_port = proxy_listen_port_setting.value if proxy_listen_port_setting and proxy_listen_port_setting.value else "8118"
                logger.debug(f"Using built-in HTTP proxy (same as torrents): {proxy_listen_host}:{proxy_listen_port}")
                # httpx 0.28.1 uses 'proxy' (string) not 'proxies' (dict)
                return f"http://{proxy_listen_host}:{proxy_listen_port}"
            
            # PRIORITY 2: Check if Tor is enabled - if so, the HTTP proxy should be running
            tor_enabled = db.query(Setting).filter(Setting.key == "tor_enabled").first()
            if tor_enabled and tor_enabled.value and tor_enabled.value.lower() == "true":
                # If Tor is enabled, the HTTP proxy should be on default port
                # Use the built-in HTTP proxy settings (it should be running)
                proxy_listen_host_setting = db.query(Setting).filter(Setting.key == "proxy_listen_host").first()
                proxy_listen_port_setting = db.query(Setting).filter(Setting.key == "proxy_listen_port").first()
                proxy_listen_host = proxy_listen_host_setting.value if proxy_listen_host_setting and proxy_listen_host_setting.value else "127.0.0.1"
                proxy_listen_port = proxy_listen_port_setting.value if proxy_listen_port_setting and proxy_listen_port_setting.value else "8118"
                logger.info(f"Tor is enabled, using built-in HTTP proxy at {proxy_listen_host}:{proxy_listen_port}")
                # httpx 0.28.1 uses 'proxy' (string) not 'proxies' (dict)
                return f"http://{proxy_listen_host}:{proxy_listen_port}"
            
            # PRIORITY 3: Fallback to bt_proxy_host (if built-in proxy not enabled)
            bt_proxy = db.query(Setting).filter(Setting.key == "bt_proxy_host").first()
            if bt_proxy and bt_proxy.value and bt_proxy.value.strip():
                proxy_port_setting = db.query(Setting).filter(Setting.key == "bt_proxy_port").first()
                proxy_port = proxy_port_setting.value if proxy_port_setting and proxy_port_setting.value else "8118"
                logger.debug(f"Using bt_proxy_host (fallback): {bt_proxy.value}:{proxy_port}")
                # httpx 0.28.1 uses 'proxy' (string) not 'proxies' (dict)
                return f"http://{bt_proxy.value}:{proxy_port}"
        finally:
            db.close()
    except Exception as e:
        logger.error(f"Error getting proxy config: {e}", exc_info=True)
    
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
        logger.error(f"{error_msg} Enable built-in HTTP proxy (proxy_enabled) in Admin → Site Settings → Built-in HTTP Proxy, or configure bt_proxy_host.")
        raise ValueError(error_msg)
    return proxy_config
