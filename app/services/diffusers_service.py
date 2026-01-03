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

from app.models import Setting

# Configure logging
logger = logging.getLogger("diffusers_service")
logger.setLevel(logging.INFO)
logger.propagate = False
if not logger.handlers:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter('%(asctime)s [DIFFUSERS] %(message)s', datefmt='%Y-%m-%d %H:%M:%S'))
    logger.addHandler(handler)

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


def _idle_check_loop():
    """Background thread to check for idle timeout and unload models"""
    global _diffusers_instance
    while not _idle_check_stop.wait(30):  # Check every 30 seconds
        if _diffusers_instance is not None and _diffusers_instance._pipe is not None:
            idle_time = time.time() - _diffusers_instance._last_used
            timeout = _diffusers_instance._idle_timeout
            if idle_time > timeout:
                logger.info(f"Model idle for {idle_time:.0f}s (>{timeout}s), unloading to free VRAM")
                _diffusers_instance.unload_model()


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
    Provides txt2img and img2img capabilities.
    Automatically unloads models after idle timeout to free VRAM.
    """

    def __init__(self, db: Session):
        self.db = db
        self._pipe = None
        self._img2img_pipe = None  # Cached img2img pipeline for faster generation
        self._inpaint_pipe = None  # Cached inpaint pipeline
        self._model_path: Optional[str] = None
        self._model_type: Optional[str] = None  # "sd15", "sdxl", "sd3", "flux"
        self._device: Optional[str] = None
        self._last_used: float = time.time()
        self._idle_timeout: int = DEFAULT_IDLE_TIMEOUT
        self._load_settings()
        _start_idle_check()

    def _load_settings(self):
        """Load settings from database"""
        settings = {s.key: s.value for s in self.db.query(Setting).all()}

        # Idle timeout for automatic unloading (default 2 minutes)
        self._idle_timeout = int(settings.get("image_idle_timeout", str(DEFAULT_IDLE_TIMEOUT)))

        # Model settings
        self.model_path = settings.get("image_model_path", "")
        self.anime_model_path = settings.get("image_anime_model_path", "")
        self.model_type = settings.get("image_model_type", "sdxl")  # sd15, sdxl, sd3, flux

        # Generation defaults
        self.default_steps = int(settings.get("image_default_steps", "20"))
        self.default_cfg = float(settings.get("image_default_cfg", "7.0"))
        self.default_width = int(settings.get("image_default_width", "1024"))
        self.default_height = int(settings.get("image_default_height", "1024"))

        # Device settings
        device_setting = settings.get("image_gpu_device", "auto")
        if device_setting == "auto":
            self._device = detect_device()
        else:
            self._device = device_setting

        # Backend setting
        self.backend = settings.get("image_backend", "comfyui")  # "native" or "comfyui"

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
                    StableDiffusionImg2ImgPipeline,
                    StableDiffusionXLImg2ImgPipeline,
                    AutoPipelineForText2Image,
                    AutoPipelineForImage2Image,
                )

                # Determine dtype based on device
                if self._device == "cpu":
                    dtype = torch.float32
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
                    else:
                        self._pipe = self._pipe.to(self._device)

                    try:
                        self._pipe.enable_attention_slicing()
                    except Exception:
                        pass

                    try:
                        self._pipe.enable_vae_slicing()
                    except Exception:
                        pass

                    try:
                        self._pipe.enable_vae_tiling()
                    except Exception:
                        pass

                    try:
                        self._pipe.enable_xformers_memory_efficient_attention()
                        logger.info("Enabled xformers memory efficient attention")
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
        if self._pipe is not None or self._img2img_pipe is not None or self._inpaint_pipe is not None:
            # Clear all pipelines
            if self._inpaint_pipe is not None:
                del self._inpaint_pipe
                self._inpaint_pipe = None
            if self._img2img_pipe is not None:
                del self._img2img_pipe
                self._img2img_pipe = None
            if self._pipe is not None:
                del self._pipe
                self._pipe = None
            self._model_path = None

            # Force garbage collection - run twice for thorough cleanup
            gc.collect()
            gc.collect()

            try:
                import torch
                if torch.cuda.is_available():
                    # Works for both NVIDIA CUDA and AMD ROCm
                    torch.cuda.empty_cache()
                    torch.cuda.synchronize()
                    # ROCm may need additional cleanup
                    if is_rocm():
                        # Reset memory stats and run IPC collect for ROCm
                        torch.cuda.reset_peak_memory_stats()
                        try:
                            torch.cuda.ipc_collect()
                        except Exception:
                            pass
                        logger.debug("Cleared ROCm HIP memory cache")
                elif hasattr(torch, "xpu") and torch.xpu.is_available():
                    torch.xpu.empty_cache()
            except Exception:
                pass

            # Additional gc pass after CUDA cleanup
            gc.collect()

            logger.info("Model unloaded, VRAM freed")

    def unload_model(self):
        """Unload model and free VRAM"""
        with _load_lock:
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
            generator = torch.Generator(device=gen_device).manual_seed(seed)

            logger.info(f"Generating: {prompt[:50]}... (seed={seed}, steps={steps})")

            # Update last used at START of generation to prevent idle timeout during long generations
            self._last_used = time.time()

            # Default negative prompt
            if not negative_prompt:
                negative_prompt = "bad quality, blurry, distorted, ugly, deformed, low resolution"

            result = self._pipe(
                prompt=prompt,
                negative_prompt=negative_prompt,
                width=width,
                height=height,
                num_inference_steps=steps,
                guidance_scale=cfg,
                generator=generator,
            )

            image = result.images[0]

            # Convert to bytes
            img_byte_arr = io.BytesIO()
            image.save(img_byte_arr, format='PNG')
            img_bytes = img_byte_arr.getvalue()

            logger.info(f"Generation complete: {len(img_bytes)} bytes")

            # Update last used timestamp for idle timeout
            self._last_used = time.time()

            # Cleanup to free VRAM
            del result
            gc.collect()
            if torch.cuda.is_available():
                torch.cuda.empty_cache()

            return img_bytes

        except Exception as e:
            logger.error(f"Generation error: {e}")
            # Try to clean up on error
            try:
                gc.collect()
                import torch
                if torch.cuda.is_available():
                    torch.cuda.empty_cache()
            except Exception:
                pass
            return None

    def _get_img2img_pipe(self):
        """Get or create cached img2img pipeline"""
        if self._img2img_pipe is not None:
            return self._img2img_pipe

        if self._pipe is None:
            return None

        from diffusers import AutoPipelineForImage2Image
        logger.info("Creating img2img pipeline from txt2img pipeline...")
        self._img2img_pipe = AutoPipelineForImage2Image.from_pipe(self._pipe)
        logger.info("img2img pipeline ready")
        return self._img2img_pipe

    def _get_inpaint_pipe(self):
        """Get or create cached inpaint pipeline"""
        if self._inpaint_pipe is not None:
            return self._inpaint_pipe

        if self._pipe is None:
            return None

        from diffusers import AutoPipelineForInpainting
        logger.info("Creating inpaint pipeline from txt2img pipeline...")
        self._inpaint_pipe = AutoPipelineForInpainting.from_pipe(self._pipe)
        logger.info("inpaint pipeline ready")
        return self._inpaint_pipe

    def _img2img_sync(
        self,
        prompt: str,
        image_bytes: bytes,
        negative_prompt: str = "",
        strength: float = 0.75,
        steps: int = None,
        cfg: float = None,
        seed: int = None,
    ) -> Optional[bytes]:
        """Synchronous img2img generation"""
        # Truncate prompt to avoid token limit errors
        prompt = self._truncate_prompt(prompt)

        # Select model based on prompt (anime vs default)
        target_model = self._get_model_for_prompt(prompt)
        self._ensure_model_loaded(target_model)

        if self._pipe is None:
            logger.error("No model loaded")
            return None

        steps = steps or self.default_steps
        cfg = cfg or self.default_cfg

        if seed is None or seed < 0:
            seed = random.randint(0, 2**32 - 1)

        try:
            import torch

            # Load source image
            source_image = Image.open(io.BytesIO(image_bytes)).convert("RGB")

            # Resize large images to prevent OOM (use admin-configured dimensions)
            max_dim = max(self.default_width, self.default_height)
            w, h = source_image.size
            if max(w, h) > max_dim:
                if w > h:
                    new_w = max_dim
                    new_h = int(h * max_dim / w)
                else:
                    new_h = max_dim
                    new_w = int(w * max_dim / h)
                # Round to nearest 8 (required for SDXL)
                new_w = (new_w // 8) * 8
                new_h = (new_h // 8) * 8
                logger.info(f"Resizing {w}x{h} -> {new_w}x{new_h} to prevent OOM")
                source_image = source_image.resize((new_w, new_h), Image.Resampling.LANCZOS)

            # Get cached img2img pipeline (much faster than recreating each time)
            img2img_pipe = self._get_img2img_pipe()
            if img2img_pipe is None:
                logger.error("Failed to get img2img pipeline")
                return None

            # model_cpu_offload (ROCm) needs CPU generator
            gen_device = "cpu" if is_rocm() else self._device
            generator = torch.Generator(device=gen_device).manual_seed(seed)

            logger.info(f"img2img: {prompt[:50]}... (strength={strength}, seed={seed})")

            if not negative_prompt:
                negative_prompt = "bad quality, blurry, distorted, ugly, deformed, low resolution"

            result = img2img_pipe(
                prompt=prompt,
                image=source_image,
                negative_prompt=negative_prompt,
                strength=strength,
                num_inference_steps=steps,
                guidance_scale=cfg,
                generator=generator,
            )

            image = result.images[0]

            img_byte_arr = io.BytesIO()
            image.save(img_byte_arr, format='PNG')
            img_bytes = img_byte_arr.getvalue()

            logger.info(f"img2img complete: {len(img_bytes)} bytes")

            # Update last used timestamp for idle timeout
            self._last_used = time.time()

            # Cleanup result only (keep pipeline cached)
            del result
            gc.collect()
            if torch.cuda.is_available():
                torch.cuda.empty_cache()

            return img_bytes

        except Exception as e:
            logger.error(f"img2img error: {e}")
            # Try to clean up on error too
            try:
                gc.collect()
                import torch
                if torch.cuda.is_available():
                    torch.cuda.empty_cache()
            except Exception:
                pass
            return None

    def _inpaint_sync(
        self,
        prompt: str,
        image_bytes: bytes,
        mask_bytes: bytes,
        negative_prompt: str = "",
        strength: float = 0.85,
        steps: int = None,
        cfg: float = None,
        seed: int = None,
    ) -> Optional[bytes]:
        """Synchronous inpainting generation - paint over masked areas"""
        # Truncate prompt to avoid token limit errors
        prompt = self._truncate_prompt(prompt)

        # Select model based on prompt (anime vs default)
        target_model = self._get_model_for_prompt(prompt)
        self._ensure_model_loaded(target_model)

        if self._pipe is None:
            logger.error("No model loaded")
            return None

        steps = steps or self.default_steps
        cfg = cfg or self.default_cfg

        if seed is None or seed < 0:
            seed = random.randint(0, 2**32 - 1)

        try:
            import torch

            # Load source image and mask
            source_image = Image.open(io.BytesIO(image_bytes)).convert("RGB")
            mask_image = Image.open(io.BytesIO(mask_bytes)).convert("L")  # Grayscale

            # Ensure mask is same size as source
            if mask_image.size != source_image.size:
                mask_image = mask_image.resize(source_image.size, Image.Resampling.LANCZOS)

            # Resize large images to prevent OOM
            max_dim = max(self.default_width, self.default_height)
            w, h = source_image.size
            if max(w, h) > max_dim:
                if w > h:
                    new_w = max_dim
                    new_h = int(h * max_dim / w)
                else:
                    new_h = max_dim
                    new_w = int(w * max_dim / h)
                # Round to nearest 8 (required for SDXL)
                new_w = (new_w // 8) * 8
                new_h = (new_h // 8) * 8
                logger.info(f"Resizing {w}x{h} -> {new_w}x{new_h} to prevent OOM")
                source_image = source_image.resize((new_w, new_h), Image.Resampling.LANCZOS)
                mask_image = mask_image.resize((new_w, new_h), Image.Resampling.LANCZOS)

            # Get cached inpaint pipeline
            inpaint_pipe = self._get_inpaint_pipe()
            if inpaint_pipe is None:
                logger.error("Failed to get inpaint pipeline")
                return None

            # model_cpu_offload (ROCm) needs CPU generator
            gen_device = "cpu" if is_rocm() else self._device
            generator = torch.Generator(device=gen_device).manual_seed(seed)

            logger.info(f"inpaint: {prompt[:50]}... (strength={strength}, seed={seed})")

            if not negative_prompt:
                negative_prompt = "bad quality, blurry, distorted, ugly, deformed, low resolution"

            # Update last used at START to prevent idle timeout
            self._last_used = time.time()

            result = inpaint_pipe(
                prompt=prompt,
                image=source_image,
                mask_image=mask_image,
                negative_prompt=negative_prompt,
                strength=strength,
                num_inference_steps=steps,
                guidance_scale=cfg,
                generator=generator,
            )

            image = result.images[0]

            img_byte_arr = io.BytesIO()
            image.save(img_byte_arr, format='PNG')
            img_bytes = img_byte_arr.getvalue()

            logger.info(f"inpaint complete: {len(img_bytes)} bytes")

            # Update last used timestamp for idle timeout
            self._last_used = time.time()

            # Cleanup result only (keep pipeline cached)
            del result
            gc.collect()
            if torch.cuda.is_available():
                torch.cuda.empty_cache()

            return img_bytes

        except Exception as e:
            logger.error(f"inpaint error: {e}")
            try:
                gc.collect()
                import torch
                if torch.cuda.is_available():
                    torch.cuda.empty_cache()
            except Exception:
                pass
            return None

    async def generate_image(self, prompt: str, negative_prompt: str = "",
                            width: int = None, height: int = None,
                            steps: int = None, cfg: float = None,
                            seed: int = None) -> Optional[str]:
        """
        Generate image from prompt.
        Returns base64 encoded image or None.
        """
        loop = asyncio.get_event_loop()

        img_bytes = await loop.run_in_executor(
            _executor,
            lambda: self._generate_sync(prompt, negative_prompt, width, height, steps, cfg, seed)
        )

        if img_bytes:
            return base64.b64encode(img_bytes).decode()
        return None

    async def generate_img2img(self, prompt: str, image_bytes: bytes,
                               denoise: float = 0.75,
                               negative_prompt: str = None) -> Optional[str]:
        """
        Generate image from prompt using source image as base.
        denoise is converted to strength (0.0 = keep original, 1.0 = full denoise)
        Returns base64 encoded image or None.
        """
        loop = asyncio.get_event_loop()

        # denoise parameter matches ComfyUI convention
        strength = denoise

        img_bytes = await loop.run_in_executor(
            _executor,
            lambda: self._img2img_sync(prompt, image_bytes, negative_prompt or "", strength)
        )

        if img_bytes:
            return base64.b64encode(img_bytes).decode()
        return None

    async def generate_inpaint(self, prompt: str, image_bytes: bytes,
                               mask_bytes: bytes, denoise: float = 0.85,
                               negative_prompt: str = None) -> Optional[str]:
        """
        Inpaint masked areas of image with prompt.
        Mask should be white (255) where to inpaint, black (0) where to keep.
        denoise/strength controls how much to change masked area (0.0-1.0)
        Returns base64 encoded image or None.
        """
        loop = asyncio.get_event_loop()

        strength = denoise

        img_bytes = await loop.run_in_executor(
            _executor,
            lambda: self._inpaint_sync(prompt, image_bytes, mask_bytes, negative_prompt or "", strength)
        )

        if img_bytes:
            return base64.b64encode(img_bytes).decode()
        return None

    async def regenerate_image(self, prompt: str) -> Optional[str]:
        """Regenerate image with same prompt (uses different seed)"""
        return await self.generate_image(prompt)


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
