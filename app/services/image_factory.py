"""
Image Generation Factory
Selects between native diffusers and ComfyUI backends based on settings.
Integrates with VRAM manager for model swapping on shared GPU.
Supports user-specific custom ComfyUI endpoints.
Supports load balancing across multiple posterchanai servers.
"""
import logging
from typing import Optional, Protocol, runtime_checkable, TYPE_CHECKING
from sqlalchemy.orm import Session

from app.models import Setting
from app.services.image_load_balancer import (
    ImageLoadBalancer,
    parse_image_server_urls,
    should_use_remote_image,
)

if TYPE_CHECKING:
    from app.models import User

logger = logging.getLogger("image_factory")


@runtime_checkable
class ImageBackend(Protocol):
    """Protocol for image generation backends"""

    async def generate_image(self, prompt: str, **kwargs) -> Optional[str]:
        """Generate image from text prompt, returns base64"""
        ...


def prepare_vram_for_image(db: Session):
    """Prepare VRAM for image generation (swap models if needed)"""
    from app.services.vram_manager import prepare_for_image
    prepare_for_image(db)


def get_image_load_balancer(db: Session) -> Optional[ImageLoadBalancer]:
    """
    Get the image load balancer if configured.
    Returns None if no remote servers are configured.
    """
    settings = {s.key: s.value for s in db.query(Setting).all()}
    image_server_urls = settings.get("image_server_urls", "")
    servers = parse_image_server_urls(image_server_urls)

    if servers:
        timeout = int(settings.get("comfyui_timeout", "300000")) / 1000
        logger.debug(f"Image load balancer available with {len(servers)} server(s)")
        return ImageLoadBalancer(servers, timeout=timeout)
    return None


