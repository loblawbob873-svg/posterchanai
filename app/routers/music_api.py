"""Direct Music Generation API (server-to-server), mirroring image_api.

Lets one posterchanai node forward a music request to another (the unified `chat_server_urls` list). The receiving
node generates LOCALLY — `generate_music_for_user(local_only=True)` takes the shared GPU lock and
runs `prepare_for_music` (freeing its LLM/image VRAM) before its local acestep server. This is the
same node→node + VRAM-swap pattern image gen uses, and it's what makes "unload the GPU before
processing" work across machines. Returns base64 audio.
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
from app.services.music_factory import generate_music_for_user
from app.services.music_service import MusicError

logger = logging.getLogger("music_api")

router = APIRouter(prefix="/api", tags=["music"])


async def get_music_auth(
    request: Request,
    authorization: Optional[str] = Header(None),
    x_api_key: Optional[str] = Header(None),
    db: Session = Depends(get_db),
) -> bool:
    """Allow load-balanced requests from other posterchanai nodes without auth; otherwise accept
    API key / JWT (mirrors image_api.get_image_auth)."""
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


class MusicGenRequest(BaseModel):
    prompt: str
    lyrics: Optional[str] = ""
    duration: Optional[float] = None
    steps: Optional[int] = None
    format: Optional[str] = None


class MusicResponse(BaseModel):
    audio: Optional[str] = None  # base64-encoded audio
    format: Optional[str] = None
    error: Optional[str] = None


@router.post("/generate-music", response_model=MusicResponse)
async def generate_music(
    request: MusicGenRequest,
    http_request: Request,
    db: Session = Depends(get_db),
    _auth: bool = Depends(get_music_auth),
):
    """Generate a song on THIS node (local_only) and return it as base64. Used server-to-server."""
    try:
        audio_bytes, ext = await generate_music_for_user(
            db=db,
            prompt=request.prompt,
            lyrics=request.lyrics or "",
            duration=request.duration,
            steps=request.steps,
            local_only=True,
            fmt=request.format,
        )
        return MusicResponse(audio=base64.b64encode(audio_bytes).decode(), format=ext)
    except MusicError as e:
        logger.warning(f"[MUSIC-API] generation failed: {e}")
        return MusicResponse(error=str(e))
    except Exception as e:
        logger.error(f"[MUSIC-API] unexpected error: {e}", exc_info=True)
        return MusicResponse(error=f"Music generation error: {e}")
