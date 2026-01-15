"""
4chan Plugin

A self-contained 4chan board browser plugin that:
- Fetches board catalog (e.g., /g/catalog)
- Displays images and thread descriptions
- Clickable links to visit threads
- Uses built-in HTTP proxy over Tor for privacy

Usage: 4chan <board> or 4chang <board>
Example: 4chan g
"""

__version__ = "1.0.0"
__plugin_name__ = "chan4"
COMMAND_HELP = "4chan boards: 4chan <board> | 4chang <board>\nExample: 4chan g"
