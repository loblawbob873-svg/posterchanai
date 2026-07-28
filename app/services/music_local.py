"""Native (in-process) music generation — ACE-Step via diffusers, on the SAME torch stack as image
and video gen.

NOT ACTIVE BY DEFAULT — gated behind the `music_native` setting (default off), because no published
ACE-Step checkpoint is in diffusers format: none carry model_index.json, so from_pretrained 404s
(checked ACE-Step/Ace-Step1.5, acestep-v15-xl-{base,turbo}, ACE-Step-v1-3.5B and the Comfy-Org
mirror, every branch and PR ref). The released weights are a transformers custom-code model
(auto_map -> modeling_acestep_v15_turbo, trust_remote_code) plus a diffusers VAE, so they cannot be
pointed at AceStepPipeline without a weight port. Music is served by the external acestep-api server
(see docs/MUSIC.md); this module is the ready-to-go client for the day an official diffusers
checkpoint ships — flip music_native then.

Deliberately mirrors video_service: module-level singleton, `_load_lock` around load/unload, an idle
monitor that unloads after `music_idle_timeout`, and an in-flight counter so the monitor can't unload
mid-generation. Callers must still hold the shared GPUResourceLock (music_factory does) — that lock
is the QUEUE that keeps chat/image/music/video to one GPU task at a time.

Audio out: the pipeline yields float samples at the VAE's rate (48kHz). We write a WAV with the
STDLIB `wave` module and let ffmpeg (already a hard dependency for every media path) transcode to
mp3 — so this adds NO new Python dependency. soundfile/torchaudio are deliberately not required.
"""
import gc
import io
import logging
import os
import threading
import time
import wave
from concurrent.futures import ThreadPoolExecutor
from typing import Optional, Tuple

from sqlalchemy.orm import Session

from app.services import settings_store

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "ACE-Step/Ace-Step1.5"
DEFAULT_IDLE_TIMEOUT = 600          # seconds; free the VRAM when nobody is making songs
_SAMPLE_RATE_FALLBACK = 48000

_instance: Optional["MusicService"] = None
_executor = ThreadPoolExecutor(max_workers=1)   # one GPU task at a time (the GPU lock enforces it too)
_load_lock = threading.Lock()
_idle_thread: Optional[threading.Thread] = None
_idle_stop = threading.Event()


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
        "duration": _f("music_duration", 60.0),
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
            import torch
            from diffusers import AceStepPipeline
            device = self._resolve_device(cfg["device"])
            self._device = device
            logger.info(f"[music] loading {model_id} on {device} ...")
            t0 = time.time()
            # bf16 everywhere but CPU — same reasoning as video: it halves VRAM and is the dtype the
            # model card ships. nas shares 12GB between music and video, so this is not optional.
            dtype = torch.float32 if device == "cpu" else torch.bfloat16
            pipe = AceStepPipeline.from_pretrained(model_id, torch_dtype=dtype)
            if device != "cpu" and cfg.get("cpu_offload"):
                # CUDA-only: accelerate's offload hooks do not work on XPU (meta-tensor bug), which
                # is why the Arc must fit the model outright.
                pipe.enable_model_cpu_offload(device=device)
            elif device != "cpu":
                pipe = pipe.to(device)
            try:
                pipe.vae.enable_tiling()
            except Exception:
                pass
            self._pipe = pipe
            self._model_id = model_id
            self._sample_rate = int(getattr(pipe, "sample_rate", 0) or _SAMPLE_RATE_FALLBACK)
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
        try:
            self._pipe.to("cpu")
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
        """Render one song. Returns (bytes, content_type). BLOCKING — call it through
        `generate_async` so the event loop keeps serving requests."""
        cfg = _get_settings(db)
        self._generating += 1
        try:
            self.load_model(db)
            if self._pipe is None:
                raise MusicLocalError("music model failed to load")
            import torch
            dur = float(duration or cfg["duration"])
            n_steps = int(steps or cfg["steps"])
            gscale = float(guidance if guidance is not None else cfg["guidance"])
            logger.info(f"[music] generating {dur:.0f}s, {n_steps} steps — {prompt[:60]!r}")
            t0 = time.time()
            with torch.inference_mode():
                out = self._pipe(
                    prompt=prompt,
                    lyrics=lyrics or "",
                    audio_duration=dur,
                    num_inference_steps=n_steps,
                    guidance_scale=gscale,
                    output_type="np",
                )
            audio = out.audios if hasattr(out, "audios") else out[0]
            logger.info(f"[music] rendered in {time.time()-t0:.1f}s")
            wav = _to_wav_bytes(audio, self._sample_rate)
            self._last_used = time.time()
            # Return a bare EXTENSION, not a MIME type: every consumer treats slot 2 as a file
            # suffix (media_service.make_music_video builds f"song.{audio_ext}"), so "audio/mpeg"
            # here yields the path "song.audio/mpeg" -> FileNotFoundError, swallowed by that
            # function's broad except -> the branded video silently degrades to raw audio.
            if fmt and fmt.lower() != "wav":
                enc, _ctype = _transcode(wav, fmt)
                if enc:
                    return enc, fmt.lower()
            return wav, "wav"
        finally:
            self._generating -= 1
            self._last_used = time.time()


