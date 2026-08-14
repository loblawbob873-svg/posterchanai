"""Where a voice request runs: this node's GPU, or another node's, or neither.

Mirrors music_factory/video_factory exactly, because the three have the same shape — one heavy model,
one GPU, several nodes — and the bugs are the same bugs. What that means concretely:

* **Locking.** The local path takes the shared `GPUResourceLock`, so chat, image, music, video AND
  voice all serialise on ONE lock. Two voice requests landing on the same node queue rather than both
  loading 6GB. The lock is taken HERE and not inside `voice_local`, so the wait is visible to the
  caller (and so a remote request can't sneak past it).
* **Queueing.** The lock IS the queue: `GPUResourceLock` waits on an asyncio.Lock plus a cross-process
  file lock, so requests from the app process and the worker process queue against each other too.
  `queue_depth()` below is what the UI shows while you wait — a voice generation runs at roughly 10x
  realtime, which is long enough that "nothing is happening" is the wrong thing to show a user.
* **VRAM.** `prepare_for_voice` unloads our LLM/image/music/video first. Paired with the lock, that
  means exactly one model is resident at a time on a shared 12/16GB card.
* **Load balancing.** Round-robin over the OTHER nodes plus this one, taken from the ONE unified
  `chat_server_urls` list that chat, image, music and video all share — there is no per-feature
  server setting, because a second list is a second thing to keep in step and the node missing from
  it fails by quietly never being asked. The four recurring LB bugs this file is written to avoid:
  advancing the round-robin index without `% len` (which pins every request to node 0 once the list
  shrinks), forgetting to exclude ourselves (a node proxying to itself deadlocks on its own GPU
  lock), not being busy-aware, and treating a node that 404s the endpoint as a hard failure instead
  of "that node hasn't got voice installed".
"""
from __future__ import annotations

import asyncio
import logging
from typing import List, Optional, Tuple

import httpx
from sqlalchemy.orm import Session

from app.services import settings_store

logger = logging.getLogger(__name__)

_rr_index = 0
_rr_lock = asyncio.Lock()
# How many requests are waiting on (or holding) the local GPU for voice. Purely for display —
# GPUResourceLock is the real gate. Incremented before contending so the number a user sees includes
# the request that is currently generating, not just the ones behind it.
_queued = 0
_queue_lock = asyncio.Lock()


def queue_depth() -> int:
    """Requests waiting for or holding this node's voice slot (0 = your request starts immediately)."""
    return _queued


def other_nodes(raw: str) -> List[str]:
    """The OTHER nodes, from the ONE unified `chat_server_urls` list — the same list chat, image,
    music and video use. There is deliberately no per-feature server setting: a second list is a
    second thing to keep in step, and the node you forgot to add to it fails by quietly never being
    asked. `exclude_self=True` is what stops a node HTTPing its own /api/generate-voice and
    deadlocking on the GPU lock it is already holding."""
    from app.services.load_balancer import parse_server_urls
    return parse_server_urls(raw, exclude_self=True)


async def _rotated(candidates: List[str]) -> List[str]:
    """`candidates` rotated by a global round-robin index so each call starts at a different node.
    The stored index advances by 1 modulo a LARGE constant, not `% len(candidates)` — doing the
    modulo against the list length means the index resets whenever the list changes size, and every
    request lands back on node 0."""
    global _rr_index
    if len(candidates) <= 1:
        return candidates
    async with _rr_lock:
        start = _rr_index % len(candidates)
        _rr_index = (_rr_index + 1) % 1_000_000
    return candidates[start:] + candidates[:start]


def _cfg() -> dict:
    s = settings_store.all_settings()
    def _i(k, d):
        try:
            return int(float(s.get(k) or d))
        except (TypeError, ValueError):
            return d
    return {
        "enabled": str(s.get("voice_enabled", "false")).lower() in ("1", "true", "yes", "on"),
        "device": (s.get("voice_device") or "auto").strip(),
        # The unified list, exactly like music/video/image — voice has no server list of its own.
        "servers": other_nodes(s.get("chat_server_urls", "")),
        "timeout": _i("voice_timeout", 600000) / 1000.0,
    }


async def _generate_local(db: Session, text: str, reference_path: str) -> bytes:
    """Generate on THIS node under the shared GPU lock + VRAM swap."""
    from app.services.locks import GPUResourceLock
    from app.services.vram_manager import prepare_for_voice
    from app.services import voice_local

    global _queued
    cpu_mode = _cfg()["device"] == "cpu"
    async with _queue_lock:
        _queued += 1
    try:
        async with GPUResourceLock("Voice", f"text={len(text or '')} chars", cpu_mode=cpu_mode):
            prepare_for_voice(db)
            svc = voice_local.get_voice_service()
            # to_thread, not the event loop: generate() is a blocking multi-second torch call, and
            # running it inline would freeze every other request on the single uvicorn worker for its
            # whole duration — the same trap the 90s synchronous music-server poll fell into.
            return await asyncio.to_thread(svc.generate, db, text, reference_path)
    finally:
        async with _queue_lock:
            _queued = max(0, _queued - 1)


