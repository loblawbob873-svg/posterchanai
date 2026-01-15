"""
Plugin System

Plugins are self-contained modules in the plugins/ directory.
Each plugin can provide:
- Database models (models.py)
- API routes (router.py)  
- Background schedulers (scheduler.py)
- Chat commands (commands.py)

Plugins are loaded dynamically based on settings.
"""
import logging
import importlib
from typing import Optional, Dict, Any, Callable
from fastapi import FastAPI
from sqlalchemy.orm import Session

from app.database import SessionLocal
from app.models import Setting

logger = logging.getLogger(__name__)

# Registry of loaded plugins
_loaded_plugins: Dict[str, Any] = {}
_command_handlers: Dict[str, Callable] = {}


def is_plugin_enabled(plugin_name: str) -> bool:
    """Check if a plugin is enabled in settings."""
    db = SessionLocal()
    try:
        setting = db.query(Setting).filter(Setting.key == f"{plugin_name}_enabled").first()
        return setting and setting.value.lower() == "true"
    finally:
        db.close()


def load_plugin(plugin_name: str, app: Optional[FastAPI] = None) -> bool:
    """
    Load a plugin by name.
    
    Args:
        plugin_name: Name of the plugin (directory name under plugins/)
        app: FastAPI app instance to register routes
        
    Returns:
        True if loaded successfully
    """
    if plugin_name in _loaded_plugins:
        return True
        
    try:
        # Import the plugin module
        plugin_module = importlib.import_module(f"plugins.{plugin_name}")
        
        # Import and register models (creates tables)
        try:
            models = importlib.import_module(f"plugins.{plugin_name}.models")
            logger.info(f"Plugin '{plugin_name}' models loaded")
        except ImportError:
            pass
            
        # Import and register router
        if app:
            try:
                router_module = importlib.import_module(f"plugins.{plugin_name}.router")
                if hasattr(router_module, 'router'):
                    app.include_router(router_module.router)
                    logger.info(f"Plugin '{plugin_name}' router registered")
            except ImportError:
                pass
        
        # Import command handlers
        try:
            commands = importlib.import_module(f"plugins.{plugin_name}.commands")
            # Look for handle_X_command function
            handler_name = f"handle_{plugin_name}_command"
            if hasattr(commands, handler_name):
                _command_handlers[plugin_name] = getattr(commands, handler_name)
                logger.info(f"Plugin '{plugin_name}' command handler registered")
        except ImportError:
            pass
            
        _loaded_plugins[plugin_name] = plugin_module
        logger.info(f"Plugin '{plugin_name}' loaded successfully")
        return True
        
    except Exception as e:
        logger.error(f"Failed to load plugin '{plugin_name}': {e}")
        return False


def start_plugin_schedulers():
    """Start schedulers for enabled plugins only."""
    for plugin_name in _loaded_plugins:
        # Only start scheduler if the plugin is enabled in settings
        if not is_plugin_enabled(plugin_name):
            continue
        try:
            scheduler = importlib.import_module(f"plugins.{plugin_name}.scheduler")
            if hasattr(scheduler, f'start_{plugin_name}_scheduler'):
                getattr(scheduler, f'start_{plugin_name}_scheduler')()
                logger.info(f"Started scheduler for plugin '{plugin_name}'")
        except ImportError:
            pass
        except Exception as e:
            logger.error(f"Failed to start scheduler for plugin '{plugin_name}': {e}")


def stop_plugin_schedulers():
    """Stop schedulers for all loaded plugins."""
    for plugin_name in _loaded_plugins:
        try:
            scheduler = importlib.import_module(f"plugins.{plugin_name}.scheduler")
            if hasattr(scheduler, f'stop_{plugin_name}_scheduler'):
                getattr(scheduler, f'stop_{plugin_name}_scheduler')()
        except ImportError:
            pass
        except Exception as e:
            logger.error(f"Failed to stop scheduler for plugin '{plugin_name}': {e}")


def get_command_handler(command: str) -> Optional[Callable]:
    """Get command handler for a plugin command."""
    return _command_handlers.get(command)


def get_plugin_commands() -> Dict[str, str]:
    """Get help text for all plugin commands."""
    commands = {}
    for plugin_name in _loaded_plugins:
        # Default help text
        commands[plugin_name] = f"{plugin_name.upper()} plugin command"
        
        # Try to get custom help from plugin
        try:
            plugin = _loaded_plugins[plugin_name]
            if hasattr(plugin, 'COMMAND_HELP'):
                commands[plugin_name] = plugin.COMMAND_HELP
        except Exception:
            pass
            
    return commands


def load_enabled_plugins(app: Optional[FastAPI] = None):
    """Load all plugins (routers always loaded, schedulers only if enabled)."""
    # List of known plugins - routers are always loaded for API access
    known_plugins = ['rss']
    
    for plugin_name in known_plugins:
        # Always load the plugin (router, models, commands)
        load_plugin(plugin_name, app)
