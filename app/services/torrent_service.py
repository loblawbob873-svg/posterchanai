"""
TorrentGalaxy scraping service for Movies, TV, Music, and Anime
Torrent site URL is configurable via admin settings.
"""
import httpx
import logging
import re
from typing import Optional
from bs4 import BeautifulSoup
from dataclasses import dataclass
from sqlalchemy.orm import Session
from app.services import settings_store

logger = logging.getLogger(__name__)

# Default URL if not configured
DEFAULT_TORRENT_URL = "https://torrentgalaxy.one"

# Category section IDs on homepage (TorrentGalaxy uses homepage sections now)
CATEGORY_SECTIONS = {
    "movies": "Movies",
    "tv": "TV",
    "music": "Music",
    "anime": "Anime",
}


@dataclass
class TorrentResult:
    """Represents a torrent result"""
    title: str
    magnet: str
    size: str
    seeders: int
    leechers: int
    category: str
    url: str = ""  # Link to torrent detail page


def get_torrent_base_url(db: Session) -> str:
    """Get the torrent site base URL from admin settings"""
    setting = settings_store.get("torrent_site_url")
    if setting:
        return setting.rstrip("/")
    return DEFAULT_TORRENT_URL


# Public trackers added to magnets built from a bare infohash (the JSON API only
# returns the hash, not a full magnet URI).
_MAGNET_TRACKERS = (
    "udp://tracker.opentrackr.org:1337/announce",
    "udp://open.tracker.cl:1337/announce",
    "udp://tracker.openbittorrent.com:6969/announce",
    "udp://exodus.desync.com:6969/announce",
    "udp://tracker.torrent.eu.org:451/announce",
)


def _format_size(num_bytes) -> str:
    """Human-readable size from a byte count (TorrentGalaxy JSON gives raw bytes)."""
    try:
        size = float(num_bytes)
    except (TypeError, ValueError):
        return "N/A"
    for unit in ("B", "KB", "MB", "GB", "TB", "PB"):
        if size < 1024 or unit == "PB":
            return f"{size:.0f} {unit}" if unit in ("B", "KB") else f"{size:.2f} {unit}"
        size /= 1024
    return "N/A"


def _build_magnet(infohash: str, name: str) -> str:
    """Construct a magnet URI from a SHA1 infohash + display name + public trackers."""
    from urllib.parse import quote
    h = (infohash or "").strip()
    if not h:
        return ""
    magnet = f"magnet:?xt=urn:btih:{h}"
    if name:
        magnet += f"&dn={quote(name)}"
    for tr in _MAGNET_TRACKERS:
        magnet += f"&tr={quote(tr)}"
    return magnet


