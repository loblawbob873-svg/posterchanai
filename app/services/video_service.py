"""Native text-to-video generation service (diffusers Wan2.1), mirroring diffusers_service.

Like image gen, this runs IN-PROCESS on the same torch stack (CUDA / Intel XPU / ROCm) — NOT a
separate HTTP server (that's how music/ACE-Step works, because ACE-Step needs a conflicting torch).
LTX/Wan/CogVideoX are stock `diffusers` pipelines, so they share this venv, the shared
`GPUResourceLock`, and the `vram_manager` model-swap. The factory (`video_factory.py`) owns the
load-balancing + GPU lock; this module is just the generator (load → generate frames → idle-unload).

Portability rule (must run on Arc XPU + CUDA + ROCm): stay on stock diffusers + torch SDPA — NO
flash-attn / xformers / fp8 / GGUF (CUDA-pinned, break Arc/ROCm).

ARC (XPU) GOTCHA: the Wan VAE decode (`conv3d`) OOMs the level_zero allocator when the VAE is fp32.
FIX (applied below): load the VAE in bf16 + `vae.enable_tiling()`. The A770's 16GB is also tight,
so keep frames/resolution modest there (see video_* settings).
"""
import gc
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import List, Optional

import numpy as np
from sqlalchemy.orm import Session

logger = logging.getLogger("video_service")
logger.setLevel(logging.INFO)
logger.propagate = False
if not logger.handlers:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter('%(asctime)s [VIDEO] %(message)s', datefmt='%Y-%m-%d %H:%M:%S'))
    logger.addHandler(handler)

DEFAULT_IDLE_TIMEOUT = 300  # unload the (large) video model after 5 min idle

_instance: Optional["VideoService"] = None
_executor = ThreadPoolExecutor(max_workers=1)  # one GPU task at a time (the GPU lock enforces this too)
_load_lock = threading.Lock()
_idle_thread: Optional[threading.Thread] = None
_idle_stop = threading.Event()


class VideoError(Exception):
    """User-facing video-generation error (disabled, bad config, OOM, runtime error)."""


def _get_settings(db: Session) -> dict:
    from app.database import safe_query_settings
    s = safe_query_settings(db)
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
        "enabled": str(s.get("video_enabled", "false")).lower() == "true",
        "local_enabled": str(s.get("video_local_enabled", "true")).lower() == "true",
        "model": s.get("video_model", "Wan-AI/Wan2.1-T2V-1.3B-Diffusers") or "Wan-AI/Wan2.1-T2V-1.3B-Diffusers",
        "device": s.get("video_gpu_device", "auto") or "auto",
        "cpu_offload": str(s.get("video_cpu_offload", "false")).lower() == "true",
        "width": _i("video_width", 832),
        "height": _i("video_height", 480),
        "num_frames": _i("video_num_frames", 49),
        "max_frames": _i("video_max_frames", 81),  # hard ceiling — Wan1.3B tops out ~81 (5s) and
        "fps": _i("video_fps", 16),               # 16GB OOMs beyond that; clamp to avoid the footgun
        "steps": _i("video_default_steps", 25),
        "guidance": _f("video_guidance", 5.0),
        "idle_timeout": _i("video_idle_timeout", DEFAULT_IDLE_TIMEOUT),
    }


def _idle_loop():
    while not _idle_stop.wait(30):
        inst = _instance
        if inst and inst._pipe is not None and inst._last_used:
            # Never unload mid-generation: a Wan run can outlast _idle_timeout and `_last_used`
            # only advances when a run COMPLETES, so it goes stale during a long generation and
            # would trick the monitor into unloading the active pipe. Mirrors the image/LLM guard.
            # The pre-check is the fast path; unload_model(skip_if_generating=True) re-checks under
            # _load_lock to close the check-then-act window (a gen starting right after this check).
            if inst._generating > 0:
                continue
            if time.time() - inst._last_used > inst._idle_timeout:
                logger.info("Idle timeout reached — unloading video model")
                inst.unload_model(skip_if_generating=True)


def _start_idle():
    global _idle_thread
    if _idle_thread is None or not _idle_thread.is_alive():
        _idle_stop.clear()
        _idle_thread = threading.Thread(target=_idle_loop, daemon=True)
        _idle_thread.start()


