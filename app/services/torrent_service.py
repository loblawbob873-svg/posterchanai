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

# Category paths (appended to base URL)
CATEGORY_PATHS = {
    "movies": "/torrents.php?cat=1",      # Movies
    "tv": "/torrents.php?cat=41",         # TV Shows
    "music": "/torrents.php?cat=22",      # Music
    "anime": "/torrents.php?cat=28",      # Anime
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
    Scrape torrents from the configured torrent site for a given category.

    Args:
        db: Database session for reading settings
        category: One of 'movies', 'tv', 'music', 'anime'
        limit: Maximum number of results to return

    Returns:
        List of TorrentResult objects
    """
    category = category.lower()
    if category not in CATEGORY_PATHS:
        logger.warning(f"Unknown category: {category}, defaulting to movies")
        category = "movies"

    base_url = get_torrent_base_url(db)
    url = base_url + CATEGORY_PATHS[category]

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.5",
        "Referer": base_url,
    }

    results = []

    try:
        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
            response = await client.get(url, headers=headers)
            response.raise_for_status()

            soup = BeautifulSoup(response.text, "lxml")

            # TorrentGalaxy uses a table-based or div-based layout for torrent listings
            # Look for torrent rows - they typically have class containing 'tgxtable' or similar
            torrent_rows = soup.select("div.tgxtablerow, tr.tgxtablerow, div[class*='torrent']")

            if not torrent_rows:
                # Fallback: look for any rows with magnet links
                torrent_rows = soup.find_all("div", class_=re.compile(r"tgx"))
                if not torrent_rows:
                    # Another fallback - find all elements containing magnet links
                    magnet_links = soup.find_all("a", href=re.compile(r"^magnet:\?"))
                    for magnet_link in magnet_links[:limit]:
                        magnet_href = magnet_link.get("href", "")
                        # Try to find the title from nearby elements
                        parent = magnet_link.find_parent(["div", "tr"])
                        title = "Unknown"
                        if parent:
                            title_link = parent.find("a", href=re.compile(r"/(?:torrent|post-detail)/"))
                            if title_link:
                                title = title_link.get_text(strip=True)
                                detail_url = base_url + title_link.get("href", "")
                            else:
                                # Get first meaningful text
                                title = parent.get_text(separator=" ", strip=True)[:100]
                                detail_url = ""

                        results.append(TorrentResult(
                            title=title,
                            magnet=magnet_href,
                            size="N/A",
                            seeders=0,
                            leechers=0,
                            category=category,
                            url=detail_url
                        ))
                    return results[:limit]

            for row in torrent_rows[:limit * 2]:  # Get more rows in case some are invalid
                try:
                    # Find title - usually in a link to the torrent details page
                    title_elem = row.find("a", href=re.compile(r"/(?:torrent|post-detail)/"))
                    if not title_elem:
                        title_elem = row.find("a", class_=re.compile(r"txlight|title"))

                    if not title_elem:
                        continue

                    title = title_elem.get_text(strip=True)
                    if not title or len(title) < 3:
                        continue

                    # Get detail page URL
                    detail_url = base_url + title_elem.get("href", "")

                    # Find magnet link
                    magnet_elem = row.find("a", href=re.compile(r"^magnet:\?"))
                    if not magnet_elem:
                        # Sometimes magnet is in a child element
                        magnet_elem = row.select_one("a[href^='magnet:']")

                    if not magnet_elem:
                        continue

                    magnet = magnet_elem.get("href", "")
                    if not magnet.startswith("magnet:"):
                        continue

                    # Find size - look for common size patterns
                    size = "N/A"
                    size_elem = row.find("span", class_=re.compile(r"size"))
                    if size_elem:
                        size = size_elem.get_text(strip=True)
                    else:
                        # Look for size pattern in text
                        row_text = row.get_text()
                        size_match = re.search(r'(\d+(?:\.\d+)?\s*(?:GB|MB|KB|TB))', row_text, re.IGNORECASE)
                        if size_match:
                            size = size_match.group(1)

                    # Find seeders/leechers
                    seeders = 0
                    leechers = 0

                    # Look for seeder/leecher elements (often have specific classes or are in order)
                    seed_elem = row.find("span", class_=re.compile(r"seed|green"))
                    leech_elem = row.find("span", class_=re.compile(r"leech|red"))

                    if seed_elem:
                        try:
                            seeders = int(re.sub(r'[^\d]', '', seed_elem.get_text()))
                        except ValueError:
                            pass

                    if leech_elem:
                        try:
                            leechers = int(re.sub(r'[^\d]', '', leech_elem.get_text()))
                        except ValueError:
                            pass

                    # If we couldn't find specific elements, try font color based approach
                    if seeders == 0:
                        green_fonts = row.find_all("font", color=re.compile(r"green|#0f0|#00ff00", re.I))
                        for font in green_fonts:
                            try:
                                val = int(re.sub(r'[^\d]', '', font.get_text()))
                                if val > 0:
                                    seeders = val
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

                    if len(results) >= limit:
                        break

                except Exception as e:
                    logger.debug(f"Error parsing torrent row: {e}")
                    continue

    except httpx.HTTPStatusError as e:
        logger.error(f"HTTP error scraping torrent site: {e.response.status_code}")
    except Exception as e:
        logger.error(f"Error scraping torrent site: {e}")

    return results[:limit]


def format_torrent_results(results: list[TorrentResult], category: str) -> str:
    """Format torrent results for display"""
    if not results:
        return f"No {category} torrents found. The site may be temporarily unavailable or not configured."

    category_title = category.upper()
    lines = [f"## {category_title} Torrents\n"]

    for i, t in enumerate(results, 1):
        # Truncate long titles
        title = t.title[:70] + "..." if len(t.title) > 70 else t.title

        # Seeder indicator
        if t.seeders >= 50:
            seed_icon = "🟢"
        elif t.seeders >= 10:
            seed_icon = "🟡"
        elif t.seeders > 0:
            seed_icon = "🟠"
        else:
            seed_icon = "🔴"

        # Make title a clickable link if URL available
        if t.url:
            title_display = f"[{title}]({t.url})"
        else:
            title_display = title

        lines.append(f"**{i}. {title_display}**")
        lines.append(f"   {seed_icon} S:{t.seeders} L:{t.leechers} | {t.size}")
        lines.append(f"   `{t.magnet[:80]}...`\n")

    lines.append("---")
    lines.append("*Use `flood add <magnet>` to download a torrent*")

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
    categories = list(CATEGORY_PATHS.keys())
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
    lines = ["## Torrents\n"]

    for category, results in all_results.items():
        cat_title = category.upper()
        lines.append(f"### {cat_title}\n")

        if not results:
            lines.append(f"*No {category} torrents found*\n")
            continue

        for i, t in enumerate(results, 1):
            title = t.title[:60] + "..." if len(t.title) > 60 else t.title

            # Seeder indicator
            if t.seeders >= 50:
                seed_icon = "🟢"
            elif t.seeders >= 10:
                seed_icon = "🟡"
            elif t.seeders > 0:
                seed_icon = "🟠"
            else:
                seed_icon = "🔴"

            # Make title a clickable link if URL available
            if t.url:
                title_display = f"[{title}]({t.url})"
            else:
                title_display = title

            lines.append(f"{i}. {seed_icon} {title_display} ({t.size})")

        lines.append("")

    lines.append("---")
    lines.append("*Use `torrents movies`, `torrents tv`, `torrents music`, or `torrents anime` for more*")

    return "\n".join(lines)
