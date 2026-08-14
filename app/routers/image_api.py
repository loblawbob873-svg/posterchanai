"""
Direct Image Generation API for external integrations.
Provides simple REST endpoints for txt2img.
Supports both JWT auth and API key auth for external services.
Sequential processing: Only one image is generated at a time to prevent GPU overload.
"""
import logging
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
from app.utils import lb_auth
from app.auth import get_current_user_optional
from app.services import settings_store
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
    # A peer node, proven by the shared secret once `lb_shared_secret` is set. The bare header
    # alone is settable by any caller — see app/utils/lb_auth.py.
    if lb_auth.is_internal(request):
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
        logger.info(f"[IMAGE-API] Generating image ({len(request.prompt or '')} chars)")

        # Server-to-server (load-balanced) requests must generate LOCALLY here, not re-balance —
        # the unified chat_server_urls list is on every node, so re-balancing would ping-pong the
        # request node→node until timeout. Mirrors chat's skip_load_balancer / music+video local_only.
        is_load_balanced = http_request.headers.get("x-posterchanai-load-balanced", "").lower() == "true"

        # Generate image with load balancing support
        # Lock is handled inside for local generation only
        # Note: Server-to-server requests use global API key from settings, not user's API key
        result = await generate_image_with_load_balancing(
            db=db,
            prompt=request.prompt,
            negative_prompt=request.negative_prompt or "",
            width=request.width,
            height=request.height,
            steps=request.steps,
            cfg=request.cfg,
            local_only=is_load_balanced
        )

        if result:
            logger.info(f"[IMAGE-API] Image generated successfully")
            return ImageResponse(image=result)
        else:
            logger.error(f"[IMAGE-API] Image generation failed (no result)")
            # Check if it's a load balancing issue
            settings = settings_store.all_settings()
            server_urls = settings.get("chat_server_urls", "")
            vram_mode = settings.get("vram_mode", "shared")

            if vram_mode == "llm_only" and not server_urls:
                return ImageResponse(error="Image generation failed: vram_mode is 'llm_only' but no servers configured in Site → Load Balancing")
            elif server_urls:
                return ImageResponse(error="Image generation failed: All servers failed or unavailable")
            else:
                return ImageResponse(error="Image generation failed: Local generation failed and no remote servers configured")

    except Exception as e:
        logger.error(f"[IMAGE-API] Image generation error: {e}", exc_info=True)
        error_msg = str(e)
        # Provide more specific error messages
        if "NoHealthyImageServersError" in error_msg or "No healthy image servers" in error_msg:
            return ImageResponse(error=f"Image generation failed: All remote image servers are unavailable. {error_msg}")
        return ImageResponse(error=f"Image generation error: {error_msg}")
