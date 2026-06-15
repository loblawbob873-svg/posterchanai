"""Native instruction image-editing service: text-grounded auto-mask (CLIPSeg) + SDXL inpaint.

`regeni <instruction>` edits an uploaded image WITHOUT a manual mask: CLIPSeg turns the named region
("hair", "background", "the shirt", "eyes") into a mask from text, then SDXL inpaint regenerates just
that region from the instruction. This is the PORTABLE editor — SDXL + CLIPSeg use small CLIP text
encoders (not a 7-8B LLM encoder), so the whole thing is ~8GB and runs on CUDA, Intel XPU and ROCm,
fitting a 16GB Arc / 12GB card with no offload. It reuses the SAME SDXL checkpoints the web UI uses
for `geni` (`image_model_path` / `image_anime_model_path`); when the instruction mentions/looks like
anime, it loads the anime model (the `_is_anime_prompt` keyword set, shared with image gen).

Strengths: recolour / replace / remove within a region (background, hair colour, eye colour, clothing,
objects). Weakness: big structural/pose changes (the mask constrains the shape). Shares the app's GPU
lock + VRAM swap via the factory.

Portability rule: stock diffusers/transformers + torch SDPA only — NO flash-attn/fp8/GGUF.
"""
import gc
import io
import logging
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Optional, Tuple

import numpy as np
from PIL import Image, ImageFilter
from sqlalchemy.orm import Session

logger = logging.getLogger("imageedit_service")
logger.setLevel(logging.INFO)
logger.propagate = False
if not logger.handlers:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter('%(asctime)s [REGENI] %(message)s', datefmt='%Y-%m-%d %H:%M:%S'))
    logger.addHandler(handler)

DEFAULT_IDLE_TIMEOUT = 300
SEG_MODEL = "CIDAS/clipseg-rd64-refined"
# Regions CLIPSeg segments reliably; also used as the fallback target scan when no "change X to Y"
# pattern matches. "background" is handled specially (it's the inverse of the foreground subject).
_REGION_WORDS = [
    "background", "hair", "eyes", "eye", "face", "skin", "lips", "mouth", "nose", "beard",
    "shirt", "dress", "jacket", "hoodie", "coat", "shorts", "pants", "trousers", "skirt",
    "shoes", "hat", "cap", "glasses", "sunglasses", "tie", "scarf", "gloves", "socks",
]
_ANIME_WORDS = ["anime", "manga", "waifu", "chibi", "kawaii", "moe", "2d", "cel-shaded", "cartoon"]
# Targets that ARE the face — don't face-protect these (the edit is meant to touch the face).
_FACE_FEATURES = {"face", "eye", "eyes", "lips", "mouth", "nose", "skin", "beard", "eyebrow",
                  "eyebrows", "cheek", "cheeks", "chin", "teeth", "freckles", "makeup"}

_instance: Optional["ImageEditService"] = None
_executor = ThreadPoolExecutor(max_workers=1)
_load_lock = threading.Lock()
_idle_thread: Optional[threading.Thread] = None
_idle_stop = threading.Event()


class ImageEditError(Exception):
    """User-facing image-edit error (disabled, bad config, no target, OOM, runtime error)."""


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
        "model_path": s.get("image_model_path", "") or "",
        "anime_model_path": s.get("image_anime_model_path", "") or "",
        "device": s.get("regeni_gpu_device", "auto") or "auto",
        "steps": _i("regeni_steps", 30),
        "strength": _f("regeni_strength", 0.99),
        "guidance": _f("regeni_guidance", 7.0),
        "mask_threshold": _i("regeni_mask_threshold", 35),   # 0-255 on the CLIPSeg probability map
        "mask_dilate": _i("regeni_mask_dilate", 19),         # px to grow the mask (covers the whole region)
        "max_side": _i("regeni_max_side", 1024),
        "idle_timeout": _i("regeni_idle_timeout", DEFAULT_IDLE_TIMEOUT),
    }


def _parse_instruction(instruction: str) -> Tuple[Optional[str], str]:
    """Split an edit instruction into (mask_target, inpaint_prompt). Tries the natural
    "change/make/replace/turn [the] <target> to/into/with <desc>" shape first; falls back to scanning
    for a known region word. Returns (None, _) if no region could be identified."""
    text = instruction.strip()
    m = re.search(
        r"\b(?:change|make|replace|turn|swap|give|set|recolou?r|paint)\b\s+"
        r"(?:the|her|his|their|its|a|an)?\s*(?P<target>.+?)\s+"
        r"(?:to|into|with|as)\s+(?P<desc>.+)$",
        text, re.IGNORECASE,
    )
    if m:
        target = re.sub(r"'s$", "", m.group("target").strip())  # drop a possessive 's, keep plurals
        desc = m.group("desc").strip()
        # The inpaint prompt describes what the region BECOMES; include the region noun for context.
        prompt = f"{desc} {target}" if target.split()[-1] not in desc.lower() else desc
        return target, prompt
    # Fallback: first known region word present → mask it, inpaint with the whole instruction.
    low = text.lower()
    for w in _REGION_WORDS:
        if re.search(rf"\b{re.escape(w)}\b", low):
            return w, text
    return None, text