async def generate_image_with_load_balancing(
    db: Session,
    prompt: str,
    negative_prompt: str = "",
    width: Optional[int] = None,
    height: Optional[int] = None,
    steps: Optional[int] = None,
    cfg: Optional[float] = None,
) -> Optional[str]:
    """
    Generate image with load balancing support.
    Alternates between local and remote servers if load balancing is configured.
    Remote requests run in parallel; local requests are serialized via lock.
    If vram_mode is 'llm_only', always uses remote servers.
    Returns base64 encoded image or None.
    """
    from app.services.locks import image_generation_lock

    settings = {s.key: s.value for s in db.query(Setting).all()}
    image_server_urls = settings.get("image_server_urls", "")
    vram_mode = settings.get("vram_mode", "shared")
    servers = parse_image_server_urls(image_server_urls)

    # If vram_mode is llm_only, always use remote servers (no local image generation)
    force_remote = vram_mode == "llm_only"

    # If remote image servers are configured, use load balancing
    if servers:
        from app.services.image_load_balancer import get_healthy_image_server
        from app.services.load_balancer import is_self_url
        
        timeout = int(settings.get("comfyui_timeout", "300000")) / 1000
        selected_server = await get_healthy_image_server(servers)
        
        if selected_server:
            # Always make HTTP request to selected server (even if it's "self")
            # This ensures proper load balancing and avoids self-detection issues
            logger.info(f"Image load balancer: selected {selected_server} -> HTTP request")
            # Make direct HTTP request instead of using ImageLoadBalancer to avoid creating a new cycle
            timeout = int(settings.get("comfyui_timeout", "300000")) / 1000
            import httpx
            payload = {
                "prompt": prompt,
                "negative_prompt": negative_prompt,
            }
            if width is not None:
                payload["width"] = width
            if height is not None:
                payload["height"] = height
            if steps is not None:
                payload["steps"] = steps
            if cfg is not None:
                payload["cfg"] = cfg
            
            try:
                # Get authentication token for server-to-server communication
                headers = {}
                # Use Global API Key (openai_api_key) for server-to-server image generation
                # This is the same key used for OpenAI API access
                global_api_key = settings.get("openai_api_key", "")
                if global_api_key:
                    global_api_key = str(global_api_key).strip()  # Ensure it's a string and trim whitespace
                
                # Fallback to storage_server_token if Global API Key is not set
                # This allows using the same token for both storage and image server communication
                if not global_api_key:
                    global_api_key = settings.get("storage_server_token", "")
                    if global_api_key:
                        global_api_key = str(global_api_key).strip()
                        logger.info(f"Using storage_server_token for image server authentication to {selected_server}")
                
                if global_api_key:
                    headers["X-API-Key"] = global_api_key
                    logger.info(f"[IMAGE] Using Global API Key for {selected_server} (length: {len(global_api_key)})")
                else:
                    # No API key configured - endpoint should allow unauthenticated if no API key is set
                    logger.warning(f"[IMAGE] No API key configured - using unauthenticated request to {selected_server} (may fail if remote requires auth)")
                
                async with httpx.AsyncClient(timeout=timeout) as client:
                    logger.info(f"Sending image generation request to {selected_server} with prompt: {prompt[:50]}...")
                    response = await client.post(
                        f"{selected_server}/api/generate-image",
                        json=payload,
                        headers=headers
                    )
                    
                    # Log response status for debugging
                    logger.info(f"Response from {selected_server}: status={response.status_code}")
                    
                    if response.status_code == 401:
                        logger.error(f"IMAGE ERROR from {selected_server} | Authentication failed - check Global API Key (openai_api_key) setting")
                        return None
                    
                    response.raise_for_status()
                    result = response.json()
                    
                    if result.get("error"):
                        logger.error(f"IMAGE ERROR from {selected_server} | error={result['error']}")
                        logger.error(f"Full error response: {result}")
                        return None
                    
                    image_data = result.get("image")
                    if image_data:
                        logger.info(f"IMAGE COMPLETE from {selected_server} ({len(image_data)} chars)")
                        return image_data
                    else:
                        logger.error(f"IMAGE ERROR from {selected_server} | no image in response, result keys: {list(result.keys())}")
                        return None
            except httpx.HTTPStatusError as e:
                error_text = e.response.text[:500] if hasattr(e.response, 'text') else str(e)
                logger.error(f"Remote image generation HTTP error from {selected_server}: {e.response.status_code} - {error_text}")
                return None
            except httpx.TimeoutException:
                logger.error(f"Remote image generation timeout from {selected_server} (timeout={timeout}s)")
                return None
            except Exception as e:
                logger.error(f"Remote image generation failed from {selected_server}: {type(e).__name__}: {e}", exc_info=True)
                return None
                
                # OLD CODE - creates new cycle, causing issues
                # load_balancer = ImageLoadBalancer([selected_server], timeout=timeout)
                # try:
                #     result = await load_balancer.generate_image(

    # Use local backend with GPU LOCK to prevent GPU overload
    # This handles both: no remote servers configured, and when "self" is selected by load balancer
    logger.info("Using local backend for image generation (serialized with GPU lock)")
    result = None
    try:
        # Use shared GPU lock to prevent LLM and image from running simultaneously
        from app.services.locks import GPUResourceLock, image_generation_lock
        async with GPUResourceLock("Image", f"prompt={prompt[:30]}..."):
            async with image_generation_lock:
                prepare_vram_for_image(db)
                backend = get_image_backend(db)
                logger.info(f"Calling backend.generate_image with prompt: {prompt[:50]}...")
                result = await backend.generate_image(
                    prompt=prompt,
                    negative_prompt=negative_prompt,
                    width=width,
                    height=height,
                    steps=steps,
                    cfg=cfg,
                )
                if result:
                    logger.info(f"Local backend returned image ({len(result) if result else 0} chars)")
                else:
                    logger.warning(f"Local backend returned None (generation may have failed)")
    except Exception as e:
        logger.error(f"Local image generation failed with exception: {e}", exc_info=True)
        result = None

    # Fallback to remote if local failed and remote servers are available
    if result is None and servers:
        logger.warning("Local image generation failed, falling back to remote server")
        timeout = int(settings.get("comfyui_timeout", "300000")) / 1000
        load_balancer = ImageLoadBalancer(servers, timeout=timeout)
        return await load_balancer.generate_image(
            prompt=prompt,
            negative_prompt=negative_prompt,
            width=width,
            height=height,
            steps=steps,
            cfg=cfg,
        )

    return result


