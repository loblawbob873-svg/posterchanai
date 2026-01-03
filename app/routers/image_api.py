"""
Direct Image Generation API for external integrations.
Provides simple REST endpoints for txt2img and img2img.
Supports both JWT auth and API key auth for external services.
Sequential processing: Only one image is generated at a time to prevent GPU overload.
"""
import base64
import logging
import os
from fastapi import APIRouter, Depends, HTTPException, Header, Request

logger = logging.getLogger(__name__)
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


class Img2ImgRequest(BaseModel):
    prompt: str
    image: str  # base64 encoded image
    denoise: Optional[float] = 0.50
    negative_prompt: Optional[str] = ""
    auto_identity: Optional[bool] = True  # Auto-detect identity tags from image


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
    async with image_generation_lock:
        try:
            # Decode source image
            try:
                image_bytes = base64.b64decode(request.image)
            except Exception:
                return ImageResponse(error="Invalid base64 image data")

            logger.info(f"[IMAGE-API] img2img: {request.prompt[:50]}... (denoise={request.denoise})")
            # Prepare VRAM for image generation
            prepare_vram_for_image(db)

            # Get image backend
            backend = get_image_backend(db)

            # Build final prompt with identity tags if requested
            final_prompt = request.prompt
            negative_parts = []
            if request.auto_identity:
                try:
                    from app.services.wd14_service import tag_image
                    tags = tag_image(image_bytes)
                    if tags:
                        tags_lower = tags.lower()
                        identity_parts = []
                        # Hair color (simplified to save tokens)
                        if 'orange_hair' in tags_lower:
                            identity_parts.append('orange hair, ginger')
                            negative_parts.append('blonde hair')
                        elif 'blonde' in tags_lower or 'yellow_hair' in tags_lower:
                            identity_parts.append('blonde hair')
                            negative_parts.append('dark hair')
                        elif 'brown_hair' in tags_lower:
                            identity_parts.append('brown hair')
                        elif 'black_hair' in tags_lower:
                            identity_parts.append('black hair')
                        elif 'red_hair' in tags_lower or 'redhead' in tags_lower:
                            identity_parts.append('red hair')
                        elif 'pink_hair' in tags_lower:
                            identity_parts.append('pink hair')
                        elif 'blue_hair' in tags_lower:
                            identity_parts.append('blue hair')
                        elif 'green_hair' in tags_lower:
                            identity_parts.append('green hair')
                        elif 'purple_hair' in tags_lower:
                            identity_parts.append('purple hair')
                        elif 'white_hair' in tags_lower or 'silver_hair' in tags_lower:
                            identity_parts.append('white hair')

                        # Skin tone - be smart about it!
                        # Light-colored hair (blonde, orange, red, pink, white) almost always = pale skin
                        # Only trust dark_skin tag if hair is dark (black, brown) or no hair detected
                        has_light_hair = any(h in tags_lower for h in ['blonde', 'yellow_hair', 'orange_hair', 'red_hair', 'redhead', 'pink_hair', 'white_hair', 'silver_hair'])
                        has_dark_hair = any(h in tags_lower for h in ['black_hair', 'brown_hair'])
                        has_dark_skin_tag = 'dark_skin' in tags_lower or 'dark-skinned' in tags_lower

                        if has_light_hair:
                            # Light hair = force pale skin (simplified to save tokens)
                            identity_parts.append('pale skin, white woman')
                            negative_parts.append('dark skin, black woman')
                            logger.info(f"[IMAGE-API] Light hair detected - forcing pale skin")
                        elif has_dark_skin_tag and has_dark_hair:
                            identity_parts.append('dark skin')
                            negative_parts.append('pale skin')
                        elif 'pale' in tags_lower or 'light_skin' in tags_lower:
                            identity_parts.append('pale skin')
                            negative_parts.append('dark skin')
                        else:
                            identity_parts.append('natural skin')
                            negative_parts.append('dark skin')
                        # Body type
                        if any(t in tags_lower for t in ['fat', 'chubby', 'plump', 'overweight']):
                            identity_parts.append('(fat:2.0), (bbw:1.5), (plus-size body:1.5)')
                        if any(t in tags_lower for t in ['large_breasts', 'huge_breasts']):
                            identity_parts.append('(large breasts:2.0)')
                        if identity_parts:
                            identity_str = ", ".join(identity_parts)
                            final_prompt = f"{request.prompt}, {identity_str}"
                            logger.info(f"[IMAGE-API] Added identity tags: {identity_str}")
                        if negative_parts:
                            neg_str = ", ".join(negative_parts)
                            logger.info(f"[IMAGE-API] Added negative tags: {neg_str}")
                        logger.info(f"[IMAGE-API] WD14 tags: {tags[:100]}...")
                except Exception as e:
                    logger.info(f"[IMAGE-API] Identity detection error: {e}")

            # Build final negative prompt
            final_negative = request.negative_prompt or ""
            if negative_parts:
                neg_identity = ", ".join(negative_parts)
                final_negative = f"{final_negative}, {neg_identity}" if final_negative else neg_identity

            # Detect if anime
            is_anime = 'anime' in final_prompt.lower() or 'manga' in final_prompt.lower() or 'illustration' in final_prompt.lower()
            # Check if nude/nsfw request
            is_nsfw = any(kw in final_prompt.lower() for kw in ['nude', 'naked', 'topless', 'nsfw'])
            # Optimal denoise: anime 0.70 (balance nude vs face), realistic 0.50 (best preservation)
            if is_anime:
                denoise_value = min(request.denoise or 0.70, 0.70)
            else:
                # Use 0.50 for realistic - best face/pose/background preservation
                denoise_value = min(request.denoise or 0.50, 0.50)

            # For NSFW, add strong clothing removal and background cleanup negatives
            if is_nsfw:
                clothing_neg = "clothing, clothes, dress, skirt, shirt, bra, panties, underwear, lingerie, fabric, covered"
                background_neg = "multiple people, crowd, group, extra faces, background people"
                final_negative = f"{final_negative}, {clothing_neg}, {background_neg}" if final_negative else f"{clothing_neg}, {background_neg}"
                logger.info(f"[IMAGE-API] NSFW mode - added clothing/background negatives")

            logger.info(f"[IMAGE-API] {'Anime' if is_anime else 'Realistic'} mode, denoise: {denoise_value}")

            # Generate img2img (inpainting disabled - causes more problems than it solves)
            result = await backend.generate_img2img(
                    prompt=final_prompt,
                    image_bytes=image_bytes,
                    denoise=denoise_value,
                    negative_prompt=final_negative
                )

            if result:
                logger.info(f"[IMAGE-API] img2img completed")
                return ImageResponse(image=result)
            else:
                logger.info(f"[IMAGE-API] img2img failed (no result)")
                return ImageResponse(error="Img2img generation failed")

        except Exception as e:
            logger.info(f"[IMAGE-API] img2img error: {e}")
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
    async with image_generation_lock:
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

            logger.info(f"[IMAGE-API] inpaint: {request.prompt[:50]}... (denoise={request.denoise})")
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
                logger.info(f"[IMAGE-API] inpaint completed successfully")
                return ImageResponse(image=result)
            else:
                logger.info(f"[IMAGE-API] inpaint failed (no result)")
                return ImageResponse(error="Inpaint generation failed")

        except Exception as e:
            logger.info(f"[IMAGE-API] inpaint error: {e}")
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
    async with image_generation_lock:
        try:
            # Decode source image
            try:
                image_bytes = base64.b64decode(request.image)
            except Exception:
                return ImageResponse(error="Invalid base64 image data")

            logger.info(f"[IMAGE-API] auto-inpaint: {request.prompt[:50]}...")

            # Get tags for better mask generation
            from app.services.wd14_service import tag_image as wd14_tag
            tags = wd14_tag(image_bytes, threshold=0.35)
            logger.info(f"[IMAGE-API] Tags for mask: {tags[:80] if tags else 'None'}...")

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
                logger.info(f"[IMAGE-API] auto-inpaint completed successfully")
                return ImageResponse(image=result)
            else:
                logger.info(f"[IMAGE-API] auto-inpaint failed (no result)")
                return ImageResponse(error="Auto-inpaint generation failed")

        except Exception as e:
            logger.info(f"[IMAGE-API] auto-inpaint error: {e}")
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

        logger.info(f"[IMAGE-API] generate-mask: {request.prompt[:50]}...")

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
        logger.info(f"[IMAGE-API] generate-mask error: {e}")
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

        logger.info(f"[IMAGE-API] Tagging image ({len(image_bytes)} bytes)...")

        # Import WD14 service
        from app.services.wd14_service import tag_image as wd14_tag

        # Tag the image
        tags = wd14_tag(image_bytes, threshold=request.threshold or 0.35)

        if tags:
            logger.info(f"[IMAGE-API] Tags: {tags[:100]}...")
            return TagImageResponse(tags=tags)
        else:
            return TagImageResponse(error="Failed to tag image")

    except Exception as e:
        logger.info(f"[IMAGE-API] Tagging error: {e}")
        return TagImageResponse(error=str(e))
