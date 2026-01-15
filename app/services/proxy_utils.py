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
    3. Built-in Tor enabled - use default HTTP proxy port (if Tor is running, HTTP proxy should be too)
    
    Returns:
        Proxy config dict for httpx, or None if not configured
    """
    try:
        from app.database import SessionLocal
        from app.models import Setting
        
        # Get settings from database directly
        db = SessionLocal()
        try:
            # Check bt_proxy_host first (BitTorrent client proxy)
            bt_proxy = db.query(Setting).filter(Setting.key == "bt_proxy_host").first()
            if bt_proxy and bt_proxy.value and bt_proxy.value.strip():
                proxy_port_setting = db.query(Setting).filter(Setting.key == "bt_proxy_port").first()
                proxy_port = proxy_port_setting.value if proxy_port_setting and proxy_port_setting.value else "8118"
                logger.debug(f"Using bt_proxy_host: {bt_proxy.value}:{proxy_port}")
                return {
                    "http://": f"http://{bt_proxy.value}:{proxy_port}",
                    "https://": f"http://{bt_proxy.value}:{proxy_port}",
                }
            
            # Check built-in HTTP proxy
            proxy_enabled = db.query(Setting).filter(Setting.key == "proxy_enabled").first()
            if proxy_enabled and proxy_enabled.value and proxy_enabled.value.lower() == "true":
                proxy_listen_host_setting = db.query(Setting).filter(Setting.key == "proxy_listen_host").first()
                proxy_listen_port_setting = db.query(Setting).filter(Setting.key == "proxy_listen_port").first()
                proxy_listen_host = proxy_listen_host_setting.value if proxy_listen_host_setting and proxy_listen_host_setting.value else "127.0.0.1"
                proxy_listen_port = proxy_listen_port_setting.value if proxy_listen_port_setting and proxy_listen_port_setting.value else "8118"
                logger.debug(f"Using built-in HTTP proxy: {proxy_listen_host}:{proxy_listen_port}")
                return {
                    "http://": f"http://{proxy_listen_host}:{proxy_listen_port}",
                    "https://": f"http://{proxy_listen_host}:{proxy_listen_port}",
                }
            
            # Check if Tor is enabled - if so, check if HTTP proxy is also enabled
            # The HTTP proxy should be enabled and running when Tor is enabled
            tor_enabled = db.query(Setting).filter(Setting.key == "tor_enabled").first()
            if tor_enabled and tor_enabled.value and tor_enabled.value.lower() == "true":
                # Check if HTTP proxy is explicitly enabled
                if not (proxy_enabled and proxy_enabled.value and proxy_enabled.value.lower() == "true"):
                    # Tor is enabled but HTTP proxy is not explicitly enabled
                    # This is a configuration issue - user needs to enable HTTP proxy
                    logger.warning("Tor is enabled but HTTP proxy is not enabled. HTTP proxy is required for 4chan/torrents/nyaa.")
                    return None
                
                # Both Tor and HTTP proxy are enabled - use HTTP proxy
                proxy_listen_host_setting = db.query(Setting).filter(Setting.key == "proxy_listen_host").first()
                proxy_listen_port_setting = db.query(Setting).filter(Setting.key == "proxy_listen_port").first()
                proxy_listen_host = proxy_listen_host_setting.value if proxy_listen_host_setting and proxy_listen_host_setting.value else "127.0.0.1"
                proxy_listen_port = proxy_listen_port_setting.value if proxy_listen_port_setting and proxy_listen_port_setting.value else "8118"
                logger.debug(f"Tor and HTTP proxy enabled, using HTTP proxy at {proxy_listen_host}:{proxy_listen_port}")
                return {
                    "http://": f"http://{proxy_listen_host}:{proxy_listen_port}",
                    "https://": f"http://{proxy_listen_host}:{proxy_listen_port}",
                }
        finally:
            db.close()
    except Exception as e:
        logger.error(f"Error getting proxy config: {e}", exc_info=True)
    
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
        logger.error(f"{error_msg} Configure bt_proxy_host or enable built-in HTTP proxy (proxy_enabled).")
        raise ValueError(error_msg)
    return proxy_config
