"""
Native Image Generation Service using diffusers.
Supports NVIDIA (CUDA), Intel Arc (XPU), AMD (ROCm), and CPU.
"""
import asyncio
import base64
import gc
import io
import logging
import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Optional, Dict, Any

from PIL import Image
from sqlalchemy.orm import Session

from app.services import settings_store

# Configure logging first (before using logger)
logger = logging.getLogger("diffusers_service")
logger.setLevel(logging.INFO)
logger.propagate = False
if not logger.handlers:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter('%(asctime)s [DIFFUSERS] %(message)s', datefmt='%Y-%m-%d %H:%M:%S'))
    logger.addHandler(handler)

# Try to import numpy for image validation
try:
    import numpy as np
    HAS_NUMPY = True
except ImportError:
    HAS_NUMPY = False
    logger.warning("numpy not available, image blank detection will be limited")

# Global instance (singleton)
_diffusers_instance: Optional["DiffusersService"] = None
_executor = ThreadPoolExecutor(max_workers=2)
_load_lock = threading.Lock()

# Idle timeout for automatic model unloading (seconds)
# Default 120 seconds - unload model after 2 minutes of no activity
DEFAULT_IDLE_TIMEOUT = 120
_idle_check_thread: Optional[threading.Thread] = None
_idle_check_stop = threading.Event()


def detect_device() -> str:
    """Auto-detect the best available device"""
    try:
        import torch

        # Check for CUDA (NVIDIA) or ROCm (AMD)
        # ROCm uses the torch.cuda API, so we check device name to distinguish
        if torch.cuda.is_available():
            device_name = torch.cuda.get_device_name(0)
            logger.info(f"Detected GPU device: {device_name}")
            # Both NVIDIA and AMD ROCm use "cuda" as the device string
            return "cuda"

        # Check for Intel XPU
        if hasattr(torch, "xpu") and torch.xpu.is_available():
            logger.info("Detected Intel XPU device")
            return "xpu"

        logger.info("No GPU detected, using CPU")
        return "cpu"
    except Exception as e:
        logger.warning(f"Error detecting device: {e}, falling back to CPU")
        return "cpu"


def _detect_device_isolated() -> str:
    """Detect the device WITHOUT initializing any GPU in this process. Runs detect_device() in a
    throwaway subprocess so the parent never holds a CUDA/XPU context (which would corrupt the
    image subprocess it later forks). Used only in subprocess mode with image_gpu_device=auto."""
    import subprocess, sys, os
    try:
        repo = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
        r = subprocess.run(
            [sys.executable, "-c",
             "from app.services.diffusers_service import detect_device; print(detect_device())"],
            capture_output=True, text=True, timeout=60, cwd=repo)
        out = (r.stdout or "").strip().splitlines()
        d = out[-1].strip() if out else "cpu"
        return d if d in ("cuda", "xpu", "cpu") else "cpu"
    except Exception:
        return "cpu"


def is_rocm() -> bool:
    """Check if running on AMD ROCm (vs NVIDIA CUDA)"""
    try:
        import torch
        if torch.cuda.is_available():
            # ROCm devices have "AMD" or "Radeon" in the name
            device_name = torch.cuda.get_device_name(0).lower()
            return "amd" in device_name or "radeon" in device_name
        return False
    except Exception:
        return False


