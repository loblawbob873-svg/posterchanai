"""Music generation — low-level HTTP client for an ACE-Step REST server (`acestep-api`).

ACE-Step 1.5 needs its own Python 3.11–3.12 environment (it conflicts with the main venv) and
ships a REST API, so it runs as a SEPARATE process and the app talks to it over HTTP. This module
is the per-SERVER client (submit → poll → download for ONE server) plus the watermark helper.
The orchestration — GPU lock, VRAM model-swap and load balancing across servers — lives in
`music_factory.py`, mirroring `image_factory`/`image_load_balancer` (the "1 task at a time, swap
models, like we do now" pattern).

REST contract (ACE-Step 1.5, default 127.0.0.1:8001):
  POST /release_task  {prompt, lyrics, audio_duration, inference_steps, model, ...}
                      -> {"data": {"task_id": "...", "status": "queued"}, "code": 200}
  POST /query_result  {"task_ids": ["..."]}
                      -> result containing audio URL(s) like "/v1/audio?path=..."
  GET  /v1/audio?path=...  -> the audio bytes (mp3/wav/flac/...)

Wired for the web UI + Telegram only (not the fedi bots — abuse surface).
"""
import asyncio
import json
import logging
from typing import Optional, Tuple

import httpx
from sqlalchemy.orm import Session

from app.services import settings_store

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "http://localhost:8001"
# Poll the async task endpoint at this cadence until the song is ready.
_POLL_INTERVAL = 2.0


class MusicError(Exception):
    """User-facing music-generation error (disabled, bad config, server error, timeout)."""


def get_settings(db: Session) -> dict:
    rows = settings_store.all_settings()
    return {
        "enabled": (rows.get("music_enabled", "false") or "").lower() == "true",
        # Local ACE-Step server: no admin UI field — auto-seeded from POSTERCHANAI_ACESTEP_URL in
        # Docker (acestep:8001), else the localhost:8001 convention default.
        "base_url": (rows.get("music_api_base", "") or "").strip() or DEFAULT_BASE_URL,
        # The RAW setting: blank means "no external server configured", which is what lets
        # music_factory pick the native in-process pipeline instead of the localhost:8001 default.
        "base_url_explicit": (rows.get("music_api_base", "") or "").strip(),
        # Cross-node LB uses the single unified list (Site → Load Balancing).
        "server_urls": rows.get("chat_server_urls", "") or "",
        "device": (rows.get("music_gpu_device", "auto") or "auto").strip().lower(),
        "model": (rows.get("music_model", "") or "").strip(),
        "duration": _to_float(rows.get("music_default_duration"), 60.0),
        "steps": _to_int(rows.get("music_default_steps"), 8),
        "fmt": (rows.get("music_format", "") or "").strip().lower() or "mp3",
        # music_timeout is in ms (mirrors image_timeout); convert to seconds.
        "timeout": _to_float(rows.get("music_timeout"), 300000.0) / 1000.0,
        "vram_mode": rows.get("vram_mode", "shared") or "shared",
    }


def build_request_body(cfg: dict, prompt: str, lyrics: str = "",
                       duration: Optional[float] = None, steps: Optional[int] = None) -> dict:
    body = {
        "prompt": prompt,
        "lyrics": lyrics or "",
        "audio_duration": duration if duration is not None else cfg["duration"],
        "inference_steps": steps if steps is not None else cfg["steps"],
        "format": cfg["fmt"],
        # One take per request — ACE-Step defaults to 2, which doubles VRAM and can OOM a 12GB GPU.
        "batch_size": 1,
    }
    if cfg["model"]:
        body["model"] = cfg["model"]
    return body


async def generate_once(base_url: str, body: dict, timeout: float, fmt: str = "mp3") -> Tuple[bytes, str]:
    """Submit a generation task to ONE ACE-Step server, poll until ready, download the audio.
    Returns (audio_bytes, ext). Raises MusicError on any failure."""
    base_url = base_url.rstrip("/")
    timeout = max(30.0, timeout)
    async with httpx.AsyncClient(timeout=httpx.Timeout(timeout, connect=15.0)) as client:
        try:
            resp = await client.post(f"{base_url}/release_task", json=body)
        except httpx.RequestError as e:
            logger.warning(f"[music] release_task request error ({base_url}): {e}")
            raise MusicError("Couldn't reach the music server. Is the ACE-Step service running?")
        if resp.status_code >= 400:
            raise MusicError(f"Music server rejected the request (HTTP {resp.status_code}).")
        task_id = _extract_task_id(resp.json())
        if not task_id:
            raise MusicError("Music server didn't return a task id.")

        audio_url = await _poll_for_audio(client, base_url, task_id, timeout)
        if not audio_url:
            raise MusicError("Timed out waiting for the song to render.")

        audio_bytes, ext = await _download_audio(client, base_url, audio_url, fmt)
    if not audio_bytes:
        raise MusicError("Music server returned an empty file.")
    return audio_bytes, ext


