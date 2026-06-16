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
    """Parse the comma/newline list of nodes (bare IPs or full URLs) into normalized node URLs."""
    if not raw:
        return []
    from app.services.load_balancer import normalize_node_url
    parts = [p for chunk in raw.splitlines() for p in chunk.split(",")]
    return [n for p in parts if (n := normalize_node_url(p))]


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
    global _rr_index
    if not candidates:
        return []
    async with _rr_lock:
        start = _rr_index % len(candidates)
        _rr_index = (_rr_index + 1) % len(candidates)
    return candidates[start:] + candidates[:start]


async def _generate_local(db: Session, cfg: dict, prompt: str, negative: str) -> bytes:
    """Generate natively on THIS node's GPU under the shared lock + VRAM swap, then assemble the
    branded MP4. Returns mp4 bytes."""
    from app.services.locks import GPUResourceLock
    from app.services.vram_manager import prepare_for_video
    from app.services import media_service
    cpu_mode = cfg["device"] == "cpu"
    async with GPUResourceLock("Video", f"prompt={prompt[:30]}...", cpu_mode=cpu_mode):
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
    headers = {"X-Posterchanai-Load-Balanced": "true"}
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
) -> bytes:
    """Generate a clip with node→node load balancing + (local) GPU lock + VRAM swap. Returns the
    assembled, branded MP4 bytes. `local_only` skips remote nodes (set by /api/generate-video).
    Raises VideoError on failure."""
    cfg = _factory_settings(db)
    if not cfg["enabled"]:
        raise VideoError(
            "Video generation is turned off. An admin can enable it in Admin → Video."
        )

    if local_only:
        # A forwarded request (from another node's LB). If THIS node has local video disabled (e.g.
        # its GPU is owned by the music server / other work), refuse so the caller falls back instead
        # of OOMing here.
        if not cfg["local_enabled"]:
            raise VideoError("Local video generation is disabled on this node.")
        candidates = [_LOCAL]
    else:
        candidates = parse_video_server_urls(cfg["server_urls"])
        if cfg["local_enabled"]:
            candidates = candidates + [_LOCAL]
    if not candidates:
        raise VideoError("No video generation nodes available (enable local, or add nodes in Site → Load Balancing).")
    candidates = await _rotated(candidates)

    video_bytes: Optional[bytes] = None
    last_err: Optional[Exception] = None
    for cand in candidates:
        try:
            if cand == _LOCAL:
                video_bytes = await _generate_local(db, cfg, prompt, negative_prompt)
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
    return video_bytes
