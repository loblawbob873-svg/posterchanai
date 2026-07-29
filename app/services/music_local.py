"""Native (in-process) music generation — ACE-Step on the SAME torch stack as image and video gen.

ACTIVE BY DEFAULT (`music_native`, default on). It loads through ACE-Step's OWN `AceStepHandler`,
NOT diffusers' `AceStepPipeline`: that pipeline class exists in diffusers, but `from_pretrained`
wants a `model_index.json` no published ACE-Step repo carries, so it 404s. That 404 is what once
sent music back to a per-node sidecar over HTTP — the wrong conclusion, because the released weights
load fine through upstream's handler, which is the very code that sidecar was running. Importing it
instead of talking HTTP to it is the whole point of this module.

Deliberately mirrors video_service: module-level singleton, `_load_lock` around load/unload, an idle
monitor that unloads after `music_idle_timeout`, and an in-flight counter so the monitor can't unload
mid-generation. Callers must still hold the shared GPUResourceLock (music_factory does) — that lock
is the QUEUE that keeps chat/image/music/video to one GPU task at a time.

Audio out: ACE-Step encodes the file itself (mp3/wav/flac via `GenerationConfig.audio_format`), so we
return its bytes untouched — there is no WAV round-trip and nothing to transcode here.
"""
import gc
import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Optional, Tuple

from sqlalchemy.orm import Session

from app.services import settings_store

logger = logging.getLogger(__name__)

def _resolve_acestep_root() -> str:
    """Where ACE-Step is checked out; `<root>/checkpoints` holds the weights.

    This used to default to the literal string "/home/verita84/ACE-Step-1.5" — one developer's home
    directory — so it resolved correctly only on a node whose Linux user happened to share that
    name, and every other bare-metal install pointed the handler at a path that does not exist.
    Docker sets ACESTEP_ROOT=/opt/ace-step; `install.sh --music` clones to
    ${ACESTEP_DIR:-$HOME/ACE-Step-1.5} and exports nothing, so honour BOTH spellings and otherwise
    take the first checkout that is actually on disk."""
    for env in ("ACESTEP_ROOT", "ACESTEP_DIR"):
        v = (os.environ.get(env) or "").strip()
        if v:
            return v
    home_default = os.path.join(os.path.expanduser("~"), "ACE-Step-1.5")
    for cand in (home_default, "/opt/ace-step"):
        if os.path.isdir(cand):
            return cand
    return home_default


_ACESTEP_ROOT = _resolve_acestep_root()
# A CHECKPOINT DIRECTORY NAME under <ACESTEP_ROOT>/checkpoints, not a Hugging Face repo id. The
# handler resolves it locally; "ACE-Step/Ace-Step1.5" (the HF id the diffusers attempt used) is not
# a thing it can load.
DEFAULT_MODEL = "acestep-v15-turbo"
DEFAULT_IDLE_TIMEOUT = 600          # seconds; free the VRAM when nobody is making songs
_SAMPLE_RATE_FALLBACK = 48000

_instance: Optional["MusicService"] = None
_executor = ThreadPoolExecutor(max_workers=1)   # one GPU task at a time (the GPU lock enforces it too)
_load_lock = threading.Lock()
_idle_thread: Optional[threading.Thread] = None
_idle_stop = threading.Event()
_available: Optional[bool] = None   # memoised is_available() probe


class MusicLocalError(Exception):
    pass


def _get_settings(db: Session) -> dict:
    s = settings_store

    def _i(k, d):
        try:
            return int(float(s.get(k, d)))
        except Exception:
            return int(d)

    def _f(k, d):
        try:
            return float(s.get(k, d))
        except Exception:
            return float(d)

    return {
        # Blank `music_model` means "the built-in default" — it used to mean "whatever the REST
        # server was configured with", which nobody could see from here.
        "model": (s.get("music_model", "") or "").strip() or DEFAULT_MODEL,
        "device": s.get("music_gpu_device", "auto") or "auto",
        "cpu_offload": str(s.get("music_cpu_offload", "false")).lower() == "true",
        "steps": _i("music_default_steps", 8),        # ACE-Step 1.5 turbo is an 8-step model
        "guidance": _f("music_guidance", 7.5),
        # `music_default_duration` — the SAME key music_service.get_settings reads for the HTTP path,
        # and the one Admin → Music writes. Reading a private `music_duration` here (which no schema
        # defines, so it never holds a value) silently pinned every native song to the fallback and
        # ignored the admin's setting — a regression the external path never had.
        "duration": _f("music_default_duration", 180.0),
        "idle_timeout": _i("music_idle_timeout", DEFAULT_IDLE_TIMEOUT),
    }


def _idle_loop():
    while not _idle_stop.wait(30):
        inst = _instance
        if inst and inst._pipe is not None and inst._last_used:
            if time.time() - inst._last_used > inst._idle_timeout:
                logger.info("Music model idle — unloading to free VRAM")
                inst.unload_model(skip_if_generating=True)


