"""Music Generation Factory.

Mirrors `image_factory` EXACTLY, including cross-node behaviour:

- REMOTE nodes (`music_server_urls` = other posterchanai nodes) are called via their
  `/api/generate-music` endpoint — NOT acestep directly. That endpoint runs the remote node's own
  local path, so the remote node frees ITS GPU (`prepare_for_music`) before generating. This is the
  same node→node pattern image gen uses (`/api/generate-image`), and it's what makes "unload the GPU
  before processing" work across machines.
- LOCAL generation (`music_api_base` = this node's acestep server) is wrapped in the shared
  `GPUResourceLock` (so chat, image AND music all QUEUE on one GPU lock) plus the VRAM swap
  (`vram_manager.prepare_for_music` unloads our LLM/image first).

`_music_gen_lock` additionally serializes music on this node so concurrent requests queue instead
of stacking generations onto one GPU. Wired for the web UI + Telegram only.
"""
import asyncio
import base64
import logging
from itertools import cycle
from typing import List, Optional, Tuple

import httpx
from sqlalchemy.orm import Session

from app.services import music_service
from app.services.music_service import MusicError

logger = logging.getLogger("music_factory")

# Round-robin state for remote music servers (mirrors image_load_balancer's cycle).
_music_server_cycle: Optional[cycle] = None
_music_server_list: List[str] = []
_music_cycle_lock = asyncio.Lock()

# Serialize music generation so concurrent requests QUEUE instead of piling onto one ACE-Step GPU
# and OOMing (the music server's GPU is small — 12GB — and a song needs most of it). Like image gen
# is serialized by the GPU lock. Per-process; the single port-3051 instance is the only producer.
_music_gen_lock = asyncio.Lock()


def parse_music_server_urls(raw: str) -> List[str]:
    """Parse the comma/newline-separated music_server_urls setting into a clean list."""
    if not raw:
        return []
    parts = [p.strip().rstrip("/") for chunk in raw.splitlines() for p in chunk.split(",")]
    return [p for p in parts if p]


async def _next_server(servers: List[str]) -> Optional[str]:
    """Simple round-robin selection across the configured remote servers."""
    global _music_server_cycle, _music_server_list
    if not servers:
        return None
    async with _music_cycle_lock:
        if _music_server_cycle is None or tuple(_music_server_list) != tuple(servers):
            _music_server_list = list(servers)
            _music_server_cycle = cycle(_music_server_list)
        return next(_music_server_cycle)


async def _generate_on_node(node_url: str, prompt: str, lyrics: str, duration, steps,
                            timeout: float, fmt: str) -> Tuple[bytes, str]:
    """Call another posterchanai node's /api/generate-music (server-to-server). That node runs its
    OWN local path (GPU lock + VRAM swap + its local acestep), so it frees its GPU first."""
    url = node_url.rstrip("/") + "/api/generate-music"
    payload = {"prompt": prompt, "lyrics": lyrics, "duration": duration, "steps": steps, "format": fmt}
    headers = {"X-Posterchanai-Load-Balanced": "true"}
    # The remote node does the full generation, so allow generous time over our request timeout.
    async with httpx.AsyncClient(timeout=httpx.Timeout(max(60.0, timeout) + 60.0, connect=15.0)) as client:
        try:
            r = await client.post(url, json=payload, headers=headers)
        except httpx.RequestError as e:
            raise MusicError(f"Couldn't reach music node {node_url}: {e}")
    if r.status_code >= 400:
        raise MusicError(f"Music node {node_url} returned HTTP {r.status_code}.")
    data = r.json()
    if data.get("error"):
        raise MusicError(data["error"])
    audio_b64 = data.get("audio")
    if not audio_b64:
        raise MusicError(f"Music node {node_url} returned no audio.")
    return base64.b64decode(audio_b64), (data.get("format") or fmt)


async def generate_music_for_user(
    db: Session,
    prompt: str,
    lyrics: str = "",
    duration: Optional[float] = None,
    steps: Optional[int] = None,
    local_only: bool = False,
) -> Tuple[bytes, str]:
    """Generate a song with node→node load balancing + (local) GPU lock + VRAM swap. Returns
    (audio_bytes, ext). `local_only` skips remote nodes (set by the /api/generate-music endpoint so
    a forwarded request generates here instead of bouncing onward). Raises MusicError on failure."""
    cfg = music_service.get_settings(db)
    if not cfg["enabled"]:
        raise MusicError(
            "Music generation is turned off. An admin can enable it in Admin → Music "
            "(and point it at a running ACE-Step server)."
        )

    timeout = cfg["timeout"]
    fmt = cfg["fmt"]
    servers = [] if local_only else parse_music_server_urls(cfg["server_urls"])

    audio_bytes: Optional[bytes] = None
    ext = fmt
    last_err: Optional[Exception] = None

    # Serialize: one song at a time on this node (a music GPU is small; concurrent gens OOM it).
    # Queues like image gen does on the GPU lock.
    if _music_gen_lock.locked():
        logger.info("[music] another song is generating — queued")
    async with _music_gen_lock:
        # 1) Remote nodes first (round-robin). Each runs its OWN VRAM swap via /api/generate-music.
        for _ in range(len(servers)):
            node = await _next_server(servers)
            if not node:
                break
            try:
                logger.info(f"[music] generating on remote node {node}")
                audio_bytes, ext = await _generate_on_node(node, prompt, lyrics, duration, steps, timeout, fmt)
                break
            except MusicError as e:
                last_err = e
                logger.warning(f"[music] remote node {node} failed: {e}; trying next")

        # 2) Local acestep server on THIS node: shared GPU lock + VRAM swap (frees our LLM/image).
        if audio_bytes is None:
            cpu_mode = cfg["device"] == "cpu"
            body = music_service.build_request_body(cfg, prompt, lyrics, duration, steps)
            try:
                from app.services.locks import GPUResourceLock
                from app.services.vram_manager import prepare_for_music
                async with GPUResourceLock("Music", f"prompt={prompt[:30]}...", cpu_mode=cpu_mode):
                    prepare_for_music(db)
                    logger.info(f"[music] generating on local acestep {cfg['base_url']}")
                    audio_bytes, ext = await music_service.generate_once(cfg["base_url"], body, timeout, fmt)
            except MusicError as e:
                last_err = e
            except Exception as e:
                logger.error(f"[music] local generation error: {e}", exc_info=True)
                last_err = MusicError(f"Music generation error: {e}")

    if audio_bytes is None:
        raise last_err or MusicError("Music generation failed on all nodes.")

    # The caller wraps this in a branded video (generic PosterChan background + the song, capped
    # with the end-card outro "watermark") — see command_service._musicgeni_command.
    return audio_bytes, ext