async def scrape_torrents(db: Session, category: str = "movies", limit: int = 15) -> list[TorrentResult]:
    """
    Scrape torrents from the configured torrent site homepage sections.

    Args:
        db: Database session for reading settings
        category: One of 'movies', 'tv', 'music', 'anime'
        limit: Maximum number of results to return

    Returns:
        List of TorrentResult objects
    """
    category = category.lower()
    if category not in CATEGORY_SECTIONS:
        logger.warning(f"Unknown category: {category}, defaulting to movies")
        category = "movies"

    section_id = CATEGORY_SECTIONS[category]
    base_url = get_torrent_base_url(db)

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.5",
        "Accept-Encoding": "gzip, deflate",
    }

    # Proxy is REQUIRED for torrent searches (privacy/security)
    from app.services.proxy_utils import require_proxy
    proxy_config = require_proxy("Torrent catalog browsing")
    
    # Validate proxy config
    if not proxy_config or not isinstance(proxy_config, str):
        logger.error(f"Invalid proxy config for torrents: {proxy_config}")
        raise ValueError(f"Invalid proxy configuration: {proxy_config}")
    
    results = []

    try:
        logger.info(f"Fetching torrents via proxy: {proxy_config} from {base_url}")
        async with httpx.AsyncClient(timeout=30, follow_redirects=True, proxy=proxy_config) as client:
            response = await client.get(base_url, headers=headers)
            response.raise_for_status()

            soup = BeautifulSoup(response.text, "lxml")

            # Detect Cloudflare/bot-protection pages
            page_title = soup.title.string if soup.title else ""
            if any(x in page_title.lower() for x in ("cloudflare", "just a moment", "ddos", "access denied")):
                logger.warning(f"Torrent site returned bot-protection page: {page_title}")
                raise ValueError(f"Torrent site is blocked by bot protection ({page_title}). Try a different torrent_site_url in Admin Settings.")

            # Find the section header for this category
            section_header = soup.find("h3", id=section_id)
            if not section_header:
                # Log available h3 IDs to help diagnose site structure changes
                h3_ids = [h.get("id") for h in soup.find_all("h3") if h.get("id")]
                logger.warning(f"Could not find section '{section_id}'. Available h3 ids: {h3_ids}")
                if h3_ids:
                    raise ValueError(f"Section '{section_id}' not found on torrent site. Site may have changed structure. Available sections: {h3_ids}")
                raise ValueError(f"No category sections found on torrent site ({base_url}). Site structure may have changed or the page returned invalid content.")

            # Find the parent panel containing the torrents
            panel = section_header.find_parent("div", class_="panel")
            if not panel:
                logger.warning(f"Could not find panel for section: {section_id}")
                raise ValueError(f"Could not find torrent listing panel for section '{section_id}'. Site structure may have changed.")

            # Find all torrent rows within this panel
            torrent_rows = panel.find_all("div", class_="tgxtablerow")

            for row in torrent_rows[:limit]:
                try:
                    # Find title link
                    title_elem = row.find("a", href=re.compile(r"/post-detail/"))
                    if not title_elem:
                        continue

                    title = title_elem.get_text(strip=True)
                    if not title or len(title) < 3:
                        continue

                    detail_url = base_url + title_elem.get("href", "")

                    # Find magnet link
                    magnet_elem = row.find("a", href=re.compile(r"^magnet:\?"))
                    if not magnet_elem:
                        continue

                    magnet = magnet_elem.get("href", "")
                    if not magnet.startswith("magnet:"):
                        continue

                    # Find size
                    size = "N/A"
                    row_text = row.get_text()
                    size_match = re.search(r'(\d+(?:\.\d+)?\s*(?:GB|MB|KB|TB|GiB|MiB))', row_text, re.IGNORECASE)
                    if size_match:
                        size = size_match.group(1)

                    # Find seeders/leechers from font colors
                    seeders = 0
                    leechers = 0

                    green_fonts = row.find_all("font", color=re.compile(r"green", re.I))
                    for font in green_fonts:
                        try:
                            val = int(re.sub(r'[^\d]', '', font.get_text()))
                            if val >= 0:
                                seeders = val
                                break
                        except ValueError:
                            continue

                    red_fonts = row.find_all("font", color=re.compile(r"red", re.I))
                    for font in red_fonts:
                        try:
                            val = int(re.sub(r'[^\d]', '', font.get_text()))
                            if val >= 0:
                                leechers = val
                                break
                        except ValueError:
                            continue

                    results.append(TorrentResult(
                        title=title,
                        magnet=magnet,
                        size=size,
                        seeders=seeders,
                        leechers=leechers,
                        category=category,
                        url=detail_url
                    ))

                except Exception as e:
                    logger.debug(f"Error parsing torrent row: {e}")
                    continue

    except httpx.HTTPStatusError as e:
        logger.error(f"HTTP error scraping torrent site: {e.response.status_code} from {base_url}")
        raise ValueError(f"Torrent site returned HTTP {e.response.status_code}. The site may be down or blocking requests. Try a different torrent_site_url in Admin Settings.")
    except httpx.RequestError as e:
        logger.error(f"Request error scraping torrent site via {proxy_config}: {e}")
        raise ValueError(f"Could not reach torrent site via proxy ({proxy_config}): {str(e)}\n\nCheck that the proxy is running and accessible.")
    except ValueError:
        # Re-raise descriptive errors from above
        raise
    except Exception as e:
        logger.error(f"Error scraping torrent site: {e}", exc_info=True)
        raise ValueError(f"Error scraping torrent site: {str(e)}")

    return results[:limit]


def format_torrent_results(results: list[TorrentResult], category: str, title: str = None) -> str:
    """Format torrent results for display

    Args:
        results: List of torrent results
        category: Category identifier for download commands (e.g., "search", "movies")
        title: Optional display title (defaults to category.upper())
    """
    if not results:
        return f"No {category} torrents found. The site may be temporarily unavailable or not configured."

    category_title = title if title else category.upper()
    lines = [f"## ◈ {category_title} TORRENTS ◈\n"]

    for i, t in enumerate(results, 1):
        # Truncate long titles
        title = t.title[:70] + "..." if len(t.title) > 70 else t.title
        # Escape brackets in title to prevent Rich markup parsing errors
        title_escaped = title.replace("[", "(").replace("]", ")")

        # Make title a clickable link if URL available
        if t.url:
            title_display = f"[{title_escaped}]({t.url})"
        else:
            title_display = title_escaped

        # Download button with numbered reference (magnet stored in cache)
        dl_cmd = f"torrents download {category} {i}"
        # Magnet link for native Android app (encoded so ) in magnet doesn't break markdown)
        from urllib.parse import quote
        magnet_enc = quote(t.magnet, safe="")

        lines.append(f"**{i}. {title_display}**")
        lines.append(f"   [Download](cmd:{dl_cmd}) [Add](magnet:{magnet_enc}) | S:{t.seeders} L:{t.leechers} | {t.size}\n")

    return "\n".join(lines)


async def get_torrents_formatted(db: Session, category: str = "movies", limit: int = 15) -> str:
    """
    Convenience function to get formatted torrent results.

    Args:
        db: Database session for reading settings
        category: One of 'movies', 'tv', 'music', 'anime'
        limit: Maximum number of results

    Returns:
        Formatted string for display
    """
    results = await scrape_torrents(db, category, limit)
    return format_torrent_results(results, category)


