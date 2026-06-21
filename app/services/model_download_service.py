"""On-demand model downloads with status tracking (chat GGUF / image diffusers / music ACE-Step).

Models are NOT auto-downloaded — the admin triggers each from its settings tab, and the UI polls
`status()` so completion and errors are visible. Each download runs in a daemon thread; status is
per-process in-memory. Video is intentionally not covered (the user supplies their own model).
"""
import os
import time
import threading
import logging

logger = logging.getLogger(__name__)

_JOBS: dict = {}          # kind -> {state, message, pct, updated}
_lock = threading.Lock()


def _set(kind: str, state: str, message: str = "", pct=None):
    with _lock:
        _JOBS[kind] = {"state": state, "message": message, "pct": pct, "updated": time.time()}


def status(kind: str) -> dict:
    with _lock:
        return dict(_JOBS.get(kind, {"state": "idle", "message": "", "pct": None}))


# ---------- chat + agentic/tools: download a GGUF by URL ----------
_DEFAULT_GGUF = ("https://huggingface.co/lukey03/Qwen3.5-9B-abliterated-GGUF/resolve/main/"
                 "Qwen3.5-9B-abliterated-Q4_K_M.gguf")
# Known GGUF basenames → download URL (for the agentic/tools model, picked by name in the UI).
_KNOWN_GGUF = {
    "Qwen3-Coder-30B-A3B-Instruct-IQ4_XS.gguf":
        "https://huggingface.co/unsloth/Qwen3-Coder-30B-A3B-Instruct-GGUF/resolve/main/Qwen3-Coder-30B-A3B-Instruct-IQ4_XS.gguf",
    "Qwen3.5-9B-Claude-Code-Q4_K_M.gguf":
        "https://huggingface.co/empero-ai/Qwen3.5-9B-Claude-Code-GGUF/resolve/main/Qwen3.5-9B-Claude-Code-Q4_K_M.gguf",
    "Qwen3.5-9B-abliterated-Q4_K_M.gguf":
        "https://huggingface.co/lukey03/Qwen3.5-9B-abliterated-GGUF/resolve/main/Qwen3.5-9B-abliterated-Q4_K_M.gguf",
}


def _models_dir() -> str:
    from app.services import settings_store
    p = (settings_store.get("llm_model_path", "") or os.environ.get("POSTERCHANAI_LLM_MODEL_PATH", "")).strip()
    return os.path.dirname(p) if p else "/var/lib/posterchanai/models"


def _stream_gguf(kind: str, path: str, url: str):
    import httpx
    if os.path.isfile(path):
        _set(kind, "done", f"Already present: {os.path.basename(path)}")
        return
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".part"
    try:
        # ALWAYS direct — never via the proxy/Tor. trust_env=False so httpx ignores any inherited
        # HTTP(S)_PROXY env (model pulls are big + from HF/CDNs that throttle/block Tor exits).
        with httpx.stream("GET", url, follow_redirects=True, trust_env=False,
                          timeout=httpx.Timeout(60.0, read=None)) as r:
            r.raise_for_status()
            total = int(r.headers.get("content-length") or 0)
            got = 0
            with open(tmp, "wb") as f:
                for chunk in r.iter_bytes(1024 * 512):
                    f.write(chunk)
                    got += len(chunk)
                    _set(kind, "running",
                         (f"{got // (1024*1024)} / {total // (1024*1024)} MB" if total else f"{got // (1024*1024)} MB"),
                         int(got * 100 / total) if total else None)
        os.replace(tmp, path)
        _set(kind, "done", f"Downloaded {os.path.basename(path)}")
    except Exception as e:
        try:
            os.remove(tmp)
        except OSError:
            pass
        _set(kind, "error", str(e)[:300])