def _start_idle():
    global _idle_thread
    if _idle_thread is None or not _idle_thread.is_alive():
        _idle_stop.clear()
        _idle_thread = threading.Thread(target=_idle_loop, daemon=True, name="music-idle")
        _idle_thread.start()


class MusicService:
    def __init__(self):
        self._pipe = None
        self._model_id: Optional[str] = None
        self._device: Optional[str] = None
        self._sample_rate = _SAMPLE_RATE_FALLBACK
        self._idle_timeout = DEFAULT_IDLE_TIMEOUT
        self._last_used = 0.0
        # In-flight counter — stops the idle monitor unloading the model mid-song. Incremented
        # BEFORE contending for _load_lock, which is what closes the check-then-act race.
        self._generating = 0
        _start_idle()

    def is_loaded(self) -> bool:
        return self._pipe is not None

    def _resolve_device(self, device_setting: str) -> str:
        from app.services.diffusers_service import detect_device
        import torch
        if device_setting == "auto":
            return detect_device()
        if device_setting == "cuda" and not torch.cuda.is_available():
            return detect_device()
        if device_setting == "xpu" and not (hasattr(torch, "xpu") and torch.xpu.is_available()):
            return detect_device()
        return device_setting

    def load_model(self, db: Session):
        cfg = _get_settings(db)
        self._idle_timeout = cfg["idle_timeout"]
        model_id = cfg["model"]
        with _load_lock:
            if self._pipe is not None and self._model_id == model_id:
                return
            if self._pipe is not None:
                self._unload_internal()   # already holding _load_lock; unload_model() would deadlock
            device = self._resolve_device(cfg["device"])
            self._device = device
            logger.info(f"[music] loading {model_id} on {device} ...")
            t0 = time.time()
            # ACE-Step's OWN handler, not diffusers' AceStepPipeline. The pipeline CLASS exists in
            # diffusers, but from_pretrained looks for model_index.json first and NO published
            # ACE-Step repo carries one (checked Ace-Step1.5, acestep-v15-xl-*, ACE-Step-v1-3.5B and
            # the Comfy mirror) — that 404 is what forced music back onto an external server. The
            # weights ARE loadable, just through the upstream handler, which is the same code that
            # server ran. Importing it instead of talking HTTP to it is the whole point.
            from acestep.handler import AceStepHandler
            handler = AceStepHandler()
            msg, ok = handler.initialize_service(
                project_root=_ACESTEP_ROOT,
                config_path=model_id,
                device="auto" if device != "cpu" else "cpu",
                # SDPA only — flash-attn is not portable to Arc/ROCm (same rule as image + video).
                use_flash_attention=False,
                compile_model=False,
                # accelerate's offload hooks are CUDA-only (meta-tensor bug on XPU), so the Arc has
                # to fit the model outright; nas may offload.
                offload_to_cpu=bool(cfg.get("cpu_offload")) and device != "xpu",
                offload_dit_to_cpu=False,
            )
            if not ok:
                raise MusicLocalError(f"music model failed to load: {str(msg)[:200]}")
            self._pipe = handler
            self._model_id = model_id
            self._sample_rate = _SAMPLE_RATE_FALLBACK
            logger.info(f"[music] loaded in {time.time()-t0:.1f}s (sample_rate={self._sample_rate})")

    def unload_model(self, skip_if_generating: bool = False):
        with _load_lock:
            if skip_if_generating and self._generating > 0:
                return
            self._unload_internal()

    def _unload_internal(self):
        """Caller MUST hold _load_lock (load_model reuses it; the lock is not reentrant)."""
        if self._pipe is None:
            return
        # AceStepHandler is a plain object, NOT an nn.Module — it has no `.to()`. Calling one (as
        # this used to) raised AttributeError straight into a bare except and freed exactly nothing,
        # leaving several GB of DiT+VAE resident at the precise moment we swap to the LLM/image/video
        # model — an OOM on the shared 12/16GB GPUs the swap exists to protect. The weights hang off
        # these attributes, so drop them explicitly, then let upstream's own reclaim helper run.
        for attr in ("model", "vae", "text_encoder", "mlx_decoder", "mlx_vae", "silence_latent"):
            try:
                if getattr(self._pipe, attr, None) is not None:
                    setattr(self._pipe, attr, None)
            except Exception:
                pass
        try:
            self._pipe._release_system_memory()
        except Exception:
            pass
        self._pipe = None
        self._model_id = None
        gc.collect()
        try:
            import torch
            if self._device == "cuda" and torch.cuda.is_available():
                torch.cuda.empty_cache()
            elif self._device == "xpu" and hasattr(torch, "xpu") and torch.xpu.is_available():
                torch.xpu.empty_cache()
        except Exception:
            pass
        from app.services.vram_manager import reset_vram_mode
        reset_vram_mode()
        logger.info("[music] model unloaded")

    def generate(self, db: Session, prompt: str, lyrics: str = "", duration: Optional[float] = None,
                 steps: Optional[int] = None, guidance: Optional[float] = None,
                 fmt: str = "mp3") -> Tuple[bytes, str]:
        """Render one song. Returns (bytes, ext) — a bare extension like "mp3", NOT a MIME type;
        consumers build f"song.{ext}". BLOCKING — call it through `generate_async` so the event loop
        keeps serving requests."""
        cfg = _get_settings(db)
        self._generating += 1
        try:
            self.load_model(db)
            if self._pipe is None:
                raise MusicLocalError("music model failed to load")
            import tempfile, shutil, os as _os
            from acestep.inference import generate_music, GenerationParams, GenerationConfig
            dur = float(duration or cfg["duration"])
            n_steps = int(steps or cfg["steps"])
            gscale = float(guidance if guidance is not None else cfg["guidance"])
            logger.info(f"[music] generating {dur:.0f}s, {n_steps} steps — {prompt[:60]!r}")
            t0 = time.time()
            # thinking=False: our own LLM already wrote the lyrics/style (see _music_write_lyrics),
            # so ACE-Step's chain-of-thought LM is not needed and llm_handler stays None — that is
            # what keeps this to ONE model on the GPU instead of two.
            params = GenerationParams(
                caption=prompt or "", lyrics=lyrics or "", instrumental=not (lyrics or "").strip(),
                duration=dur, inference_steps=n_steps, guidance_scale=gscale, thinking=False,
            )
            gconf = GenerationConfig(batch_size=1, audio_format=(fmt or "mp3").lower())
            out_dir = tempfile.mkdtemp(prefix="pcai_music_")
            try:
                result = generate_music(self._pipe, None, params, gconf, out_dir, None)
                if not getattr(result, "success", False):
                    raise MusicLocalError(f"generation failed: {getattr(result, 'error', '')}"[:200])
                paths = [a.get("path") or a.get("file") for a in (result.audios or []) if isinstance(a, dict)]
                paths = [x for x in paths if x and _os.path.exists(x)]
                if not paths:
                    raise MusicLocalError("generation produced no audio")
                data = open(paths[0], "rb").read()
                ext = (_os.path.splitext(paths[0])[1] or ".mp3").lstrip(".").lower()
            finally:
                shutil.rmtree(out_dir, ignore_errors=True)
            logger.info(f"[music] rendered in {time.time()-t0:.1f}s ({len(data)/1e6:.2f} MB)")
            self._last_used = time.time()
            # ACE-Step writes the encoded file itself, so there is nothing to transcode: return its
            # bytes and a bare EXTENSION (consumers build f"song.{ext}"; a MIME type here would
            # produce the path "song.audio/mpeg").
            return data, ext
        finally:
            self._generating -= 1
            self._last_used = time.time()


