"""
VRAM Manager - Handles GPU memory when the LLM and image models share the same GPU.
The local LLM is always native llama.cpp and local image gen is always native diffusers.
Modes:
- shared: swap models in/out of VRAM (single GPU) - default
- dedicated: keep both loaded (dual GPU or high VRAM)
- llm_only: keep LLM loaded, image uses external nodes (the unified chat_server_urls list)
- image_only: keep image model loaded, LLM uses an external/remote server
"""
import logging
import subprocess
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
_current_mode: Optional[Literal["llm", "image", "music", "video"]] = None
_swap_lock = threading.Lock()


def _native_music_active() -> bool:
    """True when music generates in-process (diffusers has the pipeline AND no external server is
    explicitly configured) — the same test music_factory routes on."""
    try:
        from app.services import music_local, settings_store
        if (settings_store.get("music_api_base", "") or "").strip():
            return False
        return music_local.is_available()
    except Exception:
        return False


def _unload_native_music(db: Session):
    """Free the in-process music model. Before music was native, prepare_for_* reclaimed its VRAM by
    stopping the acestep PROCESS; with the model in our own address space that no longer frees
    anything, so switching to LLM/image/video would leave a multi-GB bf16 pipe resident alongside the
    newly loaded model — an OOM on exactly the shared 12/16GB GPUs this is meant to protect."""
    try:
        from app.services import music_local
        svc = music_local.get_music_service(db)
        if svc.is_loaded():
            svc.unload_model()
    except Exception as e:
        logger.debug(f"native music unload skipped: {e}")


def _unload_native_video(db: Session):
    """Free the in-process video-generation model (Wan/diffusers) if it's loaded. Called whenever we
    swap to another GPU task so video never co-resides with LLM/image."""
    try:
        from app.services.video_service import get_video_service
        service = get_video_service(db)
        if service.is_loaded():
            logger.info("Unloading video model to free VRAM...")
            service.unload_model()
    except Exception as e:
        logger.error(f"Error unloading video model: {e}")


def _music_service_ctl(db: Session, action: str):
    """Stop/start the co-located ACE-Step music server (a SEPARATE process holding several GB) so it
    participates in the GPU VRAM swap. The in-process swap can't free another process's VRAM, so on a
    node where music + video/image share ONE GPU (e.g. a 12GB card) we stop the service to make room
    for the (large) video model, and start it again for music. Gated by the `video_free_music`
    setting (default off — other nodes unaffected). Needs passwordless sudo for `systemctl`."""
    from app.database import safe_query_settings
    s = safe_query_settings(db)
    if str(s.get("video_free_music", "false")).lower() != "true":
        return
    svc = s.get("music_service_name", "acestep") or "acestep"
    try:
        r = subprocess.run(["sudo", "-n", "systemctl", action, svc], timeout=40, capture_output=True, text=True)
        logger.info(f"music server: systemctl {action} {svc} (rc={r.returncode})")
    except Exception as e:
        logger.error(f"music service {action} failed: {e}")


def _ensure_music_server(db: Session):
    """Start the co-located music server (if managed) and wait for its port, so the first music gen
    after a video render doesn't race acestep coming back up. No-op unless `video_free_music` is on."""
    from app.database import safe_query_settings
    s = safe_query_settings(db)
    if str(s.get("video_free_music", "false")).lower() != "true":
        return
    # A stop sends SIGTERM (acestep exits 143), leaving the unit "failed" — clear that so `start`
    # reliably brings it back.
    _music_service_ctl(db, "reset-failed")
    _music_service_ctl(db, "start")
    import socket, time as _t
    from urllib.parse import urlparse
    u = urlparse(s.get("music_api_base", "http://localhost:8001") or "http://localhost:8001")
    host, port = (u.hostname or "localhost"), (u.port or 8001)
    deadline = _t.time() + 90
    while _t.time() < deadline:
        try:
            with socket.create_connection((host, port), timeout=3):
                return
        except Exception:
            _t.sleep(2)
    logger.warning("music server did not come up within 90s after start")


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

    # Complete the per-GPU swap: free the co-located ACE-Step music server's VRAM (it's a SEPARATE
    # process holding several GB, so unload_model() can't touch it) — otherwise the LLM can't get
    # GPU layers and silently runs on CPU (very slow, pegs cores). Gated by video_free_music; no-op
    # elsewhere. acestep restarts on the next music gen (prepare_for_music → _ensure_music_server).
    _music_service_ctl(db, "stop")

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

        _unload_native_video(db)
        _unload_native_music(db)
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

    # Free the co-located ACE-Step music server's VRAM (stop acestep) so the image model fits on a
    # shared GPU — mirrors prepare_for_video. Gated by video_free_music; no-op elsewhere. acestep is
    # restarted on the next music gen (prepare_for_music → _ensure_music_server).
    _music_service_ctl(db, "stop")

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

        _unload_native_video(db)
        _unload_native_music(db)
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

    # LEGACY external server only. _ensure_music_server polls localhost:8001 for up to 90s
    # SYNCHRONOUSLY, and this runs on the single uvicorn worker — so with the acestep daemon retired
    # it would block the whole app (chat, image, admin, every user) for 90s before each song, and
    # could even restart the old server into VRAM outside the GPU lock. Native music manages its own
    # weights, so skip the whole dance unless a REST server is actually configured.
    if not _native_music_active():
        _ensure_music_server(db)

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
        _unload_native_video(db)
        _current_mode = "music"
        logger.info("VRAM ready for music generation")
        return True


def prepare_for_video(db: Session) -> bool:
    """Prepare VRAM for LOCAL (native, in-process) video generation. Mirrors prepare_for_image: in
    shared mode, unloads the LLM and image models first so the Wan/diffusers video model has the GPU
    to itself. Always paired with the shared GPUResourceLock in video_factory so only one GPU task
    runs at a time. No-op model-unloading in dedicated mode."""
    global _current_mode

    settings = _get_vram_settings(db)
    vram_mode = settings["vram_mode"]

    # Free music's VRAM so the large video model fits. Music is now IN-PROCESS, so unloading the
    # pipe is what actually reclaims it — `_music_service_ctl(db, "stop")` only ever stopped the
    # retired external daemon and is a no-op today, kept solely for a node still running one.
    _unload_native_music(db)
    if not _native_music_active():
        _music_service_ctl(db, "stop")

    if vram_mode == "dedicated":
        _current_mode = "video"
        return True

    # Shared (and the single-purpose llm_only/image_only modes): free our other in-process models so
    # the video model has room. The video model itself loads on-demand inside video_service.generate.
    with _swap_lock:
        if _current_mode == "video":
            return True
        try:
            from app.services.llama_service import get_llama_service
            service = get_llama_service(db)
            if service._model is not None:
                logger.info("Unloading LLM to free VRAM for video generation...")
                service.unload_model()
        except Exception as e:
            logger.error(f"Error unloading LLM for video: {e}")
        try:
            from app.services.diffusers_service import get_diffusers_service
            service = get_diffusers_service(db)
            if service.is_loaded():
                logger.info("Unloading image model to free VRAM for video generation...")
                service.unload_model()
        except Exception as e:
            logger.error(f"Error unloading image model for video: {e}")
        _current_mode = "video"
        logger.info("VRAM ready for video generation")
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
