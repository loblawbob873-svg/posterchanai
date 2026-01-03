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
    face_swap: Optional[bool] = False  # Swap original face onto result
    auto_identity: Optional[bool] = False  # Auto-detect identity tags from image


class InpaintRequest(BaseModel):
    prompt: str
    image: str  # base64 encoded source image
    mask: str  # base64 encoded mask (white=inpaint, black=keep)
    denoise: Optional[float] = 0.85
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

            # Build final prompt with identity tags if requested
            final_prompt = request.prompt
            if request.auto_identity:
                try:
                    from app.services.wd14_tagger import tag_image_bytes
                    tags = await tag_image_bytes(image_bytes)
                    if tags:
                        tags_lower = tags.lower()
                        identity_parts = []
                        if 'dark_skin' in tags_lower or 'dark-skinned' in tags_lower:
                            identity_parts.append('dark brown skin')
                        if any(t in tags_lower for t in ['fat', 'chubby', 'plump', 'overweight']):
                            identity_parts.append('fat, obese, bbw, plus-size body')
                        if any(t in tags_lower for t in ['large_breasts', 'huge_breasts']):
                            identity_parts.append('large breasts')
                        if identity_parts:
                            identity_str = ", ".join(identity_parts)
                            final_prompt = f"{request.prompt}, {identity_str}"
                            print(f"[IMAGE-API] Added identity tags: {identity_str}")
                except Exception as e:
                    print(f"[IMAGE-API] Identity detection error: {e}")

            # Generate img2img
            result = await backend.generate_img2img(
                prompt=final_prompt,
                image_bytes=image_bytes,
                denoise=request.denoise or 0.75,
                negative_prompt=request.negative_prompt
            )

            if result:
                # Face swap if requested
                if request.face_swap:
                    try:
                        from app.services.face_swap_service import swap_face_bytes
                        result_bytes = base64.b64decode(result)
                        swapped = swap_face_bytes(image_bytes, result_bytes)
                        if swapped:
                            result = base64.b64encode(swapped).decode('utf-8')
                            print(f"[IMAGE-API] img2img with face swap completed")
                        else:
                            print(f"[IMAGE-API] img2img completed (face swap failed)")
                    except Exception as e:
                        print(f"[IMAGE-API] Face swap error: {e}")
                else:
                    print(f"[IMAGE-API] img2img completed successfully")
                return ImageResponse(image=result)
            else:
                print(f"[IMAGE-API] img2img failed (no result)")
                return ImageResponse(error="Img2img generation failed")

        except Exception as e:
            print(f"[IMAGE-API] img2img error: {e}")
            return ImageResponse(error=str(e))


