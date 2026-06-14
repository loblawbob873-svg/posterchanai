"""Music Generation Factory.

Mirrors `image_factory`: music generation can run on REMOTE ACE-Step servers (round-robin load
balancing over `music_server_urls`) or on the LOCAL co-located server (`music_api_base`). Because
the local server shares this box's GPU with the LLM and image models, the local path is wrapped in
the shared `GPUResourceLock` (so only ONE GPU task runs at a time — chat, image OR music) and a
VRAM model-swap (`vram_manager.prepare_for_music` frees our LLM/image weights first). Remote
servers run without the local lock. This is the same lock+swap+LB arrangement image gen uses.

The branded watermark is appended once, after a song is produced, regardless of which server made
it. Wired for the web UI + Telegram only.
"""
import asyncio
import logging
from itertools import cycle
from typing import List, Optional, Tuple

from sqlalchemy.orm import Session

from app.services import music_service
from app.services.music_service import MusicError

logger = logging.getLogger("music_factory")

# Round-robin state for remote music servers (mirrors image_load_balancer's cycle).
_music_server_cycle: Optional[cycle] = None
_music_server_list: List[str] = []
_music_cycle_lock = asyncio.Lock()


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


async def generate_music_for_user(
    db: Session,
    prompt: str,
    lyrics: str = "",
    duration: Optional[float] = None,
    steps: Optional[int] = None,
) -> Tuple[bytes, str]:
    """Generate a song with load balancing + (local) GPU lock + VRAM swap. Returns
    (audio_bytes, ext). Raises MusicError with user-facing guidance on failure."""
    cfg = music_service.get_settings(db)
    if not cfg["enabled"]:
        raise MusicError(
            "Music generation is turned off. An admin can enable it in Admin → Music "
            "(and point it at a running ACE-Step server)."
        )

    body = music_service.build_request_body(cfg, prompt, lyrics, duration, steps)
    timeout = cfg["timeout"]
    fmt = cfg["fmt"]
    servers = parse_music_server_urls(cfg["server_urls"])

    audio_bytes: Optional[bytes] = None
    ext = fmt
    last_err: Optional[Exception] = None

    # 1) Remote load-balanced servers first (no local GPU lock — they own their own GPUs).
    #    Try each once, rotating start point, before falling back to local.
    for _ in range(len(servers)):
        server = await _next_server(servers)
        if not server:
            break
        try:
            logger.info(f"[music] generating on remote server {server}")
            audio_bytes, ext = await music_service.generate_once(server, body, timeout, fmt)
            break
        except MusicError as e:
            last_err = e
            logger.warning(f"[music] remote server {server} failed: {e}; trying next")

    # 2) Local server (co-located on this GPU): serialize with the shared GPU lock and swap VRAM.
    if audio_bytes is None:
        cpu_mode = cfg["device"] == "cpu"
        try:
            from app.services.locks import GPUResourceLock
            from app.services.vram_manager import prepare_for_music
            async with GPUResourceLock("Music", f"prompt={prompt[:30]}...", cpu_mode=cpu_mode):
                prepare_for_music(db)
                logger.info(f"[music] generating on local server {cfg['base_url']}")
                audio_bytes, ext = await music_service.generate_once(cfg["base_url"], body, timeout, fmt)
        except MusicError as e:
            last_err = e
        except Exception as e:
            logger.error(f"[music] local generation error: {e}", exc_info=True)
            last_err = MusicError(f"Music generation error: {e}")

    if audio_bytes is None:
        raise last_err or MusicError("Music generation failed on all servers.")

    # The caller wraps this in a branded video (generic PosterChan background + the song, capped
    # with the end-card outro "watermark") — see command_service._musicgeni_command.
    return audio_bytes, ext
