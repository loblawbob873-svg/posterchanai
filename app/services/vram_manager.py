"""
VRAM Manager - Handles GPU memory when the LLM and image models share the same GPU.
The local LLM is always native llama.cpp and local image gen is always native diffusers.
Modes:
- shared: swap models in/out of VRAM (single GPU) - default
- dedicated: keep both loaded (dual GPU or high VRAM)
- llm_only: keep LLM loaded, image uses an external image server (image_server_urls)
- image_only: keep image model loaded, LLM uses an external/remote server
"""
import logging
import threading
from typing import Optional, Literal
from sqlalchemy.orm import Session


# Configure logging
logger = logging.getLogger("vram_manager")
logger.setLevel(logging.INFO)
logger.propagate = False
if not logger.handlers:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter('%(asctime)s [VRAM] %(message)s', datefmt='%Y-%m-%d %H:%M:%S'))
    logger.addHandler(handler)

# Global state
_current_mode: Optional[Literal["llm", "image", "music"]] = None
_swap_lock = threading.Lock()


def _get_vram_settings(db: Session) -> dict:
    """Get VRAM management settings from database"""
    from app.database import safe_query_settings
    settings = safe_query_settings(db)
    return {
        "vram_mode": settings.get("vram_mode", "shared"),  # shared | dedicated | llm_only | image_only
    }


def get_current_mode() -> Optional[str]:
    """Get current VRAM mode (which model is loaded)"""
    return _current_mode

def reset_vram_mode():
    """Reset VRAM mode (call when models are unloaded outside of VRAM manager)"""
    global _current_mode
    _current_mode = None
    logger.debug("VRAM mode reset to None")


def prepare_for_llm(db: Session) -> bool:
    """
    Prepare VRAM for LLM inference. In shared mode, unloads the image model first.
    Returns True if ready, False on error.
    """
    global _current_mode

    settings = _get_vram_settings(db)
    vram_mode = settings["vram_mode"]

    # image_only mode - LLM should use an external/remote server, don't load locally
    if vram_mode == "image_only":
        logger.debug("image_only mode - LLM uses external service")
        return True

    # In dedicated or llm_only mode, just ensure LLM is loaded
    if vram_mode in ("dedicated", "llm_only"):
        _ensure_llm_loaded(db)
        _current_mode = "llm"
        return True

    # Shared mode - need to swap
    with _swap_lock:
        if _current_mode == "llm":
            return True

        # Unload the native image model to free VRAM
        try:
            from app.services.diffusers_service import get_diffusers_service
            service = get_diffusers_service(db)
            if service.is_loaded():
                logger.info("Unloading image model to free VRAM for LLM...")
                service.unload_model()
                _current_mode = None
        except Exception as e:
            logger.error(f"Error unloading image model: {e}")

        _ensure_llm_loaded(db)
        _current_mode = "llm"
        logger.info("VRAM ready for LLM")
        return True


def prepare_for_image(db: Session) -> bool:
    """
    Prepare VRAM for local image generation. In shared mode, unloads the LLM first.
    Returns True if ready, False on error.
    """
    global _current_mode

    settings = _get_vram_settings(db)
    vram_mode = settings["vram_mode"]

    # llm_only mode - image should use an external image server, don't load locally
    if vram_mode == "llm_only":
        logger.debug("llm_only mode - image uses external service")
        return True

    # In dedicated or image_only mode, just ensure the image model is loaded
    if vram_mode in ("dedicated", "image_only"):
        _ensure_image_loaded(db)
        _current_mode = "image"
        return True

    # Shared mode - need to swap
    with _swap_lock:
        if _current_mode == "image":
            return True

        # Unload the native LLM to free VRAM
        try:
            from app.services.llama_service import get_llama_service
            service = get_llama_service(db)
            if service._model is not None:
                logger.info("Unloading LLM to free VRAM for image generation...")
                service.unload_model()
                _current_mode = None
        except Exception as e:
            logger.error(f"Error unloading LLM: {e}")

        _ensure_image_loaded(db)
        _current_mode = "image"
        logger.info("VRAM ready for image generation")
        return True


def prepare_for_music(db: Session) -> bool:
    """Prepare VRAM for LOCAL music generation (an ACE-Step server co-located on this GPU).

    Music runs in a SEPARATE process (the acestep REST server), so unlike the LLM/image we can't
    load/unload its model from here — it manages (and idle-unloads) its own weights. What we CAN
    do, in shared mode, is unload OUR in-process LLM and image models to free VRAM for it. No-op
    in dedicated mode (assumes enough VRAM / a separate GPU). Always paired with the GPU lock in
    music_factory so only one model uses the GPU at a time."""
    global _current_mode

    settings = _get_vram_settings(db)
    vram_mode = settings["vram_mode"]

    if vram_mode == "dedicated":
        _current_mode = "music"
        return True

    # Shared (and the llm_only/image_only single-purpose modes): free our own models so the
    # co-located music server has room.
    with _swap_lock:
        if _current_mode == "music":
            return True
        try:
            from app.services.llama_service import get_llama_service
            service = get_llama_service(db)
            if service._model is not None:
                logger.info("Unloading LLM to free VRAM for music generation...")
                service.unload_model()
        except Exception as e:
            logger.error(f"Error unloading LLM for music: {e}")
        try:
            from app.services.diffusers_service import get_diffusers_service
            service = get_diffusers_service(db)
            if service.is_loaded():
                logger.info("Unloading image model to free VRAM for music generation...")
                service.unload_model()
        except Exception as e:
            logger.error(f"Error unloading image model for music: {e}")
        _current_mode = "music"
        logger.info("VRAM ready for music generation")
        return True


def _ensure_llm_loaded(db: Session):
    """Ensure the native LLM service is initialized (model loaded on-demand during inference,
    inside the GPU lock, to prevent concurrent load attempts)."""
    try:
        from app.services.llama_service import get_llama_service
        get_llama_service(db)
    except Exception as e:
        logger.error(f"Error initializing LLM service: {e}")
        raise


def _ensure_image_loaded(db: Session):
    """Ensure the native image service is ready (model loaded on-demand during generation)."""
    try:
        from app.services.diffusers_service import get_diffusers_service
        get_diffusers_service(db)
    except Exception as e:
        logger.error(f"Error initializing image service: {e}")
        raise


def get_vram_status(db: Session) -> dict:
    """Get current VRAM status"""
    settings = _get_vram_settings(db)

    llm_loaded = False
    image_loaded = False

    try:
        from app.services.llama_service import get_llama_service
        llm_loaded = get_llama_service(db)._model is not None
    except Exception:
        pass

    try:
        from app.services.diffusers_service import get_diffusers_service
        image_loaded = get_diffusers_service(db).is_loaded()
    except Exception:
        pass

    return {
        "vram_mode": settings["vram_mode"],
        "current_mode": _current_mode,
        "llm_loaded": llm_loaded,
        "image_loaded": image_loaded,
    }