def get_music_service(db: Session) -> MusicService:
    global _instance
    if _instance is None:
        _instance = MusicService()
    return _instance


async def generate_async(db: Session, prompt: str, lyrics: str = "", duration=None, steps=None,
                         guidance=None, fmt: str = "mp3") -> Tuple[bytes, str]:
    """Off-thread wrapper. The single-worker executor mirrors video_service: even though the shared
    GPUResourceLock already serialises GPU work, a second concurrent load would double VRAM before
    the lock is reached."""
    import asyncio
    svc = get_music_service(db)
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        _executor, lambda: svc.generate(db, prompt, lyrics, duration, steps, guidance, fmt))


def is_available() -> bool:
    """True when THIS process can actually load ACE-Step — i.e. the `acestep` package is installed.

    Probe the package the load path really uses. This used to test diffusers for `AceStepPipeline`,
    which `load_model` never touches, so it was wrong in both directions:
      - acestep installed but an older diffusers → reported UNAVAILABLE, and callers fell back to an
        HTTP sidecar that no longer exists. On a node with `video_free_music` on, that fallback runs
        `vram_manager._ensure_music_server`, which `systemctl start`s the retired unit and then polls
        port 8001 for 90s SYNCHRONOUSLY on the single uvicorn worker — the whole app stalls, per song.
      - acestep NOT installed but diffusers new enough → reported available, then ImportError deep
        inside generate().
    Probes the TOP-LEVEL package only, and memoises: `find_spec("acestep.handler")` would import the
    parent to read its `__path__` (dragging in torch), and `vram_manager._native_music_active` calls
    this on every prepare_for_* swap, so it has to stay cheap.
    """
    global _available
    if _available is None:
        try:
            import importlib.util
            _available = importlib.util.find_spec("acestep") is not None
        except Exception:
            _available = False
    return _available
