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


def _run_sync(coro):
    """Run one coroutine to completion from a download thread. Safe for a self-contained awaitable
    (an httpx call that builds its own client); the GPU lock is deliberately NOT taken this way —
    see GPUResourceLockSync, an asyncio.Lock cannot cross event loops."""
    import asyncio
    return asyncio.run(coro)


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
    from app.services.locks import GPUResourceLockSync
    from app.services.vram_manager import prepare_for_image
    # UNDER THE GPU LOCK. This runs in a download THREAD, so it needs the sync twin — without any
    # lock, pressing Download while a song/clip/chat was generating ran prepare_for_image (which
    # unloads the LLM/music/video model) and loaded the image model onto the GPU underneath the
    # in-flight task.
    with GPUResourceLockSync("Image", "model download"):
        prepare_for_image(db)   # free the LLM first so the model fits on a shared GPU
        svc = get_diffusers_service(db)
        _set("image", "running", "downloading + loading the base image model…")
        svc._ensure_model_loaded()
        anime = (getattr(svc, "anime_model_path", "") or "").strip()
        if anime:
            _set("image", "running", "downloading + loading the anime model…")
            svc._ensure_model_loaded(anime)
    _set("image", "done", "Image model(s) ready." + (" (base + anime)" if anime else ""))


# ---------- music: fetch the ACE-Step weights (and prove they load) ----------
def _download_music(db):
    """Fetch the model, then load it once so a failure surfaces HERE rather than on someone's first
    song. Music is in-process now (upstream's `AceStepHandler` — NOT diffusers' AceStepPipeline,
    which no published checkpoint can satisfy), so this no longer pokes a REST server — that path is
    kept only for a node still pointed at one via music_api_base."""
    from app.services import music_service, music_local
    from app.services.locks import GPUResourceLockSync
    from app.services.vram_manager import prepare_for_music
    cfg = music_service.get_settings(db)
    # Decide the SAME way music_factory._generate_local does, or the button warms a path generation
    # never takes: with music_native off it would snapshot_download a checkpoint nothing loads while
    # songs are actually served by the external ACE-Step server.
    from app.services.vram_manager import _native_music_active
    if not _native_music_active():
        body = music_service.build_request_body(cfg, "ambient test tone", "", duration=10, steps=4)
        base = cfg.get("base_url") or music_service.DEFAULT_BASE_URL
        _set("music", "running", "warming up the external ACE-Step server…")
        # We are in a download THREAD. The old spelling wrapped this in `asyncio.run()` to reach the
        # async lock, but `_gpu_lock_base` is bound to the MAIN event loop — awaiting it from a
        # second loop attaches futures to the wrong loop instead of excluding anything. Take the
        # cross-process file lock directly, which is what actually serialises against generations.
        with GPUResourceLockSync("Music", "model download", cpu_mode=(cfg.get("device") == "cpu")):
            prepare_for_music(db)
            _run_sync(music_service.generate_once(base, body, timeout=1800.0, fmt=cfg.get("fmt", "mp3")))
        _set("music", "done", "Music model ready (external server).")
        return

    model_id = music_local._get_settings(db)["model"]
    # NO snapshot_download here. `music_model` is a CHECKPOINT DIRECTORY NAME under
    # <ACESTEP_ROOT>/checkpoints (see music_local.DEFAULT_MODEL) — passing it to the Hub as a repo id
    # made this button fail 100% of the time with "Repository Not Found for
    # .../models/acestep-v15-turbo" (a 401, so it read as an auth problem) on a node whose weights
    # were sitting on disk and generating songs fine. Upstream's handler fetches whatever is missing
    # into that directory on first use, so LOADING is the download — and the one step worth timing
    # out on. Loading is also the only thing that proves the checkpoint is usable on this GPU.
    #
    # Load once, under the lock, so an OOM or a backend incompatibility is reported here. The lock is
    # REAL now: this said "under the lock" while taking none, so verifying the download loaded a
    # second multi-GB model onto a GPU that a song or chat was already using. The old code fetched
    # outside the lock to avoid holding the GPU through a long download — but the handler couples
    # fetch and load, and that split is what let two models land on one GPU. Serialising wins: this
    # is a one-time admin action, and a first-run fetch happens on a node nobody is chatting on yet.
    _set("music", "running", f"downloading + loading {model_id} on this GPU (first run fetches several GB)…")
    svc = music_local.get_music_service(db)
    try:
        with GPUResourceLockSync("Music", "model download", cpu_mode=(cfg.get("device") == "cpu")):
            prepare_for_music(db)
            try:
                svc.load_model(db)
            finally:
                # Unload INSIDE the lock — freeing VRAM after releasing it would race the next
                # generation, which may already have loaded its own model.
                try:
                    svc.unload_model()      # don't hold VRAM just because someone pressed Download
                except Exception:
                    pass
    except Exception as e:
        _set("music", "error", f"loading failed: {e}")
        return
    _set("music", "done", "Music model downloaded and verified.")


