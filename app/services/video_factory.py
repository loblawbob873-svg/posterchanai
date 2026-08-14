"""Video Generation Factory — node→node load balancing for `videogeni`.

Mirrors `music_factory` (and image LB), with ONE difference: the LOCAL path is NATIVE/in-process
(diffusers Wan via `video_service`), not an HTTP call to a co-located server. So:

- LOCAL generation runs under the shared `GPUResourceLock` (chat/image/music/video all QUEUE on one
  GPU lock) + `vram_manager.prepare_for_video` (frees our LLM/image first), then assembles the
  branded MP4 (frames → `media_service.make_generated_video` → end-card outro "watermark").
- REMOTE nodes (the unified `chat_server_urls` list = other posterchanai nodes) are called via their
  `/api/generate-video` endpoint, which runs the remote node's own local path (so it frees ITS GPU).

Concurrent requests fan out across DIFFERENT nodes in parallel (one clip per GPU); each node
serializes its own GPU via the shared `GPUResourceLock`. No dispatcher-wide lock. Returns assembled
MP4 bytes. Wired for the web UI + Telegram only (NOT the fedi bots — abuse surface).
"""
import asyncio
from app.utils import lb_auth
import base64
import logging
from typing import List, Optional

import httpx
from sqlalchemy.orm import Session

from app.services.video_service import VideoError, get_video_service

logger = logging.getLogger("video_factory")

_LOCAL = "__local__"
_rr_index = 0
_rr_lock = asyncio.Lock()


def parse_video_server_urls(raw: str) -> List[str]:
    """Parse the unified node list (bare IPs or URLs) into normalized peer URLs, EXCLUDING this node
    (it's already represented by _LOCAL — keeping its own IP here would forward video to itself and
    starve real peers in the rotation)."""
    if not raw:
        return []
    from app.services.load_balancer import parse_server_urls
    return parse_server_urls(raw, exclude_self=True)


def _factory_settings(db: Session) -> dict:
    from app.database import safe_query_settings
    s = safe_query_settings(db)
    def _i(k, d):
        try:
            return int(float(s.get(k, d)))
        except Exception:
            return int(d)
    return {
        "enabled": str(s.get("video_enabled", "false")).lower() == "true",
        "local_enabled": str(s.get("video_local_enabled", "true")).lower() == "true",
        # Cross-node LB uses the single unified list (Site → Load Balancing).
        "server_urls": s.get("chat_server_urls", "") or "",
        "device": s.get("video_gpu_device", "auto") or "auto",
        "timeout": _i("video_timeout", 600000) / 1000.0,
        "watermark": str(s.get("video_watermark_enabled", "true")).lower() != "false",
        "upscale_height": _i("video_upscale_height", 720),
    }


async def _rotated(candidates: List[str]) -> List[str]:
    """Rotate by a global round-robin index. The stored index advances by 1 (mod a large constant),
    NOT `% len(candidates)` — otherwise single-candidate (local_only) calls reset it to 0 and starve
    later nodes. Single-candidate calls don't advance it (not a balancing decision)."""
    global _rr_index
    if not candidates:
        return []
    async with _rr_lock:
        start = _rr_index % len(candidates)
        if len(candidates) > 1:
            _rr_index = (_rr_index + 1) % 1_000_000
    return candidates[start:] + candidates[:start]


async def _generate_local(db: Session, cfg: dict, prompt: str, negative: str) -> bytes:
    """Generate natively on THIS node's GPU under the shared lock + VRAM swap, then assemble the
    branded MP4. Returns mp4 bytes."""
    from app.services.locks import GPUResourceLock
    from app.services.vram_manager import prepare_for_video
    from app.services import media_service
    cpu_mode = cfg["device"] == "cpu"
    async with GPUResourceLock("Video", f"prompt={len(prompt or '')} chars", cpu_mode=cpu_mode):
        prepare_for_video(db)
        service = get_video_service(db)
        frames, fps = await asyncio.to_thread(service.generate, db, prompt, negative)
        if not frames:
            raise VideoError("Video generation produced no frames.")
        mp4 = await asyncio.to_thread(
            media_service.make_generated_video, frames, fps, cfg["watermark"], "", cfg["upscale_height"]
        )
        if not mp4:
            raise VideoError("Failed to assemble the video file (is ffmpeg installed?).")
        return mp4


