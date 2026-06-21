"""YouTube thumbnail proxy - serves img.youtube.com thumbnails from same origin so they load in chat."""
import logging
import re

import httpx
from fastapi import APIRouter, Query
from fastapi.responses import Response

from app.services.proxy_utils import afallback_transport

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["youtube-thumb"])

# Only allow valid YouTube video IDs (11 chars, alphanumeric + - _)
VIDEO_ID_RE = re.compile(r"^[a-zA-Z0-9_-]{11}$")


@router.get("/youtube-thumbnail")
async def youtube_thumbnail(
    video_id: str = Query(..., description="YouTube video ID (e.g. 8o-WO5LmWbA)"),
):
    """Proxy YouTube thumbnail so it loads from same origin (avoids referrer/blocking in chat)."""
    video_id = (video_id or "").strip()
    if not VIDEO_ID_RE.match(video_id):
        return Response(status_code=400, content="Invalid video_id")

    url = f"https://img.youtube.com/vi/{video_id}/hqdefault.jpg"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "image/webp,image/*,*/*;q=0.8",
    }
    client_kw = {"timeout": 10.0, "follow_redirects": True, "transport": afallback_transport()}   # proxy-first, direct fallback

    try:
        async with httpx.AsyncClient(**client_kw) as client:
            resp = await client.get(url, headers=headers)
            resp.raise_for_status()
            content_type = (resp.headers.get("content-type") or "image/jpeg").split(";")[0].strip() or "image/jpeg"
            return Response(
                content=resp.content,
                media_type=content_type,
                headers={"Cache-Control": "private, max-age=86400"},
            )
    except httpx.HTTPStatusError as e:
        logger.warning("YouTube thumbnail HTTP %s for video_id=%s", e.response.status_code, video_id)
        return Response(status_code=e.response.status_code)
    except Exception as e:
        logger.warning("YouTube thumbnail error for video_id=%s: %s", video_id, e)
        return Response(status_code=502)
