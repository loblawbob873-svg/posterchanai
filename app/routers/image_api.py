"""
Direct Image Generation API for external integrations.
Provides simple REST endpoints for txt2img.
Supports both JWT auth and API key auth for external services.
Sequential processing: Only one image is generated at a time to prevent GPU overload.
"""
import logging
import os
from fastapi import APIRouter, Depends, HTTPException, Header, Request

# Configure logging with handler for stdout
logger = logging.getLogger("image_api")
logger.setLevel(logging.INFO)
logger.propagate = False
if not logger.handlers:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter('%(asctime)s [IMAGE-API] %(message)s', datefmt='%Y-%m-%d %H:%M:%S'))
    logger.addHandler(handler)
from pydantic import BaseModel
from typing import Optional
from sqlalchemy.orm import Session

from app.database import get_db
from app.auth import get_current_user_optional
from app.services.image_factory import get_image_backend, prepare_vram_for_image
from app.services.locks import image_generation_lock

router = APIRouter(prefix="/api", tags=["image"])

# API key for external integrations (set via environment or admin settings)
IMAGE_API_KEY = os.getenv("IMAGE_API_KEY", "")


async def get_image_auth(
    request: Request,
    authorization: Optional[str] = Header(None),
    x_api_key: Optional[str] = Header(None),
    db: Session = Depends(get_db)
) -> bool:
    """
    Authenticate for image API - supports JWT token or API key.
    Returns True if authenticated, raises HTTPException otherwise.
    """
    # Check API key first (for external integrations like Sharkey)
    if x_api_key and IMAGE_API_KEY and x_api_key == IMAGE_API_KEY:
        return True

    # Check for API key in Authorization header (Bearer format)
    if authorization and authorization.startswith("Bearer "):
        token = authorization[7:]
        if IMAGE_API_KEY and token == IMAGE_API_KEY:
            return True

    # Try JWT auth (for logged-in users)
    try:
        user = get_current_user_optional(request, db)  # Sync function, no await
        if user:
            return True
    except Exception:
        pass

    # Allow if no API key is configured (open access mode)
    if not IMAGE_API_KEY:
        return True

    raise HTTPException(status_code=401, detail="Not authenticated")


class ImageGenRequest(BaseModel):
    prompt: str
    negative_prompt: Optional[str] = ""
    width: Optional[int] = None
    height: Optional[int] = None
    steps: Optional[int] = None
    cfg: Optional[float] = None


class ImageResponse(BaseModel):
    image: Optional[str] = None  # base64 encoded result
    error: Optional[str] = None


@router.post("/generate-image", response_model=ImageResponse)
async def generate_image(
    request: ImageGenRequest,
    http_request: Request,
    db: Session = Depends(get_db),
    _auth: bool = Depends(get_image_auth)
):
    """
    Generate an image from a text prompt.
    Returns base64 encoded image.
    Supports JWT auth or API key auth (X-API-Key header or Bearer token).
    Uses a lock to ensure sequential processing (one image at a time).
    """
    async with image_generation_lock:
        try:
            logger.info(f"[IMAGE-API] Generating image: {request.prompt[:50]}...")
            # Prepare VRAM for image generation
            prepare_vram_for_image(db)

            # Get image backend (native or comfyui)
            backend = get_image_backend(db)

            # Generate image
            result = await backend.generate_image(
                prompt=request.prompt,
                negative_prompt=request.negative_prompt or "",
                width=request.width,
                height=request.height,
                steps=request.steps,
                cfg=request.cfg
            )

            if result:
                logger.info(f"[IMAGE-API] Image generated successfully")
                return ImageResponse(image=result)
            else:
                logger.info(f"[IMAGE-API] Image generation failed (no result)")
                return ImageResponse(error="Image generation failed")

        except Exception as e:
            logger.info(f"[IMAGE-API] Image generation error: {e}")
            return ImageResponse(error=str(e))
