"""
Image Generation Factory
Image generation is always native diffusers (torch-XPU/CUDA/HIP/CPU). Integrates with the
VRAM manager for model swapping on a shared GPU, and supports load balancing across multiple
posterchanai nodes (the unified chat_server_urls list).
"""
import asyncio
from app.utils import lb_auth
import logging
from typing import Optional, Protocol, runtime_checkable, TYPE_CHECKING
from sqlalchemy.orm import Session

from app.services import settings_store
from app.services.image_load_balancer import (
    ImageLoadBalancer,
    NoHealthyImageServersError,
    parse_image_server_urls,
    should_use_remote_image,
)

if TYPE_CHECKING:
    from app.models import User

logger = logging.getLogger("image_factory")

# This node's own GPU is a rotation candidate alongside remote nodes (mirrors music/video factories).
_LOCAL = "__local__"
# Round-robin index across [remote nodes…, local] so images spread over ALL GPUs incl. this one,
# instead of always forwarding to peers and only using local as a fallback.
_rr_index = 0
_rr_lock = asyncio.Lock()


async def _rotated(candidates: list) -> list:
    """Rotate `candidates` by a global round-robin index so each call starts at a different node.
    The stored index is advanced by 1 (mod a large constant) — NOT `% len(candidates)` — so that
    single-candidate (local_only / forwarded) calls don't reset the shared rotation to 0 and starve
    later nodes. Single-candidate calls don't advance the index at all (they aren't a balancing
    decision)."""
    global _rr_index
    if not candidates:
        return []
    async with _rr_lock:
        start = _rr_index % len(candidates)
        if len(candidates) > 1:
            _rr_index = (_rr_index + 1) % 1_000_000
    return candidates[start:] + candidates[:start]


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
    settings = settings_store.all_settings()
    # Single unified load-balancing list (Site → Load Balancing) drives chat/image/music/video.
    server_urls = settings.get("chat_server_urls", "")
    servers = parse_image_server_urls(server_urls)

    if servers:
        timeout = int(settings.get("image_timeout", "300000")) / 1000
        logger.debug(f"Image load balancer available with {len(servers)} server(s)")
        return ImageLoadBalancer(servers, timeout=timeout)
    return None


async def _generate_image_local(db: Session, settings: dict, prompt: str, negative_prompt: str,
                                width, height, steps, cfg) -> Optional[str]:
    """Generate on THIS node's GPU under the shared GPU lock + VRAM swap. Returns base64 or None."""
    # Determine CPU vs GPU mode for the lock WITHOUT initializing a GPU here: this is the parent
    # process that forks the image subprocess, and a GPU (CUDA/XPU) context initialized in the
    # parent corrupts the child's GPU state. Use the configured device, not detect_device().
    image_cpu_mode = settings.get("image_gpu_device", "auto") == "cpu"
    from app.services.locks import GPUResourceLock, image_generation_lock
    async with GPUResourceLock("Image", f"prompt={prompt[:30]}...", cpu_mode=image_cpu_mode):
        async with image_generation_lock:
            prepare_vram_for_image(db)
            backend = get_image_backend(db)
            logger.info(f"[IMAGE] local backend generating: {prompt[:50]}...")
            return await backend.generate_image(
                prompt=prompt, negative_prompt=negative_prompt,
                width=width, height=height, steps=steps, cfg=cfg,
            )


async def _generate_image_on_node(node_url: str, timeout: float, prompt: str, negative_prompt: str,
                                  width, height, steps, cfg) -> Optional[str]:
    """Forward to another posterchanai node's /api/generate-image (server-to-server). That node runs
    its OWN local path (local_only: GPU lock + VRAM swap). Returns base64, or None to try the next."""
    import httpx
    payload = {"prompt": prompt, "negative_prompt": negative_prompt}
    if width is not None:
        payload["width"] = width
    if height is not None:
        payload["height"] = height
    if steps is not None:
        payload["steps"] = steps
    if cfg is not None:
        payload["cfg"] = cfg
    headers = lb_auth.headers()
    async with httpx.AsyncClient(timeout=timeout) as client:
        r = await client.post(f"{node_url}/api/generate-image", json=payload, headers=headers)
    if r.status_code >= 400:
        logger.warning(f"[IMAGE] node {node_url} returned HTTP {r.status_code}")
        return None
    data = r.json()
    if data.get("error"):
        logger.warning(f"[IMAGE] node {node_url} error: {data['error']}")
        return None
    return data.get("image")


