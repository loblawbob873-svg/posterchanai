"""Direct Image-Edit API (server-to-server), mirroring video_api / music_api / image_api.

Lets one posterchanai node forward a `regeni` edit to another (`regeni_server_urls`). The receiving
node edits LOCALLY — `edit_image_for_user(local_only=True)` takes the shared GPU lock and runs
`prepare_for_imageedit` (freeing its LLM/image/video VRAM) before the native diffusers OmniGen
pipeline. Returns base64 PNG.
"""
import base64
import logging
from typing import Optional

from fastapi import APIRouter, Depends, Header, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.database import get_db
from app.auth import get_current_user_optional
from app.services.imageedit_factory import edit_image_for_user
from app.services.imageedit_service import ImageEditError

logger = logging.getLogger("imageedit_api")

router = APIRouter(prefix="/api", tags=["imageedit"])


async def get_imageedit_auth(
    request: Request,
    authorization: Optional[str] = Header(None),
    x_api_key: Optional[str] = Header(None),
    db: Session = Depends(get_db),
) -> bool:
    """Allow load-balanced requests from other posterchanai nodes without auth; otherwise accept
    API key / JWT (mirrors video_api.get_video_auth)."""
    if request.headers.get("x-posterchanai-load-balanced", "").lower() == "true":
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


class ImageEditRequest(BaseModel):
    image: str  # base64-encoded input image
    instruction: str


class ImageEditResponse(BaseModel):
    image: Optional[str] = None  # base64-encoded PNG
    format: Optional[str] = None
    error: Optional[str] = None


@router.post("/edit-image", response_model=ImageEditResponse)
async def edit_image(
    request: ImageEditRequest,
    http_request: Request,
    db: Session = Depends(get_db),
    _auth: bool = Depends(get_imageedit_auth),
):
    """Edit an image on THIS node (local_only) and return it as base64 PNG. Used server-to-server."""
    try:
        image_bytes = base64.b64decode(request.image)
    except Exception:
        return ImageEditResponse(error="Invalid base64 image.")
    try:
        out = await edit_image_for_user(
            db=db,
            image_bytes=image_bytes,
            instruction=request.instruction,
            local_only=True,
        )
        return ImageEditResponse(image=base64.b64encode(out).decode(), format="png")
    except ImageEditError as e:
        logger.warning(f"[REGENI-API] edit failed: {e}")
        return ImageEditResponse(error=str(e))
    except Exception as e:
        logger.error(f"[REGENI-API] unexpected error: {e}", exc_info=True)
        return ImageEditResponse(error=f"Image edit error: {e}")
