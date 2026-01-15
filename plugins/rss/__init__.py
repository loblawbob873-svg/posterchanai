"""
Native RSS Plugin

A self-contained RSS feed reader plugin with:
- Feed subscriptions per user
- Automatic fetching every 30 minutes
- AI-powered article summarization
- Summaries stored in "RSS News" conversation

Enable in Admin → Services → Native RSS
"""

__version__ = "1.0.0"
__plugin_name__ = "rss"
COMMAND_HELP = "RSS feeds: rss | rss sync | rss add <url> | rss import (OPML via UI)"
