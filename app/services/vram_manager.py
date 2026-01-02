"""
VRAM Manager - Handles GPU memory when LLM and Image models share the same GPU.
Supports two modes:
- shared: Swap models in/out of VRAM (single GPU)
- dedicated: Keep both loaded (dual GPU or high VRAM)
"""
import logging
import threading
from typing import Optional, Literal
from sqlalchemy.orm import Session

from app.models import Setting

# Configure logging
logger = logging.getLogger("vram_manager")
logger.setLevel(logging.INFO)
logger.propagate = False
if not logger.handlers:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter('%(asctime)s [VRAM] %(message)s', datefmt='%Y-%m-%d %H:%M:%S'))
    logger.addHandler(handler)

# Global state
_current_mode: Optional[Literal["llm", "image"]] = None
_swap_lock = threading.Lock()


def _get_vram_settings(db: Session) -> dict:
    """Get VRAM management settings from database"""
    settings = {s.key: s.value for s in db.query(Setting).all()}
    return {
        "vram_mode": settings.get("vram_mode", "shared"),  # "shared" or "dedicated"
        "llm_backend": settings.get("llm_backend", "native"),
        "image_backend": settings.get("image_backend", "comfyui"),
    }


def get_current_mode() -> Optional[str]:
    """Get current VRAM mode (which model is loaded)"""
    return _current_mode


def prepare_for_llm(db: Session) -> bool:
    """
    Prepare VRAM for LLM inference.
    In shared mode, unloads image model first.
    Returns True if ready, False on error.
    """
    global _current_mode

    settings = _get_vram_settings(db)

    # If using external services (ollama/comfyui), no VRAM management needed
    if settings["llm_backend"] == "ollama":
        return True

    # In dedicated mode, just ensure LLM is loaded
    if settings["vram_mode"] == "dedicated":
        _ensure_llm_loaded(db, settings)
        _current_mode = "llm"
        return True

    # Shared mode - need to swap
    with _swap_lock:
        if _current_mode == "llm":
            # Already in LLM mode
            return True

        # Unload image model if using native backend
        if settings["image_backend"] == "native":
            try:
                from app.services.diffusers_service import get_diffusers_service
                service = get_diffusers_service(db)
                if service.is_loaded():
                    logger.info("Unloading image model to free VRAM for LLM...")
                    service.unload_model()
            except Exception as e:
                logger.error(f"Error unloading image model: {e}")

        # Load LLM
        _ensure_llm_loaded(db, settings)
        _current_mode = "llm"
        logger.info("VRAM ready for LLM")
        return True


def prepare_for_image(db: Session) -> bool:
    """
    Prepare VRAM for image generation.
    In shared mode, unloads LLM first.
    Returns True if ready, False on error.
    """
    global _current_mode

    settings = _get_vram_settings(db)

    # If using ComfyUI or disabled, no VRAM management needed on our side
    if settings["image_backend"] in ("comfyui", "disabled"):
        return True

    # In dedicated mode, just ensure image model is loaded
    if settings["vram_mode"] == "dedicated":
        _ensure_image_loaded(db, settings)
        _current_mode = "image"
        return True

    # Shared mode - need to swap
    with _swap_lock:
        if _current_mode == "image":
            # Already in image mode
            return True

        # Unload LLM if using native backend
        if settings["llm_backend"] in ("native", "ipex"):
            try:
                if settings["llm_backend"] == "native":
                    from app.services.llama_service import get_llama_service
                    service = get_llama_service(db)
                else:
                    from app.services.ipex_service import get_ipex_service
                    service = get_ipex_service(db)

                if service._model is not None:
                    logger.info("Unloading LLM to free VRAM for image generation...")
                    service.unload_model()
            except Exception as e:
                logger.error(f"Error unloading LLM: {e}")

        # Load image model
        _ensure_image_loaded(db, settings)
        _current_mode = "image"
        logger.info("VRAM ready for image generation")
        return True


def _ensure_llm_loaded(db: Session, settings: dict):
    """Ensure LLM is loaded"""
    try:
        if settings["llm_backend"] == "native":
            from app.services.llama_service import get_llama_service
            service = get_llama_service(db)
            service._ensure_model_loaded()
        elif settings["llm_backend"] == "ipex":
            from app.services.ipex_service import get_ipex_service
            service = get_ipex_service(db)
            service._ensure_model_loaded()
    except Exception as e:
        logger.error(f"Error loading LLM: {e}")
        raise


def _ensure_image_loaded(db: Session, settings: dict):
    """Ensure image model is loaded"""
    try:
        if settings["image_backend"] == "native":
            from app.services.diffusers_service import get_diffusers_service
            service = get_diffusers_service(db)
            service._ensure_model_loaded()
    except Exception as e:
        logger.error(f"Error loading image model: {e}")
        raise


def get_vram_status(db: Session) -> dict:
    """Get current VRAM status"""
    settings = _get_vram_settings(db)

    llm_loaded = False
    image_loaded = False

    # Check LLM status
    if settings["llm_backend"] == "native":
        try:
            from app.services.llama_service import get_llama_service
            service = get_llama_service(db)
            llm_loaded = service._model is not None
        except Exception:
            pass
    elif settings["llm_backend"] == "ipex":
        try:
            from app.services.ipex_service import get_ipex_service
            service = get_ipex_service(db)
            llm_loaded = service._model is not None
        except Exception:
            pass
    elif settings["llm_backend"] == "ollama":
        llm_loaded = True  # External service

    # Check image status
    if settings["image_backend"] == "native":
        try:
            from app.services.diffusers_service import get_diffusers_service
            service = get_diffusers_service(db)
            image_loaded = service.is_loaded()
        except Exception:
            pass
    elif settings["image_backend"] == "comfyui":
        image_loaded = True  # External service

    return {
        "vram_mode": settings["vram_mode"],
        "current_mode": _current_mode,
        "llm_loaded": llm_loaded,
        "image_loaded": image_loaded,
        "llm_backend": settings["llm_backend"],
        "image_backend": settings["image_backend"],
    }