class VideoService:
    def __init__(self):
        self._pipe = None
        self._model_id: Optional[str] = None
        self._device: Optional[str] = None
        self._idle_timeout = DEFAULT_IDLE_TIMEOUT
        self._last_used = 0.0
        # In-flight generation counter — keeps the idle monitor from unloading the model while a
        # (long) generation is running. See _idle_loop. Same guard as diffusers/llama services.
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
                self._unload_internal()  # already holding _load_lock; unload_model() would deadlock
            import torch
            from diffusers import DiffusionPipeline
            device = self._resolve_device(cfg["device"])
            self._device = device
            logger.info(f"Loading video model {model_id} on {device} ...")
            t0 = time.time()
            # GENERIC loader: DiffusionPipeline.from_pretrained auto-selects the right pipeline class
            # from the model's config (Wan / LTX / CogVideoX / …), so `video_model` can be ANY
            # diffusers text-to-video model. Everything (incl. the VAE) loads in bf16 — that's the
            # Arc fp32-conv3d OOM fix, and it halves VRAM everywhere. CPU stays fp32.
            dtype = torch.float32 if device == "cpu" else torch.bfloat16
            pipe = DiffusionPipeline.from_pretrained(model_id, torch_dtype=dtype)
            if device != "cpu" and cfg.get("cpu_offload"):
                # For big models (e.g. CogVideoX-5B) that don't fit fully in VRAM: keep weights in
                # system RAM and stream layers to the GPU on demand. Slower, but fits.
                pipe.enable_model_cpu_offload(device=device)
            elif device != "cpu":
                pipe = pipe.to(device)
            # Tiled VAE decode keeps peak memory in budget (REQUIRED on the 16GB Arc / 12GB nas).
            try:
                pipe.vae.enable_tiling()
            except Exception:
                pass
            self._pipe = pipe
            self._model_id = model_id
            logger.info(f"Video model loaded in {time.time() - t0:.0f}s")

    def unload_model(self, skip_if_generating: bool = False):
        # skip_if_generating (idle monitor): re-check the in-flight counter UNDER the lock and skip
        # if a generation is active. A generation increments `_generating` before it contends for
        # `_load_lock`, so this provably closes the idle loop's check-then-act race.
        with _load_lock:
            if skip_if_generating and self._generating > 0:
                return
            self._unload_internal()

    def _unload_internal(self):
        """Unload the pipe. Caller MUST hold _load_lock (so load_model can reuse it without
        re-acquiring the non-reentrant lock — calling unload_model() there would deadlock)."""
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
        logger.info("Video model unloaded")

    def generate(self, db: Session, prompt: str, negative_prompt: str = "",
                 width: Optional[int] = None, height: Optional[int] = None,
                 num_frames: Optional[int] = None, steps: Optional[int] = None,
                 guidance: Optional[float] = None) -> tuple:
        """Generate a clip. Returns (frames: list[np.uint8 HxWx3], fps:int). Blocking — call via the
        factory's thread + GPU lock."""
        cfg = _get_settings(db)
        # Bracket the whole run (model load + diffusion) so the idle monitor won't unload the
        # pipe mid-generation. _last_used only advances on completion, so it can't protect the run.
        self._generating += 1
        try:
            self.load_model(db)
            import torch
            # Wan needs width/height divisible by 16 and num_frames = 4k+1.
            def _r16(v):
                v = int(v); return max(16, v - (v % 16))
            w = _r16(width or cfg["width"])
            h = _r16(height or cfg["height"])
            nf = int(num_frames or cfg["num_frames"])
            # Clamp to the model/VRAM ceiling so an over-large frame count fails fast & clearly instead
            # of OOMing the GPU (Wan1.3B is a ~5s/81-frame model; 16GB can't hold more).
            max_nf = max(5, int(cfg["max_frames"]))
            if nf > max_nf:
                logger.warning(f"num_frames {nf} exceeds max {max_nf}; clamping (model/VRAM ceiling)")
                nf = max_nf
            nf = max(5, nf - ((nf - 1) % 4))
            st = int(steps or cfg["steps"])
            gs = float(guidance if guidance is not None else cfg["guidance"])
            logger.info(f"Generating video {w}x{h} {nf}f steps={st} on {self._device} "
                        f"({len(prompt or '')} chars)")
            t0 = time.time()
            try:
                result = self._pipe(
                    prompt=prompt,
                    negative_prompt=negative_prompt or "",
                    width=w, height=h, num_frames=nf,
                    num_inference_steps=st, guidance_scale=gs,
                )
            except Exception as e:
                msg = str(e)
                if "OUT_OF" in msg or "out of memory" in msg.lower() or "OutOfMemory" in msg:
                    raise VideoError(
                        "Ran out of GPU memory generating the video. Lower video_num_frames / "
                        "video_width / video_height in Admin → Video (the Arc 16GB is tight)."
                    )
                raise VideoError(f"Video generation failed: {e}")
            self._last_used = time.time()
            frames = result.frames[0]
            out: List[np.ndarray] = []
            for fr in frames:
                arr = np.array(fr)
                if arr.dtype != np.uint8:
                    arr = (np.clip(arr, 0, 1) * 255).astype(np.uint8) if arr.max() <= 1.0 else arr.astype(np.uint8)
                out.append(arr)
            logger.info(f"Generated {len(out)} frames in {time.time() - t0:.0f}s")
            return out, cfg["fps"]
        finally:
            self._generating -= 1


def get_video_service(db: Session) -> VideoService:
    global _instance
    if _instance is None:
        _instance = VideoService()
    return _instance
