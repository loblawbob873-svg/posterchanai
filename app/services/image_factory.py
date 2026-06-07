"""
Image Generation Factory
Selects between native diffusers and ComfyUI backends based on settings.
Integrates with VRAM manager for model swapping on shared GPU.
Supports user-specific custom ComfyUI endpoints.
Supports load balancing across multiple posterchanai servers.
"""
import logging
import random
from typing import Optional, Protocol, runtime_checkable, TYPE_CHECKING
from sqlalchemy.orm import Session

from app.models import Setting
from app.services.image_load_balancer import (
    ImageLoadBalancer,
    NoHealthyImageServersError,
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


# Always-on prompt enrichment, applied once at the entry server (not on forwarded
# server-to-server requests). Suppresses the framing/cropping/flat-background the model
# tends to add, and varies the setting so images aren't all the same backdrop.
_BASE_NEGATIVE = "bad quality, blurry, distorted, ugly, deformed, low resolution"
_FRAME_NEGATIVE = (
    "picture frame, border, frame, vignette, letterbox, pillarbox, "
    "cropped, cut off, headshot, bust, close-up, "
    "painted background, flat background, studio backdrop, gradient background, "
    "sketch, sketchbook, pencil sketch, pencil drawing, charcoal drawing, rough sketch, "
    "line art, lineart, doodle, scribble, unfinished, draft, hand-drawn"
)
_RANDOM_SCENES = [
    "in a neon-lit city street at night", "on a sunny tropical beach",
    "in a cozy coffee shop by the window", "in a misty pine forest",
    "on a rooftop overlooking a city skyline at dusk", "in a cyberpunk alley with glowing signs",
    "in a sunlit meadow full of wildflowers", "inside a grand library with tall bookshelves",
    "on a snowy mountain trail", "in a bustling night market",
    "in an autumn park with falling leaves", "on a quiet pier at sunset",
    "in a futuristic sci-fi corridor", "in a flower garden in full bloom",
    "on a rain-soaked street with neon reflections", "in a desert canyon at golden hour",
    "in a traditional Japanese garden", "in a candle-lit medieval tavern",
]


def _enrich_prompt(prompt: str) -> str:
    """Append a random background/location so generations vary their setting."""
    return f"{prompt}, {random.choice(_RANDOM_SCENES)}"


def _ensure_frame_negative(negative: str) -> str:
    """Merge the caller's negative with the base + frame-suppression negatives."""
    parts = [p for p in [(negative or "").strip().rstrip(","), _BASE_NEGATIVE, _FRAME_NEGATIVE] if p]
    return ", ".join(parts)


async def generate_image_with_load_balancing(
    db: Session,
    prompt: str,
    negative_prompt: str = "",
    width: Optional[int] = None,
    height: Optional[int] = None,
    steps: Optional[int] = None,
    cfg: Optional[float] = None,
    is_load_balanced: bool = False,
) -> Optional[str]:
    """
    Generate image with load balancing support.
    Alternates between local and remote servers if load balancing is configured.
    Remote requests run in parallel; local requests are serialized via lock.
    If vram_mode is 'llm_only', always uses remote servers.
    Returns base64 encoded image or None.

    `is_load_balanced` is True only for forwarded server-to-server requests (they already
    carry the enriched prompt/negative from the origin), so we enrich exactly once.
    """
    from app.services.locks import image_generation_lock

    # Enrich once, at the origin server only — never on forwarded requests. This runs BEFORE
    # any routing, so both local and remote backends receive the same enriched values. The
    # load-balancing logic below is unchanged.
    if not is_load_balanced:
        prompt = _enrich_prompt(prompt)
        negative_prompt = _ensure_frame_negative(negative_prompt)

    # Query settings from database
    settings = {s.key: s.value for s in db.query(Setting).all()}
    image_server_urls = settings.get("image_server_urls", "")
    vram_mode = settings.get("vram_mode", "shared")
    servers = parse_image_server_urls(image_server_urls)

    # If vram_mode is llm_only, always use remote servers (no local image generation)
    force_remote = vram_mode == "llm_only"
    
    if force_remote and not servers:
        logger.error("[IMAGE] vram_mode is 'llm_only' but no image servers configured")
        return None

    # If remote image servers are configured, use load balancing
    # Simple round-robin: select one server and make request
    logger.info(f"[IMAGE] Checking load balancing: servers={servers}, len={len(servers) if servers else 0}, force_remote={force_remote}")
    if servers:
        from app.services.image_load_balancer import get_healthy_image_server
        
        timeout = int(settings.get("comfyui_timeout", "300000")) / 1000
        selected_server = await get_healthy_image_server(servers)
        logger.info(f"[IMAGE] Load balancer returned: {selected_server}")
        
        if selected_server:
            # Make HTTP request to selected server (round-robin)
            logger.info(f"[IMAGE] Load balancer selected: {selected_server} (from {len(servers)} server(s): {servers})")
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
            
            # Server-to-server requests don't need authentication - use load-balanced header
            headers = {}
            headers["X-Posterchanai-Load-Balanced"] = "true"
            logger.debug(f"[IMAGE] Sending load-balanced request to {selected_server} (no auth required)")
            
            try:
                async with httpx.AsyncClient(timeout=timeout) as client:
                    logger.info(f"[IMAGE] Request to {selected_server} with prompt: {prompt[:50]}... (headers: {list(headers.keys())})")
                    response = await client.post(
                        f"{selected_server}/api/generate-image",
                        json=payload,
                        headers=headers
                    )
                    
                    logger.info(f"[IMAGE] Response from {selected_server}: status={response.status_code}")
                    
                    if response.status_code == 401:
                        error_body = response.text[:500] if hasattr(response, 'text') else ""
                        logger.error(f"[IMAGE] ERROR from {selected_server} | Authentication failed (401) - Response: {error_body}")
                        logger.error(f"[IMAGE] Load-balanced header sent: {headers.get('X-Posterchanai-Load-Balanced')}")
                        # Fall through to local backend
                    else:
                        try:
                            response.raise_for_status()
                            result = response.json()
                            
                            if result.get("error"):
                                logger.error(f"[IMAGE] ERROR from {selected_server} | error={result['error']}")
                                # Fall through to local backend
                            else:
                                image_data = result.get("image")
                                if image_data:
                                    logger.info(f"[IMAGE] SUCCESS from {selected_server} ({len(image_data)} chars)")
                                    return image_data
                                else:
                                    logger.error(f"[IMAGE] ERROR from {selected_server} | no image in response")
                                    # Fall through to local backend
                        except httpx.HTTPStatusError:
                            # Will be caught by outer exception handler
                            raise
            except httpx.HTTPStatusError as e:
                error_text = e.response.text[:500] if hasattr(e.response, 'text') else str(e)
                logger.error(f"[IMAGE] HTTP error from {selected_server}: {e.response.status_code} - {error_text}")
                # Fall through to local backend
            except httpx.TimeoutException:
                logger.error(f"[IMAGE] Timeout from {selected_server} (timeout={timeout}s)")
                # Fall through to local backend
            except Exception as e:
                logger.error(f"[IMAGE] Failed from {selected_server}: {type(e).__name__}: {e}", exc_info=True)
                # Fall through to local backend

    # Use local backend with GPU/CPU LOCK to prevent resource overload
    # This handles: no remote servers configured, remote request failed, or when "self" is selected by load balancer
    # Skip local if vram_mode is llm_only (force_remote)
    if force_remote:
        logger.warning("[IMAGE] vram_mode is 'llm_only' - skipping local generation, will try fallback to all remote servers")
        result = None
    else:
        logger.info("Using local backend for image generation (serialized with GPU/CPU lock)")
        result = None
        try:
            # Determine CPU vs GPU mode for the lock WITHOUT initializing a GPU here: this is the
            # parent process that forks the image subprocess, and a GPU (CUDA/XPU) context
            # initialized in the parent corrupts the child's GPU state (generation crashes at the
            # first compute step). Use the configured device instead of detect_device().
            image_device = settings.get("image_gpu_device", "auto")
            image_cpu_mode = image_device == "cpu"
            
            # Use shared GPU/CPU lock to prevent LLM and image from running simultaneously
            from app.services.locks import GPUResourceLock, image_generation_lock
            async with GPUResourceLock("Image", f"prompt={prompt[:30]}...", cpu_mode=image_cpu_mode):
                async with image_generation_lock:
                    prepare_vram_for_image(db)
                    backend = get_image_backend(db)
                    logger.info(f"Calling backend.generate_image with prompt: {prompt[:50]}...")
                    logger.info(f"Backend type: {type(backend).__name__}")
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
                        logger.error(f"Local backend returned None (generation failed - backend returned no result)")
                        # Try to get more info about why it failed
                        try:
                            backend_info = get_image_backend_info(db)
                            logger.error(f"Backend info: {backend_info}")
                        except Exception as info_e:
                            logger.debug(f"Could not get backend info: {info_e}")
        except Exception as e:
            logger.error(f"Local image generation failed with exception: {type(e).__name__}: {e}", exc_info=True)
            result = None

    # Fallback to remote if local failed and remote servers are available
    if result is None and servers:
        logger.warning("Local image generation failed, falling back to remote server")
        timeout = int(settings.get("comfyui_timeout", "300000")) / 1000
        
        # Server-to-server fallback - no authentication needed
        try:
            load_balancer = ImageLoadBalancer(servers, timeout=timeout)
            return await load_balancer.generate_image(
                prompt=prompt,
                negative_prompt=negative_prompt,
                width=width,
                height=height,
                steps=steps,
                cfg=cfg,
            )
        except NoHealthyImageServersError as e:
            logger.error(f"[IMAGE] All remote servers failed in fallback: {e}")
            logger.error(f"[IMAGE] Tried {len(servers)} server(s): {servers}")
            return None
        except Exception as e:
            logger.error(f"[IMAGE] Fallback to remote server failed: {type(e).__name__}: {e}", exc_info=True)
            return None

    if result is None:
        logger.error(f"[IMAGE] All image generation attempts failed - local and remote")
        # Log configuration for debugging
        settings = {s.key: s.value for s in db.query(Setting).all()}
        logger.error(f"[IMAGE] Config - image_backend: {settings.get('image_backend', 'comfyui')}, "
                    f"image_server_urls: {settings.get('image_server_urls', '')}, "
                    f"vram_mode: {settings.get('vram_mode', 'shared')}")
    
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
    - Uses load balancing if configured
    - Falls back to local backend
    Returns base64 encoded image or None.
    """
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
