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
from app.services.image_factory import generate_image_with_load_balancing
# Lock moved to image_factory.py for fine-grained control (local only)

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
    Supports load balancing across multiple posterchanai servers.
    Remote requests run in parallel; local requests are serialized.
    """
    try:
        logger.info(f"[IMAGE-API] Generating image: {request.prompt[:50]}...")

        # Check if this is a server-to-server request (no user, just API key)
        # In that case, we should use LOCAL generation only, not load balancing
        # to avoid loops
        from app.services.image_load_balancer import parse_image_server_urls
        settings = {s.key: s.value for s in db.query(Setting).all()}
        image_server_urls = settings.get("image_server_urls", "")
        servers = parse_image_server_urls(image_server_urls)
        
        # If this is an API request (not from a logged-in user), force local generation
        # to prevent proxy loops
        user = None
        try:
            from app.auth import get_current_user_optional
            user = get_current_user_optional(http_request, db)
        except:
            pass
        
        # If no user and we have remote servers configured, this might be a proxy request
        # Force local generation to avoid loops
        if not user and servers:
            logger.info(f"[IMAGE-API] API request detected (no user) - forcing LOCAL generation to avoid proxy loop")
            # Use local backend directly, bypassing load balancing
            from app.services.image_factory import get_image_backend, prepare_vram_for_image
            from app.services.locks import GPUResourceLock, image_generation_lock
            
            result = None
            try:
                async with GPUResourceLock("Image", f"prompt={request.prompt[:30]}..."):
                    async with image_generation_lock:
                        prepare_vram_for_image(db)
                        backend = get_image_backend(db)
                        result = await backend.generate_image(
                            prompt=request.prompt,
                            negative_prompt=request.negative_prompt or "",
                            width=request.width,
                            height=request.height,
                            steps=request.steps,
                            cfg=request.cfg,
                        )
            except Exception as e:
                logger.error(f"[IMAGE-API] Local image generation failed: {e}", exc_info=True)
                result = None
        else:
            # Generate image with load balancing support (for user requests)
            # Lock is handled inside for local generation only
            result = await generate_image_with_load_balancing(
                db=db,
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
            logger.error(f"[IMAGE-API] Image generation failed (no result) - check backend configuration and logs")
            return ImageResponse(error="Image generation failed")

    except Exception as e:
        logger.error(f"[IMAGE-API] Image generation error: {e}", exc_info=True)
        return ImageResponse(error=str(e))