def _download_voice(db):
    """Fetch the voice-cloning weights (~6GB), then prove they load on THIS box.

    Deliberately downloads on the CPU and takes NO GPU lock. That is the difference from the music
    button: chatterbox's fetch-and-load is separable, so the expensive part (pulling several GB over
    the network) can happen while someone else is chatting or rendering, instead of holding the GPU
    for the length of a download. Loading on the CPU still proves the checkpoint is intact and that
    the pinned torch/transformers can construct the model — the two things that actually go wrong —
    without ever putting a second model on a shared card.

    It also proves the portability fix: from_pretrained goes through voice_local, which forces a CPU
    map_location, so a node that would have died on CUDA-tagged storages reports success here.
    """
    from app.services import voice_local
    if not voice_local.is_available():
        _set("voice", "error", "chatterbox-tts isn't installed on this node — run ./install.sh --voice")
        return
    model_id = voice_local._get_settings(db)["model"]
    if voice_local.is_downloaded():
        _set("voice", "running", f"{model_id} already cached — verifying it loads…")
    else:
        _set("voice", "running", f"downloading {model_id} (~6GB on first run)…")
    try:
        info = voice_local.download_model(db, model_id)
    except Exception as e:
        _set("voice", "error", f"{e}"[:300])
        return
    mb = int(info.get("bytes", 0) / (1024 * 1024))
    _set("voice", "done", f"Voice model ready ({mb} MB cached, loads cleanly).")


_FNS = {"chat": _download_chat, "tools": _download_tools, "image": _download_image,
        "music": _download_music, "voice": _download_voice}


def _no_ai_build() -> str:
    """Why this node must not fetch weights, or "" when it may.

    The nostr-only image installs requirements-nostr.txt — no llama-cpp, no torch, no diffusers, no
    onnxruntime. Every download here would therefore land several GB on the data volume that nothing
    in the container can load, and the admin panel is NOT gated by nostr-only mode, so its
    "Download chat model" button is right there on a build whose entire point is not having one.

    `PC_ACCEL=nostr` is a BUILD fact baked into the image (true even under a bare `docker run`);
    `POSTERCHANAI_NOSTR_ONLY` is the operator asking for a Nostr-only node. Refused with a sentence
    rather than silently — a button that does nothing is worse than one that says why. The same two
    signals gate the entrypoint's pre-fetches (docker-entrypoint.sh, PC_WANT_MODELS).
    """
    if (os.getenv("PC_ACCEL", "") or "").strip().lower() == "nostr":
        return ("This is a Nostr-only build — it ships no AI stack (no llama-cpp/torch/diffusers), "
                "so the weights could not be loaded. Use an AI image (cpu/cuda/rocm/intel) instead.")
    if (os.getenv("POSTERCHANAI_NOSTR_ONLY", "0") or "").strip().lower() in ("1", "true", "yes", "on"):
        return ("This node runs in Nostr-only mode, where the AI features are switched off — "
                "downloading model weights here would use the disk and reach nothing.")
    return ""


def start(kind: str, db_factory) -> bool:
    """Kick off (or no-op if already running) the download for `kind`. db_factory = SessionLocal."""
    if kind not in _FNS:
        _set(kind, "error", "unknown model kind")
        return False
    _blocked = _no_ai_build()
    if _blocked:
        _set(kind, "error", _blocked)
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
