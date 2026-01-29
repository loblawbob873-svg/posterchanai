"""4chan catalog API - fetches board catalog with Chrome User-Agent to avoid Cloudflare."""
from fastapi import APIRouter, Query
from fastapi.responses import Response
import httpx
import logging
import re
from urllib.parse import urlparse

from app.services.proxy_utils import get_proxy_config

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/4chan", tags=["4chan"])

# Chrome User-Agent to avoid Cloudflare blocks
CHROME_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

ALLOWED_BOARDS = ("g", "pol")

# Allowed image hosts for proxy (avoid hotlink/referrer blocking)
ALLOWED_IMAGE_HOSTS = ("i.4cdn.org", "is2.4channel.org")


def _strip_html(html: str) -> str:
    if not html:
        return ""
    # Remove tags and decode common entities
    text = re.sub(r"<[^>]+>", " ", html)
    text = text.replace("&quot;", '"').replace("&#039;", "'").replace("&amp;", "&")
    return " ".join(text.split())[:200]


def _build_thread(thread: dict, board: str) -> dict:
    no = thread.get("no")
    sub = (thread.get("sub") or "").strip()
    com = thread.get("com") or ""
    title = sub or _strip_html(com) or f"Thread {no}"
    tim = thread.get("tim")
    ext = thread.get("ext") or ".jpg"
    replies = thread.get("replies", 0) or 0
    images_count = thread.get("images", 0) or 0
    last_modified = thread.get("last_modified") or 0

    thumb_url = None
    image_url = None
    if tim is not None:
        # 4chan thumb: {tim}s.jpg (letter s before .jpg)
        thumb_url = f"https://i.4cdn.org/{board}/{tim}s.jpg"
        image_url = f"https://i.4cdn.org/{board}/{tim}{ext}"

    return {
        "thread_id": no,
        "title": title,
        "thumb_url": thumb_url,
        "image_url": image_url,
        "replies": replies,
        "images": images_count,
        "link": f"https://boards.4chan.org/{board}/thread/{no}",
        "last_modified": last_modified,
    }


@router.get("/catalog")
async def get_catalog(
    board: str = Query(..., description="Board code (e.g. g, pol)"),
):
    """Fetch 4chan catalog for a board. Returns threads with thumb, title, link."""
    board = (board or "g").strip().lower()
    if board not in ALLOWED_BOARDS:
        return {"error": f"Board not allowed. Use one of: {', '.join(ALLOWED_BOARDS)}"}

    url = f"https://a.4cdn.org/{board}/catalog.json"
    headers = {"User-Agent": CHROME_UA, "Accept": "application/json"}
    proxy_config = get_proxy_config()
    client_kw = {"timeout": 15.0}
    if proxy_config:
        client_kw["proxy"] = proxy_config
        logger.debug("4chan catalog via proxy: %s", proxy_config)

    try:
        async with httpx.AsyncClient(**client_kw) as client:
            resp = await client.get(url, headers=headers)
            resp.raise_for_status()
            data = resp.json()
    except httpx.HTTPStatusError as e:
        logger.warning("4chan catalog HTTP error: %s", e)
        return {"error": f"4chan returned {e.response.status_code}"}
    except Exception as e:
        logger.warning("4chan catalog fetch error: %s", e)
        return {"error": str(e)}

    threads = []
    for page_obj in data:
        for t in page_obj.get("threads", []):
            threads.append(_build_thread(t, board))

    # Newest first: sort by last_modified descending (last bump time)
    threads.sort(key=lambda x: x["last_modified"], reverse=True)

    return {"board": board, "threads": threads}


@router.get("/proxy")
async def proxy_image(
    url: str = Query(..., description="Image URL (e.g. i.4cdn.org thumbnail)"),
):
    """Proxy 4chan CDN images to avoid referrer/hotlink blocking in the browser."""
    try:
        parsed = urlparse(url)
        if parsed.scheme != "https" or parsed.netloc not in ALLOWED_IMAGE_HOSTS:
            return Response(status_code=400, content="Invalid or disallowed URL")
    except Exception:
        return Response(status_code=400, content="Invalid URL")

    headers = {
        "User-Agent": CHROME_UA,
        "Referer": "https://boards.4chan.org/",
        "Accept": "image/webp,image/apng,image/*,*/*;q=0.8",
    }
    proxy_config = get_proxy_config()
    client_kw = {"timeout": 10.0, "follow_redirects": True}
    if proxy_config:
        client_kw["proxy"] = proxy_config
        logger.debug("4chan image proxy via proxy: %s", proxy_config)

    try:
        async with httpx.AsyncClient(**client_kw) as client:
            resp = await client.get(url, headers=headers)
            resp.raise_for_status()
            content_type = (resp.headers.get("content-type") or "image/jpeg").split(";")[0].strip() or "image/jpeg"
            return Response(
                content=resp.content,
                media_type=content_type,
                headers={"Cache-Control": "private, max-age=3600"},
            )
    except httpx.HTTPStatusError as e:
        logger.warning("4chan proxy HTTP error %s for %s", e.response.status_code, url)
        return Response(status_code=e.response.status_code)
    except Exception as e:
        logger.warning("4chan proxy error for %s: %s", url, e)
        return Response(status_code=502)