def _to_wav_bytes(audio, sample_rate: int) -> bytes:
    """float samples (channels-first or -last, batched or not) -> 16-bit PCM WAV, stdlib only."""
    import numpy as np
    a = np.asarray(audio, dtype=np.float32)
    while a.ndim > 2:                 # drop the batch dim(s)
        a = a[0]
    if a.ndim == 1:
        a = a[None, :]
    # channels-first if the short axis is first (2xN, not Nx2) — ACE-Step returns (channels, samples)
    if a.shape[0] > a.shape[1]:
        a = a.T
    a = np.clip(a, -1.0, 1.0)
    pcm = (a * 32767.0).astype("<i2").T.reshape(-1)    # interleave for WAV
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(int(a.shape[0]))
        w.setsampwidth(2)
        w.setframerate(int(sample_rate or _SAMPLE_RATE_FALLBACK))
        w.writeframes(pcm.tobytes())
    return buf.getvalue()


_CTYPE = {"mp3": "audio/mpeg", "flac": "audio/flac", "ogg": "audio/ogg", "wav": "audio/wav"}


def _transcode(wav_bytes: bytes, fmt: str) -> Tuple[Optional[bytes], str]:
    """WAV -> mp3/flac/ogg via ffmpeg, which every other media path already depends on. Returns
    (None, "") on failure so the caller can fall back to serving the WAV rather than nothing."""
    import subprocess
    import tempfile
    from app.services.media_service import resolve_ffmpeg
    fmt = (fmt or "mp3").lower()
    fd, out_path = tempfile.mkstemp(suffix="." + fmt)
    os.close(fd)
    try:
        p = subprocess.run(
            [resolve_ffmpeg(), "-y", "-hide_banner", "-loglevel", "error",
             "-f", "wav", "-i", "pipe:0", "-b:a", "192k", out_path],
            input=wav_bytes, capture_output=True, timeout=300)
        if p.returncode == 0 and os.path.getsize(out_path) > 0:
            with open(out_path, "rb") as fh:
                return fh.read(), _CTYPE.get(fmt, "application/octet-stream")
        logger.warning("[music] ffmpeg transcode failed (%s): %s", p.returncode, p.stderr[-200:])
    except Exception as e:
        logger.warning("[music] transcode error: %s", e)
    finally:
        try:
            os.unlink(out_path)
        except Exception:
            pass
    return None, ""


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
    """True when the diffusers build actually has ACE-Step. Lets callers fall back to a configured
    REST server instead of failing, on an older diffusers."""
    try:
        import importlib.util
        return importlib.util.find_spec("diffusers") is not None and \
            hasattr(__import__("diffusers", fromlist=["AceStepPipeline"]), "AceStepPipeline")
    except Exception:
        return False
