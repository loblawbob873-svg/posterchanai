"""
Miniflux API Service

Handles interactions with a Miniflux RSS reader instance.
"""
import logging
import aiohttp
import base64
from typing import Optional, List, Dict, Any
from sqlalchemy.orm import Session
from app.models import Setting, User

logger = logging.getLogger(__name__)


class MinifluxService:
    """Service for interacting with Miniflux API"""

    def __init__(self, url: str, username: str, password: str):
        self.url = url.rstrip("/")
        self.username = username
        self.password = password
        # Create basic auth header
        credentials = f"{username}:{password}"
        b64_credentials = base64.b64encode(credentials.encode()).decode()
        self.auth_header = f"Basic {b64_credentials}"

    @classmethod
    def from_settings(cls, db: Session, user: Optional[User] = None) -> Optional["MinifluxService"]:
        """
        Create a MinifluxService from settings.
        If user has custom settings, use those. Otherwise use global defaults.
        """
        settings = {s.key: s.value for s in db.query(Setting).all()}

        # Check if Miniflux is enabled globally
        if settings.get("miniflux_enabled", "false").lower() != "true":
            return None

        # Get credentials - user overrides take precedence
        if user and user.miniflux_url and user.miniflux_username and user.miniflux_password:
            url = user.miniflux_url
            username = user.miniflux_username
            password = user.miniflux_password
        else:
            url = settings.get("miniflux_url", "")
            username = settings.get("miniflux_username", "")
            password = settings.get("miniflux_password", "")

        if not url or not username or not password:
            logger.warning("Miniflux credentials not configured")
            return None

        return cls(url, username, password)

    async def get_unread_entries(self, limit: int = 100) -> List[Dict[str, Any]]:
        """
        Fetch unread entries from Miniflux.

        Returns a list of entry dictionaries with:
        - id: Entry ID (for marking as read)
        - title: Article title
        - url: Article URL
        - content: Article content (HTML)
        - feed: Feed information
        - published_at: Publication date
        """
        try:
            async with aiohttp.ClientSession() as session:
                headers = {"Authorization": self.auth_header}
                params = {"status": "unread", "limit": limit, "order": "published_at", "direction": "desc"}

                async with session.get(
                    f"{self.url}/v1/entries",
                    headers=headers,
                    params=params,
                    timeout=aiohttp.ClientTimeout(total=30)
                ) as resp:
                    if resp.status == 401:
                        logger.error("Miniflux authentication failed")
                        return []
                    if resp.status != 200:
                        logger.error(f"Miniflux API error: {resp.status}")
                        return []

                    data = await resp.json()
                    entries = data.get("entries", [])
                    logger.info(f"Fetched {len(entries)} unread entries from Miniflux")
                    return entries
        except Exception as e:
            logger.error(f"Error fetching Miniflux entries: {e}")
            return []

    async def mark_entries_as_read(self, entry_ids: List[int]) -> bool:
        """
        Mark entries as read in Miniflux.

        Args:
            entry_ids: List of entry IDs to mark as read

        Returns:
            True if successful, False otherwise
        """
        if not entry_ids:
            return True

        try:
            async with aiohttp.ClientSession() as session:
                headers = {
                    "Authorization": self.auth_header,
                    "Content-Type": "application/json"
                }

                # Miniflux uses PUT /v1/entries to update entry status
                payload = {"entry_ids": entry_ids, "status": "read"}

                async with session.put(
                    f"{self.url}/v1/entries",
                    headers=headers,
                    json=payload,
                    timeout=aiohttp.ClientTimeout(total=30)
                ) as resp:
                    if resp.status == 204 or resp.status == 200:
                        logger.info(f"Marked {len(entry_ids)} entries as read")
                        return True
                    else:
                        logger.error(f"Failed to mark entries as read: {resp.status}")
                        return False
        except Exception as e:
            logger.error(f"Error marking entries as read: {e}")
            return False

    async def get_entry_content(self, entry_id: int) -> Optional[str]:
        """
        Fetch full content for a specific entry.

        Args:
            entry_id: The entry ID

        Returns:
            The entry content as HTML, or None on error
        """
        try:
            async with aiohttp.ClientSession() as session:
                headers = {"Authorization": self.auth_header}

                async with session.get(
                    f"{self.url}/v1/entries/{entry_id}",
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=30)
                ) as resp:
                    if resp.status != 200:
                        logger.error(f"Failed to fetch entry {entry_id}: {resp.status}")
                        return None

                    data = await resp.json()
                    return data.get("content", "")
        except Exception as e:
            logger.error(f"Error fetching entry content: {e}")
            return None

    async def fetch_entry_url_content(self, url: str) -> Optional[str]:
        """
        Fetch the actual article content from the URL.
        This is used to get the full article text for summarization.

        Args:
            url: The article URL

        Returns:
            The article text content, or None on error
        """
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    url,
                    timeout=aiohttp.ClientTimeout(total=30),
                    headers={"User-Agent": "Mozilla/5.0 (compatible; Posterchanai/1.0)"}
                ) as resp:
                    if resp.status != 200:
                        return None

                    html = await resp.text()

                    # Basic HTML to text conversion
                    import re
                    # Remove script and style elements
                    html = re.sub(r'<script[^>]*>.*?</script>', '', html, flags=re.DOTALL | re.IGNORECASE)
                    html = re.sub(r'<style[^>]*>.*?</style>', '', html, flags=re.DOTALL | re.IGNORECASE)
                    # Remove HTML tags
                    text = re.sub(r'<[^>]+>', ' ', html)
                    # Clean up whitespace
                    text = re.sub(r'\s+', ' ', text).strip()
                    # Limit length
                    return text[:10000] if len(text) > 10000 else text
        except Exception as e:
            logger.error(f"Error fetching article content from {url}: {e}")
            return None