def _is_anime(instruction: str) -> bool:
    low = instruction.lower()
    return any(w in low for w in _ANIME_WORDS)


def _idle_loop():
    while not _idle_stop.wait(30):
        inst = _instance
        # Never unload mid-edit (the worker holds the pipe across a generate not under _load_lock).
        if inst and inst._pipe is not None and inst._last_used and inst._pending == 0:
            if time.time() - inst._last_used > inst._idle_timeout:
                logger.info("Idle timeout reached — unloading edit models")
                inst.unload_model()


def _start_idle():
    global _idle_thread
    if _idle_thread is None or not _idle_thread.is_alive():
        _idle_stop.clear()
        _idle_thread = threading.Thread(target=_idle_loop, daemon=True)
        _idle_thread.start()


class ImageEditService:
    def __init__(self):
        self._pipe = None              # SDXL inpaint pipeline (model-swappable)
        self._pipe_model: Optional[str] = None  # which checkpoint the pipe holds
        self._seg = None               # CLIPSeg model (persistent, small)
        self._seg_proc = None
        self._device: Optional[str] = None
        self._idle_timeout = DEFAULT_IDLE_TIMEOUT
        self._last_used = 0.0
        self._pending = 0
        _start_idle()

    def is_loaded(self) -> bool:
        return self._pipe is not None or self._seg is not None

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

    def _ensure_seg(self):
        # Guard on BOTH so a mid-load failure (OOM during .to()) can't leave a half-state where _seg
        # looks loaded but _seg_proc is None → "'NoneType' object is not callable" in _segment.
        if self._seg is not None and self._seg_proc is not None:
            return
        from transformers import CLIPSegProcessor, CLIPSegForImageSegmentation
        logger.info("Loading CLIPSeg segmentation model ...")
        proc = CLIPSegProcessor.from_pretrained(SEG_MODEL)
        seg = CLIPSegForImageSegmentation.from_pretrained(SEG_MODEL).eval()
        if self._device != "cpu":
            seg = seg.to(self._device)
        self._seg_proc = proc  # assign both only after both succeed
        self._seg = seg

    def _ensure_pipe(self, model_path: str):
        """Load the SDXL inpaint pipeline for `model_path` (swapping if a different checkpoint is up)."""
        if self._pipe is not None and self._pipe_model == model_path:
            return
        import torch
        from diffusers import StableDiffusionXLInpaintPipeline
        if self._pipe is not None:
            self._unload_pipe()
        dtype = torch.float32 if self._device == "cpu" else torch.bfloat16
        logger.info(f"Loading SDXL inpaint pipeline {model_path} on {self._device} ...")
        t0 = time.time()
        pipe = StableDiffusionXLInpaintPipeline.from_single_file(
            model_path, torch_dtype=dtype, use_safetensors=model_path.endswith(".safetensors"))
        if self._device != "cpu":
            pipe = pipe.to(self._device)
        try:
            pipe.vae.enable_tiling()
        except Exception:
            pass
        self._pipe = pipe
        self._pipe_model = model_path
        logger.info(f"Inpaint pipeline loaded in {time.time() - t0:.0f}s")

    def load_model(self, db: Session):
        cfg = _get_settings(db)
        self._idle_timeout = cfg["idle_timeout"]
        with _load_lock:
            if self._device is None:
                self._device = self._resolve_device(cfg["device"])
            self._ensure_seg()
            self._last_used = time.time()

    def _unload_pipe(self):
        if self._pipe is None:
            return
        try:
            self._pipe.to("cpu")
        except Exception:
            pass
        self._pipe = None
        self._pipe_model = None

    def unload_model(self):
        with _load_lock:
            if self._pipe is None and self._seg is None:
                return
            self._unload_pipe()
            if self._seg is not None:
                try:
                    self._seg.to("cpu")
                except Exception:
                    pass
            self._seg = None
            self._seg_proc = None  # always clear both together (no half-state)
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
            logger.info("Edit models unloaded")

    def _clipseg_prob(self, img: Image.Image, phrases) -> np.ndarray:
        """Max-pool CLIPSeg over a few phrasings → an HxW uint8 probability map the size of `img`.
        CLIPSeg is coarse (352px), so "X"/"the X" catch different pixels."""
        import torch
        W, H = img.size
        acc = None
        with torch.no_grad():
            for p in phrases:
                si = self._seg_proc(text=[p], images=[img], return_tensors="pt")
                if self._device != "cpu":
                    si = {k: v.to(self._device) for k, v in si.items()}
                logits = self._seg(**si).logits
                pr = torch.sigmoid(logits).squeeze().float().cpu().numpy()
                if pr.ndim != 2:
                    pr = pr[0]
                pr = np.array(Image.fromarray((pr * 255).astype("uint8")).resize((W, H)))
                acc = pr if acc is None else np.maximum(acc, pr)
        return acc

    def _segment(self, img: Image.Image, target: str, cfg: dict) -> Image.Image:
        """CLIPSeg: text target -> a soft binary mask (PIL 'L') the size of `img`."""
        prompts = [target] if target.lower().startswith("the ") else [target, f"the {target}"]
        prob = self._clipseg_prob(img, prompts)
        mask = ((prob > cfg["mask_threshold"]).astype("uint8")) * 255
        mask_img = Image.fromarray(mask)
        d = max(1, int(cfg["mask_dilate"]))
        if d > 1:
            mask_img = mask_img.filter(ImageFilter.MaxFilter(d if d % 2 else d + 1))
        # Protect the face from NON-face edits: a coarse CLIPSeg "hair"/clothing mask bleeds over the
        # face, and at strength ~1.0 the inpaint then regenerates the face into a DIFFERENT person.
        # So carve out the whole face/skin generously (low threshold + grow it) before inpainting.
        if target.lower() not in _FACE_FEATURES:
            face = self._clipseg_prob(img, ["face", "skin", "the face"])
            face_mask = ((face > 50).astype("uint8")) * 255           # generous face/skin area
            face_mask = np.array(Image.fromarray(face_mask).filter(ImageFilter.MaxFilter(23)))  # grow margin
            arr = np.array(mask_img)
            arr[face_mask > 0] = 0
            mask_img = Image.fromarray(arr)
        return mask_img.filter(ImageFilter.GaussianBlur(6))  # soft edges → seamless inpaint

    def generate(self, db: Session, image_bytes: bytes, instruction: str,
                 steps: Optional[int] = None) -> bytes:
        """Edit `image_bytes` per `instruction` via auto-mask + SDXL inpaint. Returns PNG bytes.
        Blocking — call via the factory's thread + GPU lock."""
        cfg = _get_settings(db)
        target, prompt = _parse_instruction(instruction)
        # Anime keywords steer MODEL choice, not content — strip them from the inpaint prompt so they
        # don't pollute it ("blue, anime hair" → "blue hair").
        prompt = re.sub(r"[, ]*\b(" + "|".join(_ANIME_WORDS) + r")\b", "", prompt, flags=re.IGNORECASE).strip(" ,")
        if not target:
            raise ImageEditError(
                "Tell me which part to change, e.g. `change the background to a beach`, "
                "`change her hair to red`, or `change the eyes to green`.")
        # Pick the webui SDXL model: anime checkpoint when the instruction looks anime (and one is set).
        use_anime = _is_anime(instruction) and bool(cfg["anime_model_path"])
        model_path = cfg["anime_model_path"] if use_anime else cfg["model_path"]
        if not model_path:
            raise ImageEditError("No SDXL image model is configured (set it in Admin → Image).")

        self._pending += 1
        try:
            self.load_model(db)
            self._ensure_pipe(model_path)
            import torch
            try:
                img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
            except Exception as e:
                raise ImageEditError(f"Couldn't read the input image: {e}")
            ms = max(64, int(cfg["max_side"]))
            if max(img.size) > ms:
                img.thumbnail((ms, ms))
            # SDXL wants dims divisible by 8.
            W, H = img.size
            W, H = W - (W % 8), H - (H % 8)
            img = img.crop((0, 0, W, H))

            t0 = time.time()
            mask = self._segment(img, target, cfg)
            cov = (np.array(mask) > 10).mean() * 100
            logger.info(f"mask '{target}' = {cov:.1f}% ({'anime' if use_anime else 'base'} model) "
                        f"on {self._device}: {instruction[:50]}")
            if cov < 0.3:
                raise ImageEditError(
                    f"Couldn't find '{target}' in the image to edit. Try naming a clearer region "
                    f"(e.g. background, hair, eyes, shirt).")
            try:
                out = self._pipe(
                    prompt=prompt,
                    negative_prompt="lowres, bad anatomy, deformed, blurry, watermark",
                    image=img, mask_image=mask,
                    strength=float(cfg["strength"]),
                    num_inference_steps=int(steps or cfg["steps"]),
                    guidance_scale=float(cfg["guidance"]),
                    height=H, width=W,
                ).images[0]
            except Exception as e:
                msg = str(e)
                if "OUT_OF" in msg or "out of memory" in msg.lower() or "OutOfMemory" in msg:
                    raise ImageEditError(
                        "Ran out of GPU memory editing the image. Try a smaller image or lower "
                        "regeni_max_side in Admin → Image.")
                raise ImageEditError(f"Image edit failed: {e}")
            self._last_used = time.time()
            buf = io.BytesIO()
            out.save(buf, format="PNG")
            logger.info(f"Edited image in {time.time() - t0:.0f}s")
            return buf.getvalue()
        finally:
            self._pending -= 1


def get_imageedit_service(db: Session) -> ImageEditService:
    global _instance
    if _instance is None:
        _instance = ImageEditService()
    return _instance