@router.post("/inpaint", response_model=ImageResponse)
async def inpaint(
    request: InpaintRequest,
    http_request: Request,
    db: Session = Depends(get_db),
    _auth: bool = Depends(get_image_auth)
):
    """
    Inpaint masked areas of an image.
    Mask should be white (255) where to inpaint, black (0) where to keep original.
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

            # Decode mask
            try:
                mask_bytes = base64.b64decode(request.mask)
            except Exception:
                return ImageResponse(error="Invalid base64 mask data")

            print(f"[IMAGE-API] inpaint: {request.prompt[:50]}... (denoise={request.denoise})")
            # Prepare VRAM for image generation
            prepare_vram_for_image(db)

            # Get image backend
            backend = get_image_backend(db)

            # Check if backend supports inpainting
            if not hasattr(backend, 'generate_inpaint'):
                return ImageResponse(error="Inpainting not supported by current backend")

            # Generate inpaint
            result = await backend.generate_inpaint(
                prompt=request.prompt,
                image_bytes=image_bytes,
                mask_bytes=mask_bytes,
                denoise=request.denoise or 0.85,
                negative_prompt=request.negative_prompt
            )

            if result:
                print(f"[IMAGE-API] inpaint completed successfully")
                return ImageResponse(image=result)
            else:
                print(f"[IMAGE-API] inpaint failed (no result)")
                return ImageResponse(error="Inpaint generation failed")

        except Exception as e:
            print(f"[IMAGE-API] inpaint error: {e}")
            return ImageResponse(error=str(e))


class AutoInpaintRequest(BaseModel):
    prompt: str  # What to generate in masked area (e.g., "nude, skin, nipples")
    image: str  # base64 encoded source image
    denoise: Optional[float] = 0.85
    negative_prompt: Optional[str] = ""


class MaskResponse(BaseModel):
    mask: Optional[str] = None  # base64 encoded mask
    error: Optional[str] = None


@router.post("/auto-inpaint", response_model=ImageResponse)
async def auto_inpaint(
    request: AutoInpaintRequest,
    http_request: Request,
    db: Session = Depends(get_db),
    _auth: bool = Depends(get_image_auth)
):
    """
    Auto-generate mask and inpaint in one step.
    Detects mask type from prompt (nude -> body mask, background -> bg mask).
    Returns base64 encoded image.
    """
    async with _image_generation_lock:
        try:
            # Decode source image
            try:
                image_bytes = base64.b64decode(request.image)
            except Exception:
                return ImageResponse(error="Invalid base64 image data")

            print(f"[IMAGE-API] auto-inpaint: {request.prompt[:50]}...")

            # Get tags for better mask generation
            from app.services.wd14_service import tag_image as wd14_tag
            tags = wd14_tag(image_bytes, threshold=0.35)
            print(f"[IMAGE-API] Tags for mask: {tags[:80] if tags else 'None'}...")

            # Auto-generate mask
            from app.services.mask_service import auto_generate_mask
            mask_bytes = auto_generate_mask(image_bytes, request.prompt, tags)

            if not mask_bytes:
                return ImageResponse(error="Could not auto-generate mask for this prompt. Use /inpaint with manual mask.")

            # Prepare VRAM for image generation
            prepare_vram_for_image(db)

            # Get image backend
            backend = get_image_backend(db)

            if not hasattr(backend, 'generate_inpaint'):
                return ImageResponse(error="Inpainting not supported by current backend")

            # Generate inpaint
            result = await backend.generate_inpaint(
                prompt=request.prompt,
                image_bytes=image_bytes,
                mask_bytes=mask_bytes,
                denoise=request.denoise or 0.85,
                negative_prompt=request.negative_prompt
            )

            if result:
                print(f"[IMAGE-API] auto-inpaint completed successfully")
                return ImageResponse(image=result)
            else:
                print(f"[IMAGE-API] auto-inpaint failed (no result)")
                return ImageResponse(error="Auto-inpaint generation failed")

        except Exception as e:
            print(f"[IMAGE-API] auto-inpaint error: {e}")
            return ImageResponse(error=str(e))


@router.post("/generate-mask", response_model=MaskResponse)
async def generate_mask(
    request: AutoInpaintRequest,
    http_request: Request,
    db: Session = Depends(get_db),
    _auth: bool = Depends(get_image_auth)
):
    """
    Generate a mask for an image based on prompt.
    Useful for previewing/adjusting masks before inpainting.
    Returns base64 encoded mask (white=inpaint, black=keep).
    """
    try:
        # Decode source image
        try:
            image_bytes = base64.b64decode(request.image)
        except Exception:
            return MaskResponse(error="Invalid base64 image data")

        print(f"[IMAGE-API] generate-mask: {request.prompt[:50]}...")

        # Get tags for better mask generation
        from app.services.wd14_service import tag_image as wd14_tag
        tags = wd14_tag(image_bytes, threshold=0.35)

        # Auto-generate mask
        from app.services.mask_service import auto_generate_mask
        mask_bytes = auto_generate_mask(image_bytes, request.prompt, tags)

        if mask_bytes:
            return MaskResponse(mask=base64.b64encode(mask_bytes).decode())
        else:
            return MaskResponse(error="Could not generate mask for this prompt type")

    except Exception as e:
        print(f"[IMAGE-API] generate-mask error: {e}")
        return MaskResponse(error=str(e))


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