def get_image_backend(db: Session) -> ImageBackend:
    """
    Get the appropriate image generation backend based on settings.

    Returns either DiffusersService (native) or ImageService (ComfyUI)
    """
    settings = {s.key: s.value for s in db.query(Setting).all()}
    backend = settings.get("image_backend", "comfyui")

    if backend == "native":
        from app.services.diffusers_service import get_diffusers_service
        logger.debug("Using native diffusers backend")
        return get_diffusers_service(db)
    else:
        from app.services.image_service import get_image_service
        logger.debug("Using ComfyUI backend")
        return get_image_service(db)


def get_image_backend_info(db: Session) -> dict:
    """Get information about the current image backend"""
    settings = {s.key: s.value for s in db.query(Setting).all()}
    backend = settings.get("image_backend", "comfyui")

    if backend == "native":
        from app.services.diffusers_service import get_diffusers_service
        service = get_diffusers_service(db)
        return service.get_model_info()
    else:
        return {
            "loaded": bool(settings.get("comfyui_url")),
            "backend": "comfyui",
            "comfyui_url": settings.get("comfyui_url", ""),
        }


def reload_image_model(db: Session):
    """Reload the image model (native backend only)"""
    settings = {s.key: s.value for s in db.query(Setting).all()}
    backend = settings.get("image_backend", "comfyui")

    if backend == "native":
        from app.services.diffusers_service import reload_diffusers_model
        reload_diffusers_model(db)
        logger.info("Native image model reloaded")
    else:
        logger.info("ComfyUI backend - no model reload needed")


def unload_image_model(db: Session):
    """Unload the image model to free VRAM (native backend only)"""
    settings = {s.key: s.value for s in db.query(Setting).all()}
    backend = settings.get("image_backend", "comfyui")

    if backend == "native":
        from app.services.diffusers_service import get_diffusers_service
        service = get_diffusers_service(db)
        service.unload_model()
        logger.info("Native image model unloaded")


def get_image_backend_for_user(db: Session, user: Optional["User"] = None) -> ImageBackend:
    """
    Get the appropriate image generation backend for a specific user.

    If user has custom image generation enabled with a custom ComfyUI URL,
    returns a ComfyUI service pointing to their custom endpoint.
    Otherwise returns the default server backend.
    """
    # Check if user has custom image generation enabled
    if (user and
        user.custom_image_enabled and
        user.custom_image_url):
        # Use custom ComfyUI endpoint
        from app.services.image_service import ImageService
        service = ImageService(db)
        # Override the URL with user's custom URL
        service.comfyui_url = user.custom_image_url.rstrip('/')
        logger.debug(f"Using custom ComfyUI backend for user: {user.custom_image_url}")
        return service

    # Use default server backend
    return get_image_backend(db)


async def generate_image_for_user(
    db: Session,
    user: Optional["User"],
    prompt: str,
    negative_prompt: str = "",
    width: Optional[int] = None,
    height: Optional[int] = None,
    steps: Optional[int] = None,
    cfg: Optional[float] = None,
) -> Optional[str]:
    """
    Generate image for a specific user with load balancing support.
    - If user has custom image generation, uses their endpoint
    - Otherwise uses load balancing if configured
    - Falls back to local backend
    Returns base64 encoded image or None.
    """
    # Check if user has custom image generation enabled - bypass load balancing
    if (user and
        user.custom_image_enabled and
        user.custom_image_url):
        logger.info(f"Using user's custom image endpoint: {user.custom_image_url}")
        prepare_vram_for_image(db)
        backend = get_image_backend_for_user(db, user)
        return await backend.generate_image(
            prompt=prompt,
            negative_prompt=negative_prompt,
            width=width,
            height=height,
            steps=steps,
            cfg=cfg,
        )

    # Use load balancing (will alternate between local and remote)
    return await generate_image_with_load_balancing(
        db=db,
        prompt=prompt,
        negative_prompt=negative_prompt,
        width=width,
        height=height,
        steps=steps,
        cfg=cfg,
    )
