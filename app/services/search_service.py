import httpx
import ipaddress
import logging
import re
import socket
from typing import Optional
from urllib.parse import urlparse
from sqlalchemy.orm import Session
from bs4 import BeautifulSoup
from app.services import settings_store

logger = logging.getLogger(__name__)


def is_safe_url(url: str) -> tuple[bool, str]:
    """
    SSRF protection: Validate that URL doesn't point to internal networks.
    Returns (is_safe, error_message).
    """
    try:
        parsed = urlparse(url)

        # Only allow http/https
        if parsed.scheme not in ('http', 'https'):
            return False, f"Invalid scheme: {parsed.scheme}"

        hostname = parsed.hostname
        if not hostname:
            return False, "No hostname in URL"

        # Trusted domains that are allowed even if they resolve to private IPs
        # (e.g., internal git servers, local services with proper DNS entries)
        trusted_domains = {'git.poster.place', 'poster.place'}
        if hostname.lower() in trusted_domains:
            return True, ""

        # Block localhost variations
        localhost_names = {'localhost', 'localhost.localdomain', '127.0.0.1', '::1'}
        if hostname.lower() in localhost_names:
            return False, "Localhost URLs not allowed"

        # Resolve hostname to IP
        try:
            ip = socket.gethostbyname(hostname)
            ip_obj = ipaddress.ip_address(ip)
        except (socket.gaierror, ValueError) as e:
            return False, f"Cannot resolve hostname: {e}"

        # Block private/internal IP ranges
        if ip_obj.is_private:
            return False, f"Private IP not allowed: {ip}"
        if ip_obj.is_loopback:
            return False, f"Loopback IP not allowed: {ip}"
        if ip_obj.is_link_local:
            return False, f"Link-local IP not allowed: {ip}"
        if ip_obj.is_reserved:
            return False, f"Reserved IP not allowed: {ip}"
        if ip_obj.is_multicast:
            return False, f"Multicast IP not allowed: {ip}"

        # Block common internal hostnames
        internal_patterns = ['internal', 'intranet', 'corp', 'local', 'private']
        for pattern in internal_patterns:
            if pattern in hostname.lower():
                return False, f"Internal hostname pattern detected: {hostname}"

        # Block AWS/cloud metadata endpoints
        metadata_ips = ['169.254.169.254', '100.100.100.200', 'fd00:ec2::254']
        if ip in metadata_ips:
            return False, "Cloud metadata endpoint not allowed"

        return True, ""
    except Exception as e:
        return False, f"URL validation error: {e}"

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
        settings = settings_store.all_settings()
        self.searxng_url = settings.get("searxng_url", "https://search.poster.place")

    async def web_search(
        self,
        query: str,
        limit: int = 5,
        categories: Optional[str] = None,
        time_range: Optional[str] = None,
        sort_recent: bool = False,
    ) -> list[dict]:
        """Search the web using SearXNG.

        categories: optional SearXNG category (e.g. "news", "videos", "science").
        time_range: optional SearXNG time filter ("day", "week", "month", "year").
        """
        if not self.searxng_url:
            return []

        params = {
            "q": query,
            "format": "json",
            "language": "en",
        }
        if categories:
            params["categories"] = categories
        if time_range:
            params["time_range"] = time_range

        # Caller decides recency sort (e.g. news). When on, sort the most-recent first (SearXNG
        # returns `publishedDate` for some engines but doesn't globally sort by it).

        async with httpx.AsyncClient(timeout=30) as client:
            try:
                response = await client.get(
                    f"{self.searxng_url}/search",
                    params=params,
                )
                response.raise_for_status()
                data = response.json()
                results = data.get("results", [])

                if sort_recent:
                    from datetime import datetime, timezone as _tz

                    def _pub(r):
                        d = r.get("publishedDate")
                        if not d:
                            return None
                        try:
                            dt = datetime.fromisoformat(str(d).replace("Z", "+00:00"))
                            return dt if dt.tzinfo else dt.replace(tzinfo=_tz.utc)
                        except Exception:
                            return None
                    # Sort the FULL list (before trimming) so the newest survive: dated newest-first,
                    # then undated results in their original (relevance) order.
                    dated = [r for r in results if _pub(r) is not None]
                    undated = [r for r in results if _pub(r) is None]
                    dated.sort(key=_pub, reverse=True)
                    results = dated + undated

                results = results[:limit]
                return [
                    {
                        "title": r.get("title", ""),
                        "url": r.get("url", ""),
                        "content": r.get("content", "")[:300] if r.get("content") else "",
                        "published": r.get("publishedDate", "") or "",
                    }
                    for r in results
                ]
            except Exception as e:
                logger.error(f"Search error: {e}")
                return []

    async def image_search(self, query: str, limit: int = 10) -> list[dict]:
        """Search for images using SearXNG. Only return results with a non-empty thumbnail URL."""
        if not self.searxng_url:
            return []

        async with httpx.AsyncClient(timeout=30) as client:
            try:
                response = await client.get(
                    f"{self.searxng_url}/search",
                    params={
                        "q": query,
                        "format": "json",
                        "categories": "images",
                        "image_proxy": "1",
                    }
                )
                response.raise_for_status()
                data = response.json()
                results = data.get("results", [])[:limit * 2]
                out = []
                for r in results:
                    thumb = (
                        (r.get("thumbnail_src") or "").strip()
                        or (r.get("img_src") or "").strip()
                        or (r.get("thumbnail") or "").strip()
                    )
                    if not thumb or (not thumb.startswith("http") and not thumb.startswith("data:")):
                        continue
                    url = (r.get("url") or "").strip() or thumb
                    out.append({
                        "title": (r.get("title") or "Image")[:200],
                        "url": url,
                        "img_src": thumb,
                    })
                    if len(out) >= limit:
                        break
                return out[:limit]
            except Exception as e:
                logger.error(f"Image search error: {e}")
                return []

    # Maps a SearXNG category to the trigger words that imply it.
    # Order matters: earlier categories win when multiple match.
    _CATEGORY_KEYWORDS = {
        "news": ("news", "headline", "headlines", "breaking", "latest on",
                 "press release", "current events"),
        "files": ("torrent", "torrents", "magnet", "iso", "download",
                  "downloads", "files"),
        "videos": ("video", "videos", "clip", "clips", "footage", "trailer"),
        "music": ("song", "songs", "lyrics", "album", "music"),
        "science": ("paper", "papers", "study", "studies", "research",
                    "journal", "publication", "preprint"),
        "it": ("github", "stackoverflow", "stack overflow", "source code",
               "repository", "pip package", "npm package", "man page"),
        "map": ("map", "directions", "near me", "nearby", "route to"),
        "social media": ("reddit", "subreddit", "tweet", "tweets", "mastodon",
                         "lemmy", "hacker news"),
    }

    # Time-range trigger words -> SearXNG time_range value.
    _TIME_KEYWORDS = {
        "day": ("today", "past 24 hours", "last 24 hours", "past day"),
        "week": ("this week", "past week", "last week"),
        "month": ("this month", "past month", "last month"),
        "year": ("this year", "past year", "last year"),
    }

    @classmethod
    def detect_search_intent(cls, query: str) -> tuple[str, Optional[str], Optional[str]]:
        """Heuristically infer the SearXNG category and time range from a natural
        query. Returns (clean_query, categories, time_range).

        The category trigger word is kept in the query (SearXNG ranks fine with
        it, and stripping risks dropping meaningful terms like "music" in a band
        name) — only the category/time filters are derived from it.
        """
        lowered = f" {query.lower()} "

        categories = None
        for category, keywords in cls._CATEGORY_KEYWORDS.items():
            if any(f" {kw} " in lowered for kw in keywords):
                categories = category
                break

        time_range = None
        for tr, keywords in cls._TIME_KEYWORDS.items():
            if any(kw in lowered for kw in keywords):
                time_range = tr
                break

        # "latest"/"recent"/"now" imply recency without a specific window → bias to the past week.
        if time_range is None and any(kw in lowered for kw in (" latest ", " recent ", " right now ", " just now ")):
            time_range = "week"

        # News with no explicit window: default to the past month so stale articles drop off while
        # the newest-first sort (in web_search) surfaces the freshest. Keeps news "by latest".
        if categories == "news" and time_range is None:
            time_range = "month"

        return query.strip(), categories, time_range

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

    async def _fetch_youtube_content(self, url: str, max_length: int = 15000) -> dict:
        """Return a fetch_url_content-shaped dict for a YouTube link using its TRANSCRIPT.

        The watch-page HTML has no spoken content, so summarizing/posting from it makes the LLM
        hallucinate. When there is no transcript we return empty content + an error so callers
        don't invent one. Title is best-effort from the page <title>."""
        import asyncio as _asyncio
        from app.services import youtube_service as _yt

        vid = _yt.extract_video_id(url)
        transcript = await _asyncio.to_thread(_yt.get_transcript, vid) if vid else None

        title = "YouTube video"
        try:  # best-effort page title (the watch-page <title> is the video title)
            async with httpx.AsyncClient(timeout=10, follow_redirects=True) as client:
                r = await client.get(url, headers={"User-Agent": "Mozilla/5.0"})
                if r.status_code == 200 and r.text:
                    t = BeautifulSoup(r.text, "lxml").title
                    if t:
                        cand = t.get_text(strip=True)
                        if cand.endswith(" - YouTube"):
                            cand = cand[: -len(" - YouTube")].strip()
                        title = cand or title
        except Exception:
            pass

        if transcript:
            return {"url": url, "title": title, "content": transcript[:max_length], "error": None}
        return {"url": url, "title": title, "content": "",
                "error": "no transcript/captions available for this video"}

    async def fetch_url_content(self, url: str, max_length: int = 15000) -> Optional[dict]:
        """Fetch and extract text content from a URL"""
        # YouTube *videos* need the transcript, not the watch-page HTML (which is contentless and
        # makes the LLM hallucinate). Centralised here so EVERY caller - telegram/web/
        # pleroma and the summarize & post commands - gets it via fetch_urls()
        # automatically. Only intercept actual video URLs (watch/shorts/embed/youtu.be); channel,
        # search and playlist pages have no video id and fall through to normal HTML fetch.
        try:
            from app.services import youtube_service as _yt
            if _yt.extract_video_id(url):
                return await self._fetch_youtube_content(url, max_length)
        except Exception as _yt_err:
            logger.warning(f"YouTube transcript path failed for {url}: {_yt_err}")

        # SSRF protection: validate URL before fetching
        is_safe, error_msg = is_safe_url(url)
        if not is_safe:
            logger.warning(f"SSRF blocked: {url} - {error_msg}")
            return {
                "url": url,
                "title": url,
                "content": "",
                "error": f"URL blocked: {error_msg}"
            }

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

                # Extract links with their text (for news sites)
                from urllib.parse import urlparse
                parsed_url = urlparse(url)
                base_url = f"{parsed_url.scheme}://{parsed_url.netloc}"
                links_text = []
                seen_texts = set()  # Avoid duplicate headlines

                # Words that indicate navigation rather than article headlines
                nav_patterns = ['trending', 'live updates', 'breaking', 'watch', 'listen',
                               'subscribe', 'sign in', 'log in', 'menu', 'search', 'more']

                for a in main_content.find_all('a', href=True):
                    # Use separator to prevent text mashing
                    link_text = a.get_text(separator=' ', strip=True)
                    # Clean up multiple spaces
                    link_text = ' '.join(link_text.split())
                    href = a['href']

                    # Skip empty, anchor-only, or very short links
                    if not link_text or len(link_text) < 15 or href.startswith('#'):
                        continue

                    # Skip navigation-like links
                    text_lower = link_text.lower()
                    if any(nav in text_lower for nav in nav_patterns):
                        continue

                    # Skip if just numbers, ranking-prefixed (e.g. "7 Avalanche AVAX"), or very generic
                    if link_text.isdigit() or text_lower in ['read more', 'click here', 'learn more']:
                        continue
                    if re.match(r'^\d+[\s.]', link_text):
                        continue

                    # Skip duplicate headlines
                    if text_lower in seen_texts:
                        continue
                    seen_texts.add(text_lower)

                    # Make relative URLs absolute
                    if href.startswith('/'):
                        href = base_url + href
                    elif not href.startswith('http'):
                        continue

                    # Format as markdown link
                    links_text.append(f"- [{link_text}]({href})")

                # Extract plain text as fallback
                text = main_content.get_text(separator="\n", strip=True)

                # Clean up whitespace and filter JS/data garbage
                raw_lines = [line.strip() for line in text.split("\n") if line.strip()]
                filtered = []
                seen = set()
                for line in raw_lines:
                    # Deduplicate identical lines (repeated table rows, nav labels)
                    key = line.lower()
                    if key in seen:
                        continue
                    seen.add(key)

                    # Skip very short tokens (nav labels, button text, currency symbols)
                    if len(line) < 8:
                        continue

                    # Skip lines that are purely numeric/symbolic (price rows, rank numbers, percentages)
                    if re.match(r'^[\d\s$%,.\-+/\\*:]+$', line):
                        continue

                    # Skip lines starting with a rank/index number or percentage rating
                    # e.g. "1 Bitcoin", "42 Solana", "100% accurate", "75% True"
                    if re.match(r'^\d+[\s.%]', line):
                        continue

                    # Skip question lines — FAQ questions cause the model to enter a Q&A loop,
                    # hallucinating follow-up questions and answers after the real response.
                    if line.endswith('?'):
                        continue

                    # Skip lines that look like JS code
                    # — long unbroken strings (minified JS, base64, data URIs)
                    if len(line) > 120 and " " not in line:
                        continue
                    # — high density of JS/JSON special characters
                    js_chars = sum(1 for c in line if c in "{}[]();=>|\\")
                    if len(line) > 0 and js_chars / len(line) > 0.12:
                        continue
                    # — starts with JS keywords
                    js_kw = ("var ", "let ", "const ", "function ", "return ",
                             "import ", "export ", "window.", "document.",
                             "module.", "require(", "__NEXT", "undefined", "null,")
                    if any(line.lstrip().startswith(kw) for kw in js_kw):
                        continue

                    # Skip lines that are predominantly non-alphabetic
                    # (price rows, percentage tables, raw numbers)
                    alpha = sum(1 for c in line if c.isalpha())
                    if len(line) > 0 and alpha / len(line) < 0.30:
                        continue

                    filtered.append(line)

                text = "\n".join(filtered)

                # Prepend extracted links if we found any
                if links_text:
                    links_section = "\n".join(links_text[:8])
                    text = f"ARTICLE LINKS:\n{links_section}\n\nPAGE TEXT:\n{text}"

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
