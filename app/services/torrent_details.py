"""A torrent's page on nyaa.si or TorrentGalaxy, read for Torrents → Nyaa/TGX → Details.

The lists only carry a title, size and peer counts; what somebody wants before downloading is the
uploader's DESCRIPTION (release notes, subtitles, encode settings) and the FILE LIST. Both come from the
site's own detail page, fetched here -- through this node's proxy like every other scrape of these sites,
never from the browser.

The page address is never taken from the client. The client names a site and an id (nyaa's numeric
/view/<id>, TGX's hex post key) and the URL is BUILT here from the configured base, so this cannot be
pointed at anything else. Answers are cached for ten minutes: opening the same details twice must not
cost a second request to a site that rate-limits.
"""
import logging
import re
import time
from typing import Optional

import httpx
from bs4 import BeautifulSoup

from app.services.nyaa_service import NYAA_BASE_URL

logger = logging.getLogger(__name__)

_NYAA_ID = re.compile(r"^\d{1,12}$")
_TGX_ID = re.compile(r"^[0-9a-f]{1,16}$")
_MAX_DESC = 20000
_MAX_FILES = 300
_CACHE_TTL = 600
_cache: dict = {}

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
}


def tgx_slug(title: str) -> str:
    """TGX's detail URL needs a slug after the key (`/post-detail/<pk>/<slug>/`) -- without one it is a
    404. Any slug is accepted; this is the one its own pages use."""
    s = re.sub(r"[^a-z0-9]+", "-", (title or "").lower()).strip("-")
    return s[:120] or "torrent"


def page_url(src: str, ident: str, base_tgx: str) -> str:
    if src == "nyaa" and _NYAA_ID.match(ident or ""):
        return f"{NYAA_BASE_URL}/view/{ident}"
    if src == "tgx" and _TGX_ID.match((ident or "").lower()):
        return f"{base_tgx.rstrip('/')}/post-detail/{ident.lower()}/{tgx_slug('')}/"
    raise ValueError("unknown torrent")


def _t(el) -> str:
    return el.get_text(" ", strip=True).replace("\u00a0", " ")


def _clean(text: str) -> str:
    text = re.sub(r"[ \t ]+", " ", text or "")
    text = re.sub(r" *\n *", "\n", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def parse_nyaa(html: str) -> dict:
    soup = BeautifulSoup(html, "lxml")
    title_el = soup.select_one(".panel-title")
    info = []
    for label in soup.select(".panel-body .row > div.col-md-1"):
        value = label.find_next_sibling("div")
        name = label.get_text(" ", strip=True).rstrip(":")
        if value is not None and name:
            info.append([name, _t(value)])
    desc_el = soup.select_one("#torrent-description")
    # nyaa stores the description as Markdown text; the client shows it as text with its links.
    desc = desc_el.get_text() if desc_el else ""
    files = []
    for li in soup.select(".torrent-file-list li"):
        if li.find("ul"):
            continue                                  # a folder: its files are listed under it
        size_el = li.select_one(".file-size")
        size = size_el.get_text(strip=True).strip("()") if size_el else ""
        if size_el:
            size_el.extract()
        files.append([_t(li), size.replace("\u00a0", " ")])
    return {"title": title_el.get_text(" ", strip=True) if title_el else "",
            "info": info, "description": _clean(desc)[:_MAX_DESC], "files": files[:_MAX_FILES],
            "file_count": len(files)}


def parse_tgx(html: str) -> dict:
    soup = BeautifulSoup(html, "lxml")
    info = []
    for row in soup.select(".tprow"):
        cells = row.select(".tpcell")
        if len(cells) < 2:
            continue
        name = cells[0].get_text(" ", strip=True).rstrip(":").strip()
        value = _t(cells[1])
        if name in ("Category", "Language", "Total Size", "Info Hash", "Added By", "Added") and value:
            info.append([name, value])
    desc = ""
    anchor = soup.find("a", attrs={"name": "description"})
    block = anchor.find_next("div", class_="container-fluid") if anchor else None
    if block is not None:
        for br in block.find_all("br"):
            br.replace_with("\n")
        for img in block.find_all("img"):
            img.decompose()                            # cover art: not text, and never fetched from here
        desc = block.get_text()
    files = []
    for tr in soup.select("#k1 tr"):
        tds = tr.find_all("td")
        if len(tds) >= 2:
            files.append([_t(tds[0]), _t(tds[1])])
    title = ""
    og = soup.find("meta", attrs={"property": "og:title"}) or soup.find("title")
    if og is not None:
        title = (og.get("content") if og.name == "meta" else og.get_text()) or ""
        title = re.sub(r"\s*(torrent)?\s*\|\s*torrentGalaxy\s*$", "", re.sub(r"^Download\s+", "", title.strip()), flags=re.I)
    return {"title": title, "info": info, "description": _clean(desc)[:_MAX_DESC],
            "files": files[:_MAX_FILES], "file_count": len(files)}


async def fetch_details(src: str, ident: str, base_tgx: str, proxy: str) -> dict:
    url = page_url(src, ident, base_tgx)
    key = (src, url)
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < _CACHE_TTL:
        return hit[1]
    async with httpx.AsyncClient(timeout=30, follow_redirects=True, proxy=proxy) as client:
        r = await client.get(url, headers=_HEADERS)
    if r.status_code == 404:
        raise LookupError("That torrent is no longer on the site.")
    r.raise_for_status()
    out = parse_nyaa(r.text) if src == "nyaa" else parse_tgx(r.text)
    out["url"] = url
    _cache[key] = (time.time(), out)
    if len(_cache) > 200:
        for k in sorted(_cache, key=lambda k: _cache[k][0])[:50]:
            _cache.pop(k, None)
    return out
