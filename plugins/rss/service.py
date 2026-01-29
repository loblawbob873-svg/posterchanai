"""
Native RSS Service

Fetches and parses RSS feeds, stores entries in the database.
"""
import logging
import re
import aiohttp
import feedparser
from datetime import datetime
from typing import Optional, List, Dict, Any
from sqlalchemy import case, exists
from sqlalchemy.orm import Session
from time import mktime

from plugins.rss.models import RssFeed, RssEntry
from app.models import User
from app.services.proxy_utils import require_proxy

logger = logging.getLogger(__name__)

# YouTube URL patterns: watch?v=, youtu.be/, embed/, /v/
_YOUTUBE_VIDEO_ID_RE = re.compile(
    r"(?:youtube\.com/(?:watch\?v=|embed/|v/)|youtu\.be/)([a-zA-Z0-9_-]{11})"
)


def youtube_video_id(url: str) -> Optional[str]:
    """Extract YouTube video ID from a URL. Returns None if not a YouTube URL."""
    if not url:
        return None
    m = _YOUTUBE_VIDEO_ID_RE.search(url)
    return m.group(1) if m else None


def youtube_thumbnail_url(url: str) -> Optional[str]:
    """
    Return YouTube thumbnail URL for a video URL, or None if not a YouTube URL.
    Uses hqdefault (480x360) which is available for all videos.
    """
    video_id = youtube_video_id(url)
    if not video_id:
        return None
    return f"https://img.youtube.com/vi/{video_id}/hqdefault.jpg"


def youtube_thumbnail_proxy_path(url: str) -> Optional[str]:
    """
    Return same-origin proxy path for YouTube thumbnail so it loads in RSS/chat.
    Use this when embedding the image in markdown (e.g. ![YouTube](path)).
    """
    video_id = youtube_video_id(url)
    if not video_id:
        return None
    return f"/api/youtube-thumbnail?video_id={video_id}"


def html_to_text(html: str) -> str:
    """Convert HTML content to plain text"""
    if not html:
        return ""
    # Remove script and style elements
    text = re.sub(r'<script[^>]*>.*?</script>', '', html, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r'<style[^>]*>.*?</style>', '', text, flags=re.DOTALL | re.IGNORECASE)
    # Convert common elements
    text = re.sub(r'<br\s*/?>', '\n', text, flags=re.IGNORECASE)
    text = re.sub(r'<p[^>]*>', '\n\n', text, flags=re.IGNORECASE)
    text = re.sub(r'</p>', '', text, flags=re.IGNORECASE)
    # Remove remaining HTML tags
    text = re.sub(r'<[^>]+>', '', text)
    # Clean up whitespace
    text = re.sub(r'\n\s*\n', '\n\n', text)
    text = text.strip()
    return text


