"""Image-Edit Factory — node→node load balancing for `regeni` (OmniGen v1 instruction editing).

Mirrors `video_factory`: the LOCAL path is NATIVE/in-process (diffusers OmniGen via
`imageedit_service`), not an HTTP call to a co-located server. So:

- LOCAL editing runs under the shared `GPUResourceLock` (chat/image/music/video/edit all QUEUE on
  one GPU lock → exactly one GPU task per node) + `vram_manager.prepare_for_imageedit` (frees our
  LLM/image/video first). Returns the edited PNG bytes.
- REMOTE nodes (`regeni_server_urls` = other posterchanai nodes) are called via their
  `/api/edit-image` endpoint, which runs the remote node's own local path (freeing ITS GPU).

Concurrent requests fan out across DIFFERENT nodes in parallel (one edit per GPU); each node
serializes its own GPU via the shared lock. No dispatcher-wide lock.
"""
import asyncio
import base64
import logging
from typing import List, Optional

import httpx
from sqlalchemy.orm import Session

from app.services.imageedit_service import ImageEditError, get_imageedit_service

logger = logging.getLogger("imageedit_factory")

_LOCAL = "__local__"
_rr_index = 0
_rr_lock = asyncio.Lock()


def parse_regeni_server_urls(raw: str) -> List[str]:
    if not raw:
        return []
    parts = [p.strip().rstrip("/") for chunk in raw.splitlines() for p in chunk.split(",")]
    return [p for p in parts if p]


def _factory_settings(db: Session) -> dict:
    from app.database import safe_query_settings
    s = safe_query_settings(db)
    def _i(k, d):
        try:
            return int(float(s.get(k, d)))
        except Exception:
            return int(d)
    return {
        "enabled": str(s.get("regeni_enabled", "false")).lower() == "true",
        "local_enabled": str(s.get("regeni_local_enabled", "true")).lower() == "true",
        "server_urls": s.get("regeni_server_urls", "") or "",
        "device": s.get("regeni_gpu_device", "auto") or "auto",
        "timeout": _i("regeni_timeout", 300000) / 1000.0,
    }


async def _rotated(candidates: List[str]) -> List[str]:
    global _rr_index
    if not candidates:
        return []
    async with _rr_lock:
        start = _rr_index % len(candidates)
        _rr_index = (_rr_index + 1) % len(candidates)
    return candidates[start:] + candidates[:start]


async def _generate_local(db: Session, cfg: dict, image_bytes: bytes, instruction: str) -> bytes:
    """Edit natively on THIS node's GPU under the shared lock + VRAM swap. Returns PNG bytes."""
    from app.services.locks import GPUResourceLock
    from app.services.vram_manager import prepare_for_imageedit
    cpu_mode = cfg["device"] == "cpu"
    async with GPUResourceLock("ImageEdit", f"instruction={instruction[:30]}...", cpu_mode=cpu_mode):
        prepare_for_imageedit(db)
        service = get_imageedit_service(db)
        png = await asyncio.to_thread(service.generate, db, image_bytes, instruction)
        if not png:
            raise ImageEditError("Image edit produced no output.")
        return png


async def _generate_on_node(node_url: str, image_bytes: bytes, instruction: str, timeout: float) -> bytes:
    url = node_url.rstrip("/") + "/api/edit-image"
    payload = {"image": base64.b64encode(image_bytes).decode(), "instruction": instruction}
    headers = {"X-Posterchanai-Load-Balanced": "true"}
    async with httpx.AsyncClient(timeout=httpx.Timeout(max(60.0, timeout) + 60.0, connect=15.0)) as client:
        try:
            r = await client.post(url, json=payload, headers=headers)
        except httpx.RequestError as e:
            raise ImageEditError(f"Couldn't reach edit node {node_url}: {e}")
    if r.status_code >= 400:
        raise ImageEditError(f"Edit node {node_url} returned HTTP {r.status_code}.")
    data = r.json()
    if data.get("error"):
        raise ImageEditError(data["error"])
    img_b64 = data.get("image")
    if not img_b64:
        raise ImageEditError(f"Edit node {node_url} returned no image.")
    return base64.b64decode(img_b64)


async def edit_image_for_user(
    db: Session,
    image_bytes: bytes,
    instruction: str,
    local_only: bool = False,
) -> bytes:
    """Edit an image with node→node load balancing + (local) GPU lock + VRAM swap. Returns edited
    PNG bytes. `local_only` skips remote nodes (set by /api/edit-image). Raises ImageEditError."""
    cfg = _factory_settings(db)
    if not cfg["enabled"]:
        raise ImageEditError("Image editing is turned off. An admin can enable it in Admin → Image.")
    if not instruction or not instruction.strip():
        raise ImageEditError("regeni needs an editing instruction, e.g. `regeni change her hair to red`.")
    if not image_bytes:
        raise ImageEditError("regeni needs an attached image to edit.")

    if local_only:
        if not cfg["local_enabled"]:
            raise ImageEditError("Local image editing is disabled on this node.")
        candidates = [_LOCAL]
    else:
        candidates = parse_regeni_server_urls(cfg["server_urls"])
        if cfg["local_enabled"]:
            candidates = candidates + [_LOCAL]
    if not candidates:
        raise ImageEditError("No image-edit nodes available (enable local or add regeni_server_urls).")
    candidates = await _rotated(candidates)

    out: Optional[bytes] = None
    last_err: Optional[Exception] = None
    for cand in candidates:
        try:
            if cand == _LOCAL:
                out = await _generate_local(db, cfg, image_bytes, instruction)
            else:
                logger.info(f"[regeni] editing on remote node {cand}")
                out = await _generate_on_node(cand, image_bytes, instruction, cfg["timeout"])
            break
        except ImageEditError as e:
            last_err = e
            logger.warning(f"[regeni] node {cand} failed: {e}; trying next")
        except Exception as e:
            logger.error(f"[regeni] node {cand} unexpected error: {e}", exc_info=True)
            last_err = ImageEditError(f"Image edit error: {e}")

    if out is None:
        raise last_err or ImageEditError("Image edit failed on all nodes.")
    return out