def is_image_blank(image: Image.Image, threshold: float = 0.99) -> bool:
    """
    Check if an image is blank (all pixels are the same or very similar).
    
    Args:
        image: PIL Image to check
        threshold: Threshold for considering image blank (0.99 = 99% of pixels must be similar)
    
    Returns:
        True if image appears blank, False otherwise
    """
    if not HAS_NUMPY:
        # Fallback: basic check using PIL histogram
        try:
            # Convert to RGB if needed
            if image.mode != 'RGB':
                image = image.convert('RGB')
            
            # Get histogram - if all pixels are the same, histogram will have very few non-zero bins
            hist = image.histogram()
            non_zero_bins = sum(1 for h in hist if h > 0)
            # For RGB, we have 256 bins per channel = 768 total
            # If most bins are zero, image is likely blank
            # Also check image size - very small images might have few bins but not be blank
            width, height = image.size
            if width * height < 100:  # Very small images (< 10x10) - be more lenient
                # For tiny images, require even fewer colors to be considered blank
                if non_zero_bins < 3:
                    logger.warning("Image appears blank (histogram check, tiny image)")
                    return True
            elif non_zero_bins < 10:  # Very few colors for normal-sized images
                logger.warning("Image appears blank (histogram check)")
                return True
            return False
        except Exception as e:
            logger.error(f"Error checking if image is blank (fallback): {e}")
            return False
    
    try:
        # Convert to RGB if needed (handles RGBA, L, etc.)
        if image.mode != 'RGB':
            image = image.convert('RGB')
        
        # Convert to numpy array
        img_array = np.array(image)
        
        # Check if image has valid dimensions
        if img_array.size == 0:
            logger.warning("Image has zero size")
            return True
        
        # Check image dimensions are reasonable
        if len(img_array.shape) < 2 or img_array.shape[0] == 0 or img_array.shape[1] == 0:
            logger.warning(f"Image has invalid dimensions: {img_array.shape}")
            return True
        
        # Calculate standard deviation for each channel
        std_dev = np.std(img_array, axis=(0, 1))
        
        # If all channels have very low variance, image is likely blank
        # A blank image would have std_dev close to 0 for all channels
        max_std = np.max(std_dev)
        
        # Also check if most pixels are the same color
        # For large images, sample pixels to avoid memory issues with np.unique()
        pixels = img_array.reshape(-1, 3)
        total_pixels = pixels.shape[0]
        
        # Sample pixels for large images to avoid expensive np.unique() on millions of pixels
        # Sample up to 100k pixels (enough for accurate estimation)
        max_sample_size = 100000
        if total_pixels > max_sample_size:
            # Randomly sample pixels
            sample_indices = np.random.choice(total_pixels, max_sample_size, replace=False)
            sample_pixels = pixels[sample_indices]
            unique_colors_in_sample = len(np.unique(sample_pixels, axis=0))
            # Use the ratio in the sample as an estimate for the full image
            # If 50 unique colors in 100k sample, ratio is 50/100k = 0.0005
            unique_ratio = unique_colors_in_sample / max_sample_size
            # For logging, estimate total unique colors (scaled to full image size)
            unique_colors = int(unique_colors_in_sample * (total_pixels / max_sample_size))
        else:
            unique_colors = len(np.unique(pixels, axis=0))
            unique_ratio = unique_colors / total_pixels if total_pixels > 0 else 0
        
        # Image is blank if:
        # 1. Very low standard deviation (all pixels nearly identical) - std_dev < 5.0
        # 2. Very few unique colors - less than (1 - threshold) of pixels are unique
        #    With threshold=0.99, this means <1% unique colors
        is_blank = max_std < 5.0 or unique_ratio < (1.0 - threshold)
        
        if is_blank:
            logger.warning(f"Image appears blank: std_dev={max_std:.2f}, unique_colors={unique_colors}/{total_pixels} ({unique_ratio*100:.2f}%)")
        
        return is_blank
        
    except Exception as e:
        logger.error(f"Error checking if image is blank: {e}")
        # If we can't check, assume it's not blank to avoid false positives
        return False


def _idle_check_loop():
    """Background thread to check for idle timeout and unload models"""
    global _diffusers_instance
    while not _idle_check_stop.wait(30):  # Check every 30 seconds
        if _diffusers_instance is not None and _diffusers_instance._pipe is not None:
            # Never unload mid-generation: a run can outlast _idle_timeout (Arc XPU), which would
            # leave `_last_used` stale and trick the monitor into unloading the active pipe. The
            # pre-check is the fast path; unload_model(skip_if_generating=True) re-checks under
            # _load_lock to close the check-then-act window (a gen starting right after this check).
            if _diffusers_instance._generating > 0:
                continue
            idle_time = time.time() - _diffusers_instance._last_used
            timeout = _diffusers_instance._idle_timeout
            if idle_time > timeout:
                logger.info(f"Model idle for {idle_time:.0f}s (>{timeout}s), unloading to free VRAM")
                _diffusers_instance.unload_model(skip_if_generating=True)


def _start_idle_check():
    """Start the idle check background thread"""
    global _idle_check_thread
    if _idle_check_thread is None or not _idle_check_thread.is_alive():
        _idle_check_stop.clear()
        _idle_check_thread = threading.Thread(target=_idle_check_loop, daemon=True)
        _idle_check_thread.start()
        logger.info("Started idle timeout monitor")