# --- helpers ----------------------------------------------------------------

def _to_float(v, default: float) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _to_int(v, default: int) -> int:
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return default


def _extract_task_id(payload) -> Optional[str]:
    if isinstance(payload, dict):
        data = payload.get("data", payload)
        if isinstance(data, dict):
            for k in ("task_id", "taskId", "id"):
                if data.get(k):
                    return str(data[k])
    return None


def _find_audio_path(obj) -> Optional[str]:
    """Recursively pull the first audio URL/path out of an arbitrary query_result payload. The
    exact response shape isn't part of the documented contract, so search defensively for the
    `/v1/audio?path=` download URL (or a key that looks like an audio path/url)."""
    if isinstance(obj, str):
        s = obj.strip()
        # ACE-Step returns the per-task `result` as a JSON-ENCODED STRING; parse and recurse so we
        # reach the inner items' `file` field (an already-formed "/v1/audio?path=..." URL).
        if s[:1] in ("[", "{"):
            try:
                return _find_audio_path(json.loads(s))
            except Exception:
                pass
        if "/v1/audio" in obj or obj.lower().endswith((".mp3", ".wav", ".flac", ".opus", ".aac")):
            return obj
        return None
    if isinstance(obj, dict):
        for k, v in obj.items():
            if str(k).lower() in ("audio_url", "audio_path", "url", "path", "file", "filepath") and isinstance(v, str):
                hit = _find_audio_path(v)
                if hit:
                    return hit
        for v in obj.values():
            hit = _find_audio_path(v)
            if hit:
                return hit
        return None
    if isinstance(obj, (list, tuple)):
        for v in obj:
            hit = _find_audio_path(v)
            if hit:
                return hit
    return None


def _is_done(obj) -> Optional[bool]:
    """Inspect a query_result payload for a terminal status: True (done), False (running), or
    None (unknown — keep polling). Raises MusicError on a failed status."""
    status = None
    if isinstance(obj, dict):
        for k in ("status", "state"):
            if k in obj and isinstance(obj[k], str):
                status = obj[k].lower()
                break
        if status is None:
            for v in obj.values():
                r = _is_done(v)
                if r is not None:
                    return r
    elif isinstance(obj, (list, tuple)):
        for v in obj:               # a terminal status nested in a LIST payload (ACE-Step) — recurse in,
            r = _is_done(v)         # else it's missed → 5-min poll timeout instead of an immediate error
            if r is not None:
                return r
    if status is None:
        return None
    if status in ("success", "succeeded", "completed", "complete", "done", "finished"):
        return True
    if status in ("failed", "error", "cancelled", "canceled"):
        raise MusicError("The music server reported the generation failed.")
    return False


async def _poll_for_audio(client: httpx.AsyncClient, base_url: str, task_id: str, timeout: float) -> Optional[str]:
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        try:
            # ACE-Step's /query_result expects `task_id_list` (a list); a completed task returns an
            # item with a `file` filesystem path.
            r = await client.post(f"{base_url}/query_result", json={"task_id_list": [task_id]})
        except httpx.RequestError as e:
            logger.warning(f"[music] query_result request error: {e}")
            await asyncio.sleep(_POLL_INTERVAL)
            continue
        if r.status_code < 400:
            payload = r.json()
            done = _is_done(payload)  # raises MusicError on a failed status
            url = _find_audio_path(payload)
            if url and done is not False:
                return url
        await asyncio.sleep(_POLL_INTERVAL)
    return None


async def _download_audio(client: httpx.AsyncClient, base_url: str, audio_url: str, fmt: str) -> Tuple[bytes, str]:
    from urllib.parse import quote
    # The found value can be: an absolute URL, a server-relative "/v1/audio?path=..." URL, or a
    # raw filesystem path (ACE-Step's query_result returns the latter in `file`). The last case
    # must be fetched through the /v1/audio?path= endpoint.
    if audio_url.startswith("http"):
        url = audio_url
    elif "/v1/audio" in audio_url:
        url = f"{base_url}{audio_url if audio_url.startswith('/') else '/' + audio_url}"
    else:
        url = f"{base_url}/v1/audio?path={quote(audio_url, safe='')}"
    r = await client.get(url)
    if r.status_code >= 400 or not r.content:
        raise MusicError(f"Couldn't download the rendered song (HTTP {r.status_code}).")
    ext = fmt
    for cand in ("mp3", "wav", "flac", "opus", "aac"):
        if audio_url.lower().endswith("." + cand):
            ext = cand
            break
    return r.content, ext