class RssService:
    """Service for fetching and managing RSS feeds"""

    def __init__(self, db: Session):
        self.db = db

    async def fetch_feed(self, url: str) -> Dict[str, Any]:
        """
        Fetch and parse an RSS feed.

        Returns dict with:
        - title: Feed title
        - entries: List of entry dicts (title, link, content, published, guid)
        - error: Error message if failed
        """
        try:
            # Proxy is required for RSS feed fetching
            proxy_config = require_proxy("RSS feed fetching")
            
            # Validate proxy config
            if not proxy_config or not isinstance(proxy_config, str):
                logger.error(f"Invalid proxy config for RSS feed: {proxy_config}")
                raise ValueError(f"Invalid proxy configuration: {proxy_config}")
            
            logger.info(f"RSS feed fetching via proxy: {proxy_config} for URL: {url}")
            
            # aiohttp supports proxy parameter directly in session.get()
            # ProxyConnector was removed in newer versions, use proxy parameter instead
            logger.debug(f"Using proxy parameter: {proxy_config}")
            async with aiohttp.ClientSession() as session:
                headers = {"User-Agent": "Mozilla/5.0 (compatible; Posterchanai/1.0)"}
                try:
                    async with session.get(url, headers=headers, proxy=proxy_config, timeout=aiohttp.ClientTimeout(total=30), ssl=False) as resp:
                        if resp.status != 200:
                            try:
                                error_text = await resp.text()
                                logger.warning(f"RSS feed returned status {resp.status} for {url}: {error_text[:200]}")
                            except:
                                pass
                            return {"error": f"HTTP {resp.status}", "entries": []}

                        content = await resp.text()
                except aiohttp.ClientProxyConnectionError as e:
                    logger.error(f"Proxy connection error for {url}: {e}")
                    return {"error": f"Proxy connection error: {str(e)}", "entries": []}
                except aiohttp.ServerTimeoutError as e:
                    logger.error(f"Timeout fetching {url}: {e}")
                    return {"error": f"Timeout: {str(e)}", "entries": []}
                except Exception as e:
                    logger.error(f"aiohttp error fetching {url}: {e}")
                    return {"error": f"Connection error: {str(e)}", "entries": []}

            # Parse with feedparser
            feed = feedparser.parse(content)

            if feed.bozo and not feed.entries:
                return {"error": str(feed.bozo_exception), "entries": []}

            entries = []
            for entry in feed.entries:
                # Get unique ID (prefer id, fall back to link or title)
                guid = entry.get("id") or entry.get("link") or entry.get("title", "")

                # Get content (prefer content, fall back to summary/description)
                content = ""
                if hasattr(entry, "content") and entry.content:
                    content = entry.content[0].get("value", "")
                elif hasattr(entry, "summary"):
                    content = entry.summary
                elif hasattr(entry, "description"):
                    content = entry.description

                # Parse published date
                published = None
                if hasattr(entry, "published_parsed") and entry.published_parsed:
                    try:
                        published = datetime.fromtimestamp(mktime(entry.published_parsed))
                    except (ValueError, OverflowError):
                        pass
                elif hasattr(entry, "updated_parsed") and entry.updated_parsed:
                    try:
                        published = datetime.fromtimestamp(mktime(entry.updated_parsed))
                    except (ValueError, OverflowError):
                        pass

                entries.append({
                    "guid": guid,
                    "title": entry.get("title", "Untitled"),
                    "link": entry.get("link", ""),
                    "content": content,
                    "published": published
                })

            return {
                "title": feed.feed.get("title", ""),
                "entries": entries,
                "error": None
            }

        except Exception as e:
            logger.error(f"Error fetching RSS feed {url}: {e}")
            return {"error": str(e), "entries": []}

    async def add_feed(self, user_id: int, url: str, custom_name: Optional[str] = None) -> RssFeed:
        """
        Add a new RSS feed subscription for a user.
        """
        # Check if already exists
        existing = self.db.query(RssFeed).filter(
            RssFeed.user_id == user_id,
            RssFeed.url == url
        ).first()
        if existing:
            return existing

        # Fetch feed to get title
        result = await self.fetch_feed(url)
        title = result.get("title") or None

        feed = RssFeed(
            user_id=user_id,
            url=url,
            title=title,
            custom_name=custom_name,
            enabled=True
        )
        self.db.add(feed)
        self.db.commit()
        self.db.refresh(feed)

        logger.info(f"Added RSS feed '{feed.display_name}' for user {user_id}")
        return feed

    def remove_feed(self, user_id: int, feed_id: int) -> bool:
        """Remove an RSS feed subscription"""
        feed = self.db.query(RssFeed).filter(
            RssFeed.id == feed_id,
            RssFeed.user_id == user_id
        ).first()
        if not feed:
            return False

        self.db.delete(feed)
        self.db.commit()
        logger.info(f"Removed RSS feed {feed_id} for user {user_id}")
        return True

    def get_user_feeds(self, user_id: int) -> List[RssFeed]:
        """Get all RSS feeds for a user"""
        return self.db.query(RssFeed).filter(
            RssFeed.user_id == user_id
        ).order_by(RssFeed.created_at).all()

    async def sync_feed(self, feed: RssFeed) -> int:
        """
        Sync a feed - fetch new entries and store them.

        Returns number of new entries added.
        """
        result = await self.fetch_feed(feed.url)

        if result.get("error"):
            feed.last_error = result["error"]
            self.db.commit()
            return 0

        # Update feed title if not set
        if not feed.title and result.get("title"):
            feed.title = result["title"]

        feed.last_fetched_at = datetime.utcnow()
        feed.last_error = None

        new_count = 0
        for entry_data in result.get("entries", []):
            # Check if entry already exists
            existing = self.db.query(RssEntry).filter(
                RssEntry.feed_id == feed.id,
                RssEntry.guid == entry_data["guid"]
            ).first()
            if existing:
                continue

            # Add new entry
            entry = RssEntry(
                feed_id=feed.id,
                guid=entry_data["guid"],
                title=entry_data["title"],
                url=entry_data["link"],
                content=entry_data["content"],
                published_at=entry_data["published"],
                is_read=False,
                is_summarized=False
            )
            self.db.add(entry)
            new_count += 1

        self.db.commit()
        if new_count:
            logger.info(f"Added {new_count} new entries from '{feed.display_name}'")
        return new_count

    def get_unread_entries(self, user_id: int, limit: int = 50) -> List[RssEntry]:
        """Get unread entries for a user across all feeds"""
        return self.db.query(RssEntry).join(RssFeed).filter(
            RssFeed.user_id == user_id,
            RssFeed.enabled == True,
            RssEntry.is_read == False
        ).order_by(RssEntry.published_at.desc()).limit(limit).all()

    def get_unsummarized_entries(self, user_id: int) -> List[RssEntry]:
        """Get entries that need AI summarization"""
        return self.db.query(RssEntry).join(RssFeed).filter(
            RssFeed.user_id == user_id,
            RssFeed.enabled == True,
            RssEntry.is_summarized == False
        ).order_by(RssEntry.published_at.desc()).all()

    def mark_entries_read(self, entry_ids: List[int]) -> int:
        """Mark entries as read"""
        count = self.db.query(RssEntry).filter(
            RssEntry.id.in_(entry_ids)
        ).update({"is_read": True}, synchronize_session=False)
        self.db.commit()
        return count

    def get_entry_content_text(self, entry: RssEntry) -> str:
        """Get entry content as plain text"""
        return html_to_text(entry.content or "")

    async def fetch_full_article(self, url: str) -> Optional[str]:
        """
        Fetch full article content from URL.
        Used when RSS content is too short.
        """
        try:
            # Proxy is required for RSS article fetching
            proxy_config = require_proxy("RSS article fetching")
            
            # Validate proxy config
            if not proxy_config or not isinstance(proxy_config, str):
                logger.error(f"Invalid proxy config for RSS article: {proxy_config}")
                raise ValueError(f"Invalid proxy configuration: {proxy_config}")
            
            logger.info(f"RSS article fetching via proxy: {proxy_config} for URL: {url}")
            
            # aiohttp supports proxy parameter directly in session.get()
            # ProxyConnector was removed in newer versions, use proxy parameter instead
            logger.debug(f"Using proxy parameter: {proxy_config}")
            async with aiohttp.ClientSession() as session:
                headers = {"User-Agent": "Mozilla/5.0 (compatible; Posterchanai/1.0)"}
                try:
                    async with session.get(url, headers=headers, proxy=proxy_config, timeout=aiohttp.ClientTimeout(total=30), ssl=False) as resp:
                        if resp.status != 200:
                            logger.warning(f"Article fetch returned status {resp.status} for {url}")
                            return None

                        html = await resp.text()
                        text = html_to_text(html)
                        # Limit length
                        return text[:10000] if len(text) > 10000 else text
                except aiohttp.ClientProxyConnectionError as e:
                    logger.error(f"Proxy connection error fetching article {url}: {e}")
                    return None
                except aiohttp.ServerTimeoutError as e:
                    logger.error(f"Timeout fetching article {url}: {e}")
                    return None
                except Exception as e:
                    logger.error(f"Error fetching article {url}: {e}")
                    return None
        except Exception as e:
            logger.error(f"Error fetching article from {url}: {e}")
            return None

    def search_entries(self, user_id: int, query: str, limit: int = 50) -> List[RssEntry]:
        """
        Search through old RSS entries for a user.
        Searches in title, content, and summary fields.
        
        Args:
            user_id: User ID to search entries for
            query: Search query string
            limit: Maximum number of results to return
            
        Returns:
            List of matching RssEntry objects, ordered by published_at descending
        """
        if not query or not query.strip():
            return []
        
        search_term = f"%{query.strip()}%"
        
        # Search in title, content, and summary fields
        entries = self.db.query(RssEntry).join(RssFeed).filter(
            RssFeed.user_id == user_id,
            RssFeed.enabled == True
        ).filter(
            (RssEntry.title.ilike(search_term)) |
            (RssEntry.content.ilike(search_term)) |
            (RssEntry.summary.ilike(search_term))
        ).order_by(RssEntry.published_at.desc()).limit(limit).all()
        
        return entries

    def cleanup_old_entries(self, user_id: int, retention_limit: int = 1000) -> int:
        """
        Clean up old RSS entries, keeping only the most recent ones.
        
        Uses a subquery with NOT EXISTS to avoid SQLite IN clause limits (999 items).
        This approach is database-agnostic and handles large retention limits safely.
        
        Args:
            user_id: User ID to clean entries for
            retention_limit: Maximum number of entries to keep (default: 1000)
            
        Returns:
            Number of entries deleted
        """
        try:
            # Create a subquery of IDs to keep (most recent entries)
            keep_subquery = self.db.query(RssEntry.id).join(RssFeed).filter(
                RssFeed.user_id == user_id
            ).order_by(
                case(
                    (RssEntry.published_at.isnot(None), RssEntry.published_at),
                    else_=RssEntry.created_at
                ).desc()
            ).limit(retention_limit).subquery()
            
            # Use NOT EXISTS to delete entries not in the keep list
            # This avoids SQLite's IN clause limit and is more efficient
            deleted_count = self.db.query(RssEntry).join(RssFeed).filter(
                RssFeed.user_id == user_id,
                ~exists().where(keep_subquery.c.id == RssEntry.id)
            ).delete(synchronize_session=False)
            
            # Note: We don't commit here - let the caller handle transactions
            # This allows the method to be used within larger transactions
            
            if deleted_count > 0:
                logger.info(f"Cleaned up {deleted_count} old RSS entries for user {user_id} (kept {retention_limit} most recent)")
            
            return deleted_count
        except Exception as e:
            logger.error(f"Error cleaning up old RSS entries for user {user_id}: {e}")
            # Don't raise - allow sync to continue even if cleanup fails
            return 0
