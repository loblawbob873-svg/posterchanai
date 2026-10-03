"""Pose-guided image generation: an OpenPose skeleton holds the body to a pose through ControlNet.

An OPTIONAL extra on the ordinary image request (`/api/generate-image` pose_image + pose_scale), so it
rides the same round-robin, the same per-node GPU lock and the same VRAM swap as every other image.
Text alone cannot hold a pose -- measured: four different dance poses asked for in words came back as
the same standing pose four times -- which is what this exists for (first user: the desktop's dancing
PosterChan frames).

Shared by the in-process generator (diffusers_service) and the subprocess one
(scripts/generate_image_subprocess.py) so the two cannot drift. SDXL only: the model is
xinsir/controlnet-openpose-sdxl-1.0 (2.5 GB), fetched ONCE on first use into
models/controlnet-openpose-sdxl/ (or the `image_controlnet_openpose_path` setting), never at import.
"""
from __future__ import annotations

import base64
import io
import logging
import os
import threading

logger = logging.getLogger(__name__)

HF_REPO = "xinsir/controlnet-openpose-sdxl-1.0"
FILES = ("config.json", "diffusion_pytorch_model.safetensors")
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_fetch_lock = threading.Lock()


def controlnet_dir(configured: str | None = None) -> str:
    return configured or os.path.join(_REPO_ROOT, "models", "controlnet-openpose-sdxl")


def ensure_controlnet(configured: str | None = None) -> str:
    """The ControlNet folder, downloading its two files the first time. Raises if it cannot."""
    d = controlnet_dir(configured)
    if all(os.path.isfile(os.path.join(d, f)) for f in FILES):
        return d
    with _fetch_lock:
        if all(os.path.isfile(os.path.join(d, f)) for f in FILES):
            return d
        os.makedirs(d, exist_ok=True)
        from huggingface_hub import hf_hub_download
        for f in FILES:
            if not os.path.isfile(os.path.join(d, f)):
                logger.info("[image-pose] fetching %s/%s (first pose request on this node)", HF_REPO, f)
                hf_hub_download(HF_REPO, f, local_dir=d)
    return d


def decode_pose(b64: str, width: int, height: int):
    """The pose image as RGB at the output size. Raises ValueError on anything that is not an image."""
    from PIL import Image
    try:
        raw = base64.b64decode(b64, validate=False)
        if len(raw) > 8 * 1024 * 1024:
            raise ValueError("pose image too large")
        img = Image.open(io.BytesIO(raw)).convert("RGB")
    except ValueError:
        raise
    except Exception as e:
        raise ValueError(f"pose image unreadable: {e}") from e
    if img.size != (width, height):
        img = img.resize((width, height))
    return img


def wrap_with_pose(pipe, dtype, device: str, configured: str | None = None, offload: bool = False):
    """An SDXL ControlNet pipeline built from an ALREADY-LOADED SDXL pipe's components (the checkpoint
    is not loaded twice). The returned pipe owns only the extra ControlNet weights."""
    import torch  # noqa: F401  (device moves below)
    from diffusers import ControlNetModel, StableDiffusionXLControlNetPipeline
    cn = ControlNetModel.from_pretrained(ensure_controlnet(configured), torch_dtype=dtype)
    posed = StableDiffusionXLControlNetPipeline(**pipe.components, controlnet=cn)
    if offload:
        posed.enable_model_cpu_offload()
    elif device != "cpu":
        posed.controlnet.to(device)
    try:
        posed.enable_vae_tiling()        # the full-size decode is the other big allocation
    except Exception:
        pass
    return posed


SMALL_GPU_BYTES = 14 * 1024 ** 3


def needs_offload(device: str) -> bool:
    """Page the posed pipeline through the CPU on a small CUDA card. SDXL + this ControlNet at 832x1216
    ran a 12 GB RTX 3060 out of memory ("Tried to allocate 832.00 MiB ... 734.88 MiB is free") while the
    16 GB Arc did it in 35-70 s. Offload costs speed, never correctness; the generator unloads the model
    after every image, so the paging hooks never outlive the request."""
    if device != "cuda":
        return False
    try:
        import torch
        return torch.cuda.get_device_properties(0).total_memory < SMALL_GPU_BYTES
    except Exception:
        return False


def clamp_scale(v) -> float:
    try:
        return max(0.0, min(2.0, float(v)))
    except (TypeError, ValueError):
        return 1.0
