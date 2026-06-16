"""Music Generation Factory.

Mirrors `image_factory` EXACTLY, including cross-node behaviour:

- REMOTE nodes (the unified `chat_server_urls` list = other posterchanai nodes) are called via their
  `/api/generate-music` endpoint — NOT acestep directly. That endpoint runs the remote node's own
  local path, so the remote node frees ITS GPU (`prepare_for_music`) before generating. This is the
  same node→node pattern image gen uses (`/api/generate-image`), and it's what makes "unload the GPU
  before processing" work across machines.
- LOCAL generation (this node's acestep server, localhost:8001 by default) is wrapped in the shared
  `GPUResourceLock` (so chat, image AND music all QUEUE on one GPU lock) plus the VRAM swap
  (`vram_manager.prepare_for_music` unloads our LLM/image first).

Concurrent requests fan out across DIFFERENT nodes in parallel (one song per GPU); each node
serializes its own GPU via the shared `GPUResourceLock` (so two songs landing on the same node
queue there, not OOM). No dispatcher-wide lock. Wired for the web UI + Telegram only.
"""
import asyncio
import base64
import logging
from typing import List, Optional, Tuple

import httpx
from sqlalchemy.orm import Session

from app.services import music_service
from app.services.music_service import MusicError

logger = logging.getLogger("music_factory")

# This node's own acestep server is a rotation candidate alongside remote nodes.
_LOCAL = "__local__"

# Round-robin index across [remote nodes…, local] so music spreads over BOTH machines (like the
# image LB alternates local/remote).
_rr_index = 0
_rr_lock = asyncio.Lock()

def parse_music_server_urls(raw: str) -> List[str]:
    """Parse the comma/newline-separated server-URL list (chat_server_urls) into a clean list."""
    if not raw:
        return []
    parts = [p.strip().rstrip("/") for chunk in raw.splitlines() for p in chunk.split(",")]
    return [p for p in parts if p]


async def _rotated(candidates: List[str]) -> List[str]:
    """Return `candidates` rotated by a global round-robin index, so each call starts at a different
    node. On failure the caller falls through to the rest of the list."""
    global _rr_index
    if not candidates:
        return []
    async with _rr_lock:
        start = _rr_index % len(candidates)
        _rr_index = (_rr_index + 1) % len(candidates)
    return candidates[start:] + candidates[:start]


async def _generate_local(db: Session, cfg: dict, prompt: str, lyrics: str, duration, steps,
                          timeout: float, fmt: str) -> Tuple[bytes, str]:
    """Generate on THIS node's acestep server under the shared GPU lock + VRAM swap (frees our
    LLM/image first), so chat/image/music all queue on one GPU."""
    from app.services.locks import GPUResourceLock
    from app.services.vram_manager import prepare_for_music
    cpu_mode = cfg["device"] == "cpu"
    body = music_service.build_request_body(cfg, prompt, lyrics, duration, steps)
    async with GPUResourceLock("Music", f"prompt={prompt[:30]}...", cpu_mode=cpu_mode):
        prepare_for_music(db)
        logger.info(f"[music] generating on local acestep {cfg['base_url']}")
        return await music_service.generate_once(cfg["base_url"], body, timeout, fmt)


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

    # Round-robin across remote nodes AND this node's local acestep, so songs spread over both
    # machines. A forwarded request (/api/generate-music) is local_only — it generates HERE.
    if local_only:
        candidates = [_LOCAL]
    else:
        candidates = parse_music_server_urls(cfg["server_urls"]) + [_LOCAL]
    candidates = await _rotated(candidates)

    audio_bytes: Optional[bytes] = None
    ext = fmt
    last_err: Optional[Exception] = None

    # NO dispatcher-wide lock here: concurrent requests must be free to fan out across DIFFERENT
    # nodes in parallel (one song per GPU). Per-GPU serialization (and OOM protection) is handled
    # ON each node — the local path takes the shared GPUResourceLock + prepare_for_music, and a
    # remote node's /api/generate-music does the same on its side. So 2 requests → nas + Arc at once.
    for cand in candidates:
        try:
            if cand == _LOCAL:
                audio_bytes, ext = await _generate_local(db, cfg, prompt, lyrics, duration, steps, timeout, fmt)
            else:
                logger.info(f"[music] generating on remote node {cand}")
                audio_bytes, ext = await _generate_on_node(cand, prompt, lyrics, duration, steps, timeout, fmt)
            break
        except MusicError as e:
            last_err = e
            logger.warning(f"[music] node {cand} failed: {e}; trying next")
        except Exception as e:
            logger.error(f"[music] node {cand} unexpected error: {e}", exc_info=True)
            last_err = MusicError(f"Music generation error: {e}")

    if audio_bytes is None:
        raise last_err or MusicError("Music generation failed on all nodes.")

    # The caller wraps this in a branded video (generic PosterChan background + the song, capped
    # with the end-card outro "watermark") — see command_service._musicgeni_command.
    return audio_bytes, ext
