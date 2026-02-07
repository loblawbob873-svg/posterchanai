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
from app.models import Setting

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
    setting = db.query(Setting).filter(Setting.key == "torrent_site_url").first()
    if setting and setting.value:
        return setting.value.rstrip("/")
    return DEFAULT_TORRENT_URL


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

            # Find the section header for this category
            section_header = soup.find("h3", id=section_id)
            if not section_header:
                logger.warning(f"Could not find section: {section_id}")
                return []

            # Find the parent panel containing the torrents
            panel = section_header.find_parent("div", class_="panel")
            if not panel:
                logger.warning(f"Could not find panel for section: {section_id}")
                return []

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
        logger.error(f"HTTP error scraping torrent site: {e.response.status_code}")
        raise ValueError(f"HTTP error {e.response.status_code} accessing torrent site. Check proxy configuration.")
    except httpx.RequestError as e:
        logger.error(f"Request error scraping torrent site: {e}")
        raise ValueError(f"Failed to connect to torrent site. Check proxy configuration: {str(e)}")
    except ValueError:
        # Re-raise proxy requirement errors
        raise
    except Exception as e:
        logger.error(f"Error scraping torrent site: {e}", exc_info=True)
        raise ValueError(f"Error accessing torrent site: {str(e)}")

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

    from urllib.parse import quote_plus
    base_url = get_torrent_base_url(db)
    # TorrentGalaxy uses /get-posts/keywords: endpoint
    search_url = f"{base_url}/get-posts/keywords:{quote_plus(query)}"

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.5",
        "Accept-Encoding": "gzip, deflate",
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
        logger.info(f"Searching torrents via proxy: {proxy_config} for query: {query}")
        async with httpx.AsyncClient(timeout=15, follow_redirects=True, proxy=proxy_config) as client:
            logger.info(f"Searching torrents: {search_url}")
            response = await client.get(search_url, headers=headers)
            response.raise_for_status()

            soup = BeautifulSoup(response.text, "lxml")

            # Find all torrent rows (may have multiple classes like "tgxtablerow txlight")
            torrent_rows = soup.find_all("div", class_=lambda c: c and "tgxtablerow" in c.split())
            logger.info(f"Found {len(torrent_rows)} torrent rows")

            # Collect detail URLs first
            detail_urls = []
            for row in torrent_rows[:limit]:
                try:
                    # Find title link - search uses /post-detail/ links
                    title_link = row.find("a", href=re.compile(r"/post-detail/"))
                    if not title_link:
                        continue

                    title = title_link.get_text(strip=True)
                    detail_path = title_link.get("href", "")
                    detail_url = base_url + detail_path if detail_path.startswith("/") else detail_path

                    detail_urls.append((title, detail_url, row))
                except Exception as e:
                    logger.debug(f"Error parsing search row: {e}")
                    continue

            logger.info(f"Found {len(detail_urls)} detail URLs from search results")

            # Fetch detail pages to get magnet links (in parallel for speed)
            import asyncio

            async def fetch_magnet(title, detail_url, row):
                try:
                    # Proxy is required - use same proxy config for detail requests
                    logger.debug(f"Fetching magnet via proxy {proxy_config} for: {detail_url}")
                    async with httpx.AsyncClient(timeout=10, proxy=proxy_config) as detail_client:
                        detail_resp = await detail_client.get(detail_url, headers=headers)
                        detail_soup = BeautifulSoup(detail_resp.text, "lxml")
                        
                        # Find magnet link on detail page
                        magnet_link = detail_soup.find("a", href=re.compile(r"^magnet:\?"))
                        if not magnet_link:
                            return None

                        magnet = magnet_link.get("href", "")
                        if not magnet.startswith("magnet:"):
                            return None

                        return (title, detail_url, row, magnet)
                except Exception as e:
                    logger.debug(f"Error fetching magnet for {title}: {e}")
                    return None

            # Fetch magnets in parallel
            logger.info(f"Fetching magnet links for {len(detail_urls)} torrents...")
            magnet_tasks = [fetch_magnet(title, url, row) for title, url, row in detail_urls]
            magnet_results = await asyncio.gather(*magnet_tasks)

            successful_magnets = [r for r in magnet_results if r is not None]
            logger.info(f"Successfully fetched {len(successful_magnets)} magnet links")

            for result in magnet_results:
                if not result:
                    continue

                title, detail_url, row, magnet = result
                try:

                    # Find size
                    size = "N/A"
                    row_text = row.get_text()
                    size_match = re.search(r'(\d+(?:\.\d+)?\s*(?:GB|MB|KB|TB|GiB|MiB))', row_text, re.IGNORECASE)
                    if size_match:
                        size = size_match.group(1)

                    # Find seeders/leechers
                    seeders = 0
                    leechers = 0
                    stats_spans = row.find_all("span", class_="badge")
                    for span in stats_spans:
                        text = span.get_text(strip=True)
                        if text.isdigit():
                            if span.get("title", "").lower() == "seeders":
                                seeders = int(text)
                            elif span.get("title", "").lower() == "leechers":
                                leechers = int(text)

                    results.append(TorrentResult(
                        title=title,
                        magnet=magnet,
                        size=size,
                        seeders=seeders,
                        leechers=leechers,
                        category="search",
                        url=detail_url
                    ))

                except Exception as e:
                    logger.debug(f"Error parsing torrent row: {e}")
                    continue

    except httpx.HTTPStatusError as e:
        logger.error(f"HTTP error searching torrents: {e.response.status_code}")
        raise ValueError(f"HTTP error {e.response.status_code} accessing torrent site. Check proxy configuration.")
    except httpx.RequestError as e:
        logger.error(f"Request error searching torrents: {e}")
        raise ValueError(f"Failed to connect to torrent site. Check proxy configuration: {str(e)}")
    except ValueError:
        # Re-raise proxy requirement errors
        raise
    except Exception as e:
        logger.error(f"Error searching torrents: {e}", exc_info=True)
        raise ValueError(f"Error accessing torrent site: {str(e)}")

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
