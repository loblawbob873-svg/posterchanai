"""Direct Video Generation API (server-to-server), mirroring music_api / image_api.

Lets one posterchanai node forward a video request to another (the unified `chat_server_urls` list). The receiving
node generates LOCALLY — `generate_video_for_user(local_only=True)` takes the shared GPU lock and
runs `prepare_for_video` (freeing its LLM/image VRAM) before its native diffusers Wan pipeline, then
assembles the branded MP4. Returns base64 mp4.
"""
import base64
import logging
from typing import Optional

from fastapi import APIRouter, Depends, Header, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.database import get_db
from app.utils import lb_auth
from app.auth import get_current_user_optional
from app.services.video_factory import generate_video_for_user
from app.services.video_service import VideoError

logger = logging.getLogger("video_api")

router = APIRouter(prefix="/api", tags=["video"])


async def get_video_auth(
    request: Request,
    authorization: Optional[str] = Header(None),
    x_api_key: Optional[str] = Header(None),
    db: Session = Depends(get_db),
) -> bool:
    """Allow load-balanced requests from other posterchanai nodes without auth; otherwise accept
    API key / JWT (mirrors music_api.get_music_auth)."""
    # A peer node, proven by the shared secret once `lb_shared_secret` is set. The bare header
    # alone is settable by any caller — see app/utils/lb_auth.py.
    if lb_auth.is_internal(request):
        return True
    for token in (x_api_key, (authorization[7:] if authorization and authorization.startswith("Bearer ") else None)):
        if not token:
            continue
        try:
            from app.utils.auth_utils import query_api_key_with_retry, get_user_from_api_key
            api_key, user_id = query_api_key_with_retry(db, str(token).strip())
            if api_key and user_id and get_user_from_api_key(db, user_id):
                return True
        except Exception:
            pass
    try:
        if get_current_user_optional(request, db):
            return True
    except Exception:
        pass
    return True


class VideoGenRequest(BaseModel):
    prompt: str
    negative_prompt: Optional[str] = ""


class VideoResponse(BaseModel):
    video: Optional[str] = None  # base64-encoded mp4
    format: Optional[str] = None
    error: Optional[str] = None


@router.post("/generate-video", response_model=VideoResponse)
async def generate_video(
    request: VideoGenRequest,
    http_request: Request,
    db: Session = Depends(get_db),
    _auth: bool = Depends(get_video_auth),
):
    """Generate a clip on THIS node (local_only) and return it as base64 mp4. Used server-to-server."""
    try:
        video_bytes = await generate_video_for_user(
            db=db,
            prompt=request.prompt,
            negative_prompt=request.negative_prompt or "",
            local_only=True,
        )
        return VideoResponse(video=base64.b64encode(video_bytes).decode(), format="mp4")
    except VideoError as e:
        logger.warning(f"[VIDEO-API] generation failed: {e}")
        return VideoResponse(error=str(e))
    except Exception as e:
        logger.error(f"[VIDEO-API] unexpected error: {e}", exc_info=True)
        return VideoResponse(error=f"Video generation error: {e}")
