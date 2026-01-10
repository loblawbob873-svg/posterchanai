"""
Nyaa.si search service for anime torrents.
"""
import httpx
import logging
import re
from urllib.parse import quote_plus
from bs4 import BeautifulSoup
from dataclasses import dataclass

logger = logging.getLogger(__name__)

NYAA_BASE_URL = "https://nyaa.si"


@dataclass
class NyaaResult:
    """Represents a nyaa.si torrent result"""
    title: str
    magnet: str
    size: str
    seeders: int
    leechers: int
    url: str = ""


async def search_nyaa(query: str, limit: int = 15) -> list[NyaaResult]:
    """
    Search nyaa.si for torrents.

    Args:
        query: Search query
        limit: Maximum number of results to return

    Returns:
        List of NyaaResult objects
    """
    if not query.strip():
        return []

    url = f"{NYAA_BASE_URL}/?f=0&c=0_0&q={quote_plus(query)}"

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.5",
        "Accept-Encoding": "gzip, deflate",
    }

    results = []

    try:
        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
            response = await client.get(url, headers=headers)
            response.raise_for_status()

            soup = BeautifulSoup(response.text, "lxml")

            table = soup.find("table", class_="torrent-list")
            if not table:
                logger.warning("No torrent-list table found on nyaa.si")
                return []

            rows = table.find_all("tr")[1:]  # Skip header row

            for row in rows[:limit]:
                try:
                    tds = row.find_all("td")
                    if len(tds) < 8:
                        continue

                    # Column 1: Title with link
                    title_td = tds[1]
                    title_link = title_td.find("a", href=re.compile(r"/view/\d+"))
                    if not title_link:
                        continue

                    title = title_link.get_text(strip=True)
                    if not title or len(title) < 3:
                        continue

                    detail_url = NYAA_BASE_URL + title_link.get("href", "")

                    # Column 2: Magnet link
                    links_td = tds[2]
                    magnet_link = links_td.find("a", href=re.compile(r"^magnet:\?"))
                    if not magnet_link:
                        continue

                    magnet = magnet_link.get("href", "")
                    if not magnet.startswith("magnet:"):
                        continue

                    # Column 3: Size
                    size = tds[3].get_text(strip=True) or "N/A"

                    # Column 5: Seeders
                    seeders = 0
                    try:
                        seeders = int(tds[5].get_text(strip=True))
                    except (ValueError, IndexError):
                        pass

                    # Column 6: Leechers
                    leechers = 0
                    try:
                        leechers = int(tds[6].get_text(strip=True))
                    except (ValueError, IndexError):
                        pass

                    results.append(NyaaResult(
                        title=title,
                        magnet=magnet,
                        size=size,
                        seeders=seeders,
                        leechers=leechers,
                        url=detail_url
                    ))

                except Exception as e:
                    logger.debug(f"Error parsing nyaa row: {e}")
                    continue

    except httpx.HTTPStatusError as e:
        logger.error(f"HTTP error searching nyaa.si: {e.response.status_code}")
    except Exception as e:
        logger.error(f"Error searching nyaa.si: {e}")

    return results[:limit]


def format_nyaa_results(results: list[NyaaResult], query: str) -> str:
    """Format nyaa.si results for display"""
    if not results:
        return f"No results found for '{query}' on nyaa.si"

    lines = [f"## Nyaa.si: {query}\n"]

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

        # Make title a clickable link
        if t.url:
            title_display = f"[{title}]({t.url})"
        else:
            title_display = title

        lines.append(f"**{i}. {title_display}**")
        lines.append(f"   {seed_icon} S:{t.seeders} L:{t.leechers} | {t.size}")
        lines.append(f"   `{t.magnet[:80]}...`\n")

    lines.append("---")
    lines.append("*Use `nyaa download <#>` to add to Flood*")

    return "\n".join(lines)