async def search_torrents(db: Session, query: str, limit: int = 15) -> list[TorrentResult]:
    """
    Search torrents on the configured torrent site.

    Args:
        db: Database session for reading settings
        query: Search query
        limit: Maximum number of results to return

    Returns:
        List of TorrentResult objects
    """
    if not query.strip():
        return []

    from urllib.parse import quote
    base_url = get_torrent_base_url(db)
    # TorrentGalaxy renders results client-side from a JSON endpoint; appending
    # ":format:json" to the keywords search returns the post list directly (each
    # item carries the infohash, so we build magnets without per-detail fetches).
    search_url = f"{base_url}/get-posts/keywords:{quote(query)}:format:json/"

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "application/json",
        "Accept-Language": "en-US,en;q=0.5",
    }

    # Proxy is REQUIRED for torrent searches (privacy/security)
    from app.services.proxy_utils import require_proxy
    proxy_config = require_proxy("Torrent search")

    # Validate proxy config
    if not proxy_config or not isinstance(proxy_config, str):
        logger.error(f"Invalid proxy config for torrent search: {proxy_config}")
        raise ValueError(f"Invalid proxy configuration: {proxy_config}")

    results = []

    try:
        logger.info(f"Searching torrents via proxy: {proxy_config} ({len(query or '')}-char query)")
        async with httpx.AsyncClient(timeout=15, follow_redirects=True, proxy=proxy_config) as client:
            logger.info(f"Searching torrents: {search_url}")
            response = await client.get(search_url, headers=headers)
            response.raise_for_status()
            data = response.json()

        posts = data.get("results", []) if isinstance(data, dict) else []
        logger.info(f"Found {len(posts)} torrent results (total {data.get('total') if isinstance(data, dict) else '?'})")

        for post in posts[:limit]:
            try:
                infohash = (post.get("h") or "").strip()
                if not infohash:
                    continue
                title = (post.get("n") or "").strip() or infohash
                magnet = _build_magnet(infohash, title)
                pk = (post.get("pk") or "").strip()
                detail_url = f"{base_url}/post-detail/{pk}/" if pk else ""
                results.append(TorrentResult(
                    title=title,
                    magnet=magnet,
                    size=_format_size(post.get("s")),
                    seeders=int(post.get("se") or 0),
                    leechers=int(post.get("le") or 0),
                    category=(post.get("c") or "search"),
                    url=detail_url,
                ))
            except Exception as e:
                logger.debug(f"Error parsing search result: {e}")
                continue

    except httpx.HTTPStatusError as e:
        logger.error(f"HTTP error searching torrents: {e.response.status_code} from {search_url}")
        raise ValueError(f"Torrent site returned HTTP {e.response.status_code}. The site may be down or blocking requests.")
    except httpx.RequestError as e:
        logger.error(f"Request error searching torrents via {proxy_config}: {e}")
        raise ValueError(f"Could not reach torrent site via proxy ({proxy_config}): {str(e)}\n\nCheck that the proxy is running and accessible.")
    except ValueError:
        # Re-raise descriptive errors from above
        raise
    except Exception as e:
        logger.error(f"Error searching torrents: {e}", exc_info=True)
        raise ValueError(f"Error searching torrents: {str(e)}")

    return results[:limit]


async def scrape_all_categories(db: Session, limit_per_category: int = 5) -> dict[str, list[TorrentResult]]:
    """
    Scrape torrents from all categories.

    Args:
        db: Database session for reading settings
        limit_per_category: Maximum results per category

    Returns:
        Dict mapping category name to list of TorrentResult
    """
    import asyncio
    categories = list(CATEGORY_SECTIONS.keys())
    tasks = [scrape_torrents(db, cat, limit_per_category) for cat in categories]
    results_list = await asyncio.gather(*tasks, return_exceptions=True)

    all_results = {}
    for cat, results in zip(categories, results_list):
        if isinstance(results, Exception):
            logger.error(f"Error scraping {cat}: {results}")
            all_results[cat] = []
        else:
            all_results[cat] = results

    return all_results


def format_all_categories(all_results: dict[str, list[TorrentResult]]) -> str:
    """Format results from all categories for display"""
    lines = ["## ◈ TORRENTS ◈\n"]

    for category, results in all_results.items():
        cat_title = category.upper()
        lines.append(f"### {cat_title}\n")

        if not results:
            lines.append(f"*No {category} torrents found*\n")
            continue

        for i, t in enumerate(results, 1):
            title = t.title[:60] + "..." if len(t.title) > 60 else t.title
            # Escape brackets in title to prevent Rich markup parsing errors
            title_escaped = title.replace("[", "(").replace("]", ")")

            # Make title a clickable link if URL available
            if t.url:
                title_display = f"[{title_escaped}]({t.url})"
            else:
                title_display = title_escaped

            # Download button with numbered reference
            dl_cmd = f"torrents download {category} {i}"

            lines.append(f"{i}. {title_display} ({t.size})")
            lines.append(f"   [Download](cmd:{dl_cmd})")

        lines.append("")

    return "\n".join(lines)
