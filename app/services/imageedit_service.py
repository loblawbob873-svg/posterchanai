"""Native instruction-based image-editing service (diffusers OmniGen v1), mirroring video_service.

Like image/video gen, this runs IN-PROCESS on the same torch stack (CUDA / Intel XPU / ROCm) — NOT
a separate HTTP server. OmniGen v1 (`OmniGenPipeline`) is a single unified ~3.8B transformer that
takes an input image + a natural-language instruction and returns an edited image (maskless, no
strength dial). It's a stock `diffusers` pipeline, so it shares this venv, the shared
`GPUResourceLock`, and the `vram_manager` model-swap. The factory (`imageedit_factory.py`) owns the
load-balancing + GPU lock; this module is just the generator (load → edit → idle-unload).

Why OmniGen v1 and not a SOTA editor: the good editors (LongCat/OmniGen2/Qwen-Edit) ship a ~16GB
Qwen2.5-VL/T5 text encoder that fits neither the 16GB Arc (offload is broken on XPU) nor the 12GB
nas. OmniGen v1 has NO separate large encoder — verified peak ~9.3GB on the Arc XPU (no offload),
so it runs on both cards. `regeni_model` is a setting so a SOTA model can replace it on bigger GPUs.

Portability rule (must run on Arc XPU + CUDA + ROCm): stock diffusers + torch SDPA — NO
flash-attn / xformers / fp8 / GGUF (CUDA-pinned, break Arc/ROCm).
"""
import gc
import io
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

from PIL import Image
from sqlalchemy.orm import Session

logger = logging.getLogger("imageedit_service")
logger.setLevel(logging.INFO)
logger.propagate = False
if not logger.handlers:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter('%(asctime)s [REGENI] %(message)s', datefmt='%Y-%m-%d %H:%M:%S'))
    logger.addHandler(handler)

DEFAULT_IDLE_TIMEOUT = 300  # unload the edit model after 5 min idle

_instance: Optional["ImageEditService"] = None
_executor = ThreadPoolExecutor(max_workers=1)  # one GPU task at a time (the GPU lock enforces this too)
_load_lock = threading.Lock()
_idle_thread: Optional[threading.Thread] = None
_idle_stop = threading.Event()


class ImageEditError(Exception):
    """User-facing image-edit error (disabled, bad config, OOM, runtime error)."""


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
        "enabled": str(s.get("regeni_enabled", "false")).lower() == "true",
        "local_enabled": str(s.get("regeni_local_enabled", "true")).lower() == "true",
        "model": s.get("regeni_model", "Shitao/OmniGen-v1-diffusers") or "Shitao/OmniGen-v1-diffusers",
        "device": s.get("regeni_gpu_device", "auto") or "auto",
        "steps": _i("regeni_steps", 25),
        "guidance": _f("regeni_guidance", 2.0),
        "img_guidance": _f("regeni_img_guidance", 1.6),
        "max_side": _i("regeni_max_side", 1024),
        "idle_timeout": _i("regeni_idle_timeout", DEFAULT_IDLE_TIMEOUT),
    }


def _idle_loop():
    while not _idle_stop.wait(30):
        inst = _instance
        if inst and inst._pipe is not None and inst._last_used:
            if time.time() - inst._last_used > inst._idle_timeout:
                logger.info("Idle timeout reached — unloading edit model")
                inst.unload_model()


def _start_idle():
    global _idle_thread
    if _idle_thread is None or not _idle_thread.is_alive():
        _idle_stop.clear()
        _idle_thread = threading.Thread(target=_idle_loop, daemon=True)
        _idle_thread.start()


class ImageEditService:
    def __init__(self):
        self._pipe = None
        self._model_id: Optional[str] = None
        self._device: Optional[str] = None
        self._idle_timeout = DEFAULT_IDLE_TIMEOUT
        self._last_used = 0.0
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
                self.unload_model()
            import torch
            from diffusers import OmniGenPipeline
            device = self._resolve_device(cfg["device"])
            self._device = device
            logger.info(f"Loading edit model {model_id} on {device} ...")
            t0 = time.time()
            dtype = torch.float32 if device == "cpu" else torch.bfloat16
            pipe = OmniGenPipeline.from_pretrained(model_id, torch_dtype=dtype)
            # OmniGen v1 is ~9GB on the GPU — fits the 16GB Arc / 12GB nas fully, so load DIRECT (no
            # CPU offload: offload is broken on XPU and unnecessary here). Tiled VAE keeps peak down.
            if device != "cpu":
                pipe = pipe.to(device)
            try:
                pipe.vae.enable_tiling()
            except Exception:
                pass
            self._pipe = pipe
            self._model_id = model_id
            # Mark loaded as "used" now so a model that loads but whose first generate fails (e.g.
            # repeated OOM) still idle-unloads instead of pinning VRAM forever (the idle loop gates
            # on `_last_used` being truthy).
            self._last_used = time.time()
            logger.info(f"Edit model loaded in {time.time() - t0:.0f}s")

    def unload_model(self):
        with _load_lock:
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
            logger.info("Edit model unloaded")

    def generate(self, db: Session, image_bytes: bytes, instruction: str,
                 steps: Optional[int] = None, guidance: Optional[float] = None,
                 img_guidance: Optional[float] = None) -> bytes:
        """Edit `image_bytes` per `instruction`, returns PNG bytes. Blocking — call via the factory's
        thread + GPU lock."""
        cfg = _get_settings(db)
        self.load_model(db)
        import torch
        try:
            img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
        except Exception as e:
            raise ImageEditError(f"Couldn't read the input image: {e}")
        # Clamp the longest side so a huge upload doesn't OOM / take forever (the GPUs are tight).
        ms = max(64, int(cfg["max_side"]))
        if max(img.size) > ms:
            img.thumbnail((ms, ms))
        st = int(steps or cfg["steps"])
        gs = float(guidance if guidance is not None else cfg["guidance"])
        igs = float(img_guidance if img_guidance is not None else cfg["img_guidance"])
        # OmniGen references the input image via the <img><|image_1|></img> placeholder in the prompt.
        prompt = f"<img><|image_1|></img> {instruction.strip()}"
        logger.info(f"Editing {img.size} steps={st} on {self._device}: {instruction[:60]}")
        t0 = time.time()
        try:
            result = self._pipe(
                prompt=prompt,
                input_images=[img],
                guidance_scale=gs,
                img_guidance_scale=igs,
                num_inference_steps=st,
                # Honour the configured max side: OmniGen otherwise caps the input at its internal
                # default (1024), so a higher regeni_max_side would silently have no effect.
                max_input_image_size=ms,
                use_input_image_size_as_output=True,
            )
        except Exception as e:
            msg = str(e)
            if "OUT_OF" in msg or "out of memory" in msg.lower() or "OutOfMemory" in msg:
                raise ImageEditError(
                    "Ran out of GPU memory editing the image. Try a smaller image or lower "
                    "regeni_max_side in Admin → Image."
                )
            raise ImageEditError(f"Image edit failed: {e}")
        self._last_used = time.time()
        # Guard against an empty/None images list (safety filter / pipeline edge case) so callers get
        # a clean error rather than a raw IndexError.
        imgs = getattr(result, "images", None)
        if not imgs:
            raise ImageEditError("The edit model returned no image.")
        out_img = imgs[0]
        buf = io.BytesIO()
        out_img.save(buf, format="PNG")
        logger.info(f"Edited image in {time.time() - t0:.0f}s")
        return buf.getvalue()


def get_imageedit_service(db: Session) -> ImageEditService:
    global _instance
    if _instance is None:
        _instance = ImageEditService()
    return _instance