async def _generate_on_node(node_url: str, text: str, reference: bytes, timeout: float) -> bytes:
    """Ask another node to generate. The reference clip travels WITH the request — the other node has
    no access to this user's Blossom drive, and re-deriving it there would need their storage key."""
    async with httpx.AsyncClient(timeout=timeout) as client:
        r = await client.post(
            f"{node_url}/api/generate-voice",
            files={"reference": ("ref.wav", reference, "audio/wav")},
            data={"text": text},
        )
        if r.status_code == 404:
            # Not an error: that node simply doesn't have voice installed/enabled. Caller moves on.
            raise LookupError(f"{node_url} has no voice endpoint")
        r.raise_for_status()
        return r.content


async def is_busy(node_url: str, timeout: float = 3.0) -> Optional[bool]:
    """Ask a node whether its GPU is already occupied, so we can prefer an idle one. None = can't
    tell (unreachable / old build), which callers must treat as "try it anyway" rather than "skip" —
    an unreachable status endpoint is not evidence of a busy GPU."""
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            r = await client.get(f"{node_url}/api/generate-voice/status")
            if r.status_code != 200:
                return None
            return bool(r.json().get("busy"))
    except Exception:
        return None


async def generate_voice(db: Session, text: str, reference: bytes,
                         reference_path: Optional[str] = None) -> Tuple[bytes, str]:
    """Speak `text` in the reference voice. Returns (wav_bytes, where) where `where` is "local" or the
    node URL, so the caller can tell the user which box did the work.

    `reference` is the clip's bytes (needed to forward to another node); `reference_path` is the same
    clip already on local disk, which the local path uses directly rather than writing it twice.
    """
    cfg = _cfg()
    if not cfg["enabled"]:
        raise RuntimeError("voice generation is disabled on this node (Admin → Voice)")

    from app.services import voice_local
    local_ok = voice_local.is_available()

    # Candidate list: other nodes first-class alongside ourselves. "local" is a sentinel rather than a
    # URL precisely so we can never HTTP to ourselves — a node posting to its own /api/generate-voice
    # would hold the GPU lock while waiting for a request that is queued behind that same lock.
    candidates: List[str] = list(cfg["servers"])
    if local_ok:
        candidates.append("local")
    if not candidates:
        raise RuntimeError(
            "no node can generate voice: this one hasn't got the model installed "
            "(./install.sh --voice) and no other nodes are configured")

    order = await _rotated(candidates)

    # Busy-aware, WITHOUT becoming "always prefer the remote". Three things this gets right that the
    # first cut did not:
    #
    #   * It probes whenever there is ANY remote, not only 2+. The common deployment is exactly one
    #     other node, and skipping the probe there is skipping it in practice: a busy node still won
    #     its turn and the request queued behind a job that can run for minutes.
    #   * It only ever DEMOTES a node it knows is busy — it does not promote idle ones. Promoting
    #     would mean the single remote goes first every time it is free, which is not round-robin any
    #     more; the shared load_balancer is round-robin + health, and voice has no business inventing
    #     a different policy for the same node list.
    #   * It weighs LOCAL on the same scale as a remote. "local" used to be left out of the check
    #     entirely (only `remote` was ever measured), so this node won its turn while its own GPU was
    #     minutes deep in an LLM/image/video job and the request simply blocked on the flock — the
    #     one case the whole busy check exists to prevent. Local can't be asked over HTTP (it is a
    #     sentinel precisely so we never HTTP ourselves), so it is read straight from the same
    #     `gpu_busy()` that image_factory/music_factory/video_factory balance on.
    #
    # `None` (can't tell — unreachable, or an older build with no status endpoint) counts as NOT busy,
    # so an unreachable status endpoint is never mistaken for evidence of a busy GPU.
    remote = [c for c in order if c != "local"]
    busy: set = set()
    if remote:
        try:
            probed = await asyncio.wait_for(
                asyncio.gather(*[is_busy(u) for u in remote], return_exceptions=True), timeout=4.0)
            busy |= {u for u, b in zip(remote, probed) if b is True}
        except Exception:
            pass
    if "local" in order:
        from app.services.locks import gpu_busy
        if gpu_busy():
            busy.add("local")
    # Demote every busy node in one pass, so a busy local lands behind an idle remote rather than
    # merely behind the other busy ones. Guarded on there being somewhere else to go: with a single
    # candidate the partition is a no-op, and announcing a preference for remotes we haven't got
    # would be a lie in the log.
    if busy and len(order) > 1:
        order = [c for c in order if c not in busy] + [c for c in order if c in busy]
        logger.info("[voice] busy → deferring %s, preferring %s",
                    ",".join(sorted(busy)), order[0])

    errors = []
    for node in order:
        try:
            if node == "local":
                if reference_path is None:
                    raise RuntimeError("no local copy of the reference clip")
                return await _generate_local(db, text, reference_path), "local"
            data = await _generate_on_node(node, text, reference, cfg["timeout"])
            if data:
                return data, node
            errors.append(f"{node}: empty response")
        except LookupError as e:
            logger.info("[voice] %s", e)
            errors.append(str(e))
        except Exception as e:
            logger.warning("[voice] %s failed: %s", node, e)
            errors.append(f"{node}: {e}")

    raise RuntimeError("voice generation failed on every node — " + "; ".join(errors[:3]))
