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

from app.services import music_service, settings_store
from app.services.music_service import MusicError

logger = logging.getLogger("music_factory")

# This node's own acestep server is a rotation candidate alongside remote nodes.
_LOCAL = "__local__"

# Round-robin index across [remote nodes…, local] so music spreads over BOTH machines (like the
# image LB alternates local/remote).
_rr_index = 0
_rr_lock = asyncio.Lock()

def parse_music_server_urls(raw: str) -> List[str]:
    """Parse the unified node list (bare IPs or URLs) into normalized peer URLs, EXCLUDING this node
    (it's already represented by _LOCAL — keeping its own IP here would forward music to itself and
    starve real peers like nas in the rotation)."""
    if not raw:
        return []
    from app.services.load_balancer import parse_server_urls
    return parse_server_urls(raw, exclude_self=True)


async def _rotated(candidates: List[str]) -> List[str]:
    """Return `candidates` rotated by a global round-robin index, so each call starts at a different
    node. The stored index advances by 1 (mod a large constant), NOT `% len(candidates)` — otherwise
    single-candidate (local_only / forwarded) calls reset it to 0 and starve later nodes. Single-
    candidate calls don't advance it (not a balancing decision)."""
    global _rr_index
    if not candidates:
        return []
    async with _rr_lock:
        start = _rr_index % len(candidates)
        if len(candidates) > 1:
            _rr_index = (_rr_index + 1) % 1_000_000
    return candidates[start:] + candidates[:start]


async def _generate_local(db: Session, cfg: dict, prompt: str, lyrics: str, duration, steps,
                          timeout: float, fmt: str) -> Tuple[bytes, str]:
    """Generate on THIS node under the shared GPU lock + VRAM swap (frees our LLM/image first), so
    chat/image/music/video all QUEUE on one GPU — the lock is the queue, and it is taken here rather
    than inside music_local so the wait is visible to the caller.

    NATIVE by default (diffusers AceStepPipeline, in-process, same torch stack as image/video). The
    old external acestep REST server is only used when `music_api_base` is explicitly set, or on a
    diffusers too old to have the pipeline — so an existing deployment keeps working while nothing
    new depends on it."""
    from app.services.locks import GPUResourceLock
    from app.services.vram_manager import prepare_for_music
    from app.services import music_local
    cpu_mode = cfg["device"] == "cpu"
    explicit_server = bool((cfg.get("base_url_explicit") or "").strip())
    # NATIVE BY DEFAULT. The earlier attempt at this failed for one reason: it loaded through
    # diffusers' AceStepPipeline, whose from_pretrained wants a model_index.json that NO published
    # ACE-Step repo carries. That 404 was read as "the model can't run in-process" and music went
    # back to a per-node sidecar over HTTP. Wrong conclusion — the weights load fine through
    # ACE-Step's OWN AceStepHandler, which is exactly the code that sidecar was running. Proven on
    # the Arc before this flip: load 10.2s, a 12s song in 15.3s, valid mp3.
    # music_local.is_available() gates on the `acestep` PACKAGE (what load_model imports), not the
    # diffusers pipeline; `music_api_base`/`music_native=false` still force the HTTP path.
    use_native = (music_local.is_available() and not explicit_server
                  and str(settings_store.get("music_native", "true")).lower() in ("1", "true", "yes", "on"))
    async with GPUResourceLock("Music", f"prompt={prompt[:30]}...", cpu_mode=cpu_mode):
        prepare_for_music(db)
        if use_native:
            logger.info("[music] generating natively (in-process ACE-Step)")
            return await music_local.generate_async(db, prompt, lyrics, duration, steps, fmt=fmt)
        logger.info(f"[music] generating on external acestep {cfg['base_url']}")
        body = music_service.build_request_body(cfg, prompt, lyrics, duration, steps)
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
    dvm_offload: bool = True,
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
    # A node load-balances its OWN work over the IP LB; Nostr dispatch is the separate machine-sharing
    # path (provider/consumer), so own-serving never auto-dispatches over Nostr.
    # dvm_offload=False (serving a DVM job): use the IP LB but NOT Nostr providers (else it re-offloads
    # the job back out over Nostr and loops).
    from app.services import nostr_dvm
    prov = {} if (local_only or not dvm_offload) else {p["pubkey"]: p["relay"] for p in nostr_dvm.providers()}
    if local_only:
        candidates = [_LOCAL]
    else:
        candidates = list(prov) + parse_music_server_urls(cfg["server_urls"]) + [_LOCAL]
    candidates = await _rotated(candidates)
    # Busy-aware: if THIS node's GPU is occupied, defer local to the end so the song goes to an idle
    # remote node instead of queueing behind the in-progress task here.
    if len(candidates) > 1 and _LOCAL in candidates:
        from app.services.locks import gpu_busy
        if gpu_busy():
            candidates = [c for c in candidates if c != _LOCAL] + [_LOCAL]
            logger.info("[music] local GPU busy → deferring local, preferring remotes")

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
            elif cand in prov:
                logger.info(f"[music] offloading to provider {cand[:12]} over Nostr")
                r = await nostr_dvm.run_remote("music", {
                    "prompt": prompt, "lyrics": lyrics, "duration": duration, "steps": steps,
                }, worker_pubkey=cand, relay=prov[cand], timeout=timeout)
                if not r or not r.get("audio"):
                    raise MusicError("worker returned no audio")
                import base64 as _b64
                audio_bytes, ext = _b64.b64decode(r["audio"]), r.get("format", fmt)
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
    # Public stats counter: count a produced song, NOT an attempt (a failed render raises above and
    # must not inflate the number). `local_only` means another node forwarded this to us, so it is
    # already counted where the user asked; counting again would double it.
    if not local_only:
        try:
            from app.services import stats_service
            stats_service.bump("music")
        except Exception:
            pass
    return audio_bytes, ext
