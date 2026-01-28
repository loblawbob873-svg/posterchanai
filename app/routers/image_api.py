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
from app.models import Setting
from sqlalchemy.orm import Session
from app.services.image_factory import generate_image_with_load_balancing
# Lock moved to image_factory.py for fine-grained control (local only)

router = APIRouter(prefix="/api", tags=["image"])

async def get_image_auth(
    request: Request,
    authorization: Optional[str] = Header(None),
    x_api_key: Optional[str] = Header(None),
    db: Session = Depends(get_db)
) -> bool:
    """
    Authenticate for image API - supports JWT token or User API Key.
    Load-balanced requests (from other posterchanai nodes) are allowed without authentication.
    Returns True if authenticated, raises HTTPException otherwise.
    """
    # Allow load-balanced requests from other posterchanai nodes without authentication
    load_balanced_header = request.headers.get("x-posterchanai-load-balanced", "").lower()
    if load_balanced_header == "true":
        logger.debug(f"[IMAGE-API] ✓ Load-balanced request from another posterchanai node - allowing without auth")
        return True
    
    # Check API key first (for external integrations and user API keys)
    if x_api_key:
        x_api_key = str(x_api_key).strip()  # Trim whitespace
        # Check user API keys from api_keys table
        try:
            from app.utils.auth_utils import query_api_key_with_retry, get_user_from_api_key
            api_key, user_id = query_api_key_with_retry(db, x_api_key)
            if api_key and user_id:
                user = get_user_from_api_key(db, user_id)
                if user:
                    logger.debug(f"[IMAGE-API] ✓ Authenticated via X-API-Key header (User API Key: {user.username})")
                    return True
        except Exception as e:
            logger.debug(f"[IMAGE-API] Error checking user API key: {e}")
            pass

    # Check for API key in Authorization header (Bearer format)
    if authorization and authorization.startswith("Bearer "):
        token = authorization[7:]
        # Check user API keys from api_keys table
        try:
            from app.utils.auth_utils import query_api_key_with_retry, get_user_from_api_key
            api_key, user_id = query_api_key_with_retry(db, token)
            if api_key and user_id:
                user = get_user_from_api_key(db, user_id)
                if user:
                    logger.debug(f"[IMAGE-API] Authenticated via Bearer token (User API Key: {user.username})")
                    return True
        except Exception as e:
            logger.debug(f"[IMAGE-API] Error checking user API key: {e}")
            pass

    # Try JWT auth (for logged-in users)
    try:
        user = get_current_user_optional(request, db)  # Sync function, no await
        if user:
            logger.debug(f"[IMAGE-API] Authenticated via JWT (user: {user.username})")
            return True
    except Exception:
        pass

    # Allow unauthenticated access (for load-balanced requests or open access)
    logger.debug(f"[IMAGE-API] Allowing unauthenticated access")
    return True


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

        # Generate image with load balancing support
        # The load balancer will detect if this server is selected and generate locally
        # Lock is handled inside for local generation only
        # Note: Server-to-server requests use global API key from settings, not user's API key
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
            logger.error(f"[IMAGE-API] Image generation failed (no result)")
            # Check if it's a load balancing issue
            settings = {s.key: s.value for s in db.query(Setting).all()}
            image_server_urls = settings.get("image_server_urls", "")
            vram_mode = settings.get("vram_mode", "shared")
            
            if vram_mode == "llm_only" and not image_server_urls:
                return ImageResponse(error="Image generation failed: vram_mode is 'llm_only' but no image servers configured")
            elif image_server_urls:
                return ImageResponse(error="Image generation failed: All image servers failed or unavailable")
            else:
                return ImageResponse(error="Image generation failed: Local generation failed and no remote servers configured")

    except Exception as e:
        logger.error(f"[IMAGE-API] Image generation error: {e}", exc_info=True)
        error_msg = str(e)
        # Provide more specific error messages
        if "NoHealthyImageServersError" in error_msg or "No healthy image servers" in error_msg:
            return ImageResponse(error=f"Image generation failed: All remote image servers are unavailable. {error_msg}")
        return ImageResponse(error=f"Image generation error: {error_msg}")