async def _generate_on_node(node_url: str, prompt: str, negative: str, timeout: float) -> bytes:
    url = node_url.rstrip("/") + "/api/generate-video"
    payload = {"prompt": prompt, "negative_prompt": negative}
    headers = lb_auth.headers()
    async with httpx.AsyncClient(timeout=httpx.Timeout(max(60.0, timeout) + 60.0, connect=15.0)) as client:
        try:
            r = await client.post(url, json=payload, headers=headers)
        except httpx.RequestError as e:
            raise VideoError(f"Couldn't reach video node {node_url}: {e}")
    if r.status_code >= 400:
        raise VideoError(f"Video node {node_url} returned HTTP {r.status_code}.")
    data = r.json()
    if data.get("error"):
        raise VideoError(data["error"])
    vid_b64 = data.get("video")
    if not vid_b64:
        raise VideoError(f"Video node {node_url} returned no video.")
    return base64.b64decode(vid_b64)


async def generate_video_for_user(
    db: Session,
    prompt: str,
    negative_prompt: str = "",
    local_only: bool = False,
    dvm_offload: bool = True,
) -> bytes:
    """Generate a clip with node→node load balancing + (local) GPU lock + VRAM swap. Returns the
    assembled, branded MP4 bytes. `local_only` skips remote nodes (set by /api/generate-video).
    Raises VideoError on failure."""

    cfg = _factory_settings(db)
    if not cfg["enabled"]:
        raise VideoError(
            "Video generation is turned off. An admin can enable it in Admin → Video."
        )

    prov = {}
    if local_only:
        # A forwarded request (from another node's LB). If THIS node has local video disabled (e.g.
        # its GPU is owned by the music server / other work), refuse so the caller falls back instead
        # of OOMing here.
        if not cfg["local_enabled"]:
            raise VideoError("Local video generation is disabled on this node.")
        candidates = [_LOCAL]
    else:
        # This node distributes its OWN work over the IP LB; the CONSUMER side adds remote PROVIDERS
        # (machines others shared with us, reached over Nostr) as extra round-robin candidates.
        # dvm_offload=False (serving a DVM job): use the IP LB but NOT Nostr providers (else loop).
        from app.services import nostr_dvm
        prov = {} if not dvm_offload else {p["pubkey"]: p["relay"] for p in nostr_dvm.providers()}
        candidates = list(prov) + parse_video_server_urls(cfg["server_urls"])
        if cfg["local_enabled"]:
            candidates = candidates + [_LOCAL]
    if not candidates:
        raise VideoError("No video generation nodes available (enable local, or add nodes in Site → Load Balancing).")
    candidates = await _rotated(candidates)
    # Busy-aware: if THIS node's GPU is occupied, defer local to the end so the clip goes to an idle
    # remote node instead of queueing behind the in-progress task here.
    if len(candidates) > 1 and _LOCAL in candidates:
        from app.services.locks import gpu_busy
        if gpu_busy():
            candidates = [c for c in candidates if c != _LOCAL] + [_LOCAL]
            logger.info("[video] local GPU busy → deferring local, preferring remotes")

    video_bytes: Optional[bytes] = None
    last_err: Optional[Exception] = None
    for cand in candidates:
        try:
            if cand == _LOCAL:
                video_bytes = await _generate_local(db, cfg, prompt, negative_prompt)
            elif cand in prov:
                logger.info(f"[video] offloading to provider {cand[:12]} over Nostr")
                from app.services import nostr_dvm
                r = await nostr_dvm.run_remote("video", {
                    "prompt": prompt, "negative_prompt": negative_prompt,
                }, worker_pubkey=cand, relay=prov[cand], timeout=cfg["timeout"])
                if not r or not r.get("video"):
                    raise VideoError("worker returned no video")
                import base64 as _b64
                video_bytes = _b64.b64decode(r["video"])
            else:
                logger.info(f"[video] generating on remote node {cand}")
                video_bytes = await _generate_on_node(cand, prompt, negative_prompt, cfg["timeout"])
            break
        except VideoError as e:
            last_err = e
            logger.warning(f"[video] node {cand} failed: {e}; trying next")
        except Exception as e:
            logger.error(f"[video] node {cand} unexpected error: {e}", exc_info=True)
            last_err = VideoError(f"Video generation error: {e}")

    if video_bytes is None:
        raise last_err or VideoError("Video generation failed on all nodes.")
    # Public stats counter: count a produced clip, NOT an attempt — same rule as image/music.
    if not local_only:
        try:
            from app.services import stats_service
            stats_service.bump("video")
        except Exception:
            pass
    return video_bytes