async def generate_image_with_load_balancing(
    db: Session,
    prompt: str,
    negative_prompt: str = "",
    width: Optional[int] = None,
    height: Optional[int] = None,
    steps: Optional[int] = None,
    cfg: Optional[float] = None,
    local_only: bool = False,
    dvm_offload: bool = True,
) -> Optional[str]:
    """
    Generate an image with node→node load balancing. Round-robins across [remote nodes…, local] so
    images spread over ALL GPUs (incl. this one), exactly like music/video — NOT "remote first, local
    only as a fallback" (which bypassed this node's GPU and piled load onto peers).
    `local_only` skips remote nodes (set by the /api/generate-image endpoint for server-to-server
    requests so a forwarded request generates HERE instead of bouncing onward → no node→node loop).
    If vram_mode is 'llm_only', local generation is skipped (remote only). Returns base64 or None.
    """
    settings = settings_store.all_settings()
    server_urls = settings.get("chat_server_urls", "")
    vram_mode = settings.get("vram_mode", "shared")
    timeout = int(settings.get("image_timeout", "300000")) / 1000

    # llm_only keeps the LLM resident → no local image gen, UNLESS this is a forwarded request (which
    # was sent here precisely to run on this node's GPU).
    allow_local = local_only or (vram_mode != "llm_only")
    # A forwarded (local_only) request must NOT re-balance, or it loops node→node.
    # exclude_self=True: THIS node is already represented by _LOCAL, so its own IP must be dropped
    # from the peer list — otherwise self is a candidate twice and the rotation wastes a slot
    # forwarding to itself instead of reaching real peers (e.g. nas). parse_server_urls has the
    # robust local-IP detection (ip addr / outbound-socket), so it reliably drops this node's IP.
    # This node distributes its OWN work over the IP LB (server_urls); the CONSUMER side adds remote
    # PROVIDERS (machines others shared with us, reached over Nostr) as extra round-robin candidates.
    # dvm_offload=False (a DVM worker serving someone else's job): spread over the IP LB (server_urls)
    # but DON'T add Nostr providers — that would re-dispatch the job back out over Nostr and loop.
    from app.services import nostr_dvm
    from app.services.load_balancer import parse_server_urls
    prov = {} if (local_only or not dvm_offload) else {p["pubkey"]: p["relay"] for p in nostr_dvm.providers(settings)}
    remote = [] if local_only else parse_server_urls(server_urls, exclude_self=True)

    candidates = ([_LOCAL] if allow_local else []) + list(prov) + remote
    if not candidates:
        logger.error("[IMAGE] No candidates (vram_mode 'llm_only' with no servers configured)")
        return None
    candidates = await _rotated(candidates)
    # Busy-aware: if THIS node's GPU is occupied, push local to the END so the request goes to an
    # idle remote node instead of queueing behind the in-progress task here (local stays as a
    # last-resort fallback if every remote fails).
    if len(candidates) > 1 and _LOCAL in candidates:
        from app.services.locks import gpu_busy
        if gpu_busy():
            candidates = [c for c in candidates if c != _LOCAL] + [_LOCAL]
            logger.info("[IMAGE] local GPU busy → deferring local, preferring remotes")
    logger.info(f"[IMAGE] candidates (round-robin): {candidates}")

    last_err: Optional[Exception] = None
    for cand in candidates:
        try:
            if cand == _LOCAL:
                result = await _generate_image_local(db, settings, prompt, negative_prompt, width, height, steps, cfg)
            elif cand in prov:
                logger.info(f"[IMAGE] offloading to provider {cand[:12]} over Nostr")
                r = await nostr_dvm.run_remote("image", {
                    "prompt": prompt, "negative_prompt": negative_prompt,
                    "width": width, "height": height, "steps": steps, "cfg": cfg,
                }, settings, worker_pubkey=cand, relay=prov[cand], timeout=timeout)
                result = r.get("image") if r else None
            else:
                logger.info(f"[IMAGE] forwarding to remote node {cand}")
                result = await _generate_image_on_node(cand, timeout, prompt, negative_prompt, width, height, steps, cfg)
            if result:
                logger.info(f"[IMAGE] SUCCESS from {cand} ({len(result)} chars)")
                return result
            logger.warning(f"[IMAGE] {cand} produced no image; trying next")
        except Exception as e:
            last_err = e
            logger.warning(f"[IMAGE] {cand} failed: {type(e).__name__}: {e}; trying next")

    logger.error(f"[IMAGE] All image attempts failed (candidates={candidates}, last_err={last_err})")
    return None


def get_image_backend(db: Session) -> ImageBackend:
    """Get the image generation backend (always native diffusers/torch-XPU now)."""
    from app.services.diffusers_service import get_diffusers_service
    return get_diffusers_service(db)


def get_image_backend_info(db: Session) -> dict:
    """Get information about the native image backend."""
    from app.services.diffusers_service import get_diffusers_service
    return get_diffusers_service(db).get_model_info()


def reload_image_model(db: Session):
    """Reload the native image model."""
    from app.services.diffusers_service import reload_diffusers_model
    reload_diffusers_model(db)
    logger.info("Native image model reloaded")


def unload_image_model(db: Session):
    """Unload the native image model to free VRAM."""
    from app.services.diffusers_service import get_diffusers_service
    get_diffusers_service(db).unload_model()
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
    _img = await generate_image_with_load_balancing(
        db=db,
        prompt=prompt,
        negative_prompt=negative_prompt,
        width=width,
        height=height,
        steps=steps,
        cfg=cfg,
    )
    # Public stats counter (Server Stats page): count a produced image, NOT an attempt. Generated
    # images are returned to the caller and never recorded, so there is nothing to aggregate later.
    # Peer-forwarded work arrives via generate_image_with_load_balancing() instead, so this counts
    # what THIS node's users asked for, not work relayed from another node.
    if _img:
        try:
            from app.services import stats_service
            stats_service.bump("image")
        except Exception:
            pass
    return _img
