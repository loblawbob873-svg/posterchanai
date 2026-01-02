"""
Direct Image Generation API for external integrations.
Provides simple REST endpoints for txt2img and img2img.
"""
import base64
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from typing import Optional
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import User
from app.auth import get_current_user
from app.services.image_factory import get_image_backend, prepare_vram_for_image

router = APIRouter(prefix="/api", tags=["image"])


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


@router.post("/generate-image", response_model=ImageResponse)
async def generate_image(
    request: ImageGenRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    Generate an image from a text prompt.
    Returns base64 encoded image.
    """
    try:
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
            return ImageResponse(image=result)
        else:
            return ImageResponse(error="Image generation failed")

    except Exception as e:
        return ImageResponse(error=str(e))


@router.post("/img2img", response_model=ImageResponse)
async def img2img(
    request: Img2ImgRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    Generate an image from a source image and prompt.
    Returns base64 encoded image.
    """
    try:
        # Decode source image
        try:
            image_bytes = base64.b64decode(request.image)
        except Exception:
            return ImageResponse(error="Invalid base64 image data")

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
            return ImageResponse(image=result)
        else:
            return ImageResponse(error="Img2img generation failed")

    except Exception as e:
        return ImageResponse(error=str(e))
