"""
RSS Plugin Commands

Command handlers for the `rss` command.
"""
import asyncio
import logging
from sqlalchemy.orm import Session

from app.models import User
from plugins.rss.service import RssService, youtube_thumbnail_url
from plugins.rss.scheduler import process_rss_for_user

logger = logging.getLogger(__name__)


async def handle_rss_command(arg: str, user: User, db: Session) -> dict:
    """Handle the `rss` command and subcommands."""
    if not user:
        return {"type": "text", "content": "Please log in to use the rss command."}

    if not getattr(user, 'rss_enabled', False):
        return {"type": "text", "content": "Native RSS is disabled. Enable it in User Settings → News & RSS."}

    rss_service = RssService(db)
    parts = arg.strip().split(maxsplit=1) if arg else []
    subcommand = parts[0].lower() if parts else ""
    subarg = parts[1] if len(parts) > 1 else ""

    try:
        if subcommand == "sync":
            feeds = rss_service.get_user_feeds(user.id)
            if not feeds:
                return {"type": "text", "content": "No RSS feeds configured. Use `rss add <url>` to add a feed."}

            total_new = 0
            for feed in feeds:
                if feed.enabled:
                    new_count = await rss_service.sync_feed(feed)
                    total_new += new_count

            # Clean up old entries (keep only 1000 most recent per user)
            try:
                deleted_count = rss_service.cleanup_old_entries(user.id, retention_limit=1000)
                db.commit()  # Commit cleanup transaction
            except Exception as e:
                logger.error(f"Error during RSS cleanup: {e}")
                deleted_count = 0

            # Run summarization in background (don't block)
            if total_new > 0:
                asyncio.create_task(process_rss_for_user(user.id))
                cleanup_msg = f" Cleaned up {deleted_count} old entries." if deleted_count > 0 else ""
                return {"type": "text", "content": f"RSS sync complete. {total_new} new articles found.{cleanup_msg} Summaries are being generated in the background - check 'RSS News' conversation shortly."}
            else:
                cleanup_msg = f" Cleaned up {deleted_count} old entries." if deleted_count > 0 else ""
                return {"type": "text", "content": f"RSS sync complete. No new articles found.{cleanup_msg}"}

        elif subcommand == "add":
            if not subarg:
                return {"type": "text", "content": "Usage: `rss add <feed_url> [name]`\nExample: `rss add https://news.ycombinator.com/rss Hacker News`"}

            url_parts = subarg.split(maxsplit=1)
            url = url_parts[0]
            name = url_parts[1] if len(url_parts) > 1 else None

            feed = await rss_service.add_feed(user.id, url, name)
            return {"type": "text", "content": f"Added RSS feed: **{feed.display_name}**\n\nUse `rss sync` to fetch articles now, or wait for the 30-minute auto-sync."}

        elif subcommand in ("remove", "rm"):
            if not subarg:
                return {"type": "text", "content": "Usage: `rss remove <feed_id>`\nUse `rss` to see feed IDs."}

            try:
                feed_id = int(subarg)
            except ValueError:
                return {"type": "text", "content": "Invalid feed ID. Use `rss` to see your feeds."}

            if rss_service.remove_feed(user.id, feed_id):
                return {"type": "text", "content": "Feed removed."}
            else:
                return {"type": "text", "content": "Feed not found."}

        elif subcommand == "search":
            if not subarg:
                return {"type": "text", "content": "Usage: `rss search <query>`\nExample: `rss search python`"}

            entries = rss_service.search_entries(user.id, subarg, limit=50)
            if not entries:
                return {"type": "text", "content": f"No articles found matching '{subarg}'."}

            lines = [f"**Found {len(entries)} article(s) matching '{subarg}':**\n"]
            for entry in entries:
                feed_name = entry.feed.display_name
                published = entry.published_at.strftime("%Y-%m-%d") if entry.published_at else "Unknown date"
                url_text = f" [{entry.url}]({entry.url})" if entry.url else ""
                thumb = youtube_thumbnail_url(entry.url) if entry.url else None
                lines.append(f"- **{entry.title}** ({feed_name}, {published}){url_text}")
                if thumb:
                    lines.append(f"  ![YouTube]({thumb})")

            return {"type": "text", "content": "\n".join(lines)}

        elif subcommand == "list" or not subcommand:
            feeds = rss_service.get_user_feeds(user.id)
            if not feeds:
                return {"type": "text", "content": "No RSS feeds configured.\n\n**Add a feed:**\n`rss add <url> [name]`\n\nExample:\n`rss add https://news.ycombinator.com/rss Hacker News`"}

            lines = ["**Your RSS Feeds:**\n"]
            for feed in feeds:
                status = "✓" if feed.enabled else "✗"
                error = f" ⚠️ {feed.last_error}" if feed.last_error else ""
                lines.append(f"- {status} [{feed.id}] **{feed.display_name}**{error}")

            lines.append("\n**Commands:**")
            lines.append("- `rss sync` - Fetch new articles now")
            lines.append("- `rss add <url> [name]` - Add a feed")
            lines.append("- `rss remove <id>` - Remove a feed")
            lines.append("- `rss search <query>` - Search old articles")

            return {"type": "text", "content": "\n".join(lines)}

        else:
            return {"type": "text", "content": "Unknown subcommand. Use `rss`, `rss sync`, `rss add <url>`, `rss remove <id>`, or `rss search <query>`."}

    except Exception as e:
        logger.error(f"RSS command error: {e}")
        return {"type": "text", "content": f"Error: {str(e)}"}
