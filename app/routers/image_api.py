"""
Direct Image Generation API for external integrations.
Provides simple REST endpoints for txt2img and img2img.
Supports both JWT auth and API key auth for external services.
Sequential processing: Only one image is generated at a time to prevent GPU overload.
"""
import asyncio
import base64
import os
from fastapi import APIRouter, Depends, HTTPException, Header, Request
from pydantic import BaseModel
from typing import Optional
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import User
from app.auth import get_current_user_optional
from app.services.image_factory import get_image_backend, prepare_vram_for_image

router = APIRouter(prefix="/api", tags=["image"])

# API key for external integrations (set via environment or admin settings)
IMAGE_API_KEY = os.getenv("IMAGE_API_KEY", "")

# Lock to ensure only one image is generated at a time
_image_generation_lock = asyncio.Lock()


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
    from app.auth import get_current_user_optional
    try:
        user = await get_current_user_optional(request, db)
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


class Img2ImgRequest(BaseModel):
    prompt: str
    image: str  # base64 encoded image
    denoise: Optional[float] = 0.75
    negative_prompt: Optional[str] = ""


class ImageResponse(BaseModel):
    image: Optional[str] = None  # base64 encoded result
    error: Optional[str] = None


class TagImageRequest(BaseModel):
    image: str  # base64 encoded image
    threshold: Optional[float] = 0.35


class TagImageResponse(BaseModel):
    tags: Optional[str] = None  # comma-separated tags
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
    async with _image_generation_lock:
        try:
            print(f"[IMAGE-API] Generating image: {request.prompt[:50]}...")
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
                print(f"[IMAGE-API] Image generated successfully")
                return ImageResponse(image=result)
            else:
                print(f"[IMAGE-API] Image generation failed (no result)")
                return ImageResponse(error="Image generation failed")

        except Exception as e:
            print(f"[IMAGE-API] Image generation error: {e}")
            return ImageResponse(error=str(e))


@router.post("/img2img", response_model=ImageResponse)
async def img2img(
    request: Img2ImgRequest,
    http_request: Request,
    db: Session = Depends(get_db),
    _auth: bool = Depends(get_image_auth)
):
    """
    Generate an image from a source image and prompt.
    Returns base64 encoded image.
    Supports JWT auth or API key auth (X-API-Key header or Bearer token).
    Uses a lock to ensure sequential processing (one image at a time).
    """
    async with _image_generation_lock:
        try:
            # Decode source image
            try:
                image_bytes = base64.b64decode(request.image)
            except Exception:
                return ImageResponse(error="Invalid base64 image data")

            print(f"[IMAGE-API] img2img: {request.prompt[:50]}... (denoise={request.denoise})")
            # Prepare VRAM for image generation
            prepare_vram_for_image(db)

            # Get image backend
            backend = get_image_backend(db)

            # Generate img2img
            result = await backend.generate_img2img(
                prompt=request.prompt,
                image_bytes=image_bytes,
                denoise=request.denoise or 0.75,
                negative_prompt=request.negative_prompt
            )

            if result:
                print(f"[IMAGE-API] img2img completed successfully")
                return ImageResponse(image=result)
            else:
                print(f"[IMAGE-API] img2img failed (no result)")
                return ImageResponse(error="Img2img generation failed")

        except Exception as e:
            print(f"[IMAGE-API] img2img error: {e}")
            return ImageResponse(error=str(e))


@router.post("/tag-image", response_model=TagImageResponse)
async def tag_image(
    request: TagImageRequest,
    http_request: Request,
    db: Session = Depends(get_db),
    _auth: bool = Depends(get_image_auth)
):
    """
    Tag an image using WD14 tagger.
    Returns comma-separated tags describing the image content.
    """
    try:
        # Decode image
        try:
            image_bytes = base64.b64decode(request.image)
        except Exception:
            return TagImageResponse(error="Invalid base64 image data")

        print(f"[IMAGE-API] Tagging image ({len(image_bytes)} bytes)...")

        # Import WD14 service
        from app.services.wd14_service import tag_image as wd14_tag

        # Tag the image
        tags = wd14_tag(image_bytes, threshold=request.threshold or 0.35)

        if tags:
            print(f"[IMAGE-API] Tags: {tags[:100]}...")
            return TagImageResponse(tags=tags)
        else:
            return TagImageResponse(error="Failed to tag image")

    except Exception as e:
        print(f"[IMAGE-API] Tagging error: {e}")
        return TagImageResponse(error=str(e))