def _download_chat(db):
    from app.services import settings_store
    path = (settings_store.get("llm_model_path", "") or os.environ.get("POSTERCHANAI_LLM_MODEL_PATH", "")).strip()
    url = (settings_store.get("llm_model_url", "") or os.environ.get("POSTERCHANAI_MODEL_URL", "") or _DEFAULT_GGUF).strip()
    if not path:
        path = _models_dir().rstrip("/") + "/" + url.split("/")[-1].split("?")[0]
    _stream_gguf("chat", path, url)


def _download_tools(db):
    """Agentic / tools model — a GGUF basename (e.g. Qwen3-Coder-30B…) in the models dir."""
    from app.services import settings_store
    name = (settings_store.get("llm_tools_model", "") or "Qwen3-Coder-30B-A3B-Instruct-IQ4_XS.gguf").strip()
    path = name if os.path.isabs(name) else (_models_dir().rstrip("/") + "/" + name)
    url = (settings_store.get("llm_tools_model_url", "") or _KNOWN_GGUF.get(os.path.basename(name), "")).strip()
    if not url:
        _set("tools", "error",
             f"No known download URL for '{os.path.basename(name)}'. Set the Agentic/Tools Model to a known "
             f"GGUF (Qwen3-Coder-30B-A3B-Instruct-IQ4_XS.gguf or Qwen3.5-9B-Claude-Code-Q4_K_M.gguf), set "
             f"llm_tools_model_url, or place the file manually.")
        return
    _stream_gguf("tools", path, url)


# ---------- image: load the diffusers model(s) — loading downloads them. ROCm/style switching uses
# TWO models (the base image_model_path + the optional image_anime_model_path), so fetch both. ----------
def _download_image(db):
    from app.services.diffusers_service import get_diffusers_service
    from app.services.vram_manager import prepare_for_image
    prepare_for_image(db)   # free the LLM first so the model fits on a shared GPU
    svc = get_diffusers_service(db)
    _set("image", "running", "downloading + loading the base image model…")
    svc._ensure_model_loaded()
    anime = (getattr(svc, "anime_model_path", "") or "").strip()
    if anime:
        _set("image", "running", "downloading + loading the anime model…")
        svc._ensure_model_loaded(anime)
    _set("image", "done", "Image model(s) ready." + (" (base + anime)" if anime else ""))


# ---------- music: warm up ACE-Step so it downloads its model now ----------
def _download_music(db):
    import asyncio
    from app.services import music_service
    from app.services.locks import GPUResourceLock
    from app.services.vram_manager import prepare_for_music
    cfg = music_service.get_settings(db)
    base = cfg.get("base_url") or music_service.DEFAULT_BASE_URL
    body = music_service.build_request_body(cfg, "ambient test tone", "", duration=10, steps=4)
    _set("music", "running", "warming up ACE-Step (downloads the model on first run)…")

    async def _run():
        async with GPUResourceLock("Music", "model download", cpu_mode=(cfg.get("device") == "cpu")):
            prepare_for_music(db)
            await music_service.generate_once(base, body, timeout=1800.0, fmt=cfg.get("fmt", "mp3"))
    asyncio.run(_run())
    _set("music", "done", "Music model ready.")


_FNS = {"chat": _download_chat, "tools": _download_tools, "image": _download_image, "music": _download_music}


def start(kind: str, db_factory) -> bool:
    """Kick off (or no-op if already running) the download for `kind`. db_factory = SessionLocal."""
    if kind not in _FNS:
        _set(kind, "error", "unknown model kind")
        return False
    if status(kind).get("state") == "running":
        return True
    _set(kind, "running", "starting…")

    def _worker():
        db = db_factory()
        try:
            _FNS[kind](db)
        except Exception as e:
            logger.warning("[model-download] %s failed: %s", kind, e)
            _set(kind, "error", str(e)[:300])
        finally:
            try:
                db.close()
            except Exception:
                pass

    threading.Thread(target=_worker, name=f"model-dl-{kind}", daemon=True).start()
    return True