class DiffusersService:
    """
    Native image generation service using diffusers.
    Provides txt2img capabilities.
    Automatically unloads models after idle timeout to free VRAM.
    """

    def __init__(self, db: Session):
        self.db = db
        self._pipe = None
        self._model_path: Optional[str] = None
        self._model_type: Optional[str] = None  # "sd15", "sdxl", "sd3", "flux"
        self._device: Optional[str] = None
        self._last_used: float = time.time()
        self._idle_timeout: int = DEFAULT_IDLE_TIMEOUT
        # In-flight generation counter. The idle monitor must NOT unload the model while a
        # generation is running: on the Arc XPU a single SDXL run can exceed _idle_timeout,
        # and `_last_used` (set only at the start/end of a run) goes stale mid-generation —
        # so without this guard the monitor unloads the pipe out from under the running
        # generation and the call hangs. Mirrors llama_service's `_pending_requests` guard.
        self._generating: int = 0
        self._attention_slicing: str = "off"
        self._load_settings()
        _start_idle_check()

    def _load_settings(self):
        """Load settings from database"""
        settings = settings_store.all_settings()

        # Idle timeout for automatic unloading (default 2 minutes)
        self._idle_timeout = int(settings.get("image_idle_timeout", str(DEFAULT_IDLE_TIMEOUT)))
        logger.info(f"Loaded image_idle_timeout setting: {self._idle_timeout}")

        # Attention slicing mode: "off" (fastest, relies on SDPA/xformers), "auto" (balanced),
        # or "max" (most VRAM-saving, slowest). Default "off" on CUDA (xformers) / XPU, but "auto" on
        # ROCm: AMD has NO xformers, and SDPA still materializes the full self-attention matrix, which
        # at 1024² is ~16 GiB and OOMs a 12 GB card (RX 6750 XT). "auto" slices it without the ~8x
        # "max" penalty. Admin can still override via the image_attention_slicing setting.
        _slice_default = "auto" if is_rocm() else "off"
        self._attention_slicing = (settings.get("image_attention_slicing", _slice_default) or _slice_default).lower()

        # Model settings
        self.model_path = settings.get("image_model_path", "")
        self.anime_model_path = settings.get("image_anime_model_path", "")
        self.model_type = settings.get("image_model_type", "sdxl")  # sd15, sdxl, sd3, flux

        # Generation defaults
        # The negative prompt's monochrome terms are LOAD-BEARING — see schemas.py. The anime path
        # loads a Danbooru-tagged checkpoint whose training set is full of monochrome manga tagged
        # exactly these words, so without them an ordinary coloured-illustration prompt kept coming
        # back as a colourless line sketch.
        self.default_negative = (settings.get("image_negative_prompt", "") or "").strip() or (
            "bad quality, blurry, distorted, ugly, deformed, low resolution, "
            "monochrome, greyscale, grayscale, sketch, lineart, line art, "
            "unfinished, rough sketch, flat color")
        self.default_steps = int(settings.get("image_default_steps", "20"))
        self.default_cfg = float(settings.get("image_default_cfg", "7.0"))
        self.default_width = int(settings.get("image_default_width", "1024"))
        self.default_height = int(settings.get("image_default_height", "1024"))

        # Subprocess mode - run each image in separate process for guaranteed VRAM release.
        # Read FIRST: when on, the parent must NOT initialize any GPU (CUDA/XPU). It forks the image
        # subprocess, and a GPU context initialized in the parent corrupts the child's GPU state -
        # generation then crashes at the first compute step. (Recommended for Intel XPU sharing the
        # GPU with the LLM.)
        self._subprocess_mode = settings.get("image_subprocess_mode", "false").lower() == "true"
        # Image request timeout (admin setting, ms -> s). Bounds the generation subprocess.
        try:
            self._request_timeout = max(60, int(settings.get("image_timeout", "300000")) // 1000)
        except (TypeError, ValueError):
            self._request_timeout = 600

        # Device settings
        device_setting = settings.get("image_gpu_device", "auto")
        if self._subprocess_mode:
            # Do NOT touch torch.cuda/torch.xpu here - that would init a GPU context in the parent
            # before it forks the subprocess. Trust the explicit setting; for 'auto', detect in an
            # isolated subprocess so this process never holds a GPU context.
            self._device = device_setting if device_setting != "auto" else _detect_device_isolated()
        elif device_setting == "auto":
            self._device = detect_device()
        else:
            self._device = device_setting
            # Validate device is actually available
            import torch
            if self._device == "cuda" and not torch.cuda.is_available():
                logger.warning(f"image_gpu_device is set to 'cuda' but CUDA is not available, falling back to auto-detection")
                self._device = detect_device()
            elif self._device == "xpu" and not (hasattr(torch, "xpu") and torch.xpu.is_available()):
                logger.warning(f"image_gpu_device is set to 'xpu' but XPU is not available, falling back to auto-detection")
                self._device = detect_device()

    def _is_anime_prompt(self, prompt: str) -> bool:
        """Check if prompt is for anime-style image"""
        anime_keywords = [
            "anime", "manga", "waifu", "chibi", "kawaii",
            "moe", "otaku", "hentai", "ecchi", "shoujo",
            "seinen", "shonen", "isekai", "2d", "cel-shaded"
        ]
        prompt_lower = prompt.lower()
        return any(keyword in prompt_lower for keyword in anime_keywords)

    def _get_model_for_prompt(self, prompt: str) -> str:
        """Select appropriate model based on prompt content"""
        if self.anime_model_path and self._is_anime_prompt(prompt):
            logger.debug(f"Using anime model for prompt: {prompt[:50]}...")
            return self.anime_model_path
        return self.model_path

    def _ensure_model_loaded(self, target_model: str = None):
        """Load model if not already loaded or if path changed"""
        # Use specified model or default
        model_to_load = target_model or self.model_path

        if self._pipe is not None and self._model_path == model_to_load:
            return

        with _load_lock:
            # Double-check after acquiring lock
            if self._pipe is not None and self._model_path == model_to_load:
                return

            # Unload previous model
            if self._pipe is not None:
                logger.info("Unloading previous model for model switch...")
                self._unload_model_internal()
                # ROCm needs extra time to actually free VRAM
                if is_rocm():
                    import torch
                    torch.cuda.synchronize()
                    time.sleep(1)  # Give ROCm time to release memory
                    gc.collect()
                    torch.cuda.empty_cache()
                    logger.info("ROCm memory cleanup complete")

            if not model_to_load:
                logger.warning("No model path configured")
                return

            logger.info(f"Loading diffusers model: {model_to_load}")
            logger.info(f"  Device: {self._device}, Type: {self.model_type}")

            try:
                import torch
                from diffusers import (
                    StableDiffusionPipeline,
                    StableDiffusionXLPipeline,
                    AutoPipelineForText2Image,
                )

                # Determine dtype based on device
                if self._device == "cpu":
                    dtype = torch.float32
                elif self._device == "xpu":
                    # Intel Arc: bfloat16 is better supported and faster than float16
                    try:
                        dtype = torch.bfloat16
                        logger.info("Using bfloat16 for Intel Arc (better performance)")
                    except AttributeError:
                        dtype = torch.float32
                        logger.info("bfloat16 not available, using float32 for Intel Arc")
                else:
                    dtype = torch.float16

                # Load based on model type
                if model_to_load.endswith(".safetensors") or model_to_load.endswith(".ckpt"):
                    # Single file checkpoint
                    if self.model_type == "sdxl":
                        self._pipe = StableDiffusionXLPipeline.from_single_file(
                            model_to_load,
                            torch_dtype=dtype,
                            use_safetensors=model_to_load.endswith(".safetensors")
                        )
                    else:
                        self._pipe = StableDiffusionPipeline.from_single_file(
                            model_to_load,
                            torch_dtype=dtype,
                            use_safetensors=model_to_load.endswith(".safetensors")
                        )
                else:
                    # HuggingFace model ID or local diffusers folder
                    self._pipe = AutoPipelineForText2Image.from_pretrained(
                        model_to_load,
                        torch_dtype=dtype,
                    )

                # Enable memory optimizations
                if self._device != "cpu":
                    # ROCm: use model_cpu_offload (like --medvram)
                    # Keeps model in CPU, moves each component to GPU only when needed
                    if is_rocm():
                        try:
                            # Disable VAE upcast to fp32 (saves ~6GB VRAM)
                            if hasattr(self._pipe, 'vae') and hasattr(self._pipe.vae, 'config'):
                                self._pipe.vae.config.force_upcast = False
                            if hasattr(self._pipe, 'upcast_vae'):
                                self._pipe.upcast_vae = False

                            self._pipe.enable_model_cpu_offload()
                            logger.info("ROCm: enabled model CPU offload + disabled VAE upcast")
                        except Exception as e:
                            logger.warning(f"CPU offload failed: {e}, loading to GPU")
                            self._pipe = self._pipe.to(self._device)
                    elif self._device == "xpu":
                        # Intel Arc: Load directly to XPU (CPU offload adds overhead)
                        # Disable VAE upcast for better performance
                        try:
                            # Disable VAE upcast to fp32 (saves VRAM and improves speed)
                            if hasattr(self._pipe, 'vae') and hasattr(self._pipe.vae, 'config'):
                                self._pipe.vae.config.force_upcast = False
                            if hasattr(self._pipe, 'upcast_vae'):
                                self._pipe.upcast_vae = False
                            logger.info("Intel Arc: disabled VAE upcast for better performance")
                        except Exception as e:
                            logger.warning(f"Failed to disable VAE upcast: {e}")
                        
                        # Load directly to XPU (faster than CPU offload on Intel Arc)
                        self._pipe = self._pipe.to(self._device)
                        logger.info("Intel Arc: model loaded to XPU (direct, no CPU offload)")
                    else:
                        self._pipe = self._pipe.to(self._device)

                    # xformers (CUDA only — not XPU/ROCm) is the most efficient attention path.
                    # Enable it independently of the slicing mode below; it supersedes slicing.
                    if self._device == "cuda":
                        try:
                            self._pipe.enable_xformers_memory_efficient_attention()
                            logger.info("Enabled xformers memory efficient attention (CUDA only)")
                        except Exception:
                            pass

                    # Attention slicing trades throughput for VRAM, driven by the
                    # `image_attention_slicing` setting so a tight-VRAM node can dial it up without
                    # a code change. Default "off": SDPA (XPU/ROCm) / xformers (CUDA) handle 1024²
                    # fine, and "max" was ~8x slower on the Arc. VAE slicing+tiling below still cap
                    # the decode peak regardless.
                    slicing = self._attention_slicing
                    try:
                        if slicing == "max":
                            self._pipe.enable_attention_slicing("max")
                            logger.info(f"Attention slicing: max ({self._device})")
                        elif slicing == "auto":
                            self._pipe.enable_attention_slicing()
                            logger.info(f"Attention slicing: auto/balanced ({self._device})")
                        else:  # "off" (default) and any unknown value
                            self._pipe.disable_attention_slicing()
                            logger.info(f"Attention slicing: off ({self._device}, using SDPA/xformers)")
                    except Exception as e:
                        logger.warning(f"Failed to apply attention slicing '{slicing}': {e}")

                    # VAE optimizations for all devices
                    try:
                        # Use new API to avoid deprecation warning (diffusers 0.40+)
                        if hasattr(self._pipe, 'vae') and hasattr(self._pipe.vae, 'enable_slicing'):
                            self._pipe.vae.enable_slicing()
                        else:
                            self._pipe.enable_vae_slicing()
                        if self._device == "xpu":
                            logger.info("Intel Arc: enabled VAE slicing")
                    except Exception:
                        pass

                    try:
                        # Use new API to avoid deprecation warning (diffusers 0.40+)
                        if hasattr(self._pipe, 'vae') and hasattr(self._pipe.vae, 'enable_tiling'):
                            self._pipe.vae.enable_tiling()
                        else:
                            self._pipe.enable_vae_tiling()
                        if self._device == "xpu":
                            logger.info("Intel Arc: enabled VAE tiling")
                    except Exception:
                        pass
                else:
                    self._pipe = self._pipe.to(self._device)

                self._model_path = model_to_load
                self._model_type = self.model_type
                logger.info("Model loaded successfully")

            except Exception as e:
                logger.error(f"Failed to load model: {e}")
                self._pipe = None
                raise

    def _unload_model_internal(self):
        """Internal method to unload model (no lock)"""
        if self._pipe is not None:
            del self._pipe
            self._pipe = None
            self._model_path = None
            
            # Reset VRAM mode if unloaded outside of VRAM manager (e.g., idle timeout)
            try:
                from app.services.vram_manager import reset_vram_mode
                reset_vram_mode()
            except Exception:
                pass  # Don't fail if VRAM manager not available

            # Force garbage collection - run twice for thorough cleanup
            gc.collect()
            gc.collect()

            try:
                import torch
                # In subprocess mode the parent owns no GPU memory (the subprocess does and frees it
                # on exit). Touching torch.cuda/torch.xpu here would initialize a GPU context in the
                # parent and corrupt the next forked subprocess - so skip all GPU cleanup.
                if self._subprocess_mode:
                    logger.debug("Subprocess mode: parent skips GPU cleanup (subprocess frees its own VRAM)")
                # Check XPU first since that's what we're using
                elif hasattr(torch, "xpu") and torch.xpu.is_available():
                    logger.info("Clearing Intel XPU memory...")
                    torch.xpu.synchronize()
                    torch.xpu.empty_cache()
                    # Additional XPU cleanup attempts
                    try:
                        torch.xpu.reset_peak_memory_stats()
                    except Exception:
                        pass
                    gc.collect()
                    torch.xpu.empty_cache()
                    logger.info("Intel XPU memory cache cleared")
                elif torch.cuda.is_available():
                    # Works for both NVIDIA CUDA and AMD ROCm
                    torch.cuda.empty_cache()
                    torch.cuda.synchronize()
                    # Reset memory stats to help with fragmentation
                    try:
                        torch.cuda.reset_peak_memory_stats()
                    except Exception:
                        pass
                    # Additional aggressive cleanup
                    torch.cuda.empty_cache()
                    # ROCm may need additional cleanup
                    if is_rocm():
                        try:
                            torch.cuda.ipc_collect()
                        except Exception:
                            pass
                        logger.debug("Cleared ROCm HIP memory cache")
                    else:
                        # NVIDIA CUDA - log memory stats after cleanup
                        try:
                            allocated = torch.cuda.memory_allocated() / 1024**3  # GB
                            reserved = torch.cuda.memory_reserved() / 1024**3  # GB
                            logger.info(f"CUDA memory after cleanup: {allocated:.2f} GB allocated, {reserved:.2f} GB reserved")
                        except Exception:
                            pass
            except Exception as e:
                logger.warning(f"Error during GPU memory cleanup: {e}")

            # Additional gc pass after CUDA cleanup
            gc.collect()

            logger.info("Model unloaded, VRAM freed")

    def unload_model(self, skip_if_generating: bool = False):
        """Unload model and free VRAM.

        skip_if_generating: when True (idle monitor), re-check the in-flight counter UNDER the
        lock and skip if a generation is active. A generation increments `_generating` before it
        contends for `_load_lock`, so this provably closes the idle loop's check-then-act race.
        The explicit post-generation unload passes False (it runs while `_generating` is still >0).
        """
        with _load_lock:
            if skip_if_generating and self._generating > 0:
                return
            self._unload_model_internal()

    def reload_model(self):
        """Reload the model (unload then load)"""
        self.unload_model()
        self._load_settings()
        self._ensure_model_loaded()

    def is_loaded(self) -> bool:
        """Check if model is loaded"""
        return self._pipe is not None

    def get_model_info(self) -> Dict[str, Any]:
        """Get information about the loaded model"""
        return {
            "loaded": self._pipe is not None,
            "model_path": self._model_path,
            "model_type": self._model_type,
            "device": self._device,
            "backend": "native",
        }

    def _truncate_prompt(self, prompt: str, max_tokens: int = 75) -> str:
        """Truncate prompt to avoid token limit errors (SDXL limit is 77)"""
        # Rough estimate: ~4 chars per token on average
        max_chars = max_tokens * 4
        if len(prompt) > max_chars:
            # Truncate at last comma before limit to keep tags intact
            truncated = prompt[:max_chars]
            last_comma = truncated.rfind(',')
            if last_comma > max_chars * 0.7:  # Keep at least 70% of content
                truncated = truncated[:last_comma]
            logger.warning(f"Prompt truncated from {len(prompt)} to {len(truncated)} chars")
            return truncated
        return prompt

    def _generate_sync(
        self,
        prompt: str,
        negative_prompt: str = "",
        width: int = None,
        height: int = None,
        steps: int = None,
        cfg: float = None,
        seed: int = None,
    ) -> Optional[bytes]:
        """Synchronous image generation"""
        # Truncate prompt to avoid token limit errors
        prompt = self._truncate_prompt(prompt)

        # Select model based on prompt (anime vs default)
        target_model = self._get_model_for_prompt(prompt)
        self._ensure_model_loaded(target_model)

        if self._pipe is None:
            logger.error("No model loaded")
            return None

        # Use defaults if not specified
        width = width or self.default_width
        height = height or self.default_height
        steps = steps or self.default_steps
        cfg = cfg or self.default_cfg

        # Generate seed if not provided
        if seed is None or seed < 0:
            seed = random.randint(0, 2**32 - 1)

        try:
            import torch

            # model_cpu_offload (ROCm) needs CPU generator
            gen_device = "cpu" if is_rocm() else self._device

            # Retry up to 2 times if we get a blank image
            max_retries = 2
            result = None
            image = None
            
            for attempt in range(max_retries + 1):
                # Use different seed on retry
                current_seed = seed if attempt == 0 else random.randint(0, 2**32 - 1)
                generator = torch.Generator(device=gen_device).manual_seed(current_seed)

                if attempt > 0:
                    logger.info(f"Retry {attempt}/{max_retries} with new seed: {current_seed}")
                else:
                    logger.info(f"Generating: {prompt[:50]}... (seed={current_seed}, steps={steps})")

                # Update last used at START of generation to prevent idle timeout during long generations
                self._last_used = time.time()

                # Default negative prompt, from settings (Admin → Image Generation).
                #
                # Its default now negates monochrome/sketch, and that is the whole reason this is not
                # a literal any more: the anime path loads a Danbooru-tagged checkpoint whose training
                # set is full of monochrome manga tagged exactly those words, so without them an
                # ordinary "cute girl, anime" prompt kept returning a COLOURLESS LINE SKETCH.
                if not negative_prompt:
                    negative_prompt = self.default_negative

                result = self._pipe(
                    prompt=prompt,
                    negative_prompt=negative_prompt,
                    width=width,
                    height=height,
                    num_inference_steps=steps,
                    guidance_scale=cfg,
                    generator=generator,
                )

                # Validate result contains images
                if not result.images or len(result.images) == 0:
                    logger.error("Pipeline returned no images")
                    # Clean up before retrying
                    del result
                    result = None
                    gc.collect()
                    if hasattr(torch, "xpu") and torch.xpu.is_available():
                        torch.xpu.empty_cache()
                    elif torch.cuda.is_available():
                        torch.cuda.empty_cache()
                    if attempt == max_retries:
                        return None
                    continue

                image = result.images[0]

                # Validate image is not blank
                if not is_image_blank(image):
                    # Image is valid, break out of retry loop
                    break
                else:
                    logger.warning(f"Generated blank image on attempt {attempt + 1}/{max_retries + 1}, retrying...")
                    del result
                    del image
                    result = None
                    image = None
                    gc.collect()
                    # Clean up GPU memory for all device types
                    if hasattr(torch, "xpu") and torch.xpu.is_available():
                        torch.xpu.empty_cache()
                    elif torch.cuda.is_available():
                        torch.cuda.empty_cache()
                    
                    # If this was the last attempt, return None
                    if attempt == max_retries:
                        logger.error("Generated blank image after all retries, returning None")
                        return None

            # At this point, if we have a valid image, result and image should be set
            # If we don't have an image, something went wrong
            if image is None or result is None:
                logger.error("No valid image generated after retries")
                return None

            # Convert to bytes
            img_byte_arr = io.BytesIO()
            image.save(img_byte_arr, format='PNG')
            img_bytes = img_byte_arr.getvalue()

            # Validate bytes are not empty
            if not img_bytes or len(img_bytes) < 100:  # PNG header is ~100 bytes minimum
                logger.error(f"Generated image bytes are empty or too small: {len(img_bytes)} bytes")
                del result
                del image
                gc.collect()
                # Clean up GPU memory for all device types
                if hasattr(torch, "xpu") and torch.xpu.is_available():
                    torch.xpu.empty_cache()
                elif torch.cuda.is_available():
                    torch.cuda.empty_cache()
                return None

            logger.info(f"Generation complete: {len(img_bytes)} bytes")

            # Update last used timestamp for idle timeout
            self._last_used = time.time()

            # Cleanup to free VRAM
            del result
            gc.collect()
            # Clean up GPU memory for all device types
            if hasattr(torch, "xpu") and torch.xpu.is_available():
                torch.xpu.empty_cache()
            elif torch.cuda.is_available():
                torch.cuda.empty_cache()

            return img_bytes

        except Exception as e:
            logger.error(f"Generation error: {e}")
            # Try to clean up on error
            try:
                gc.collect()
                import torch
                # Clean up GPU memory for all device types
                if hasattr(torch, "xpu") and torch.xpu.is_available():
                    torch.xpu.empty_cache()
                elif torch.cuda.is_available():
                    torch.cuda.empty_cache()
            except Exception:
                pass
            return None

    def _generate_subprocess(
        self,
        prompt: str,
        negative_prompt: str = "",
        width: int = None,
        height: int = None,
        steps: int = None,
        cfg: float = None,
        seed: int = None,
    ) -> Optional[str]:
        """
        Generate image using subprocess for guaranteed VRAM release.
        Returns base64 encoded image or None.
        """
        import subprocess
        import json
        import os

        # Select model based on prompt
        target_model = self._get_model_for_prompt(prompt)

        # Use defaults if not specified
        width = width or self.default_width
        height = height or self.default_height
        steps = steps or self.default_steps
        cfg = cfg or self.default_cfg

        # Build config
        config = {
            "model_path": target_model,
            "model_type": self.model_type,
            "prompt": self._truncate_prompt(prompt),
            "negative_prompt": negative_prompt or "bad quality, blurry, distorted, ugly, deformed, low resolution",
            "width": width,
            "height": height,
            "steps": steps,
            "cfg": cfg,
            "seed": seed,
            "device": self._device,
            "attention_slicing": self._attention_slicing,
        }

        _repo = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
        script_path = os.path.join(_repo, "scripts", "generate_image_subprocess.py")
        # Pick the image venv's python. Prefer the modern torch-2.8 venv (venv-xpu-new); fall back to
        # the legacy venv-xpu, then the current interpreter. NEVER mix venvs - loading one venv's
        # torch/libsycl with another's libur_loader fails with a LIBUR_LOADER version mismatch.
        import sys
        python_path = sys.executable
        for _v in ("venv-unified", "venv-xpu-new", "venv-xpu"):
            _p = os.path.join(_repo, _v, "bin", "python")
            if os.path.exists(_p):
                python_path = _p
                break

        logger.info(f"Subprocess generation: {prompt[:50]}... (device={self._device})")

        try:
            # Inherit environment, especially LD_LIBRARY_PATH for oneAPI/XPU
            env = os.environ.copy()
            # Ensure oneAPI paths are in LD_LIBRARY_PATH if not already
            if "LD_LIBRARY_PATH" in env:
                # Keep existing paths
                pass
            else:
                env["LD_LIBRARY_PATH"] = ""
            
            result = subprocess.run(
                [python_path, script_path, json.dumps(config)],
                capture_output=True,
                text=True,
                timeout=self._request_timeout,  # admin image_timeout setting (default 300s)
                cwd=os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
                env=env,
            )

            if result.returncode != 0:
                # Check if stderr contains actual errors (not just kernel warnings)
                stderr_text = result.stderr[:1000] if result.stderr else ""
                # Kernel registration warnings are harmless - check for actual errors
                if "kernel" in stderr_text.lower() and "warning" in stderr_text.lower():
                    # Might just be kernel warnings, check if we got valid JSON output
                    if result.stdout.strip():
                        try:
                            output = json.loads(result.stdout.strip())
                            if "error" not in output:
                                # Got valid output despite warnings, use it
                                logger.warning(f"Subprocess had kernel warnings but succeeded: {stderr_text[:200]}")
                                logger.info(f"Subprocess generation complete (seed={output.get('seed')})")
                                return output.get("image")
                        except json.JSONDecodeError:
                            pass  # Not valid JSON, continue with error handling
                
                logger.error(f"Subprocess failed (exit={result.returncode}): {stderr_text}")
                # Try to parse error from stdout if available
                if result.stdout.strip():
                    try:
                        output = json.loads(result.stdout.strip())
                        if "error" in output:
                            logger.error(f"Subprocess error: {output['error']}")
                            if "traceback" in output:
                                logger.debug(f"Traceback: {output['traceback'][:500]}")
                    except json.JSONDecodeError:
                        pass
                return None

            # Log stderr warnings but don't fail on them (kernel registration warnings are harmless)
            if result.stderr:
                # Only log if it's not just kernel warnings
                if "kernel" not in result.stderr.lower() or "warning" not in result.stderr.lower():
                    logger.debug(f"Subprocess stderr: {result.stderr[:200]}")

            # Parse output
            try:
                output = json.loads(result.stdout.strip())
            except json.JSONDecodeError:
                logger.error(f"Invalid subprocess output: {result.stdout[:200]}")
                return None

            if "error" in output:
                logger.error(f"Subprocess error: {output['error']}")
                return None

            logger.info(f"Subprocess generation complete (seed={output.get('seed')})")
            return output.get("image")

        except subprocess.TimeoutExpired:
            logger.error("Subprocess generation timed out")
            return None
        except Exception as e:
            logger.error(f"Subprocess error: {e}")
            return None

    async def generate_image(self, prompt: str, negative_prompt: str = "",
                            width: int = None, height: int = None,
                            steps: int = None, cfg: float = None,
                            seed: int = None) -> Optional[str]:
        """
        Generate image from prompt.
        Returns base64 encoded image or None.
        If subprocess_mode is enabled, runs in separate process for guaranteed VRAM release.
        If image_idle_timeout is 0, unloads model immediately after generation.
        """
        # Reload settings to ensure we have the latest idle_timeout value
        self._load_settings()
        
        loop = asyncio.get_event_loop()

        # Bracket the whole run so the idle monitor won't unload the pipe mid-generation.
        self._generating += 1
        try:
            # Use subprocess mode for guaranteed VRAM release (recommended for Intel XPU)
            if self._subprocess_mode:
                logger.info("Using subprocess mode for image generation")
                return await loop.run_in_executor(
                    _executor,
                    lambda: self._generate_subprocess(prompt, negative_prompt, width, height, steps, cfg, seed)
                )

            # Standard in-process generation
            img_bytes = await loop.run_in_executor(
                _executor,
                lambda: self._generate_sync(prompt, negative_prompt, width, height, steps, cfg, seed)
            )

            # Always unload model after generation to release VRAM
            logger.info(f"Post-generation: unloading to release VRAM")
            self.unload_model()

            if img_bytes:
                return base64.b64encode(img_bytes).decode()
            return None
        finally:
            self._generating -= 1


def get_diffusers_service(db: Session) -> DiffusersService:
    """Get or create the singleton DiffusersService instance"""
    global _diffusers_instance

    if _diffusers_instance is None:
        _diffusers_instance = DiffusersService(db)
    else:
        # Update db session
        _diffusers_instance.db = db
        _diffusers_instance._load_settings()

    return _diffusers_instance


def reload_diffusers_model(db: Session):
    """Reload the diffusers model"""
    global _diffusers_instance

    if _diffusers_instance is not None:
        _diffusers_instance.db = db
        _diffusers_instance.reload_model()
    else:
        _diffusers_instance = DiffusersService(db)
        _diffusers_instance._ensure_model_loaded()
