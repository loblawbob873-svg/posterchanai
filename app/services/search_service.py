import httpx
import logging
import re
from typing import Optional
from sqlalchemy.orm import Session
from bs4 import BeautifulSoup
from app.models import Setting

logger = logging.getLogger(__name__)

# Regex patterns for URL detection
# Match full URLs with protocol
URL_WITH_PROTOCOL = re.compile(
    r'https?://[^\s<>"\')\]},]+[^\s<>"\')\]},.]',
    re.IGNORECASE
)
# Match domain-style URLs without protocol (e.g., example.com/path)
URL_WITHOUT_PROTOCOL = re.compile(
    r'(?<![/@])\b([a-zA-Z0-9][-a-zA-Z0-9]*\.)+(?:com|org|net|edu|gov|io|co|info|biz|me|tv|us|uk|de|fr|jp|cn|ru|br|au|in|nl|se|no|fi|dk|pl|cz|ch|at|be|es|it|pt|ca|mx|ar|nz|za|kr|tw|hk|sg|my|th|vn|id|ph|ae|il|tr)(?:/[^\s<>"\')\]},]*)?',
    re.IGNORECASE
)


class SearchService:
    def __init__(self, db: Session):
        self.db = db
        self._load_settings()

    def _load_settings(self):
        settings = {s.key: s.value for s in self.db.query(Setting).all()}
        self.searxng_url = settings.get("searxng_url", "https://search.poster.place")

    async def web_search(self, query: str, limit: int = 5) -> list[dict]:
        """Search the web using SearXNG"""
        if not self.searxng_url:
            return []

        async with httpx.AsyncClient(timeout=30) as client:
            try:
                response = await client.get(
                    f"{self.searxng_url}/search",
                    params={
                        "q": query,
                        "format": "json",
                        "language": "en"
                    }
                )
                response.raise_for_status()
                data = response.json()
                results = data.get("results", [])[:limit]
                return [
                    {
                        "title": r.get("title", ""),
                        "url": r.get("url", ""),
                        "content": r.get("content", "")[:300] if r.get("content") else ""
                    }
                    for r in results
                ]
            except Exception as e:
                logger.error(f"Search error: {e}")
                return []

    async def image_search(self, query: str, limit: int = 10) -> list[dict]:
        """Search for images using SearXNG"""
        if not self.searxng_url:
            return []

        async with httpx.AsyncClient(timeout=30) as client:
            try:
                response = await client.get(
                    f"{self.searxng_url}/search",
                    params={
                        "q": query,
                        "format": "json",
                        "categories": "images"
                    }
                )
                response.raise_for_status()
                data = response.json()
                results = data.get("results", [])[:limit]
                return [
                    {
                        "title": r.get("title", ""),
                        "url": r.get("url", ""),
                        "img_src": r.get("img_src", r.get("thumbnail_src", r.get("thumbnail", "")))
                    }
                    for r in results
                    if r.get("img_src") or r.get("thumbnail_src") or r.get("thumbnail")
                ]
            except Exception as e:
                logger.error(f"Image search error: {e}")
                return []

    @staticmethod
    def extract_urls(text: str) -> list[str]:
        """Extract URLs from text, including those without http:// prefix"""
        urls = []

        # Find URLs with protocol
        for url in URL_WITH_PROTOCOL.findall(text):
            urls.append(url)

        # Find URLs without protocol and add https://
        for match in URL_WITHOUT_PROTOCOL.finditer(text):
            url = match.group(0)
            full_url = f"https://{url}"
            # Don't add if we already have this URL with protocol
            if full_url not in urls and f"http://{url}" not in urls:
                urls.append(full_url)

        # Deduplicate while preserving order
        seen = set()
        unique_urls = []
        for url in urls:
            normalized = url.lower().rstrip('/')
            if normalized not in seen:
                seen.add(normalized)
                unique_urls.append(url)
        return unique_urls

    async def fetch_url_content(self, url: str, max_length: int = 15000) -> Optional[dict]:
        """Fetch and extract text content from a URL"""
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.5",
        }

        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
            try:
                response = await client.get(url, headers=headers)
                response.raise_for_status()

                content_type = response.headers.get("content-type", "").lower()

                # Only process HTML content
                if "text/html" not in content_type and "application/xhtml" not in content_type:
                    return {
                        "url": url,
                        "title": url,
                        "content": f"[Non-HTML content: {content_type}]",
                        "error": None
                    }

                html = response.text
                soup = BeautifulSoup(html, "lxml")

                # Remove script, style, nav, footer, and other non-content elements
                for element in soup(["script", "style", "nav", "footer", "header",
                                     "aside", "noscript", "iframe", "form", "button"]):
                    element.decompose()

                # Get title
                title = ""
                if soup.title:
                    title = soup.title.get_text(strip=True)

                # Try to find main content area
                main_content = None
                for selector in ["main", "article", "[role='main']", "#content", ".content", "#main", ".main"]:
                    main_content = soup.select_one(selector)
                    if main_content:
                        break

                # Fall back to body if no main content area found
                if not main_content:
                    main_content = soup.body or soup

                # Extract text
                text = main_content.get_text(separator="\n", strip=True)

                # Clean up whitespace
                lines = [line.strip() for line in text.split("\n") if line.strip()]
                text = "\n".join(lines)

                # Truncate if too long
                if len(text) > max_length:
                    text = text[:max_length] + "\n\n[Content truncated...]"

                return {
                    "url": url,
                    "title": title or url,
                    "content": text,
                    "error": None
                }

            except httpx.HTTPStatusError as e:
                logger.warning(f"HTTP error fetching {url}: {e.response.status_code}")
                return {
                    "url": url,
                    "title": url,
                    "content": "",
                    "error": f"HTTP {e.response.status_code}"
                }
            except Exception as e:
                logger.warning(f"Error fetching {url}: {e}")
                return {
                    "url": url,
                    "title": url,
                    "content": "",
                    "error": str(e)
                }

    async def fetch_urls(self, urls: list[str], max_urls: int = 3) -> list[dict]:
        """Fetch content from multiple URLs"""
        results = []
        for url in urls[:max_urls]:
            result = await self.fetch_url_content(url)
            if result:
                results.append(result)
        return results


def get_search_service(db: Session) -> SearchService:
    return SearchService(db)
